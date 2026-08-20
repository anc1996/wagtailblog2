from dataclasses import dataclass
from typing import Mapping, TypeAlias


BlockValue: TypeAlias = str | Mapping[str, str]


@dataclass(frozen=True, slots=True)
class MarkdownInlineImage:
    """记录表格内图片在 Markdown 原文中的稳定位置，不携带文件或数据库对象。"""

    occurrence_id: str
    source: str
    alt: str
    title: str
    source_kind: str
    syntax: str
    start_offset: int
    end_offset: int
    raw: str
    table_index: int
    row_index: int
    cell_index: int
    image_index: int


@dataclass(frozen=True, slots=True)
class MarkdownImportBlock:
    """描述解析阶段的一个有序块，不包含数据库或存储对象。"""

    block_type: str
    value: BlockValue
    source_start_line: int
    source_end_line: int
    inline_images: tuple[MarkdownInlineImage, ...] = ()
