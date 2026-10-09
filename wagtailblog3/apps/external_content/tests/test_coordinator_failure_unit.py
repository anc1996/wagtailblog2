"""
RB-02 / RB-08 安全边界定向单元测试：ExternalContentCoordinator 失败注入与原子性关闭。

本测试专注于验证协调器在多字段准备阶段遇到各类故障注入时的安全契约：
1. 第一字段确认写入、第二字段写前失败：无部分 Manifest 泄露，已创建版本保留，confirmed 账本仅记录已确认新建；
2. 第二字段实际写入后抛超时：未知写结果仍必须保留在仓储中（不可误判为未写），confirmed 账本严禁假造超时的返回 ID；
3. 既有六维版本复用后另一字段失败：复用版本不计入本次新建孤儿，页面既有正文与用户输入保持原样；
4. 故障取消后重试可复用相同六维不可变版本，不产生重复正文（不可变内容寻址去重，非 Web exactly-once）；
5. 物理删除调用始终为 0：版本仓储替身安装删除守卫，任何 delete/delete_version 调用均计数且直接报错；
6. 协调器告警日志仅包含安全的 page_id 与新建计数，严禁包含正文 Payload 片段或异常连接凭据。

所有测试均在 L0 无真实外部 I/O 隔离沙箱中运行，使用内存版本仓储替身与真实 StreamField 字段。
"""

from copy import deepcopy
import logging
from typing import Any, Optional, Tuple
from unittest.mock import patch

from django.test import SimpleTestCase
from wagtail.blocks import StreamValue

from external_content.codecs.streamfield import StreamFieldV1Codec
from external_content.coordinator import ExternalContentCoordinator
from external_content.exceptions import CodecPayloadError, ContentStoreError, FieldStorageConfigError
from external_content.manifest import ExternalContentManifest
from external_content.registry import ContentStoragePolicy, FieldStorageConfig
from external_content.tests.test_field_state import (
    FieldStatePage,
    MemoryVersionStore,
    payload,
)


class GuardedMemoryVersionStore(MemoryVersionStore):
    """
    带有物理删除守卫与失败注入能力的不可变版本仓储替身。

    安全设计：
    - 真实不可变版本仓储禁止同步物理删除，任何 delete/cleanup 操作必须被严格拦截；
    - 记录 delete 调用次数；一旦被调用直接抛出异常，防止静默放行；
    - 支持按字段注入写入后超时（模拟底层已写入但网络 ACK 丢失场景）与临时故障。
    """

    def __init__(self) -> None:
        super().__init__()
        self.delete_calls: int = 0
        self.write_fields: list[str] = []
        # 超时注入钩子：针对指定字段在写入底层字典后抛出异常
        self.post_write_timeout_fields: set[str] = set()
        # 预先故障注入钩子：针对指定字段直接抛出异常
        self.pre_write_fail_fields: set[str] = set()

    def delete(self, *args: Any, **kwargs: Any) -> None:
        """物理删除兜底拦截。"""
        self.delete_calls += 1
        raise AssertionError("COORDINATOR_MUST_NOT_DELETE_IMMUTABLE_VERSION: delete()")

    def delete_version(self, *args: Any, **kwargs: Any) -> None:
        """单版本物理删除拦截。"""
        self.delete_calls += 1
        raise AssertionError("COORDINATOR_MUST_NOT_DELETE_IMMUTABLE_VERSION: delete_version()")

    def cleanup_unreferenced_version(self, *args: Any, **kwargs: Any) -> None:
        """未引用版本清理拦截。"""
        self.delete_calls += 1
        raise AssertionError("COORDINATOR_MUST_NOT_DELETE_IMMUTABLE_VERSION: cleanup_unreferenced_version()")

    def save_version_with_status(self, **doc: Any) -> Tuple[str, bool]:
        """
        保存版本并返回 (version_id, is_new)。

        若配置了 pre_write_fail_fields，则在写前抛出异常；
        若配置了 post_write_timeout_fields，则在真实写入底层 docs 后抛出超时异常（模拟未知写结果）。
        """
        field_name = doc.get("field_name")
        self.write_fields.append(field_name)
        if field_name in self.pre_write_fail_fields:
            raise ContentStoreError(f"PRE_WRITE_INJECTED_FAILURE for field '{field_name}'")

        # 正常或超时前写入底层 docs
        version_id, is_new = super().save_version_with_status(**doc)

        if field_name in self.post_write_timeout_fields:
            # 模拟存储已完成写入，但向调用方反馈 ACK 时发生网络超时
            raise ContentStoreError(
                f"POST_WRITE_TIMEOUT_ON_ACK for field '{field_name}': write succeeded but ACK timed out"
            )

        return version_id, is_new


