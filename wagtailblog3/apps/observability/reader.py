"""受限地读取已注册日志，避免大文件整体进入内存。"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core import signing

from .parser import LogRecord, parse_bytes
from .registry import LOG_FILE_BY_KEY, LogFileSpec, iter_log_files
from .sanitizer import sanitize_log_text


CHUNK_SIZE = 64 * 1024
UNKNOWN_RECORD_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_RESULTS = 1000
CURSOR_SALT = "observability.log-reader"
CURSOR_VERSION = 3
ANCHOR_BYTES = 128
SNAPSHOT_SAMPLES = 8


@dataclass(slots=True)
class ReadResult:
    records: list[LogRecord]
    next_cursor: str
    has_more: bool
    bytes_read: int
    snapshot_valid: bool = True


@dataclass(frozen=True, slots=True)
class CursorState:
    offset: int
    anchor: str = ""
    snapshot_size: int = 0
    snapshot_anchor: str = ""


@dataclass(slots=True)
class SourceScan:
    records: list[LogRecord]
    resume_offset: int
    bytes_read: int
    valid: bool = True


def _log_root() -> Path:
    return Path(settings.LOG_DIR).resolve()


def resolve_registered_path(spec: LogFileSpec, rotation: int = 0) -> Path:
    """解析注册路径并拒绝符号链接和日志根目录之外的目标。"""
    if rotation < 0 or rotation > spec.backup_count:
        raise ValueError("轮转编号超出允许范围")
    relative = spec.relative_path if rotation == 0 else f"{spec.relative_path}.{rotation}"
    root = _log_root()
    candidate = root / relative
    if candidate.is_symlink():
        raise ValueError("不允许读取符号链接日志")
    resolved = candidate.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise ValueError("日志路径越过允许目录")
    return resolved


def _source_id(spec: LogFileSpec, identity: tuple[int, int]) -> str:
    """使用设备号和 inode 标识真实文件，轮转改名后身份保持不变。"""
    return f"{spec.key}|{identity[0]}|{identity[1]}"


def _parse_source_id(source: str) -> tuple[LogFileSpec, tuple[int, int]]:
    try:
        key, device, inode = source.rsplit("|", 2)
        spec = LOG_FILE_BY_KEY[key]
        identity = (int(device), int(inode))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("分页游标版本无效或已过期") from exc
    return spec, identity


def _decode_cursor(value: str) -> dict[str, CursorState]:
    if not value:
        return {}
    try:
        payload = signing.loads(value, salt=CURSOR_SALT, max_age=3600)
    except signing.BadSignature as exc:
        raise ValueError("分页游标无效或已过期") from exc

    if not isinstance(payload, dict) or payload.get("version") != CURSOR_VERSION:
        raise ValueError("分页游标格式无效")
    sources = payload.get("sources")
    if not isinstance(sources, dict):
        raise ValueError("分页游标格式无效")

    # 游标来自客户端，必须逐字段校验类型和非负范围后才能参与文件定位。
    states: dict[str, CursorState] = {}
    for source, state in sources.items():
        if (
            not isinstance(source, str)
            or not isinstance(state, dict)
            or not isinstance(state.get("offset"), int)
            or state["offset"] < 0
            or not isinstance(state.get("anchor", ""), str)
        ):
            raise ValueError("分页游标格式无效")
        snapshot_size = state.get("snapshot_size")
        snapshot_anchor = state.get("snapshot_anchor")
        if (
            not isinstance(snapshot_size, int)
            or snapshot_size < 0
            or not isinstance(snapshot_anchor, str)
        ):
            raise ValueError("分页游标格式无效")
        states[source] = CursorState(
            state["offset"],
            state.get("anchor", ""),
            snapshot_size,
            snapshot_anchor,
        )
    return states


def _encode_cursor(states: dict[str, CursorState]) -> str:
    payload = {
        "version": CURSOR_VERSION,
        "sources": {
            source: {
                "offset": state.offset,
                "anchor": state.anchor,
                "snapshot_size": state.snapshot_size,
                "snapshot_anchor": state.snapshot_anchor,
            }
            for source, state in states.items()
        },
    }
    return signing.dumps(payload, salt=CURSOR_SALT, compress=True)


def _open_verified(path: Path, identity: tuple[int, int]):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    stat = os.fstat(descriptor)
    if (stat.st_dev, stat.st_ino) != identity:
        os.close(descriptor)
        return None
    return descriptor, stat


def _content_anchor(path: Path, offset: int, identity: tuple[int, int]) -> str:
    """指纹化游标前缀，识别 inode 未变但内容已替换的 copytruncate。"""
    opened = _open_verified(path, identity)
    if not opened:
        return ""
    descriptor, stat = opened
    try:
        if offset <= 0 or stat.st_size < offset:
            return ""
        start = max(0, offset - ANCHOR_BYTES)
        data = os.pread(descriptor, offset - start, start)
        return hashlib.blake2s(data, digest_size=12).hexdigest()
    finally:
        os.close(descriptor)


def _snapshot_anchor(
    path: Path, snapshot_size: int, identity: tuple[int, int]
) -> str:
    """对原快照的多个位置采样，区分追加写和截断后重写。"""
    opened = _open_verified(path, identity)
    if not opened:
        return ""
    descriptor, stat = opened
    try:
        if snapshot_size <= 0 or stat.st_size < snapshot_size:
            return ""
        maximum_start = max(0, snapshot_size - ANCHOR_BYTES)
        starts = {
            (maximum_start * index) // max(1, SNAPSHOT_SAMPLES - 1)
            for index in range(SNAPSHOT_SAMPLES)
        }
        digest = hashlib.blake2s(digest_size=16)
        digest.update(snapshot_size.to_bytes(8, "big"))
        for start in sorted(starts):
            amount = min(ANCHOR_BYTES, snapshot_size - start)
            digest.update(start.to_bytes(8, "big"))
            digest.update(os.pread(descriptor, amount, start))
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _unknown_records(data: bytes, start: int, end: int) -> list[LogRecord]:
    """从文件尾向前按固定字节块保存无结构内容。"""
    records: list[LogRecord] = []
    cursor = end
    while cursor > start:
        chunk_start = max(start, cursor - UNKNOWN_RECORD_BYTES)
        raw = data[chunk_start - start : cursor - start].decode(
            "utf-8", errors="replace"
        ).rstrip("\r\n")
        raw = sanitize_log_text(raw)
        records.append(
            LogRecord(
                timestamp=None,
                level="UNKNOWN",
                logger="",
                relative_path="",
                function="",
                line=None,
                pid=None,
                thread="",
                message=raw,
                traceback="",
                raw=raw,
                start_offset=chunk_start,
                end_offset=cursor,
            )
        )
        cursor = chunk_start
    records.reverse()
    return records


def _parse_segment(data: bytes, start: int, end: int) -> tuple[list[LogRecord], int]:
    records = parse_bytes(data, base_offset=start)
    structured = any(record.level != "UNKNOWN" for record in records)
    if not structured:
        return _unknown_records(data, start, end), start

    # 非零块起点可能落在多行记录内部。下一页从首个完整日志头继续，
    # 既不返回伪 UNKNOWN，也不重复扫描已经判定不匹配的尾部。
    if start > 0 and records and records[0].level == "UNKNOWN":
        records.pop(0)
    resume_offset = records[0].start_offset if records else end
    return records, resume_offset


def _matches(
    record: LogRecord,
    *,
    level: str,
    keyword: str,
    since: datetime | None,
    until: datetime | None,
) -> bool:
    if level and record.level != level:
        return False
    if since and (record.timestamp is None or record.timestamp < since):
        return False
    if until and (record.timestamp is None or record.timestamp > until):
        return False
    if keyword and keyword.casefold() not in record.raw.casefold():
        return False
    return True


def _scan_source(
    path: Path,
    end_offset: int,
    target_matches: int,
    budget: int,
    expected_identity: tuple[int, int],
    *,
    level: str,
    keyword: str,
    since: datetime | None,
    until: datetime | None,
) -> SourceScan:
    """向前扫描并在扫描过程中筛选，直到匹配数或字节预算满足。"""
    # 先按设备号和 inode 验证文件，再在受限字节预算内从尾部向前扫描，避免读入整文件。
    opened = _open_verified(path, expected_identity)
    if not opened:
        return SourceScan([], 0, 0, valid=False)
    descriptor, stat = opened
    if stat.st_size < end_offset:
        os.close(descriptor)
        return SourceScan([], 0, 0, valid=False)

    end = min(max(end_offset, 0), stat.st_size)
    start = end
    data = b""
    bytes_read = 0
    records: list[LogRecord] = []
    resume_offset = end
    try:
        while start > 0 and bytes_read < budget:
            # 窗口指数扩展，避免深层无匹配时反复解析 64KB 递增的全部缓冲区。
            # 最终累计解析量小于末次窗口的两倍，实际文件读取量仍受 budget 限制。
            window = CHUNK_SIZE if bytes_read == 0 else bytes_read
            amount = min(window, start, budget - bytes_read)
            start -= amount
            data = os.pread(descriptor, amount, start) + data
            bytes_read += amount
            records, resume_offset = _parse_segment(data, start, end)
            matched = sum(
                _matches(
                    record,
                    level=level,
                    keyword=keyword,
                    since=since,
                    until=until,
                )
                for record in records
            )
            if matched >= target_matches:
                break
    finally:
        os.close(descriptor)

    matched_records = [
        record
        for record in reversed(records)
        if _matches(
            record,
            level=level,
            keyword=keyword,
            since=since,
            until=until,
        )
    ]
    return SourceScan(matched_records, resume_offset, bytes_read)


def _find_snapshot_file(spec: LogFileSpec, identity: tuple[int, int]):
    """在当前及轮转槽位中寻找游标记录的同一 inode。"""
    for rotation in range(spec.backup_count + 1):
        path = resolve_registered_path(spec, rotation)
        try:
            stat = path.stat()
        except OSError:
            continue
        if path.is_file() and (stat.st_dev, stat.st_ino) == identity:
            return rotation, path
    return None


def _initial_sources(specs, include_rotated: bool):
    """第一页建立文件快照；后续页只跟随这些真实文件。"""
    sources = []
    seen = set()
    for spec in specs:
        rotations = range(spec.backup_count + 1) if include_rotated else (0,)
        for rotation in rotations:
            path = resolve_registered_path(spec, rotation)
            try:
                stat = path.stat()
            except OSError:
                continue
            if not path.is_file():
                continue
            identity = (stat.st_dev, stat.st_ino)
            source = _source_id(spec, identity)
            if source in seen:
                continue
            seen.add(source)
            snapshot_anchor = _snapshot_anchor(path, stat.st_size, identity)
            if stat.st_size and not snapshot_anchor:
                continue
            sources.append(
                (
                    spec,
                    rotation,
                    path,
                    source,
                    CursorState(
                        stat.st_size,
                        snapshot_size=stat.st_size,
                        snapshot_anchor=snapshot_anchor,
                    ),
                    identity,
                )
            )
    return sources


def _cursor_sources(states: dict[str, CursorState], allowed_specs):
    """按 inode 重定位游标文件，并验证 copytruncate 内容锚点。"""
    allowed_keys = {spec.key for spec in allowed_specs}
    sources = []
    for source, state in states.items():
        if state.offset <= 0:
            continue
        spec, identity = _parse_source_id(source)
        if spec.key not in allowed_keys:
            continue
        located = _find_snapshot_file(spec, identity)
        if not located:
            continue
        rotation, path = located
        if state.anchor and _content_anchor(path, state.offset, identity) != state.anchor:
            continue
        if (
            state.snapshot_anchor
            and _snapshot_anchor(path, state.snapshot_size, identity)
            != state.snapshot_anchor
        ):
            continue
        sources.append((spec, rotation, path, source, state, identity))
    return sources


def _state_for_offset(
    spec: LogFileSpec,
    identity: tuple[int, int],
    offset: int,
    snapshot: CursorState,
) -> CursorState | None:
    if offset <= 0:
        return None
    located = _find_snapshot_file(spec, identity)
    if not located:
        return None
    _, path = located
    anchor = _content_anchor(path, offset, identity)
    if not anchor:
        return None
    if (
        snapshot.snapshot_anchor
        and _snapshot_anchor(path, snapshot.snapshot_size, identity)
        != snapshot.snapshot_anchor
    ):
        return None
    return CursorState(
        offset,
        anchor,
        snapshot.snapshot_size,
        snapshot.snapshot_anchor,
    )


def read_logs(
    *,
    domain: str = "",
    kind: str = "error",
    level: str = "",
    keyword: str = "",
    since: datetime | None = None,
    until: datetime | None = None,
    include_rotated: bool = False,
    page_size: int = 100,
    cursor: str = "",
) -> ReadResult:
    """跨文件读取最新记录，并用签名游标保存各文件扫描进度。"""
    page_size = min(max(page_size, 1), 200)
    result_limit = min(page_size, MAX_RESULTS)
    # 首页建立真实文件快照；后续请求只接受能通过 inode 和内容锚点验证的游标来源。
    specs = iter_log_files(domain or None, kind or None)
    states = _decode_cursor(cursor)
    sources = _cursor_sources(states, specs) if cursor else _initial_sources(specs, include_rotated)
    snapshot_valid = not cursor or bool(sources)
    bytes_read = 0
    candidates: list[LogRecord] = []
    scans: dict[str, SourceScan] = {}
    source_meta = {
        source: (spec, identity, state)
        for spec, _rotation, _path, source, state, identity in sources
    }

    for spec, rotation, path, source, state, identity in sources:
        budget = MAX_TOTAL_BYTES - bytes_read
        if budget <= 0:
            break
        scan = _scan_source(
            path,
            state.offset,
            result_limit + 1,
            budget,
            identity,
            level=level,
            keyword=keyword,
            since=since,
            until=until,
        )
        scans[source] = scan
        bytes_read += scan.bytes_read
        if not scan.valid:
            continue
        for record in scan.records:
            record.source_key = source
            record.source_label = spec.label
            record.source_path = spec.relative_path
            record.rotation = rotation
            candidates.append(record)

    # 不同日志文件合并后按时间倒序取一页，多取一条用于判断是否还有下一页。
    candidates.sort(
        key=lambda item: item.timestamp or datetime.min,
        reverse=True,
    )
    selected = candidates[:result_limit]
    selected_ids = {id(record) for record in selected}
    next_states: dict[str, CursorState] = {}

    for source, (spec, identity, snapshot) in source_meta.items():
        scan = scans.get(source)
        if scan is None:
            next_offset = snapshot.offset
        elif not scan.valid:
            continue
        else:
            unselected = [
                record for record in scan.records if id(record) not in selected_ids
            ]
            # 保留最新未返回匹配记录的结束边界；其后的非匹配内容已经扫描，
            # 下一页无需重复。若无待返回记录，则直接推进到安全扫描边界。
            next_offset = (
                unselected[0].end_offset if unselected else scan.resume_offset
            )
        state = _state_for_offset(spec, identity, next_offset, snapshot)
        if state:
            next_states[source] = state

    has_more = bool(next_states)
    return ReadResult(
        records=selected,
        next_cursor=_encode_cursor(next_states) if has_more else "",
        has_more=has_more,
        bytes_read=bytes_read,
        snapshot_valid=snapshot_valid,
    )
