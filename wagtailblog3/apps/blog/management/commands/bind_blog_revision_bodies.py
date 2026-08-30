"""只读审计可安全补充正式正文指针的 Wagtail live Revision。

该命令只扫描当前仍公开的 BlogPage 及其 live Revision。它绝不创建 Revision、
不修改 Revision JSON、不改变 Page 指针，也不触发 Mongo、Outbox 或搜索写入；
输出的 manifest 仅供未来经备份和独立授权后的受控 apply 实现使用。
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any

import pymongo
from django.conf import settings
from django.core.management.base import BaseCommand

from blog.models import BlogPage, BlogPublicationState
from wagtail.models import Revision


def _revision_content(revision: Revision) -> dict[str, object] | None:
    """解析 Revision JSON；格式异常时返回空，防止审计把损坏历史误列为候选。"""

    content: object = revision.content
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    return content if isinstance(content, dict) else None


def _manifest_sha256(entries: list[dict[str, object]]) -> str:
    """计算稳定 manifest 哈希；内容不含正文或其它敏感字段。"""

    payload = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Command(BaseCommand):
    """生成可绑定 live Revision 的只读候选清单，不提供写入开关。"""

    help = "只读审计 BlogPage live Revision 的 Mongo 正文指针绑定候选"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--after-page-id", type=int, default=0, help="跳过不大于该主键的页面")
        parser.add_argument("--limit", type=int, default=1000, help="扫描上限，范围 1--5000")
        parser.add_argument("--dry-run", action="store_true", help="显式标注只读预演；命令始终只读")

    def handle(self, *args: object, **options: object) -> None:
        limit = max(1, min(int(options.get("limit") or 1000), 5000))
        after_page_id = max(0, int(options.get("after_page_id") or 0))
        pages = list(
            BlogPage.objects.filter(pk__gt=after_page_id, live=True, live_revision_id__isnull=False)
            .order_by("pk")
            .only("pk", "live_revision_id")[:limit]
        )
        report = self._build_report(pages, after_page_id)
        self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))

    def _build_report(self, pages: list[BlogPage], after_page_id: int) -> dict[str, object]:
        """按页面稳定顺序分类候选；Mongo 不可用时保留拒绝记录而不降级猜测。"""

        page_ids = [int(page.pk) for page in pages]
        states = {
            state.page_id: state
            for state in BlogPublicationState.objects.filter(page_id__in=page_ids).only(
                "page_id", "published_body_version_id", "published_body_sha256", "published_body_schema_version"
            )
        }
        revisions = {
            int(revision.pk): revision
            for revision in Revision.objects.filter(pk__in=[page.live_revision_id for page in pages]).only(
                "pk", "content"
            )
        }
        references = {
            page_id: (
                state.published_body_version_id,
                state.published_body_sha256,
                state.published_body_schema_version,
            )
            for page_id, state in states.items()
            if state.published_body_version_id
            and state.published_body_sha256
            and isinstance(state.published_body_schema_version, int)
            and state.published_body_schema_version >= 1
        }
        versions: set[tuple[str, str, int]] = set()
        mongo_error: str | None = None
        if references:
            try:
                versions = self._read_versions(references)
            except Exception as exc:  # 只读审计不能因外部库短暂不可用而输出虚假候选。
                mongo_error = type(exc).__name__
        entries: list[dict[str, object]] = []
        reason_counts: dict[str, int] = defaultdict(int)
        for page in pages:
            page_id = int(page.pk)
            revision = revisions.get(int(page.live_revision_id))
            state = states.get(page_id)
            reasons: list[str] = []
            content = _revision_content(revision) if revision else None
            if revision is None:
                reasons.append("live_revision_missing")
            elif content is None:
                reasons.append("revision_content_invalid")
            else:
                existing = (
                    content.get("mongo_body_version_id"),
                    content.get("body_sha256"),
                    content.get("body_schema_version"),
                )
                if any(value is not None for value in existing):
                    if state is not None and existing == references.get(page_id):
                        reasons.append("already_bound")
                    else:
                        reasons.append("revision_pointer_conflict")
            reference = references.get(page_id)
            if state is None:
                reasons.append("state_missing")
            elif reference is None:
                reasons.append("published_pointer_invalid")
            elif mongo_error is not None:
                reasons.append("mongo_unavailable")
            elif reference not in versions:
                reasons.append("published_body_version_unavailable")
            eligible = not reasons
            if reasons == ["already_bound"]:
                eligible = False
            for reason in reasons:
                reason_counts[reason] += 1
            entry: dict[str, object] = {
                "page_id": page_id,
                "revision_id": int(page.live_revision_id),
                "eligible": eligible,
                "refusal_reasons": reasons,
            }
            if eligible and reference is not None:
                entry["suggested_pointer"] = {
                    "mongo_body_version_id": reference[0],
                    "body_sha256": reference[1],
                    "body_schema_version": reference[2],
                }
            entries.append(entry)
        manifest = [entry for entry in entries if entry["eligible"]]
        return {
            "read_only": True,
            "dry_run": True,
            "after_page_id": after_page_id,
            "next_after_page_id": page_ids[-1] if page_ids else after_page_id,
            "scanned": len(entries),
            "candidate_count": len(manifest),
            "refusal_counts": dict(sorted(reason_counts.items())),
            "mongo_error": mongo_error,
            "manifest_sha256": _manifest_sha256(manifest),
            "manifest": manifest,
            "rows": entries,
        }

    def _read_versions(self, references: dict[int, tuple[str, str, int]]) -> set[tuple[str, str, int]]:
        """只读检查 State 三元组对应的 Mongo 版本；不实例化会建索引的 MongoManager。"""

        mongo_settings = settings.MONGO_DB
        client = pymongo.MongoClient(
            host=mongo_settings["HOST"],
            port=mongo_settings["PORT"],
            serverSelectionTimeoutMS=5000,
        )
        try:
            conditions = [
                {
                    "aggregate_type": "blog_page",
                    "aggregate_id": str(page_id),
                    "body_version_id": version_id,
                    "body_sha256": body_sha256,
                    "body_schema_version": schema_version,
                }
                for page_id, (version_id, body_sha256, schema_version) in references.items()
            ]
            documents = client[mongo_settings["NAME"]]["content_body_versions"].find(
                {"$or": conditions},
                {"body_version_id": 1, "body_sha256": 1, "body_schema_version": 1},
            )
            return {
                (str(document["body_version_id"]), str(document["body_sha256"]), int(document["body_schema_version"]))
                for document in documents
            }
        finally:
            client.close()
