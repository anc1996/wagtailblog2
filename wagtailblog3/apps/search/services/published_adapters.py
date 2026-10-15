"""正式 Revision 与不可变正文的只读适配；禁止从当前草稿补齐投影。"""

from copy import deepcopy
from datetime import datetime
import hashlib
import re
from typing import Any

from django.apps import apps
from django.utils.html import strip_tags
from wagtail import blocks

from external_content.registry import get_policy_for_version
from external_content.store import ContentVersionStore, compute_canonical_hash
from search.services.published_snapshot import (
    PublishedSnapshotError, PublishedSnapshotV1, canonical_json, positive_int,
    utc_datetime,
)


def _capture_context(
    model: type, page: Any, revision: Any,
    source_page_id: int | None, source_revision_id: int | None,
) -> tuple[Any, dict, dict]:
    """核对真实模型、指定 Revision 与别名来源；不查询 latest_revision。"""
    meta = getattr(model, "_meta", None)
    page_meta = getattr(page, "_meta", None)
    if meta is None and type(page) is model:
        meta = page_meta
    if meta is None or page_meta is None or meta.label_lower != page_meta.label_lower:
        raise PublishedSnapshotError("capture_model_mismatch")
    page_id, revision_id = getattr(page, "pk", None), getattr(revision, "pk", None)
    if not positive_int(page_id) or not positive_int(revision_id):
        raise PublishedSnapshotError("capture_identity_invalid")
    alias_id = getattr(page, "alias_of_id", None)
    if source_page_id is not None or source_revision_id is not None:
        if (
            not positive_int(source_page_id) or not positive_int(source_revision_id)
            or not positive_int(alias_id) or alias_id != source_page_id
            or source_page_id == page_id or source_revision_id != revision_id
        ):
            raise PublishedSnapshotError("capture_source_mismatch")
        owner, local_revision = source_page_id, None
    else:
        if alias_id is not None:
            raise PublishedSnapshotError("capture_source_missing")
        owner, local_revision = page_id, revision_id
    content_type = getattr(revision, "content_type", None)
    label = f"{getattr(content_type, 'app_label', '')}.{getattr(content_type, 'model', '')}"
    object_id = getattr(revision, "object_id", None)
    if label != meta.label_lower or (
        type(object_id) not in (int, str) or str(object_id) != str(owner)
    ):
        raise PublishedSnapshotError("capture_revision_mismatch")
    content = getattr(revision, "content", None)
    if not isinstance(content, dict):
        raise PublishedSnapshotError("capture_revision_content_invalid")
    for key in ("pk", "id"):
        if key in content and (
            type(content[key]) not in (int, str) or str(content[key]) != str(owner)
        ):
            raise PublishedSnapshotError("capture_revision_content_identity_mismatch")
    # Revision JSON 是元数据权威；不调用 as_object，避免正文恢复钩子和草稿回退。
    return meta, deepcopy(content), {
        "published_revision_id": local_revision,
        "source_page_id": source_page_id,
        "source_revision_id": source_revision_id,
    }


def _snapshot(
    page: Any, meta: Any, ref: dict, metadata: dict, body_ref: dict,
    kind: str, publication_generation: int, search_type: str,
) -> PublishedSnapshotV1:
    return PublishedSnapshotV1.from_dict({
        "snapshot_schema_version": 1, "snapshot_kind": kind,
        "page_id": page.pk, "model_label": meta.label_lower, "search_type": search_type,
        "publication_generation": publication_generation, "publication_ref": ref,
        "published_metadata": metadata, "body_ref": body_ref,
        "projection_schema_version": 2,
    })


