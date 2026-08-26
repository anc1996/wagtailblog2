"""检查前端语义 token 桥接层和后台样式的颜色契约。"""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "wagtailblog3/static/css/site-token-bridge.css"
ADMIN_FILES = (
    ROOT / "wagtailblog3/static/search/css/admin_analytics.css",
    ROOT / "wagtailblog3/static/archive/css/admin_archive.css",
    ROOT / "wagtailblog3/templates/archive/admin/dashboard.html",
)
RAW_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}|\b(?:rgb|rgba|hsl|hsla)\(")
FORBIDDEN_LEGACY = re.compile(r"#(?:007bff|0056b3|28a745|218838|dc3545|d32f2f)", re.I)
DARK_OVERRIDE = re.compile(r":root\[data-theme=['\"]dark['\"]\]")


def main() -> int:
    """验证新增桥接层不携带 raw color，后台样式不恢复 Bootstrap 状态色。"""
    failures: list[str] = []
    bridge_text = BRIDGE.read_text(encoding="utf-8")
    if RAW_COLOR.search(bridge_text):
        failures.append(f"{BRIDGE}: 桥接层不得直接写颜色值")
    if DARK_OVERRIDE.search(bridge_text):
        failures.append(f"{BRIDGE}: duplicate dark-theme override")
    for path in ADMIN_FILES:
        text = path.read_text(encoding="utf-8")
        if FORBIDDEN_LEGACY.search(text):
            failures.append(f"{path}: 发现未收敛的 Bootstrap 状态色")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"CSS token lint passed: bridge + {len(ADMIN_FILES)} admin styles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
