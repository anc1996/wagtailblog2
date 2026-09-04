"""安全的日志清理预览与文件级执行。"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import stat
import time
import uuid

from django.conf import settings

from .registry import LOG_FILE_SPECS, LogFileSpec


VALID_SCOPES = {"current", "rotated", "all"}
MAX_ROTATION = max(spec.backup_count for spec in LOG_FILE_SPECS)
SAFE_ORPHAN_AGE_SECONDS = 60


class UnsafeLogTarget(ValueError):
    """目标不再是注册表指向的安全普通文件。"""


@dataclass(slots=True)
class CleanupResult:
    target: str
    scope: str
    file_results: list[dict]

    @property
    def files_before(self) -> int:
        """返回执行前实际存在的文件数。

        返回值只统计已经存在的候选文件；不存在的轮转编号是安全的空操作，
        不应让预览或审计误报为已清理文件。
        """
        return sum(int(result["pre_exists"]) for result in self.file_results)

    @property
    def bytes_before(self) -> int:
        """返回清理前实际存在日志文件的总字节数。"""
        return sum(result["bytes_before"] for result in self.file_results)

    @property
    def bytes_freed(self) -> int:
        """返回截断或删除后释放的磁盘字节数。"""
        return sum(result["bytes_freed"] for result in self.file_results)

    @property
    def failed_files(self) -> list[dict[str, str]]:
        """返回失败文件及原因，供审计记录和后台错误提示使用。"""
        return [
            {"file": result["file"], "error": result["error"]}
            for result in self.file_results
            if not result["succeeded"]
        ]

    @property
    def changed_files(self) -> list[str]:
        """返回实际被截断或删除的文件名，包含标准轮转与已清理孤儿文件。"""
        return [
            result["file"]
            for result in self.file_results
            if result["outcome"] in {"truncated", "unlinked", "unlinked_orphan"}
        ]

    @property
    def succeeded(self) -> bool:
        """所有候选操作均未失败时返回 ``True``。"""
        return not self.failed_files


def _validate_scope(scope: str) -> None:
    """校验清理范围。

    参数：``scope`` 为 current、rotated 或 all。
    返回：无。
    异常：范围不在白名单中时抛出 ``ValueError``，防止调用方扩大删除面。
    """
    if scope not in VALID_SCOPES:
        raise ValueError("不支持的清理范围")


def _selected_rotations(spec: LogFileSpec, scope: str):
    """按范围产出一个注册日志允许处理的轮转编号。

    参数：``spec`` 定义最大备份数，``scope`` 定义当前/历史范围。
    返回：生成器，0 表示当前文件，正数表示 ``.N`` 轮转文件。
    异常：``scope`` 非法时由 ``_validate_scope`` 抛出 ``ValueError``。
    """
    _validate_scope(scope)
    if scope in {"current", "all"}:
        yield 0
    if scope in {"rotated", "all"}:
        yield from range(1, spec.backup_count + 1)


def _relative_name(spec: LogFileSpec, rotation: int) -> str:
    """根据注册项与轮转编号生成相对文件名。

    参数：``spec`` 为注册项，``rotation`` 为 0 或允许的备份编号。
    返回：不含日志根目录的 POSIX 相对路径。
    异常：无；编号边界由调用方校验。
    """
    return spec.relative_path if rotation == 0 else f"{spec.relative_path}.{rotation}"


def _registered_candidate(spec: LogFileSpec, rotation: int) -> Path:
    """返回已验证的注册日志候选路径。

    参数：``spec`` 只能来自日志注册表，``rotation`` 只能在其备份范围内。
    返回：位于 ``settings.LOG_DIR`` 下的候选路径。
    异常：路径越界、符号链接目录、非目录父级或编号越界时抛出
    ``UnsafeLogTarget``。

    该函数刻意不接受客户端路径，避免清理功能变成任意文件删除入口。
    """
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


def _parent_directory_for_spec(spec: LogFileSpec) -> Path:
    """返回日志注册项对应的受控父目录，并校验父目录未越界且非符号链接。"""
    relative = PurePosixPath(spec.relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise UnsafeLogTarget("catalog 日志路径无效")
    root = Path(settings.LOG_DIR).resolve()
    candidate_parent = root.joinpath(*relative.parts[:-1])
    resolved_parent = candidate_parent.resolve(strict=False)
    if resolved_parent != root and root not in resolved_parent.parents:
        raise UnsafeLogTarget("日志路径越过允许目录")
    if not candidate_parent.exists():
        return candidate_parent
    parent_stat = _safe_lstat(candidate_parent)
    if parent_stat is not None and stat.S_ISLNK(parent_stat.st_mode):
        raise UnsafeLogTarget("日志路径包含符号链接目录")
    return candidate_parent


def discover_orphan_rotations(
    spec: LogFileSpec,
    *,
    min_age_seconds: int = SAFE_ORPHAN_AGE_SECONDS,
) -> list[dict]:
    """发现并校验与指定日志关联的孤儿轮转临时文件及异常隔离残留。

    参数：
        spec: 来自注册表的合法日志规格。
        min_age_seconds: 最小静默时间（秒），默认 60 秒。未满足该时间的文件视为可能处于并发轮转竞态中，跳过清理。
    返回：
        列表，包含孤儿文件详细信息的字典：
        {
            "path": Path,
            "relative_path": str,
            "name": str,
            "size": int,
            "mtime": float,
            "is_safe": bool,
            "skip_reason": str,
        }
    """
    parent = _parent_directory_for_spec(spec)
    if not parent.exists() or not parent.is_dir():
        return []

    stem = PurePosixPath(spec.relative_path).name
    rotate_prefix = f"{stem}.rotate."
    cleanup_prefix = f".{stem}."
    cleanup_suffix = ".cleanup-"
    now = time.time()
    orphans = []

    try:
        entries = list(parent.iterdir())
    except OSError:
        return []

    for entry in entries:
        name = entry.name
        # 严格匹配关联当前 spec 的孤儿轮转文件与隔离残留
        is_rotate_orphan = name.startswith(rotate_prefix) and len(name) > len(rotate_prefix)
        is_cleanup_orphan = name.startswith(cleanup_prefix) and cleanup_suffix in name
        if not (is_rotate_orphan or is_cleanup_orphan):
            continue

        file_stat = _safe_lstat(entry)
        if file_stat is None:
            continue

        # 必须是普通文件，严禁处理符号链接、目录或设备文件
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
            continue

        rel_parts = list(PurePosixPath(spec.relative_path).parts[:-1]) + [name]
        relative_path = "/".join(rel_parts)
        age = now - file_stat.st_mtime
        is_safe = age >= min_age_seconds
        skip_reason = "" if is_safe else f"文件产生仅 {age:.1f} 秒，低于 {min_age_seconds} 秒保护窗口"

        orphans.append({
            "path": entry,
            "relative_path": relative_path,
            "name": name,
            "size": file_stat.st_size,
            "mtime": file_stat.st_mtime,
            "is_safe": is_safe,
            "skip_reason": skip_reason,
        })

    return orphans


def _safe_lstat(path: Path):
    """读取路径元数据并把不存在转换为 ``None``。

    参数：``path`` 为待检查路径。
    返回：``os.stat_result`` 或 ``None``。
    异常：除文件不存在外的系统错误继续交给调用方处理。
    """
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _identity(file_stat) -> tuple[int, int]:
    """提取设备号与 inode，作为文件身份而非路径身份。"""
    return file_stat.st_dev, file_stat.st_ino


def _assert_regular(file_stat) -> None:
    """确认目标是普通文件。

    参数：``file_stat`` 为 ``lstat`` 或 ``fstat`` 的结果。
    返回：无。
    异常：符号链接或非普通文件时抛出 ``UnsafeLogTarget``；这样不会误处理
    FIFO、设备文件或被替换的链接。
    """
    if stat.S_ISLNK(file_stat.st_mode):
        raise UnsafeLogTarget("拒绝清理符号链接")
    if not stat.S_ISREG(file_stat.st_mode):
        raise UnsafeLogTarget("清理目标不是普通文件")


def _base_result(spec: LogFileSpec, rotation: int, action: str) -> dict:
    """创建统一的逐文件审计结果骨架。

    参数：``spec``、``rotation`` 和 ``action`` 描述受控目标与操作。
    返回：包含清理前后身份、字节数、结果和错误字段的字典。
    异常：无。
    """
    return {
        "source_key": spec.key,
        "source_path": spec.relative_path,
        "file": _relative_name(spec, rotation) if rotation >= 0 else spec.relative_path,
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
    """将预初始化结果标记为失败并保留可审计的错误文本。"""
    result["succeeded"] = False
    result["outcome"] = "failed"
    result["error"] = str(exc)
    return result


def _restore_quarantined(path: Path, quarantine: Path) -> bool:
    """恢复发生竞争的目标文件，但不覆盖后来生成的新路径。"""
    try:
        os.link(quarantine, path, follow_symlinks=False)
    except (FileExistsError, OSError):
        return False
    os.unlink(quarantine)
    return True


def _truncate_current(spec: LogFileSpec) -> dict:
    """安全地原地截断当前日志文件。

    参数：``spec`` 为注册日志项。
    返回：逐文件执行结果；文件缺失时返回成功的 ``already_absent`` 结果。
    异常：预期的文件与安全错误被写入结果而非向上抛出。

    当前文件必须保留 inode，避免正在写入的 Django/Celery handler 继续指向
    已删除文件；因此此处不用 rename 或 unlink。
    """
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

        # 通过已打开的文件描述符截断，避免路径在检查和写入之间被替换。
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

        # 并发写入进程可能已经写入新内容；旧内容仍由文件截断操作完整释放。
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


def _unlink_candidate(
    spec: LogFileSpec,
    path: Path,
    relative_name: str,
    rotation: int,
    action: str = "unlink",
    outcome_name: str = "unlinked",
) -> dict:
    """安全隔离并删除一个指定的轮转或孤儿历史文件。"""
    result = _base_result(spec, rotation, action)
    result["file"] = relative_name
    quarantine = None
    try:
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
            raise UnsafeLogTarget("目标日志在删除前已被替换，已拒绝删除")

        # 原子改名是文件所有权边界：成功后，清理过程只持有隔离文件，
        # 即使日志路径被新的写入进程替换，也不会误删新文件。
        quarantine = path.with_name(f".{path.name}.cleanup-{uuid.uuid4().hex}")
        os.rename(path, quarantine)
        isolated = _safe_lstat(quarantine)
        if isolated is None:
            raise UnsafeLogTarget("目标日志隔离后消失")
        _assert_regular(isolated)
        if _identity(isolated) != _identity(before):
            restored = _restore_quarantined(path, quarantine)
            quarantine = None if restored else quarantine
            suffix = "并已恢复原路径" if restored else f"；隔离副本保留为 {quarantine.name}"
            raise UnsafeLogTarget(f"目标日志在隔离前已被替换，已拒绝删除{suffix}")

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
            raise UnsafeLogTarget("目标日志删除后路径仍然存在")
        result["bytes_freed"] = before.st_size
        result["outcome"] = outcome_name
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


def _unlink_rotation(spec: LogFileSpec, rotation: int) -> dict:
    """隔离并删除一个标准轮转历史文件（.1 至 .N）。"""
    path = _registered_candidate(spec, rotation)
    return _unlink_candidate(
        spec,
        path,
        _relative_name(spec, rotation),
        rotation,
        action="unlink",
        outcome_name="unlinked",
    )


def _unlink_orphan_file(spec: LogFileSpec, path: Path, relative_name: str) -> dict:
    """隔离并删除一个孤儿轮转临时文件。"""
    return _unlink_candidate(
        spec,
        path,
        relative_name,
        rotation=-1,
        action="unlink_orphan",
        outcome_name="unlinked_orphan",
    )


def preview_cleanup(
    specs: tuple[LogFileSpec, ...],
    *,
    target_type: str,
    target: str,
    kind: str,
    scope: str,
) -> dict:
    """只读取文件元数据并生成清理预览。

    参数：``specs`` 为受控注册项，目标字段用于回显，``scope`` 决定预计范围。
    返回：当前文件、轮转文件、孤儿文件、各轮转编号及合计的数量和字节数。
    异常：注册路径不安全或范围非法时抛出相应异常。
    """
    _validate_scope(scope)
    if not specs:
        raise ValueError("没有匹配的注册日志")

    current = {"file_count": 0, "total_bytes": 0}
    rotated = {"file_count": 0, "total_bytes": 0}
    orphan = {"file_count": 0, "total_bytes": 0, "files": []}
    rotation_totals = {
        rotation: {"rotation": rotation, "file_count": 0, "total_bytes": 0}
        for rotation in range(1, MAX_ROTATION + 1)
    }

    # 预览只读取元数据，不执行截断或删除；返回值与实际执行结果使用相同的轮转范围。
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

        if scope in {"rotated", "all"}:
            discovered = discover_orphan_rotations(spec, min_age_seconds=0)
            for item in discovered:
                orphan["file_count"] += 1
                orphan["total_bytes"] += item["size"]
                orphan["files"].append({
                    "source_key": spec.key,
                    "file": item["relative_path"],
                    "size": item["size"],
                    "mtime": item["mtime"],
                    "is_safe": item["is_safe"],
                    "skip_reason": item["skip_reason"],
                })

    if scope in {"rotated", "all"}:
        rotated["file_count"] += orphan["file_count"]
        rotated["total_bytes"] += orphan["total_bytes"]

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
        "orphan": orphan,
        "rotations": list(rotation_totals.values()),
        "total": total,
    }


def execute_cleanup(specs: tuple[LogFileSpec, ...], scope: str) -> CleanupResult:
    """执行受控日志清理并汇总逐文件结果。

    参数：``specs`` 必须来自注册表，``scope`` 为合法清理范围。
    返回：``CleanupResult``，其中保留每个候选文件的执行证据。
    异常：没有目标或范围非法时抛出 ``ValueError``；单文件安全失败不会中断
    其他文件的处理，而会记录在结果中。
    """
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
        if scope in {"rotated", "all"}:
            orphans = discover_orphan_rotations(spec, min_age_seconds=SAFE_ORPHAN_AGE_SECONDS)
            for item in orphans:
                if item["is_safe"]:
                    results.append(
                        _unlink_orphan_file(spec, item["path"], item["relative_path"])
                    )
                else:
                    results.append({
                        "source_key": spec.key,
                        "source_path": spec.relative_path,
                        "file": item["relative_path"],
                        "rotation": -1,
                        "action": "skip_orphan",
                        "pre_exists": True,
                        "pre_device": None,
                        "pre_inode": None,
                        "bytes_before": item["size"],
                        "bytes_freed": 0,
                        "post_exists": True,
                        "post_device": None,
                        "post_inode": None,
                        "post_size": item["size"],
                        "inode_preserved": None,
                        "succeeded": True,
                        "outcome": "skipped_recent",
                        "error": item["skip_reason"],
                    })
    return CleanupResult(
        target=",".join(spec.key for spec in specs),
        scope=scope,
        file_results=results,
    )
