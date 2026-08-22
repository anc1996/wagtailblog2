"""博客公开RSS和Atom订阅源。"""

from __future__ import annotations

import hashlib
import logging
from io import BytesIO
from typing import Any

from django.conf import settings
from django.contrib.syndication.views import Feed
from django.http import HttpResponse, HttpResponseNotAllowed, HttpResponsePermanentRedirect
from django.utils.cache import get_conditional_response
from django.utils.http import http_date
from django.utils.feedgenerator import Atom1Feed

from blog.services.feed_cache import BlogFeedCache
from blog.services.feed_query import BlogFeedContext, BlogFeedEntry, BlogFeedQueryService
from blog.services.feed_analytics import FeedRequestRecorder

logger = logging.getLogger(__name__)


class BaseBlogFeed(Feed):
    """提供缓存、条件请求和公开文章字段的订阅源基类。

    Feed 生成只消费查询服务返回的公开元数据，不读取正文或 Mongo 草稿；响应先按站点、
    语言、范围、来源和格式缓存，再通过 ETag/Last-Modified 处理条件请求。GET/HEAD 之外
    的方法被拒绝，追踪查询参数重定向到无参数规范 URL，避免缓存键碎片化。
    """

    feed_format = "rss"

    def __call__(self, request: Any, *args: Any, **kwargs: Any) -> HttpResponse:
        """执行 Feed 请求、缓存命中/生成和条件响应协商。"""
        if request.method not in {"GET", "HEAD"}:
            return HttpResponseNotAllowed(["GET", "HEAD"])
        if request.GET:
            # Feed没有筛选语义，统一丢弃追踪参数以避免缓存键碎片化。
            return HttpResponsePermanentRedirect(request.path)

        context = self.get_object(request, *args, **kwargs)
        cache = BlogFeedCache()
        generation, payload = cache.get_payload(
            context.scope,
            self.feed_format,
            context.origin,
        )
        if payload:
            logger.info(
                "feed_cache_hit site_id=%s locale_id=%s format=%s generation=%s",
                context.site.pk,
                context.locale.pk,
                self.feed_format,
                generation,
            )
            return self._response_from_payload(request, payload, context)

        logger.info(
            "feed_cache_miss site_id=%s locale_id=%s format=%s generation=%s",
            context.site.pk,
            context.locale.pk,
            self.feed_format,
            generation,
        )
        feed = self.get_feed(context, request)
        output = BytesIO()
        feed.write(output, "utf-8")
        xml = output.getvalue()
        last_modified = self._latest_entry_time(feed.items)
        payload = {
            "xml": xml,
            "content_type": feed.content_type,
            "last_modified": last_modified,
            "etag": self._etag(xml),
        }
        cache.set_payload(
            context.scope,
            generation,
            self.feed_format,
            context.origin,
            payload,
        )
        return self._response_from_payload(request, payload, context)

    @staticmethod
    def _etag(xml: bytes) -> str:
        return f'"{hashlib.sha256(xml).hexdigest()}"'

    @staticmethod
    def _latest_entry_time(items: list[dict[str, Any]]) -> Any:
        """从 Feed 项目读取最新更新时间，供 Last-Modified 使用。"""
        dates = []
        for item in items:
            for field in ("updateddate", "pubdate"):
                value = item.get(field)
                if value is not None:
                    dates.append(value)
        return max(dates) if dates else None

    def _response_from_payload(
        self, request: Any, payload: dict[str, Any], context: BlogFeedContext
    ) -> HttpResponse:
        """将缓存 payload 转换为 GET/HEAD 响应并附加条件缓存头。"""
        xml = payload["xml"]
        response = HttpResponse(
            b"" if request.method == "HEAD" else xml,
            content_type=payload["content_type"],
        )
        if request.method == "HEAD":
            response["Content-Length"] = str(len(xml))
        response["Cache-Control"] = (
            f"public, max-age={getattr(settings, 'BLOG_FEED_CLIENT_MAX_AGE', 60)}"
        )
        response["ETag"] = payload["etag"]
        last_modified = payload.get("last_modified")
        if last_modified is not None:
            response["Last-Modified"] = http_date(last_modified.timestamp())
        response = get_conditional_response(
            request,
            etag=payload["etag"],
            last_modified=last_modified,
            response=response,
        )
        if getattr(settings, "BLOG_FEED_ANALYTICS_ENABLED", True):
            FeedRequestRecorder.record(request, response, context.scope, self.feed_format)
        return response

    def get_object(self, request: Any, *args: Any, **kwargs: Any) -> BlogFeedContext:
        """构造全局范围的站点/语言 Feed 上下文。"""
        return BlogFeedQueryService.build_context(request)

    def title(self, context: BlogFeedContext) -> str:
        """生成 RSS/Atom 频道标题。"""
        site_name = context.site.site_name or getattr(settings, "WAGTAIL_SITE_NAME", "博客")
        if context.scope_type == "tag":
            return f"{site_name} - 标签：{context.scope_label}"
        if context.scope_type == "author":
            return f"{site_name} - 作者：{context.scope_label}"
        return f"{site_name} - 最新文章"

    def description(self, context: BlogFeedContext) -> str:
        """生成频道描述，不包含正文内容。"""
        if context.scope_type == "tag":
            return f"标签“{context.scope_label}”下最新发布的博客文章"
        if context.scope_type == "author":
            return f"作者“{context.scope_label}”最新发布的博客文章"
        return "最新发布的博客文章"

    def link(self, context: BlogFeedContext) -> str:
        """返回站点根链接。"""
        return f"{context.origin}/"

    def feed_url(self, context: BlogFeedContext) -> str:
        """返回当前请求对应的规范订阅 URL。"""
        return context.feed_url

    def feed_guid(self, context: BlogFeedContext) -> str:
        """生成按站点、语言、范围和格式稳定区分的频道 GUID。"""
        return (
            f"urn:wagtailblog:feed:{context.site.pk}:{context.locale.pk}:"
            f"{context.scope_type}:{context.scope_id}:{self.feed_format}"
        )

    def items(self, context: BlogFeedContext) -> list[BlogFeedEntry]:
        """读取当前范围的公开文章元数据。"""
        return BlogFeedQueryService.list_entries(context)

    def item_title(self, item: BlogFeedEntry) -> str:
        return item.title

    def item_description(self, item: BlogFeedEntry) -> str:
        return item.summary

    def item_link(self, item: BlogFeedEntry) -> str:
        return item.url

    def item_guid(self, item: BlogFeedEntry) -> str:
        return item.guid

    def item_guid_is_permalink(self, item: BlogFeedEntry) -> bool:
        return False

    def item_pubdate(self, item: BlogFeedEntry) -> Any:
        return item.published_at

    def item_updateddate(self, item: BlogFeedEntry) -> Any:
        return item.updated_at

    def item_author_name(self, item: BlogFeedEntry) -> str | None:
        return "、".join(item.authors) if item.authors else None

    def item_categories(self, item: BlogFeedEntry) -> tuple[str, ...]:
        return item.categories


