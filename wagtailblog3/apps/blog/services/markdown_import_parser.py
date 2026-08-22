"""Markdown 导入解析：保持原文结构并提取可导入的媒体块。"""

from dataclasses import dataclass, replace
from html.parser import HTMLParser
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token

from blog.services.markdown_import_types import MarkdownImportBlock, MarkdownInlineImage


MERMAID_RENDERER = "modern-v11.12"
EMBED_HOSTS = frozenset(
    {
        "b23.tv",
        "bilibili.com",
        "www.bilibili.com",
        "youtube.com",
        "www.youtube.com",
        "youtu.be",
        "music.163.com",
        "y.qq.com",
        "music.taihe.com",
        "www.kugou.com",
        "kugou.com",
        "music.migu.cn",
    }
)


@dataclass(frozen=True, slots=True)
class _ExtractedBlock:
    """解析出的独立 block，记录原文行范围以保持源顺序。"""
    start_line: int
    end_line: int
    block_type: str
    value: dict[str, str]


@dataclass(frozen=True, slots=True)
class _PendingInlineImage:
    """待替换的表格内图片；偏移用于倒序替换，避免位置漂移。"""
    source: str
    alt: str
    title: str
    source_kind: str
    syntax: str
    start_offset: int
    end_offset: int
    raw: str
    table_start_offset: int
    row_index: int
    cell_index: int


def _line_offsets(source: str) -> list[int]:
    offsets = [0]
    for index, character in enumerate(source):
        if character == "\n":
            offsets.append(index + 1)
    return offsets


class _TableHTMLImageParser(HTMLParser):
    """定位真实 HTML 表格中的图片标签，忽略 script/style 内容。"""
    """只定位真实 HTML 表格中的图片标签，保留原文偏移供后续定点替换。"""

    def __init__(self, source: str, *, base_offset: int = 0) -> None:
        super().__init__(convert_charrefs=True)
        self.source = source
        self.base_offset = base_offset
        self.offsets = _line_offsets(source)
        self.tables: list[dict[str, int]] = []
        self.images: list[_PendingInlineImage] = []
        self.ignored_depth = 0

    def _offset(self) -> int:
        line, column = self.getpos()
        return self.base_offset + self.offsets[line - 1] + column

    def _record_image(self, attrs: list[tuple[str, str | None]]) -> None:
        if not self.tables or self.ignored_depth:
            return
        table = self.tables[-1]
        if table["row"] < 1 or table["cell"] < 1:
            return
        values = dict(attrs)
        source = str(values.get("src") or "").strip()
        raw = self.get_starttag_text() or ""
        if not source or not raw:
            return
        start = self._offset()
        self.images.append(
            _PendingInlineImage(
                source=source,
                alt=str(values.get("alt") or ""),
                title=str(values.get("title") or ""),
                source_kind=_source_kind(source),
                syntax="html",
                start_offset=start,
                end_offset=start + len(raw),
                raw=raw,
                table_start_offset=table["start"],
                row_index=table["row"],
                cell_index=table["cell"],
            )
        )

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.casefold()
        if tag == "table":
            self.tables.append({"start": self._offset(), "row": 0, "cell": 0})
            return
        if not self.tables:
            return
        if tag in {"script", "style"}:
            self.ignored_depth += 1
            return
        if self.ignored_depth:
            return
        if tag == "tr":
            self.tables[-1]["row"] += 1
            self.tables[-1]["cell"] = 0
        elif tag in {"td", "th"}:
            self.tables[-1]["cell"] += 1
        elif tag == "img":
            self._record_image(attrs)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"script", "style"} and self.ignored_depth:
            self.ignored_depth -= 1
            return
        if tag == "table" and self.tables:
            self.tables.pop()


