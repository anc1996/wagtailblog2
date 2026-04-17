# search/core.py
from wagtail.models import Page
from blog.models import BlogPage
from wagtail.contrib.search_promotions.models import Query
from django.db.models import Count, QuerySet
import logging, traceback
import time
from datetime import datetime

logger = logging.getLogger(__name__)


def perform_search(query_string, search_type='all', page_number=1, per_page=10,
                   start_date=None, end_date=None, order_by=None):
	"""
	执行搜索并返回结果

	参数:
		query_string: 搜索关键词
		search_type: 搜索类型 (all, blog, pages)
		page_number: 页码
		per_page: 每页结果数
		start_date: 开始日期 (格式: YYYY-MM-DD)
		end_date: 结束日期 (格式: YYYY-MM-DD)
		order_by: 排序方式 ('date', '-date', 'relevance')

	返回:
		搜索结果对象 (QuerySet)
	"""
	
	# 记录执行开始时间
	start_time = time.time()
	
	# 初始化空结果
	search_results = Page.objects.none()
	
	if query_string:
		# 记录搜索查询（用于搜索推广功能）
		query = Query.get(query_string)
		query.add_hit()
		
		try:
			# 根据搜索类型选择不同的查询
			if search_type == "blog":
				# 只搜索博客文章
				search_results = BlogPage.objects.live().public().search(query_string)
				logger.debug(f"博客搜索初始结果数: {search_results.count()}")
			elif search_type == "pages":
				# 只搜索普通页面（排除博客）
				search_results = Page.objects.live().public().exclude(
					id__in=BlogPage.objects.values_list('id', flat=True)
				).search(query_string)
				logger.debug(f"页面搜索初始结果数: {search_results.count()}")
			else:
				# 搜索所有内容
				search_results = Page.objects.live().public().search(query_string)
				logger.debug(f"全部搜索初始结果数: {search_results.count()}")
			
			# 确保结果是QuerySet或列表，便于后续处理
			if not isinstance(search_results, (QuerySet, list)):
				logger.warning(f"搜索结果类型异常: {type(search_results)}")
				ids = []
				for item in search_results:
					if hasattr(item, 'id'):
						ids.append(item.id)
				search_results = Page.objects.filter(id__in=ids)
			
			# 应用日期范围过滤 - 分离处理BlogPage与普通Page
			if start_date or end_date:
				logger.debug(f"应用日期过滤: 开始={start_date}, 结束={end_date}")
				
				# 转换日期字符串为日期对象(如果是字符串)
				if start_date and isinstance(start_date, str):
					try:
						start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
					except ValueError:
						logger.error(f"开始日期格式错误: {start_date}")
						start_date = None
				
				if end_date and isinstance(end_date, str):
					try:
						end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
					except ValueError:
						logger.error(f"结束日期格式错误: {end_date}")
						end_date = None
				
				# 分离博客页面和非博客页面
				if isinstance(search_results, QuerySet):
					# 获取所有结果的ID
					result_ids = list(search_results.values_list('id', flat=True))
					
					# 博客页面过滤
					blog_query = BlogPage.objects.filter(id__in=result_ids)
					if start_date:
						blog_query = blog_query.filter(date__gte=start_date)
					if end_date:
						blog_query = blog_query.filter(date__lte=end_date)
					
					filtered_blog_ids = list(blog_query.values_list('id', flat=True))
					
					# 非博客页面保持不变
					non_blog_ids = list(Page.objects.filter(id__in=result_ids)
					                    .exclude(id__in=BlogPage.objects.values_list('id', flat=True))
					                    .values_list('id', flat=True))
					
					# 合并IDs
					final_ids = filtered_blog_ids + non_blog_ids
					
					# 重建结果集
					search_results = Page.objects.filter(id__in=final_ids)
					logger.debug(f"日期过滤后结果数: {search_results.count()}")
			
			# 应用排序逻辑
			if order_by:
				logger.debug(f"应用排序: {order_by}")
				
				if order_by == '-date':
					# 分离博客页面和非博客页面
					if isinstance(search_results, QuerySet):
						# 获取所有结果的ID
						result_ids = list(search_results.values_list('id', flat=True))
						
						# 按date降序排列博客页面
						blog_results = list(BlogPage.objects.filter(id__in=result_ids)
						                    .order_by('-date')
						                    .values_list('id', flat=True))
						
						# 按first_published_at降序排列非博客页面
						non_blog_results = list(Page.objects.filter(id__in=result_ids)
						                        .exclude(id__in=BlogPage.objects.values_list('id', flat=True))
						                        .order_by('-first_published_at')
						                        .values_list('id', flat=True))
						
						# 合并排序列表 (保持博客优先)
						ordered_ids = blog_results + non_blog_results
						
						# 使用Case-When保持顺序
						from django.db.models import Case, When, IntegerField
						preserved_order = Case(
							*[When(id=pk, then=pos) for pos, pk in enumerate(ordered_ids)],
							output_field=IntegerField()
						)
						search_results = Page.objects.filter(id__in=ordered_ids).order_by(preserved_order)
				
				elif order_by == 'date':
					# 与上面类似，但使用升序
					if isinstance(search_results, QuerySet):
						result_ids = list(search_results.values_list('id', flat=True))
						
						blog_results = list(BlogPage.objects.filter(id__in=result_ids)
						                    .order_by('date')
						                    .values_list('id', flat=True))
						
						non_blog_results = list(Page.objects.filter(id__in=result_ids)
						                        .exclude(id__in=BlogPage.objects.values_list('id', flat=True))
						                        .order_by('first_published_at')
						                        .values_list('id', flat=True))
						
						ordered_ids = blog_results + non_blog_results
						
						from django.db.models import Case, When, IntegerField
						preserved_order = Case(
							*[When(id=pk, then=pos) for pos, pk in enumerate(ordered_ids)],
							output_field=IntegerField()
						)
						search_results = Page.objects.filter(id__in=ordered_ids).order_by(preserved_order)
		
		except Exception as e:
			logger.error(f"搜索出错: {e}")
			logger.error(traceback.format_exc())
			# 发生错误时返回空结果
			search_results = Page.objects.none()
	
	# 记录执行耗时
	execution_time = time.time() - start_time
	logger.info(f"搜索查询 '{query_string}' 耗时: {execution_time:.3f}秒 " +
	            f"(过滤器: type={search_type}, start_date={start_date}, " +
	            f"end_date={end_date}, order_by={order_by})")
	
	return search_results


