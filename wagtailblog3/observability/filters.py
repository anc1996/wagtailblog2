"""Reusable logging filters."""

from __future__ import annotations

import logging


class ModuleFilter(logging.Filter):
    """Allow exact logger namespaces and descendants, never prefix collisions."""

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
    """Allow records up to and including ``max_level``."""

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
