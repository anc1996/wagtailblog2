# -*- coding: utf-8 -*-
"""
Django 管理命令：只读核对 Wagtail Page 外置正文完整性。

契约与规范：
1. 纯只读：严禁保存、修改、物理删除数据，严禁自动调用 ensure_indexes() 修复索引；
2. 参数支持：
   --page-id: 仅核查指定的 Page ID（正整数，严格拒绝 bool）
   --batch-size: 批次迭代大小（限制在 1 到 1000 之间，默认 100，严格拒绝 bool）
   --format: text（人类可读文本摘要）或 json（机器结构化统计）
   --no-check-indexes: 跳过索引检查
3. 退出码：
   - 若发现完整性问题或异常，以非零状态码退出（如 sys.exit(1)）；
   - 若全量核查通过无缺陷，正常退出（退出码 0）。
4. 严格脱敏：
   仅输出统计数据与有限错误码，严禁输出正文 payload、密码或 raw exception trace。
"""

from __future__ import annotations

import json
import sys
from typing import Any, Optional

from django.core.management.base import BaseCommand, CommandError

from external_content.integrity import (
    DEFAULT_BATCH_SIZE,
    MAX_BATCH_SIZE,
    MIN_BATCH_SIZE,
    ExternalContentIntegrityVerifier,
)


class Command(BaseCommand):
    help = "只读核对 Wagtail Page 外置正文完整性（严格只读、不修改数据、不自动建索引、脱敏统计输出）"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--page-id",
            type=int,
            default=None,
            dest="page_id",
            help="指定核查的单个 Wagtail Page ID（可选，默认扫描全部）",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=DEFAULT_BATCH_SIZE,
            dest="batch_size",
            help=f"分批迭代扫描内存批次大小（默认 {DEFAULT_BATCH_SIZE}，有效范围 [{MIN_BATCH_SIZE}, {MAX_BATCH_SIZE}]）",
        )
        parser.add_argument(
            "--format",
            choices=["text", "json"],
            default="text",
            dest="format",
            help="输出格式：text（人类可读摘要）或 json（机器结构化统计）",
        )
        parser.add_argument(
            "--no-check-indexes",
            action="store_true",
            dest="no_check_indexes",
            help="跳过 MongoDB 唯一索引完备性检查（默认执行检查）",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        """
        执行外置正文只读完整性核查命令。

        校验参数边界、启动只读核查器并按指定格式脱敏输出，核查未通过时非零退出。
        """
        batch_size = options.get("batch_size", DEFAULT_BATCH_SIZE)
        if (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or batch_size < MIN_BATCH_SIZE
            or batch_size > MAX_BATCH_SIZE
        ):
            raise CommandError(
                f"--batch-size 必须处于 [{MIN_BATCH_SIZE}, {MAX_BATCH_SIZE}] 范围内的整数且不能为布尔值，当前传入: {batch_size}"
            )

        page_id = options.get("page_id")
        if page_id is not None:
            if isinstance(page_id, bool) or not isinstance(page_id, int) or page_id <= 0:
                raise CommandError(
                    f"--page-id 必须为正整数且不能为布尔值，当前传入: {page_id}"
                )

        fmt = options.get("format", "text")
        no_check_indexes = bool(options.get("no_check_indexes", False))

        verifier = options.get("verifier")
        if verifier is None:
            verifier = ExternalContentIntegrityVerifier(
                batch_size=batch_size,
                check_indexes_flag=not no_check_indexes,
            )

        try:
            summary = verifier.verify(page_id=page_id)
        except Exception as e:
            raise CommandError(f"完整性核查执行异常: {type(e).__name__}: {str(e)}") from e

        if fmt == "json":
            self.stdout.write(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
        else:
            self.stdout.write("=== Wagtail Page 外部正文完整性核查报告 ===")
            self.stdout.write(f"核查页面总数: {summary.total_pages_checked}")
            self.stdout.write(f"核查修订总数: {summary.total_revisions_checked}")
            self.stdout.write(f"核查版本总数: {summary.total_versions_checked}")
            self.stdout.write(f"发现问题总数: {summary.total_issues_count}")
            self.stdout.write(f"索引就绪状态: {summary.indexes_ready}")
            self.stdout.write(f"最终判定结果: {'【通过】' if summary.passed else '【未通过】'}")
            if summary.issues:
                self.stdout.write(f"问题样本详情（最多展示 {len(summary.issues)} 项）:")
                for issue in summary.issues:
                    self.stdout.write(f"  - [{issue.error_code}] Page {issue.page_id}: {issue.message}")

        if not summary.passed:
            sys.exit(1)
