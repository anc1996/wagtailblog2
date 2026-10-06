# -*- coding: utf-8 -*-
"""
Wagtail Page 正文外置通用协议 - 只读完整性核查与管理命令纯 Fake 单元测试套件。

测试原则与约束：
1. 纯 Fake/Mock 隔离：禁止调用 django.setup()，禁止真实数据库、Docker 或网络；
2. 真实红绿测：拒绝将 assertRaises(NotImplementedError) 作为业务红测，期望真实契约与业务产物；
3. 最小闭环覆盖：参数 bool 拒绝、有界 summary 样本截断但总量精确、干净空集合零写只读安全；
4. 依赖解耦：不直接在顶层导入 wagtail.models 或 external_content.manifest/store。
"""

from __future__ import annotations

import argparse
from io import StringIO
import json
from typing import Any, Dict, List, Optional
import unittest
from unittest.mock import MagicMock, patch

from django.core.management.base import CommandError

from external_content.integrity import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_SAMPLE_LIMIT,
    MAX_BATCH_SIZE,
    MIN_BATCH_SIZE,
    ExternalContentIntegrityVerifier,
    IntegrityErrorCode,
    IntegrityIssue,
    IntegritySummary,
)
from external_content.management.commands.verify_external_content_integrity import (
    Command,
)


class StrictFakeStore:
    """
    纯 Fake 存储替身，用于断言只读安全性与零写入。
    任何写操作（save/insert/update/delete）或 ensure_indexes 均会立即抛出断言错误。
    """

    def __init__(self, indexes_ready: bool = True) -> None:
        self.indexes_ready = indexes_ready
        self.write_calls: List[str] = []
        self.ensure_indexes_called = False

    def check_indexes(self) -> bool:
        return self.indexes_ready

    def ensure_indexes(self) -> Any:
        self.ensure_indexes_called = True
        raise AssertionError("严禁在完整性核查中调用 ensure_indexes！")

    def save(self, *args: Any, **kwargs: Any) -> Any:
        self.write_calls.append("save")
        raise AssertionError("只读核查中严禁调用 save！")

    def update(self, *args: Any, **kwargs: Any) -> Any:
        self.write_calls.append("update")
        raise AssertionError("只读核查中严禁调用 update！")

    def delete(self, *args: Any, **kwargs: Any) -> Any:
        self.write_calls.append("delete")
        raise AssertionError("只读核查中严禁调用 delete！")

    def insert(self, *args: Any, **kwargs: Any) -> Any:
        self.write_calls.append("insert")
        raise AssertionError("只读核查中严禁调用 insert！")


class StrictFakeQuerySet:
    """
    纯 Fake QuerySet 替身，模拟 Django QuerySet 的空流式迭代行为。
    """

    def __init__(self, items: Optional[List[Any]] = None) -> None:
        self.items = items if items is not None else []
        self.iterator_chunk_sizes: List[int] = []

    def filter(self, *args: Any, **kwargs: Any) -> "StrictFakeQuerySet":
        return self

    def iterator(self, chunk_size: int = 100):
        self.iterator_chunk_sizes.append(chunk_size)
        yield from self.items

    def all(self) -> "StrictFakeQuerySet":
        return self

    def count(self) -> int:
        return len(self.items)

    def __iter__(self):
        yield from self.items