def _block_text(block: blocks.Block, value: Any, name: str = "") -> list[str]:
    """按声明结构遍历原始副本，避免 StructValue 字符串化或媒体对象惰性取值。"""
    if isinstance(block, blocks.StructBlock):
        return [
            text for key, child in block.child_blocks.items() if key in value
            for text in _block_text(child, value[key], key)
        ]
    if isinstance(block, blocks.StreamBlock):
        return [
            text for item in value
            for text in _block_text(block.child_blocks[item["type"]], item["value"], item["type"])
        ]
    if isinstance(block, blocks.ListBlock):
        return [text for item in value for text in _block_text(block.child_block, item["value"], name)]
    if isinstance(block, blocks.ChoiceBlock):
        # 选择值本身可能是正文文本；只索引原始值，不拼入显示标签或关联对象属性。
        if isinstance(value, str):
            return [value] if value else []
        if value is None or type(value) in (int, float, bool):
            return []
        raise PublishedSnapshotError("projection_block_value_invalid")
    if isinstance(block, (
        blocks.CharBlock, blocks.TextBlock, blocks.RichTextBlock, blocks.RawHTMLBlock,
        blocks.URLBlock, blocks.EmailBlock, blocks.RegexBlock,
    )):
        if value is None:
            return []
        if not isinstance(value, str):
            raise PublishedSnapshotError("projection_block_value_invalid")
        if name == "markdown_block":
            text = re.sub(r"[#*`>\-_[\](){}|]", " ", value)
        elif isinstance(block, (blocks.RichTextBlock, blocks.RawHTMLBlock)):
            text = strip_tags(value)
        else:
            text = value
        return [text] if text else []
    # 已声明的日期/数字/媒体块不是正文文本；不读取关联对象或私密展示属性。
    if isinstance(block, (
        blocks.ChooserBlock, blocks.BooleanBlock, blocks.IntegerBlock,
        blocks.FloatBlock, blocks.DecimalBlock, blocks.DateBlock, blocks.TimeBlock,
        blocks.DateTimeBlock,
    )):
        return []
    raise PublishedSnapshotError("projection_block_unsupported")


