"""只读检查旧 BlogPage，并生成登记不可变正文版本前的诊断报告。

命令默认永远是 dry-run；``--apply`` 当前明确拒绝，避免在尚未完成迁移门禁前
误写 MySQL、MongoDB、Revision 或搜索 Outbox。Mongo 读取直接使用只读查询，
不实例化 ``MongoManager``，从而不会触发连接时的索引创建副作用。
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from typing import Any

import pymongo
from bson import json_util
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from blog.models import BlogPage, BlogPublicationState
from wagtail.models import Revision

logger = logging.getLogger(__name__)


def _revision_content(revision: Revision) -> dict[str, Any] | None:
	"""解析 Revision.content，兼容 Wagtail 的 JSON 字符串和映射值。"""
	content: Any = revision.content
	if isinstance(content, str):
		try:
			content = json.loads(content)
		except (TypeError, ValueError, json.JSONDecodeError):
			return None
	return content if isinstance(content, dict) else None


def _body_sha256(body: list[Any]) -> str | None:
	"""按 Mongo 正文版本采用的规范化 JSON 计算哈希，不改变原始正文。"""
	try:
		payload = json.dumps(
			body,
			sort_keys=True,
			ensure_ascii=False,
			separators=(",", ":"),
			default=json_util.default,
		)
	except (TypeError, ValueError):
		return None
	return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Command(BaseCommand):
	"""输出单页或批量旧正文登记诊断；本命令不提供写入实现。"""

	help = "只读检查旧 BlogPage 的 Mongo 正文、Revision 和不可变版本登记状态"

	def add_arguments(self, parser):
		parser.add_argument("--expected-hash", help="旧 Mongo 正文规范化 JSON 的 SHA-256")
		parser.add_argument("--page-id", type=int, help="仅检查一个 BlogPage 主键")
		parser.add_argument("--after-page-id", type=int, default=0, help="批量检查时跳过不大于此主键的页面")
		parser.add_argument("--limit", type=int, default=1000, help="最多检查页面数（1-5000）")
		parser.add_argument("--dry-run", action="store_true", help="只读模式（默认行为）")
		parser.add_argument("--apply", action="store_true", help="尝试写入；当前版本明确不支持")

	def handle(self, *args, **options):
		if options.get("apply"):
			return self._handle_apply(options)
		limit = max(1, min(int(options.get("limit") or 1000), 5000))
		page_id = options.get("page_id")
		if page_id is not None:
			pages = list(
				BlogPage.objects.filter(pk=page_id).only(
					"pk", "title", "live", "live_revision_id", "mongo_content_id"
				)
			)
			if not pages:
				raise CommandError(f"未找到 BlogPage id={page_id}")
		else:
			after_page_id = max(0, int(options.get("after_page_id") or 0))
			pages = list(
				BlogPage.objects.filter(pk__gt=after_page_id)
				.order_by("pk")
				.only("pk", "title", "live", "live_revision_id", "mongo_content_id")[:limit]
			)
		report = self._build_report(pages)
		report["read_only"] = True
		report["dry_run"] = True
		self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))

	def _handle_apply(self, options: dict[str, Any]) -> None:
		"""受控登记单篇旧正文；校验哈希后写入 Mongo 版本与 MySQL State。"""
		page_id = options.get("page_id")
		expected_hash = options.get("expected_hash")
		if page_id is None or not isinstance(expected_hash, str) or len(expected_hash) != 64 or any(char not in "0123456789abcdefABCDEF" for char in expected_hash):
			raise CommandError("--apply 必须同时提供有效的 --page-id 和 --expected-hash")
		page = BlogPage.objects.filter(pk=page_id).only("pk", "live").first()
		if page is None:
			raise CommandError(f"未找到 BlogPage id={page_id}")
		legacy_docs, _ = self._read_mongo([int(page.pk)])
		body = (legacy_docs.get(int(page.pk)) or {}).get("body")
		if not isinstance(body, list) or _body_sha256(body) != expected_hash:
			raise CommandError("旧正文缺失或哈希不匹配，已拒绝登记")
		try:
			from wagtailblog3.mongo import MongoManager
			version = MongoManager().save_content_body_version("blog_page", page.pk, body)
		except Exception as exc:
			logger.error("legacy_registration_mongo_failed page_id=%s error=%s", page.pk, type(exc).__name__)
			raise CommandError("Mongo 正文版本写入失败，未创建 State") from exc
		try:
			with transaction.atomic():
				locked = BlogPage.objects.select_for_update().get(pk=page.pk)
				state = BlogPublicationState.objects.select_for_update().filter(page_id=locked.pk).first()
				version_id = str(version["body_version_id"])
				if state is not None and (state.draft_body_version_id == version_id or state.published_body_version_id == version_id):
					result = {"status": "already_registered", "page_id": locked.pk, **version}
				else:
					if state is None:
						state = BlogPublicationState(page_id=locked.pk)
					state.draft_body_version_id = version_id
					state.draft_body_sha256 = str(version["body_sha256"])
					state.draft_body_schema_version = int(version["body_schema_version"])
					if locked.live:
						state.published_body_version_id = version_id
						state.published_body_sha256 = str(version["body_sha256"])
						state.published_body_schema_version = int(version["body_schema_version"])
						state.publication_generation = max(1, int(state.publication_generation or 0))
					state.save()
					if locked.live:
						from search.services.outbox import ContentSearchOutboxService
						ContentSearchOutboxService.record_publication(locked)
					result = {"status": "registered", "page_id": locked.pk, **version}
		except Exception as exc:
			logger.error(
				"legacy_registration_mysql_failed page_id=%s body_version_id=%s error=%s",
				page.pk,
				version.get("body_version_id"),
				type(exc).__name__,
			)
			raise CommandError(
				"MySQL 登记失败，事务已回滚；Mongo 版本保留，可按相同 hash 重试"
			) from exc
		self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))

	def _build_report(self, pages: list[BlogPage]) -> dict[str, Any]:
		"""读取页面关联数据并按登记风险分类，整个过程只执行数据库查询。"""
		page_ids = [int(page.pk) for page in pages]
		states = {
			state.page_id: state
			for state in BlogPublicationState.objects.filter(page_id__in=page_ids)
		}
		revision_ids = [page.live_revision_id for page in pages if page.live_revision_id]
		revisions = {
			revision.pk: revision
			for revision in Revision.objects.filter(pk__in=revision_ids).only("pk", "content", "created_at")
		}
		latest_revisions: dict[int, Revision] = {}
		for revision in Revision.objects.filter(object_id__in=page_ids).only("pk", "object_id", "content", "created_at").order_by("object_id", "-created_at", "-pk"):
			latest_revisions.setdefault(int(revision.object_id), revision)

		legacy_docs: dict[int, dict[str, Any]] = {}
		versions: dict[int, list[dict[str, Any]]] = defaultdict(list)
		mongo_error: str | None = None
		try:
			legacy_docs, versions = self._read_mongo(page_ids)
		except Exception as exc:  # 只读诊断必须保留页面清单，即使 Mongo 暂时不可用
			mongo_error = type(exc).__name__

		rows: list[dict[str, Any]] = []
		category_counts: dict[str, int] = defaultdict(int)
		for page in pages:
			page_id = int(page.pk)
			doc = legacy_docs.get(page_id)
			latest = latest_revisions.get(page_id)
			live_revision = revisions.get(page.live_revision_id) if page.live_revision_id else None
			live_data = _revision_content(live_revision) if live_revision else None
			latest_data = _revision_content(latest) if latest else None
			body = doc.get("body") if doc else None
			body_is_list = isinstance(body, list)
			version_list = versions.get(page_id, [])
			latest_version = version_list[0] if version_list else None
			categories: list[str] = []
			if page_id not in states:
				categories.append("state_missing")
			if doc is None:
				categories.append("mongo_content_missing" if mongo_error is None else "mongo_unavailable")
			if body_is_list and not body:
				categories.append("mongo_body_empty")
			if latest is not None and latest.pk != page.live_revision_id:
				categories.append("draft_revision_present")
			if page.live and live_revision is None:
				categories.append("live_revision_missing")
			live_pointer = live_data.get("mongo_body_version_id") if live_data else None
			if page.live and live_revision is not None and not live_pointer:
				categories.append("legacy_revision_unbound")
			if latest_version is not None:
				categories.append("modern_body_version_present")
			for category in categories:
				category_counts[category] += 1
			rows.append(
				{
					"page_id": page_id,
					"title": page.title,
					"live": bool(page.live),
					"publication_status": "published" if page.live else "draft_or_unpublished",
					"state_exists": page_id in states,
					"mongo_content_id": getattr(page, "mongo_content_id", None),
					"mongo_content_exists": doc is not None,
					"mongo_body_blocks": len(body) if body_is_list else None,
					"mongo_body_sha256": _body_sha256(body) if body_is_list else None,
					"has_draft_revision": latest is not None and latest.pk != page.live_revision_id,
					"latest_revision_id": latest.pk if latest else None,
					"live_revision_id": page.live_revision_id,
					"live_revision_body_version_id": live_pointer,
					"latest_revision_body_version_id": latest_data.get("mongo_body_version_id") if latest_data else None,
					"modern_body_version_id": latest_version.get("body_version_id") if latest_version else None,
					"modern_body_version_count": len(version_list),
					"categories": categories,
				}
			)
		return {
			"scanned": len(rows),
			"category_counts": dict(sorted(category_counts.items())),
			"pages": rows,
			"mongo_error": mongo_error,
		}

	def _read_mongo(self, page_ids: list[int]) -> tuple[dict[int, dict[str, Any]], dict[int, list[dict[str, Any]]]]:
		"""以只读游标查询旧正文和不可变版本，不创建索引、不更新文档。"""
		mongo_settings = settings.MONGO_DB
		client = pymongo.MongoClient(
			host=mongo_settings["HOST"],
			port=mongo_settings["PORT"],
			serverSelectionTimeoutMS=5000,
		)
		try:
			db = client[mongo_settings["NAME"]]
			legacy = {
				int(doc["page_id"]): doc
				for doc in db["blog_content"].find(
					{"page_id": {"$in": page_ids}},
					{"page_id": 1, "body": 1, "_id": 1},
				)
				if doc.get("page_id") is not None
			}
			versions: dict[int, list[dict[str, Any]]] = defaultdict(list)
			for doc in db["content_body_versions"].find(
				{"aggregate_type": "blog_page", "aggregate_id": {"$in": [str(value) for value in page_ids]}},
				{"aggregate_id": 1, "body_version_id": 1, "body_sha256": 1, "body_schema_version": 1, "created_at": 1},
			).sort("created_at", pymongo.DESCENDING):
				try:
					versions[int(doc["aggregate_id"])].append(doc)
				except (KeyError, TypeError, ValueError):
					continue
			return legacy, versions
		finally:
			client.close()
