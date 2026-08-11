# 搜索应用的核心检索引擎
"""
搜索核心引擎（v4 终极版 - 动态映射 + 停用词拦截）

设计目标：
1. 架构级解耦：利用反射动态提取 models 中的 search_fields，未来加字段零代码修改。
2. 字段权重 × 短语精确命中权重 双层加权，全部下推 ES，Python 侧零计算。
3. 停用词拦截：从应用侧直接阻断单字/常见助词引发的全表扫描。
4. 严格的长尾兜底：分词 multi_match 强制要求 minimum_should_match="75%"。
5. Paginator 直接消费惰性代理，from/size 走 ES 游标。
"""
import re
import logging
from datetime import datetime

from django.conf import settings
from django.db.models import Count, Case, When, Subquery
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
	get_content_search_shadow_target,
)
from .services.shadow import ShadowSearchRequest, wrap_search_results
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


def _is_meaningless_query(clean_query):
	"""单字或停用词直接拒绝，避免单字击穿导致 1000+ 无意义结果"""
	if len(clean_query) < 2:
		return True
	if clean_query in CHINESE_STOPWORDS:
		return True
	return False


# =============================================================================
# 字段权重配置（反射动态生成，极致解耦）
# =============================================================================
def _generate_dynamic_field_weights():
	"""自动读取 BlogPage 中定义的 search_fields，生成 ES 物理字段名与权重映射"""
	weights = {}

	for field in BlogPage.get_search_fields():
		if isinstance(field, SearchField):
			boost = field.boost if field.boost is not None else 1.0
			if field.field_name == 'title':
				es_name = 'title'
			else:
				app_label = BlogPage._meta.app_label
				model_name = BlogPage._meta.model_name
				es_name = f"{app_label}_{model_name}__{field.field_name}"

			weights[es_name] = boost

	return weights


# 动态生成字段权重字典，代替硬编码
FIELD_WEIGHTS = _generate_dynamic_field_weights()
PHRASE_BOOST = 10
MAX_RESULT_WINDOW = 10000


class SearchUnavailableError(RuntimeError):
	"""搜索后端不可用时返回的稳定领域错误。"""


def _minimum_should_match(query_string):
	"""按查询词数量调整召回门槛，避免短词被过度过滤或长查询充斥低相关结果。"""
	tokens = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", query_string)
	if len(tokens) <= 2:
		return "100%"
	if len(tokens) <= 6:
		return "75%"
	return "60%"


def _get_search_field_specs(query_compiler, include_blog_fields=False):
	"""从实际 QuerySet 编译器取得字段名、权重和展示字段，避免硬编码索引前缀。"""
	specs = []
	fields = list(query_compiler.get_searchable_fields())
	if include_blog_fields and query_compiler.queryset.model is Page:
		mapping_class = query_compiler.mapping_class
		blog_mapping = mapping_class(BlogPage)
		fields = list(BlogPage.get_searchable_search_fields())
		return [
			(
				blog_mapping.get_field_column_name(field),
				float(field.boost if field.boost is not None else 1.0),
				field.field_name,
			)
			for field in fields
			if isinstance(field, SearchField)
		]
	for field in fields:
		if not isinstance(field, SearchField):
			continue
		field_name = query_compiler.mapping.get_field_column_name(field)
		weight = float(field.boost if field.boost is not None else 1.0)
		label = field.field_name
		specs.append((field_name, weight, label))
	return specs


def _build_quality_query(query_string, field_specs):
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


def _safe_highlight_fragment(fragment):
	"""只保留转义文本和本服务生成的 mark 标签，拒绝 ES 原始 HTML。"""
	return safe_highlight_fragment(fragment)


class HighlightedSearchResults:
	"""使用 Wagtail 公开过滤条件执行带安全高亮的 ES 分页结果。"""

	def __init__(self, queryset, backend, index_name, query, sort, field_specs, query_string=""):
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

	def _backend_search(self, body, **kwargs):
		if getattr(self.backend, "use_new_elasticsearch_api", True):
			return self.backend.es.search(**body, **kwargs)
		return self.backend.es.search(body=body, **kwargs)

	def _backend_count(self):
		return self.backend.es.count(
			index=self.index_name,
			body={"query": self.query},
		)["count"]

	def count(self):
		if self._count_cache is None:
			try:
				self._count_cache = self._backend_count()
			except Exception as error:
				logger.error(f"高亮搜索计数失败: {error}", exc_info=True)
				raise SearchUnavailableError("搜索后端暂时不可用") from error
		return self._count_cache

	def __len__(self):
		return self.count()

	def __bool__(self):
		return self.count() > 0

	def __getitem__(self, key):
		if isinstance(key, slice):
			start = key.start or 0
			stop = key.stop if key.stop is not None else start + 20
			return self._fetch_slice(start, max(stop - start, 0))
		items = self._fetch_slice(key, 1)
		if not items:
			raise IndexError(key)
		return items[0]

	def _extract_highlights(self, hit):
		matched_field, fragments, title_fragment = extract_safe_highlights(
			hit,
			((field_name, label) for field_name, _, label in self.field_specs),
		)
		return matched_field, list(fragments), title_fragment

	def _fetch_slice(self, start, size):
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
def _parse_date(date_val):
	if date_val and isinstance(date_val, str):
		try:
			return datetime.strptime(date_val, '%Y-%m-%d').date()
		except ValueError:
			return None
	return date_val


