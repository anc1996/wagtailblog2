"""R3-D Copy/Alias L0：真实控制流，SQL 快照替身不证明 InnoDB/treebeard。"""
from contextlib import ContextDecorator, ExitStack
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import UUID

from django.contrib.contenttypes.models import ContentType
from django.db import DatabaseError, transaction
from django.db.models import Q
from django.db.models.signals import post_save, pre_save
from django.db.transaction import Atomic
from django.test import SimpleTestCase
from wagtail.actions.convert_alias import ConvertAliasPageAction
from wagtail.actions.copy_page import CopyPageAction
from wagtail.actions.copy_for_translation import CopyPageForTranslationAction
from wagtail.actions.create_alias import CreatePageAliasAction
from wagtail.models import Locale, Page, Revision
from wagtail.models.pages import COMMENTS_RELATION_NAME

from external_content import copying, locking, mixins
from external_content.codecs.streamfield import StreamFieldV1Codec
from external_content.coordinator import ExternalContentCoordinator
from external_content.exceptions import ContentStoreError
from external_content.models import (
    ExternalContentDeletionIntent, ExternalContentPublicationState,
)
from external_content.registry import ContentStoragePolicy, FieldStorageConfig
from external_content.tests.test_field_state import FieldStatePage, MemoryVersionStore, payload


class SnapshotAtomic(ContextDecorator):
    """每个保存点真实快照撤回持久字典；Python 实例与 Mongo 不属于 SQL 快照。"""

    def __init__(self, owner: "CopyFailureTests") -> None:
        self.owner = owner

    def _recreate_cm(self) -> "SnapshotAtomic":
        return type(self)(self.owner)

    def __enter__(self) -> "SnapshotAtomic":
        owner = self.owner
        self.before = deepcopy(owner.sql)
        self.callbacks: list[Any] = []
        owner.transactions.append(self)
        owner.events.append(("begin", len(owner.transactions)))
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        owner = self.owner
        assert owner.transactions.pop() is self
        if exc_type is not None:
            owner.sql.clear()
            owner.sql.update(self.before)
            owner.events.append(("rollback", len(owner.transactions) + 1))
            return False
        if owner.transactions:
            owner.transactions[-1].callbacks.extend(self.callbacks)
            owner.events.append(("release", len(owner.transactions) + 1))
            return False
        outcome = owner.exit_outcome
        owner.events.append(("commit", outcome))
        if outcome == "unknown_not_committed":
            owner.sql.clear()
            owner.sql.update(self.before)
        if outcome in {"unknown_committed", "unknown_not_committed"}:
            # COMMIT 响应未知时既不能凭异常断言持久状态，也不能自动重放。
            raise owner.injected
        for callback in self.callbacks:
            callback()
        return False


