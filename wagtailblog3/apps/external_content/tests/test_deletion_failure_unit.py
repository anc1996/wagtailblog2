"""RB-15 L0：执行真实删除包装、Collector、接收器及意图服务；不证明真实提交。"""

from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import timedelta
import logging
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, PropertyMock, patch

from django.db.models.deletion import Collector
from django.test import RequestFactory, override_settings
from django.utils import timezone
from treebeard.mp_tree import MP_NodeQuerySet
from wagtail.actions.delete_page import DeletePageAction
from wagtail.admin.views.pages.bulk_actions.delete import DeleteBulkAction
from wagtail.admin.views.pages.delete import DeleteView
from wagtail.models import Page, Revision

from external_content import deletion, locking, signals
from external_content.exceptions import ExternalContentError, ExternalContentOperationBlockedError
from external_content.models import DeletionIntentStatus, ExternalContentDeletionIntent
from guides.models import GuidePage


class InjectedFailure(RuntimeError):
    """只表示合成故障阶段，不包含连接信息或用户数据。"""


def manifest(page_id: int, version: str) -> dict:
    """独立手工夹具；不能用被测收集器构造期望值。"""
    return {
        "protocol_version": 1, "content_storage_schema": 1,
        "model_label": "guides.guidepage", "page_id": page_id,
        "fields": {"body": {
            "codec": "streamfield", "schema_version": 1,
            "version_id": version, "payload_sha256": "a" * 64,
        }},
    }


class IntentQuery:
    """替换 ORM 求值；每次读返回新实例，不模拟删除协议。"""

    def __init__(self, fixture: "DeletionFixture", filters: dict | None = None):
        self.fixture = fixture
        self.filters = filters or {}

    def filter(self, **kwargs: object) -> "IntentQuery":
        return IntentQuery(self.fixture, {**self.filters, **kwargs})

    def first(self) -> ExternalContentDeletionIntent | None:
        return next(iter(self), None)

    def __iter__(self):
        for row in self.fixture.sql["intents"].values():
            if all(row[key] == value for key, value in self.filters.items()):
                yield self.fixture.load_intent(row)

    def create(self, **kwargs: object) -> ExternalContentDeletionIntent:
        self.fixture.hit("intent-create", kwargs["page_id"])
        intent = ExternalContentDeletionIntent(**kwargs)
        self.fixture.sql["intents"][intent.pk] = self.fixture.intent_row(intent)
        self.fixture.loaded.append(intent)
        return intent


