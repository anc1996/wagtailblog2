"""同步生产版油猴脚本至 tools/ 目录的维护工具脚本."""

import pathlib
import shutil
import sys


def sync_userscript() -> None:
    """将 wagtailblog3/static 中的唯一事实源脚本同步到 tools/ 目录方便复制."""
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    source = repo_root / "wagtailblog3" / "static" / "vendor" / "Script" / "downlaod_markdown.js"

    if not source.exists():
        print(f"[ERROR] 未找到脚本源码事实源: {source}", file=sys.stderr)
        sys.exit(1)

    targets = [
        repo_root / "tools" / "downlaod_markdown.user.js",
        repo_root / "tools" / "downlaod_markdown.js",
    ]

    for target in targets:
        shutil.copyfile(source, target)
        print(f"[OK] 已同步最新生产油猴脚本 -> {target.name} ({target.stat().st_size} bytes)")


if __name__ == "__main__":
    sync_userscript()