class ExternalPublishedAdapter:
    """所有声明外置字段都参与全文；store 只在真正提取时按需创建。"""

    def __init__(self, model: type, store: Any = None) -> None:
        self.model = model
        self.store = store
        self._meta = getattr(model, "_meta", None)

    def _policy(self, manifest: dict) -> Any:
        try:
            policy = get_policy_for_version(self.model, manifest["content_storage_schema"])
            if (
                self._meta is None or manifest["model_label"] != self._meta.label_lower
                or policy.model_label != self._meta.label_lower
                or type(policy.schema_version) is not int
                or policy.schema_version != manifest["content_storage_schema"]
                or set(policy.external_fields) != set(manifest["fields"])
            ):
                raise PublishedSnapshotError("capture_policy_mismatch")
            for name, config in policy.external_fields.items():
                entry = manifest["fields"][name]
                if (
                    config.field_name != name or config.backend != "mongo"
                    or config.codec.codec_id != entry["codec"]
                    or type(config.codec.schema_version) is not int
                    or config.codec.schema_version != entry["schema_version"]
                ):
                    raise PublishedSnapshotError("capture_codec_mismatch")
                self._meta.get_field(name)
            return policy
        except PublishedSnapshotError:
            raise
        except Exception:
            raise PublishedSnapshotError("capture_policy_unavailable") from None

    def capture(
        self, page: Any, revision: Any, manifest: dict, publication_generation: int,
        first_published_at: datetime | str, search_type: str,
        *, source_page_id: int | None = None, source_revision_id: int | None = None,
    ) -> PublishedSnapshotV1:
        meta, content, ref = _capture_context(
            self.model, page, revision, source_page_id, source_revision_id,
        )
        if canonical_json(content.get("_external_content_manifest")) != canonical_json(manifest):
            raise PublishedSnapshotError("capture_manifest_revision_mismatch")
        snapshot = _snapshot(page, meta, ref, {
            "title": content.get("title"), "summary": content.get("summary", ""),
            "first_published_at": utc_datetime(first_published_at),
        }, manifest, "external_v1", publication_generation, search_type)
        self._meta = meta
        self._policy(snapshot.to_dict()["body_ref"])
        return snapshot

    def extract_text(self, snapshot: PublishedSnapshotV1) -> str:
        if not isinstance(snapshot, PublishedSnapshotV1) or snapshot.snapshot_kind != "external_v1":
            raise PublishedSnapshotError("projection_snapshot_kind_mismatch")
        manifest = snapshot.to_dict()["body_ref"]
        policy = self._policy(manifest)
        try:
            store = self.store if self.store is not None else ContentVersionStore()
            field_texts = []
            # 保持策略声明顺序，并明确隔开字段；canonical JSON 的键排序不改变正文顺序。
            for name, config in policy.external_fields.items():
                entry = manifest["fields"][name]
                identity = {
                    "model_label": manifest["model_label"], "page_id": manifest["page_id"],
                    "field_name": name, "codec": entry["codec"],
                    "schema_version": entry["schema_version"], "payload_sha256": entry["payload_sha256"],
                }
                document = store.get_version(entry["version_id"], expected_identity=identity)
                if not isinstance(document, dict) or document.get("version_id") != entry["version_id"]:
                    raise PublishedSnapshotError("projection_version_mismatch")
                if any(
                    type(document.get(key)) is not type(value) or document.get(key) != value
                    for key, value in identity.items()
                ):
                    raise PublishedSnapshotError("projection_external_identity_mismatch")
                payload = deepcopy(document["payload"])
                if compute_canonical_hash(payload) != identity["payload_sha256"]:
                    raise PublishedSnapshotError("projection_external_hash_mismatch")
                field = self._meta.get_field(name)
                # codec 做严格嵌套无损往返，不能让 Wagtail 静默丢弃未知块。
                config.codec.decode(payload, field=field)
                field_texts.append("\n".join(_block_text(field.stream_block, payload)))
            return "\n\n".join(field_texts) if any(field_texts) else ""
        except PublishedSnapshotError:
            raise
        except Exception:
            raise PublishedSnapshotError("projection_external_body_unavailable") from None


