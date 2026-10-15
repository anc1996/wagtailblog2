"""P-03 按模型排他的分类注册，不以 ES 是否已有文档决定归属。"""

import importlib
import importlib.util
from contextlib import ExitStack
from unittest.mock import patch

from django.test import SimpleTestCase
from django.test.utils import isolate_apps
from wagtail import blocks
from wagtail.fields import StreamField
from wagtail.models import Page

from external_content.mixins import ExternalContentPageMixin
from guides.models import GuidePage, GuideIndexPage
from blog.models import BlogPage


@isolate_apps("guides")
class ContentRegistryTests(SimpleTestCase):
    def external_model(self, name):
        """隔离未来双字段模型，不注册到全局 apps，也不实例化或写策略基线。"""
        return type(name, (ExternalContentPageMixin, Page), {
            "__module__": __name__,
            "Meta": type("Meta", (), {"app_label": "guides"}),
            "body": StreamField([("text", blocks.CharBlock())], use_json_field=True),
            "appendix": StreamField([("text", blocks.CharBlock())], use_json_field=True),
            "content_storage": {"body": "mongo", "appendix": "mongo"},
        })

    def registry_class(self):
        self.assertIsNotNone(importlib.util.find_spec("search.content_registry"))
        return importlib.import_module("search.content_registry").ContentRegistry

    def test_default_pages_and_ordinary_page_stays_native(self):
        with patch.object(GuidePage, "content_search", {}, create=True):
            registry = self.registry_class().discover([GuideIndexPage, GuidePage, BlogPage])
        entry = registry.for_model(GuidePage)
        self.assertIs(entry.model_class, GuidePage)
        self.assertEqual(entry.model_label, "guides.guidepage")
        self.assertEqual(entry.search_type, "pages")
        self.assertEqual(entry.snapshot_kind, "external_v1")
        self.assertTrue(entry.enabled)
        self.assertEqual(entry.fields, ("body",))
        self.assertIn(GuidePage, registry.native_excluded_models)
        self.assertIn(BlogPage, registry.native_excluded_models)
        self.assertNotIn(GuideIndexPage, registry.native_excluded_models)
        self.assertIsNone(registry.for_model(GuideIndexPage))

    def test_independent_category_and_disabled_remain_excluded(self):
        with patch.object(GuidePage, "content_search", {"type": "guides", "label": "专题指南", "enabled": True}, create=True):
            active = self.registry_class().discover([GuidePage, BlogPage])
        self.assertEqual(active.for_model(GuidePage).search_type, "guides")
        self.assertEqual(dict(active.categories)["guides"], "专题指南")
        with patch.object(GuidePage, "content_search", {"type": "guides", "label": "专题指南", "enabled": False}, create=True):
            disabled = self.registry_class().discover([GuidePage, BlogPage])
        self.assertFalse(disabled.for_model(GuidePage).enabled)
        self.assertIn(GuidePage, disabled.native_excluded_models)
        self.assertNotIn("guides", dict(disabled.categories))
        self.assertNotEqual(active.epoch, disabled.epoch)

    def test_conflicting_reserved_category_and_unknown_configuration_rejected(self):
        cls = self.registry_class()
        from search.content_registry import ContentRegistryError
        for declaration in (
            {"type": "all"},
            {"type": "blog", "label": "占用博客"},
            {"type": "pages", "label": "改名冲突"},
            {"enabled": "false"},
            {"enabled": 1},
            {"fields": ["body"]},
            {"fields": ["missing"]},
            {"search_type": "guides", "label": "专题指南"},
            {"unrecognized": True},
            {"type": ""},
            {"type": " Guides"},
            {"type": "bad-key"},
            {"type": "guides/other"},
            {"type": "a" * 65},
            {"type": None},
            {"type": False},
            {"label": ""},
            {"label": "   "},
            {"label": 1},
            None,
            [],
        ):
            with self.subTest(declaration=declaration), patch.object(GuidePage, "content_search", declaration, create=True):
                with self.assertRaises(ContentRegistryError):
                    cls().discover([GuidePage, BlogPage])

    def test_auto_discovery_and_epoch_stable_under_order(self):
        cls = self.registry_class()
        from search.content_registry import get_content_registry
        self.assertIsNotNone(get_content_registry().for_model(GuidePage))
        self.assertEqual(
            cls().discover([BlogPage, GuidePage]).epoch,
            cls().discover([GuidePage, BlogPage]).epoch,
        )

    def test_blog_contract_is_fixed_and_entries_are_iterable(self):
        with patch.object(BlogPage, "content_search", {"type": "recipes", "enabled": False}, create=True):
            registry = self.registry_class().discover([BlogPage])
        entry = registry.for_model(BlogPage)
        self.assertIs(entry.model_class, BlogPage)
        self.assertEqual(entry.model_label, "blog.blogpage")
        self.assertEqual(entry.search_type, "blog")
        self.assertEqual(entry.snapshot_kind, "blog_v1")
        self.assertEqual(entry.fields, ("body",))
        self.assertTrue(entry.enabled)
        self.assertEqual(tuple(registry.entries), (entry,))
        self.assertIsInstance(registry.native_excluded_models, tuple)
        self.assertIsInstance(registry.categories, tuple)
        self.assertEqual(tuple(key for key, _ in registry.categories), ("all", "blog", "pages"))

    def test_all_declared_mongo_fields_are_included_without_subset_option(self):
        model = self.external_model("DualFieldPage")
        registry = self.registry_class().discover([model])
        self.assertEqual(registry.for_model(model).fields, ("appendix", "body"))
        self.assertEqual(registry.for_model(model).search_type, "pages")
        from search.content_registry import ContentRegistryError
        with patch.object(model, "content_search", {"fields": ["body"]}, create=True):
            with self.assertRaises(ContentRegistryError):
                self.registry_class().discover([model])

    def test_same_category_can_share_models_but_not_conflicting_labels(self):
        model = self.external_model("OtherGuidePage")
        with patch.object(model, "content_search", {"type": "guides", "label": "专题指南"}, create=True):
            registry = self.registry_class().discover([model, GuidePage])
        self.assertEqual(registry.for_model(model).search_type, "guides")
        self.assertEqual(registry.categories[-1], ("guides", "专题指南"))
        self.assertEqual(len([key for key, _ in registry.categories if key == "guides"]), 1)
        from search.content_registry import ContentRegistryError
        # 禁用不解除声明冲突，避免启用后才发现分类标签漂移。
        for enabled in (True, False):
            with self.subTest(enabled=enabled), patch.object(model, "content_search", {
                "type": "guides", "label": "不同标签", "enabled": enabled,
            }, create=True):
                for ordered in ([model, GuidePage], [GuidePage, model]):
                    with self.assertRaises(ContentRegistryError):
                        self.registry_class().discover(ordered)

    def test_categories_and_epoch_are_stable_for_model_and_field_order(self):
        model = self.external_model("RecipePage")
        with patch.object(model, "content_search", {"type": "recipes", "label": "食谱"}, create=True):
            first = self.registry_class().discover([model, GuidePage, BlogPage])
            with patch.object(model, "content_storage", {"appendix": "mongo", "body": "mongo"}):
                second = self.registry_class().discover([BlogPage, GuidePage, model, model])
        self.assertEqual(first.entries, second.entries)
        self.assertEqual(first.categories, second.categories)
        self.assertEqual(first.categories[-2:], (("guides", "专题指南"), ("recipes", "食谱")))
        self.assertEqual(first.epoch, second.epoch)
        self.assertRegex(first.epoch, r"^[0-9a-f]{64}$")

    def test_epoch_changes_for_label_fields_category_and_enablement(self):
        model = self.external_model("EpochPage")
        model.content_search = {"type": "recipes", "label": "食谱"}
        cls = self.registry_class()
        baseline = cls().discover([model]).epoch
        for declaration in (
            {"type": "recipes", "label": "菜谱"},
            {"type": "guides", "label": "专题指南"},
            {"type": "recipes", "label": "食谱", "enabled": False},
        ):
            with self.subTest(declaration=declaration), patch.object(model, "content_search", declaration, create=True):
                self.assertNotEqual(baseline, cls().discover([model]).epoch)
        with patch.object(model, "content_storage", {"body": "mongo"}):
            self.assertNotEqual(baseline, cls().discover([model]).epoch)

    def test_ordinary_page_is_not_managed_by_a_search_or_storage_declaration(self):
        with patch.object(GuideIndexPage, "content_search", {"type": "guides"}, create=True), patch.object(
            GuideIndexPage, "content_storage", {"intro": "mongo"}, create=True
        ):
            registry = self.registry_class().discover([GuideIndexPage])
        self.assertIsNone(registry.for_model(GuideIndexPage))
        self.assertEqual(registry.native_excluded_models, ())

    def test_discovery_avoids_database_network_and_storage_policy_io(self):
        cls = self.registry_class()
        from search.content_registry import get_content_registry
        with ExitStack() as stack:
            for target in (
                "django.db.backends.base.base.BaseDatabaseWrapper.ensure_connection",
                "socket.socket.connect",
                "builtins.open",
                "external_content.registry.get_storage_policy_for_model",
                "external_content.registry.record_policy_baseline",
            ):
                stack.enter_context(patch(target, side_effect=AssertionError("发现不得执行外部 I/O")))
            explicit = cls().discover([GuidePage, BlogPage])
            automatic = get_content_registry()
        self.assertEqual(explicit.for_model(GuidePage), automatic.for_model(GuidePage))

    def test_failed_discovery_does_not_change_an_existing_registry(self):
        cls = self.registry_class()
        registry = cls.discover([BlogPage])
        entries, epoch, categories = registry.entries, registry.epoch, registry.categories
        from search.content_registry import ContentRegistryError
        with patch.object(GuidePage, "content_search", {"type": "all"}, create=True):
            with self.assertRaises(ContentRegistryError):
                cls.discover([BlogPage, GuidePage])
        self.assertEqual(registry.entries, entries)
        self.assertEqual(registry.epoch, epoch)
        self.assertEqual(registry.categories, categories)
