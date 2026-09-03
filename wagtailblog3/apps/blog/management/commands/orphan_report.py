"""只读扫描与受控清理 Mongo 正文孤儿数据。

命令把 Mongo 三个正文集合与 MySQL 页面、正文指针、删除意图及搜索事件交叉核对，
支持只读输出诊断 JSON（默认行为）。
当显式传入 `--apply --yes` 时，受控清理经强阻断 Fencing Token 复核确认的完全孤儿数据。
"""

from __future__ import annotations

import json
import sys
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from blog.services.mongo_orphan import MongoOrphanService


class Command(BaseCommand):
    """生成 Mongo 正文孤儿候选清单，或在明确授权下执行安全清理。"""

    help = "扫描或受控清理 Mongo 正文孤儿数据（默认只读，清理需 --apply --yes）"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="显式只读模式（默认行为）")
        parser.add_argument("--page-id", type=int, help="只报告指定页面编号（用于定向核验）")
        parser.add_argument("--limit", type=int, default=1000, help="最多输出候选行数，范围 1--5000")
        parser.add_argument("--batch-size", type=int, default=500, help="Mongo 游标批大小，范围 50--5000")
        parser.add_argument("--apply", action="store_true", help="受控清理孤儿正文；必须同时指定 --yes")
        parser.add_argument("--yes", action="store_true", help="确认执行物理删除操作")

    def handle(self, *args: object, **options: object) -> None:
        is_apply = bool(options.get("apply"))
        is_yes = bool(options.get("yes"))

        if is_apply and not is_yes:
            # 在非交互或未传 --yes 时直接阻断抛出 CommandError，防止误触
            if not sys.stdin.isatty():
                raise CommandError("执行 --apply 必须同时指定 --yes 明确授权清理，避免无意误操作")
            confirm = input("警告：即将对完全无主的 Mongo 孤儿正文执行物理删除！输入 yes 确认: ")
            if confirm.strip().lower() != "yes":
                raise CommandError("操作已取消：未获得用户确认")

        limit = max(1, min(int(options.get("limit") or 1000), 5000))
        batch_size = max(50, min(int(options.get("batch_size") or 500), 5000))
        page_filter = options.get("page_id")

        # 执行扫描
        scan_result = MongoOrphanService.scan_orphans(
            limit=limit,
            batch_size=batch_size,
            page_filter=page_filter,
        )

        if not is_apply:
            report = {
                "report": "mongo-orphan-v1",
                "read_only": True,
                "dry_run": True,
                "page_filter": page_filter,
                "collections": scan_result["collections"],
                "category_counts": scan_result["category_counts"],
                "candidate_count": scan_result["candidate_count"],
                "emitted_count": scan_result["emitted_count"],
                "truncated": scan_result["truncated"],
                "candidates": scan_result["candidates"],
                "mongo_error": scan_result["mongo_error"],
            }
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return

        # 受控清理模式：仅清理分类为 orphan_candidate 的完全孤儿
        candidates = scan_result.get("candidates", [])
        orphan_candidates = [c for c in candidates if c.get("category") == "orphan_candidate"]

        deleted_records: list[dict[str, Any]] = []
        failed_records: list[dict[str, Any]] = []

        for cand in orphan_candidates:
            coll = str(cand["collection"])
            m_id = str(cand["mongo_id"])
            try:
                del_res = MongoOrphanService.delete_orphan_document(
                    coll,
                    m_id,
                    actor="CLI:orphan_report",
                )
                deleted_records.append(del_res)
            except Exception as exc:
                failed_records.append({
                    "collection": coll,
                    "mongo_id": m_id,
                    "error": str(exc),
                })

        cleanup_report = {
            "report": "mongo-orphan-v1-applied",
            "read_only": False,
            "dry_run": False,
            "page_filter": page_filter,
            "total_orphan_candidates": len(orphan_candidates),
            "deleted_count": len(deleted_records),
            "failed_count": len(failed_records),
            "deleted_records": deleted_records,
            "failed_records": failed_records,
        }
        self.stdout.write(json.dumps(cleanup_report, ensure_ascii=False, sort_keys=True))

    # 兼容历史测试用例直接访问 command 内部方法的接口
    def _collect_mysql_context(self) -> dict[str, Any]:
        return MongoOrphanService.collect_mysql_context()

    @staticmethod
    def _classify_document(name: str, document: dict[str, Any], context: dict[str, Any]) -> tuple[str, int | None]:
        return MongoOrphanService.classify_document(name, document, context)
