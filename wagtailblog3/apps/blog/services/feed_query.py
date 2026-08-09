"""RSS和Atom共用的博客文章查询服务。"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urljoin

from django.conf import settings
from django.http import Http404, HttpRequest
from django.utils.html import strip_tags
from django.utils.translation import get_language
from wagtail.models import Locale, Site

from blog.inline_title_renderer import InlineTitleRenderer
from blog.models import Author, BlogPage
from blog.services.feed_cache import BlogFeedScope
from taggit.models import Tag

logger = logging.getLogger(__name__)

_INVALID_XML_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


@dataclass(frozen=True)
class BlogFeedContext:
    """一次Feed请求的站点、语言和规范URL上下文。"""

    request: HttpRequest
    site: Site
    locale: Locale
    origin: str
    feed_url: str
    scope_type: str = "global"
    scope_id: int = 0
    scope_slug: str = ""
    scope_label: str = ""

    @property
    def scope(self) -> BlogFeedScope:
        return BlogFeedScope(
            site_id=self.site.pk,
            locale_id=self.locale.pk,
            scope_type=self.scope_type,
            scope_id=self.scope_id,
            scope_slug=self.scope_slug,
            scope_label=self.scope_label,
        )


@dataclass(frozen=True)
class BlogFeedEntry:
    """Feed生成所需的公开文章元数据，永远不携带正文。"""

    title: str
    summary: str
    url: str
    guid: str
    published_at: datetime
    updated_at: datetime
    authors: tuple[str, ...]
    categories: tuple[str, ...]


class BlogFeedQueryService:
    """集中处理Feed可见性、文本清理和关系预取规则。"""

    @classmethod
    def build_context(
        cls,
        request: HttpRequest,
        *,
        scope_type: str = "global",
        scope_id: int = 0,
        scope_slug: str = "",
        scope_label: str = "",
    ) -> BlogFeedContext:
        """将请求语言映射到Wagtail Locale，缺失时返回404而非混用默认语言。"""

        language_code = get_language() or settings.LANGUAGE_CODE
        try:
            locale = Locale.objects.get(language_code=language_code)
        except Locale.DoesNotExist as error:
            raise Http404("当前语言未配置订阅源") from error

        site = Site.find_for_request(request)
        if site is None:
            raise Http404("当前站点未配置订阅源")
        # request.build_absolute_uri 会先经过 Django 的 ALLOWED_HOSTS 校验；不直接信任未校验Host。
        origin = request.build_absolute_uri("/").rstrip("/")
        return BlogFeedContext(
            request=request,
            site=site,
            locale=locale,
            origin=origin,
            feed_url=f"{origin}{request.path}",
            scope_type=scope_type,
            scope_id=scope_id,
            scope_slug=scope_slug,
            scope_label=scope_label,
        )

    @classmethod
    def build_tag_context(cls, request: HttpRequest, tag_slug: str) -> BlogFeedContext:
        """解析Taggit稳定slug；对象不存在时明确返回404。"""

        try:
            tag = Tag.objects.get(slug=tag_slug)
        except Tag.DoesNotExist as error:
            raise Http404("指定标签不存在") from error
        return cls.build_context(
            request,
            scope_type="tag",
            scope_id=tag.pk,
            scope_slug=tag.slug,
            scope_label=tag.name,
        )

    @classmethod
    def build_author_context(
        cls,
        request: HttpRequest,
        author_slug: str,
    ) -> BlogFeedContext:
        """作者订阅地址只依赖稳定slug，作者改名不会使既有订阅失效。"""

        try:
            author = Author.objects.get(slug=author_slug)
        except Author.DoesNotExist as error:
            raise Http404("指定作者不存在") from error
        return cls.build_context(
            request,
            scope_type="author",
            scope_id=author.pk,
            scope_slug=author.slug,
            scope_label=author.name,
        )

    @classmethod
    def list_entries(cls, context: BlogFeedContext) -> list[BlogFeedEntry]:
        """查询有限数量的公开文章，并避免读取MongoDB正文。"""

        limit = getattr(settings, "BLOG_FEED_ITEM_LIMIT", 20)
        filters: dict[str, object] = {
            "locale": context.locale,
            "first_published_at__isnull": False,
        }
        if context.scope_type == "tag":
            filters["tags__pk"] = context.scope_id
        elif context.scope_type == "author":
            filters["authors__pk"] = context.scope_id

        pages = (
            BlogPage.objects.live()
            .public()
            .in_site(context.site)
            .filter(**filters)
            .defer("body", "mongo_content_id")
            .prefetch_related("authors", "categories", "tags")
            .order_by("-first_published_at", "-pk")[:limit]
        )

        entries: list[BlogFeedEntry] = []
        for page in pages:
            entry = cls._build_entry(page, context)
            if entry is not None:
                entries.append(entry)
        return entries

    @classmethod
    def _build_entry(
        cls,
        page: BlogPage,
        context: BlogFeedContext,
    ) -> BlogFeedEntry | None:
        page_url = page.get_url(request=context.request, current_site=context.site)
        if not page_url:
            logger.warning("feed_entry_skipped page_id=%s reason=unroutable", page.pk)
            return None
        url = urljoin(f"{context.origin}/", page_url)

        published_at = page.first_published_at
        if published_at is None:
            logger.warning("feed_entry_skipped page_id=%s reason=missing_publish_time", page.pk)
            return None

        title = cls._title_text(page.title)
        summary = cls._summary_text(page.intro)
        authors = cls._unique_text(item.name for item in page.authors.all())
        categories = cls._unique_text(
            [item.name for item in page.categories.all()]
            + [item.name for item in page.tags.all()]
        )
        return BlogFeedEntry(
            title=title,
            summary=summary,
            url=url,
            guid=(
                f"urn:wagtailblog:blogpage:{page.translation_key}:{page.locale_id}"
            ),
            published_at=published_at,
            updated_at=page.last_published_at or published_at,
            authors=authors,
            categories=categories,
        )

    @staticmethod
    def _normalise_text(value: object) -> str:
        """删除HTML和非法控制字符，保留Feed需要的可读纯文本。"""

        text = html.unescape(strip_tags(str(value or "")))
        text = _INVALID_XML_CHARS.sub("", text)
        return " ".join(text.split())

    @classmethod
    def _title_text(cls, title: str) -> str:
        try:
            return cls._normalise_text(InlineTitleRenderer.plain_text(title))
        except Exception:
            # 非法Markdown标题不能让整个订阅源失败，回退为已转义的原始标题文本。
            logger.warning("feed_title_plain_text_fallback")
            return cls._normalise_text(title)

    @classmethod
    def _summary_text(cls, intro: object) -> str:
        summary = cls._normalise_text(intro)
        if not summary:
            return "点击阅读全文"
        maximum = getattr(settings, "BLOG_FEED_SUMMARY_LENGTH", 300)
        if len(summary) > maximum:
            return f"{summary[:maximum].rstrip()}..."
        return summary

    @classmethod
    def _unique_text(cls, values) -> tuple[str, ...]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            text = cls._normalise_text(value)
            if text and text not in seen:
                seen.add(text)
                result.append(text)
        return tuple(result)
