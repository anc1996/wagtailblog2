"""Markdown 导入解析阶段使用的不可变块和行内图片数据对象。"""

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
    """描述解析阶段的一个有序块，不包含数据库或存储对象。

    ``value`` 保留可序列化的块内容，``source_*_line`` 用于错误定位，
    ``inline_images`` 保存同一块内图片的稳定出现顺序；该对象只在解析/准备阶段存活。
    """

    block_type: str
    value: BlockValue
    source_start_line: int
    source_end_line: int
    inline_images: tuple[MarkdownInlineImage, ...] = ()