def _clean_query(query_string):
	return re.sub(r'["“”]', '', query_string).strip()


# =============================================================================
# 惰性搜索结果代理：总数走计数接口，分页走 from/size。
# =============================================================================
class ESLazyResults:
	def __init__(self, es_client, index_name, dsl_query, dsl_filter):
		self.es = es_client
		self.index = index_name
		self.query = dsl_query
		self.filter = dsl_filter
		self._count_cache = None

	def _build_body(self, frm=0, size=20, source=False):
		# 统一构造查询体，确保计数和分页使用完全相同的过滤条件。
		bool_body = {"must": [self.query]}
		if self.filter:
			bool_body["filter"] = self.filter
		return {
			"query": {"bool": bool_body},
			"from": frm,
			"size": size,
			"_source": source,
			"track_total_hits": False,
		}

	def count(self):
		# 计数结果只请求一次，避免分页流程重复访问搜索引擎。
		if self._count_cache is None:
			body = {"query": self._build_body()["query"]}
			try:
				self._count_cache = self.es.count(
					index=self.index, body=body
				)["count"]
			except Exception as e:
				logger.error(f"ES _count 失败: {e}", exc_info=True)
				self._count_cache = 0
		return self._count_cache

	def __len__(self):
		return min(self.count(), MAX_RESULT_WINDOW)

	def __getitem__(self, k):
		# 将 Django 分页器的切片转换为搜索引擎的 from/size 参数。
		if isinstance(k, slice):
			start = k.start or 0
			stop = k.stop if k.stop is not None else (start + 20)
			size = max(stop - start, 0)
			return self._fetch_slice(start, size)
		objs = self._fetch_slice(k, 1)
		return objs[0] if objs else None

	def _fetch_slice(self, start, size):
		# 搜索引擎返回 ID 后，再按原顺序从 Wagtail 查询具体页面对象。
		if size <= 0:
			return []
		if start >= MAX_RESULT_WINDOW:
			return []
		size = min(size, MAX_RESULT_WINDOW - start)

		body = self._build_body(frm=start, size=size, source=False)

		try:
			hits = self.es.search(index=self.index, body=body)["hits"]["hits"]
		except Exception as e:
			logger.error(f"ES search 失败: {e}", exc_info=True)
			return []

		page_ids = []
		for h in hits:
			raw_id = str(h.get("_id", ""))
			digits = re.findall(r'\d+', raw_id)
			if digits:
				try:
					page_ids.append(int(digits[-1]))
				except ValueError:
					continue

		if not page_ids:
			return []

		preserved = Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(page_ids)])
		# ES 索引不承载完整访问限制，回查必须再次执行 Wagtail 公开范围校验。
		return list(
			Page.objects.live().public().filter(pk__in=page_ids).specific().order_by(preserved)
		)


# =============================================================================
# 基础 QuerySet 构造
# =============================================================================
def _build_base_qs(search_type, parsed_start, parsed_end, order_by):
	if search_type == 'blog':
		qs = BlogPage.objects.live().public()
		if parsed_start:
			qs = qs.filter(date__gte=parsed_start)
		if parsed_end:
			qs = qs.filter(date__lte=parsed_end)
		if order_by in ('date', '-date'):
			qs = qs.order_by(order_by)

	elif search_type == 'pages':
		blog_ids_subq = Subquery(BlogPage.objects.values('id'))
		qs = Page.objects.live().public().exclude(id__in=blog_ids_subq)
		if parsed_start:
			qs = qs.filter(last_published_at__gte=parsed_start)
		if parsed_end:
			qs = qs.filter(last_published_at__lte=parsed_end)
		if order_by == 'date':
			qs = qs.order_by('last_published_at')
		elif order_by == '-date':
			qs = qs.order_by('-last_published_at')

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
# ES 客户端 / 索引名获取（使用 Wagtail 原生 API 保证 100% 准确）
# =============================================================================
def _get_es_client_and_index():
	backend = get_search_backend('default')
	es = getattr(backend, 'es', None) or getattr(backend, 'client', None)

	# 直接调用原生 API，避免一切拼接错误（例如前缀丢失、多加下划线等问题）
	index_name = backend.get_index_for_model(Page).name
	return es, index_name


