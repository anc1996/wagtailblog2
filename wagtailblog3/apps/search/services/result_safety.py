"""所有来源和会话重放共用的最终展示围栏；不信任 ES 的展示正文。"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from django.utils.html import escape, strip_tags
from django.utils.text import Truncator
from wagtail.models import Revision

from search.models import ContentSearchState
from search.services.published_snapshot import PublishedSnapshotError, PublishedSnapshotV1
from search.services.search_scope import SearchSafetyUnavailable, SearchScope, public_queryset

logger = logging.getLogger(__name__)
FENCE_FIELDS = (
    "page_id", "model_label", "search_type", "snapshot_kind", "content_version",
    "publication_generation", "snapshot_digest", "projection_schema_version", "content_hash",
)


def matches_state(source: Mapping[str, Any], state: Any) -> bool:
    """禁止 bool 冒充版本整数；NULL legacy 不自动获得 v2 准入。"""
    if (
        getattr(state, "searchable", None) is not True
        or getattr(state, "desired_operation", None) != "upsert"
        or type(getattr(state, "projection_schema_version", None)) is not int
        or state.projection_schema_version != 2
    ):
        return False
    for key in FENCE_FIELDS:
        expected = getattr(state, key, None)
        if expected is None or type(source.get(key)) is not type(expected) or source.get(key) != expected:
            return False
    return True


def _display(page: Any, title: str, summary: str, search_type: str, first_published_at: Any = None) -> dict[str, Any]:
    """只复制明确公开的字段；绝不把 specific 对象交给模板读取后来草稿。"""
    url = page.get_url()
    if not isinstance(url, str) or not url or (not url.startswith("/") and not url.startswith(("https://", "http://"))):
        raise SearchSafetyUnavailable("page_url_unavailable")
    return {
        "id": page.pk, "page_id": page.pk, "title": title, "url": url,
        "search_type": search_type, "content_type": page._meta.label_lower,
        "search_description": Truncator(strip_tags(summary)).chars(190),
        "first_published_at": first_published_at,
        "matched_field": "", "highlight_fragments": [], "title_highlight": "",
    }


def native_result(page: Any, revision: Any, search_type: str) -> dict[str, Any] | None:
    """无可证明的正式 Revision 就不展示；不回读 latest_revision 或 Page.title。"""
    if revision is None or not getattr(page, "live_revision_id", None):
        return None
    content = getattr(revision, "content", None)
    if not isinstance(content, dict) or not isinstance(content.get("title"), str):
        return None
    summary = content.get("summary", content.get("intro", ""))
    summary = summary if isinstance(summary, str) else ""
    return _display(page, content["title"], summary, search_type, content.get("first_published_at"))


class ResultValidator:
    """按当前 SQL Scope 批量取页/State/Revision；Mongo 缺损剔除，服务故障关闭。"""

    def __init__(self, scope: SearchScope, registry: Any):
        self.scope = scope
        self.registry = registry
        # 仅用于本次响应结束的再次批量复验，不持久化任何授权结果。
        self.observed: dict[int, Any] = {}

    def __call__(self, candidate: Any) -> dict[str, Any] | None:
        self.observed[candidate.page_id] = candidate
        return self.validate_batch((candidate,)).get(candidate.page_id)

    def validate_batch(self, candidates: Sequence[Any]) -> dict[int, dict[str, Any]]:
        if not candidates:
            return {}
        try:
            pages = {page.pk: page for page in public_queryset(self.scope).filter(
                pk__in=[c.page_id for c in candidates]
            ).specific()}
            managed_ids = [pk for pk, page in pages.items() if self.registry.for_model(type(page))]
            states = {state.page_id: state for state in ContentSearchState.objects.filter(page_id__in=managed_ids)}
            revision_ids = [p.live_revision_id for p in pages.values() if p.live_revision_id]
            revisions = {r.pk: r for r in Revision.objects.filter(pk__in=revision_ids)}
        except Exception:
            raise SearchSafetyUnavailable("sql_visibility_unavailable") from None
        results = {}
        for candidate in candidates:
            page = pages.get(candidate.page_id)
            if page is None:
                continue
            entry = self.registry.for_model(type(page))
            payload = candidate.payload
            if not isinstance(payload, dict):
                continue
            if entry is None:
                if payload.get("source") != "native" or self.scope.search_type not in ("all", "pages"):
                    continue
                revision = revisions.get(page.live_revision_id)
                if revision is not None and (
                    str(revision.object_id) != str(page.pk)
                    or revision.content_type_id != page.content_type_id
                ):
                    continue
                result = native_result(page, revision, "pages")
            else:
                if not entry.enabled or payload.get("source") != "managed":
                    continue
                if self.scope.search_type not in ("all", entry.search_type):
                    continue
                state = states.get(page.pk)
                source = payload.get("document") or {}
                if state is None or not matches_state(source, state) or state.model_label != page._meta.label_lower or state.search_type != entry.search_type:
                    continue
                try:
                    snapshot = PublishedSnapshotV1.from_dict(
                        state.published_snapshot, expected_digest=state.snapshot_digest
                    )
                    snapshot.validate_state(state)
                    # adapter 必须校验完整 Manifest/Blog 身份和哈希；不读 Page.body。
                    from search.services.published_adapters import BlogPublishedAdapter, ExternalPublishedAdapter
                    adapter = BlogPublishedAdapter() if snapshot.snapshot_kind == "blog_v1" else ExternalPublishedAdapter(model=type(page))
                    text = adapter.extract_text(snapshot)
                    if not isinstance(text, str):
                        raise PublishedSnapshotError("projection_text_invalid")
                except PublishedSnapshotError:
                    logger.warning("search_candidate_integrity_rejected page_id=%s", page.pk)
                    continue
                except Exception:
                    raise SearchSafetyUnavailable("published_body_unavailable") from None
                metadata = snapshot.to_dict()["published_metadata"]
                result = _display(page, metadata["title"], metadata["summary"], entry.search_type, metadata["first_published_at"])
                # 使用验证过的文本，避免 ES 旧正文/任意 HTML 绕过正式快照。
                result["search_description"] = Truncator(strip_tags(metadata["summary"] or text)).chars(190)
            if result is not None:
                results[page.pk] = result
        return results
