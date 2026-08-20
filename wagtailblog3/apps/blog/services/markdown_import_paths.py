import os
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote, urlsplit


_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


class LocalMediaPathError(ValueError):
    """表示本地媒体路径不满足导入安全边界。"""


@dataclass(frozen=True, slots=True)
class ResolvedLocalMedia:
    path: Path
    normalized_source: str
    safe_filename: str


def _reject_non_relative_source(source: str) -> None:
    parsed = urlsplit(source)
    if parsed.scheme or parsed.netloc:
        raise LocalMediaPathError("unsupported_local_path")
    if (
        Path(source).is_absolute()
        or PureWindowsPath(source).is_absolute()
        or _WINDOWS_DRIVE.match(source)
        or source.startswith(("\\\\", "//"))
    ):
        raise LocalMediaPathError("absolute_path_forbidden")


def resolve_local_media_path(
    source_root: str | os.PathLike[str], source: str
) -> ResolvedLocalMedia:
    """解析根目录内的真实文件，并拒绝目录穿越与链接逃逸。"""

    # Markdown 链接允许对非 ASCII 文件名和路径分隔符做百分号编码，先解码再执行边界校验。
    cleaned = unquote(source.strip())
    if not cleaned or "\x00" in cleaned:
        raise LocalMediaPathError("invalid_local_path")
    _reject_non_relative_source(cleaned)

    root = Path(source_root).resolve(strict=True)
    if not root.is_dir():
        raise LocalMediaPathError("source_root_not_directory")
    candidate = (root / Path(cleaned)).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise LocalMediaPathError("path_outside_source_root") from exc

    if not candidate.exists():
        raise LocalMediaPathError("file_missing")
    if not candidate.is_file():
        raise LocalMediaPathError("not_a_file")
    if not os.access(candidate, os.R_OK):
        raise LocalMediaPathError("file_unreadable")

    normalized = candidate.relative_to(root).as_posix()
    return ResolvedLocalMedia(
        path=candidate,
        normalized_source=normalized,
        safe_filename=candidate.name,
    )
