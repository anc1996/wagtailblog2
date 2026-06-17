# apps/search/core.py
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
import traceback
from datetime import datetime

from django.db.models import Count, Case, When, Subquery
from wagtail.models import Page
from wagtail.contrib.search_promotions.models import Query
from wagtail.search.backends import get_search_backend
from wagtail.search.index import SearchField

from blog.models import BlogPage

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
# 惰性 ES 结果代理：count 走 _count、切片走 from/size
# =============================================================================
class ESLazyResults:
	def __init__(self, es_client, index_name, dsl_query, dsl_filter):
		self.es = es_client
		self.index = index_name
		self.query = dsl_query
		self.filter = dsl_filter
		self._count_cache = None
	
	def _build_body(self, frm=0, size=20, source=False):
		bool_body = {"must": [self.query]}
		if self.filter:
			bool_body["filter"] = self.filter
		return {
			"query": {"bool": bool_body},
			"from": frm,
			"size": size,
			"_source": source,
			"track_total_hits": True,
		}
	
	def count(self):
		if self._count_cache is None:
			body = {"query": self._build_body()["query"]}
			try:
				self._count_cache = self.es.count(
					index=self.index, body=body
				)["count"]
			except Exception as e:
				logger.error(f"ES _count 失败: {e}")
				self._count_cache = 0
		return self._count_cache
	
	def __len__(self):
		return min(self.count(), MAX_RESULT_WINDOW)
	
	def __getitem__(self, k):
		if isinstance(k, slice):
			start = k.start or 0
			stop = k.stop if k.stop is not None else (start + 20)
			size = max(stop - start, 0)
			return self._fetch_slice(start, size)
		objs = self._fetch_slice(k, 1)
		return objs[0] if objs else None
	
	def _fetch_slice(self, start, size):
		if size <= 0:
			return []
		if start >= MAX_RESULT_WINDOW:
			return []
		size = min(size, MAX_RESULT_WINDOW - start)
		
		body = self._build_body(frm=start, size=size, source=False)
		
		try:
			hits = self.es.search(index=self.index, body=body)["hits"]["hits"]
		except Exception as e:
			logger.error(f"ES search 失败: {e}\n{traceback.format_exc()}")
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
		return list(
			Page.objects.filter(pk__in=page_ids).specific().order_by(preserved)
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
# 核心 DSL 构造：字段加权 × 短语命中 × 分词 fallback
# =============================================================================
def _build_search_dsl(clean_query, search_type, parsed_start, parsed_end):
	should_clauses = []
	
	# A. 字段级 match_phrase（精确连贯短语 × 字段权重 × 10）
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
	
	# B. 分词 fallback（加入 75% 命中率防线）
	should_clauses.append({
		"multi_match": {
			"query": clean_query,
			"fields": [f"{f}^{w}" for f, w in FIELD_WEIGHTS.items()],
			"type": "best_fields",
			"operator": "or",
			"minimum_should_match": "75%",  # 核心拦截：搜4个词至少要命中3个，彻底告别单字满屏
			"boost": 1.0,
		}
	})
	
	dsl_query = {
		"bool": {
			"should": should_clauses,
			"minimum_should_match": 1,
		}
	}
	
	# C. 过滤条件
	dsl_filter = [
		{"term": {"live_filter": True}},
	]
	
	if search_type == 'blog':
		dsl_filter.append({"term": {"_django_content_type": "blog.BlogPage"}})
	elif search_type == 'pages':
		dsl_filter.append({
			"bool": {"must_not": [{"term": {"_django_content_type": "blog.BlogPage"}}]}
		})
	
	# D. 日期区间
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
def perform_search(query_string, search_type='all', start_date=None, end_date=None, order_by=None):
	parsed_start = _parse_date(start_date)
	parsed_end = _parse_date(end_date)
	
	if not query_string:
		return _build_base_qs(search_type, parsed_start, parsed_end, order_by)
	
	clean_query = _clean_query(query_string)
	
	# 【新增应用层拦截】：如果是空字符、单字查询，或常见停用词，直接返回空查询集
	if not clean_query or _is_meaningless_query(clean_query):
		return _build_base_qs(search_type, parsed_start, parsed_end, order_by).none()
	
	use_relevance = order_by not in ('date', '-date')
	
	if not use_relevance:
		qs = _build_base_qs(search_type, parsed_start, parsed_end, order_by)
		try:
			results = qs.search(clean_query, operator='or', order_by_relevance=False)
			Query.get(query_string).add_hit()
			return results
		except Exception as e:
			logger.error(f"时间排序搜索失败，降级到原始 QS: {e}")
			return qs
	
	try:
		es, index_name = _get_es_client_and_index()
		if es is None:
			raise RuntimeError("无法获取 ES 客户端")
		
		dsl_query, dsl_filter = _build_search_dsl(
			clean_query, search_type, parsed_start, parsed_end
		)
		
		Query.get(query_string).add_hit()
		return ESLazyResults(es, index_name, dsl_query, dsl_filter)
	
	except Exception as e:
		logger.error(f"ES 原生 DSL 检索失败，降级到 Wagtail 抽象层: {e}\n{traceback.format_exc()}")
		qs = _build_base_qs(search_type, parsed_start, parsed_end, order_by)
		try:
			results = qs.search(clean_query, operator='or', order_by_relevance=True)
			return results
		except Exception as e2:
			logger.error(f"降级搜索也失败: {e2}")
			return qs.none() if hasattr(qs, 'none') else []


# =============================================================================
# JSON 网关
# =============================================================================
def format_search_results_for_api(search_results):
	results_data = []
	if not search_results:
		return results_data
	try:
		for page in search_results:
			specific_page = page.specific if hasattr(page, 'specific') else page
			data = {
				'id': page.id,
				'title': page.title,
				'url': page.get_url(),
				'search_description': getattr(page, 'search_description', '') or '',
				'content_type': page.content_type.model,
				'last_published_at': page.last_published_at.strftime('%Y-%m-%d %H:%M')
				if page.last_published_at else '',
			}
			if isinstance(specific_page, BlogPage):
				data['intro'] = specific_page.intro or ''
				data['date'] = specific_page.date.strftime('%Y-%m-%d') if specific_page.date else ''
				if hasattr(specific_page, 'tags'):
					data['tags'] = [tag.name for tag in specific_page.tags.all()]
				if hasattr(specific_page, 'categories'):
					data['categories'] = [cat.name for cat in specific_page.categories.all()]
			results_data.append(data)
	except Exception as e:
		logger.error(f"格式化搜索结果非预期异常: {e}\n{traceback.format_exc()}")
	return results_data


# =============================================================================
# 搜索联想建议
# =============================================================================
def get_search_suggestions(query_string, limit=5):
	if not query_string or len(query_string) < 2:
		return []
	
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
		logger.error(f"获取搜索建议时出错: {e}\n{traceback.format_exc()}")
		return []