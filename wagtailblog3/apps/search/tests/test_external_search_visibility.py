"""P-05 L0：Scope 与正式出口；替身只替代数据库和不可变正文读取。"""

from datetime import datetime, timezone
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase


class ExternalSearchVisibilityTests(SimpleTestCase):
    def service(self):
        try:
            return import_module("search.services.search_scope")
        except ModuleNotFoundError:
            self.fail("缺少共享 SearchScope 与 SQL public 安全出口")

    def test_date_boundaries_are_half_open_site_timezone_and_bound_to_epochs(self):
        module = self.service()
        scope = module.SearchScope(
            site_id=7, root_page_id=2, root_path="00010002", locale_id=3,
            search_type="guides", order_by="date", registry_epoch="r1",
            index_epoch="i1", cache_epoch="c1",
            start_at=datetime(2026, 10, 7, 16, tzinfo=timezone.utc),
            end_at=datetime(2026, 10, 8, 16, tzinfo=timezone.utc),
        )
        self.assertEqual(scope.as_dict()["visibility"], "public")
        self.assertEqual(scope.as_dict()["locale_id"], 3)
        self.assertNotEqual(scope.digest, module.SearchScope(
            **{**scope.__dict__, "index_epoch": "i2"}
        ).digest)
        start, end = module.parse_date_range("2026-10-08", "2026-10-08", "Asia/Singapore")
        self.assertEqual(start.isoformat(), "2026-10-07T16:00:00+00:00")
        self.assertEqual(end.isoformat(), "2026-10-08T16:00:00+00:00")

    def test_bad_dates_and_sort_are_parameter_errors_not_silently_ignored(self):
        module = self.service()
        for start, end in (("2026-02-30", ""), ("2026-10-09", "2026-10-08")):
            with self.subTest(start=start), self.assertRaises(module.SearchRequestError):
                module.parse_date_range(start, end, "Asia/Singapore")
        with self.assertRaises(module.SearchRequestError):
            module.SearchScope(7, 2, "0001", 3, order_by="body")

    def test_sql_scope_includes_live_public_ancestor_locale_and_freeze(self):
        module = self.service()

        class Query:
            def __init__(self):
                self.calls = []
            def live(self):
                self.calls.append(("live", {}))
                return self
            def public(self):
                self.calls.append(("public", {}))
                return self
            def filter(self, **kwargs):
                self.calls.append(("filter", kwargs))
                return self
            def exclude(self, **kwargs):
                self.calls.append(("exclude", kwargs))
                return self
            def values(self, *args):
                return self

        query = Query()
        frozen = Query()
        with patch.object(module.Page, "objects", query), patch.object(
            module.ExternalContentDeletionIntent, "objects", frozen
        ):
            module.public_queryset(module.SearchScope(7, 2, "00010002", 3))
        self.assertIn(("live", {}), query.calls)
        self.assertIn(("public", {}), query.calls)
        self.assertIn(("filter", {"path__startswith": "00010002", "locale_id": 3}), query.calls)
        self.assertIn(("exclude", {"pk__in": frozen}), query.calls)
        self.assertIn(("exclude", {"status": "cancelled"}), frozen.calls)


class SafeResultTests(SimpleTestCase):
    def service(self):
        try:
            return import_module("search.services.result_safety")
        except ModuleNotFoundError:
            self.fail("缺少统一候选正文围栏")

    def test_native_uses_live_revision_metadata_never_draft_or_es_highlights(self):
        module = self.service()
        page = SimpleNamespace(
            pk=8, live_revision_id=10, title="未发布草稿", url="/native/",
            _meta=SimpleNamespace(label_lower="home.homepage"),
            get_url=lambda **kwargs: "/native/",
        )
        revision = SimpleNamespace(content={"title": "正式标题", "intro": "<b>正式摘要</b>"})
        result = module.native_result(page, revision, "pages")
        self.assertEqual(result["title"], "正式标题")
        self.assertEqual(result["search_description"], "正式摘要")
        self.assertEqual(result["highlight_fragments"], [])
        self.assertNotIn("未发布草稿", str(result))
        self.assertIsNone(module.native_result(page, None, "pages"))

    def test_managed_fence_rejects_version_digest_schema_and_identity_drift(self):
        module = self.service()
        state = SimpleNamespace(
            page_id=8, content_version=11, publication_generation=2,
            snapshot_digest="a" * 64, projection_schema_version=2,
            model_label="guides.guidepage", search_type="guides",
            snapshot_kind="external_v1", content_hash="b" * 64,
            searchable=True, desired_operation="upsert",
        )
        source = dict(state.__dict__)
        self.assertTrue(module.matches_state(source, state))
        for key, value in (
            ("content_version", 10), ("snapshot_digest", "c" * 64),
            ("projection_schema_version", 1), ("page_id", 9),
            ("model_label", "blog.blogpage"), ("publication_generation", True),
        ):
            with self.subTest(key=key):
                self.assertFalse(module.matches_state({**source, key: value}, state))