class IntegrityDataStructuresAndSanitizationTests(unittest.TestCase):
    """
    数据类、有限正交错误码枚举与脱敏契约测试。
    """

    def test_error_codes_completeness_and_orthogonality(self) -> None:
        """
        验证 11 个有限正交错误码全部存在且唯一。
        """
        expected_codes = {
            "INDEX_MISSING",
            "PAGE_MISSING",
            "POLICY_MISMATCH",
            "STATE_INVALID",
            "MANIFEST_INVALID",
            "REVISION_MISSING",
            "REVISION_IDENTITY_MISMATCH",
            "VERSION_MISSING",
            "IDENTITY_MISMATCH",
            "HASH_MISMATCH",
            "ALIAS_INVALID",
        }
        actual_codes = {choice[0] for choice in IntegrityErrorCode.choices}
        self.assertEqual(actual_codes, expected_codes)
        self.assertEqual(len(IntegrityErrorCode), 11)

    def test_integrity_issue_sanitization_no_payload_or_credentials(self) -> None:
        """
        验证 IntegrityIssue 及其序列化字典严禁包含正文内容、密码或敏感信息。
        """
        issue = IntegrityIssue(
            error_code=IntegrityErrorCode.HASH_MISMATCH,
            page_id=101,
            field_name="body",
            version_id="ver-12345",
            revision_id=55,
            message="正文哈希不匹配",
            details={"expected_hash": "a" * 64, "actual_hash": "b" * 64},
        )
        data = issue.to_dict()
        self.assertEqual(data["error_code"], "HASH_MISMATCH")
        self.assertEqual(data["page_id"], 101)
        self.assertEqual(data["field_name"], "body")
        self.assertEqual(data["version_id"], "ver-12345")
        self.assertEqual(data["revision_id"], 55)

        serialized = json.dumps(data)
        self.assertNotIn("payload", serialized)
        self.assertNotIn("password", serialized)
        self.assertNotIn("token", serialized)
        self.assertNotIn("secret", serialized)


class IntegrityParameterValidationTests(unittest.TestCase):
    """
    参数类型防护与边界校验测试：严格拒绝 bool 伪装 int，严格校验有效范围。
    """

    def test_verifier_batch_size_rejects_bool(self) -> None:
        """
        验证 ExternalContentIntegrityVerifier 显式拒绝 bool 类型的 batch_size。
        """
        with self.assertRaises(ValueError):
            ExternalContentIntegrityVerifier(batch_size=True)
        with self.assertRaises(ValueError):
            ExternalContentIntegrityVerifier(batch_size=False)

    def test_verifier_batch_size_boundaries(self) -> None:
        """
        验证 ExternalContentIntegrityVerifier 对 batch_size 的有界硬约束 [1, 1000]。
        """
        v1 = ExternalContentIntegrityVerifier(batch_size=MIN_BATCH_SIZE)
        self.assertEqual(v1.batch_size, 1)
        v1000 = ExternalContentIntegrityVerifier(batch_size=MAX_BATCH_SIZE)
        self.assertEqual(v1000.batch_size, 1000)

        with self.assertRaises(ValueError):
            ExternalContentIntegrityVerifier(batch_size=0)
        with self.assertRaises(ValueError):
            ExternalContentIntegrityVerifier(batch_size=-1)
        with self.assertRaises(ValueError):
            ExternalContentIntegrityVerifier(batch_size=1001)
        with self.assertRaises(ValueError):
            ExternalContentIntegrityVerifier(batch_size="100")  # type: ignore

    def test_verifier_sample_limit_rejects_bool_and_non_positive(self) -> None:
        """
        验证 sample_limit 参数严格为正整数，拒绝 bool 及 <= 0。
        """
        with self.assertRaises(ValueError):
            ExternalContentIntegrityVerifier(sample_limit=True)
        with self.assertRaises(ValueError):
            ExternalContentIntegrityVerifier(sample_limit=False)
        with self.assertRaises(ValueError):
            ExternalContentIntegrityVerifier(sample_limit=0)
        with self.assertRaises(ValueError):
            ExternalContentIntegrityVerifier(sample_limit=-10)

    def test_verifier_verify_page_id_rejects_bool_and_non_positive(self) -> None:
        """
        验证 verify(page_id=...) 严格校验正整数，拒绝 bool 及 <= 0。
        """
        fake_store = StrictFakeStore(indexes_ready=True)
        fake_pages = StrictFakeQuerySet([])
        verifier = ExternalContentIntegrityVerifier(
            store=fake_store,
            page_source=fake_pages,
        )
        with self.assertRaises(ValueError):
            verifier.verify(page_id=True)
        with self.assertRaises(ValueError):
            verifier.verify(page_id=False)
        with self.assertRaises(ValueError):
            verifier.verify(page_id=0)
        with self.assertRaises(ValueError):
            verifier.verify(page_id=-1)

    def test_command_rejects_bool_and_out_of_bound_parameters(self) -> None:
        """
        验证 Django 管理命令在接收 bool 或越界参数时抛出 CommandError。
        """
        cmd = Command()
        with self.assertRaises(CommandError):
            cmd.handle(batch_size=True)
        with self.assertRaises(CommandError):
            cmd.handle(batch_size=0)
        with self.assertRaises(CommandError):
            cmd.handle(batch_size=1001)
        with self.assertRaises(CommandError):
            cmd.handle(page_id=True)
        with self.assertRaises(CommandError):
            cmd.handle(page_id=0)
        with self.assertRaises(CommandError):
            cmd.handle(page_id=-5)


