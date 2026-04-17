# search/views.py
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.template.response import TemplateResponse
from django.http import JsonResponse
from .analytics import SearchAnalytics
from .core import perform_search, get_search_suggestions
from .cache import SearchCache
import logging

logger = logging.getLogger(__name__)


def clean_search_param(value):
	"""清理搜索参数，移除None值和'None'字符串"""
	if value in [None, 'None', 'null', '']:
		return None
	return value.strip() if isinstance(value, str) else value


def search(request):
	"""搜索视图"""
	# 获取参数
	search_query = request.GET.get("query", None)
	page = request.GET.get("page", 1)
	search_type = request.GET.get("type", "all")  # all, blog, pages
	
	# 获取新增参数并清理
	start_date = clean_search_param(request.GET.get("start_date", None))
	end_date = clean_search_param(request.GET.get("end_date", None))
	order_by = clean_search_param(request.GET.get("order_by", None))
	
	# 清理搜索查询
	search_query = clean_search_param(search_query)
	
	# 尝试获取缓存结果
	if search_query:
		cache_key = SearchCache.get_cache_key(
			search_query, search_type, page, start_date, end_date, order_by
		)
		cached_html = None  # 使用片段缓存，可选功能
	
	# 执行搜索
	search_results = None
	if search_query:
		search_results = perform_search(
			search_query,
			search_type,
			start_date=start_date,
			end_date=end_date,
			order_by=order_by
		)
	
	# 分页处理
	if search_results:
		paginator = Paginator(search_results, 20)  # 每页20条
		try:
			paginated_results = paginator.page(page)
		except PageNotAnInteger:
			paginated_results = paginator.page(1)
		except EmptyPage:
			paginated_results = paginator.page(paginator.num_pages)
	else:
		# 创建空的分页对象以避免模板错误
		paginator = Paginator([], 20)
		paginated_results = paginator.page(1)
	
	# 如果是AJAX请求（例如，用于无限滚动或动态加载），则返回JSON
	if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
		return search_ajax(request, paginated_results, search_query, search_type, start_date, end_date, order_by)
	
	# 获取热门搜索词 (例如最近30天的前10个)
	try:
		popular_search_terms_list = SearchAnalytics.get_popular_searches(days=30, limit=10)
	except Exception as e:
		logger.error(f"获取热门搜索词失败: {e}")
		popular_search_terms_list = []
	
	# 记录搜索分析 (只有在实际执行搜索时)
	if search_query:
		try:
			SearchAnalytics.log_search(
				search_query,
				results_count=paginator.count if hasattr(paginator, 'count') else 0,
				search_type=search_type
			)
		except Exception as e:
			logger.error(f"记录搜索分析错误: {e}")
	
	# 确保传递给模板的值是清理过的，避免显示None
	context = {
		"search_query": search_query or "",
		"search_results": paginated_results,
		"search_type": search_type,
		"popular_search_terms": popular_search_terms_list,  # 热门搜索词
		"start_date": start_date or "",
		"end_date": end_date or "",
		"order_by": order_by or "",
	}
	return TemplateResponse(
		request,
		"search/search.html",
		context,
	)


def search_ajax(request, search_results, search_query, search_type=None, start_date=None, end_date=None, order_by=None):
	"""AJAX搜索响应"""
	try:
		# 清理参数
		start_date = clean_search_param(start_date)
		end_date = clean_search_param(end_date)
		order_by = clean_search_param(order_by)
		search_query = clean_search_param(search_query)
		
		# 使用核心模块的格式化方法
		from .core import format_search_results_for_api
		results_data = format_search_results_for_api(search_results.object_list)
		
		response_data = {
			'query': search_query or "",
			'results': results_data,
			'has_next': search_results.has_next(),
			'has_previous': search_results.has_previous(),
			'total_count': search_results.paginator.count,
			'current_page': search_results.number,
			'total_pages': search_results.paginator.num_pages,
			'search_type': search_type,
			'start_date': start_date or "",
			'end_date': end_date or "",
			'order_by': order_by or "",
		}
		return JsonResponse(response_data)
	except Exception as e:
		logger.error(f"AJAX搜索响应错误: {e}")
		return JsonResponse({
			'error': f"搜索处理错误: {str(e)}",
			'query': search_query or "",
			'results': []
		}, status=500)


def search_suggestions(request):
	"""搜索建议API"""
	query = clean_search_param(request.GET.get('q', ''))
	
	# 获取搜索建议
	try:
		suggestions = get_search_suggestions(query)
		
		# 返回结果
		return JsonResponse({'suggestions': suggestions})
	except Exception as e:
		logger.error(f"获取搜索建议错误: {e}")
		return JsonResponse({
			'error': f"获取搜索建议错误: {str(e)}",
			'suggestions': []
		}, status=500)