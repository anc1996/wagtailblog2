"""D3 P-03 的 additive schema 契约；只检查内存模型和迁移状态，不访问数据库。"""

from django.db import migrations, models
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.state import ProjectState
from django.test import SimpleTestCase, override_settings

from search.models import ContentSearchOutbox, ContentSearchState, ContentSearchTarget


LEGACY_NODE = ("search", "0008_contentsearchscopejob_leases")
PROJECTION_NODE = ("search", "0009_contentsearch_projection_schema")
SNAPSHOT_FIELDS = {
    "model_label": (models.CharField, 255),
    "search_type": (models.CharField, 64),
    "snapshot_kind": (models.CharField, 32),
    "published_snapshot": (models.JSONField, None),
    "snapshot_digest": (models.CharField, 64),
    "projection_schema_version": (models.PositiveIntegerField, None),
}


class ProjectionSchemaTests(SimpleTestCase):
    """防止旧事件被默认升级为 v2，或 schema 迁移意外修改旧列和幂等约束。"""

    def test_state_and_outbox_snapshot_metadata_is_nullable_without_defaults(self) -> None:
        for model in (ContentSearchState, ContentSearchOutbox):
            fields = {field.name: field for field in model._meta.fields}
            for name, (field_type, max_length) in SNAPSHOT_FIELDS.items():
                with self.subTest(model=model.__name__, field=name):
                    self.assertIn(name, fields)
                    field = fields[name]
                    self.assertIs(type(field), field_type)
                    self.assertEqual(field.max_length, max_length)
                    self.assertTrue(field.null)
                    self.assertTrue(field.blank)
                    self.assertFalse(field.has_default())
                    self.assertFalse(field.has_db_default())
                    self.assertFalse(field.db_index)
                    self.assertFalse(field.unique)

    def test_legacy_instances_keep_missing_snapshot_fields_as_none(self) -> None:
        for instance in (
            ContentSearchState(page_id=1001),
            ContentSearchOutbox(page_id=1001, content_version=1, operation="upsert"),
        ):
            for name in SNAPSHOT_FIELDS:
                with self.subTest(model=type(instance).__name__, field=name):
                    self.assertIsNone(getattr(instance, name, "MISSING"))

    def test_explicit_legacy_nulls_are_not_promoted_to_v2(self) -> None:
        values = {name: None for name in SNAPSHOT_FIELDS}
        for model in (ContentSearchState, ContentSearchOutbox):
            with self.subTest(model=model.__name__):
                self.assertTrue(set(SNAPSHOT_FIELDS).issubset(
                    {field.name for field in model._meta.fields}
                ))
                instance = model(page_id=1001, **values)
                for name in SNAPSHOT_FIELDS:
                    self.assertIsNone(getattr(instance, name))

    def test_snapshot_json_and_explicit_version_are_retained_without_rewriting(self) -> None:
        snapshot = {
            "snapshot_schema_version": 1,
            "snapshot_kind": "external_v1",
            "page_id": 1001,
            "model_label": "guides.guidepage",
            "search_type": "guides",
            "projection_schema_version": 2,
        }
        for model in (ContentSearchState, ContentSearchOutbox):
            with self.subTest(model=model.__name__):
                self.assertIn("published_snapshot", {field.name for field in model._meta.fields})
                instance = model(
                    page_id=1001,
                    published_snapshot=snapshot,
                    projection_schema_version=2,
                )
                self.assertEqual(instance.published_snapshot, snapshot)
                self.assertEqual(instance.projection_schema_version, 2)

    def test_target_defaults_to_v1_and_allows_explicit_v2(self) -> None:
        fields = {field.name: field for field in ContentSearchTarget._meta.fields}
        self.assertIn("projection_schema_version", fields)
        field = fields["projection_schema_version"]
        self.assertIs(type(field), models.PositiveIntegerField)
        self.assertFalse(field.null)
        self.assertFalse(field.blank)
        self.assertEqual(field.default, 1)
        self.assertFalse(field.has_db_default())
        self.assertEqual(ContentSearchTarget().projection_schema_version, 1)
        self.assertEqual(
            ContentSearchTarget(projection_schema_version=2).projection_schema_version,
            2,
        )


