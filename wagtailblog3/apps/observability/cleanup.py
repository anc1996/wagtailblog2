"""安全的日志清理预览与文件级执行。"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import stat
import uuid

from django.conf import settings

from .registry import LOG_FILE_SPECS, LogFileSpec


VALID_SCOPES = {"current", "rotated", "all"}
MAX_ROTATION = max(spec.backup_count for spec in LOG_FILE_SPECS)


class UnsafeLogTarget(ValueError):
    """目标不再是 catalog 指向的安全普通文件。"""


@dataclass(slots=True)
class CleanupResult:
    target: str
    scope: str
    file_results: list[dict]

    @property
    def files_before(self) -> int:
        return sum(int(result["pre_exists"]) for result in self.file_results)

    @property
    def bytes_before(self) -> int:
        return sum(result["bytes_before"] for result in self.file_results)

    @property
    def bytes_freed(self) -> int:
        return sum(result["bytes_freed"] for result in self.file_results)

    @property
    def failed_files(self) -> list[dict[str, str]]:
        return [
            {"file": result["file"], "error": result["error"]}
            for result in self.file_results
            if not result["succeeded"]
        ]

    @property
    def changed_files(self) -> list[str]:
        return [
            result["file"]
            for result in self.file_results
            if result["outcome"] in {"truncated", "unlinked"}
        ]

    @property
    def succeeded(self) -> bool:
        return not self.failed_files


def _validate_scope(scope: str) -> None:
    if scope not in VALID_SCOPES:
        raise ValueError("不支持的清理范围")


def _selected_rotations(spec: LogFileSpec, scope: str):
    _validate_scope(scope)
    if scope in {"current", "all"}:
        yield 0
    if scope in {"rotated", "all"}:
        yield from range(1, spec.backup_count + 1)


def _relative_name(spec: LogFileSpec, rotation: int) -> str:
    return spec.relative_path if rotation == 0 else f"{spec.relative_path}.{rotation}"


def _registered_candidate(spec: LogFileSpec, rotation: int) -> Path:
    if rotation < 0 or rotation > spec.backup_count:
        raise UnsafeLogTarget("轮转编号超出 catalog 允许范围")
    relative = PurePosixPath(_relative_name(spec, rotation))
    if relative.is_absolute() or ".." in relative.parts:
        raise UnsafeLogTarget("catalog 日志路径无效")

    root = Path(settings.LOG_DIR).resolve()
    candidate = root.joinpath(*relative.parts)
    resolved_parent = candidate.parent.resolve(strict=False)
    if resolved_parent != root and root not in resolved_parent.parents:
        raise UnsafeLogTarget("日志路径越过允许目录")

    current = root
    for component in relative.parts[:-1]:
        current /= component
        try:
            current_stat = os.lstat(current)
        except FileNotFoundError:
            break
        if stat.S_ISLNK(current_stat.st_mode):
            raise UnsafeLogTarget("日志路径包含符号链接目录")
        if not stat.S_ISDIR(current_stat.st_mode):
            raise UnsafeLogTarget("日志路径父级不是目录")
    return candidate


def _safe_lstat(path: Path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _identity(file_stat) -> tuple[int, int]:
    return file_stat.st_dev, file_stat.st_ino


def _assert_regular(file_stat) -> None:
    if stat.S_ISLNK(file_stat.st_mode):
        raise UnsafeLogTarget("拒绝清理符号链接")
    if not stat.S_ISREG(file_stat.st_mode):
        raise UnsafeLogTarget("清理目标不是普通文件")


def _base_result(spec: LogFileSpec, rotation: int, action: str) -> dict:
    return {
        "file": _relative_name(spec, rotation),
        "rotation": rotation,
        "action": action,
        "pre_exists": False,
        "pre_device": None,
        "pre_inode": None,
        "bytes_before": 0,
        "bytes_freed": 0,
        "post_exists": False,
        "post_device": None,
        "post_inode": None,
        "post_size": 0,
        "inode_preserved": None,
        "succeeded": True,
        "outcome": "already_absent",
        "error": "",
    }


def _mark_failure(result: dict, exc: Exception) -> dict:
    result["succeeded"] = False
    result["outcome"] = "failed"
    result["error"] = str(exc)
    return result


def _restore_quarantined(path: Path, quarantine: Path) -> bool:
    """Restore a raced target without overwriting a newer path."""
    try:
        os.link(quarantine, path, follow_symlinks=False)
    except (FileExistsError, OSError):
        return False
    os.unlink(quarantine)
    return True


def _truncate_current(spec: LogFileSpec) -> dict:
    result = _base_result(spec, 0, "truncate")
    descriptor = None
    try:
        path = _registered_candidate(spec, 0)
        before = _safe_lstat(path)
        if before is None:
            return result
        _assert_regular(before)
        result.update(
            pre_exists=True,
            pre_device=before.st_dev,
            pre_inode=before.st_ino,
            bytes_before=before.st_size,
        )

        flags = os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        _assert_regular(opened)
        if _identity(opened) != _identity(before):
            raise UnsafeLogTarget("当前日志在打开前已被替换，已拒绝截断")

        os.ftruncate(descriptor, 0)
        result["bytes_freed"] = before.st_size
        descriptor_stat = os.fstat(descriptor)
        if _identity(descriptor_stat) != _identity(before):
            raise UnsafeLogTarget("截断后的文件描述符身份异常")

        after = _safe_lstat(path)
        if after is None:
            raise UnsafeLogTarget("截断后当前日志路径消失")
        _assert_regular(after)
        result.update(
            post_exists=True,
            post_device=after.st_dev,
            post_inode=after.st_ino,
            post_size=after.st_size,
            inode_preserved=_identity(after) == _identity(before),
        )
        if not result["inode_preserved"]:
            raise UnsafeLogTarget("截断后当前日志 inode 已被替换")

        # 并发 writer 可能已经写入新内容；旧内容仍由 ftruncate 完整释放。
        result["outcome"] = "truncated"
    except (OSError, UnsafeLogTarget, ValueError) as exc:
        _mark_failure(result, exc)
        try:
            after = _safe_lstat(path)
        except (OSError, UnboundLocalError):
            after = None
        if after is not None:
            result.update(
                post_exists=True,
                post_device=after.st_dev,
                post_inode=after.st_ino,
                post_size=after.st_size,
            )
            if result["pre_inode"] is not None:
                result["inode_preserved"] = _identity(after) == (
                    result["pre_device"],
                    result["pre_inode"],
                )
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return result


def _unlink_rotation(spec: LogFileSpec, rotation: int) -> dict:
    result = _base_result(spec, rotation, "unlink")
    quarantine = None
    try:
        path = _registered_candidate(spec, rotation)
        before = _safe_lstat(path)
        if before is None:
            return result
        _assert_regular(before)
        result.update(
            pre_exists=True,
            pre_device=before.st_dev,
            pre_inode=before.st_ino,
            bytes_before=before.st_size,
        )

        immediate = _safe_lstat(path)
        if immediate is None:
            result["outcome"] = "already_absent"
            return result
        _assert_regular(immediate)
        if _identity(immediate) != _identity(before):
            raise UnsafeLogTarget("轮转日志在删除前已被替换，已拒绝删除")

        # rename is the atomic ownership boundary: after it succeeds, no later
        # replacement at the catalog path can be unlinked by this operation.
        quarantine = path.with_name(f".{path.name}.cleanup-{uuid.uuid4().hex}")
        os.rename(path, quarantine)
        isolated = _safe_lstat(quarantine)
        if isolated is None:
            raise UnsafeLogTarget("轮转日志隔离后消失")
        _assert_regular(isolated)
        if _identity(isolated) != _identity(before):
            restored = _restore_quarantined(path, quarantine)
            quarantine = None if restored else quarantine
            suffix = "并已恢复原路径" if restored else f"；隔离副本保留为 {quarantine.name}"
            raise UnsafeLogTarget(f"轮转日志在隔离前已被替换，已拒绝删除{suffix}")

        os.unlink(quarantine)
        quarantine = None
        after = _safe_lstat(path)
        if after is not None:
            result.update(
                post_exists=True,
                post_device=after.st_dev,
                post_inode=after.st_ino,
                post_size=after.st_size,
            )
            raise UnsafeLogTarget("轮转日志删除后路径仍然存在")
        result["bytes_freed"] = before.st_size
        result["outcome"] = "unlinked"
    except (OSError, UnsafeLogTarget, ValueError) as exc:
        _mark_failure(result, exc)
        try:
            after = _safe_lstat(path)
        except (OSError, UnboundLocalError):
            after = None
        if after is not None:
            result.update(
                post_exists=True,
                post_device=after.st_dev,
                post_inode=after.st_ino,
                post_size=after.st_size,
            )
        if quarantine is not None and quarantine.exists():
            result["quarantine"] = quarantine.name
    return result


def preview_cleanup(
    specs: tuple[LogFileSpec, ...],
    *,
    target_type: str,
    target: str,
    kind: str,
    scope: str,
) -> dict:
    _validate_scope(scope)
    if not specs:
        raise ValueError("没有匹配的注册日志")

    current = {"file_count": 0, "total_bytes": 0}
    rotated = {"file_count": 0, "total_bytes": 0}
    rotation_totals = {
        rotation: {"rotation": rotation, "file_count": 0, "total_bytes": 0}
        for rotation in range(1, MAX_ROTATION + 1)
    }

    for spec in specs:
        for rotation in range(spec.backup_count + 1):
            path = _registered_candidate(spec, rotation)
            file_stat = _safe_lstat(path)
            if file_stat is None:
                continue
            _assert_regular(file_stat)
            bucket = current if rotation == 0 else rotated
            bucket["file_count"] += 1
            bucket["total_bytes"] += file_stat.st_size
            if rotation:
                rotation_totals[rotation]["file_count"] += 1
                rotation_totals[rotation]["total_bytes"] += file_stat.st_size

    selected = []
    if scope in {"current", "all"}:
        selected.append(current)
    if scope in {"rotated", "all"}:
        selected.append(rotated)
    total = {
        "file_count": sum(item["file_count"] for item in selected),
        "total_bytes": sum(item["total_bytes"] for item in selected),
    }
    return {
        "target_type": target_type,
        "target": target,
        "kind": kind,
        "scope": scope,
        "current": current,
        "rotated": rotated,
        "rotations": list(rotation_totals.values()),
        "total": total,
    }


def execute_cleanup(specs: tuple[LogFileSpec, ...], scope: str) -> CleanupResult:
    _validate_scope(scope)
    if not specs:
        raise ValueError("没有匹配的注册日志")
    results = []
    for spec in specs:
        for rotation in _selected_rotations(spec, scope):
            results.append(
                _truncate_current(spec)
                if rotation == 0
                else _unlink_rotation(spec, rotation)
            )
    return CleanupResult(
        target=",".join(spec.key for spec in specs),
        scope=scope,
        file_results=results,
    )
