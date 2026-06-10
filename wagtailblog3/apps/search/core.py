# apps/search/core.py
from wagtail.models import Page
from blog.models import BlogPage
from wagtail.contrib.search_promotions.models import Query
from django.db.models import Count
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def _parse_date(date_val):
	"""安全解析日期格式"""
	if date_val and isinstance(date_val, str):
		try:
			return datetime.strptime(date_val, '%Y-%m-%d').date()
		except ValueError:
			return None # 或返回 date_val 本身
	return date_val


def perform_search(query_string, search_type='all', start_date=None, end_date=None, order_by=None):
	"""
    【重构核心】基于 Elasticsearch 8 原生分词与下推路由的单轨搜索引擎
    四大核心前端参数 (query, type, start_date/end_date, order_by) 一体化编译。
    """
	parsed_start = _parse_date(start_date)
	parsed_end = _parse_date(end_date)
	
	# 1. 编译 search_type 隔离带
	if search_type == 'blog':
		# 博客单轨：仅查询 BlogPage 节点
		qs = BlogPage.objects.live().public()
		
		# 博客日期过滤：锁定专属业务 FilterField -> date
		if parsed_start:
			qs = qs.filter(date__gte=parsed_start)
		if parsed_end:
			qs = qs.filter(date__lte=parsed_end)
		
		# 博客时间轴排序
		if order_by == 'date':
			qs = qs.order_by('date')
		elif order_by == '-date':
			qs = qs.order_by('-date')
	
	elif search_type == 'pages':
		# 页面单轨：剔除所有 BlogPage 的 ID，只留下普通页面
		blog_ids = BlogPage.objects.values_list('id', flat=True)
		qs = Page.objects.live().public().exclude(id__in=blog_ids)
		
		# 普通页面日期过滤：锁定 Wagtail 原生自带的最后发布时间 -> last_published_at
		if parsed_start:
			qs = qs.filter(last_published_at__gte=parsed_start)
		if parsed_end:
			qs = qs.filter(last_published_at__lte=parsed_end)
		
		# 普通页面时间轴排序
		if order_by == 'date':
			qs = qs.order_by('last_published_at')
		elif order_by == '-date':
			qs = qs.order_by('-last_published_at')
	
	else:
		# 全站混合检索单轨 (all)
		qs = Page.objects.live().public()
		
		# 混合检索全局日期过滤：统一折叠到基类起步时间 -> first_published_at
		if parsed_start:
			qs = qs.filter(first_published_at__gte=parsed_start)
		if parsed_end:
			qs = qs.filter(first_published_at__lte=parsed_end)
		
		# 全站时间轴排序
		if order_by == 'date':
			qs = qs.order_by('first_published_at')
		elif order_by == '-date':
			qs = qs.order_by('-first_published_at')
	
	# 2. 触发 Elasticsearch 8 引擎终极检索
	if query_string:
		# 💡 架构师核心修复：检测用户是否启用了显式的时间轴排序（date 或 -date）
		# 如果启用了自定义排序，则将 order_by_relevance 设为 False，强迫 ES 尊重 QuerySet 的 order_by() 规则
		use_relevance = order_by not in ['date', '-date']
		
		# 核心绝杀：传入 order_by_relevance 参数，打通 ES 的真实排序通道
		search_results = qs.search(query_string, order_by_relevance=use_relevance)
		
		# 记录搜索词点击日志（Wagtail 内置统计，用于下拉搜索建议，保持原状）
		query_obj = Query.get(query_string)
		query_obj.add_hit()
		
		return search_results
	
	# 如果用户没有输入关键词，退回普通的 Django 结果集包装，由原生 QuerySet 承载
	return qs


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