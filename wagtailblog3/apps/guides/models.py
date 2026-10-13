"""
专题指南数据模型与后台交互定义。

核心模型：
1. GuideTopic: 指南主题 Wagtail Snippet，采用唯一 slug 标识与 PROTECT 删除保护；
2. GuideIndexPage: 专题指南索引页，原生 MySQL Page，提供主题/难度筛选与带核查日期优先排序；
3. GuideRelatedPage: 有序相关文章关联（ParentalKey + Orderable + Page chooser）；
4. GuidePage: 专题指南详情页，接入 ExternalContentPageMixin 仅 body 外置至 MongoDB；
5. GuidePageForm: 定制后台表单，实现草稿容忍未完成结构、公开发布强制要求主题与步骤的业务门禁。
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict, List, Optional

from django import forms
from django.conf import settings
from django.core.paginator import Paginator
from django.db import models
from modelcluster.fields import ParentalKey
from wagtail.blocks import RichTextBlock, StreamBlock, StructBlock
from wagtail.admin.panels import FieldPanel, InlinePanel, MultiFieldPanel
from wagtail.fields import StreamField
from wagtail.models import Orderable, Page
from wagtail.snippets.models import register_snippet

from external_content.exceptions import PublicationRevisionInvalidError
from external_content.forms import ExternalContentPageForm
from external_content.mixins import ExternalContentPageMixin
from guides.blocks import GuideStreamBlock


@register_snippet
class GuideTopic(models.Model):
    """
    专题指南主题片段 (Snippet)。

    作为指南的核心分类维度，具备唯一稳定 slug，支持后台由编辑维护词表。
    被 GuidePage 引用时受 models.PROTECT 约束，禁止物理级联删除以防业务数据悬空。
    """

    name = models.CharField(
        max_length=100,
        verbose_name="主题名称",
        help_text="展示在前端与管理后台的主题名称，如：安全加固、云原生运维",
    )
    slug = models.SlugField(
        max_length=100,
        unique=True,
        allow_unicode=True,
        verbose_name="标识符",
        help_text="用于 URL 过滤与稳定分类标识，如：security、cloud-native",
    )
    description = models.TextField(
        blank=True,
        verbose_name="主题描述",
        help_text="主题面向场景与简要介绍",
    )

    panels = [
        FieldPanel("name"),
        FieldPanel("slug"),
        FieldPanel("description"),
    ]

    class Meta:
        verbose_name = "指南主题"
        verbose_name_plural = "指南主题"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class GuideIndexPage(Page):
    """
    专题指南索引入口页。

    原生 Wagtail Page，完全归属 MySQL，不继承 ExternalContentPageMixin，不触发 MongoDB 写入。
    列表展示当前站点的已发布公开指南，支持主题筛选、难度筛选与稳定分页。
    """

    subpage_types: List[str] = ["guides.GuidePage"]
    parent_page_types: List[str] = ["home.HomePage", "wagtailcore.Page"]

    intro = models.TextField(
        blank=True,
        verbose_name="导语介绍",
        help_text="显示在专题指南列表顶部的引导文案",
    )

    content_panels = Page.content_panels + [
        FieldPanel("intro"),
    ]

    class Meta:
        verbose_name = "专题指南索引页"
        verbose_name_plural = "专题指南索引页"

    def get_context(self, request: Any, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        """
        构建列表页上下文：执行主题筛选、难度筛选与带核查日期优先的倒序稳定排序。
        """
        context = super().get_context(request, *args, **kwargs)

        # 仅查询当前索引页下的已发布且公开的 GuidePage
        guides_qs = (
            GuidePage.objects.child_of(self)
            .filter(locale=self.locale)
            .live()
            .public()
            .select_related("topic", "cover_image")
        )

        # 1. 主题筛选
        current_topic = request.GET.get("topic", "").strip()
        if current_topic:
            guides_qs = guides_qs.filter(topic__slug=current_topic)

        # 2. 难度等级筛选
        current_difficulty = request.GET.get("difficulty", "").strip()
        valid_difficulties = {k for k, _ in GuidePage.DIFFICULTY_CHOICES}
        if current_difficulty in valid_difficulties:
            guides_qs = guides_qs.filter(difficulty=current_difficulty)

        # 3. 稳定排序：有核查日期优先，再按核查日期倒序、首次发布时间倒序、ID 倒序
        guides_qs = guides_qs.order_by(
            models.F("last_reviewed_at").desc(nulls_last=True),
            "-first_published_at",
            "-id",
        )

        # 4. 分页处理（每页 10 篇）
        paginator = Paginator(guides_qs, 10)
        page_num = request.GET.get("page", 1)
        guides_page = paginator.get_page(page_num)

        context["guides"] = guides_page
        context["topics"] = GuideTopic.objects.all()
        context["current_topic"] = current_topic
        context["current_difficulty"] = current_difficulty
        context["difficulties"] = GuidePage.DIFFICULTY_CHOICES
        return context


class GuidePageForm(ExternalContentPageForm):
    """
    专题指南后台编辑表单。

    实现草稿阶段与发布阶段的差异化验证门禁：
    1. 草稿阶段（is_deferred_validation=True）：允许空主题与空步骤块，方便编辑暂存未完成构思；
    2. 发布阶段（is_deferred_validation=False）：强制校验必须指定有效主题且至少包含一个操作步骤。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._guide_body_baseline: Optional[List[Dict[str, Any]]] = None
        if self.is_bound and "body" in self.fields:
            # 使用当前编辑/历史 Revision 实例，不回查 live/latest；读取失败照常关闭。
            # _post_clean 会给 instance 赋新值，必须提前冻结，且不得原地改写旧正文。
            field = self.instance._meta.get_field("body")
            self._guide_body_baseline = deepcopy(
                field.stream_block.get_prep_value(self.instance.body)
            )

    @staticmethod
    def _matches_editor_roundtrip(block: Any, original: Any, submitted: Any) -> bool:
        """确认整份指南仅发生编辑器往返，不把双向归一化相等当作未编辑。

        submitted 已是 widget 清洗后的数据库 HTML；其重解析结果仅供读取段落 key，
        最终仍须与原文单向往返结果逐字节相等，否则必须保留用户的新正文。
        """
        if original == submitted:
            return True
        if isinstance(block, RichTextBlock):
            # 转换器加载 Page 模型，只能在 apps ready 后的表单阶段导入。
            from wagtail.admin.rich_text.editors.draftail import DraftailRichTextArea

            widget = block.field.widget
            if not isinstance(widget, DraftailRichTextArea):
                return False
            expected_state = json.loads(widget.format_value(original))
            submitted_state = json.loads(widget.format_value(submitted))
            if len(expected_state["blocks"]) != len(submitted_state["blocks"]):
                return False
            # Wagtail 8 会为旧 HTML 无 key 的段落随机生成 data-block-key。
            # 只对齐编辑器段落身份；仍把原文单向转换后的完整 HTML 与提交字节比较，
            # 不对提交内容做空白折叠或语义归一化，文字/链接/样式/结构差异不会被吞掉。
            for expected, actual in zip(expected_state["blocks"], submitted_state["blocks"]):
                expected["key"] = actual["key"]
            return widget.converter.to_database_format(json.dumps(expected_state)) == submitted
        if isinstance(block, StructBlock):
            return (
                isinstance(original, dict) and isinstance(submitted, dict)
                and original.keys() == submitted.keys()
                and all(
                    GuidePageForm._matches_editor_roundtrip(
                        block.child_blocks[name], original[name], submitted[name],
                    )
                    for name in original
                )
            )
        if isinstance(block, StreamBlock):
            if not isinstance(original, list) or not isinstance(submitted, list):
                return False
            if len(original) != len(submitted):
                return False
            for before, after in zip(original, submitted):
                # 块 ID、类型、顺序及信封字段必须一致；非富文本值只能精确相等。
                if {k: v for k, v in before.items() if k != "value"} != {
                    k: v for k, v in after.items() if k != "value"
                }:
                    return False
                if not GuidePageForm._matches_editor_roundtrip(
                    block.child_blocks[before["type"]], before["value"], after["value"],
                ):
                    return False
            return True
        return False

    def clean(self) -> Dict[str, Any]:
        cleaned_data = super().clean()

        baseline = getattr(self, "_guide_body_baseline", None)
        if baseline is not None and "body" in cleaned_data:
            field = self.instance._meta.get_field("body")
            submitted = field.stream_block.get_prep_value(cleaned_data["body"])
            if self._matches_editor_roundtrip(field.stream_block, baseline, submitted):
                # 整体等于原文经编辑器往返才还原；任何真实改动均保留整份新输入。
                cleaned_data["body"] = field.to_python(deepcopy(baseline))

        if not getattr(self, "is_deferred_validation", False):
            # 校验主题
            topic = cleaned_data.get("topic")
            if not topic:
                self.add_error("topic", forms.ValidationError("发布前必须选择指南主题。"))

            # 校验至少包含一个 step 块
            body = cleaned_data.get("body")
            has_step = False
            if body:
                for block in body:
                    b_type = getattr(block, "block_type", None) or (
                        block.get("type") if isinstance(block, dict) else None
                    )
                    if b_type == "step":
                        has_step = True
                        break
            if not has_step:
                self.add_error("body", forms.ValidationError("公开发布前指南正文必须至少包含一个操作步骤 (step) 块。"))

        return cleaned_data