class CoordinatorFailureUnitTests(SimpleTestCase):
    """
    ExternalContentCoordinator 失败注入与安全边界单元测试（RB-02 / RB-08）。
    """

    def setUp(self) -> None:
        super().setUp()
        self.codec = StreamFieldV1Codec()
        self.policy = ContentStoragePolicy(
            model_label=FieldStatePage._meta.label_lower,
            schema_version=1,
            external_fields={
                name: FieldStorageConfig(name, "mongo", self.codec)
                for name in ("article", "appendix")
            },
        )
        self.store = GuardedMemoryVersionStore()
        self.coordinator = ExternalContentCoordinator(store=self.store)

    def _create_page(self, pk: int = 101, **values: Any) -> FieldStatePage:
        """创建具备虚拟主键的测试页面实例。"""
        return FieldStatePage(
            id=pk,
            page_ptr_id=pk,
            content_type_id=1,
            locale_id=1,
            **values,
        )

    def test_missing_page_identity_resets_only_current_attempt_diagnostics(self) -> None:
        """RB-01：新调用尚未写入即拒绝，不能把上次已确认 ID 当作本次孤儿。"""
        page = self._create_page(article=payload("历史正文"), appendix=[])
        self.coordinator.prepare_external_content_for_revision(page, policy=self.policy)
        self.assertEqual(len(self.coordinator.last_created_version_ids), 2)
        before = deepcopy(self.store.docs)
        attempts = list(self.store.write_fields)

        with self.assertRaises(FieldStorageConfigError):
            self.coordinator.prepare_external_content_for_revision(
                self._create_page(pk=None, article=[], appendix=[]), policy=self.policy
            )

        self.assertEqual(self.store.docs, before)
        self.assertEqual(self.store.write_fields, attempts)
        self.assertEqual(self.store.delete_calls, 0)
        self.assertEqual(self.coordinator.last_created_version_ids, [])

    def test_policy_failure_resets_diagnostics_and_propagates_original_error(self) -> None:
        """RB-01：策略解析失败仍属于本次零写入，保持异常类别与原异常对象。"""
        page = self._create_page(article=payload("历史正文"), appendix=[])
        self.coordinator.prepare_external_content_for_revision(page, policy=self.policy)
        self.assertEqual(len(self.coordinator.last_created_version_ids), 2)
        before = deepcopy(self.store.docs)
        attempts = list(self.store.write_fields)
        failure = FieldStorageConfigError("L0_POLICY_REJECTED")

        with patch("external_content.coordinator.get_storage_policy_for_model", side_effect=failure):
            with self.assertRaises(FieldStorageConfigError) as caught:
                self.coordinator.prepare_external_content_for_revision(page)

        self.assertIs(caught.exception, failure)
        self.assertEqual(self.store.docs, before)
        self.assertEqual(self.store.write_fields, attempts)
        self.assertEqual(self.store.delete_calls, 0)
        self.assertEqual(self.coordinator.last_created_version_ids, [])

    def test_second_field_codec_rejection_has_no_first_field_write(self) -> None:
        """RB-01：真实 codec 拒绝后字段 NaN，前字段也不能提前写入 Mongo。"""
        field = FieldStatePage._meta.get_field("appendix")
        invalid = StreamValue(
            field.stream_block,
            [{"type": "markdown_block", "value": float("nan"), "id": "invalid-number"}],
            is_lazy=True,
        )
        page = self._create_page(article=payload("前字段合法输入"), appendix=invalid)
        # 夹具保留原始非法值；不让 descriptor/块默认转换把故障变成合法空正文。
        self.assertIs(page.appendix, invalid)
        with self.assertRaises(CodecPayloadError):
            self.codec.encode(page.appendix, field=field)
        returned_manifest = None

        with self.assertRaises(CodecPayloadError):
            returned_manifest = self.coordinator.prepare_external_content_for_revision(
                page, policy=self.policy
            )

        self.assertIsNone(returned_manifest)
        self.assertEqual(page.article[0].value, "前字段合法输入")
        self.assertIs(page.appendix, invalid)
        self.assertEqual(self.store.delete_calls, 0)
        self.assertEqual(self.store.docs, {})
        self.assertEqual(self.store.write_fields, [])
        self.assertEqual(self.coordinator.last_created_version_ids, [])

    def test_later_unloaded_source_identity_failure_precedes_all_new_writes(self) -> None:
        """RB-01：后字段来源核验失败，不能把前字段的新编辑变成提前写入的孤儿。"""
        original = self._create_page(article=payload("来源正文"), appendix=payload("来源附录"))
        source = self.coordinator.prepare_external_content_for_revision(original, policy=self.policy)
        template = self._create_page(
            live=False, latest_revision_id=11, article=[], appendix=[]
        )
        names = [field.attname for field in FieldStatePage._meta.concrete_fields]
        page = FieldStatePage.from_db(
            "default", names, [template.__dict__.get(name) for name in names]
        )
        page.article = payload("本次新编辑")
        page._test_content_store = self.store
        page._bind_external_field_source("appendix", source, source="REVISION", revision_id=11)
        self.assertEqual(page.get_external_field_state("appendix").status, "UNLOADED")
        self.assertIs(page.get_external_field_state("appendix").manifest, source)
        self.store.docs[source.fields["appendix"].version_id]["page_id"] = 999
        before = deepcopy(self.store.docs)
        attempts = list(self.store.write_fields)
        returned_manifest = None

        with patch("external_content.coordinator.get_policy_for_version", return_value=self.policy):
            with self.assertRaises(ContentStoreError):
                returned_manifest = self.coordinator.prepare_external_content_for_revision(
                    page, policy=self.policy
                )

        self.assertIsNone(returned_manifest)
        self.assertEqual(page.article[0].value, "本次新编辑")
        self.assertEqual(self.store.delete_calls, 0)
        self.assertEqual(self.store.docs, before)
        self.assertEqual(self.store.write_fields, attempts)
        self.assertEqual(self.coordinator.last_created_version_ids, [])

    def test_first_field_confirmed_second_field_pre_write_failure(self) -> None:
        """
        边界 1：第一字段确认写入、第二字段写前失败。

        断言契约：
        - prepare_external_content_for_revision 抛出异常，无部分 Manifest 返回；
        - 第一字段新创建的版本安全保留在仓储中，不被回滚；
        - coordinator.last_created_version_ids 只含已确认新建的第一字段版本 ID；
        - 仓储替身删除计数为 0。
        """
        page = self._create_page(
            article=payload("第一字段合法正文内容"),
            appendix=payload("第二字段合法正文内容"),
        )
        self.store.pre_write_fail_fields.add("appendix")

        returned_manifest: Optional[ExternalContentManifest] = None
        with self.assertRaises(ContentStoreError):
            returned_manifest = self.coordinator.prepare_external_content_for_revision(
                page, policy=self.policy
            )

        # 1. 无部分 Manifest 返回（原子性失败关闭）
        self.assertIsNone(returned_manifest)

        # 2. 第一字段的新版本安全保留在仓储中
        self.assertEqual(len(self.store.docs), 1)
        created_vid = self.coordinator.last_created_version_ids[0]
        self.assertIn(created_vid, self.store.docs)
        self.assertEqual(self.store.docs[created_vid]["field_name"], "article")
        self.assertEqual(self.store.docs[created_vid]["page_id"], 101)

        # 3. last_created_version_ids 只包含已确认新建的 ID
        self.assertEqual(self.coordinator.last_created_version_ids, [created_vid])

        # 4. 严禁物理删除
        self.assertEqual(self.store.delete_calls, 0)

    def test_second_field_post_write_timeout_retains_unknown_result_without_faking_id(self) -> None:
        """
        边界 2：第二字段实际写入仓储后抛出超时（未知写结果）。

        断言契约：
        - 未知写结果仍保留在不可变仓储中，不能归类为未写（不被删除或丢弃）；
        - coordinator.last_created_version_ids 绝不假造超时的返回 ID，仅包含第一字段确认成功的新建 ID；
        - 仓储替身删除计数为 0。
        """
        page = self._create_page(
            article=payload("首字段确认正文"),
            appendix=payload("次字段写后超时正文", block_id="app-block-timeout"),
        )
        # 配置第二字段在实际写入底层 store 后触发超时异常
        self.store.post_write_timeout_fields.add("appendix")

        initial_docs_count = len(self.store.docs)
        self.assertEqual(initial_docs_count, 0)

        with self.assertRaises(ContentStoreError) as ctx:
            self.coordinator.prepare_external_content_for_revision(page, policy=self.policy)

        self.assertIn("POST_WRITE_TIMEOUT_ON_ACK", str(ctx.exception))

        # 1. 验证未知写结果已在底层持久化，且绝不被回滚删除（此时应包含 article 和 appendix 两个版本）
        self.assertEqual(len(self.store.docs), 2)
        appendix_docs = [doc for doc in self.store.docs.values() if doc["field_name"] == "appendix"]
        self.assertEqual(len(appendix_docs), 1)
        self.assertEqual(appendix_docs[0]["page_id"], 101)

        # 2. confirmed 列表不假造超时的返回 ID：仅包含第一字段成功返回的 ID
        self.assertEqual(len(self.coordinator.last_created_version_ids), 1)
        first_vid = self.coordinator.last_created_version_ids[0]
        self.assertEqual(self.store.docs[first_vid]["field_name"], "article")

        # 3. 仓储删除调用为 0
        self.assertEqual(self.store.delete_calls, 0)

    def test_reused_six_dimension_version_not_counted_as_new_orphan_on_subsequent_failure(self) -> None:
        """
        边界 3：既有六维版本复用后另一字段失败。

        断言契约：
        - 既有不可变版本复用时不属于本次新创建版本；
        - 另一字段发生失败后，coordinator.last_created_version_ids 为空，不将复用版本算作本次新孤儿；
        - 页面既有正文与用户输入保持原样不变；
        - 仓储既有文档完整保留，删除计数为 0。
        """
        # 1. 预先在仓储中持久化已有的 article 六维不可变版本
        art_payload, art_sha = self.codec.encode(
            payload("已持久化的历史不可变正文"), field=FieldStatePage._meta.get_field("article")
        )
        existing_vid, is_new = self.store.save_version_with_status(
            model_label=self.policy.model_label,
            page_id=101,
            field_name="article",
            codec=self.codec.codec_id,
            schema_version=self.codec.schema_version,
            payload_sha256=art_sha,
            payload=art_payload,
        )
        self.assertTrue(is_new)
        docs_snapshot = deepcopy(self.store.docs)

        # 2. 构建页面：article 内容与已有版本完全一致（触发六维复用），appendix 配置为写前失败
        page = self._create_page(
            article=payload("已持久化的历史不可变正文"),
            appendix=payload("次字段正文内容"),
        )
        self.store.pre_write_fail_fields.add("appendix")

        with self.assertRaises(ContentStoreError):
            self.coordinator.prepare_external_content_for_revision(page, policy=self.policy)

        # 3. 复用版本不计入本次新建孤儿账本
        self.assertEqual(self.coordinator.last_created_version_ids, [])

        # 4. 页面既有正文与用户输入保持不变
        self.assertEqual(page.article[0].value, "已持久化的历史不可变正文")
        self.assertEqual(page.appendix[0].value, "次字段正文内容")

        # 5. 既有文档未发生任何改变，物理删除为 0
        self.assertEqual(self.store.docs, docs_snapshot)
        self.assertEqual(self.store.delete_calls, 0)

    def test_retry_after_failure_reuses_immutable_version_without_duplicate_content(self) -> None:
        """
        边界 4：故障取消后重试可复用相同六维不可变版本，不增加重复正文。

        断言契约：
        - 首次尝试中第一字段成功持久化，第二字段失败；
        - 故障消除后重试相同内容，第一字段依据六维身份命中不可变去重，不新增重复文档；
        - 第二字段成功写入，最终构建完整闭环 Manifest；
        - coordinator.last_created_version_ids 在重试中仅包含本次实际新建的第二字段 ID；
        - 仓储层六维去重是内容寻址的幂等保障，不是 Web 层的 exactly-once。
        """
        page = self._create_page(
            article=payload("幂等重试首字段正文"),
            appendix=payload("幂等重试次字段正文"),
        )

        # 首次调用：第二字段临时故障
        self.store.pre_write_fail_fields.add("appendix")
        with self.assertRaises(ContentStoreError):
            self.coordinator.prepare_external_content_for_revision(page, policy=self.policy)

        # 此时仓储中仅包含第一字段创建的版本
        self.assertEqual(len(self.store.docs), 1)
        first_attempt_art_vid = self.coordinator.last_created_version_ids[0]
        self.assertEqual(self.store.docs[first_attempt_art_vid]["field_name"], "article")

        # 故障取消
        self.store.pre_write_fail_fields.clear()

        # 第二次调用：重试准备 Revision
        manifest = self.coordinator.prepare_external_content_for_revision(page, policy=self.policy)

        # 1. 验证成功返回完整 Manifest
        self.assertIsNotNone(manifest)
        self.assertEqual(manifest.fields["article"].version_id, first_attempt_art_vid)
        second_attempt_app_vid = manifest.fields["appendix"].version_id
        self.assertNotEqual(first_attempt_art_vid, second_attempt_app_vid)

        # 2. 仓储中文档总数正好为 2（article 与 appendix 各 1 份），首字段未生成重复版本
        self.assertEqual(len(self.store.docs), 2)
        article_docs = [doc for doc in self.store.docs.values() if doc["field_name"] == "article"]
        self.assertEqual(len(article_docs), 1, "存储层六维哈希去重必须保证不可变文档不重复生成")

        # 3. last_created_version_ids 在重试调用中只包含本次实际新建的 appendix 版本
        self.assertEqual(self.coordinator.last_created_version_ids, [second_attempt_app_vid])

        # 4. 物理删除为 0
        self.assertEqual(self.store.delete_calls, 0)

    def test_physical_delete_calls_strictly_zero_across_all_failure_injections(self) -> None:
        """
        边界 5：物理删除在所有失败注入场景中始终为 0。

        断言契约：
        - 替身 delete / delete_version / cleanup_unreferenced_version 设有守卫，任何调用直接报错；
        - 在字段失败、部分写入失败过程中，协调器绝不尝试物理删除或同步补偿；
        - 仓储现有文档集不减少。
        """
        # 预先放入基线文档
        _, _ = self.store.save_version_with_status(
            model_label=self.policy.model_label,
            page_id=999,
            field_name="article",
            codec=self.codec.codec_id,
            schema_version=self.codec.schema_version,
            payload_sha256="a" * 64,
            payload=[],
        )
        docs_before = set(self.store.docs.keys())

        # 触发任意失败（例如第二字段失败）
        page = self._create_page(
            article=payload("正常第一字段"),
            appendix=payload("即将失败的第二字段"),
        )
        self.store.pre_write_fail_fields.add("appendix")

        with self.assertRaises(ContentStoreError):
            self.coordinator.prepare_external_content_for_revision(page, policy=self.policy)

        # 核心断言：delete_calls 严格为 0
        self.assertEqual(self.store.delete_calls, 0)

        # 核心断言：预先存在的文档绝对保留
        self.assertTrue(docs_before.issubset(set(self.store.docs.keys())))

    def test_guarded_store_trap_intercepts_direct_delete_invocations(self) -> None:
        """
        验证仓储替身删除守卫有效性（非空断言支持）。

        断言契约：
        - 显式调用 delete / delete_version / cleanup_unreferenced_version 均被守卫拦截并抛出 AssertionError；
        - 调用计数准确累加，证明业务用例中 delete_calls == 0 具有真实拦截保证而非空断言。
        """
        initial_calls = self.store.delete_calls
        self.assertEqual(initial_calls, 0)

        with self.assertRaises(AssertionError) as ctx:
            self.store.delete("dummy-key")
        self.assertIn("COORDINATOR_MUST_NOT_DELETE_IMMUTABLE_VERSION: delete()", str(ctx.exception))

        with self.assertRaises(AssertionError) as ctx:
            self.store.delete_version("dummy-id")
        self.assertIn("COORDINATOR_MUST_NOT_DELETE_IMMUTABLE_VERSION: delete_version()", str(ctx.exception))

        with self.assertRaises(AssertionError) as ctx:
            self.store.cleanup_unreferenced_version("dummy-id")
        self.assertIn("COORDINATOR_MUST_NOT_DELETE_IMMUTABLE_VERSION: cleanup_unreferenced_version()", str(ctx.exception))

        self.assertEqual(self.store.delete_calls, 3)

    def test_coordinator_warning_log_contains_only_safe_metadata_without_payload_or_credentials(self) -> None:
        """
        边界 6：协调器告警日志仅包含安全的 page_id 与计数，不包含正文 Payload 或异常连接凭据。

        断言契约：
        - 合成数据包含敏感正文标记与敏感连接密码；
        - 第一字段确认写入，第二字段抛出携带连接凭据的异常；
        - 记录的 WARNING 日志中仅包含 safe_page_id 与 created_version_count；
        - 检查所有日志表面，严禁泄露正文与连接密码；
        - record.exc_info 必须为 None。
        """
        sensitive_payload_marker = "SYNTHETIC_TOP_SECRET_MARKER_987654321"
        sensitive_credential = "super_secret_mongo_password_xyz"
        sensitive_connection_url = f"mongodb://admin:{sensitive_credential}@cluster.internal:27017"

        page = self._create_page(
            article=payload(f"用户核心隐私正文片段_{sensitive_payload_marker}"),
            appendix=payload("次字段正文"),
        )

        def raise_store_error_with_credentials(**doc: Any) -> Tuple[str, bool]:
            if doc.get("field_name") == "appendix":
                raise ContentStoreError(f"Connection failed to storage endpoint: {sensitive_connection_url}")
            return GuardedMemoryVersionStore.save_version_with_status(self.store, **doc)

        with patch.object(self.store, "save_version_with_status", side_effect=raise_store_error_with_credentials):
            with self.assertLogs("external_content.coordinator", level="WARNING") as captured:
                with self.assertRaises(ContentStoreError):
                    self.coordinator.prepare_external_content_for_revision(page, policy=self.policy)

        # 1. 验证日志条数与级别
        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        self.assertEqual(record.levelno, logging.WARNING)

        # 2. 验证日志内容仅包含 safe_page_id 和计数
        msg = record.getMessage()
        self.assertIn("EXTERNAL_CONTENT_PREPARE_FAILED", msg)
        self.assertIn("page_id=101", msg)
        self.assertIn("created_version_count=1", msg)

        # 3. 严格核验所有日志表面均未泄露正文标记或敏感凭据
        log_surfaces = [
            str(record.msg),
            repr(record.args),
            record.getMessage(),
            record.exc_text or "",
            record.stack_info or "",
            "\n".join(captured.output),
        ]
        for surface in log_surfaces:
            self.assertNotIn(sensitive_payload_marker, surface, "协调器日志严禁泄漏正文内容")
            self.assertNotIn(sensitive_credential, surface, "协调器日志严禁泄漏连接密码凭据")
            self.assertNotIn(sensitive_connection_url, surface, "协调器日志严禁泄漏完整连接串")

        # 4. record.exc_info 必须为 None，防止异常对象或未脱敏 Traceback 隐蔽传递
        self.assertIsNone(record.exc_info, "协调器日志不得附带原始异常栈")
