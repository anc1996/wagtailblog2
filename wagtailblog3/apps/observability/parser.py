"""将项目文本日志解析为后台可展示的结构化记录。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .sanitizer import project_relative_path, sanitize_log_text


HEADER_RE = re.compile(
    r"^\[(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+"
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+"
    r"\[(?P<location>[^\]]+)\]\s+"
    r"\[pid=(?P<pid>\d+)\s+thread=(?P<thread>[^\]]+)\]\s*(?P<message>.*)$"
)


@dataclass(slots=True)
class LogRecord:
    timestamp: datetime | None
    level: str
    logger: str
    relative_path: str
    function: str
    line: int | None
    pid: int | None
    thread: str
    message: str
    traceback: str
    raw: str
    source_key: str = ""
    source_label: str = ""
    source_path: str = ""
    rotation: int = 0
    start_offset: int = 0
    end_offset: int = 0

    @property
    def has_traceback(self) -> bool:
        return bool(self.traceback)


def _split_location(location: str) -> tuple[str, str, str, int | None]:
    """兼容新格式、旧格式和仅含 logger 的邮件简化格式。"""
    logger, separator, code_location = location.partition("|")
    if not separator:
        logger = location
        code_location = location
    parts = code_location.rsplit(":", 2)
    if len(parts) == 3 and parts[2].isdigit():
        if separator:
            return logger, parts[0], parts[1], int(parts[2])
        return parts[0], "", parts[1], int(parts[2])
    return logger, "", "", None


def parse_record(
    lines: list[str], *, start_offset: int = 0, end_offset: int = 0
) -> LogRecord:
    raw = "\n".join(lines).rstrip("\n")
    match = HEADER_RE.match(lines[0]) if lines else None
    if not match:
        return LogRecord(
            timestamp=None,
            level="UNKNOWN",
            logger="",
            relative_path="",
            function="",
            line=None,
            pid=None,
            thread="",
            message=sanitize_log_text(raw),
            traceback="",
            raw=sanitize_log_text(raw),
            start_offset=start_offset,
            end_offset=end_offset,
        )

    logger, relative_path, function, line = _split_location(match.group("location"))
    relative_path = project_relative_path(relative_path) if relative_path else ""
    continuation = "\n".join(lines[1:]).rstrip()
    # Python traceback 可能包含异常前的业务上下文；多行内容统一归入可展开详情。
    traceback = continuation if continuation else ""
    return LogRecord(
        timestamp=datetime.strptime(match.group("timestamp"), "%Y-%m-%d %H:%M:%S"),
        level=match.group("level"),
        logger=logger,
        relative_path=relative_path,
        function=function,
        line=line,
        pid=int(match.group("pid")),
        thread=match.group("thread"),
        message=sanitize_log_text(match.group("message")),
        traceback=sanitize_log_text(traceback),
        raw=sanitize_log_text(raw),
        start_offset=start_offset,
        end_offset=end_offset,
    )


def parse_bytes(data: bytes, *, base_offset: int = 0) -> list[LogRecord]:
    """解析一段日志字节，并保留每条记录在文件中的起始偏移。"""
    lines = data.splitlines(keepends=True)
    records: list[LogRecord] = []
    current: list[str] = []
    current_offset = base_offset
    offset = base_offset

    for raw_line in lines:
        line = raw_line.rstrip(b"\r\n").decode("utf-8", errors="replace")
        if HEADER_RE.match(line):
            if current:
                records.append(
                    parse_record(
                        current,
                        start_offset=current_offset,
                        end_offset=offset,
                    )
                )
            current = [line]
            current_offset = offset
        else:
            if not current:
                current_offset = offset
            current.append(line)
        offset += len(raw_line)

    if current:
        records.append(
            parse_record(
                current,
                start_offset=current_offset,
                end_offset=base_offset + len(data),
            )
        )
    return records