class DeletionFixture(TestCase):
    """事务替身只快照 SQL 字典；不会自动恢复 Python 对象，避免镜像通过。"""

    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.events: list = []
        self.failure: tuple[str, int | None] | None = None
        self.exit_failure = False
        self.depth = 0
        self.loaded: list[ExternalContentDeletionIntent] = []
        self.now = timezone.now()
        self.pages = {
            pk: GuidePage(id=pk, page_ptr_id=pk, title=f"L0-{pk}", live=True)
            for pk in (1, 2, 3)
        }
        self.batches = {pk: [page] for pk, page in self.pages.items()}
        self.sql = {
            "pages": {pk: {"live": True} for pk in self.pages},
            "revisions": {pk: [pk * 10] for pk in self.pages},
            "tree": 3, "logs": [], "tombstones": [], "intents": {},
        }
        self.states = {
            pk: SimpleNamespace(
                published_manifest=manifest(pk, f"published-{pk}"),
                approved_manifest=manifest(pk, f"approved-{pk}"),
                draft_manifest=manifest(pk, f"draft-{pk}"),
            ) for pk in self.pages
        }
        self.revisions = {
            pk: [SimpleNamespace(pk=pk * 10, content={
                "_external_content_manifest": manifest(pk, f"history-{pk}")
            })] for pk in self.pages
        }
        # 同一 Django transaction 模块用于原生 Action/View/Collector，保持外围跨度。
        self.stack.enter_context(patch.object(deletion.transaction, "atomic", self.atomic))
        self.stack.enter_context(patch.object(deletion.router, "db_for_write", return_value="default"))
        self.stack.enter_context(patch.object(locking, "lock_page_lifecycle_hierarchy", self.locks))
        self.stack.enter_context(patch.object(
            ExternalContentDeletionIntent, "objects", IntentQuery(self)
        ))
        fixture = self

        def save(intent, update_fields=None, **kwargs):
            fixture.hit("intent-update" if intent.status == DeletionIntentStatus.PENDING
                        else "intent-tombstone", intent.page_id)
            fixture.sql["intents"][intent.pk] = fixture.intent_row(intent)

        def refresh(intent, **kwargs):
            row = fixture.sql["intents"][intent.pk]
            for key, value in row.items():
                setattr(intent, key, deepcopy(value))

        self.stack.enter_context(patch.object(ExternalContentDeletionIntent, "save", save))
        self.stack.enter_context(patch.object(ExternalContentDeletionIntent, "refresh_from_db", refresh))
        aliases = SimpleNamespace(values_list=lambda *a, **k: [])
        self.stack.enter_context(patch.object(Page, "aliases", PropertyMock(return_value=aliases)))
        self.stack.enter_context(patch.object(deletion.timezone, "now", return_value=self.now))
        # 禁止正文仓储构造及旧博客物理删除旁路。零调用断言是保护边界，不是业务替身。
        self.store = self.stack.enter_context(patch.object(
            deletion, "ContentVersionStore", side_effect=AssertionError("Mongo must stay offline")
        ))
        self.mongo_guards = []
        for target in (
            "external_content.store.ContentVersionStore.delete_version",
            "external_content.store.ContentVersionStore.cleanup_unreferenced_version",
            "wagtailblog3.mongo.MongoManager.delete_blog_content",
            "wagtailblog3.mongo.MongoManager.delete_single_revision",
            "wagtailblog3.mongo.MongoManager.delete_page_revisions",
        ):
            guard = self.stack.enter_context(patch(target, side_effect=AssertionError(target)))
            self.mongo_guards.append(guard)
        # 原生 Action 的树/日志 IO 下沉为替身；删除协议仍由真实 wrapper/Collector/receiver 实行。
        self.stack.enter_context(patch.object(DeletePageAction, "_delete_page", self.native_action))
        self.stack.enter_context(patch.object(MP_NodeQuerySet, "delete", self.native_tree_delete))
        deletion.wrap_native_page_deletion()
        self.stack.enter_context(patch("django.db.models.deletion.sql.DeleteQuery", self.delete_query))
        self.stack.enter_context(patch(
            "django.db.models.deletion.signals.pre_delete.send", self.pre_send
        ))
        self.stack.enter_context(patch(
            "django.db.models.deletion.signals.post_delete.send", self.post_send
        ))
        self.previous_context = vars(deletion._deletion_context).copy()
        vars(deletion._deletion_context).clear()
        self.addCleanup(self.restore_context)

    def restore_context(self) -> None:
        vars(deletion._deletion_context).clear()
        vars(deletion._deletion_context).update(self.previous_context)

    def tearDown(self) -> None:
        self.assertEqual(self.depth, 0)
        self.assertFalse(hasattr(deletion._deletion_context, "deleting_page_ids"))
        self.store.assert_not_called()
        for guard in self.mongo_guards:
            guard.assert_not_called()

    def hit(self, stage: str, page_id: int | None = None) -> None:
        self.events.append((stage, page_id, self.depth))
        if self.failure == (stage, page_id):
            raise InjectedFailure(stage)

    @contextmanager
    def atomic(self, *args, **kwargs):
        before = deepcopy(self.sql)
        self.depth += 1
        self.events.append(("atomic-enter", kwargs, self.depth))
        try:
            yield
            if self.exit_failure and self.depth == 1:
                raise InjectedFailure("atomic-exit")
        except BaseException:
            self.sql = before
            self.events.append(("atomic-rollback", None, self.depth))
            raise
        finally:
            self.depth -= 1

    @staticmethod
    def intent_row(intent: ExternalContentDeletionIntent) -> dict:
        return deepcopy({
            name: getattr(intent, name) for name in (
                "id", "page_id", "model_label", "status", "manifest", "safe_gc_after",
                "actor_id", "request_id", "collected_versions", "failed_versions", "retry_count",
            )
        })

    def load_intent(self, row: dict) -> ExternalContentDeletionIntent:
        intent = ExternalContentDeletionIntent(**deepcopy(row))
        self.loaded.append(intent)
        return intent

    def locks(self, page_id: int, **kwargs) -> SimpleNamespace:
        self.hit("locks", page_id)
        return SimpleNamespace(
            page=SimpleNamespace(content_type=SimpleNamespace(model_class=lambda: GuidePage)),
            state=self.states[page_id], revisions=self.revisions[page_id],
            intents=list(IntentQuery(self).filter(page_id=page_id)),
        )

    def pre_send(self, sender, instance, **kwargs) -> None:
        self.hit("pre", instance.pk)
        signals.handle_external_content_pre_delete(sender, instance, **kwargs)
        # 墓碑生产者 IO 注入在真实 pre 接收器之后，不能自行编排意图或授权。
        self.hit("search-tombstone", instance.pk)
        self.sql["tombstones"].append(instance.pk)

    def post_send(self, sender, instance, **kwargs) -> None:
        self.hit("post", instance.pk)
        signals.handle_external_content_post_delete(sender, instance, **kwargs)

    def delete_query(self, model) -> SimpleNamespace:
        def delete_batch(pks, using):
            for pk in pks:
                self.hit("sql-delete", pk)
                self.sql["pages"].pop(pk)
                self.sql["revisions"].pop(pk)
            return len(pks)
        return SimpleNamespace(delete_batch=delete_batch)

    def native_tree_delete(self, queryset, *args, **kwargs):
        self.sql["tree"] -= len(queryset.pages)
        collector = Collector(using=queryset.db)
        collector.data = {GuidePage: list(queryset.pages)}
        collector.can_fast_delete = lambda *a, **k: False
        result = collector.delete()  # 真实 Django Collector 与已安装隔离包装
        self.hit("nodes-signal")
        return result

    def native_action(self, page, *args, **kwargs):
        page_id = page.pk
        self.hit("action-log", page_id)
        self.sql["logs"].append(page_id)
        queryset = SimpleNamespace(model=GuidePage, db="default", pages=self.batches[page_id])
        return MP_NodeQuerySet.delete(queryset, *args, **kwargs)

    def raw_delete(self, page_id: int = 1):
        return DeletePageAction(self.pages[page_id]).execute()

    def seed_pending(self, page_id: int = 1) -> ExternalContentDeletionIntent:
        return deletion.record_deletion_intent(self.pages[page_id], actor_id="L0-before")

    def assert_rollback(self, operation, stage: str, page_id: int | None = None) -> None:
        before = deepcopy(self.sql)
        self.failure = (stage, page_id)
        with self.assertRaisesRegex(InjectedFailure, stage):
            operation()
        self.assertEqual(self.sql, before)