class BlogRssFeed(BaseBlogFeed):
    """RSS 2.0订阅源。"""

    feed_format = "rss"


class BlogAtomFeed(BaseBlogFeed):
    """Atom 1.0订阅源。"""

    feed_format = "atom"
    feed_type = Atom1Feed

    def subtitle(self, context: BlogFeedContext) -> str:
        return self.description(context)


class BaseScopedBlogFeed(BaseBlogFeed):
    """让范围路由只负责传入 slug，具体对象解析统一留在查询服务。"""

    scope_type = "global"

    def get_object(self, request: Any, *args: Any, **kwargs: Any) -> BlogFeedContext:
        """按范围类型把 tag/author slug 委托给查询服务解析。"""
        if self.scope_type == "tag":
            return BlogFeedQueryService.build_tag_context(request, kwargs["tag_slug"])
        if self.scope_type == "author":
            return BlogFeedQueryService.build_author_context(request, kwargs["author_slug"])
        return super().get_object(request, *args, **kwargs)


class TagBlogRssFeed(BaseScopedBlogFeed, BlogRssFeed):
    """标签范围的RSS 2.0订阅源。"""

    scope_type = "tag"


class TagBlogAtomFeed(BaseScopedBlogFeed, BlogAtomFeed):
    """标签范围的Atom 1.0订阅源。"""

    scope_type = "tag"


class AuthorBlogRssFeed(BaseScopedBlogFeed, BlogRssFeed):
    """作者范围的RSS 2.0订阅源。"""

    scope_type = "author"


class AuthorBlogAtomFeed(BaseScopedBlogFeed, BlogAtomFeed):
    """作者范围的Atom 1.0订阅源。"""

    scope_type = "author"
