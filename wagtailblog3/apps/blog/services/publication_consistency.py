"""BlogPage 发布状态的只读对账服务。

本模块只比较 MySQL 中的页面、Revision、发布状态和搜索状态，并按需读取 Mongo
不可变正文版本。对账结果只包含计数和有限主键样本，不执行自动修复或外部写入。
"""

from __future__ import annotations

from collections import Counter
import json
from typing import Any

from blog.models import BlogPage, BlogPublicationState
from wagtail.models import Revision
from wagtailblog3.mongo import MongoRevisionReadError
from search.models import ContentSearchOutbox, ContentSearchState


def _sample(samples: dict[str, list[int]], key: str, page_id: int, limit: int) -> None:
	if len(samples[key]) < limit:
		samples[key].append(page_id)


def _revision_content(revision: Revision) -> dict[str, Any] | None:
	"""解析 Wagtail 8.0 Revision.content，兼容 TextField 字符串和映射值。"""
	content = revision.content
	if isinstance(content, str):
		try:
			content = json.loads(content)
		except (TypeError, ValueError, json.JSONDecodeError):
			return None
	return content if isinstance(content, dict) else None


def check_blog_publication_consistency(
	after_page_id: int = 0,
	limit: int = 1000,
	sample_limit: int = 20,
	*,
	check_mongo: bool = True,
	upper_bound_page_id: int | None = None,
) -> dict[str, Any]:
	"""分批只读核对 BlogPage 正式指针、Revision、搜索状态和 Mongo 版本。

	参数：``after_page_id`` 为游标，``limit`` 限制本批页面数，``sample_limit`` 限制
	每类异常样本数；``check_mongo`` 控制是否读取 Mongo 版本。返回值不含正文。
	异常：Mongo 读取失败只计入 ``mongo_unavailable``，不会中断其他页面核对。
	副作用：仅执行 MySQL/Mongo 读取，不写入页面、状态、Outbox、Mongo 或搜索索引。
	"""
	limit = max(1, min(int(limit), 5000))
	sample_limit = max(1, min(int(sample_limit), 100))
	after_page_id = max(0, int(after_page_id))
	# BlogPage 是游标主表；这样完全缺失 State 的页面也不会被扫描遗漏。
	page_filter = {"pk__gt": after_page_id}
	if upper_bound_page_id is not None:
		page_filter["pk__lte"] = max(after_page_id, int(upper_bound_page_id))
	pages = list(
		BlogPage.objects.filter(**page_filter)
		.order_by("pk")
		.only("pk", "live", "live_revision_id")[:limit]
	)
	page_ids = [page.pk for page in pages]
	max_page_id = page_ids[-1] if page_ids else None
	state_filter = {"page_id__gt": after_page_id}
	if max_page_id is not None:
		state_filter["page_id__lte"] = max_page_id
	elif upper_bound_page_id is not None:
		state_filter["page_id__lte"] = upper_bound_page_id
	states = list(BlogPublicationState.objects.filter(**state_filter))
	states_by_page = {state.page_id: state for state in states}
	# 保留游离 State 检查；当本批没有页面时仍读取有限的孤儿行，避免静默遗漏。
	if max_page_id is None:
		fallback_state_filter = {"page_id__gt": after_page_id}
		if upper_bound_page_id is not None:
			# 周期扫描必须遵守启动时的 high-water，避免把新写入的孤儿 State 混入本轮。
			fallback_state_filter["page_id__lte"] = int(upper_bound_page_id)
		states = list(
			BlogPublicationState.objects.filter(**fallback_state_filter)
			.order_by("page_id")[:limit]
		)
		states_by_page = {state.page_id: state for state in states}
	state_page_ids = set(states_by_page)
	page_ids_for_related = page_ids + [page_id for page_id in state_page_ids if page_id not in page_ids]
	search_states = {
		state.page_id: state
		for state in ContentSearchState.objects.filter(page_id__in=page_ids_for_related)
	}
	latest_events = {}
	for event in ContentSearchOutbox.objects.filter(page_id__in=page_ids_for_related).order_by(
		"page_id", "-content_version", "-pk"
	):
		latest_events.setdefault(event.page_id, event)
	revisions = {
		revision.pk: revision
		for revision in Revision.objects.filter(
			pk__in=[page.live_revision_id for page in pages if page.live_revision_id]
		).only("pk", "content")
	}

	counts = Counter()
	samples = {
		"page_missing": [],
		"state_missing": [],
		"live_pointer_missing": [],
		"revision_missing": [],
		"revision_body_mismatch": [],
		"mongo_missing": [],
		"mongo_unavailable": [],
		"search_state_missing": [],
		"search_identity_mismatch": [],
		"outbox_missing": [],
		"outbox_identity_mismatch": [],
	}

	page_id_set = set(page_ids)
	for page in pages:
		state = states_by_page.get(page.pk)
		if state is None:
			counts["state_missing"] += 1
			_sample(samples, "state_missing", page.pk, sample_limit)
			continue
		if page.live and not state.published_body_version_id:
			counts["live_pointer_missing"] += 1
			_sample(samples, "live_pointer_missing", state.page_id, sample_limit)

		if page.live_revision_id:
			revision = revisions.get(page.live_revision_id)
			if revision is None:
				counts["revision_missing"] += 1
				_sample(samples, "revision_missing", state.page_id, sample_limit)
			else:
				revision_content = _revision_content(revision)
				revision_body_id = (
					revision_content.get("mongo_body_version_id")
					if revision_content is not None
					else None
				)
				if state.published_body_version_id and revision_body_id != state.published_body_version_id:
					counts["revision_body_mismatch"] += 1
					_sample(samples, "revision_body_mismatch", state.page_id, sample_limit)

		if check_mongo and state.published_body_version_id:
			try:
				from blog.models import MongoManager

				document = MongoManager().get_content_body_version(
					"blog_page",
					state.page_id,
					state.published_body_version_id,
					state.published_body_sha256 or "",
					state.published_body_schema_version or 0,
				)
				if not isinstance(document, dict) or not isinstance(document.get("body"), list):
					counts["mongo_missing"] += 1
					_sample(samples, "mongo_missing", state.page_id, sample_limit)
			except MongoRevisionReadError:
				counts["mongo_unavailable"] += 1
				_sample(samples, "mongo_unavailable", state.page_id, sample_limit)

		search_state = search_states.get(state.page_id)
		if search_state is None:
			counts["search_state_missing"] += 1
			_sample(samples, "search_state_missing", state.page_id, sample_limit)
		else:
			if (
				state.publication_generation > 0
				and search_state.publication_generation != state.publication_generation
			) or (
				state.published_body_version_id
				and search_state.body_version_id != state.published_body_version_id
			):
				counts["search_identity_mismatch"] += 1
				_sample(samples, "search_identity_mismatch", state.page_id, sample_limit)

		event = latest_events.get(state.page_id)
		if event is None:
			counts["outbox_missing"] += 1
			_sample(samples, "outbox_missing", state.page_id, sample_limit)
		elif (
			state.publication_generation > 0
			and event.publication_generation != state.publication_generation
		) or (
			state.published_body_version_id
			and event.body_version_id != state.published_body_version_id
		):
			counts["outbox_identity_mismatch"] += 1
			_sample(samples, "outbox_identity_mismatch", state.page_id, sample_limit)

	# State 行可能因历史删除或人工残留而没有对应页面，作为独立异常保留。
	for state in states:
		if state.page_id not in page_id_set:
			counts["page_missing"] += 1
			_sample(samples, "page_missing", state.page_id, sample_limit)

	return {
		"after_page_id": after_page_id,
		"next_after_page_id": page_ids[-1] if page_ids else after_page_id,
		"scanned": len(pages),
		"counts": dict(counts),
		"samples": samples,
	}