class _CellHTMLImageParser(HTMLParser):
    """定位单个 Markdown 表格单元格内的 HTML 图片。"""
    """定位 Markdown 表格单元格中的 HTML 图片，不解析单元格外部结构。"""

    def __init__(
        self,
        source: str,
        *,
        base_offset: int,
        table_start_offset: int,
        row_index: int,
        cell_index: int,
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.source = source
        self.base_offset = base_offset
        self.table_start_offset = table_start_offset
        self.row_index = row_index
        self.cell_index = cell_index
        self.offsets = _line_offsets(source)
        self.images: list[_PendingInlineImage] = []
        self.ignored_depth = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.casefold()
        if tag in {"script", "style"}:
            self.ignored_depth += 1
            return
        if tag != "img" or self.ignored_depth:
            return
        values = dict(attrs)
        source = str(values.get("src") or "").strip()
        raw = self.get_starttag_text() or ""
        if not source or not raw:
            return
        line, column = self.getpos()
        start = self.base_offset + self.offsets[line - 1] + column
        self.images.append(
            _PendingInlineImage(
                source=source,
                alt=str(values.get("alt") or ""),
                title=str(values.get("title") or ""),
                source_kind=_source_kind(source),
                syntax="html",
                start_offset=start,
                end_offset=start + len(raw),
                raw=raw,
                table_start_offset=self.table_start_offset,
                row_index=self.row_index,
                cell_index=self.cell_index,
            )
        )

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style"} and self.ignored_depth:
            self.ignored_depth -= 1


class _SingleMediaHTMLParser(HTMLParser):
    """验证一个独立 img/audio/video 标签及其资源属性。"""
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root_tag: str | None = None
        self.source: str | None = None
        self.alt = ""
        self.title = ""
        self.valid = True
        self.closed = False

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if self.root_tag is not None or tag not in {"audio", "video", "img"}:
            self.valid = False
            return
        values = dict(attrs)
        source = values.get("src")
        if not source:
            self.valid = False
            return
        self.root_tag = tag
        self.source = source
        self.alt = str(values.get("alt") or "")
        self.title = str(values.get("title") or "")
        if tag == "img":
            self.closed = True

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.closed = True

    def handle_endtag(self, tag: str) -> None:
        if tag != self.root_tag or self.closed:
            self.valid = False
            return
        self.closed = True

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.valid = False


def _source_kind(source: str) -> str:
    return "remote_https" if urlsplit(source).scheme.casefold() == "https" else "local"


def _code_span_ranges(source: str) -> tuple[tuple[int, int], ...]:
    """返回代码 span 范围，代码中的图片语法不会被导入。"""
    ranges: list[tuple[int, int]] = []
    cursor = 0
    while cursor < len(source):
        if source[cursor] != "`":
            cursor += 1
            continue
        marker_end = cursor + 1
        while marker_end < len(source) and source[marker_end] == "`":
            marker_end += 1
        marker = source[cursor:marker_end]
        closing = source.find(marker, marker_end)
        if closing < 0:
            cursor = marker_end
            continue
        ranges.append((cursor, closing + len(marker)))
        cursor = closing + len(marker)
    return tuple(ranges)


def _inside_ranges(offset: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(start <= offset < end for start, end in ranges)


def _markdown_image_end(source: str, start: int) -> int | None:
    """扫描嵌套括号和引号，找到 Markdown 图片语法的完整结束偏移。"""
    if not source.startswith("![", start):
        return None
    cursor = start + 2
    bracket_depth = 1
    escaped = False
    while cursor < len(source):
        character = source[cursor]
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "[":
            bracket_depth += 1
        elif character == "]":
            bracket_depth -= 1
            if bracket_depth == 0:
                break
        cursor += 1
    if cursor >= len(source) or cursor + 1 >= len(source) or source[cursor + 1] != "(":
        return None

    cursor += 2
    parenthesis_depth = 1
    quote = ""
    escaped = False
    while cursor < len(source):
        character = source[cursor]
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif quote:
            if character == quote:
                quote = ""
        elif character in {'"', "'"}:
            quote = character
        elif character == "(":
            parenthesis_depth += 1
        elif character == ")":
            parenthesis_depth -= 1
            if parenthesis_depth == 0:
                return cursor + 1
        cursor += 1
    return None


def _markdown_cell_images(
    source: str,
    *,
    base_offset: int,
    table_start_offset: int,
    row_index: int,
    cell_index: int,
) -> list[_PendingInlineImage]:
    """解析单元格内 Markdown/HTML 图片并保留源文本绝对偏移。

    先扫描 Markdown 图片，再屏蔽代码 span 后解析 HTML，避免重复识别。
    """
    images: list[_PendingInlineImage] = []
    code_ranges = _code_span_ranges(source)
    cursor = 0
    parser = MarkdownIt("commonmark")
    while cursor < len(source):
        start = source.find("![", cursor)
        if start < 0:
            break
        if _inside_ranges(start, code_ranges):
            cursor = start + 2
            continue
        end = _markdown_image_end(source, start)
        if end is None:
            cursor = start + 2
            continue
        raw = source[start:end]
        tokens = parser.parseInline(raw)
        children = tokens[0].children if len(tokens) == 1 else None
        image = _standalone_image(children)
        if image is None:
            cursor = end
            continue
        images.append(
            _PendingInlineImage(
                source=image["source"],
                alt=image["alt"],
                title=image["title"],
                source_kind=image["source_kind"],
                syntax="markdown",
                start_offset=base_offset + start,
                end_offset=base_offset + end,
                raw=raw,
                table_start_offset=table_start_offset,
                row_index=row_index,
                cell_index=cell_index,
            )
        )
        cursor = end

    masked = list(source)
    for start, end in code_ranges:
        for index in range(start, end):
            if masked[index] not in {"\n", "\r"}:
                masked[index] = " "
    html_parser = _CellHTMLImageParser(
        "".join(masked),
        base_offset=base_offset,
        table_start_offset=table_start_offset,
        row_index=row_index,
        cell_index=cell_index,
    )
    try:
        html_parser.feed("".join(masked))
        html_parser.close()
    except ValueError:
        return images
    images.extend(html_parser.images)
    return images


def _table_inline_images(source: str) -> tuple[MarkdownInlineImage, ...]:
    """收集表格图片并转换为稳定的 ``MarkdownInlineImage`` 引用。

    无法定位的 HTML 片段会被跳过，调用方仍保留原始 Markdown。
    """
    lines = source.splitlines(keepends=True)
    offsets = _line_offsets(source)
    tokens = MarkdownIt("commonmark").enable("table").parse(source)
    pending: list[_PendingInlineImage] = []
    line_cursors: dict[int, int] = {}
    table_start_offset: int | None = None
    row_index = 0
    cell_index = 0

    for token in tokens:
        if token.type == "html_block" and token.map is not None:
            start_line, end_line = token.map
            raw = "".join(lines[start_line:end_line])
            if "<table" not in raw.casefold():
                continue
            parser = _TableHTMLImageParser(raw, base_offset=offsets[start_line])
            try:
                parser.feed(raw)
                parser.close()
            except ValueError:
                continue
            pending.extend(parser.images)
            continue
        if token.type == "table_open" and token.map is not None:
            table_start_offset = offsets[token.map[0]]
            row_index = 0
            cell_index = 0
            continue
        if token.type == "table_close":
            table_start_offset = None
            continue
        if table_start_offset is None:
            continue
        if token.type == "tr_open":
            row_index += 1
            cell_index = 0
            continue
        if token.type in {"th_open", "td_open"}:
            cell_index += 1
            continue
        if token.type != "inline" or token.map is None or not token.content:
            continue
        line_index = token.map[0]
        if line_index >= len(lines):
            continue
        raw_line = lines[line_index]
        search_start = line_cursors.get(line_index, 0)
        content_start = raw_line.find(token.content, search_start)
        if content_start < 0:
            continue
        line_cursors[line_index] = content_start + len(token.content)
        pending.extend(
            _markdown_cell_images(
                token.content,
                base_offset=offsets[line_index] + content_start,
                table_start_offset=table_start_offset,
                row_index=row_index,
                cell_index=cell_index,
            )
        )

    pending.sort(key=lambda item: (item.start_offset, item.end_offset))
    table_indexes = {
        table_start: index
        for index, table_start in enumerate(
            sorted({item.table_start_offset for item in pending}), start=1
        )
    }
    cell_counts: dict[tuple[int, int, int], int] = {}
    references: list[MarkdownInlineImage] = []
    occupied_end = -1
    for item in pending:
        if item.start_offset < occupied_end:
            continue
        table_index = table_indexes[item.table_start_offset]
        cell_key = (table_index, item.row_index, item.cell_index)
        image_index = cell_counts.get(cell_key, 0) + 1
        cell_counts[cell_key] = image_index
        references.append(
            MarkdownInlineImage(
                occurrence_id=(
                    f"table-{table_index}-row-{item.row_index}-cell-{item.cell_index}-image-{image_index}"
                ),
                source=item.source,
                alt=item.alt,
                title=item.title,
                source_kind=item.source_kind,
                syntax=item.syntax,
                start_offset=item.start_offset,
                end_offset=item.end_offset,
                raw=source[item.start_offset:item.end_offset],
                table_index=table_index,
                row_index=item.row_index,
                cell_index=item.cell_index,
                image_index=image_index,
            )
        )
        occupied_end = item.end_offset
    return tuple(references)


def attach_table_inline_images(
    blocks: tuple[MarkdownImportBlock, ...] | list[MarkdownImportBlock],
) -> tuple[MarkdownImportBlock, ...]:
    """为 Markdown block 附加服务端重建的表格图片引用。

    occurrence_id 由源行、表格、行列和图片序号组成，不信任客户端偏移。
    """
    """服务端与客户端共同从正文重建引用，避免信任客户端提交的偏移。"""

    attached: list[MarkdownImportBlock] = []
    table_base = 0
    for block in blocks:
        if block.block_type != "markdown_block" or not isinstance(block.value, str):
            attached.append(block)
            continue
        references = _table_inline_images(block.value)
        reindexed: list[MarkdownInlineImage] = []
        local_table_count = 0
        for reference in references:
            table_index = table_base + reference.table_index
            local_table_count = max(local_table_count, reference.table_index)
            reindexed.append(
                replace(
                    reference,
                    occurrence_id=(
                        f"line-{block.source_start_line}-table-{table_index}-row-{reference.row_index}"
                        f"-cell-{reference.cell_index}-image-{reference.image_index}"
                    ),
                    table_index=table_index,
                )
            )
        table_base += local_table_count
        attached.append(replace(block, inline_images=tuple(reindexed)))
    return tuple(attached)


def _standalone_image(children: list[Token] | None) -> dict[str, str] | None:
    """仅接受单独 image token，避免误拆带文字的段落。"""
    if not children or len(children) != 1 or children[0].type != "image":
        return None
    token = children[0]
    source = str(token.attrGet("src") or "")
    if not source:
        return None
    return {
        "source": source,
        "alt": token.content,
        "title": str(token.attrGet("title") or ""),
        "source_kind": _source_kind(source),
    }


def _embed_value(children: list[Token] | None, raw: str) -> dict[str, str] | None:
    """提取并限制 embed URL，允许主机由 ``EMBED_HOSTS`` 固定控制。"""
    url = ""
    title = ""
    if children and len(children) >= 2:
        if children[0].type == "link_open" and children[-1].type == "link_close":
            url = str(children[0].attrGet("href") or "")
            title = "".join(child.content for child in children[1:-1])
        elif len(children) == 1 and children[0].type == "text":
            url = children[0].content.strip()
    if not url:
        url = raw.strip()
    parsed = urlsplit(url)
    if parsed.scheme.casefold() != "https" or (parsed.hostname or "").casefold() not in EMBED_HOSTS:
        return None
    return {"url": url, "title": title.strip() or url}


def _html_media_value(raw: str) -> tuple[str, dict[str, str]] | None:
    """将单个 HTML 媒体标签转换为 block 类型和值。"""
    parser = _SingleMediaHTMLParser()
    try:
        parser.feed(raw.strip())
        parser.close()
    except ValueError:
        return None
    if not parser.valid or not parser.closed or not parser.root_tag or not parser.source:
        return None
    parsed = urlsplit(parser.source)
    if parser.root_tag == "img":
        if parsed.scheme.casefold() not in {"", "https"} or parser.source.startswith("//"):
            return None
        block_type = "image_block"
    else:
        if parsed.scheme or parser.source.startswith("//"):
            return None
        block_type = f"{parser.root_tag}_block"
    value = {
        "source": parser.source,
        "source_kind": _source_kind(parser.source),
    }
    if parser.root_tag == "img":
        value.update({"alt": parser.alt, "title": parser.title})
    return (
        block_type,
        value,
    )


def _paragraph_extraction(
    token: Token, raw: str, start_line: int, end_line: int
) -> _ExtractedBlock | None:
    """按图片、HTML 媒体、embed 顺序提取段落级特殊 block。"""
    image = _standalone_image(token.children)
    if image is not None:
        return _ExtractedBlock(start_line, end_line, "image_block", image)

    media = _html_media_value(raw)
    if media is not None:
        block_type, value = media
        return _ExtractedBlock(start_line, end_line, block_type, value)

    embed = _embed_value(token.children, raw)
    if embed is not None:
        return _ExtractedBlock(start_line, end_line, "embed_block", embed)
    return None


def _markdown_block(
    lines: list[str], start_line: int, end_line: int
) -> MarkdownImportBlock | None:
    value = "".join(lines[start_line:end_line])
    if not value.strip():
        return None
    return MarkdownImportBlock(
        block_type="markdown_block",
        value=value,
        source_start_line=start_line + 1,
        source_end_line=end_line,
    )


def _skip_block_separator(lines: list[str], start_line: int) -> int:
    """跳过已拆出独立块后的空白分隔行，避免污染下一个 Markdown 块。"""

    cursor = start_line
    while cursor < len(lines) and not lines[cursor].strip():
        cursor += 1
    return cursor


def parse_markdown_blocks(source: str) -> tuple[MarkdownImportBlock, ...]:
    """按源代码顺序拆出独立 block，其余 Markdown 原文保持不变。"""
    """按源码顺序拆出独占媒体块，其余 Markdown 字节内容保持不变。"""

    lines = source.splitlines(keepends=True)
    blocks: list[MarkdownImportBlock] = []
    cursor = 0
    tokens = MarkdownIt("commonmark").parse(source)
    extracted: list[_ExtractedBlock] = []

    for index, token in enumerate(tokens):
        if token.map is None:
            continue
        start_line, end_line = token.map
        if token.type == "fence" and token.info.strip().casefold() == "mermaid":
            extracted.append(
                _ExtractedBlock(
                    start_line,
                    end_line,
                    "mermaid_chart",
                    {"code": token.content, "renderer": MERMAID_RENDERER},
                )
            )
            continue
        if token.type == "html_block":
            raw = "".join(lines[start_line:end_line])
            media = _html_media_value(raw)
            if media is not None:
                block_type, value = media
                extracted.append(_ExtractedBlock(start_line, end_line, block_type, value))
            continue
        if token.type != "paragraph_open" or index + 1 >= len(tokens):
            continue
        inline = tokens[index + 1]
        if inline.type != "inline":
            continue
        raw = "".join(lines[start_line:end_line])
        paragraph = _paragraph_extraction(inline, raw, start_line, end_line)
        if paragraph is not None:
            extracted.append(paragraph)

    for item in extracted:
        start_line, end_line = item.start_line, item.end_line
        markdown = _markdown_block(lines, cursor, start_line)
        if markdown is not None:
            blocks.append(markdown)
        blocks.append(
            MarkdownImportBlock(
                block_type=item.block_type,
                value=item.value,
                source_start_line=start_line + 1,
                source_end_line=end_line,
            )
        )
        cursor = _skip_block_separator(lines, end_line)

    markdown = _markdown_block(lines, cursor, len(lines))
    if markdown is not None:
        blocks.append(markdown)
    return attach_table_inline_images(blocks)