# =============================================================================
# 核心查询构造：字段加权、短语命中与分词兜底。
# =============================================================================
def _build_search_dsl(clean_query, search_type, parsed_start, parsed_end):
	should_clauses = []

	# A. 字段级短语匹配：连续命中时叠加字段权重和短语加成。
	for field, weight in FIELD_WEIGHTS.items():
		should_clauses.append({
			"match_phrase": {
				field: {
					"query": clean_query,
					"boost": weight * PHRASE_BOOST,
					"slop": 0,
				}
			}
		})

	# B. 分词兜底：要求至少命中 75% 的词，阻止低质量的满屏结果。
	should_clauses.append({
		"multi_match": {
			"query": clean_query,
			"fields": [f"{f}^{w}" for f, w in FIELD_WEIGHTS.items()],
			"type": "best_fields",
			"operator": "or",
			"minimum_should_match": "75%",  # 例如四个词至少命中三个。
			"boost": 1.0,
		}
	})

	dsl_query = {
		"bool": {
			"should": should_clauses,
			"minimum_should_match": 1,
		}
	}

	# C. 按内容类型过滤，保证“博客”和“普通页面”互斥。
	dsl_filter = [
		{"term": {"live_filter": True}},
	]

	if search_type == 'blog':
		dsl_filter.append({"term": {"_django_content_type": "blog.BlogPage"}})
	elif search_type == 'pages':
		dsl_filter.append({
			"bool": {"must_not": [{"term": {"_django_content_type": "blog.BlogPage"}}]}
		})

	# D. 日期区间过滤；博客使用自定义日期字段，其他页面使用发布时间字段。
	if parsed_start or parsed_end:
		range_q = {}
		if parsed_start:
			range_q["gte"] = parsed_start.isoformat()
		if parsed_end:
			range_q["lte"] = parsed_end.isoformat()
		date_field = (
			"blog_blogpage__date_filter"
			if search_type == 'blog'
			else "first_published_at_filter"
		)
		dsl_filter.append({"range": {date_field: range_q}})

	return dsl_query, dsl_filter


# =============================================================================
# 对外主接口
# =============================================================================
def _wrap_with_content_search_shadow(
	results,
	clean_query,
	search_type,
	parsed_start,
	parsed_end,
	order_by,
):
	if not getattr(settings, "CONTENT_SEARCH_SHADOW_READ_ENABLED", False):
		return results
	if search_type != "blog":
		return results
	try:
		target = get_content_search_shadow_target()
	except ContentSearchQueryUnavailable as error:
		logger.warning("content_search_shadow_disabled code=%s", error.code)
		return results

	def request_factory(start, size, expected_page_ids):
		return ShadowSearchRequest(
			query_string=clean_query,
			search_type=search_type,
			start=start,
			size=size,
			start_date=parsed_start,
			end_date=parsed_end,
			order_by=order_by,
			expected_page_ids=expected_page_ids,
			target=target,
		)

	return wrap_search_results(results, request_factory)


def perform_search(query_string, search_type='all', start_date=None, end_date=None, order_by=None):
	parsed_start = _parse_date(start_date)
	parsed_end = _parse_date(end_date)

	if not query_string:
		return _build_base_qs(search_type, parsed_start, parsed_end, order_by)

	clean_query = _clean_query(query_string)

	# 空字符串、单字或停用词直接返回空结果，避免触发无意义的全表扫描。
	if not clean_query or _is_meaningless_query(clean_query):
		return _build_base_qs(search_type, parsed_start, parsed_end, order_by).none()

	if getattr(settings, "CONTENT_SEARCH_QUERY_ENABLED", False) and search_type == "blog":
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

	qs = _build_base_qs(search_type, parsed_start, parsed_end, order_by)
	try:
		# 先让 Wagtail 编译公开 QuerySet，再复用其 content type、权限和日期过滤条件。
		compiled_results = qs.search(
			clean_query,
			operator='or',
			order_by_relevance=order_by not in ('date', '-date'),
		)
		try:
			Query.get(query_string).add_hit()
		except Exception as error:
			logger.warning(f"记录搜索词命中次数失败: {error}")
		if not getattr(settings, "SEARCH_HIGHLIGHTS_ENABLED", True):
			return _wrap_with_content_search_shadow(
				compiled_results,
				clean_query,
				search_type,
				parsed_start,
				parsed_end,
				order_by,
			)
		query_compiler = getattr(compiled_results, "query_compiler", None)
		if query_compiler is None:
			return _wrap_with_content_search_shadow(
				compiled_results,
				clean_query,
				search_type,
				parsed_start,
				parsed_end,
				order_by,
			)

		field_specs = _get_search_field_specs(
			query_compiler,
			include_blog_fields=search_type == "all",
		)
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
		return _wrap_with_content_search_shadow(
			HighlightedSearchResults(
				queryset=qs,
				backend=backend,
				index_name=index_name,
				query=query,
				sort=query_compiler.get_sort(),
				field_specs=field_specs,
				query_string=clean_query,
			),
			clean_query,
			search_type,
			parsed_start,
			parsed_end,
			order_by,
		)
	except Exception as error:
		logger.error(f"公开搜索后端不可用: {error}", exc_info=True)
		raise SearchUnavailableError("搜索后端暂时不可用") from error


# =============================================================================
# 接口结果转换
# =============================================================================
def format_search_results_for_api(search_results):
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
def get_search_suggestions(query_string, limit=5):
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
