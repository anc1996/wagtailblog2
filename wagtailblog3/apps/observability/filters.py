"""可复用的日志过滤器。"""

from __future__ import annotations

import logging

from .sanitizer import project_relative_path


class ModuleFilter(logging.Filter):
    """允许精确的 logger 命名空间及其子级，避免前缀碰撞。"""

    def __init__(self, modules=None):
        super().__init__()
        self.modules = tuple(module.rstrip(".") for module in (modules or []))

    def filter(self, record):
        if not self.modules:
            return True
        return any(
            record.name == module or record.name.startswith(f"{module}.")
            for module in self.modules
        )


class MaxLevelFilter(logging.Filter):
    """允许级别不高于 ``max_level`` 的日志记录。"""

    def __init__(self, max_level="WARNING"):
        super().__init__()
        if isinstance(max_level, int):
            self.max_level = max_level
        else:
            self.max_level = logging._nameToLevel.get(
                str(max_level).upper(), logging.WARNING
            )

    def filter(self, record):
        return record.levelno <= self.max_level


class ProjectRelativePathFilter(logging.Filter):
    """为 formatter 提供不会越过项目根目录的 ``relative_path``。"""

    def __init__(self, base_dir=None):
        super().__init__()
        self.base_dir = base_dir

    def filter(self, record):
        record.relative_path = project_relative_path(
            getattr(record, "pathname", "") or getattr(record, "filename", ""),
            self.base_dir,
        )
        return True