class IntegrityBoundedSampleSummaryTests(unittest.TestCase):
    """
    有界 sample 样本截断但总量精确测试。
    """

    def test_summary_bounded_sample_with_accurate_totals(self) -> None:
        """
        验证 IntegritySummary 样本列表被截断至 sample_limit，但 total_issues_count 和 issues_by_code 必须全量准确。
        """
        summary = IntegritySummary(sample_limit=3)
        self.assertTrue(summary.passed)
        self.assertEqual(summary.total_issues_count, 0)
        self.assertEqual(len(summary.issues), 0)
        self.assertFalse(summary.issues_truncated)

        # 连续添加 5 个 issue（超出上限 3）
        for i in range(5):
            summary.add_issue(
                IntegrityIssue(
                    error_code=IntegrityErrorCode.VERSION_MISSING,
                    page_id=i + 1,
                    message=f"Missing version {i}",
                )
            )

        self.assertFalse(summary.passed)
        # 总计数与分类统计必须全量精确，记录 5 个
        self.assertEqual(summary.total_issues_count, 5)
        self.assertEqual(summary.issues_by_code[IntegrityErrorCode.VERSION_MISSING], 5)
        # 样本明细列表严格受限在 3 个
        self.assertEqual(len(summary.issues), 3)
        self.assertTrue(summary.issues_truncated)

        data = summary.to_dict()
        self.assertEqual(data["total_issues_count"], 5)
        self.assertEqual(len(data["issues"]), 3)
        self.assertTrue(data["issues_truncated"])
        self.assertFalse(data["passed"])


class CleanEmptyCollectionZeroWriteTests(unittest.TestCase):
    """
    干净空集合只读零写与通过契约测试。
    """

    def test_verifier_clean_empty_collection_read_only_zero_write(self) -> None:
        """
        验证当核查目标为空集合时，核查结果返回 passed=True, total_issues_count=0，且写调用次数严格为 0。
        """
        fake_store = StrictFakeStore(indexes_ready=True)
        fake_pages = StrictFakeQuerySet([])

        verifier = ExternalContentIntegrityVerifier(
            store=fake_store,
            batch_size=100,
            page_source=fake_pages,
            check_indexes_flag=True,
        )
        summary = verifier.verify()

        self.assertTrue(summary.passed)
        self.assertEqual(summary.total_issues_count, 0)
        self.assertEqual(summary.total_pages_checked, 0)
        self.assertTrue(summary.indexes_checked)
        self.assertTrue(summary.indexes_ready)

        # 严格零写入与无 ensure_indexes 调用
        self.assertEqual(len(fake_store.write_calls), 0)
        self.assertFalse(fake_store.ensure_indexes_called)

    def test_command_handle_clean_empty_zero_exit(self) -> None:
        """
        验证管理命令在核查通过时正常执行并退出（退出码为 0，不抛 SystemExit(1)），输出格式符合脱敏 JSON。
        """
        fake_store = StrictFakeStore(indexes_ready=True)
        fake_pages = StrictFakeQuerySet([])
        verifier = ExternalContentIntegrityVerifier(
            store=fake_store,
            page_source=fake_pages,
        )
        cmd = Command()
        out = StringIO()
        with patch.object(cmd, "stdout", out):
            cmd.handle(
                batch_size=100,
                format="json",
                no_check_indexes=False,
                verifier=verifier,
            )
        output_text = out.getvalue()
        parsed = json.loads(output_text)
        self.assertTrue(parsed["passed"])
        self.assertEqual(parsed["total_issues_count"], 0)
