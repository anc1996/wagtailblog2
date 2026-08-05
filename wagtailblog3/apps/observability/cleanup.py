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
        """返回实际被截断或删除的文件名，不包含本来不存在的候选项。"""
        return [
            result["file"]
            for result in self.file_results
            if result["outcome"] in {"truncated", "unlinked"}
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
    # 清理目标只能来自注册表，随后逐级检查父目录，阻断路径穿越和符号链接跳转。
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


def _unlink_rotation(spec: LogFileSpec, rotation: int) -> dict:
    """隔离并删除一个轮转历史文件。

    参数：``spec`` 为注册日志项，``rotation`` 为大于 0 的允许轮转编号。
    返回：逐文件执行结果；不存在的历史文件是成功空操作。
    异常：安全或文件系统错误记录在返回结果中。

    先原子改名到随机隔离名，再删除隔离文件，避免并发轮转在路径复用时误删
    新产生的同名文件。
    """
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

        # 原子改名是文件所有权边界：成功后，清理过程只持有隔离文件，
        # 即使日志路径被新的写入进程替换，也不会误删新文件。
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
    """只读取文件元数据并生成清理预览。

    参数：``specs`` 为受控注册项，目标字段用于回显，``scope`` 决定预计范围。
    返回：当前文件、轮转文件、各轮转编号及合计的数量和字节数。
    异常：注册路径不安全或范围非法时抛出相应异常。

    预览与实际执行使用同一轮转上限，避免确认页显示的删除面和实际删除面不一致。
    """
    _validate_scope(scope)
    if not specs:
        raise ValueError("没有匹配的注册日志")

    current = {"file_count": 0, "total_bytes": 0}
    rotated = {"file_count": 0, "total_bytes": 0}
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
    """执行受控日志清理并汇总逐文件结果。

    参数：``specs`` 必须来自注册表，``scope`` 为合法清理范围。
    返回：``CleanupResult``，其中保留每个候选文件的执行证据。
    异常：没有目标或范围非法时抛出 ``ValueError``；单文件安全失败不会中断
    其他文件的处理，而会记录在结果中。
    """
    _validate_scope(scope)
    if not specs:
        raise ValueError("没有匹配的注册日志")
    # 当前文件采用截断以保持写入句柄有效，历史轮转文件采用隔离后删除。
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
