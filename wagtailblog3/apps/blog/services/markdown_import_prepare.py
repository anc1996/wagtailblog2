"""为浏览器 Markdown 导入生成只读的块与媒体引用计划。"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from blog.services.markdown_import_types import MarkdownImportBlock


def serialize_block(block: MarkdownImportBlock) -> dict[str, Any]:
    """把解析块转换为 JSON 可序列化结构，保留 Markdown 原文字符串。"""

    value = block.value
    if hasattr(value, "items"):
        value = dict(value)
    payload: dict[str, Any] = {
        "block_type": block.block_type,
        "value": value,
        "source_start_line": block.source_start_line,
        "source_end_line": block.source_end_line,
    }
    if block.inline_images:
        payload["inline_images"] = [
            {
                "occurrence_id": image.occurrence_id,
                "source": image.source,
                "alt": image.alt,
                "title": image.title,
                "source_kind": image.source_kind,
                "syntax": image.syntax,
                "table_index": image.table_index,
                "row_index": image.row_index,
                "cell_index": image.cell_index,
                "image_index": image.image_index,
            }
            for image in block.inline_images
        ]
    return payload


def _safe_filename(source: str) -> str:
    path = PurePosixPath(urlsplit(source).path)
    name = path.name or "remote-image.bin"
    name = name.split("?")[0].split("#")[0].strip()
    return name[:255] or "remote-image.bin"


def _source_kind(source: str) -> str:
    return "remote_https" if urlsplit(source).scheme.casefold() == "https" else "local"


def build_required_artifacts(
    blocks: tuple[MarkdownImportBlock, ...],
) -> tuple[dict[str, Any], ...]:
    """按正文顺序合并同源引用，生成现有 session manifest 可复用的计划。"""

    grouped: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for block in blocks:
        references: list[tuple[str, str, str, str]] = []
        if block.block_type in {"image_block", "audio_block", "video_block"}:
            if isinstance(block.value, dict):
                references.append(
                    (
                        str(block.value.get("source") or ""),
                        block.block_type.removesuffix("_block"),
                        "block_media",
                        "",
                    )
                )
        references.extend(
            (image.source, "image", "inline_image", image.occurrence_id)
            for image in block.inline_images
        )
        for source, media_type, scope, occurrence_id in references:
            if not source:
                continue
            item = grouped.setdefault(
                source,
                {
                    "position": len(grouped),
                    "media_type": media_type,
                    "source_kind": _source_kind(source),
                    "normalized_source": source,
                    "reference_sources": [],
                    "reference_scope": scope,
                    "occurrence_ids": [],
                    "safe_filename": _safe_filename(source),
                },
            )
            if item["media_type"] != media_type:
                raise ValueError("artifact_media_type_conflict")
            if source not in item["reference_sources"]:
                item["reference_sources"].append(source)
            if item["reference_scope"] != scope:
                item["reference_scope"] = "mixed"
            if occurrence_id:
                item["occurrence_ids"].append(occurrence_id)
    return tuple(grouped.values())


def prepare_summary(
    blocks: tuple[MarkdownImportBlock, ...], markdown: str
) -> dict[str, int]:
    image_count = sum(
        1
        for block in blocks
        for reference in (
            [block.value]
            if block.block_type == "image_block"
            else []
        )
        if isinstance(reference, dict)
    ) + sum(len(block.inline_images) for block in blocks)
    return {
        "block_count": len(blocks),
        "image_count": image_count,
        "markdown_chars": len(markdown),
    }
