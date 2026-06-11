# apps/search/core.py
import re

from bson import ObjectId
from django.utils.html import strip_tags
from wagtail.models import Page
from blog.models import BlogPage
from wagtail.contrib.search_promotions.models import Query
from django.db.models import Count
import logging
from datetime import datetime
from django.db.models import Case, When  # <-- 引入高效的条件外键排序构造器

from wagtailblog3.mongo import MongoManager

logger = logging.getLogger(__name__)


class LazyChainedResultList:
	"""
	【架构师级：惰性结果拼接器】
	解决 1亿 级深度分页的终极利器。
	它欺骗 Django 分页器 (Paginator) 伪装成一个完整的千万级列表。
	在 Top N (如前 200) 内直接从内存极速返回，超过 Top N 的深度翻页，实时下推给 ES 引擎！
	"""
	
	def __init__(self, head_list, tail_query_set, split_point):
		self.head_list = head_list  # 内存中精排好的核心数据 (例如前 200 条)
		self.tail_query_set = tail_query_set  # 挂载着 ES 引擎的 Wagtail 惰性查询集
		self.split_point = split_point  # 分界线 (例如 200)
		
		# 极速获取总命中数，ES 会利用 _count 瞬间返回，绝不加载真实数据
		self._count = len(tail_query_set) if hasattr(tail_query_set, '__len__') else tail_query_set.count()
	
	def count(self):
		"""兼容 Django Paginator 的 count() 方法"""
		return self._count
	
	def __len__(self):
		"""兼容 len() 方法"""
		return self._count
	
	def _get_specific_slice(self, start, stop):
		"""
		【零 N+1 切片引擎】
		负责接管第 201 条之后的数据请求。
		利用单次 ES 请求 + 单次 MySQL IN 查询，彻底杜绝深度翻页卡顿！
		"""
		# 触发 ES 深度分页查询 (例如 LIMIT 20 OFFSET 5000)
		raw_slice = list(self.tail_query_set[start:stop])
		if not raw_slice:
			return []
		
		# 提取这 20 个结果的 ID，使用一次 SQL 拿到真实的 Specific 子类对象
		page_ids = [p.pk for p in raw_slice]
		preserved_order = Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(page_ids)])
		# 只查这 20 个 ID，没有任何多余的数据库操作！
		return list(Page.objects.filter(pk__in=page_ids).specific().order_by(preserved_order))
	
	def __getitem__(self, k):
		"""核心魔法：拦截分页器的数组切片操作 (例如 paginator 取第 [5000:5020] 条)"""
		if isinstance(k, slice):
			start = k.start or 0
			stop = k.stop if k.stop is not None else self._count
			
			if stop <= self.split_point:
				# Paginator 请求的是前几页，直接从内存的高速 head_list 无损返回
				return self.head_list[start:stop]
			elif start >= self.split_point:
				# 🎯 深度翻页发生 (比如第 5001 篇)！把切片下推给 ES！
				return self._get_specific_slice(start, stop)
			else:
				# 跨界请求（刚好跨过第 200 条的边界）
				head_part = self.head_list[start:self.split_point]
				tail_part = self._get_specific_slice(self.split_point, stop)
				return head_part + tail_part
		else:
			# 单个索引取值处理
			if k < self.split_point:
				return self.head_list[k]
			else:
				return self._get_specific_slice(k, k + 1)[0]


def _parse_date(date_val):
	"""安全解析日期格式"""
	if date_val and isinstance(date_val, str):
		try:
			return datetime.strptime(date_val, '%Y-%m-%d').date()
		except ValueError:
			return None
	return date_val


