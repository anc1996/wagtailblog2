# 搜索应用的核心检索引擎
"""
搜索核心引擎。

Blog 查询固定走独立内容索引，普通 Pages 查询继续使用 Wagtail 默认 Page 索引；
全站查询由两条正式路径联邦合并。
"""
import re
import logging
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from django.conf import settings
from django.db.models import Count
from django.utils.html import strip_tags
from django.utils.text import Truncator
from wagtail.models import Page
from wagtail.contrib.search_promotions.models import Query
from wagtail.search.backends import get_search_backend
from wagtail.search.index import SearchField

from blog.models import BlogPage
from .services.content_query import (
	ContentSearchQueryUnavailable,
	build_content_search_results,
)
from .services.federated_query import build_federated_search_results
from .services.pages_query import build_public_pages_queryset
from .services.highlights import (
	BODY_HIGHLIGHT_MAX_ANALYZER_OFFSET,
	HIGHLIGHT_END_TAG,
	HIGHLIGHT_START_TAG,
	build_highlight_fields,
	extract_safe_highlights,
	safe_highlight_fragment,
)

logger = logging.getLogger(__name__)

# =============================================================================
# 停用词防线（阻断无意义的高频全表扫描）
# =============================================================================
CHINESE_STOPWORDS = {
	'的', '了', '是', '在', '和', '与', '及', '或', '也', '都', '就',
	'我', '你', '他', '她', '这', '那', '一', '不', '人', '有', '上',
	'个', '种', '些', '等', '之', '其', '于', '而', '以', '所'
}


def _is_meaningless_query(clean_query: str) -> bool:
	"""单字或停用词直接拒绝，避免单字击穿导致 1000+ 无意义结果"""
	if len(clean_query) < 2:
		return True
	if clean_query in CHINESE_STOPWORDS:
		return True
	return False


PHRASE_BOOST = 10
MAX_RESULT_WINDOW = 10000


class SearchUnavailableError(RuntimeError):
	"""搜索后端不可用时返回的稳定领域错误。"""


def _minimum_should_match(query_string: str) -> str:
	"""按查询词数量调整召回门槛，避免短词被过度过滤或长查询充斥低相关结果。"""
	tokens = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", query_string)
	if len(tokens) <= 2:
		return "100%"
	if len(tokens) <= 6:
		return "75%"
	return "60%"


def _get_search_field_specs(query_compiler: Any) -> list[tuple[str, float, str]]:
	"""从实际 QuerySet 编译器取得字段名、权重和展示字段，避免硬编码索引前缀。"""
	specs = []
	fields = list(query_compiler.get_searchable_fields())
	for field in fields:
		if not isinstance(field, SearchField):
			continue
		field_name = query_compiler.mapping.get_field_column_name(field)
		weight = float(field.boost if field.boost is not None else 1.0)
		label = field.field_name
		specs.append((field_name, weight, label))
	return specs


def _build_quality_query(
    query_string: str,
    field_specs: Iterable[tuple[str, float, str]],
) -> dict[str, Any]:
	"""构造字段加权、短语优先且带动态词项门槛的公开搜索查询。"""
	should_clauses = []
	for field_name, weight, _ in field_specs:
		should_clauses.append({
			"match_phrase": {
				field_name: {
					"query": query_string,
					"boost": weight * PHRASE_BOOST,
					"slop": 0,
				}
			}
		})

	should_clauses.append({
		"multi_match": {
			"query": query_string,
			"fields": [
				f"{field_name}^{weight:g}"
				for field_name, weight, _ in field_specs
			],
			"type": "best_fields",
			"operator": "or",
			"minimum_should_match": _minimum_should_match(query_string),
		}
	})
	return {
		"bool": {
			"should": should_clauses,
			"minimum_should_match": 1,
		}
	}


def _safe_highlight_fragment(fragment: Any) -> str:
	"""只保留转义文本和本服务生成的 mark 标签，拒绝 ES 原始 HTML。"""
	return safe_highlight_fragment(fragment)


