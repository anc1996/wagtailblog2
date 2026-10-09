"""R3-D 发布/下架故障注入：原生 Action 与外置协议真实执行，持久化仅为 L0。"""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.contenttypes.models import ContentType
from django.db import DatabaseError, IntegrityError
from django.test import SimpleTestCase
from django.utils import timezone
from wagtail.actions.publish_page_revision import PublishPageRevisionAction
from wagtail.actions.unpublish import UnpublishAction
from wagtail.actions.unpublish_page import UnpublishPageAction
from wagtail.models import Page, Revision
from wagtail.models.pages import COMMENTS_RELATION_NAME
from wagtail.permissions import policy_registry

from external_content import mixins, publication
from external_content.codecs.streamfield import StreamFieldV1Codec
from external_content.coordinator import ExternalContentCoordinator
from external_content.exceptions import (
    ContentStoreError, ExternalContentOperationBlockedError,
    PublicationBodyUnavailableError, PublicationRevisionInvalidError,
)
from external_content.registry import ContentStoragePolicy, FieldStorageConfig
from external_content.tests.test_field_state import FieldStatePage, MemoryVersionStore, payload


NATIVE_PAGE_AFTER_UNPUBLISH = UnpublishPageAction._after_unpublish


class MemoryRevisionQuery:
    """仅替代原生排期的 SQL 批量更新；仍保留指定版本选择算法。"""

    def __init__(self, fixture, excluded=None):
        self.fixture = fixture
        self.excluded = excluded

    def exclude(self, id):
        return type(self)(self.fixture, id)

    def update(self, **values):
        for pk in self.fixture.sql["approved"]:
            if pk != self.excluded:
                self.fixture.sql["approved"][pk] = values["approved_go_live_at"]


