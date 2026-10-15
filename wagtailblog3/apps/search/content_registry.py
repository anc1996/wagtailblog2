"""按模型划分搜索来源；发现只读声明，不加载正文或登记存储策略。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import hashlib
import json
import re

from django.apps import apps
from django.core.exceptions import FieldDoesNotExist
from wagtail.models import Page


_BASE_CATEGORIES = (("all", "全部"), ("blog", "博客文章"), ("pages", "其他页面"))
_SEARCH_KEYS = frozenset({"type", "label", "enabled"})
# 与 State/Outbox.search_type 的 64 字符 SQL 契约一致，禁止发现后再截断。
_TYPE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")


class ContentRegistryError(ValueError):
    """分类声明冲突或不合法；拒绝生成可能泄漏原生来源的注册表。"""


@dataclass(frozen=True)
class ContentRegistryEntry:
    """不可变分类描述；snapshot_kind 仅用于分派，不导入未来的适配器。"""

    model_class: type[Page]
    model_label: str
    search_type: str
    label: str
    enabled: bool
    fields: tuple[str, ...]
    snapshot_kind: str


def _epoch(
    entries: tuple[ContentRegistryEntry, ...],
    categories: tuple[tuple[str, str], ...],
) -> str:
    """禁用条目同样参与 epoch，避免启停后复用旧搜索会话。"""
    payload = {
        "registry_schema_version": 1,
        "categories": categories,
        "entries": [
            {
                "model_label": entry.model_label,
                "search_type": entry.search_type,
                "label": entry.label,
                "enabled": entry.enabled,
                "fields": entry.fields,
                "snapshot_kind": entry.snapshot_kind,
            }
            for entry in entries
        ],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ContentRegistry:
    """纳管模型即排除原生来源，不依赖 ES 覆盖情况或 enabled。"""

    def __init__(self) -> None:
        self.entries: tuple[ContentRegistryEntry, ...] = ()
        self.native_excluded_models: tuple[type[Page], ...] = ()
        self.categories: tuple[tuple[str, str], ...] = _BASE_CATEGORIES
        self.epoch: str = _epoch(self.entries, self.categories)
        self._by_model: dict[type[Page], ContentRegistryEntry] = {}

    @classmethod
    def discover(cls, models: Iterable[type[Page]] | None = None) -> ContentRegistry:
        """扫描已安装模型或显式模型列表，全部校验成功后返回新注册表。"""
        # 延迟导入业务类；不引入 adapters、存储 policy 解析或 Page 实例懒读。
        from blog.models import BlogPage
        from external_content.mixins import ExternalContentPageMixin

        candidates = apps.get_models() if models is None else models
        by_model: dict[type[Page], ContentRegistryEntry] = {}
        category_labels = dict(_BASE_CATEGORIES)
        active_types: set[str] = set()

        for model in candidates:
            if not isinstance(model, type) or not issubclass(model, Page) or model._meta.abstract:
                continue
            if model is BlogPage:
                entry = ContentRegistryEntry(
                    model, model._meta.label_lower, "blog", category_labels["blog"],
                    True, ("body",), "blog_v1",
                )
            elif issubclass(model, ExternalContentPageMixin):
                fields = cls._mongo_fields(model)
                if not fields:
                    continue
                declaration = getattr(model, "content_search", {})
                if not isinstance(declaration, dict) or declaration.keys() - _SEARCH_KEYS:
                    raise ContentRegistryError(
                        f"{model._meta.label_lower}: content_search 仅允许 type/label/enabled"
                    )
                search_type = declaration.get("type", "pages")
                if (
                    not isinstance(search_type, str)
                    or not _TYPE_PATTERN.fullmatch(search_type)
                    or search_type in {"all", "blog"}
                ):
                    raise ContentRegistryError(f"{model._meta.label_lower}: 非法或保留的分类键")
                label = declaration.get("label", dict(_BASE_CATEGORIES).get(search_type, search_type))
                enabled = declaration.get("enabled", True)
                if not isinstance(label, str) or not label.strip() or label != label.strip():
                    raise ContentRegistryError(f"{model._meta.label_lower}: 分类标签必须为非空字符串")
                if type(enabled) is not bool:
                    raise ContentRegistryError(f"{model._meta.label_lower}: enabled 必须为 bool")
                entry = ContentRegistryEntry(
                    model, model._meta.label_lower, search_type, label, enabled, fields, "external_v1",
                )
            else:
                continue

            # 禁用声明也必须校验冲突；不得按发现顺序覆盖类别标签。
            if entry.search_type in category_labels and category_labels[entry.search_type] != entry.label:
                raise ContentRegistryError(f"{entry.model_label}: 同一分类键的标签冲突")
            category_labels[entry.search_type] = entry.label
            if entry.enabled:
                active_types.add(entry.search_type)
            by_model[model] = entry

        entries = tuple(sorted(by_model.values(), key=lambda entry: entry.model_label))
        categories = _BASE_CATEGORIES + tuple(
            (key, category_labels[key])
            for key in sorted(active_types - dict(_BASE_CATEGORIES).keys())
        )
        epoch = _epoch(entries, categories)
        # 失败发现不暴露半成品；SQL/Mongo/索引均无写入，也无补偿责任。
        registry = cls()
        registry.entries = entries
        registry._by_model = by_model
        registry.native_excluded_models = tuple(entry.model_class for entry in entries)
        registry.categories = categories
        registry.epoch = epoch
        return registry

    @staticmethod
    def _mongo_fields(model: type[Page]) -> tuple[str, ...]:
        """只读声明和 Django 元数据，不调用会读写基线的策略解析入口。"""
        storage = getattr(model, "content_storage", None)
        if storage is None:
            return ()
        if not isinstance(storage, dict):
            raise ContentRegistryError(f"{model._meta.label_lower}: content_storage 必须为 dict")
        fields = []
        for name, backend in storage.items():
            if backend != "mongo":
                continue
            if not isinstance(name, str) or not name:
                raise ContentRegistryError(f"{model._meta.label_lower}: 非法外置字段名")
            try:
                model._meta.get_field(name)
            except FieldDoesNotExist as exc:
                raise ContentRegistryError(f"{model._meta.label_lower}: 声明的外置字段不存在") from exc
            fields.append(name)
        return tuple(sorted(fields))

    def for_model(self, cls: type[Page]) -> ContentRegistryEntry | None:
        """精确匹配模型；普通 Page 不因相似声明或 ES 延迟被自动纳管。"""
        return self._by_model.get(cls)


def get_content_registry() -> ContentRegistry:
    """从 installed apps 自动发现；不缓存可变模型声明或依赖远端服务。"""
    return ContentRegistry.discover()