class HighlightedSearchResults:
	"""ES 高亮结果适配器；页面最终仍由公开 QuerySet 二次过滤。

	ES 只提供候选 ID 和高亮片段，避免下线或非公开页面因索引延迟直接暴露。
	"""

	def __init__(
        self,
        queryset: Any,
        backend: Any,
        index_name: str,
        query: Mapping[str, Any],
        sort: Any,
        field_specs: Sequence[tuple[str, float, str]],
        query_string: str = "",
    ) -> None:
		self.queryset = queryset
		self.backend = backend
		self.index_name = index_name
		self.query = query
		self.sort = sort
		self.field_specs = field_specs
		self.query_string = query_string
		self.highlight_fields = build_highlight_fields(
			(field_name, label) for field_name, _, label in field_specs
		)
		self._count_cache = None

	def _backend_search(self, body: Mapping[str, Any], **kwargs: Any) -> Mapping[str, Any]:
		if getattr(self.backend, "use_new_elasticsearch_api", True):
			return self.backend.es.search(**body, **kwargs)
		return self.backend.es.search(body=body, **kwargs)

	def _backend_count(self) -> int:
		return self.backend.es.count(
			index=self.index_name,
			body={"query": self.query},
		)["count"]

	def count(self) -> int:
		if self._count_cache is None:
			try:
				self._count_cache = self._backend_count()
			except Exception as error:
				logger.error(f"高亮搜索计数失败: {error}", exc_info=True)
				raise SearchUnavailableError("搜索后端暂时不可用") from error
		return self._count_cache

	def __len__(self) -> int:
		return self.count()

	def __bool__(self) -> bool:
		return self.count() > 0

	def __getitem__(self, key: int | slice) -> Any:
		if isinstance(key, slice):
			start = key.start or 0
			stop = key.stop if key.stop is not None else start + 20
			return self._fetch_slice(start, max(stop - start, 0))
		items = self._fetch_slice(key, 1)
		if not items:
			raise IndexError(key)
		return items[0]

	def _extract_highlights(self, hit: Mapping[str, Any]) -> tuple[str, list[str], str]:
		matched_field, fragments, title_fragment = extract_safe_highlights(
			hit,
			((field_name, label) for field_name, _, label in self.field_specs),
		)
		return matched_field, list(fragments), title_fragment

	def _fetch_slice(self, start: int, size: int) -> list[Any]:
		if size <= 0 or start >= MAX_RESULT_WINDOW:
			return []
		size = min(size, MAX_RESULT_WINDOW - start)
		body = {"query": self.query, "track_total_hits": False}
		if self.sort is not None:
			body["sort"] = self.sort
		if self.highlight_fields:
			body["highlight"] = {
				"pre_tags": [HIGHLIGHT_START_TAG],
				"post_tags": [HIGHLIGHT_END_TAG],
				"order": "score",
				"fields": self.highlight_fields,
			}

		try:
			response = self._backend_search(
				body,
				index=self.index_name,
				from_=start,
				size=size,
				stored_fields="pk",
			)["hits"]["hits"]
		except Exception as error:
			if "max_analyzed_offset" in str(error).lower() or "max_analyzer_offset" in str(error).lower():
				# Filebeat 会采集该结构化告警，作为正文高亮超限的可聚合观测指标。
				logger.warning(
					"search_highlight_body_offset_exceeded start=%s size=%s max_analyzer_offset=%s",
					start,
					size,
					BODY_HIGHLIGHT_MAX_ANALYZER_OFFSET,
				)
			logger.error(f"高亮搜索查询失败: {error}", exc_info=True)
			raise SearchUnavailableError("搜索后端暂时不可用") from error

		page_ids = []
		hits_with_ids = []
		for hit in response:
			fields = hit.get("fields") or {}
			raw_id = (fields.get("pk") or [hit.get("_id")])[0]
			try:
				page_id = int(raw_id)
			except (TypeError, ValueError):
				continue
			page_ids.append(page_id)
			hits_with_ids.append((page_id, hit))
		if not page_ids:
			return []

		public_pages = self.queryset.filter(pk__in=page_ids)
		if hasattr(public_pages, "specific"):
			public_pages = public_pages.specific()
		pages_by_id = {str(page.pk): page for page in public_pages}
		results = []
		for page_id, hit in hits_with_ids:
			page = pages_by_id.get(str(page_id))
			if page is None:
				continue
			matched_field, fragments, title_fragment = self._extract_highlights(hit)
			# 先完成公开 QuerySet 回查，再把任何搜索片段挂到页面对象。
			setattr(page, "search_matched_field", matched_field)
			setattr(page, "search_highlight_fragments", fragments)
			setattr(page, "search_title_highlight", title_fragment)
			if title_fragment:
				setattr(page, "search_title_query", self.query_string)
			results.append(page)
		return results


