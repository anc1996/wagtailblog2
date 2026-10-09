"""R3-D L0：真实 Revision 保存流程；SQL/Mongo 尾部替身不证明 InnoDB 回滚。"""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.contenttypes.models import ContentType
from django.db import DatabaseError, IntegrityError, transaction
from django.test import SimpleTestCase
from wagtail.models import Page, Revision
from wagtail.models.pages import COMMENTS_RELATION_NAME

from external_content import mixins
from external_content.codecs.streamfield import StreamFieldV1Codec
from external_content.coordinator import ExternalContentCoordinator
from external_content.exceptions import ContentStoreError
from external_content.forms import _wrap_external_content_create_post
from external_content.registry import ContentStoragePolicy, FieldStorageConfig
from external_content.tests.test_field_state import FieldStatePage, MemoryVersionStore, payload


NATIVE_DECORATED_SAVE_REVISION = Page.save_revision


class MemoryNestedConnection:
    """只模拟 Atomic 的内层保存点分支；无驱动、连接或真实事务能力。"""

    def __init__(self, error: DatabaseError, cleanup_fails: bool) -> None:
        self.in_atomic_block = True
        self.atomic_blocks = [object()]
        self.savepoint_ids = []
        self.needs_rollback = False
        self.closed_in_transaction = False
        self.commit_on_exit = True
        self.error = error
        self.cleanup_fails = cleanup_fails
        self.events = []
        self.release_count = 0

    def savepoint(self) -> str:
        self.events.append("savepoint")
        return "l0_nested_savepoint"

    def savepoint_commit(self, sid: str) -> None:
        self.events.append("release")
        self.release_count += 1
        if self.release_count == 1:
            raise self.error

    def savepoint_rollback(self, sid: str) -> None:
        self.events.append("rollback_to_savepoint")
        if self.cleanup_fails:
            raise DatabaseError("L0_SAVEPOINT_CLEANUP_FAILED")


