# apps/search/core.py
import re,traceback
from bson import ObjectId
from django.utils.html import strip_tags
from wagtail.models import Page
from blog.models import BlogPage
from wagtail.contrib.search_promotions.models import Query
from django.db.models import Count, Case, When
import logging
from datetime import datetime
from wagtailblog3.mongo import MongoManager

logger = logging.getLogger(__name__)


def normalize_text_for_match(text):
	"""
	【企业级杀手锏：降维归一化引擎】
	消除一切由于富文本排版、换行、HTML实体或中英文空格导致的匹配失败。
	将 "Python 的生态 力量！" 与 "python的生态力量" 在内存中拉平至同一维度。
	"""
	if not text:
		return ""
	# 去除常见 HTML 实体残留
	text = str(text).replace('&nbsp;', '').replace('&amp;', '')
	# 核心降维：正则保留所有字母、数字、中文字符，彻底抹除空格和标点符号，全转小写
	return re.sub(r'[^\w\u4e00-\u9fa5]', '', text).lower()


class LazyChainedResultList:
	"""
	【架构师级：惰性结果拼接器】
	无损承接 L2 精排后的头部数据与 ES 未拉取的长尾数据。
	"""
	
	def __init__(self, head_list, tail_query_set, split_point):
		self.head_list = head_list
		self.tail_query_set = tail_query_set
		self.split_point = split_point
		
		# 极速获取总命中数，利用 ES 的 _count 瞬间返回，绝不加载真实数据防 OOM
		self._count = tail_query_set.count() if hasattr(tail_query_set, 'count') else len(tail_query_set)
	
	def count(self):
		return self._count
	
	def __len__(self):
		return self._count
	
	def _get_specific_slice(self, start, stop):
		raw_slice = list(self.tail_query_set[start:stop])
		if not raw_slice:
			return []
		page_ids = [p.pk for p in raw_slice]
		preserved_order = Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(page_ids)])
		return list(Page.objects.filter(pk__in=page_ids).specific().order_by(preserved_order))
	
	def __getitem__(self, k):
		if isinstance(k, slice):
			start = k.start or 0
			stop = k.stop if k.stop is not None else self._count
			
			if stop <= self.split_point:
				return self.head_list[start:stop]
			elif start >= self.split_point:
				return self._get_specific_slice(start, stop)
			else:
				head_part = self.head_list[start:self.split_point]
				tail_part = self._get_specific_slice(self.split_point, stop)
				return head_part + tail_part
		else:
			if k < self.split_point:
				return self.head_list[k]
			else:
				return self._get_specific_slice(k, k + 1)[0]


def _parse_date(date_val):
	if date_val and isinstance(date_val, str):
		try:
			return datetime.strptime(date_val, '%Y-%m-%d').date()
		except ValueError:
			return None
	return date_val