# =============================================================================
# 工具函数
# =============================================================================
def _parse_date(date_val: Any) -> Any:
	if date_val and isinstance(date_val, str):
		try:
			return datetime.strptime(date_val, '%Y-%m-%d').date()
		except ValueError:
			return None
	return date_val


def _clean_query(query_string: str) -> str:
	return re.sub(r'["“”]', '', query_string).strip()


# =============================================================================
# 基础 QuerySet 构造
# =============================================================================
def _build_base_qs(
    search_type: str,
    parsed_start: Any,
    parsed_end: Any,
    order_by: str | None,
) -> Any:
	if search_type == 'pages':
		qs = build_public_pages_queryset(parsed_start, parsed_end, order_by)

	else:
		qs = Page.objects.live().public()
		if parsed_start:
			qs = qs.filter(first_published_at__gte=parsed_start)
		if parsed_end:
			qs = qs.filter(first_published_at__lte=parsed_end)
		if order_by == 'date':
			qs = qs.order_by('first_published_at')
		elif order_by == '-date':
			qs = qs.order_by('-first_published_at')

	return qs

# =============================================================================
# 对外主接口
# =============================================================================
def _build_search_results_for_queryset(
	qs: Any,
	clean_query: str,
	search_type: str,
	start_date: Any = None,
	end_date: Any = None,
	order_by: str | None = None,
) -> Any:
	"""执行 Wagtail Page 搜索，供 pages 和联邦 all 共享。"""
	try:
		# 先让 Wagtail 编译公开 QuerySet，再复用其 content type、权限和日期过滤条件。
		compiled_results = qs.search(
			clean_query,
			operator='or',
			order_by_relevance=order_by not in ('date', '-date'),
		)
		try:
			Query.get(clean_query).add_hit()
		except Exception as error:
			logger.warning(f"记录搜索词命中次数失败: {error}")
		if not getattr(settings, "SEARCH_HIGHLIGHTS_ENABLED", True):
			return compiled_results
		query_compiler = getattr(compiled_results, "query_compiler", None)
		if query_compiler is None:
			return compiled_results

		field_specs = _get_search_field_specs(query_compiler)
		if not field_specs:
			return qs.none()
		quality_query = _build_quality_query(clean_query, field_specs)
		filters = [item for item in query_compiler.get_filters() if item]
		query = {"bool": {"must": quality_query}}
		if len(filters) == 1:
			query["bool"]["filter"] = filters[0]
		elif filters:
			query["bool"]["filter"] = filters

		backend = get_search_backend('default')
		index_name = backend.get_index_for_model(qs.model).name
		return HighlightedSearchResults(
			queryset=qs,
			backend=backend,
			index_name=index_name,
			query=query,
			sort=query_compiler.get_sort(),
			field_specs=field_specs,
			query_string=clean_query,
		)
	except Exception as error:
		logger.error(f"公开搜索后端不可用: {error}", exc_info=True)
		raise SearchUnavailableError("搜索后端暂时不可用") from error