class GuidePage(ExternalContentPageMixin, Page):
    """
    专题指南详情页面。

    继承 ExternalContentPageMixin，仅 body 字段外置至 MongoDB external_content_versions；
    其余基础元数据（summary、topic、difficulty、last_reviewed_at、cover_image、related_pages）
    均严格留存在 MySQL 与 Wagtail 原生 Revision 中。
    """

    base_form_class = GuidePageForm

    # 正文外置策略声明
    content_storage = {"body": "mongo"}
    content_storage_schema = 1
    content_search = {"type": "guides", "label": "专题指南"}

    subpage_types: List[str] = []
    parent_page_types: List[str] = ["guides.GuideIndexPage"]

    summary = models.TextField(
        max_length=500,
        blank=True,
        verbose_name="指南摘要",
        help_text="简短纯文本摘要（不超过500字），用于列表卡片、SEO描述与分享导语",
    )
    topic = models.ForeignKey(
        GuideTopic,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="guides",
        verbose_name="指南主题",
        help_text="所属技术主题。保存草稿可暂不填，公开发布前必须选择有效主题",
    )

    DIFFICULTY_CHOICES = [
        ("beginner", "入门"),
        ("intermediate", "进阶"),
        ("advanced", "深入"),
    ]
    difficulty = models.CharField(
        max_length=20,
        choices=DIFFICULTY_CHOICES,
        default="beginner",
        verbose_name="难度等级",
        help_text="指南面向的技术受众与操作深度",
    )
    last_reviewed_at = models.DateField(
        null=True,
        blank=True,
        verbose_name="最后核查日期",
        help_text="由技术负责人人工复核内容的有效性日期；不得由系统普通保存自动触发修改",
    )
    cover_image = models.ForeignKey(
        settings.WAGTAILIMAGES_IMAGE_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="封面图片",
        help_text="可选的指南封面大图，用于列表与详情顶部展示",
    )
    body = StreamField(
        GuideStreamBlock(),
        use_json_field=True,
        verbose_name="指南正文",
        help_text="支持导读、步骤、代码、提示、FAQ、参考资料与原始 Markdown 七类块",
    )

    content_panels = Page.content_panels + [
        MultiFieldPanel(
            [
                FieldPanel("summary"),
                FieldPanel("topic"),
                FieldPanel("difficulty"),
                FieldPanel("last_reviewed_at"),
                FieldPanel("cover_image"),
            ],
            heading="指南基础信息",
        ),
        FieldPanel("body"),
        InlinePanel("related_pages", label="相关文章与阅读"),
    ]

    class Meta:
        verbose_name = "专题指南"
        verbose_name_plural = "专题指南"

    def validate_publication_revision(self, revision: Any) -> None:
        """
        针对待发布的锁定 Revision 执行严格业务门禁校验：
        1. 必须关联有效主题 (GuideTopic)；
        2. 正文必须包含至少一个操作步骤 (step 块)。
        校验依据严格基于 Revision 内容快照，防止通过后台原生 Action 绕过或发生草稿漂移。
        """
        content_dict = (
            revision.content
            if isinstance(revision.content, dict)
            else json.loads(revision.content)
        )

        # 1. 检查主题关联及其在数据库中的真实有效性
        topic_id = content_dict.get("topic_id") or content_dict.get("topic")
        if not topic_id:
            raise PublicationRevisionInvalidError(
                "专题指南公开发布前必须关联有效的主题 (GuideTopic)",
                details={"page_id": self.pk, "revision_id": revision.pk},
            )
        if not GuideTopic.objects.filter(pk=topic_id).exists():
            raise PublicationRevisionInvalidError(
                f"专题指南关联的主题不存在或已失效 (topic_id={topic_id})",
                details={"page_id": self.pk, "revision_id": revision.pk, "topic_id": topic_id},
            )

        # 2. 检查正文至少包含一个 step 块
        manifest_dict = content_dict.get("_external_content_manifest", {})
        has_step = False

        if manifest_dict and "fields" in manifest_dict and "body" in manifest_dict.get("fields", {}):
            rev_page = revision.as_object()
            body_val = getattr(rev_page, "body", None)
            if body_val:
                for block in body_val:
                    b_type = getattr(block, "block_type", None) or (
                        block.get("type") if isinstance(block, dict) else None
                    )
                    if b_type == "step":
                        has_step = True
                        break
        else:
            raw_body = content_dict.get("body")
            if isinstance(raw_body, str):
                try:
                    raw_body = json.loads(raw_body)
                except Exception:
                    raw_body = []
            if isinstance(raw_body, list):
                for block in raw_body:
                    if isinstance(block, dict) and block.get("type") == "step":
                        has_step = True
                        break

        if not has_step:
            raise PublicationRevisionInvalidError(
                "专题指南发布失败：正文必须至少包含一个操作步骤 (step) 块",
                details={"page_id": self.pk, "revision_id": revision.pk},
            )

    def publish(self, revision: Any = None, *args: Any, **kwargs: Any) -> Any:
        """
        公开发布前门禁拦截：
        针对锁定的待发布 Revision 执行完整业务完整性校验。
        """
        target_rev = revision or self.get_latest_revision()
        if target_rev is not None:
            self.validate_publication_revision(target_rev)
        return super().publish(revision, *args, **kwargs)

    def get_context(self, request: Any, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        """
        构建详情页上下文：
        1. 提取并生成步骤目录索引 (TOC)，并为每个 step 块设置确定性的 anchor_id；
        2. 动态过滤并返回可见的相关页面 (visible_related_pages)：
           严格核验目标及其祖先的 privacy 限制、Locale 一致性与当前 Site 树归属。
        """
        context = super().get_context(request, *args, **kwargs)

        # 1. 从 body 中提取 steps 生成目录 (TOC) 并绑定 anchor_id
        toc = []
        if self.body:
            step_idx = 1
            for block in self.body:
                b_type = getattr(block, "block_type", None) or (
                    block.get("type") if isinstance(block, dict) else None
                )
                if b_type == "step":
                    val = getattr(block, "value", block)
                    title = val.get("title") if isinstance(val, dict) else getattr(val, "title", "")
                    block_id = getattr(block, "id", None) or (
                        block.get("id") if isinstance(block, dict) else None
                    )
                    clean_id = block_id or f"step-{step_idx}"
                    try:
                        object.__setattr__(block, "anchor_id", clean_id)
                    except Exception:
                        try:
                            setattr(block, "anchor_id", clean_id)
                        except Exception:
                            if isinstance(block, dict):
                                block["anchor_id"] = clean_id

                    toc.append(
                        {
                            "id": clean_id,
                            "title": title or f"步骤 {step_idx}",
                            "number": step_idx,
                        }
                    )
                    step_idx += 1
        context["toc"] = toc

        # 2. 动态过滤可见的相关页面（核验 live、Locale、当前 Site 与全祖先 Privacy Restrictions）
        visible_related: List[Page] = []
        related_objs = (
            self.related_pages.select_related("related_page").all()
            if hasattr(self, "related_pages")
            else []
        )
        current_site = self.get_site()
        from wagtail.models import PageViewRestriction

        for item in related_objs:
            target = item.related_page.specific
            if not getattr(target, "live", False):
                continue

            # 核验 Locale 匹配
            if getattr(target, "locale_id", None) and getattr(self, "locale_id", None):
                if target.locale_id != self.locale_id:
                    continue

            # 核验当前 Site 树归属
            if current_site and current_site.root_page:
                if target.pk != current_site.root_page_id and not target.is_descendant_of(current_site.root_page):
                    continue

            # 核验自身及所有祖先页面的隐私访问限制
            if target.get_view_restrictions().exists():
                continue

            visible_related.append(target)

        context["visible_related_pages"] = visible_related
        return context


class GuideRelatedPage(Orderable):
    """
    指南相关页面关联表。

    使用 ParentalKey 绑定父级 GuidePage，支持通过 PageChooserPanel 选择内部 Page，
    并在前台按排序输出。
    """

    page = ParentalKey(
        GuidePage,
        on_delete=models.CASCADE,
        related_name="related_pages",
    )
    related_page = models.ForeignKey(
        "wagtailcore.Page",
        on_delete=models.CASCADE,
        related_name="+",
        verbose_name="相关页面",
        help_text="选择推荐给读者的相关技术文章或指南",
    )

    panels = [
        FieldPanel("related_page"),
    ]

    class Meta(Orderable.Meta):
        verbose_name = "相关页面关联"
        verbose_name_plural = "相关页面关联"