@override_settings(INSTALLED_APPS=["search"])
class ProjectionSchemaMigrationTests(SimpleTestCase):
    """只加载 search 图，避免 Wagtail 历史迁移导入时探测 MySQL 版本。"""

    def setUp(self) -> None:
        self.loader = MigrationLoader(None)
        self.assertIn(PROJECTION_NODE, self.loader.disk_migrations)
        self.migration = self.loader.disk_migrations[PROJECTION_NODE]

    def test_migration_extends_0008_as_the_only_search_leaf(self) -> None:
        self.assertEqual(self.migration.dependencies, [LEGACY_NODE])
        self.assertEqual(self.loader.graph.leaf_nodes("search"), [PROJECTION_NODE])
        self.assertNotIn("search", self.loader.detect_conflicts())

    def test_migration_only_adds_nullable_snapshots_and_legacy_target_default(self) -> None:
        expected = {
            (model_name, field_name)
            for model_name in ("contentsearchstate", "contentsearchoutbox")
            for field_name in SNAPSHOT_FIELDS
        } | {("contentsearchtarget", "projection_schema_version")}
        operations = self.migration.operations
        self.assertEqual(len(operations), 13)
        self.assertTrue(all(type(operation) is migrations.AddField for operation in operations))
        self.assertEqual(
            {(operation.model_name, operation.name) for operation in operations},
            expected,
        )
        for operation in operations:
            with self.subTest(model=operation.model_name, field=operation.name):
                self.assertTrue(operation.preserve_default)
                if operation.model_name == "contentsearchtarget":
                    self.assertIs(type(operation.field), models.PositiveIntegerField)
                    self.assertFalse(operation.field.null)
                    self.assertEqual(operation.field.default, 1)
                else:
                    field_type, max_length = SNAPSHOT_FIELDS[operation.name]
                    self.assertIs(type(operation.field), field_type)
                    self.assertEqual(operation.field.max_length, max_length)
                    self.assertTrue(operation.field.null)
                    self.assertTrue(operation.field.blank)
                    self.assertFalse(operation.field.has_default())
                self.assertFalse(operation.field.has_db_default())

    def test_migration_state_preserves_every_legacy_field_constraint_and_index(self) -> None:
        before = self.loader.project_state([LEGACY_NODE])
        after = self.loader.project_state([PROJECTION_NODE])
        self.assertEqual(set(before.models), set(after.models))
        for key, old_model in before.models.items():
            new_model = after.models[key]
            with self.subTest(model=key):
                self.assertEqual(old_model.options, new_model.options)
                self.assertEqual(old_model.bases, new_model.bases)
                self.assertEqual(old_model.managers, new_model.managers)
                for name, field in old_model.fields.items():
                    self.assertEqual(field.deconstruct(), new_model.fields[name].deconstruct())
                additions = set(new_model.fields) - set(old_model.fields)
                if key in (("search", "contentsearchstate"), ("search", "contentsearchoutbox")):
                    self.assertEqual(additions, set(SNAPSHOT_FIELDS))
                elif key == ("search", "contentsearchtarget"):
                    self.assertEqual(additions, {"projection_schema_version"})
                else:
                    self.assertEqual(additions, set())

    def test_static_migration_state_matches_current_search_models(self) -> None:
        from django.apps import apps

        detector = MigrationAutodetector(
            self.loader.project_state(),
            ProjectState.from_apps(apps),
        )
        changes = detector.changes(graph=self.loader.graph)
        self.assertNotIn("search", changes)

    def test_historical_models_keep_legacy_nulls_and_target_v1_default(self) -> None:
        state = self.loader.project_state([PROJECTION_NODE])
        for model_name in ("ContentSearchState", "ContentSearchOutbox"):
            model = state.apps.get_model("search", model_name)
            instance = model(page_id=1001)
            for name in SNAPSHOT_FIELDS:
                with self.subTest(model=model_name, field=name):
                    self.assertIsNone(getattr(instance, name))
        target = state.apps.get_model("search", "ContentSearchTarget")
        self.assertEqual(target().projection_schema_version, 1)
