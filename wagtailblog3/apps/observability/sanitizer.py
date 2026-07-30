"""日志写入和展示共用的敏感信息及服务器路径清洗。"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from colorlog import ColoredFormatter


REDACTED = "[REDACTED]"
SENSITIVE_NAMES = (
    "password",
    "passwd",
    "token",
    "authorization",
    "cookie",
    "sessionid",
    "secret",
    "api_key",
    "access_key",
    "private_key",
)
SENSITIVE_KEY_RE = re.compile(
    rf"(?i)(?P<prefix>[\"']?(?:{'|'.join(SENSITIVE_NAMES)})[\"']?\s*[:=]\s*)"
    r"(?P<value>\[REDACTED\]|\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;&}\]]+)"
)
AUTHORIZATION_RE = re.compile(
    r"(?i)(?P<prefix>\bauthorization\b\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"
)
COOKIE_RE = re.compile(r"(?i)(?P<prefix>\bcookie\b\s*[:=]\s*)[^\r\n]+")
BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.DOTALL,
)
TRACEBACK_FILE_RE = re.compile(
    r"(?P<prefix>\bFile\s+[\"'])(?P<path>[^\"']+)(?P<suffix>[\"'])"
)
SERVER_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9:])(?P<path>/(?:home|root|usr|opt|var|tmp|srv|mnt)/[^\s\"'<>:,]+(?:/[^\s\"'<>:,]+)*)"
)
WINDOWS_PATH_RE = re.compile(r"(?i)(?P<path>[A-Z]:\\(?:[^\s\"'<>:,]+\\)*[^\s\"'<>:,]+)")


def _project_root(base_dir=None) -> Path | None:
    if base_dir is None:
        try:
            from django.conf import settings

            base_dir = settings.BASE_DIR
        except Exception:
            return None
    try:
        return Path(base_dir).resolve()
    except (OSError, TypeError, ValueError):
        return None


def project_relative_path(pathname: str, base_dir=None) -> str:
    """项目内返回相对路径，项目外只返回文件名，永不产生 ``..``。"""
    if not pathname:
        return ""
    root = _project_root(base_dir)
    try:
        path = Path(pathname)
        if not path.is_absolute():
            if ".." not in path.parts:
                return path.as_posix()
            return path.name
        path = path.resolve(strict=False)
        if root is not None:
            relative = path.relative_to(root)
            if ".." not in relative.parts:
                return relative.as_posix()
        return path.name
    except (OSError, TypeError, ValueError):
        return Path(str(pathname)).name


def sanitize_paths(text: str, base_dir=None) -> str:
    if not text:
        return text
    root = _project_root(base_dir)

    def replace_traceback(match):
        return (
            f"{match.group('prefix')}"
            f"{project_relative_path(match.group('path'), base_dir)}"
            f"{match.group('suffix')}"
        )

    text = TRACEBACK_FILE_RE.sub(replace_traceback, text)
    if root is not None:
        for prefix in (str(root) + os.sep, root.as_posix() + "/"):
            text = text.replace(prefix, "")
    text = SERVER_PATH_RE.sub(
        lambda match: project_relative_path(match.group("path"), base_dir), text
    )
    return WINDOWS_PATH_RE.sub(
        lambda match: project_relative_path(match.group("path"), base_dir), text
    )


def redact_sensitive(text: str) -> str:
    if not text:
        return text
    text = PRIVATE_KEY_RE.sub(REDACTED, text)
    text = COOKIE_RE.sub(lambda match: f"{match.group('prefix')}{REDACTED}", text)
    text = AUTHORIZATION_RE.sub(
        lambda match: f"{match.group('prefix')}{REDACTED}", text
    )
    text = BEARER_RE.sub(f"Bearer {REDACTED}", text)
    return SENSITIVE_KEY_RE.sub(
        lambda match: f"{match.group('prefix')}{REDACTED}", text
    )


def sanitize_log_text(text: str, base_dir=None) -> str:
    return redact_sensitive(sanitize_paths(str(text), base_dir))


class SensitiveDataFilter(logging.Filter):
    """在任何 handler 处理前遮盖消息参数。"""

    def filter(self, record):
        try:
            record.msg = sanitize_log_text(record.getMessage())
            record.args = ()
        except Exception:
            record.msg = REDACTED
            record.args = ()
        return True


class RedactingFormatter(logging.Formatter):
    """二次清洗完整输出，包括格式化后的 traceback。"""

    def __init__(self, format=None, **kwargs):
        super().__init__(fmt=format, **kwargs)

    def format(self, record):
        return sanitize_log_text(super().format(record))


class RedactingColoredFormatter(ColoredFormatter):
    """控制台也不输出敏感消息或绝对服务器路径。"""

    def __init__(self, format=None, **kwargs):
        super().__init__(fmt=format, **kwargs)

    def format(self, record):
        return sanitize_log_text(super().format(record))