class PublicationFailureTests(SimpleTestCase):
    """不假定 SQL 回滚会恢复 Python，也不以异常推断 COMMIT 是否成功。"""

    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.store = MemoryVersionStore()
        self.coordinator = ExternalContentCoordinator(self.store)
        self.policy = ContentStoragePolicy(
            model_label=FieldStatePage._meta.label_lower, schema_version=1,
            external_fields={
                name: FieldStorageConfig(name, "mongo", StreamFieldV1Codec())
                for name in ("article", "appendix")
            },
        )
        self.ct = ContentType(id=1, app_label="external_content", model="fieldstatepage")
        self.base_ct = ContentType(id=2, app_label="wagtailcore", model="page")
        self.events = []
        self.fail_at = None
        self.injected = IntegrityError("L0_PUBLICATION_INTERRUPTED")
        self.exit_outcome = None
        self.depth = 0
        self.callbacks = []
        self.wakes = []
        self.delete_calls = []
        for name in ("delete", "delete_version", "cleanup_unreferenced_version"):
            setattr(self.store, name, self.forbidden_delete)
        for module in ("mixins", "coordinator"):
            self.stack.enter_context(patch(
                f"external_content.{module}.get_storage_policy_for_model", return_value=self.policy,
            ))
        self.stack.enter_context(patch(
            "external_content.registry.get_storage_policy_for_model", return_value=self.policy,
        ))
        self.stack.enter_context(patch(
            "external_content.coordinator.get_policy_for_version", return_value=self.policy,
        ))
        self.stack.enter_context(patch(
            "external_content.coordinator.ContentVersionStore", return_value=self.store,
        ))
        self.stack.enter_context(patch.object(ContentType.objects, "get_for_id", return_value=self.ct))
        self.stack.enter_context(patch.object(self.ct, "model_class", return_value=FieldStatePage))
        self.stack.enter_context(patch.object(
            Revision.content_object, "get_content_type", return_value=self.ct,
        ))
        self.stack.enter_context(patch.object(Page, "get_base_content_type", return_value=self.base_ct))
        self.stack.enter_context(patch.object(Page, "get_parent", return_value=None))
        self.stack.enter_context(patch.object(Page, "set_url_path", return_value="/l0/"))
        self.stack.enter_context(patch.object(
            policy_registry, "get",
            return_value=SimpleNamespace(user_has_permission=lambda *args: True),
        ))
        self.revisions = {}
        self.manifests = {}
        # A 正式、B 最新草稿、C 历史，各有真实 codec/六维身份的不可变正文。
        for pk, title in ((11, "A"), (30, "B"), (20, "C")):
            page = self.make_page(title=title, article=payload(title + "\r\n"))
            manifest = self.coordinator.prepare_external_content_for_revision(page)
            self.manifests[pk] = manifest.to_dict()
            with mixins.preserve_threadlocal_context(
                mixins._controlled_serialization_context, "manifest", manifest,
            ):
                content = page.serializable_data()
            self.revisions[pk] = Revision(
                id=pk, content_type=self.ct, base_content_type=self.base_ct,
                object_id="101", content=content,
            )
        self.sql = {
            "page": {"live": True, "live_revision_id": 11, "latest_revision_id": 30,
                     "title": "A", "has_unpublished_changes": True, "expired": False,
                     "alias_of_id": None},
            "state": {
                "draft_revision_id": 30, "draft_manifest": self.manifests[30],
                "approved_revision_id": None, "approved_manifest": None,
                "published_revision_id": 11, "published_manifest": self.manifests[11],
                "published_at": None, "publication_generation": 4,
            },
            "approved": {11: None, 20: None, 30: None}, "outbox": [],
        }
        self.sql_before = deepcopy(self.sql)
        self.docs_before = deepcopy(self.store.docs)
        self.page = self.make_page(title="用户未提交标题", article=payload("用户未提交正文 B\r\n"))
        self.page.live_revision = self.revisions[11]
        self.caller = deepcopy(self.revisions[20])
        self.stack.enter_context(patch.object(
            publication.transaction, "atomic", side_effect=self.sql_atomic,
        ))
        self.stack.enter_context(patch.object(
            publication.transaction, "on_commit", side_effect=self.on_commit,
        ))
        self.stack.enter_context(patch.object(
            publication, "lock_alias_lifecycle_closure", side_effect=self.lock_closure,
        ))
        self.stack.enter_context(patch.object(
            mixins, "check_page_write_not_blocked",
        ))
        self.state_manager = manager = self.stack.enter_context(patch.object(
            publication.ExternalContentPublicationState, "objects",
        ))
        manager.filter.return_value.values.return_value.first.side_effect = (
            lambda: deepcopy(self.sql["state"])
        )
        manager.select_for_update.return_value.get.side_effect = lambda **kwargs: self.state_record()
        manager.select_for_update.return_value.get_or_create.side_effect = self.create_state
        self.stack.enter_context(patch.object(Page, "revisions", property(
            lambda page: MemoryRevisionQuery(self),
        )))
        self.stack.enter_context(patch.object(Page, "aliases", property(
            lambda page: SimpleNamespace(all=lambda: []),
        )))
        page_rows = self.stack.enter_context(patch.object(FieldStatePage.objects, "filter"))
        page_rows.return_value.values_list.return_value.first.side_effect = (
            lambda: self.sql["page"]["live"]
        )
        self.stack.enter_context(patch.object(
            Page, "save", lambda instance, *a, **kw: self.persist_page(instance, *a, **kw),
        ))
        self.stack.enter_context(patch.object(
            Revision, "save", lambda instance, *a, **kw: self.persist_revision(instance, *a, **kw),
        ))
        # modelcluster 还原仍执行真实字段解析，仅将 FK 存在性查询换成内存表。
        fk_tables = {}
        for field in FieldStatePage._meta.fields:
            relation = field.remote_field
            if relation and not relation.parent_link:
                table = fk_tables.setdefault(relation.model, {})
                for revision in self.revisions.values():
                    value = revision.content.get(field.name)
                    if value is not None:
                        table[value] = SimpleNamespace(pk=value)
        for model, table in fk_tables.items():
            def lookup(table=table, model=model, **kwargs):
                key, = kwargs.values()
                if key not in table:
                    raise model.DoesNotExist()
                return table[key]
            self.stack.enter_context(patch.object(model._default_manager, "get", side_effect=lookup))
        self.stack.enter_context(patch.object(Revision, "is_latest_revision", return_value=False))
        self.stack.enter_context(patch.object(Revision, "get_previous", side_effect=Revision.DoesNotExist))
        # 后置信号/Outbox 尾部留在事务中，L0 不连 ES、broker 或缓存。
        self.stack.enter_context(patch.object(
            PublishPageRevisionAction, "_after_publish", lambda action: self.signal("publish"),
        ))
        self.stack.enter_context(patch.object(
            UnpublishPageAction, "_after_unpublish", lambda action, page: self.signal("unpublish"),
        ))

    def make_page(self, **values):
        defaults = dict(
            id=101, page_ptr_id=101, content_type=self.ct, locale_id=1,
            title="A", draft_title="B", live=True, live_revision_id=11,
            latest_revision_id=30, has_unpublished_changes=True, article=[],
            appendix=payload("附录\n", "appendix-id"),
        )
        defaults.update(values)
        page = FieldStatePage(**defaults)
        page._state.adding = False
        page._cluster_related_objects = {COMMENTS_RELATION_NAME: []}
        page._test_content_store = self.store
        return page

    def forbidden_delete(self, *args, **kwargs):
        self.delete_calls.append((args, kwargs))
        raise AssertionError("L0_PHYSICAL_DELETE_FORBIDDEN")

    @contextmanager
    def sql_atomic(self):
        """内层为 savepoint；仅最外层模拟未知提交/提交后回调失败。"""
        before = deepcopy(self.sql)
        callbacks_before = len(self.callbacks)
        self.depth += 1
        try:
            yield
        except BaseException:
            self.sql.clear()
            self.sql.update(before)
            del self.callbacks[callbacks_before:]
            raise
        else:
            if self.depth == 1:
                if self.exit_outcome == "unknown_not_committed":
                    self.sql.clear()
                    self.sql.update(before)
                    self.callbacks.clear()
                if self.exit_outcome in ("unknown_committed", "unknown_not_committed"):
                    raise DatabaseError("L0_COMMIT_OUTCOME_UNKNOWN")
                callbacks, self.callbacks = self.callbacks, []
                for callback in callbacks:
                    callback()
        finally:
            self.depth -= 1

    def on_commit(self, callback, *args, **kwargs):
        self.callbacks.append(callback)

    def state_record(self, key="state"):
        record = SimpleNamespace(**deepcopy(self.sql[key]))

        def save(*, update_fields):
            self.events.append("state")
            for name in update_fields:
                if name != "updated_at":
                    self.sql[key][name] = deepcopy(getattr(record, name))
            if self.fail_at == "state":
                raise self.injected
        record.save = save
        return record

    def lock_closure(self, page_id):
        page = self.make_page(**self.sql["page"])
        locks = SimpleNamespace(page=page, state=self.state_record() if "state" in self.sql else None,
                                revisions=list(self.revisions.values()))
        return SimpleNamespace(pages={101: locks})

    def create_state(self, *, page_id, defaults):
        """缺行初始化真实改动持久字典，不用空 mock 隐藏额外 SQL 写。"""
        if "state" in self.sql:
            return self.state_record(), False
        self.events.append("state_create")
        self.sql["state"] = {
            "page_id": page_id, **defaults,
            "draft_revision_id": None, "draft_manifest": None,
            "approved_revision_id": None, "approved_manifest": None,
            "published_revision_id": None, "published_manifest": None,
            "published_at": None, "source_page_id": None, "source_revision_id": None,
        }
        return self.state_record(), True

    def persist_page(self, instance, *args, **kwargs):
        self.events.append("page")
        for name in self.sql["page"]:
            self.sql["page"][name] = getattr(instance, name)
        if self.fail_at == "page":
            raise self.injected

    def persist_revision(self, instance, *args, **kwargs):
        self.sql["approved"][instance.pk] = instance.approved_go_live_at
        if self.fail_at == "revision":
            raise self.injected

    def signal(self, operation):
        self.events.append("signal")
        self.sql["outbox"].append((operation, 101))
        publication.transaction.on_commit(lambda: self.wakes.append(operation))
        if self.fail_at == "signal":
            raise self.injected
        if self.exit_outcome == "postcommit":
            publication.transaction.on_commit(lambda: self.raise_injected())

    def raise_injected(self):
        raise self.injected

    def assert_immutable(self):
        self.assertEqual(self.store.docs, self.docs_before)
        self.assertEqual(self.delete_calls, [])
        self.assertEqual(self.depth, 0)

    def test_publish_success_selects_C_not_latest_B(self):
        self.page.publish(self.caller, log_action=False)
        self.assertEqual(self.sql["page"]["title"], "C")
        self.assertEqual(self.sql["page"]["live_revision_id"], 20)
        self.assertEqual(self.sql["state"]["published_manifest"], self.manifests[20])
        self.assertEqual(self.sql["state"]["draft_revision_id"], 30)
        self.assertEqual(self.wakes, ["publish"])
        self.assert_immutable()

    def test_publish_state_page_and_signal_failures_roll_back_all_SQL(self):
        for fail_at in ("state", "page", "signal"):
            with self.subTest(fail_at=fail_at):
                self.fail_at = fail_at
                with self.assertRaises(IntegrityError):
                    self.page.publish(self.caller, log_action=False)
                self.assertEqual(self.sql, self.sql_before)
                self.assertEqual(self.wakes, [])
                self.assertEqual(self.callbacks, [])
                self.assert_immutable()

    def test_revision_hash_and_timeout_fail_before_formal_or_native_write(self):
        for failure in ("hash", "timeout"):
            with self.subTest(failure=failure):
                if failure == "hash":
                    version = self.manifests[20]["fields"]["article"]["version_id"]
                    original = deepcopy(self.store.docs[version])
                    self.store.docs[version]["payload_sha256"] = "0" * 64
                    context = patch.object(
                        self.store, "get_version", wraps=self.store.get_version,
                    )
                else:
                    context = patch.object(
                        self.store, "get_version", side_effect=ContentStoreError("L0_TIMEOUT"),
                    )
                with context, self.assertRaises(PublicationBodyUnavailableError):
                    self.page.publish(self.caller, log_action=False)
                self.assertEqual(self.sql, self.sql_before)
                self.assertEqual(self.events, [])
                if failure == "hash":
                    self.store.docs[version] = original
        self.assert_immutable()

    def test_locked_future_schedule_never_uses_stale_caller_for_formal_pointer(self):
        future = timezone.now() + timedelta(days=1)
        self.revisions[20].approved_go_live_at = future
        self.page.publish(self.caller, log_action=False)
        self.assertEqual(self.sql["state"]["published_revision_id"], 11)
        self.assertEqual(self.sql["state"]["publication_generation"], 4)
        self.assertEqual(self.sql["page"]["title"], "A")
        self.assertEqual(self.page.live_revision_id, 11)
        self.assert_immutable()

    def test_stale_page_future_date_cannot_suppress_locked_immediate_publish(self):
        self.page.go_live_at = timezone.now() + timedelta(days=1)
        self.page.publish(self.caller, log_action=False)
        self.assertEqual(self.sql["state"]["published_revision_id"], 20)
        self.assertEqual(self.sql["page"]["live_revision_id"], 20)
        self.assertEqual(self.page.live_revision_id, 20)
        self.assert_immutable()

    def test_scheduled_execution_revalidates_locked_approval_and_rolls_back_draft_write(self):
        self.sql["state"]["draft_revision_id"] = 11
        self.sql["state"]["draft_manifest"] = self.manifests[11]
        self.sql_before = deepcopy(self.sql)
        self.caller.approved_go_live_at = timezone.now() - timedelta(seconds=1)
        self.revisions[20].approved_go_live_at = None
        with self.assertRaises(PublicationRevisionInvalidError):
            self.page.publish(self.caller, log_action="wagtail.publish.scheduled")
        self.assertEqual(self.sql, self.sql_before)
        self.assertEqual(self.wakes, [])
        self.assert_immutable()

    def test_future_publish_body_failure_restores_input_schedule(self):
        future = timezone.now() + timedelta(days=1)
        self.revisions[20].approved_go_live_at = future
        self.caller.approved_go_live_at = future
        self.fail_at = "revision"
        with self.assertRaises(IntegrityError):
            self.page.publish(self.caller, log_action=False)
        self.assertEqual(self.sql, self.sql_before)
        self.assertIsNone(self.page.go_live_at)
        self.assertEqual(self.page.live_revision_id, 11)
        self.assert_immutable()

    def test_direct_unpublish_action_body_failure_restores_input(self):
        self.fail_at = "signal"
        previous_revision = self.page.live_revision
        with self.assertRaises(IntegrityError):
            UnpublishPageAction(self.page, set_expired=True, log_action=False).execute()
        self.assertEqual(self.sql, self.sql_before)
        self.assertTrue(self.page.live)
        self.assertFalse(self.page.expired)
        self.assertIs(self.page.live_revision, previous_revision)
        self.assert_immutable()

    def test_unpublish_body_failure_restores_input_and_live_relation_cache(self):
        self.fail_at = "signal"
        previous_revision = self.page.live_revision
        before_states = self.page._external_content_field_states.copy()
        with self.assertRaises(IntegrityError):
            self.page.unpublish(log_action=False)
        self.assertEqual(self.sql, self.sql_before)
        self.assertTrue(self.page.live)
        self.assertEqual(self.page.live_revision_id, 11)
        self.assertIs(self.page.live_revision, previous_revision)
        self.assertFalse(self.page.expired)
        self.assertEqual(self.page.title, "用户未提交标题")
        self.assertEqual(self.page.article[0].value, "用户未提交正文 B\r\n")
        self.assertEqual(self.page._external_content_field_states, before_states)
        self.assertEqual(self.wakes, [])
        self.assert_immutable()

    def test_commit_false_action_does_not_write_formal_generation(self):
        UnpublishPageAction(self.page, commit=False, log_action=False).execute()
        self.assertEqual(self.sql["state"], self.sql_before["state"])
        self.assertEqual(self.sql["page"], self.sql_before["page"])
        self.assertNotIn("state", self.events)
        self.assert_immutable()

    def test_commit_false_mixin_never_restores_formal_by_SQL_rewrite(self):
        self.page.unpublish(commit=False, log_action=False)
        self.assertEqual(self.sql["state"], self.sql_before["state"])
        self.assertNotIn("state", self.events)
        self.assert_immutable()

    def test_missing_state_commit_false_action_and_mixin_do_not_create_row(self):
        """抓住非提交下架在原生 no-op 前仍持久创建 State 的缺口。"""
        for entry in ("action", "mixin"):
            with self.subTest(entry=entry):
                self.sql = deepcopy(self.sql_before)
                self.sql.pop("state")
                self.sql["page"].update(live=False, live_revision_id=None)
                before = deepcopy(self.sql)
                self.page = self.make_page(**self.sql["page"])
                self.events.clear()
                if entry == "action":
                    UnpublishPageAction(self.page, commit=False, log_action=False).execute()
                else:
                    self.page.unpublish(commit=False, log_action=False)
                self.assertEqual(self.sql, before)
                self.assertNotIn("state_create", self.events)
                self.assert_immutable()

    def test_missing_state_committed_action_and_mixin_still_initialize_once(self):
        for entry in ("action", "mixin"):
            with self.subTest(entry=entry):
                self.sql = deepcopy(self.sql_before)
                self.sql.pop("state")
                self.page = self.make_page(**self.sql["page"])
                self.events.clear()
                if entry == "action":
                    UnpublishPageAction(self.page, log_action=False).execute()
                else:
                    self.page.unpublish(log_action=False)
                self.assertFalse(self.sql["page"]["live"])
                self.assertIsNone(self.sql["state"]["published_manifest"])
                self.assertEqual(self.sql["state"]["publication_generation"], 0)
                self.assertEqual(self.events.count("state_create"), 1)
                self.assert_immutable()

    def test_unpublish_rejects_alias_relation_drift_including_none_before_native_save(self):
        """full save 不能把锁内 Alias 半转换，也不能借外层授权新增关系。"""
        for entry in ("action", "mixin"):
            for locked_source, caller_source in ((9, None), (None, 9), (9, 10)):
                with self.subTest(entry=entry, locked=locked_source, caller=caller_source):
                    self.sql = deepcopy(self.sql_before)
                    self.sql["page"]["alias_of_id"] = locked_source
                    if locked_source is not None:
                        self.sql["state"].update(
                            published_revision_id=None, source_page_id=locked_source,
                            source_revision_id=11,
                        )
                    before = deepcopy(self.sql)
                    self.page = self.make_page(**self.sql["page"])
                    self.page.alias_of_id = caller_source
                    self.events.clear()
                    self.wakes.clear()
                    with mixins.preserve_threadlocal_context(
                        mixins._controlled_alias_context, "in_alias", True,
                    ), mixins.preserve_threadlocal_context(
                        mixins._controlled_alias_context, "alias_update_source_page_id", caller_source,
                    ):
                        with self.assertRaises(ExternalContentOperationBlockedError):
                            if entry == "action":
                                UnpublishPageAction(self.page, log_action=False).execute()
                            else:
                                self.page.unpublish(log_action=False)
                        self.assertIs(mixins._controlled_alias_context.in_alias, True)
                        self.assertEqual(
                            mixins._controlled_alias_context.alias_update_source_page_id,
                            caller_source,
                        )
                    self.assertEqual(self.sql, before)
                    self.assertEqual(self.events, [])
                    self.assertEqual(self.wakes, [])
                    self.assert_immutable()

    def test_unpublish_scopes_alias_authorization_to_locked_relation_and_restores_outer(self):
        """即使锁内非 Alias，也要遮蔽外层授权；异常和成功均精确恢复。"""
        missing = object()
        outer_marker = object()
        for locked_source in (None, 9):
            for outer in (missing, None, outer_marker):
                for failure in (False, True):
                    with self.subTest(locked=locked_source, outer=outer, failure=failure):
                        self.sql = deepcopy(self.sql_before)
                        self.sql["page"]["alias_of_id"] = locked_source
                        if locked_source is not None:
                            self.sql["state"].update(
                                published_revision_id=None, source_page_id=locked_source,
                                source_revision_id=11,
                            )
                        before = deepcopy(self.sql)
                        self.page = self.make_page(**self.sql["page"])
                        observed = []

                        def signal(action, page):
                            observed.append((
                                getattr(mixins._controlled_alias_context, "in_alias", None),
                                getattr(mixins._controlled_alias_context,
                                        "alias_update_source_page_id", None),
                            ))
                            if failure:
                                raise self.injected

                        with ExitStack() as scope:
                            if outer is not missing:
                                for name in ("in_alias", "alias_update_source_page_id"):
                                    scope.enter_context(mixins.preserve_threadlocal_context(
                                        mixins._controlled_alias_context, name, outer,
                                    ))
                            scope.enter_context(patch.object(
                                UnpublishPageAction, "_after_unpublish", signal,
                            ))
                            if failure:
                                with self.assertRaises(IntegrityError):
                                    UnpublishPageAction(self.page, log_action=False).execute()
                                self.assertEqual(self.sql, before)
                            else:
                                UnpublishPageAction(self.page, log_action=False).execute()
                                self.assertFalse(self.sql["page"]["live"])
                                self.assertIsNone(self.sql["state"]["published_manifest"])
                            self.assertEqual(observed, [(locked_source is not None, locked_source)])
                            for name in ("in_alias", "alias_update_source_page_id"):
                                self.assertIs(
                                    getattr(mixins._controlled_alias_context, name, missing), outer,
                                )
                        self.assertEqual(self.sql["page"]["alias_of_id"], locked_source)
                        self.assert_immutable()

    def test_nested_committed_alias_does_not_inherit_parent_commit_false(self):
        """保留原生 Alias 的默认 commit=True，并精确恢复父级线程标记。"""
        self.assert_nested_committed_alias(parent_state_missing=False)

    def test_missing_state_parent_commit_false_still_commits_native_child_alias(self):
        self.assert_nested_committed_alias(parent_state_missing=True)

    def assert_nested_committed_alias(self, *, parent_state_missing):
        alias = self.make_page(id=202, page_ptr_id=202, alias_of_id=101)
        self.sql["alias_page"] = deepcopy(self.sql["page"])
        self.sql["alias_page"]["alias_of_id"] = 101
        self.sql["alias_state"] = deepcopy(self.sql["state"])
        self.sql["alias_state"].update(
            draft_revision_id=None, draft_manifest=None,
            published_revision_id=None, source_page_id=101, source_revision_id=11,
        )
        if parent_state_missing:
            self.sql.pop("state")

        def closure(page_id):
            if page_id == 101:
                return self.lock_closure(page_id)
            self.assertEqual(page_id, 202)
            locks = SimpleNamespace(
                page=alias, state=self.state_record("alias_state"), revisions=[],
            )
            return SimpleNamespace(pages={202: locks})

        def persist(instance, *args, **kwargs):
            if instance.pk == 101:
                return self.persist_page(instance, *args, **kwargs)
            self.events.append("alias_page")
            for name in self.sql["alias_page"]:
                self.sql["alias_page"][name] = getattr(instance, name)

        with patch.object(publication, "lock_alias_lifecycle_closure", side_effect=closure), \
             patch.object(Page, "save", persist), \
             patch.object(Page, "aliases", property(lambda page: SimpleNamespace(
                 all=lambda: [alias] if page.pk == 101 else [],
             ))), \
             patch.object(UnpublishPageAction, "_after_unpublish", NATIVE_PAGE_AFTER_UNPUBLISH), \
             patch("wagtail.actions.unpublish_page.page_unpublished.send"), \
             patch.object(UnpublishAction, "_after_unpublish",
                          lambda action, page: self.signal("unpublish")):
            try:
                self.page.unpublish(commit=False, log_action=False)
            except Exception as exc:
                self.fail(f"锁内 Alias 下架应允许原生元数据保存，实际拒绝：{type(exc).__name__}")
        self.assertEqual(self.sql["page"], self.sql_before["page"])
        if parent_state_missing:
            self.assertNotIn("state", self.sql)
            self.assertNotIn("state_create", self.events)
        else:
            self.assertEqual(self.sql["state"], self.sql_before["state"])
        self.assertFalse(self.sql["alias_page"]["live"])
        self.assertIsNone(self.sql["alias_state"]["published_revision_id"])
        self.assertIsNone(self.sql["alias_state"]["published_manifest"])
        self.assertEqual(self.sql["alias_state"]["source_page_id"], 101)
        self.assertEqual(self.sql["alias_state"]["source_revision_id"], 11)
        self.assertEqual(self.sql["alias_page"]["alias_of_id"], 101)
        self.assertEqual(self.sql["alias_state"]["publication_generation"], 5)
        self.assertFalse(hasattr(publication._controlled_save_context, "commit_false"))
        self.assert_immutable()

    def test_direct_unpublish_state_tail_failure_restores_input(self):
        self.fail_at = "state"
        previous = self.page.live_revision
        with self.assertRaises(IntegrityError):
            UnpublishPageAction(self.page, set_expired=True, log_action=False).execute()
        self.assertEqual(self.sql, self.sql_before)
        self.assertTrue(self.page.live)
        self.assertFalse(self.page.expired)
        self.assertIs(self.page.live_revision, previous)
        self.assertEqual(self.wakes, [])
        self.assert_immutable()

    def test_unpublish_unknown_commit_and_postcommit_failure_never_restore_old_memory(self):
        for outcome in ("unknown_committed", "unknown_not_committed", "postcommit"):
            with self.subTest(outcome=outcome):
                self.sql = deepcopy(self.sql_before)
                self.events.clear()
                self.callbacks.clear()
                self.wakes.clear()
                self.page = self.make_page()
                self.exit_outcome = outcome
                expected = IntegrityError if outcome == "postcommit" else DatabaseError
                with self.assertRaises(expected):
                    self.page.unpublish(log_action=False)
                committed = outcome != "unknown_not_committed"
                self.assertEqual(self.sql["page"]["live"], not committed)
                self.assertEqual(self.sql["state"]["published_revision_id"],
                                 None if committed else 11)
                self.assertFalse(self.page.live)
                self.assertIsNone(self.page.live_revision_id)
                self.assertEqual(self.events.count("page"), 1)
                self.assertEqual(self.wakes, ["unpublish"] if outcome == "postcommit" else [])
                self.assert_immutable()

    def test_unpublish_success_does_not_read_Mongo_and_is_idempotent(self):
        with patch.object(self.store, "get_version", side_effect=AssertionError("MONGO_FORBIDDEN")):
            self.page.unpublish(log_action=False)
            self.page.unpublish(log_action=False)
        self.assertFalse(self.sql["page"]["live"])
        self.assertIsNone(self.sql["state"]["published_revision_id"])
        self.assertEqual(self.sql["state"]["publication_generation"], 5)
        self.assert_immutable()

    def test_inner_publish_success_then_outer_failure_cancels_SQL_and_callback(self):
        with self.assertRaises(IntegrityError):
            with self.sql_atomic():
                self.page.publish(self.caller, log_action=False)
                self.assertEqual(self.sql["page"]["live_revision_id"], 20)
                raise self.injected
        self.assertEqual(self.sql, self.sql_before)
        self.assertEqual(self.callbacks, [])
        self.assertEqual(self.wakes, [])
        # 操作已经返回的 Python 实例须丢弃，不能假装外层回滚已恢复它。
        self.assertEqual(self.page.live_revision_id, 20)
        self.assert_immutable()

    def test_commit_unknown_both_outcomes_raise_without_replay_or_compensation(self):
        for outcome in ("unknown_committed", "unknown_not_committed"):
            with self.subTest(outcome=outcome):
                self.sql = deepcopy(self.sql_before)
                self.events.clear()
                self.callbacks.clear()
                self.page = self.make_page()
                self.exit_outcome = outcome
                with self.assertRaises(DatabaseError):
                    self.page.publish(self.caller, log_action=False)
                self.assertEqual(self.sql["page"]["live_revision_id"],
                                 20 if outcome == "unknown_committed" else 11)
                self.assertEqual(self.page.live_revision_id, 20)
                self.assertEqual(self.events.count("page"), 1)
                self.assertEqual(self.wakes, [])
                self.assert_immutable()

    def test_postcommit_callback_failure_preserves_committed_SQL(self):
        self.exit_outcome = "postcommit"
        with self.assertRaises(IntegrityError):
            self.page.publish(self.caller, log_action=False)
        self.assertEqual(self.sql["page"]["live_revision_id"], 20)
        self.assertEqual(self.sql["state"]["published_revision_id"], 20)
        self.assertEqual(self.sql["outbox"], [("publish", 101)])
        self.assertEqual(self.wakes, ["publish"])
        self.assertEqual(self.page.live_revision_id, 20)
        self.assert_immutable()

    def test_failure_restores_nested_controlled_context(self):
        self.fail_at = "signal"
        with mixins.preserve_threadlocal_context(
            publication._controlled_save_context, "in_controlled_save", "outer",
        ), mixins.preserve_threadlocal_context(
            publication._controlled_save_context, "commit_false", False,
        ):
            with self.assertRaises(IntegrityError):
                self.page.unpublish(log_action=False)
            self.assertEqual(publication._controlled_save_context.in_controlled_save, "outer")
            self.assertIs(publication._controlled_save_context.commit_false, False)
        self.assertFalse(hasattr(publication._controlled_save_context, "in_controlled_save"))
        self.assertFalse(hasattr(publication._controlled_save_context, "commit_false"))
        self.assert_immutable()