def perform_search(query_string, search_type='all', start_date=None, end_date=None, order_by=None):
	parsed_start = _parse_date(start_date)
	parsed_end = _parse_date(end_date)
	
	# =========================================================================
	# 1. 基础条件网构建
	# =========================================================================
	if search_type == 'blog':
		qs = BlogPage.objects.live().public()
		if parsed_start: qs = qs.filter(date__gte=parsed_start)
		if parsed_end: qs = qs.filter(date__lte=parsed_end)
		if order_by in ['date', '-date']: qs = qs.order_by(order_by)
	elif search_type == 'pages':
		blog_ids = BlogPage.objects.values_list('id', flat=True)
		qs = Page.objects.live().public().exclude(id__in=blog_ids)
		if parsed_start: qs = qs.filter(last_published_at__gte=parsed_start)
		if parsed_end: qs = qs.filter(last_published_at__lte=parsed_end)
		if order_by == 'date':
			qs = qs.order_by('last_published_at')
		elif order_by == '-date':
			qs = qs.order_by('-last_published_at')
	else:
		qs = Page.objects.live().public()
		if parsed_start: qs = qs.filter(first_published_at__gte=parsed_start)
		if parsed_end: qs = qs.filter(first_published_at__lte=parsed_end)
		if order_by == 'date':
			qs = qs.order_by('first_published_at')
		elif order_by == '-date':
			qs = qs.order_by('-first_published_at')
	
	if not query_string:
		return qs
	
	# =========================================================================
	# 2. ES 底层分词召回 (L1 Recall)
	# =========================================================================
	clean_query = re.sub(r'["“”]', '', query_string).strip()
	use_relevance = order_by not in ['date', '-date']
	
	# 【核心防御 1】：强制 operator='or'。
	# 彻底解决包含停用词（如"的"）的长句触发 AND 严格校验导致 0 结果的 Bug。
	# ES 的 BM25 算法会自动将包含词频最高的文章推到头部，确保长尾不断。
	raw_results = qs.search(clean_query, operator='or', order_by_relevance=use_relevance)
	
	if not use_relevance:
		query_obj = Query.get(query_string)
		query_obj.add_hit()
		return raw_results
	
	# =========================================================================
	# 3. 内存极速归一化精排 (L2 Rescoring) - 扩大至 Top 300
	# =========================================================================
	# 增加精排池深度至 300，确保 BM25 初筛出的精确匹配长句绝对不会掉出池外
	top_pages = list(raw_results[:300])
	split_point = len(top_pages)
	
	if not top_pages:
		return []
	
	page_ids = [p.pk for p in top_pages]
	preserved_order = Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(page_ids)])
	specific_pages = list(Page.objects.filter(pk__in=page_ids).specific().order_by(preserved_order))
	
	valid_object_ids = []
	for page in specific_pages:
		if isinstance(page, BlogPage):
			mongo_id_str = getattr(page, 'mongo_content_id', None)
			if mongo_id_str:
				try:
					valid_object_ids.append(ObjectId(mongo_id_str))
				except Exception as e:
					logger.warning(f"跳过无效 Mongo ID [{mongo_id_str}]")
	
	mongo_contents = {}
	if valid_object_ids:
		mongo_manager = MongoManager()
		cursor = mongo_manager.blog_content.find({'_id': {'$in': valid_object_ids}})
		for doc in cursor:
			mongo_contents[str(doc['_id'])] = doc
	
	exact_match_top = []
	long_tail_fallback = []
	
	# 获取完全降维的搜索词
	normalized_query = normalize_text_for_match(clean_query)
	
	for page in specific_pages:
		search_text_pool = ""
		if page.title: search_text_pool += page.title + " "
		if getattr(page, 'search_description', None): search_text_pool += page.search_description + " "
		
		if isinstance(page, BlogPage):
			if page.intro: search_text_pool += strip_tags(str(page.intro)) + " "
			
			mid = getattr(page, 'mongo_content_id', None)
			if mid and mid in mongo_contents:
				body_data = mongo_contents[mid].get('body', [])
				for block in body_data:
					if isinstance(block, dict) and block.get('value'):
						val = block['value']
						if isinstance(val, str):
							search_text_pool += strip_tags(val) + " "
						elif isinstance(val, dict):
							search_text_pool += str(val.get('code', '')) + " "
		
		# 将页面的文本池也进行彻底降维
		normalized_pool = normalize_text_for_match(search_text_pool)
		
		# 【核心防御 2】：纯净字符串连贯包含匹配！
		if normalized_query and normalized_query in normalized_pool:
			exact_match_top.append(page)
		else:
			long_tail_fallback.append(page)
	
	# 物理前置：将完全匹配原句的文章强行缝合在列表最顶端
	final_ordered_results = exact_match_top + long_tail_fallback
	
	query_obj = Query.get(query_string)
	query_obj.add_hit()
	
	# =========================================================================
	# 4. 黄金缝合：惰性代理返回
	# =========================================================================
	return LazyChainedResultList(final_ordered_results, raw_results, split_point)


def format_search_results_for_api(search_results):
	"""序列化为 JSON 网关输出"""
	results_data = []
	if not search_results: return results_data
	try:
		for page in search_results:
			specific_page = page.specific if hasattr(page, 'specific') else page
			data = {
				'id': page.id,
				'title': page.title,
				'url': page.get_url(),
				'search_description': getattr(page, 'search_description', '') or '',
				'content_type': page.content_type.model,
				'last_published_at': page.last_published_at.strftime(
					'%Y-%m-%d %H:%M') if page.last_published_at else '',
			}
			if isinstance(specific_page, BlogPage):
				data['intro'] = specific_page.intro or ''
				data['date'] = specific_page.date.strftime('%Y-%m-%d') if specific_page.date else ''
				if hasattr(specific_page, 'tags'): data['tags'] = [tag.name for tag in specific_page.tags.all()]
				if hasattr(specific_page, 'categories'): data['categories'] = [cat.name for cat in
				                                                               specific_page.categories.all()]
			results_data.append(data)
	except Exception as e:
		logger.error(f"格式化搜索结果非预期异常: {e}")
	return results_data


def get_search_suggestions(query_string, limit=5):
	"""搜索联想建议"""
	if not query_string or len(query_string) < 2:
		return []
	
	try:
		# 使用你原本写好的优秀聚合逻辑
		suggestions = Query.objects.filter(
			query_string__icontains=query_string
		).annotate(
			# 这里你定义的别名叫 total_hits_count
			total_hits_count=Count('daily_hits')
		).order_by('-total_hits_count')[:limit]
		
		# 构建建议列表
		results = [
			{
				'query': item.query_string,
				# 🎯 核心修复点：将 item.total_hits 改为 item.total_hits_count！
				# 必须和上面 annotate 里的变量名一模一样
				'hits': item.total_hits_count
			}
			for item in suggestions
		]
		
		return results
	
	except Exception as e:
		logger.error(f"获取搜索建议时出错: {e}")
		logger.error(traceback.format_exc())
		return []