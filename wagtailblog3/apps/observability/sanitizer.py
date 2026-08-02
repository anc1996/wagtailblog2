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
# 匹配 password、token 等敏感键的值，同时保留键名和分隔符，便于日志检索。
SENSITIVE_KEY_RE = re.compile(
    rf"(?i)(?P<prefix>[\"']?(?:{'|'.join(SENSITIVE_NAMES)})[\"']?\s*[:=]\s*)"
    r"(?P<value>\[REDACTED\]|\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;&}\]]+)"
)
# 单独处理 Authorization 头，兼容带 Bearer 前缀和不带前缀的写法。
AUTHORIZATION_RE = re.compile(
    r"(?i)(?P<prefix>\bauthorization\b\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"
)
# Cookie 通常包含多个键值，整行隐藏可避免遗漏其中的会话标识。
COOKIE_RE = re.compile(r"(?i)(?P<prefix>\bcookie\b\s*[:=]\s*)[^\r\n]+")
# 匹配脱离 Authorization 字段单独出现的 Bearer 令牌。
BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
# 私钥是多行内容，必须使用 DOTALL 才能一次替换完整的密钥块。
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.DOTALL,
)
# 清洗 Python 回溯中的 File 路径，但保留 File 和引号格式。
TRACEBACK_FILE_RE = re.compile(
    r"(?P<prefix>\bFile\s+[\"'])(?P<path>[^\"']+)(?P<suffix>[\"'])"
)
# 匹配 Linux/Unix 常见服务器根目录，避免把机器绝对路径返回给前端。
SERVER_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9:])(?P<path>/(?:home|root|usr|opt|var|tmp|srv|mnt)/[^\s\"'<>:,]+(?:/[^\s\"'<>:,]+)*)"
)
# 同样清洗 Windows 盘符路径，兼容大小写盘符和多级目录。
WINDOWS_PATH_RE = re.compile(r"(?i)(?P<path>[A-Z]:\\(?:[^\s\"'<>:,]+\\)*[^\s\"'<>:,]+)")


def _project_root(base_dir=None) -> Path | None:
    # 未显式传入根目录时从 Django 配置读取；脱离 Django 环境则返回空值，
    # 让调用方仍可进行不依赖项目配置的基础路径清洗。
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
    # 统一输出项目内相对路径；项目外只保留文件名，避免泄露部署目录结构。
    root = _project_root(base_dir)
    try:
        path = Path(pathname)
        if not path.is_absolute():
            # 相对路径若包含 .. 可能绕过展示边界，因此只接受不含父目录跳转的路径。
            if ".." not in path.parts:
                return path.as_posix()
            return path.name
        # resolve 只用于规范化比较，不要求目标文件实际存在。
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
    # 路径清洗按三类来源依次处理：Python 回溯、项目绝对前缀、服务器绝对路径。
    root = _project_root(base_dir)

    def replace_traceback(match):
        # 仅替换捕获的 path 部分，保留 File 关键字、空格和引号等原始格式。
        return (
            f"{match.group('prefix')}"
            f"{project_relative_path(match.group('path'), base_dir)}"
            f"{match.group('suffix')}"
        )

    text = TRACEBACK_FILE_RE.sub(replace_traceback, text)
    if root is not None:
        # 处理没有出现在标准回溯格式中的项目绝对路径。
        for prefix in (str(root) + os.sep, root.as_posix() + "/"):
            text = text.replace(prefix, "")
    # 最后处理项目外的 Unix 和 Windows 路径，统一交给 project_relative_path 决定输出。
    text = SERVER_PATH_RE.sub(
        lambda match: project_relative_path(match.group("path"), base_dir), text
    )
    return WINDOWS_PATH_RE.sub(
        lambda match: project_relative_path(match.group("path"), base_dir), text
    )


def redact_sensitive(text: str) -> str:
    """按敏感信息类型逐层替换日志中的凭据和认证数据。"""
    if not text:
        return text

    # 先处理私钥块，避免多行私钥内容在后续规则中被部分暴露。
    text = PRIVATE_KEY_RE.sub(REDACTED, text)

    # Cookie 和 Authorization 可能包含多个键值，整体替换其值而保留字段名，
    # 便于排查请求来源，同时避免泄露会话或访问凭据。
    text = COOKIE_RE.sub(lambda match: f"{match.group('prefix')}{REDACTED}", text)
    text = AUTHORIZATION_RE.sub(
        lambda match: f"{match.group('prefix')}{REDACTED}", text
    )

    # 单独处理没有字段名的 Bearer 令牌，以及常见的 password/token/secret 等键。
    # 每轮都保留前缀，最终日志仍能表达原始字段类型。
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