class TestDeletionFailures(DeletionFixture):
    def test_single_page_intent_failure_rolls_back_log_and_tree(self) -> None:
        self.assert_rollback(self.raw_delete, "intent-create", 1)

    def test_single_page_sql_failure_rolls_back_new_intent(self) -> None:
        self.assert_rollback(self.raw_delete, "sql-delete", 1)

    def test_unpublished_draft_page_uses_the_same_failure_boundary(self) -> None:
        self.pages[1].live = False
        self.states[1].published_manifest = None
        self.states[1].approved_manifest = None
        self.assert_rollback(self.raw_delete, "sql-delete", 1)
        self.assertIn(("pre", 1, 3), self.events)

    def test_single_page_required_post_tombstone_error_is_not_swallowed(self) -> None:
        self.assert_rollback(self.raw_delete, "intent-tombstone", 1)

    def test_required_search_tombstone_error_prevents_sql_delete(self) -> None:
        self.assert_rollback(self.raw_delete, "search-tombstone", 1)
        self.assertFalse(any(event[0] == "sql-delete" for event in self.events))

    def test_tree_signal_after_collector_pk_clear_rolls_back_whole_action(self) -> None:
        self.assert_rollback(self.raw_delete, "nodes-signal")
        # L0 故意保留原生已清 PK 的 Python 对象；必须丢弃重读，不声称 SQL 回滚还原它。
        self.assertIsNone(self.pages[1].pk)

    def test_parent_child_failure_does_not_commit_the_first_child(self) -> None:
        self.batches[1] = [self.pages[1], self.pages[2]]
        self.assert_rollback(self.raw_delete, "intent-tombstone", 1)
        self.assertIn(("post", 2, 3), self.events)

    def test_queryset_bulk_failure_rolls_back_tree_and_both_intents(self) -> None:
        queryset = SimpleNamespace(model=GuidePage, db="default", pages=list(self.pages.values())[:2])
        self.assert_rollback(lambda: MP_NodeQuerySet.delete(queryset), "sql-delete", 1)
        self.assertTrue(any(event[:2] == ("pre", 2) for event in self.events))

    def test_pending_manifest_and_grace_period_survive_failed_deletion(self) -> None:
        pending = self.seed_pending()
        self.sql["intents"][pending.pk]["safe_gc_after"] = self.now + timedelta(days=30)
        self.assert_rollback(self.raw_delete, "intent-tombstone", 1)
        row = self.sql["intents"][pending.pk]
        self.assertEqual(row["status"], DeletionIntentStatus.PENDING)
        self.assertEqual(row["safe_gc_after"], self.now + timedelta(days=30))
        self.assertEqual(row["actor_id"], "L0-before")

    def test_execute_tombstone_failure_restores_its_locked_intent_memory(self) -> None:
        pending = self.seed_pending()
        before_loaded = len(self.loaded)
        self.assert_rollback(
            lambda: deletion.execute_safe_page_deletion(self.pages[1], pending),
            "intent-tombstone", 1,
        )
        self.assertEqual(self.loaded[before_loaded].status, DeletionIntentStatus.PENDING)

    def test_execute_failure_after_pk_clear_restores_only_target_identity(self) -> None:
        pending = self.seed_pending()
        self.assert_rollback(
            lambda: deletion.execute_safe_page_deletion(self.pages[1], pending),
            "intent-tombstone", 1,
        )
        self.assertEqual(self.pages[1].pk, 1)
        self.assertFalse(hasattr(self.pages[1], "_controlled_safe_deletion_in_progress"))

    def test_execute_atomic_exit_failure_does_not_leave_mysql_deleted_memory(self) -> None:
        pending = self.seed_pending()
        first = len(self.loaded)
        before = deepcopy(self.sql)
        self.exit_failure = True
        with self.assertRaisesRegex(InjectedFailure, "atomic-exit"):
            deletion.execute_safe_page_deletion(self.pages[1], pending)
        self.assertEqual(self.sql, before)
        self.assertEqual(self.loaded[first].status, DeletionIntentStatus.PENDING)
        self.assertEqual(self.pages[1].pk, 1)

    def test_execute_can_retry_after_a_known_rollback(self) -> None:
        pending = self.seed_pending()
        self.assert_rollback(
            lambda: deletion.execute_safe_page_deletion(self.pages[1], pending),
            "intent-tombstone", 1,
        )
        # 先断言可重试身份，避免夹具以 None 查表将业务缺陷变成未处理错误。
        self.assertEqual(self.pages[1].pk, 1)
        self.failure = None
        result = deletion.execute_safe_page_deletion(self.pages[1], pending)
        self.assertEqual(result.status, DeletionIntentStatus.MYSQL_DELETED)
        self.assertNotIn(1, self.sql["pages"])

    def test_pending_record_save_failure_restores_manifest_and_grace_memory(self) -> None:
        pending = self.seed_pending()
        self.states[1].draft_manifest = manifest(1, "new-draft")
        first = len(self.loaded)
        self.assert_rollback(lambda: deletion.record_deletion_intent(
            self.pages[1], actor_id="L0-new", safe_period_seconds=20 * 86400
        ), "intent-update", 1)
        loaded = self.loaded[first]
        self.assertEqual(loaded.manifest, pending.manifest)
        self.assertEqual(loaded.safe_gc_after, pending.safe_gc_after)
        self.assertEqual(loaded.actor_id, "L0-before")

    def test_pending_record_atomic_exit_failure_restores_frozen_memory(self) -> None:
        pending = self.seed_pending()
        self.states[1].draft_manifest = manifest(1, "new-draft")
        first = len(self.loaded)
        before = deepcopy(self.sql)
        self.exit_failure = True
        with self.assertRaisesRegex(InjectedFailure, "atomic-exit"):
            deletion.record_deletion_intent(self.pages[1], actor_id="L0-new")
        self.assertEqual(self.sql, before)
        self.assertEqual(self.loaded[first].manifest, pending.manifest)
        self.assertEqual(self.loaded[first].actor_id, "L0-before")

    def test_receiver_missing_scope_must_not_commit_a_pending_intent(self) -> None:
        before = deepcopy(self.sql)
        with self.assertRaises(ExternalContentOperationBlockedError):
            signals.handle_external_content_pre_delete(GuidePage, self.pages[1])
        self.assertEqual(self.sql, before)
        self.assertFalse(any(event[0] == "locks" for event in self.events))

    def test_all_four_version_sources_are_recorded_without_mongo(self) -> None:
        intent = self.seed_pending()
        self.assertEqual(intent.manifest["body_version_ids"], [
            "approved-1", "draft-1", "history-1", "published-1",
        ])
        self.assertEqual(intent.manifest["revision_ids"], [10])
        self.assertEqual(intent.safe_gc_after, self.now + timedelta(days=7))
        self.assertEqual(len(intent.manifest["versions"]), 4)
        for version in intent.manifest["versions"]:
            self.assertEqual(version["expected_identity"], {
                "model_label": "guides.guidepage", "page_id": 1, "field_name": "body",
                "codec": "streamfield", "schema_version": 1, "payload_sha256": "a" * 64,
            })

    def test_malformed_state_manifest_fails_closed_before_intent_write(self) -> None:
        for malformed in ({}, [], {"fields": {"body": {"version_id": "broken"}}}):
            with self.subTest(manifest=malformed):
                self.states[1].draft_manifest = malformed
                before = deepcopy(self.sql)
                with self.assertRaises(ExternalContentError):
                    self.seed_pending()
                self.assertEqual(self.sql, before)

    def test_malformed_history_manifest_fails_closed_before_intent_write(self) -> None:
        self.revisions[1][0].content["_external_content_manifest"] = {
            "fields": {"body": {"version_id": "broken"}}
        }
        before = deepcopy(self.sql)
        with self.assertRaises(ExternalContentError):
            self.seed_pending()
        self.assertEqual(self.sql, before)

    def test_explicit_null_history_manifest_is_not_a_legacy_absence(self) -> None:
        self.revisions[1][0].content["_external_content_manifest"] = None
        before = deepcopy(self.sql)
        with self.assertRaises(ExternalContentError):
            self.seed_pending()
        self.assertEqual(self.sql, before)

    def test_conflicting_identity_for_same_version_fails_closed(self) -> None:
        self.states[1].draft_manifest = manifest(1, "published-1")
        self.states[1].draft_manifest["fields"]["body"]["payload_sha256"] = "b" * 64
        before = deepcopy(self.sql)
        with self.assertRaises(ExternalContentError):
            self.seed_pending()
        self.assertEqual(self.sql, before)

    def test_absent_legacy_revision_manifest_is_not_invented(self) -> None:
        self.revisions[1][0].content = {"title": "legacy"}
        intent = self.seed_pending()
        self.assertEqual(intent.manifest["revision_ids"], [10])
        self.assertNotIn("history-1", intent.manifest["body_version_ids"])

    def test_shared_immutable_manifest_identity_is_not_rebound_to_alias(self) -> None:
        self.pages[1].alias_of_id = 2
        self.states[1].published_manifest = manifest(2, "shared-source")
        intent = self.seed_pending()
        pointer = next(row for row in intent.manifest["versions"]
                       if row["version_id"] == "shared-source")
        self.assertEqual(pointer["expected_identity"]["page_id"], 2)

    def test_safety_period_cannot_be_shortened(self) -> None:
        with self.assertRaises(ValueError):
            deletion.record_deletion_intent(self.pages[1], safe_period_seconds=1)
        self.assertEqual(self.sql["intents"], {})
        pending = self.seed_pending()
        self.sql["intents"][pending.pk]["safe_gc_after"] = self.now + timedelta(days=30)
        again = self.seed_pending()
        self.assertEqual(again.safe_gc_after, self.now + timedelta(days=30))

    def test_gc_refuses_before_clock_orm_or_mongo(self) -> None:
        with patch.object(deletion.timezone, "now", side_effect=AssertionError("clock")), \
                patch.object(ExternalContentDeletionIntent, "objects") as orm:
            with self.assertRaises(ExternalContentOperationBlockedError):
                deletion.perform_garbage_collection(batch_size=999999)
            orm.assert_not_called()
            self.assertEqual(orm.mock_calls, [])

    def test_revision_only_collector_does_not_authorize_page_deletion(self) -> None:
        revision = Revision(id=100, content={})
        collector = Collector(using="default")
        collector.data = {Revision: [revision]}
        collector.can_fast_delete = lambda *a, **k: False
        with patch("django.db.models.deletion.sql.DeleteQuery") as query:
            query.return_value.delete_batch.return_value = 1
            collector.delete()
        self.assertIsNone(revision.pk)
        self.assertEqual(self.sql["intents"], {})

    def test_bulk_admin_second_page_failure_rolls_back_first_page(self) -> None:
        view = DeleteBulkAction.__new__(DeleteBulkAction)
        view.request = SimpleNamespace(user=None)
        view.next_url = "/L0/"
        for page in list(self.pages.values())[:2]:
            page.get_descendant_count = lambda: 0
        view.get_actionable_objects = lambda: (list(self.pages.values())[:2], {})
        view.annotate_items = lambda rows: rows
        view.prepare_action = lambda *a: None
        view.get_execution_context = lambda: {"user": None}
        view.get_success_message = lambda *a: None
        with patch.object(view, "_BulkAction__run_before_hooks", return_value=None), \
                patch.object(view, "_BulkAction__run_after_hooks", return_value=None):
            self.assert_rollback(lambda: view.form_valid(None), "intent-create", 2)
        self.assertNotIn(1, [event[1] for event in self.events if event[0] == "locks"
                            and event[2] == 0])
        self.assertIsNone(self.pages[1].pk)

    @override_settings(WAGTAIL_I18N_ENABLED=True)
    def test_translation_admin_second_page_failure_rolls_back_source(self) -> None:
        import wagtail.admin.views.pages.delete as native_view

        source_parent = SimpleNamespace(id=50)
        translation_parent = SimpleNamespace(id=60)
        source_parent.get_translations = lambda: [translation_parent]
        self.pages[1].get_parent = lambda: source_parent
        self.pages[2].get_parent = lambda: translation_parent
        self.pages[1].permissions_for_user = lambda user: SimpleNamespace(can_delete=lambda: True)
        self.pages[2].permissions_for_user = self.pages[1].permissions_for_user
        request = RequestFactory().post("/L0/")
        request.user = SimpleNamespace()
        view = DeleteView()
        view.setup(request, page_id=1)

        def hooks(name):
            if name == "construct_translated_pages_to_cascade_actions":
                return [lambda *a: {"L0": [self.pages[2]]}]
            return []

        # dispatch 装饰器捕获的是已安装 Atomic 类，替换其底层 IO 而非跳过装饰器。
        fixture = self

        def enter(atomic):
            atomic._l0_context = fixture.atomic()
            return atomic._l0_context.__enter__()

        def exit(atomic, *exc):
            return atomic._l0_context.__exit__(*exc)

        with patch.object(native_view, "get_object_or_404", return_value=self.pages[1]), \
                patch.object(GuidePage, "specific", PropertyMock(
                    side_effect=lambda: self.pages[1])), \
                patch.object(native_view.hooks, "get_hooks", side_effect=hooks), \
                patch.object(native_view.ReferenceIndex, "get_references_to") as references, \
                patch.object(deletion.transaction.Atomic, "__enter__", enter), \
                patch.object(deletion.transaction.Atomic, "__exit__", exit), \
                patch.object(native_view, "type_to_delete_confirmation", return_value=True):
            references.return_value.group_by_source_object.return_value.is_protected = False
            self.assert_rollback(
                lambda: view.dispatch(request, page_id=1), "intent-create", 2
            )
        self.assertTrue(any(event[:2] == ("sql-delete", 1) for event in self.events))