class Rows:
    """只替代 ORM 查询尾部；每次重读从独立持久字典重建实例。"""

    def __init__(self, owner: "CopyFailureTests", layer: str,
                 predicates: tuple[Any, ...] = (), locked: bool = False,
                 ordering: tuple[str, ...] = ()) -> None:
        self.owner, self.layer = owner, layer
        self.predicates, self.locked, self.ordering = predicates, locked, ordering

    def select_for_update(self, **kwargs: Any) -> "Rows":
        return Rows(self.owner, self.layer, self.predicates, True, self.ordering)

    def filter(self, *args: Any, **kwargs: Any) -> "Rows":
        return Rows(self.owner, self.layer, self.predicates + args + (Q(**kwargs),),
                    self.locked, self.ordering)

    def exclude(self, **kwargs: Any) -> "Rows":
        return self.filter(~Q(**kwargs))

    def order_by(self, *keys: str) -> "Rows":
        return Rows(self.owner, self.layer, self.predicates, self.locked, keys)

    def _matches(self, row: Any, predicate: Any) -> bool:
        if isinstance(predicate, Q):
            results = [self._matches(row, child) for child in predicate.children]
            result = all(results) if predicate.connector == Q.AND else any(results)
            return not result if predicate.negated else result
        name, expected = predicate
        bits = name.split("__")
        name, operation = bits[0], bits[1] if len(bits) > 1 else "eq"
        field = row._meta.get_field(name) if name != "pk" else row._meta.pk
        actual = getattr(row, field.attname)
        expected = getattr(expected, "pk", expected)
        if operation == "in":
            return actual in expected
        if operation == "startswith":
            return actual.startswith(expected)
        if operation == "isnull":
            return (actual is None) == expected
        return actual == expected

    def __iter__(self):
        rows = [self.owner.read(self.layer, pk) for pk in self.owner.sql[self.layer]]
        rows = [row for row in rows if all(self._matches(row, q) for q in self.predicates)]
        for key in reversed(self.ordering):
            rows.sort(key=lambda row: getattr(row, key.lstrip("-")), reverse=key.startswith("-"))
        if self.locked:
            self.owner.events.extend((self.layer, row.pk) for row in rows)
        return iter(rows)

    def __len__(self) -> int:
        return len(list(iter(self)))

    def get(self, **kwargs: Any) -> Any:
        rows = list(self.filter(**kwargs))
        model = self.owner.models[self.layer]
        if not rows:
            raise model.DoesNotExist
        if len(rows) > 1:
            raise model.MultipleObjectsReturned
        return rows[0]

    def first(self) -> Any:
        return next(iter(self), None)

    def exists(self) -> bool:
        return self.first() is not None

    def values_list(self, *names: str, flat: bool = False) -> list[Any]:
        values = [tuple(getattr(row, name) for name in names) for row in self]
        return [item[0] for item in values] if flat else values

    def specific(self) -> "Rows":
        return self

    def iterator(self):
        return iter(self)

    def for_instance(self, instance: Any) -> "Rows":
        return self.filter(object_id=str(instance.pk), base_content_type_id=1)

    def create(self, **values: Any) -> Any:
        if self.layer == "revision" and ("object" in values or "content_object" in values):
            instance = values.pop("content_object") if "content_object" in values else values.pop("object")
            values.update(object_id=str(instance.pk), content_type_id=7, base_content_type_id=1)
        row = self.owner.models[self.layer](**values)
        self.owner.persist(self.layer, row)
        return row

    def get_or_create(self, defaults: dict[str, Any] | None = None, **kwargs: Any):
        try:
            return self.get(**kwargs), False
        except self.owner.models[self.layer].DoesNotExist:
            return self.create(**dict(defaults or {}, **kwargs)), True


