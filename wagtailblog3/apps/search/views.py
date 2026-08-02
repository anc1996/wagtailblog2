# 搜索应用的页面视图
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
	search_type = request.GET.get("type", "all")  # 可选：全部、博客或普通页面
	
	# 获取新增参数并清理
	start_date = clean_search_param(request.GET.get("start_date", None))
	end_date = clean_search_param(request.GET.get("end_date", None))
	order_by = clean_search_param(request.GET.get("order_by", None))
	
	# 清理搜索查询
	search_query = clean_search_param(search_query)
	
	# 预留片段缓存入口；当前页面仍由核心搜索结果直接渲染。
	if search_query:
		cache_key = SearchCache.get_cache_key(
			search_query, search_type, page, start_date, end_date, order_by
		)
		cached_html = None  # 使用片段缓存，可选功能
	
	# 仅在有有效关键词时访问搜索后端。
	search_results = None
	if search_query:
		search_results = perform_search(
			search_query,
			search_type,
			start_date=start_date,
			end_date=end_date,
			order_by=order_by
		)
	
	# 分页对象同时兼容惰性搜索结果和空结果，保证模板始终有稳定结构。
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
	
	# 异步请求只返回结果片段，供无限滚动或动态加载使用。
	if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
		return search_ajax(request, paginated_results, search_query, search_type, start_date, end_date, order_by)
	
	# 获取最近 30 天的热门搜索词，分析失败时不影响搜索页面。
	try:
		popular_search_terms_list = SearchAnalytics.get_popular_searches(days=30, limit=10)
	except Exception as e:
		logger.error(f"获取热门搜索词失败: {e}", exc_info=True)
		popular_search_terms_list = []
	
	# 只有真正执行了关键词搜索才记录统计，避免空页面污染数据。
	if search_query:
		try:
			SearchAnalytics.log_search(
				search_query,
				results_count=paginator.count if hasattr(paginator, 'count') else 0,
				search_type=search_type
			)
		except Exception as e:
			logger.error(f"记录搜索分析错误: {e}", exc_info=True)
	
	# 向模板传递已清理的值，避免把空参数显示成 None。
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
		logger.error(f"AJAX搜索响应错误: {e}", exc_info=True)
		return JsonResponse({
			'error': f"搜索处理错误: {str(e)}",
			'query': search_query or "",
			'results': []
		}, status=500)


def search_suggestions(request):
	"""
	【架构师级：传统 AJAX 搜索建议端点】
	与 REST API 保持 100% 架构对齐，统一调用 core 层的降维联想引擎。
	"""
	# 1. 获取并清理参数
	query = request.GET.get('q', '')
	query = clean_search_param(query)
	
	# 2. 视图层硬核防波堤：防止单字符击穿数据库
	if not query or len(query) < 2:
		return JsonResponse({'suggestions': []})
	
	# 3. 核心业务下推
	try:
		# 统一调用核心模块的搜索建议聚合逻辑。
		suggestions = get_search_suggestions(query)
		return JsonResponse({'suggestions': suggestions})
	
	except Exception as e:
		# 搜索建议失败时记录日志并返回空列表，避免影响主搜索页面。
		logger.error(f"传统 AJAX 获取搜索建议时发生错误: {e}", exc_info=True)
		return JsonResponse({'suggestions': []})