def perform_search(query_string, search_type='all', start_date=None, end_date=None, order_by=None):
	"""
	【企业级核心搜索引擎入口】
	"""
	parsed_start = _parse_date(start_date)
	parsed_end = _parse_date(end_date)
	
	# =========================================================================
	# 1. 基础条件网构建 (排序必须提前应用)
	# =========================================================================
	if search_type == 'blog':
		qs = BlogPage.objects.live().public()
		if parsed_start: qs = qs.filter(date__gte=parsed_start)
		if parsed_end: qs = qs.filter(date__lte=parsed_end)
		if order_by == 'date':
			qs = qs.order_by('date')
		elif order_by == '-date':
			qs = qs.order_by('-date')
	
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
	
	# 发射检索指令。如果 use_relevance=False，ES 会严格遵循上面的时间线排序
	raw_results = qs.search(clean_query, order_by_relevance=use_relevance)
	
	if not use_relevance:
		query_obj = Query.get(query_string)
		query_obj.add_hit() # 搜索词点击量统计
		return raw_results
	
	# =========================================================================
	# 3. 内存极速重排 (L2 Rescoring) - 仅限 Top 200！
	# =========================================================================
	top_pages = list(raw_results[:200])
	split_point = len(top_pages)  # 记录真实的物理分界线
	
	if not top_pages:
		return []
	
	# 零 N+1 获取前 200 篇文章的实体与大文本
	page_ids = [p.pk for p in top_pages]
	preserved_order = Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(page_ids)])
	specific_pages = list(Page.objects.filter(pk__in=page_ids).specific().order_by(preserved_order))
	
	# [消除 Mongo N+1] 单次批量拉出所有的正文
	valid_object_ids = []
	for page in specific_pages:
		if isinstance(page, BlogPage):
			# 先安全获取字符串指针
			mongo_id_str = getattr(page, 'mongo_content_id', None)
			if mongo_id_str:
				try:
					valid_object_ids.append(ObjectId(mongo_id_str))
				except Exception as e:
					# 🚨 替换掉原来的 pass，加入精准的监控日志
					logger.warning(
						f"⚠️ 脏数据警告: 发现无效的 MongoDB ID 指针跳过查询。 "
						f"MySQL 页面 ID: [{page.id}] | "
						f"异常 Mongo ID: [{mongo_id_str}] | "
						f"异常原因: {str(e)}"
					)
	
	mongo_contents = {}
	if valid_object_ids:
		mongo_manager = MongoManager()
		cursor = mongo_manager.blog_content.find({'_id': {'$in': valid_object_ids}})
		for doc in cursor:
			mongo_contents[str(doc['_id'])] = doc
	
	exact_match_top = []
	long_tail_fallback = []
	
	for page in specific_pages:
		search_text_pool = ""
		if page.title: search_text_pool += page.title + " "
		
		if isinstance(page, BlogPage):
			if page.intro: search_text_pool += strip_tags(str(page.intro)) + " "
			
			mid = getattr(page, 'mongo_content_id', None)
			if mid and mid in mongo_contents:
				body_data = mongo_contents[mid].get('body', [])
				text_parts = []
				for block in body_data:
					if isinstance(block, dict) and block.get('value'):
						val = block['value']
						if isinstance(val, str):
							text_parts.append(strip_tags(val))
						elif isinstance(val, dict):
							text_parts.append(str(val.get('code', '')))
				search_text_pool += " ".join(text_parts)
		
		# 精准提取：连贯长句保送榜首！
		if clean_query.lower() in search_text_pool.lower():
			exact_match_top.append(page)
		else:
			long_tail_fallback.append(page)
	
	final_ordered_results = exact_match_top + long_tail_fallback
	
	query_obj = Query.get(query_string) # 搜索词点击量统计
	query_obj.add_hit()
	
	# =========================================================================
	# 4. 黄金缝合：将前 200 名完美拼接上底层的海量长尾 ES 数据！
	# =========================================================================
	return LazyChainedResultList(final_ordered_results, raw_results, split_point)


def format_search_results_for_api(search_results):
	"""
    将搜索结果页序列化为标准干净的 JSON。
    由于 ES8 返回的对象序列可以直接通过 .specific 拿到具体模型，这里逻辑完全自洽，无需改动。
    """
	results_data = []
	if not search_results:
		return results_data
	
	try:
		for page in search_results:
			# 动态向上转型，拿到子类（如 HomePage 或 BlogPage）的特有属性
			specific_page = page.specific if hasattr(page, 'specific') else page
			
			# 基础公共参数组装
			data = {
				'id': page.id,
				'title': page.title,
				'url': page.get_url(),
				'search_description': getattr(page, 'search_description', '') or '',
				'content_type': page.content_type.model,
				'last_published_at': page.last_published_at.strftime(
					'%Y-%m-%d %H:%M') if page.last_published_at else '',
			}
			
			# 博客专属数据填充
			if isinstance(specific_page, BlogPage):
				data['intro'] = specific_page.intro or ''
				data['date'] = specific_page.date.strftime('%Y-%m-%d') if specific_page.date else ''
				
				if hasattr(specific_page, 'tags'):
					data['tags'] = [tag.name for tag in specific_page.tags.all()]
				if hasattr(specific_page, 'categories'):
					data['categories'] = [cat.name for cat in specific_page.categories.all()]
				if hasattr(specific_page, 'authors'):
					data['authors'] = [{'id': a.id, 'name': a.name} for a in specific_page.authors.all()]
				if hasattr(specific_page, 'featured_image') and specific_page.featured_image:
					data['featured_image'] = {
						'id': specific_page.featured_image.id,
						'title': specific_page.featured_image.title,
						'url': specific_page.featured_image.file.url if hasattr(specific_page.featured_image,
						                                                        'file') else None
					}
			
			results_data.append(data)
	except Exception as e:
		logger.error(f"格式化搜索结果时发生非预期异常: {e}")
	return results_data


def get_search_suggestions(query_string, limit=5):
	"""搜索建议提示，直接基于搜索记录模型进行 icontains 统计，保持不动"""
	if not query_string or len(query_string) < 2:
		return []
	try:
		suggestions = Query.objects.filter(
			query_string__icontains=query_string
		).annotate(
			total_hits_count=Count('daily_hits')
		).order_by('-total_hits_count')[:limit]
		
		# 构建建议列表
		results = [
			{
				'query': item.query_string,
				'hits': item.total_hits  # 使用聚合的 total_hits
			}
			for item in suggestions
		]
		
		return results
	except Exception as e:
		logger.error(f"获取搜索建议时出错: {e}")
		return []