def format_search_results_for_api(search_results):
	"""
	将搜索结果格式化为API响应格式

	参数:
		search_results: 搜索结果对象

	返回:
		API格式的结果列表
	"""
	results_data = []
	
	# 确保search_results是可迭代的
	if search_results is None:
		return results_data
	
	try:
		for result in search_results:
			# 创建基本信息字典
			data = {
				'id': result.id,
				'title': result.title,
				'url': result.url,
				'type': result._meta.model_name,
			}
			
			# 处理特定模型的字段
			specific_page = result.specific
			
			# 特别处理BlogPage类型
			if hasattr(specific_page, 'date'):
				data['date'] = specific_page.date.isoformat() if specific_page.date else None
			
			# 添加简介信息
			if hasattr(specific_page, 'intro'):
				data['intro'] = specific_page.intro
			
			# 添加分类信息（如果有）
			if hasattr(specific_page, 'categories') and hasattr(specific_page.categories, 'all'):
				categories = specific_page.categories.all()
				if categories:
					data['categories'] = [
						{'id': cat.id, 'name': cat.name, 'slug': cat.slug}
						for cat in categories
					]
			
			# 添加标签信息（如果有）
			if hasattr(specific_page, 'tags') and hasattr(specific_page.tags, 'all'):
				tags = specific_page.tags.all()
				if tags:
					data['tags'] = [tag.name for tag in tags]
			
			# 添加特色图片信息（如果有）
			if hasattr(specific_page, 'featured_image') and specific_page.featured_image:
				data['featured_image'] = {
					'id': specific_page.featured_image.id,
					'title': specific_page.featured_image.title,
					'url': specific_page.featured_image.file.url if hasattr(specific_page.featured_image,
					                                                        'file') else None
				}
			
			# 添加作者信息（如果有）
			if hasattr(specific_page, 'authors') and hasattr(specific_page.authors, 'all'):
				authors = specific_page.authors.all()
				if authors:
					data['authors'] = [
						{'id': author.id, 'name': author.name}
						for author in authors
					]
			
			results_data.append(data)
	except Exception as e:
		logger.error(f"格式化搜索结果时出错: {e}")
		logger.error(traceback.format_exc())
	
	return results_data


def get_search_suggestions(query_string, limit=5):
	"""
	获取搜索建议列表

	参数:
		query_string: 搜索关键词前缀
		limit: 最大返回数量

	返回:
		搜索建议列表
	"""
	if not query_string or len(query_string) < 2:
		return []
	
	# 获取包含查询字符串的搜索记录，按点击量排序
	try:
		from wagtail.contrib.search_promotions.models import Query
		# 使用 daily_hits 聚合得到总点击量
		suggestions = Query.objects.filter(
			query_string__icontains=query_string
		).annotate(
			total_hits=Count('daily_hits')
		).order_by('-total_hits')[:limit]
		
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