class TestDeletionContext(TestCase):
    def test_scope_restores_absence_after_success_and_failure(self) -> None:
        with patch.object(deletion, "_deletion_context", SimpleNamespace()):
            with deletion.safe_deletion_scope({1}):
                self.assertEqual(deletion._deletion_context.deleting_page_ids, {1})
            self.assertFalse(hasattr(deletion._deletion_context, "deleting_page_ids"))
            with self.assertRaises(InjectedFailure):
                with deletion.safe_deletion_scope({2}):
                    raise InjectedFailure()
            self.assertFalse(hasattr(deletion._deletion_context, "deleting_page_ids"))

    def test_nested_scope_restores_exact_outer_set_without_extra_authorization(self) -> None:
        outer = {9}
        with patch.object(deletion, "_deletion_context", SimpleNamespace(deleting_page_ids=outer)):
            with deletion.safe_deletion_scope({1}):
                middle = deletion._deletion_context.deleting_page_ids
                with self.assertRaises(InjectedFailure):
                    with deletion.safe_deletion_scope({2}):
                        self.assertEqual(deletion._deletion_context.deleting_page_ids, {9, 1, 2})
                        raise InjectedFailure()
                self.assertIs(deletion._deletion_context.deleting_page_ids, middle)
                self.assertEqual(middle, {9, 1})
            self.assertIs(deletion._deletion_context.deleting_page_ids, outer)
            self.assertEqual(outer, {9})