class RevisionFailureTests(SimpleTestCase):
    """执行 Page.save_revision 原生函数体；其 SQL 装饰器及存储尾用离线替身。"""

    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.store = MemoryVersionStore()
        self.coordinator = ExternalContentCoordinator(self.store)
        codec = StreamFieldV1Codec()
        self.policy = ContentStoragePolicy(
            model_label=FieldStatePage._meta.label_lower, schema_version=1,
            external_fields={
                name: FieldStorageConfig(name, "mongo", codec)
                for name in ("article", "appendix")
            },
        )
        self.page = FieldStatePage(
            id=101, page_ptr_id=101, title="用户新标题", draft_title="已提交标题",
            content_type=ContentType(id=1, app_label="external_content", model="fieldstatepage"),
            locale_id=1, latest_revision_id=11, live_revision_id=11, live=True,
            has_unpublished_changes=False, article=payload("新正文 B\r\n"),
            appendix=payload("附录 B\n", "appendix-id"),
        )
        self.page._state.adding = False
        self.page._cluster_related_objects = {COMMENTS_RELATION_NAME: []}
        self.old_time = datetime(2026, 10, 8, tzinfo=timezone.utc)
        self.page.latest_revision_created_at = self.old_time
        self.old_revision = Revision(id=11, created_at=self.old_time)
        self.page.latest_revision = self.old_revision
        self.state = SimpleNamespace(
            draft_revision_id=11, draft_manifest={"previous": "draft"},
            published_revision_id=11, published_manifest={"previous": "live"},
        )
        self.sql = {
            "page_ids": [101], "parent_count": 1,
            "revision_ids": [11], "latest_revision_id": 11,
            "draft_revision_id": 11, "draft_manifest": {"previous": "draft"},
            "published_revision_id": 11, "published_manifest": {"previous": "live"},
        }
        self.sql_before = deepcopy(self.sql)
        self.events = []
        self.fail_at = None
        self.exit_outcome = None
        self.injected = RuntimeError("L0_SAVE_INTERRUPTED")
        self.delete_calls = []
        for method in ("delete", "delete_version", "cleanup_unreferenced_version"):
            setattr(self.store, method, self.forbidden_delete)
        for module in ("mixins", "coordinator"):
            self.stack.enter_context(patch(
                f"external_content.{module}.get_storage_policy_for_model",
                return_value=self.policy,
            ))
        self.stack.enter_context(patch(
            "external_content.coordinator.get_policy_for_version", return_value=self.policy,
        ))
        self.stack.enter_context(patch(
            "external_content.mixins.ExternalContentCoordinator", return_value=self.coordinator,
        ))
        self.stack.enter_context(patch(
            "external_content.mixins.check_page_write_not_blocked",
        ))
        self.stack.enter_context(patch(
            "external_content.mixins.lock_page_lifecycle_hierarchy",
        ))
        # 两个入口使用同一 django transaction 模块；替换一次即可覆盖创建桥和 Mixin。
        self.stack.enter_context(patch(
            "external_content.mixins.transaction.atomic", side_effect=self.sql_atomic,
        ))
        # 装饰器在依赖导入时已捕获真实 Atomic；只去掉该 SQL 装饰层，
        # 保留原生保存函数体，不能移除 L0 的 socket/ensure_connection 守卫。
        self.stack.enter_context(patch.object(Page, "save_revision", Page.save_revision.__wrapped__))
        # 原生 specific_class 通过 ContentType 的缓存管理器取模型身份，
        # 仅替代其 SQL 查询，保留真实 model_class/isinstance 判断。
        self.stack.enter_context(patch.object(
            ContentType.objects, "get_for_id", return_value=self.page.content_type,
        ))
        manager = self.stack.enter_context(patch(
            "external_content.mixins.ExternalContentPublicationState.objects",
        ))
        manager.select_for_update.return_value.get_or_create.return_value = (self.state, False)
        self.state.save = self.save_state
        self.stack.enter_context(patch.object(Page, "get_base_content_type", return_value=self.page.content_type))
        self.stack.enter_context(patch.object(
            Page, "serializable_data",
            lambda instance: {"article": [], "appendix": [], "native_intro": ""},
        ))
        self.stack.enter_context(patch(
            "wagtail.models.pages.Revision.objects.create", side_effect=self.create_revision,
        ))
        self.stack.enter_context(patch.object(
            Page, "save", lambda instance, *args, **kwargs: self.save_page(instance, *args, **kwargs),
        ))

    def forbidden_delete(self, *args, **kwargs) -> None:
        self.delete_calls.append((args, kwargs))
        raise AssertionError("L0_MONGO_DELETE_FORBIDDEN")

    @contextmanager
    def sql_atomic(self):
        """替身模拟体异常回滚；退出阶段分别模拟未知/提交后失败。"""
        before = deepcopy(self.sql)
        self.events.append("begin")
        try:
            yield
        except BaseException:
            self.sql.clear()
            self.sql.update(before)
            self.events.append("rollback")
            raise
        else:
            if self.exit_outcome == "unknown_not_committed":
                self.sql.clear()
                self.sql.update(before)
            self.events.append("exit")
            if self.exit_outcome:
                raise self.injected

    def create_revision(self, **values):
        self.events.append("revision")
        if self.fail_at == "revision":
            raise self.injected
        self.sql["revision_ids"].append(20)
        return Revision(
            id=20, created_at=datetime(2026, 10, 9, tzinfo=timezone.utc),
            content=values["content"],
        )

    def save_page(self, instance, *args, **kwargs) -> None:
        self.events.append("page")
        self.sql["latest_revision_id"] = instance.latest_revision_id
        if self.fail_at == "page":
            raise self.injected

    def save_state(self, **kwargs) -> None:
        self.events.append("state")
        self.sql["draft_revision_id"] = self.state.draft_revision_id
        self.sql["draft_manifest"] = deepcopy(self.state.draft_manifest)
        if self.fail_at == "state":
            raise self.injected

    def assert_input_preserved(self) -> None:
        self.assertEqual(self.page.article[0].value, "新正文 B\r\n")
        self.assertEqual(self.page.appendix[0].value, "附录 B\n")
        self.assertEqual(self.page.title, "用户新标题")
        self.assertEqual(self.delete_calls, [])

    def assert_failed_body_restored(self) -> None:
        self.assertEqual(self.sql, self.sql_before)
        self.assertEqual(self.events[-1], "rollback")
        self.assertEqual(self.page.latest_revision_id, 11)
        self.assertIs(self.page.latest_revision, self.old_revision)
        self.assertEqual(self.page.latest_revision_created_at, self.old_time)
        self.assertEqual(self.page.draft_title, "已提交标题")
        self.assertFalse(self.page.has_unpublished_changes)
        self.assert_input_preserved()

    def test_native_page_save_failure_restores_revision_metadata(self) -> None:
        self.fail_at = "page"
        with self.assertRaises(RuntimeError) as raised:
            self.page.save_revision(clean=False)
        self.assertIs(raised.exception, self.injected)
        self.assertEqual(self.events, ["begin", "revision", "page", "rollback"])
        self.assert_failed_body_restored()
        self.assertEqual(len(self.store.docs), 2)

    def test_draft_state_failure_restores_metadata_and_keeps_orphans(self) -> None:
        self.fail_at = "state"
        with self.assertRaises(RuntimeError):
            self.page.save_revision(clean=False)
        self.assertEqual(self.events, ["begin", "revision", "page", "state", "rollback"])
        self.assert_failed_body_restored()
        self.assertEqual(len(self.store.docs), 2)
        self.assertEqual(self.coordinator.last_created_version_ids, list(self.store.docs))

    def test_partial_field_binding_failure_restores_sources_and_hashes(self) -> None:
        previous = dict(self.page._external_content_field_states)
        original_article = self.page.article
        original_appendix = self.page.appendix
        self.page._external_content_loaded_hashes = {"preexisting": "marker"}
        bind = self.page._set_external_field_loaded

        def fail_second(name, *args, **kwargs):
            if name == "appendix":
                raise self.injected
            bind(name, *args, **kwargs)

        with patch.object(self.page, "_set_external_field_loaded", side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                self.page.save_revision(clean=False)
        self.assert_failed_body_restored()
        self.assertEqual(self.page._external_content_field_states, previous)
        self.assertEqual(self.page._external_content_loaded_hashes, {"preexisting": "marker"})
        self.assertIs(self.page.article, original_article)
        self.assertIs(self.page.appendix, original_appendix)

    def test_binding_failure_removes_new_hash_cache_and_new_relation_cache(self) -> None:
        self.page._state.fields_cache.pop("latest_revision")
        self.page.__dict__.pop("draft_title")
        with patch.object(self.page, "_set_external_field_loaded", side_effect=self.injected):
            with self.assertRaises(RuntimeError):
                self.page.save_revision(clean=False)
        self.assertNotIn("latest_revision", self.page._state.fields_cache)
        self.assertNotIn("draft_title", self.page.__dict__)
        self.assertNotIn("_external_content_loaded_hashes", self.page.__dict__)
        self.assertEqual(self.page.latest_revision_id, 11)
        self.assert_input_preserved()

    def test_revision_insert_failure_restores_nested_thread_context(self) -> None:
        self.fail_at = "revision"
        marker = object()
        with mixins.preserve_threadlocal_context(
            mixins._controlled_serialization_context, "manifest", marker,
        ), mixins.preserve_threadlocal_context(
            mixins._controlled_save_context, "in_controlled_save", "outer",
        ):
            with self.assertRaises(RuntimeError):
                self.page.save_revision(clean=False)
            self.assertIs(mixins._controlled_serialization_context.manifest, marker)
            self.assertEqual(mixins._controlled_save_context.in_controlled_save, "outer")
        self.assertFalse(hasattr(mixins._controlled_serialization_context, "manifest"))
        self.assertFalse(hasattr(mixins._controlled_save_context, "in_controlled_save"))
        self.assert_failed_body_restored()

    def test_success_binds_revision_and_preserves_formal_state(self) -> None:
        revision = self.page.save_revision(clean=False)
        self.assertEqual(revision.pk, 20)
        self.assertEqual(self.page.latest_revision_id, 20)
        self.assertEqual(self.sql["draft_revision_id"], 20)
        self.assertEqual(self.sql["published_manifest"], self.sql_before["published_manifest"])
        self.assertEqual(self.page.get_external_field_state("article").revision_id, 20)
        self.assertEqual(self.page.get_external_field_state("article").source, "REVISION")
        self.assert_input_preserved()

    def assert_exit_failure_not_restored(self, outcome: str) -> None:
        """每个测试独立初态；SQL 字典只是三种退出分类的替身，不证明 COMMIT。"""
        self.exit_outcome = outcome
        with self.assertRaises(RuntimeError) as raised:
            self.page.save_revision(clean=False)
        self.assertIs(raised.exception, self.injected)
        self.assertEqual(self.events, ["begin", "revision", "page", "state", "exit"])
        if outcome == "unknown_not_committed":
            self.assertEqual(self.sql, self.sql_before)
        else:
            self.assertEqual(self.sql["revision_ids"], [11, 20])
            self.assertEqual(self.sql["latest_revision_id"], 20)
            self.assertEqual(self.sql["draft_revision_id"], 20)
            self.assertEqual(set(self.sql["draft_manifest"]["fields"]), {"article", "appendix"})
            self.assertEqual(self.sql["published_revision_id"], 11)
            self.assertEqual(self.sql["published_manifest"], {"previous": "live"})
        # 相同异常兼容不同持久结果；调用者必须丢弃/核对实例，不猜测提交。
        self.assertEqual(self.page.latest_revision_id, 20)
        self.assertEqual(self.page.get_external_field_state("article").revision_id, 20)
        self.assert_input_preserved()

    def test_unknown_exit_with_uncommitted_double_does_not_restore_memory(self) -> None:
        self.assert_exit_failure_not_restored("unknown_not_committed")

    def test_unknown_exit_with_committed_double_does_not_restore_memory(self) -> None:
        self.assert_exit_failure_not_restored("unknown_committed")

    def test_post_commit_callback_failure_does_not_restore_memory(self) -> None:
        self.assert_exit_failure_not_restored("post_commit")

    def assert_native_nested_release_failure_restored(self, cleanup_fails: bool) -> None:
        """原生内层退出仍处于 Mixin 保存体内，不能误归类为最终 COMMIT 未知。"""
        original = DatabaseError("L0_SAVEPOINT_RELEASE_FAILED")
        connection = MemoryNestedConnection(original, cleanup_fails)
        # 恢复 Wagtail 捕获的真实 Atomic，仅替换连接获取；外层 SQL 仍用内存替身。
        # socket/ensure_connection 守卫不变，不证明 InnoDB 保存点回滚成功。
        with patch.object(
            Page, "save_revision", NATIVE_DECORATED_SAVE_REVISION,
        ), patch.object(transaction, "get_connection", return_value=connection):
            with self.assertRaises(DatabaseError) as raised:
                self.page.save_revision(clean=False)
        self.assertIs(raised.exception, original)
        self.assert_failed_body_restored()
        self.assertEqual(connection.needs_rollback, cleanup_fails)
        expected = ["savepoint", "release", "rollback_to_savepoint"]
        if not cleanup_fails:
            expected.append("release")
        self.assertEqual(connection.events, expected)
        self.assertEqual(self.events, ["begin", "revision", "page", "rollback"])
        self.assertEqual(len(self.store.docs), 2)
        self.assertFalse(hasattr(mixins._controlled_serialization_context, "manifest"))
        self.assertFalse(hasattr(mixins._controlled_save_context, "in_controlled_save"))

    def test_native_nested_release_failure_restores_metadata(self) -> None:
        self.assert_native_nested_release_failure_restored(False)

    def test_native_nested_release_and_cleanup_failure_restores_metadata(self) -> None:
        self.assert_native_nested_release_failure_restored(True)

    def test_keyboard_interrupt_restores_binding_and_propagates_same_exception(self) -> None:
        self.fail_at = "state"
        self.injected = KeyboardInterrupt()
        before_states = dict(self.page._external_content_field_states)
        with self.assertRaises(KeyboardInterrupt) as raised:
            self.page.save_revision(clean=False)
        self.assertIs(raised.exception, self.injected)
        self.assert_failed_body_restored()
        self.assertEqual(self.page._external_content_field_states, before_states)
        self.assertFalse(hasattr(mixins._controlled_serialization_context, "manifest"))
        self.assertFalse(hasattr(mixins._controlled_save_context, "in_controlled_save"))

    def test_unloaded_source_failure_restores_unpinned_origin_without_materializing(self) -> None:
        manifest = self.coordinator.prepare_external_content_for_revision(self.page)
        self.state.published_manifest = manifest.to_dict()
        with patch("external_content.mixins.ExternalContentPublicationState.objects") as manager:
            manager.filter.return_value.first.return_value = self.state
            manager.select_for_update.return_value.get_or_create.return_value = (self.state, False)
            with self.page._external_internal_assignment():
                self.page.article = []
                self.page.appendix = []
            fields = [field.attname for field in FieldStatePage._meta.concrete_fields]
            page = FieldStatePage.from_db(
                "default", fields, [self.page.__dict__.get(name) for name in fields],
            )
            page.content_type = self.page.content_type
            page.latest_revision = self.old_revision
            page._cluster_related_objects = {COMMENTS_RELATION_NAME: []}
            before_states = dict(page._external_content_field_states)
            before_values = {name: page.__dict__[name] for name in ("article", "appendix")}
            self.fail_at = "state"
            with self.assertRaises(RuntimeError):
                page.save_revision(clean=False)
        self.assertEqual(self.sql, self.sql_before)
        self.assertEqual(page.latest_revision_id, 11)
        self.assertIs(page.latest_revision, self.old_revision)
        self.assertEqual(page._external_content_field_states, before_states)
        for name in ("article", "appendix"):
            self.assertIsNone(page.get_external_field_state(name).manifest)
            self.assertEqual(page.get_external_field_state(name).status, "UNLOADED")
            self.assertIs(page.__dict__[name], before_values[name])
            self.assertEqual(list(page.__dict__[name].raw_data), [])
        self.assertEqual(len(self.store.docs), 2)
        self.assertEqual(self.coordinator.last_created_version_ids, [])
        self.assertEqual(self.delete_calls, [])

    def test_native_revision_insert_integrity_error_restores_baseline_and_keeps_orphans(self) -> None:
        self.fail_at = "revision"
        self.injected = IntegrityError("L0_REVISION_UNIQUE_FAILED")
        marker = object()
        with mixins.preserve_threadlocal_context(
            mixins._controlled_serialization_context, "manifest", marker,
        ), mixins.preserve_threadlocal_context(
            mixins._controlled_save_context, "in_controlled_save", "marker",
        ):
            with self.assertRaises(IntegrityError) as raised:
                self.page.save_revision(clean=False)
            self.assertIs(raised.exception, self.injected)
            self.assertIs(mixins._controlled_serialization_context.manifest, marker)
            self.assertEqual(mixins._controlled_save_context.in_controlled_save, "marker")
        self.assertEqual(self.events, ["begin", "revision", "rollback"])
        self.assert_failed_body_restored()
        self.assertEqual(len(self.store.docs), 2)
        self.assertEqual(self.delete_calls, [])
        self.assertFalse(hasattr(mixins._controlled_serialization_context, "manifest"))
        self.assertFalse(hasattr(mixins._controlled_save_context, "in_controlled_save"))

    def test_native_page_save_integrity_error_restores_baseline_and_keeps_orphans(self) -> None:
        self.fail_at = "page"
        self.injected = IntegrityError("L0_PAGE_INTEGRITY_FAILED")
        marker = object()
        with mixins.preserve_threadlocal_context(
            mixins._controlled_serialization_context, "manifest", marker,
        ), mixins.preserve_threadlocal_context(
            mixins._controlled_save_context, "in_controlled_save", "marker",
        ):
            with self.assertRaises(IntegrityError) as raised:
                self.page.save_revision(clean=False)
            self.assertIs(raised.exception, self.injected)
            self.assertIs(mixins._controlled_serialization_context.manifest, marker)
            self.assertEqual(mixins._controlled_save_context.in_controlled_save, "marker")
        self.assertEqual(self.events, ["begin", "revision", "page", "rollback"])
        self.assert_failed_body_restored()
        self.assertEqual(len(self.store.docs), 2)
        self.assertEqual(self.delete_calls, [])
        self.assertFalse(hasattr(mixins._controlled_serialization_context, "manifest"))
        self.assertFalse(hasattr(mixins._controlled_save_context, "in_controlled_save"))

    def test_native_state_save_integrity_error_restores_baseline_and_keeps_orphans(self) -> None:
        self.fail_at = "state"
        self.injected = IntegrityError("L0_STATE_INTEGRITY_FAILED")
        marker = object()
        with mixins.preserve_threadlocal_context(
            mixins._controlled_serialization_context, "manifest", marker,
        ), mixins.preserve_threadlocal_context(
            mixins._controlled_save_context, "in_controlled_save", "marker",
        ):
            with self.assertRaises(IntegrityError) as raised:
                self.page.save_revision(clean=False)
            self.assertIs(raised.exception, self.injected)
            self.assertIs(mixins._controlled_serialization_context.manifest, marker)
            self.assertEqual(mixins._controlled_save_context.in_controlled_save, "marker")
        self.assertEqual(self.events, ["begin", "revision", "page", "state", "rollback"])
        self.assert_failed_body_restored()
        self.assertEqual(len(self.store.docs), 2)
        self.assertEqual(self.delete_calls, [])
        self.assertFalse(hasattr(mixins._controlled_serialization_context, "manifest"))
        self.assertFalse(hasattr(mixins._controlled_save_context, "in_controlled_save"))

    def test_mongo_prepare_timeout_after_second_field_aborts_before_sql(self) -> None:
        real_save = self.store.save_version_with_status
        timeout_error = ContentStoreError("L0_MONGO_WRITE_RESULT_UNKNOWN")
        cause = TimeoutError("L0_MONGO_ACK_TIMEOUT")
        attempts = []
        before_states = dict(self.page._external_content_field_states)
        before_values = {name: self.page.__dict__[name] for name in ("article", "appendix")}

        def save_and_timeout(**doc):
            attempts.append(doc["field_name"])
            version_id, is_new = real_save(**doc)
            if doc["field_name"] == "appendix":
                # 存储已经写入，但调用者未取得确认；不能猜测 ID 或自动重试。
                raise timeout_error from cause
            return version_id, is_new

        marker = object()
        with mixins.preserve_threadlocal_context(
            mixins._controlled_serialization_context, "manifest", marker,
        ), mixins.preserve_threadlocal_context(
            mixins._controlled_save_context, "in_controlled_save", "marker",
        ):
            with patch.object(self.store, "save_version_with_status", side_effect=save_and_timeout):
                with self.assertRaises(ContentStoreError) as raised:
                    self.page.save_revision(clean=False)
                self.assertIs(raised.exception, timeout_error)
                self.assertIs(raised.exception.__cause__, cause)
            self.assertIs(mixins._controlled_serialization_context.manifest, marker)
            self.assertEqual(mixins._controlled_save_context.in_controlled_save, "marker")
        self.assertNotIn("revision", self.events)
        self.assertNotIn("page", self.events)
        self.assertNotIn("state", self.events)
        self.assertEqual(self.events, ["begin", "rollback"])
        self.assert_failed_body_restored()
        self.assertEqual(attempts, ["article", "appendix"])
        self.assertEqual(self.page._external_content_field_states, before_states)
        self.assertNotIn("_external_content_loaded_hashes", self.page.__dict__)
        for name in before_values:
            self.assertIs(self.page.__dict__[name], before_values[name])
        self.assertEqual(len(self.store.docs), 2)
        self.assertIn("memory-1", self.store.docs)
        self.assertIn("memory-2", self.store.docs)
        self.assertEqual(self.coordinator.last_created_version_ids, ["memory-1"])
        self.assertEqual(self.delete_calls, [])
        self.assert_input_preserved()
        self.assertFalse(hasattr(mixins._controlled_serialization_context, "manifest"))
        self.assertFalse(hasattr(mixins._controlled_save_context, "in_controlled_save"))

    def test_create_view_failure_rolls_back_outer_identity_and_retains_mongo_for_target_page(self) -> None:
        new_page = FieldStatePage(
            id=None, page_ptr_id=None, title="新建页面", draft_title="新建页面",
            content_type=self.page.content_type, locale_id=1, live=False,
            has_unpublished_changes=True, article=payload("新正文 C\r\n"),
            appendix=payload("新附录 C\n", "appendix-c"),
        )
        new_page._state.adding = False
        new_page._cluster_related_objects = {COMMENTS_RELATION_NAME: []}
        before_states = dict(new_page._external_content_field_states)
        before_values = {name: new_page.__dict__[name] for name in ("article", "appendix")}

        test_case = self

        class FakeCreateView:
            def __init__(self, page):
                self.page = page

            def post(self, *args, **kwargs):
                test_case.sql["page_ids"].append(102)
                test_case.sql["parent_count"] += 1
                self.page.id = 102
                self.page.page_ptr_id = 102
                return self.page.save_revision(clean=False)

        _wrap_external_content_create_post(FakeCreateView)
        view = FakeCreateView(new_page)

        self.fail_at = "state"
        self.injected = DatabaseError("L0_CREATE_POST_STATE_SAVE_FAILED")

        with self.assertRaises(DatabaseError) as raised:
            view.post()
        self.assertIs(raised.exception, self.injected)

        self.assertEqual(self.sql["page_ids"], [101])
        self.assertEqual(self.sql["parent_count"], 1)
        self.assertEqual(self.sql["revision_ids"], [11])
        self.assertEqual(self.sql["latest_revision_id"], 11)
        self.assertEqual(self.sql["draft_revision_id"], 11)
        self.assertEqual(self.sql, self.sql_before)

        self.assertEqual(
            self.events,
            ["begin", "begin", "revision", "page", "state", "rollback", "rollback"],
        )

        self.assertEqual(len(self.store.docs), 2)
        for doc in self.store.docs.values():
            self.assertEqual(doc["page_id"], 102)
        self.assertEqual(self.delete_calls, [])
        self.assertEqual(new_page.latest_revision_id, None)
        self.assertEqual(new_page.latest_revision_created_at, None)
        self.assertNotIn("latest_revision", new_page._state.fields_cache)
        self.assertNotIn("_external_content_loaded_hashes", new_page.__dict__)
        self.assertEqual(new_page._external_content_field_states, before_states)
        for name in before_values:
            self.assertIs(new_page.__dict__[name], before_values[name])
        self.assertEqual(new_page.article[0].value, "新正文 C\r\n")
        self.assertEqual(new_page.appendix[0].value, "新附录 C\n")
        self.assertFalse(new_page.live)
        self.assertIsNone(new_page.live_revision_id)
        self.assertEqual(new_page.draft_title, "新建页面")
        self.assertTrue(new_page.has_unpublished_changes)
        self.assertFalse(hasattr(mixins._controlled_serialization_context, "manifest"))
        self.assertFalse(hasattr(mixins._controlled_save_context, "in_controlled_save"))
        # SQL 身份已撤回；Python PK 不由事务自动恢复，失败实例必须丢弃，不能重用。
        self.assertEqual(new_page.pk, 102)
