# 搜索应用的页面视图
from urllib.parse import urlencode

from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.http import JsonResponse
from django.template.loader import render_to_string
from django.template.response import TemplateResponse
from django.urls import reverse

from .analytics import SearchAnalytics
from .core import perform_search, get_search_suggestions
import logging

logger = logging.getLogger(__name__)
SEARCH_RESULTS_PER_PAGE = 20


def clean_search_param(value):
	"""清理搜索参数，移除None值和'None'字符串"""
	if value in [None, 'None', 'null', '']:
		return None
	return value.strip() if isinstance(value, str) else value


def get_search_results_context(query_params):
	"""Build the shared paginated search context for page and fragment responses."""
	search_query = clean_search_param(query_params.get("query"))
	search_type = query_params.get("type") or "all"
	start_date = clean_search_param(query_params.get("start_date"))
	end_date = clean_search_param(query_params.get("end_date"))
	order_by = clean_search_param(query_params.get("order_by"))
	page = query_params.get("page", 1)

	search_results = None
	if search_query:
		search_results = perform_search(
			search_query,
			search_type,
			start_date=start_date,
			end_date=end_date,
			order_by=order_by,
		)

	# Keep the paginator shape stable for the welcome state and lazy search results.
	if search_results:
		paginator = Paginator(search_results, SEARCH_RESULTS_PER_PAGE)
		try:
			paginated_results = paginator.page(page)
		except PageNotAnInteger:
			paginated_results = paginator.page(1)
		except EmptyPage:
			paginated_results = paginator.page(paginator.num_pages)
	else:
		paginator = Paginator([], SEARCH_RESULTS_PER_PAGE)
		paginated_results = paginator.page(1)

	return {
		"search_query": search_query or "",
		"search_results": paginated_results,
		"search_type": search_type,
		"start_date": start_date or "",
		"end_date": end_date or "",
		"order_by": order_by or "",
	}


def get_search_canonical_url(context):
	"""Return the normalized, server-rendered search URL for browser history."""
	if not context["search_query"]:
		return reverse("search:search")

	params = {
		"query": context["search_query"],
		"type": context["search_type"],
	}
	if context["start_date"]:
		params["start_date"] = context["start_date"]
	if context["end_date"]:
		params["end_date"] = context["end_date"]
	if context["order_by"]:
		params["order_by"] = context["order_by"]
	if context["search_results"].number > 1:
		params["page"] = context["search_results"].number

	return f'{reverse("search:search")}?{urlencode(params)}'


def search(request):
	"""Render the full progressive-enhancement search page."""
	context = get_search_results_context(request.GET)

	# Preserve the existing raw JSON contract for callers of the original endpoint.
	if request.headers.get("X-Requested-With") == "XMLHttpRequest":
		return search_ajax(
			request,
			context["search_results"],
			context["search_query"],
			context["search_type"],
			context["start_date"],
			context["end_date"],
			context["order_by"],
		)

	# Fetching popular queries must not make the page unavailable when analytics fails.
	try:
		context["popular_search_terms"] = SearchAnalytics.get_popular_searches(
			days=30, limit=10
		)
	except Exception as e:
		logger.error(f"获取热门搜索词失败: {e}", exc_info=True)
		context["popular_search_terms"] = []

	if context["search_query"]:
		try:
			SearchAnalytics.log_search(
				context["search_query"],
				results_count=context["search_results"].paginator.count,
				search_type=context["search_type"],
			)
		except Exception as e:
			logger.error(f"记录搜索分析错误: {e}", exc_info=True)

	return TemplateResponse(request, "search/search.html", context)


def search_results_api(request):
	"""Return the server-rendered search-results fragment as JSON."""
	if request.method != "GET":
		response = JsonResponse(
			{
				"ok": False,
				"error": {
					"code": "method_not_allowed",
					"message": "仅支持 GET 请求。",
				},
			},
			status=405,
		)
		response["Allow"] = "GET"
		response["Cache-Control"] = "private, no-store"
		return response

	try:
		context = get_search_results_context(request.GET)
		response = JsonResponse(
			{
				"ok": True,
				"data": {
					"query": context["search_query"],
					"filters": {
						"type": context["search_type"],
						"start_date": context["start_date"],
						"end_date": context["end_date"],
						"order_by": context["order_by"],
					},
					"result_count": context["search_results"].paginator.count,
					"html": render_to_string(
						"search/partials/_search_results.html",
						context,
						request=request,
					),
					"pagination": {
						"page": context["search_results"].number,
						"page_size": SEARCH_RESULTS_PER_PAGE,
						"total_pages": context["search_results"].paginator.num_pages,
						"has_previous": context["search_results"].has_previous(),
						"has_next": context["search_results"].has_next(),
					},
					"canonical_url": get_search_canonical_url(context),
				},
			}
		)
	except Exception as e:
		logger.error(f"搜索结果片段响应错误: {e}", exc_info=True)
		response = JsonResponse(
			{
				"ok": False,
				"error": {
					"code": "search_unavailable",
					"message": "搜索结果暂时无法加载，请稍后重试。",
				},
			},
			status=500,
		)

	response["Cache-Control"] = "private, no-store"
	return response


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