class TestDeletionRepair(DeletionFixture):
    """捕获真实日志记录；敏感哨兵只作合成故障输入，不输出到业务日志。"""

    def setUp(self) -> None:
        super().setUp()
        self.records: list[logging.LogRecord] = []
        records = self.records

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                if record.levelno >= logging.WARNING:
                    records.append(record)

        handler = Capture()
        for log in (deletion.logger, signals.logger):
            log.addHandler(handler)
            self.addCleanup(log.removeHandler, handler)

    def assert_stages(self, *required: str) -> None:
        allowed = {
            "execute", "record-intent", "native-action", "tree-delete", "collector",
            "pre-delete", "post-delete", "intent-status-save",
            "transaction-enter", "transaction-exit",
        }
        self.assertTrue(self.records, "失败必须有安全阶段诊断")
        actual = set()
        for record in self.records:
            self.assertEqual(record.msg, "external_content_deletion_failed stage=%s")
            self.assertEqual(len(record.args), 1)
            self.assertIn(record.args[0], allowed)
            self.assertIsNone(record.exc_info)
            self.assertIsNone(record.stack_info)
            self.assertNotIn("SENSITIVE", record.getMessage())
            actual.add(record.args[0])
        self.assertTrue(set(required) <= actual, (required, actual))

    def test_execute_sql_failure_records_safe_observable_boundaries(self) -> None:
        pending = self.seed_pending()
        self.assert_rollback(
            lambda: deletion.execute_safe_page_deletion(self.pages[1], pending),
            "sql-delete", 1,
        )
        self.assertEqual(self.pages[1].pk, 1)
        self.assert_stages("execute", "collector", "tree-delete", "native-action")
        self.assertNotIn("transaction-exit", {r.args[0] for r in self.records})

    def test_direct_pre_missing_scope_records_without_registration(self) -> None:
        before = deepcopy(self.sql)
        with self.assertRaises(ExternalContentOperationBlockedError):
            signals.handle_external_content_pre_delete(GuidePage, self.pages[1])
        self.assertEqual(self.sql, before)
        self.assert_stages("pre-delete")

    def test_pre_registration_failure_records_stage_and_restores_scope(self) -> None:
        pending = self.seed_pending()
        self.assert_rollback(self.raw_delete, "intent-update", 1)
        self.assertEqual(self.sql["intents"][pending.pk]["status"], DeletionIntentStatus.PENDING)
        self.assert_stages("pre-delete", "record-intent")

    def test_native_action_before_tree_failure_records_action_boundary(self) -> None:
        self.assert_rollback(self.raw_delete, "action-log", 1)
        self.assert_stages("native-action")

    def test_direct_tree_tail_failure_records_tree_boundary(self) -> None:
        queryset = SimpleNamespace(model=GuidePage, db="default", pages=[self.pages[1]])
        self.assert_rollback(lambda: MP_NodeQuerySet.delete(queryset), "nodes-signal")
        self.assertIsNone(self.pages[1].pk)  # 非执行器目标对象仍须丢弃重读。
        self.assert_stages("tree-delete")

    def test_direct_collector_sql_failure_records_collector_boundary(self) -> None:
        collector = Collector(using="default")
        collector.data = {GuidePage: [self.pages[1]]}
        collector.can_fast_delete = lambda *a, **k: False
        self.assert_rollback(collector.delete, "sql-delete", 1)
        self.assert_stages("collector")

    def test_executor_status_save_failure_records_specific_stage(self) -> None:
        pending = self.seed_pending()
        first = len(self.loaded)
        self.assert_rollback(
            lambda: deletion.execute_safe_page_deletion(self.pages[1], pending),
            "intent-tombstone", 1,
        )
        self.assertEqual(self.loaded[first].status, DeletionIntentStatus.PENDING)
        self.assertEqual(self.pages[1].pk, 1)
        self.assert_stages("intent-status-save", "execute")

    def test_native_post_status_failure_has_no_exception_payload(self) -> None:
        pending = self.seed_pending()
        before = deepcopy(self.sql)
        original = InjectedFailure("SENSITIVE-manifest-body-actor-request")
        with patch.object(ExternalContentDeletionIntent, "save", side_effect=original):
            with self.assertRaises(InjectedFailure) as caught:
                signals.handle_external_content_post_delete(GuidePage, self.pages[1])
        self.assertIs(caught.exception, original)
        self.assertEqual(self.sql, before)
        self.assertEqual(pending.status, DeletionIntentStatus.PENDING)
        self.assert_stages("post-delete", "intent-status-save")

    def test_atomic_exit_is_neutral_for_execute_record_action_and_tree(self) -> None:
        for entry in ("execute", "record", "action", "tree"):
            with self.subTest(entry=entry):
                self.records.clear()
                before = deepcopy(self.sql)
                self.exit_failure = True
                operation = {
                    "execute": lambda: deletion.execute_safe_page_deletion(self.pages[1]),
                    "record": lambda: deletion.record_deletion_intent(self.pages[1]),
                    "action": self.raw_delete,
                    "tree": lambda: MP_NodeQuerySet.delete(SimpleNamespace(
                        model=GuidePage, db="default", pages=[self.pages[1]]
                    )),
                }[entry]
                with self.assertRaises(InjectedFailure):
                    operation()
                self.assertEqual(self.sql, before)
                self.exit_failure = False
                self.pages[1].pk = 1  # 原生入口失败后夹具显式重建身份，不冒称自动恢复。
                self.assert_stages("transaction-exit")

    def test_logger_failure_never_replaces_original_business_exception(self) -> None:
        original = InjectedFailure("SENSITIVE-primary")
        before = deepcopy(self.sql)
        with patch.object(locking, "lock_page_lifecycle_hierarchy", side_effect=original), \
                patch.object(deletion.logger, "error", side_effect=RuntimeError("logger-down")), \
                patch.object(signals.logger, "error", side_effect=RuntimeError("logger-down")):
            with self.assertRaises(InjectedFailure) as caught:
                with deletion.safe_deletion_scope(set()):
                    signals.handle_external_content_pre_delete(GuidePage, self.pages[1])
        self.assertIs(caught.exception, original)
        self.assertEqual(self.sql, before)

    def test_post_logger_failure_preserves_original_exception_identity(self) -> None:
        self.seed_pending()
        before = deepcopy(self.sql)
        original = InjectedFailure("SENSITIVE-primary")
        with patch.object(ExternalContentDeletionIntent, "save", side_effect=original), \
                patch.object(signals.logger, "error", side_effect=RuntimeError("logger-down")):
            with self.assertRaises(RuntimeError) as caught:
                signals.handle_external_content_post_delete(GuidePage, self.pages[1])
        self.assertIs(caught.exception, original)
        self.assertEqual(self.sql, before)

    def test_optional_existing_intent_restores_actual_internal_object_before_registration(self) -> None:
        pending = self.seed_pending()
        self.sql["intents"][pending.pk]["safe_gc_after"] = self.now + timedelta(days=30)
        before = deepcopy(self.sql)
        self.states[1].draft_manifest = manifest(1, "repair-new-draft")
        held = []
        original = deletion.record_deletion_intent
        original_save = ExternalContentDeletionIntent.save
        frozen_updated_at = self.now - timedelta(days=1)

        def locks_with_timestamp(*args, **kwargs):
            result = self.locks(*args, **kwargs)
            for locked in result.intents:
                locked.updated_at = frozen_updated_at
            return result

        def save_with_timestamp(instance, *args, **kwargs):
            instance.updated_at = self.now
            return original_save(instance, *args, **kwargs)

        def observe(*args, **kwargs):
            result = original(*args, **kwargs)
            held.append(result)
            return result

        self.failure = ("intent-tombstone", 1)
        with patch.object(deletion, "record_deletion_intent", observe), \
                patch.object(locking, "lock_page_lifecycle_hierarchy", locks_with_timestamp), \
                patch.object(ExternalContentDeletionIntent, "save", save_with_timestamp):
            with self.assertRaises(InjectedFailure):
                deletion.execute_safe_page_deletion(
                    self.pages[1], actor_id="SENSITIVE-new-actor", request_id="SENSITIVE-request"
                )
        self.assertEqual(self.sql, before)
        self.assertEqual(self.pages[1].pk, 1)
        self.assertEqual(len(held), 2)  # 执行器嵌套登记与真实 pre 接收器各返回 fresh 对象。
        for field in ("status", "manifest", "safe_gc_after", "actor_id", "request_id"):
            self.assertEqual(getattr(held[0], field), before["intents"][pending.pk][field])
        self.assertEqual(held[0].updated_at, frozen_updated_at)
        self.assert_stages("execute", "intent-status-save")