def perform_search(
	query_string: str,
	search_type: str = 'all',
	start_date: Any = None,
	end_date: Any = None,
	order_by: str | None = None,
) -> Any:
	"""按搜索类型选择后端，并保持空查询和无效查询的短路语义。

	Blog 使用独立内容索引，Pages 使用 Wagtail Page 索引，all 合并两条公开结果流。
	后端不可用统一转换为 SearchUnavailableError，由视图层决定响应格式。
	"""
	parsed_start = _parse_date(start_date)
	parsed_end = _parse_date(end_date)

	if not query_string:
		return _build_base_qs(search_type, parsed_start, parsed_end, order_by)

	clean_query = _clean_query(query_string)

	# 空字符串、单字或停用词直接返回空结果，避免触发无意义的全表扫描。
	if not clean_query or _is_meaningless_query(clean_query):
		return _build_base_qs(search_type, parsed_start, parsed_end, order_by).none()

	if search_type == "blog":
		try:
			content_results = build_content_search_results(
				clean_query,
				start_date=parsed_start,
				end_date=parsed_end,
				order_by=order_by,
			)
		except ContentSearchQueryUnavailable as error:
			logger.error("content_search_query_unavailable code=%s", error.code)
			raise SearchUnavailableError("内容搜索服务暂时不可用") from error
		try:
			Query.get(query_string).add_hit()
		except Exception as error:
			logger.warning(f"记录搜索词命中次失败: {error}")
		return content_results

	if search_type == "all":
		try:
			return build_federated_search_results(
				clean_query,
				start_date=parsed_start,
				end_date=parsed_end,
				order_by=order_by,
			)
		except ContentSearchQueryUnavailable as error:
			logger.error("federated_search_unavailable code=%s", error.code)
			raise SearchUnavailableError("全站搜索服务暂时不可用") from error

	qs = _build_base_qs(search_type, parsed_start, parsed_end, order_by)
	return _build_search_results_for_queryset(
		qs,
		clean_query,
		search_type=search_type,
		start_date=parsed_start,
		end_date=parsed_end,
		order_by=order_by,
	)


# =============================================================================
# 接口结果转换
# =============================================================================
def format_search_results_for_api(search_results: Iterable[Any]) -> list[dict[str, Any]]:
	# 将页面对象转换为稳定的 JSON 字段，避免把 Wagtail 内部对象直接暴露给前端。
	results_data = []
	if not search_results:
		return results_data
	try:
		for page in search_results:
			specific_page = page.specific if hasattr(page, 'specific') else page
			highlight_fragments = [
				str(fragment)
				for fragment in getattr(page, 'search_highlight_fragments', [])
			]
			data = {
				'id': page.id,
				'title': page.title,
				'url': page.get_url(),
				'matched_field': getattr(page, 'search_matched_field', ''),
				'highlight_fragments': highlight_fragments,
				'title_highlight': str(getattr(page, 'search_title_highlight', '') or ''),
				'search_description': Truncator(strip_tags(str(getattr(page, 'search_description', '') or ''))).chars(190),
				'content_type': page.content_type.model,
				'last_published_at': page.last_published_at.strftime('%Y-%m-%d %H:%M')
				if page.last_published_at else '',
			}
			if isinstance(specific_page, BlogPage):
				data['intro'] = Truncator(strip_tags(str(specific_page.intro or ''))).chars(190)
				data['date'] = specific_page.date.strftime('%Y-%m-%d') if specific_page.date else ''
				if hasattr(specific_page, 'tags'):
					data['tags'] = [tag.name for tag in specific_page.tags.all()]
				if hasattr(specific_page, 'categories'):
					data['categories'] = [cat.name for cat in specific_page.categories.all()]
			results_data.append(data)
	except Exception as e:
		logger.error(f"格式化搜索结果非预期异常: {e}", exc_info=True)
	return results_data


# =============================================================================
# 搜索联想建议
# =============================================================================
def get_search_suggestions(query_string: str, limit: int = 5) -> list[dict[str, Any]]:
	if not query_string or len(query_string) < 2:
		return []
	if getattr(settings, "SEARCH_SUGGESTIONS_V2_ENABLED", False):
		from .services.suggestions import get_public_search_suggestions
		return get_public_search_suggestions(query_string, limit)

	try:
		suggestions = Query.objects.filter(
			query_string__icontains=query_string
		).annotate(
			total_hits_count=Count('daily_hits')
		).order_by('-total_hits_count')[:limit]

		return [
			{'query': item.query_string, 'hits': item.total_hits_count}
			for item in suggestions
		]
	except Exception as e:
		logger.error(f"获取搜索建议时出错: {e}", exc_info=True)
		return []