class BlogPublishedAdapter:
    """保留 Blog 原不可变集合和全文提取契约，不迁移到通用外置协议。"""

    def __init__(self, model: type, store: Any = None) -> None:
        self.model, self.store = model, store

    def capture(
        self, page: Any, revision: Any, publication_generation: int,
        first_published_at: datetime | str, search_type: str, manifest: dict | None = None,
        *, source_page_id: int | None = None, source_revision_id: int | None = None,
    ) -> PublishedSnapshotV1:
        meta, content, ref = _capture_context(
            self.model, page, revision, source_page_id, source_revision_id,
        )
        if manifest is not None or meta.label_lower != "blog.blogpage":
            raise PublishedSnapshotError("capture_blog_identity_invalid")
        if not {"intro", "date", "categories", "tagged_items"} <= content.keys():
            raise PublishedSnapshotError("capture_blog_metadata_missing")
        intro = content.get("intro")
        if not isinstance(intro, str):
            raise PublishedSnapshotError("capture_blog_metadata_invalid")
        tagged_items = content["tagged_items"]
        if not isinstance(tagged_items, list) or any(
            not isinstance(item, dict) or not positive_int(item.get("tag")) for item in tagged_items
        ):
            raise PublishedSnapshotError("capture_blog_tags_invalid")
        return _snapshot(page, meta, ref, {
            "title": content.get("title"), "summary": strip_tags(intro), "intro": strip_tags(intro),
            "date": content["date"], "tag_ids": sorted({item["tag"] for item in tagged_items}),
            "category_ids": content["categories"],
            "first_published_at": utc_datetime(first_published_at),
        }, {
            "mongo_content_id": content.get("mongo_content_id"),
            "body_version_id": content.get("mongo_body_version_id"),
            "body_sha256": content.get("body_sha256"),
            "body_schema_version": content.get("body_schema_version"),
        }, "blog_v1", publication_generation, search_type)

    def extract_text(self, snapshot: PublishedSnapshotV1) -> str:
        if (
            not isinstance(snapshot, PublishedSnapshotV1) or snapshot.snapshot_kind != "blog_v1"
            or self.model._meta.label_lower != snapshot.to_dict()["model_label"]
        ):
            raise PublishedSnapshotError("projection_snapshot_kind_mismatch")
        data = snapshot.to_dict()
        pointer = data["body_ref"]
        owner = data["publication_ref"]["source_page_id"] or data["page_id"]
        identity = {
            "aggregate_type": "blog_page", "aggregate_id": str(owner),
            "body_version_id": pointer["body_version_id"], "body_sha256": pointer["body_sha256"],
            "body_schema_version": pointer["body_schema_version"],
        }
        try:
            if self.store is not None:
                document = self.store.get_content_body_version(
                    "blog_page", owner, pointer["body_version_id"],
                    pointer["body_sha256"], pointer["body_schema_version"],
                )
            else:
                # MongoManager() 会自动建索引；只读消费者必须绕开其初始化写入。
                from external_content.store import get_mongo_client, get_mongo_settings, get_read_concern
                collection = get_mongo_client()[get_mongo_settings()["NAME"]].get_collection(
                    "content_body_versions", read_concern=get_read_concern(),
                )
                document = collection.find_one(identity)
            if not isinstance(document, dict) or any(
                type(document.get(key)) is not type(value) or document.get(key) != value
                for key, value in identity.items()
            ):
                raise PublishedSnapshotError("projection_blog_identity_mismatch")
            body = document.get("body")
            if not isinstance(body, list):
                raise PublishedSnapshotError("projection_blog_body_invalid")
            from wagtailblog3.mongo import MongoManager
            digest = hashlib.sha256(MongoManager._canonical_body_json(None, body).encode("utf-8")).hexdigest()
            if digest != pointer["body_sha256"]:
                raise PublishedSnapshotError("projection_blog_hash_mismatch")
            known = self.model._meta.get_field("body").stream_block.child_blocks
            for item in body:
                if (
                    not isinstance(item, dict) or not isinstance(item.get("type"), str)
                    or item["type"] not in known or "value" not in item
                ):
                    raise PublishedSnapshotError("projection_blog_block_invalid")
            # 明确提供不可变正文，原方法不会执行 get_content_from_mongodb 的兼容回退。
            return self.model.get_full_text_for_search(None, content=deepcopy(document))
        except PublishedSnapshotError:
            raise
        except Exception:
            raise PublishedSnapshotError("projection_blog_body_unavailable") from None


def get_published_adapter(model_or_snapshot: type | PublishedSnapshotV1) -> BlogPublishedAdapter | ExternalPublishedAdapter:
    """公共路由只接受已知受管模型/快照；工厂不建立连接或登记策略。"""
    snapshot = model_or_snapshot if isinstance(model_or_snapshot, PublishedSnapshotV1) else None
    try:
        model = apps.get_model(snapshot.to_dict()["model_label"]) if snapshot else model_or_snapshot
        from blog.models import BlogPage
        from external_content.mixins import ExternalContentPageMixin
        if model is BlogPage:
            adapter = BlogPublishedAdapter(model)
            kind = "blog_v1"
        elif isinstance(model, type) and issubclass(model, ExternalContentPageMixin):
            adapter = ExternalPublishedAdapter(model)
            kind = "external_v1"
        else:
            raise PublishedSnapshotError("projection_model_unsupported")
        if snapshot and snapshot.snapshot_kind != kind:
            raise PublishedSnapshotError("projection_snapshot_kind_mismatch")
        return adapter
    except PublishedSnapshotError:
        raise
    except Exception:
        raise PublishedSnapshotError("projection_model_unsupported") from None