class CopyFailureTests(SimpleTestCase):
    """移除整操作围栏、按位置映射历史或粗糙恢复 frame 都必须被检测。"""

    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.sql: dict[str, dict[int, dict[str, Any]]] = {
            name: {} for name in ("page", "state", "revision", "intent")
        }
        self.models = dict(page=FieldStatePage, state=ExternalContentPublicationState,
                           revision=Revision, intent=ExternalContentDeletionIntent)
        self.events: list[tuple[str, Any]] = []
        self.transactions: list[SnapshotAtomic] = []
        self.atomic_instances: dict[int, list[SnapshotAtomic]] = {}
        self.exit_outcome = None
        self.fail_at: tuple[str, int] | None = None
        self.counts: dict[str, int] = {}
        self.injected = DatabaseError("L0_COPY_INJECTED_FAILURE")
        self.instances: list[Any] = []
        self.delete_calls: list[Any] = []
        self.store = MemoryVersionStore()
        for name in ("delete", "delete_version", "cleanup_unreferenced_version"):
            setattr(self.store, name, self.forbidden_delete)
        self.policy = ContentStoragePolicy(
            model_label=FieldStatePage._meta.label_lower, schema_version=1,
            external_fields={name: FieldStorageConfig(name, "mongo", StreamFieldV1Codec())
                             for name in ("article", "appendix")},
        )
        self.ct = ContentType(id=7, app_label="external_content", model="fieldstatepage")
        self.base_ct = ContentType(id=1, app_label="wagtailcore", model="page")
        self.locales = {pk: Locale(id=pk, language_code=code)
                        for pk, code in ((1, "en"), (2, "fr"))}
        owner = self
        connection = SimpleNamespace(vendor="mysql", features=SimpleNamespace(
            has_select_for_update_nowait=True))
        # in_atomic_block 必须随实际替身层数变化，而非恒定 True。
        class OfflineConnection:
            vendor = connection.vendor
            features = connection.features

            @property
            def in_atomic_block(self) -> bool:
                return bool(owner.transactions)

        for module in (locking, copying):
            self.stack.enter_context(patch.object(module, "connection", OfflineConnection()))
        for model, layer in ((Page, "page"), (FieldStatePage, "page"),
                             (Revision, "revision"), (ExternalContentPublicationState, "state"),
                             (ExternalContentDeletionIntent, "intent")):
            self.stack.enter_context(patch.object(model, "objects", Rows(self, layer)))
        for module in ("copying", "coordinator", "mixins"):
            self.stack.enter_context(patch(
                f"external_content.{module}.get_storage_policy_for_model", return_value=self.policy))
            self.stack.enter_context(patch(
                f"external_content.{module}.ContentVersionStore", return_value=self.store))
        for module in ("copying", "coordinator"):
            self.stack.enter_context(patch(
                f"external_content.{module}.get_policy_for_version", return_value=self.policy))
        self.stack.enter_context(patch.object(
            transaction, "atomic", lambda *a, **kw: SnapshotAtomic(self)))
        self.stack.enter_context(patch.object(transaction, "on_commit", self.on_commit))
        # 原生装饰器已捕获 Atomic：仍进入相同快照封套，不将装饰层空操作化。
        self.stack.enter_context(patch.object(
            Atomic, "__enter__", lambda atomic: self.enter_native_atomic(atomic)))
        self.stack.enter_context(patch.object(
            Atomic, "__exit__", lambda atomic, *args: self.exit_native_atomic(atomic, *args)))
        self.stack.enter_context(patch.object(ContentType.objects, "get_for_id", return_value=self.ct))
        self.stack.enter_context(patch.object(Page, "get_base_content_type", return_value=self.base_ct))
        self.stack.enter_context(patch.object(FieldStatePage, "specific", property(lambda p: p)))
        self.stack.enter_context(patch.object(FieldStatePage, "specific_deferred", property(lambda p: p)))
        self.stack.enter_context(patch.object(FieldStatePage, "specific_class", property(lambda p: type(p))))
        self.stack.enter_context(patch.object(
            FieldStatePage, "get_parent", lambda p, **kw: self.parent(p)))
        self.stack.enter_context(patch.object(
            FieldStatePage, "get_children", lambda p: Rows(self, "page").filter(
                path__startswith=p.path, depth=p.depth + 1)))
        self.stack.enter_context(patch.object(
            FieldStatePage, "get_descendants", lambda p, inclusive=False: (
                Rows(self, "page").filter(path__startswith=p.path)
                if inclusive else Rows(self, "page").filter(path__startswith=p.path).exclude(pk=p.pk))))
        self.stack.enter_context(patch.object(
            FieldStatePage, "get_translation", lambda p, locale: Rows(self, "page").get(
                translation_key=p.translation_key, locale_id=locale.pk)))
        self.stack.enter_context(patch.object(
            FieldStatePage, "add_child", lambda p, *, instance: self.add_child(p, instance)))
        self.stack.enter_context(patch.object(
            FieldStatePage, "copy_all_child_relations", return_value={}))
        self.stack.enter_context(patch.object(
            FieldStatePage, "get_view_restrictions", return_value=SimpleNamespace(exists=lambda: True)))
        self.stack.enter_context(patch.object(Page, "save", lambda p, *a, **kw: self.persist(
            "page", p, kw.get("update_fields"))))
        self.stack.enter_context(patch.object(Revision, "save", lambda r, *a, **kw: self.persist(
            "revision", r, kw.get("update_fields"))))
        self.stack.enter_context(patch.object(
            ExternalContentPublicationState, "save", lambda s, *a, **kw: self.persist(
                "state", s, kw.get("update_fields"))))
        # 不替换复制/转换/同步 helper；下列均为关系 SQL/投影/日志 IO 尾部。
        for target in ("wagtail.models.copying._copy_m2m_relations",
                       "wagtail.models.pages._copy_m2m_relations",
                       "wagtail.actions.create_alias.log", "wagtail.actions.convert_alias.log",
                       "wagtail.log_actions.log"):
            self.stack.enter_context(patch(target))
        for signal in ("page_published", "copy_for_translation_done"):
            self.stack.enter_context(patch(f"wagtail.signals.{signal}.send"))
        self.stack.enter_context(patch(
            "wagtail.actions.copy_for_translation.find_available_slug",
            side_effect=lambda parent, slug: slug))
        # FK existence checks 是 SQL 尾部；保留原生反序列化及 Mixin 来源门禁。
        from modelcluster import models as cluster
        native_deserialize = cluster.model_from_serializable_data
        self.stack.enter_context(patch.object(
            cluster, "model_from_serializable_data",
            side_effect=lambda model, data, **kw: native_deserialize(model, data, check_fks=False)))
        self.root = self.seed(1, "0001")
        self.parent_page = self.seed(2, "00010001")
        self.source = self.seed(3, "000100010001")
        self.destination = self.seed(4, "00010002")
        self.sql_before = deepcopy(self.sql)
        self.mongo_before = deepcopy(self.store.docs)
        self.events.clear()
        self.counts.clear()
        self.instances.clear()

    def enter_native_atomic(self, atomic: Atomic) -> None:
        scope = SnapshotAtomic(self)
        self.atomic_instances.setdefault(id(atomic), []).append(scope)
        scope.__enter__()

    def exit_native_atomic(self, atomic: Atomic, *args: Any) -> bool:
        return self.atomic_instances[id(atomic)].pop().__exit__(*args)

    def on_commit(self, callback: Any, *args: Any, **kwargs: Any) -> None:
        if self.transactions:
            self.transactions[-1].callbacks.append(callback)
        else:
            callback()

    def forbidden_delete(self, *args: Any, **kwargs: Any) -> None:
        self.delete_calls.append((args, kwargs))
        raise AssertionError("L0_MONGO_DELETE_FORBIDDEN")

    def dump(self, row: Any) -> dict[str, Any]:
        values = {}
        for field in row._meta.concrete_fields:
            if field.attname in row.__dict__:
                value = row.__dict__[field.attname]
                if field.name in ("article", "appendix"):
                    value = field.get_prep_value(value)
                values[field.attname] = deepcopy(value)
        return values

    def read(self, layer: str, pk: int) -> Any:
        row = self.models[layer](**deepcopy(self.sql[layer][pk]))
        row._state.adding = False
        if layer == "page":
            row.content_type, row.locale = self.ct, self.locales[row.locale_id]
            row._test_content_store = self.store
            row._cluster_related_objects = {COMMENTS_RELATION_NAME: []}
        elif layer == "revision":
            row.content_type, row.base_content_type = self.ct, self.base_ct
        return row

    def persist(self, layer: str, row: Any, update_fields: Any = None) -> None:
        inserted = row.pk is None or row.pk not in self.sql[layer]
        event = "insert" if layer == "page" and inserted else layer + "_save"
        self.counts[event] = self.counts.get(event, 0) + 1
        self.events.append((event, row.pk))
        if self.fail_at == (event, self.counts[event]):
            raise self.injected
        if layer == "page":
            if inserted:
                row._cluster_related_objects = {COMMENTS_RELATION_NAME: []}
            pre_save.send(sender=FieldStatePage, instance=row, raw=False)
        if row.pk is None:
            pk = max(self.sql[layer], default=100) + 100
            row.pk = pk
            if layer == "page":
                row.id = pk
        values = self.dump(row)
        if update_fields is not None and not inserted:
            names = {row._meta.get_field(name).attname for name in update_fields
                     if name != COMMENTS_RELATION_NAME}
            values = {name: value for name, value in values.items() if name in names}
        self.sql[layer].setdefault(row.pk, {}).update(values)
        row._state.adding = False
        self.instances.append(row)
        post_save.send(sender=self.models[layer], instance=row, created=inserted, raw=False)

    def parent(self, page: Page) -> Page | None:
        return Rows(self, "page").filter(path=page.path[:-4]).first()

    def add_child(self, parent: Page, instance: Page) -> Page:
        durable = self.sql["page"][parent.pk]
        instance.path = parent.path + f"{durable['numchild'] + 1:04d}"
        instance.depth = parent.depth + 1
        self.persist("page", instance)
        durable["numchild"] += 1
        return instance

    def seed(self, pk: int, path: str) -> Page:
        page = FieldStatePage(
            id=pk, page_ptr_id=pk, content_type=self.ct, locale=self.locales[1],
            title=f"page-{pk}", slug=f"page-{pk}", path=path, depth=len(path) // 4,
            numchild=1, url_path=f"/page-{pk}/", translation_key=UUID(int=pk),
            live=True, latest_revision_id=pk * 10 + 2, live_revision_id=pk * 10 + 1,
            has_unpublished_changes=True, article=[], appendix=[],
        )
        page._state.adding = False
        page._test_content_store = self.store
        page._cluster_related_objects = {COMMENTS_RELATION_NAME: []}
        manifests = {}
        for offset, text in ((1, "A"), (2, "B"), (0, "C")):
            page.article = payload(f"{text}-{pk}\r\n")
            page.appendix = payload(f"appendix-{text}-{pk}", "appendix-id")
            manifest = ExternalContentCoordinator(self.store).prepare_external_content_for_revision(page)
            manifests[offset] = manifest.to_dict()
            content = page.serializable_data()
            content.update(_external_content_manifest=manifest.to_dict(), article=[], appendix=[])
            revision = Revision(
                id=pk * 10 + offset, object_id=str(pk), content_type=self.ct,
                base_content_type=self.base_ct, content=content,
            )
            self.sql["revision"][revision.pk] = self.dump(revision)
        state = ExternalContentPublicationState(
            page_id=pk, model_label=self.policy.model_label,
            draft_revision_id=pk * 10 + 2, draft_manifest=manifests[2],
            published_revision_id=pk * 10 + 1, published_manifest=manifests[1],
            publication_generation=1,
        )
        self.sql["state"][pk] = self.dump(state)
        page.article, page.appendix = [], []
        self.sql["page"][pk] = self.dump(page)
        return self.read("page", pk)

    def action(self, **kwargs: Any) -> CopyPageAction:
        return CopyPageAction(self.source, to=self.destination, log_action=None, **kwargs)

    def assert_rolled_back(self) -> None:
        self.assertEqual(self.sql, self.sql_before)
        for vid, doc in self.mongo_before.items():
            self.assertEqual(self.store.docs[vid], doc)
        self.assertEqual(self.delete_calls, [])
        self.assertEqual(self.transactions, [])

    def create_alias(self, source: Page | None = None, **kwargs: Any) -> Page:
        return CreatePageAliasAction(
            source or self.source, parent=self.destination, log_action=None, **kwargs,
        ).execute()

    def checkpoint(self) -> None:
        self.sql_before, self.mongo_before = deepcopy(self.sql), deepcopy(self.store.docs)
        self.events.clear()
        self.counts.clear()
        self.instances.clear()

    def context_snapshot(self) -> list[dict[str, Any]]:
        """保留属性存在性和原对象绑定，不对 threadlocal 做深拷贝。"""
        return [dict(context.__dict__) for context in (
            copying.translation_lifecycle_context,
            mixins._controlled_alias_context, mixins._controlled_copy_context,
            mixins._controlled_serialization_context, mixins._external_content_access_context,
            mixins._controlled_save_context,
        )]

    def test_parent_alias_then_translation_failure_retracts_entire_mapping(self) -> None:
        action = CopyPageForTranslationAction(
            self.source, self.locales[2], copy_parents=True,
        )
        before = self.context_snapshot()
        receivers = (list(pre_save.receivers), list(post_save.receivers))
        self.fail_at = ("revision_save", 1)
        with self.assertRaises(DatabaseError) as raised:
            action.execute()
        self.assertIs(raised.exception, self.injected)
        self.assert_rolled_back()
        self.assertIs(action.page, self.source)
        self.assertEqual(self.counts["insert"], 2)
        aliases = [row for row in self.instances
                   if isinstance(row, Page) and row.alias_of_id == 2]
        self.assertTrue(aliases)  # 父 Alias 确实成功写入过，而非入口提前拒绝。
        self.assertTrue(all(row.pk not in self.sql["page"] for row in aliases))
        self.assertEqual(self.context_snapshot(), before)
        self.assertEqual((pre_save.receivers, post_save.receivers), receivers)
        self.assertGreater(len(self.store.docs), len(self.mongo_before))

    def test_recursive_alias_nth_insert_failure_retracts_all_aliases(self) -> None:
        self.seed(5, self.source.path + "0001")
        self.seed(6, self.source.path + "0002")
        self.checkpoint()
        before = self.context_snapshot()
        self.fail_at = ("insert", 3)
        with self.assertRaises(DatabaseError) as raised:
            self.create_alias(recursive=True)
        self.assertIs(raised.exception, self.injected)
        self.assert_rolled_back()
        self.assertEqual(self.counts["insert"], 3)
        self.assertGreaterEqual(self.counts["state_save"], 1)
        self.assertEqual(self.context_snapshot(), before)
        # Alias 允许复用 canonical 不可变正文，不强制独立 Mongo 副本。
        self.assertEqual(self.store.docs, self.mongo_before)

    def alias_sync_fixture(self) -> tuple[Page, Page, Page]:
        first = self.create_alias()
        second = self.create_alias()
        nested = self.create_alias(first)
        self.checkpoint()
        return first, second, nested

    def test_alias_sync_nth_state_failure_retracts_all_states(self) -> None:
        self.alias_sync_fixture()
        before = self.context_snapshot()
        self.fail_at = ("state_save", 3)
        with self.assertRaises(DatabaseError) as raised:
            self.source.update_aliases()
        self.assertIs(raised.exception, self.injected)
        self.assert_rolled_back()
        self.assertEqual(self.counts["state_save"], 3)
        self.assertEqual(self.context_snapshot(), before)
        self.assertEqual(self.store.docs, self.mongo_before)

    def test_alias_sync_nth_native_page_failure_retracts_states_and_pages(self) -> None:
        first, second, nested = self.alias_sync_fixture()
        before = self.context_snapshot()
        self.fail_at = ("page_save", 2)
        with self.assertRaises(DatabaseError) as raised:
            self.source.update_aliases()
        self.assertIs(raised.exception, self.injected)
        self.assert_rolled_back()
        self.assertEqual(self.counts["page_save"], 2)
        self.assertGreaterEqual(self.counts["state_save"], 3)
        self.assertEqual(self.context_snapshot(), before)
        self.assertEqual(self.store.docs, self.mongo_before)
        # 原生反序列化所得污染实例被丢弃；调用者与持久重新读取不是同一对象。
        self.assertEqual([first.alias_of_id, second.alias_of_id, nested.alias_of_id],
                         [3, 3, first.pk])

    def test_alias_sync_uses_formal_a_and_restores_none_and_sentinel_authorization(self) -> None:
        first, second, nested = self.alias_sync_fixture()
        # 陈旧调用者甚至持有新正文赋值，仍不能把 B/调用者内容公开到 Alias。
        self.source.title = "stale-caller"
        self.source.article = payload("must-not-publish")
        for value in (None, object()):
            with self.subTest(value=value), ExitStack() as scope:
                for name in ("in_alias", "alias_update_source_page_id"):
                    scope.enter_context(locking.preserve_threadlocal_context(
                        mixins._controlled_alias_context, name, value,
                    ))
                before = self.context_snapshot()
                self.source.update_aliases()
                self.assertEqual(self.context_snapshot(), before)
                for alias, parent_id in ((first, 3), (second, 3), (nested, first.pk)):
                    state = self.sql["state"][alias.pk]
                    self.assertEqual(state["source_page_id"], parent_id)
                    self.assertEqual(state["source_revision_id"], 31)
                    self.assertIsNone(state["published_revision_id"])
                    self.assertEqual(state["published_manifest"],
                                     self.sql_before["state"][3]["published_manifest"])
                    self.assertEqual(self.sql["page"][alias.pk]["title"], "page-3")
                    self.assertEqual(list(Rows(self, "revision").for_instance(alias)), [])
        self.assertEqual(self.store.docs, self.mongo_before)
        for layer in ("revision", "intent"):
            self.assertEqual(self.sql[layer], self.sql_before[layer])
        self.assertEqual(self.sql["page"][3], self.sql_before["page"][3])
        self.assertEqual(self.sql["state"][3], self.sql_before["state"][3])
        self.assertEqual(self.delete_calls, [])

    def test_convert_success_returns_persistent_independent_page_not_caller_binding(self) -> None:
        caller = self.create_alias()
        self.checkpoint()
        action = ConvertAliasPageAction(caller, log_action=None)
        before = self.context_snapshot()
        converted = action.execute()
        self.assertIs(action.page, caller)
        self.assertIsNot(converted, caller)
        self.assertEqual(caller.alias_of_id, 3)  # 陈旧调用者不能冒充转换后持久真相。
        state = self.sql["state"][converted.pk]
        self.assertIsNone(self.sql["page"][converted.pk]["alias_of_id"])
        self.assertIsNone(state["source_page_id"])
        self.assertIsNone(state["source_revision_id"])
        revision_pk = state["published_revision_id"]
        self.assertEqual(state["draft_revision_id"], revision_pk)
        self.assertEqual(self.sql["page"][converted.pk]["live_revision_id"], revision_pk)
        self.assertEqual(self.sql["revision"][revision_pk]["object_id"], str(converted.pk))
        manifest = state["published_manifest"]
        self.assertEqual(manifest["page_id"], converted.pk)
        for name, entry in manifest["fields"].items():
            source = self.sql_before["state"][3]["published_manifest"]["fields"][name]
            self.assertEqual(entry["payload_sha256"], source["payload_sha256"])
            self.assertEqual(self.store.docs[entry["version_id"]]["page_id"], converted.pk)
        for layer, rows in self.sql_before.items():
            for pk, row in rows.items():
                if layer in ("page", "state") and pk == converted.pk:
                    continue
                self.assertEqual(self.sql[layer][pk], row)
        for vid, doc in self.mongo_before.items():
            self.assertEqual(self.store.docs[vid], doc)
        self.assertEqual(self.context_snapshot(), before)
        self.assertEqual(self.delete_calls, [])

    def test_target_second_version_write_failure_keeps_first_version_and_retracts_sql(self) -> None:
        native_save = self.store.save_version_with_status
        writes = []
        before = self.context_snapshot()
        failure = ContentStoreError("L0_TARGET_SECOND_VERSION_FAILURE")

        def save_with_failure(**doc: Any) -> Any:
            writes.append(doc["field_name"])
            if len(writes) == 2:
                raise failure
            return native_save(**doc)

        with patch.object(self.store, "save_version_with_status", side_effect=save_with_failure):
            with self.assertRaises(ContentStoreError) as raised:
                self.action(copy_revisions=True, keep_live=True).execute()
        self.assertIs(raised.exception, failure)
        self.assert_rolled_back()
        # Manifest JSON 的字段顺序不是业务契约；只要求第二个不同字段故障。
        self.assertEqual(len(writes), 2)
        self.assertEqual(set(writes), {"article", "appendix"})
        self.assertEqual(len(self.store.docs), len(self.mongo_before) + 1)
        self.assertEqual(self.context_snapshot(), before)

    def test_inner_copy_success_outer_rollback_discards_result_and_callback(self) -> None:
        callbacks = []
        action = self.action(copy_revisions=True, keep_live=True)
        before = self.context_snapshot()
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                target = action.execute()
                transaction.on_commit(lambda: callbacks.append(target.pk))
                self.assertIn(target.pk, self.sql["page"])
                raise self.injected
        self.assert_rolled_back()
        self.assertNotIn(target.pk, self.sql["page"])  # target 只可丢弃。
        self.assertEqual(callbacks, [])
        self.assertNotIn("_external_content_copy_frame", action.__dict__)
        self.assertEqual(self.context_snapshot(), before)
        self.assertEqual(self.counts["insert"], 1)
        self.assertGreater(len(self.store.docs), len(self.mongo_before))

    def test_unknown_commit_both_outcomes_do_not_replay_or_compensate(self) -> None:
        for outcome in ("unknown_committed", "unknown_not_committed"):
            with self.subTest(outcome=outcome):
                # 两种持久结果从相同 SQL 起点独立执行，已准备版本仍允许幂等复用。
                self.sql = deepcopy(self.sql_before)
                self.counts.clear()
                self.exit_outcome = outcome
                action = self.action(copy_revisions=True, keep_live=True)
                sentinel = object()
                action._external_content_copy_frame = sentinel
                before = self.context_snapshot()
                with self.assertRaises(DatabaseError) as raised:
                    action.execute()
                self.assertIs(raised.exception, self.injected)
                self.assertIs(action._external_content_copy_frame, sentinel)
                self.assertEqual(self.context_snapshot(), before)
                self.assertEqual(self.counts["insert"], 1)
                self.assertEqual(self.counts["revision_save"], 3)
                self.assertEqual(self.delete_calls, [])
                self.assertEqual(self.transactions, [])
                if outcome == "unknown_not_committed":
                    self.assert_rolled_back()
                else:
                    targets = set(self.sql["page"]) - set(self.sql_before["page"])
                    self.assertEqual(len(targets), 1)
                    target_pk = targets.pop()
                    self.assertEqual(self.sql["state"][target_pk]["page_id"], target_pk)
                    self.assertEqual(self.sql["page"][4]["numchild"], 2)
                    for layer, rows in self.sql_before.items():
                        for pk, row in rows.items():
                            expected = dict(row, numchild=2) if layer == "page" and pk == 4 else row
                            self.assertEqual(self.sql[layer][pk], expected)
                for vid, doc in self.mongo_before.items():
                    self.assertEqual(self.store.docs[vid], doc)
                self.assertGreater(len(self.store.docs), len(self.mongo_before))

    def test_post_commit_callback_failure_keeps_copy_without_replay(self) -> None:
        callbacks = []
        action = self.action(copy_revisions=True, keep_live=True)
        action._external_content_copy_frame = None
        before = self.context_snapshot()

        def failed_callback() -> None:
            callbacks.append("executed")
            raise self.injected

        with self.assertRaises(DatabaseError) as raised:
            with transaction.atomic():
                target = action.execute()
                transaction.on_commit(failed_callback)
        self.assertIs(raised.exception, self.injected)
        self.assertEqual(callbacks, ["executed"])
        self.assertIn(target.pk, self.sql["page"])
        self.assertIn(target.pk, self.sql["state"])
        self.assertEqual(self.counts["insert"], 1)
        self.assertEqual(self.counts["revision_save"], 3)
        self.assertIn("_external_content_copy_frame", action.__dict__)
        self.assertIsNone(action._external_content_copy_frame)
        self.assertEqual(self.context_snapshot(), before)
        self.assertEqual(self.transactions, [])
        self.assertEqual(self.delete_calls, [])

    def test_convert_revision_failure_rolls_back_and_restores_action_page(self) -> None:
        caller = self.create_alias()
        self.checkpoint()
        action = ConvertAliasPageAction(caller, log_action=None)
        self.fail_at = ("revision_save", 1)
        with self.assertRaises(DatabaseError) as raised:
            action.execute()
        self.assertIs(raised.exception, self.injected)
        self.assert_rolled_back()
        self.assertIs(action.page, caller)
        self.assertEqual(caller.alias_of_id, 3)
        self.assertGreater(len(self.store.docs), len(self.mongo_before))

    def test_convert_final_state_failure_retracts_revision_and_formal_mapping(self) -> None:
        caller = self.create_alias()
        self.checkpoint()
        action = ConvertAliasPageAction(caller, log_action=None)
        self.fail_at = ("state_save", 2)
        with self.assertRaises(DatabaseError) as raised:
            action.execute()
        self.assertIs(raised.exception, self.injected)
        self.assert_rolled_back()
        self.assertEqual(self.counts["revision_save"], 1)
        self.assertEqual(self.counts["state_save"], 2)
        self.assertIs(action.page, caller)

    def test_history_failure_rolls_back_target_mapping_but_keeps_prepared_versions(self) -> None:
        self.fail_at = ("revision_save", 2)
        with self.assertRaises(DatabaseError) as raised:
            self.action(copy_revisions=True, keep_live=True).execute()
        self.assertIs(raised.exception, self.injected)
        self.assert_rolled_back()
        self.assertGreater(len(self.store.docs), len(self.mongo_before))
        self.assertEqual(self.counts["insert"], 1)
        self.assertEqual(self.counts["revision_save"], 2)
        # 失败新增实例持有已撤回 PK；明确丢弃，不能将它视为持久存在。
        discarded = [row for row in self.instances if isinstance(row, Page)]
        self.assertTrue(discarded)
        self.assertTrue(all(row.pk not in self.sql["page"] for row in discarded))

    def test_history_maps_real_revision_ids_and_rebinds_each_manifest(self) -> None:
        target = self.action(copy_revisions=True, keep_live=True).execute()
        target_state = self.read("state", target.pk)
        revisions = list(Rows(self, "revision").for_instance(target).order_by("pk"))
        self.assertEqual(len(revisions), 3)
        self.assertEqual([r.content["_external_content_manifest"]["page_id"] for r in revisions],
                         [target.pk, target.pk, target.pk])
        hashes = {self.sql["revision"][pk]["content"]["_external_content_manifest"]["fields"]
                  ["article"]["payload_sha256"]: pk for pk in (30, 31, 32)}
        mapped = {hashes[r.content["_external_content_manifest"]["fields"]["article"]["payload_sha256"]]:
                  r.pk for r in revisions}
        self.assertEqual(target_state.published_revision_id, mapped[31])
        self.assertEqual(target_state.draft_revision_id, mapped[32])
        self.assertEqual(target.live_revision_id, mapped[31])
        self.assertEqual(target.latest_revision_id, mapped[32])
        for revision in revisions:
            raw = revision.content["_external_content_manifest"]
            for name, entry in raw["fields"].items():
                self.assertEqual(self.store.docs[entry["version_id"]]["page_id"], target.pk)
                self.assertEqual(self.store.docs[entry["version_id"]]["field_name"], name)
        for layer, rows in self.sql_before.items():
            self.assertEqual({pk: self.sql[layer][pk] for pk in rows}, rows
                             if layer != "page" else {**rows, 4: dict(rows[4], numchild=2)})
        self.assertEqual(self.delete_calls, [])

    def test_copy_frame_existing_none_is_restored_after_body_failure(self) -> None:
        action = self.action(copy_revisions=True)
        action._external_content_copy_frame = None
        self.fail_at = ("revision_save", 2)
        with self.assertRaises(DatabaseError):
            action.execute()
        self.assert_rolled_back()
        self.assertIn("_external_content_copy_frame", action.__dict__)
        self.assertIsNone(action._external_content_copy_frame)

    def test_nth_subtree_failure_retracts_all_nodes_states_and_parent_count(self) -> None:
        self.seed(5, self.source.path + "0001")
        self.seed(6, self.source.path + "0002")
        self.sql_before, self.mongo_before = deepcopy(self.sql), deepcopy(self.store.docs)
        self.fail_at = ("insert", 3)
        action = self.action(recursive=True, copy_revisions=True, keep_live=True)
        sentinel = object()
        action._external_content_copy_frame = sentinel
        with self.assertRaises(DatabaseError):
            action.execute()
        self.assert_rolled_back()
        self.assertIs(action._external_content_copy_frame, sentinel)
        self.assertEqual(self.counts["insert"], 3)
        self.assertGreaterEqual(self.counts["state_save"], 2)
        self.assertGreater(len(self.store.docs), len(self.mongo_before))
