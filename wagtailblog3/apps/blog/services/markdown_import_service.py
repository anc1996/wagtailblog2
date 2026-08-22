"""将 Markdown 解析结果和媒体结果组装为 BlogPage 草稿。"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html import escape
from urllib.parse import urlsplit

from blog.services.markdown_import_media import MediaImportResult
from blog.services.markdown_import_parser import EMBED_HOSTS
from blog.services.markdown_import_types import MarkdownImportBlock


class MarkdownImportAssemblyError(ValueError):
    """稳定的组装错误编码，供调用方选择展示或补偿策略。"""
    """解析结果与媒体结果无法组成可靠正文时使用的稳定错误。"""


@dataclass(frozen=True, slots=True)
class DraftImportResult:
    """草稿页面、revision 和 Mongo 草稿指针的成组结果。"""
    page: object
    revision: object
    mongo_draft_pointer: str


@dataclass(frozen=True, slots=True)
class DraftCompensationResult:
    """补偿结果；errors 只记录稳定错误码，不泄漏异常原文。"""
    cleaned: bool
    errors: tuple[str, ...]


def _media_key(block: MarkdownImportBlock) -> str:
    """读取媒体 block 的 source，并拒绝缺失或非映射值。"""
    value = block.value
    if not isinstance(value, Mapping):
        raise MarkdownImportAssemblyError("media_source_invalid")
    source = str(value.get("source") or "")
    if not source:
        raise MarkdownImportAssemblyError("media_source_missing")
    return source


def _chooser_value(result: MediaImportResult) -> int | str:
    """将 Wagtail chooser 对象归一化为可序列化主键。"""
    value = result.value
    primary_key = getattr(value, "pk", None)
    if primary_key is not None:
        return primary_key
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise MarkdownImportAssemblyError("media_object_missing")


def _assemble_media_block(
    block: MarkdownImportBlock,
    result: MediaImportResult,
) -> dict[str, object]:
    """校验媒体结果类型并生成 StreamField block 原始值。"""
    if result.block_type == "markdown_block":
        return {"type": "markdown_block", "value": result.value}
    if result.block_type != block.block_type:
        raise MarkdownImportAssemblyError("media_result_type_mismatch")
    return {"type": block.block_type, "value": _chooser_value(result)}


def _inline_image_replacement(reference: object, result: MediaImportResult) -> str:
    """生成安全的 Wagtail 图片 embed，只保留项目需要的属性。"""
    if result.block_type == "markdown_block":
        return str(result.value)
    if result.block_type != "image_block":
        raise MarkdownImportAssemblyError("inline_image_result_type_mismatch")
    image_id = _chooser_value(result)
    file_field = getattr(result.value, "file", None)
    try:
        preview_url = str(getattr(file_field, "url", "") or "")
    except Exception as exc:
        raise MarkdownImportAssemblyError("inline_image_preview_url_missing") from exc
    if not preview_url:
        raise MarkdownImportAssemblyError("inline_image_preview_url_missing")
    # 只输出项目现有图片嵌入所需属性，原始 style、事件和未知 HTML 属性一律不回写。
    return (
        '<embed embedtype="image" '
        f'id="{image_id}" format="fullwidth_web" '
        f'src="{escape(preview_url, quote=True)}" '
        f'alt="{escape(reference.alt, quote=True)}" />'
    )


def _rewrite_inline_images(
    source: str,
    references: Sequence[object],
    media_results: Mapping[str, MediaImportResult],
) -> str:
    """按偏移倒序替换图片，保证原始偏移不会因前序替换而失效。"""
    rewritten = source
    previous_start = len(source)
    for reference in sorted(references, key=lambda item: item.start_offset, reverse=True):
        if (
            reference.start_offset < 0
            or reference.end_offset > len(source)
            or reference.start_offset >= reference.end_offset
            or reference.end_offset > previous_start
            or source[reference.start_offset:reference.end_offset] != reference.raw
        ):
            raise MarkdownImportAssemblyError("inline_image_location_invalid")
        result = media_results.get(reference.source)
        if result is None:
            raise MarkdownImportAssemblyError("inline_image_result_missing")
        replacement = _inline_image_replacement(reference, result)
        rewritten = (
            rewritten[:reference.start_offset]
            + replacement
            + rewritten[reference.end_offset:]
        )
        previous_start = reference.start_offset
    return rewritten


def assemble_import_body(
    blocks: Sequence[MarkdownImportBlock],
    *,
    media_results: Mapping[str, MediaImportResult],
) -> list[dict[str, object]]:
    """按解析顺序生成可交给 BlogPage StreamField 的原始块数据。"""

    assembled: list[dict[str, object]] = []
    for block in blocks:
        if block.block_type == "markdown_block":
            value = _rewrite_inline_images(
                str(block.value),
                block.inline_images,
                media_results,
            )
            assembled.append({"type": "markdown_block", "value": value})
            continue

        if block.block_type in {"image_block", "audio_block", "video_block"}:
            key = _media_key(block)
            result = media_results.get(key)
            if result is None:
                raise MarkdownImportAssemblyError("media_result_missing")
            assembled.append(_assemble_media_block(block, result))
            continue

        if block.block_type == "embed_block":
            value = block.value
            if not isinstance(value, Mapping):
                raise MarkdownImportAssemblyError("embed_value_invalid")
            url = str(value.get("url") or "")
            title = str(value.get("title") or url)
            parsed = urlsplit(url)
            if not url:
                raise MarkdownImportAssemblyError("embed_url_missing")
            try:
                port = parsed.port
            except ValueError as exc:
                raise MarkdownImportAssemblyError("embed_url_invalid") from exc
            if (
                parsed.scheme.casefold() != "https"
                or (parsed.hostname or "").casefold() not in EMBED_HOSTS
                or parsed.username is not None
                or parsed.password is not None
                or port not in (None, 443)
            ):
                raise MarkdownImportAssemblyError("embed_url_invalid")
            assembled.append(
                {
                    "type": "embed_block",
                    "value": {"title": title, "embed_url": url},
                }
            )
            continue

        if block.block_type == "mermaid_chart":
            value = block.value
            if not isinstance(value, Mapping) or not str(value.get("code") or ""):
                raise MarkdownImportAssemblyError("mermaid_code_missing")
            assembled.append(
                {
                    "type": "mermaid_chart",
                    "value": {
                        "code": str(value["code"]),
                        "renderer": str(value.get("renderer") or "modern-v11.12"),
                    },
                }
            )
            continue

        raise MarkdownImportAssemblyError("unsupported_block_type")
    return assembled


def create_unpublished_blog_draft(
    parent: object,
    *,
    title: str,
    date: object,
    intro: str,
    body_values: Sequence[Mapping[str, object]],
    tags: Sequence[str] = (),
    user: object,
    page_factory: object | None = None,
    log_action: str = "wagtail.create",
) -> DraftImportResult:
    """创建未发布页面并只保存 revision 草稿正文。"""

    if page_factory is None:
        from blog.models import BlogPage

        page_factory = BlogPage
    page = page_factory(title=title, date=date, intro=intro, body=list(body_values))
    page.live = False
    if hasattr(page, "has_unpublished_changes"):
        page.has_unpublished_changes = True
    # BlogPage.save 使用此标记跳过正式 Mongo 内容，只让 revision 保存草稿快照。
    page._markdown_import_draft_only = True
    parent.add_child(instance=page)
    if tags and hasattr(page, "tags"):
        page.tags.set([tag.strip() for tag in tags if str(tag).strip()])
    revision = page.save_revision(user=user, log_action=log_action)
    content = getattr(revision, "content", {}) or {}
    pointer = str(content.get("mongo_draft_pointer") or "")
    if not pointer:
        raise MarkdownImportAssemblyError("mongo_draft_pointer_missing")
    return DraftImportResult(page, revision, pointer)


def compensate_draft_failure(
    *,
    page: object | None,
    mongo_draft_pointer: str,
    media_artifacts: Sequence[object],
    delete_page: object,
    delete_mongo_pointer: object,
    cleanup_media: object,
) -> DraftCompensationResult:
    """按页面、Mongo 指针、媒体依赖顺序执行精确补偿。

    页面删除失败时停止后续依赖清理，避免删除仍可能被页面引用的资源。
    """
    """按页面、Mongo 草稿指针、媒体 artifact 顺序执行精确补偿。"""

    errors: list[str] = []
    page_deleted = True
    if page is not None:
        try:
            delete_page(page)
        except Exception:
            errors.append("page_delete_failed")
            page_deleted = False
    if mongo_draft_pointer and page_deleted:
        try:
            delete_mongo_pointer(mongo_draft_pointer)
        except Exception:
            errors.append("mongo_revision_delete_failed")
    if page_deleted:
        for artifact in media_artifacts:
            try:
                if not cleanup_media(artifact):
                    errors.append("media_cleanup_pending")
            except Exception:
                errors.append("media_cleanup_failed")
    elif media_artifacts:
        # 页面仍可能引用草稿指针和媒体；页面删除失败时不能继续删除其依赖。
        errors.append("compensation_dependency_blocked")
    return DraftCompensationResult(not errors, tuple(errors))
