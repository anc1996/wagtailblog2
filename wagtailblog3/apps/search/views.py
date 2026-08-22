# 搜索应用的页面视图
from urllib.parse import urlencode
from typing import Any, Mapping

from django.conf import settings
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.http import JsonResponse
from django.template.loader import render_to_string
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.translation import get_language

from .analytics import SearchAnalytics
from .core import MAX_RESULT_WINDOW, SearchUnavailableError, get_search_suggestions, perform_search
from .services.content_query import ContentSearchQueryUnavailable, ContentSearchResults
from .services.federated_query import FederatedSearchResults
from .services.cursor import ContentSearchCursorError
import logging

logger = logging.getLogger(__name__)
SEARCH_RESULTS_PER_PAGE = 20


class SearchResultWindowError(ValueError):
	"""请求超出 ES 公开分页窗口时返回的参数错误。"""


class SearchCursorRequestError(ValueError):
	"""公开游标不能验证或不能用于当前查询。"""


def clean_search_param(value: Any) -> Any:
	"""清理搜索参数，移除None值和'None'字符串"""
	if value in [None, 'None', 'null', '']:
		return None
	return value.strip() if isinstance(value, str) else value


def get_search_results_context(
	query_params: Mapping[str, Any],
	locale: str | None = None,
) -> dict[str, Any]:
	"""构造页面、片段和 JSON 接口共用的搜索上下文。

	先校验 offset 窗口和游标适用范围，再调用核心搜索引擎；分页错误不会触发 ES 查询。
	"""
	search_query = clean_search_param(query_params.get("query"))
	search_type = query_params.get("type") or "all"
	start_date = clean_search_param(query_params.get("start_date"))
	end_date = clean_search_param(query_params.get("end_date"))
	order_by = clean_search_param(query_params.get("order_by"))
	cursor = clean_search_param(query_params.get("cursor"))
	page = query_params.get("page", 1)
	try:
		page_number = max(int(page), 1)
	except (TypeError, ValueError):
		page_number = 1
	cursor_requested = bool(cursor)
	cursor_candidate = bool(
		search_query
		and search_type in {"blog", "all"}
		and getattr(settings, "CONTENT_SEARCH_CURSOR_ENABLED", False)
	)
	if search_query and not cursor_candidate and (page_number - 1) * SEARCH_RESULTS_PER_PAGE >= MAX_RESULT_WINDOW:
		raise SearchResultWindowError("搜索结果最多支持前 10000 条")
	if cursor_requested and not cursor_candidate:
		raise SearchCursorRequestError("游标不适用于当前搜索条件")

	search_results = None
	if search_query:
		search_results = perform_search(
			search_query,
			search_type,
			start_date=start_date,
			end_date=end_date,
			order_by=order_by,
		)

	cursor_mode = cursor_candidate and isinstance(search_results, (ContentSearchResults, FederatedSearchResults))
	if cursor_mode:
		try:
			paginated_results = search_results.cursor_page(
				cursor,
				SEARCH_RESULTS_PER_PAGE,
				search_type=search_type,
				locale=locale or get_language() or "",
			)
		except ContentSearchCursorError as error:
			raise SearchCursorRequestError(error.code) from error
	# Keep the paginator shape stable for the welcome state and lazy search results.
	elif search_results:
		paginator = Paginator(search_results, SEARCH_RESULTS_PER_PAGE)
		try:
			paginated_results = paginator.page(page_number)
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
		"cursor": cursor or "",
		"cursor_mode": cursor_mode,
	}


def get_search_canonical_url(context: Mapping[str, Any]) -> str:
	"""返回规范化的服务端搜索 URL，供浏览器历史记录和异步响应复用。"""
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
	if context.get("cursor_mode"):
		if context.get("cursor"):
			params["cursor"] = context["cursor"]
	elif context["search_results"].number > 1:
		params["page"] = context["search_results"].number

	return f'{reverse("search:search")}?{urlencode(params)}'


def get_search_cursor_url(context: Mapping[str, Any], cursor: str) -> str:
	"""构造与当前查询绑定的上一页或下一页地址。"""

	params = {
		"query": context["search_query"],
		"type": context["search_type"],
		"cursor": cursor,
	}
	for key in ("start_date", "end_date", "order_by"):
		if context[key]:
			params[key] = context[key]
	return f'{reverse("search:search")}?{urlencode(params)}'


def add_cursor_navigation(context: dict[str, Any]) -> dict[str, Any]:
	if not context.get("cursor_mode"):
		return context
	context["previous_cursor_url"] = (
		get_search_cursor_url(context, context["search_results"].previous_cursor)
		if context["search_results"].previous_cursor
		else ""
	)
	context["next_cursor_url"] = (
		get_search_cursor_url(context, context["search_results"].next_cursor)
		if context["search_results"].next_cursor
		else ""
	)
	return context


def search(request: Any) -> Any:
	"""渲染完整搜索页面，并兼容传统 AJAX 请求。"""
	try:
		context = add_cursor_navigation(
			get_search_results_context(request.GET, getattr(request, "LANGUAGE_CODE", None))
		)
	except SearchResultWindowError as error:
		return _search_error_response(request, request.GET, str(error), 400)
	except SearchCursorRequestError:
		return _search_error_response(request, request.GET, "搜索游标无效或已过期，请重新搜索。", 400)
	except (SearchUnavailableError, ContentSearchQueryUnavailable):
		return _search_error_response(request, request.GET, "搜索服务暂时不可用，请稍后重试。", 503)

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


def _search_error_response(
	request: Any,
	query_params: Mapping[str, Any],
	message: str,
	status: int,
) -> Any:
	"""让 HTML、传统 AJAX 和异步页面共享稳定的搜索错误语义。"""
	query = clean_search_param(query_params.get("query"))
	if request.headers.get("X-Requested-With") == "XMLHttpRequest":
		code = "result_window_exceeded" if status == 400 else "search_unavailable"
		return JsonResponse({"error": {"code": code, "message": message}}, status=status)
	context = {
		"search_query": query or "",
		"search_results": Paginator([], SEARCH_RESULTS_PER_PAGE).page(1),
		"search_type": query_params.get("type") or "all",
		"start_date": clean_search_param(query_params.get("start_date")) or "",
		"end_date": clean_search_param(query_params.get("end_date")) or "",
		"order_by": clean_search_param(query_params.get("order_by")) or "",
		"cursor": "",
		"cursor_mode": False,
		"search_error": message,
		"popular_search_terms": [],
	}
	return TemplateResponse(request, "search/search.html", context, status=status)


def search_results_api(request: Any) -> JsonResponse:
	"""以 JSON 返回服务端渲染的结果片段和分页元数据。"""
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
		context = add_cursor_navigation(
			get_search_results_context(request.GET, getattr(request, "LANGUAGE_CODE", None))
		)
		pagination = {
			"page_size": SEARCH_RESULTS_PER_PAGE,
			"has_previous": context["search_results"].has_previous(),
			"has_next": context["search_results"].has_next(),
		}
		if context["cursor_mode"]:
			pagination.update(
				{
					"mode": "cursor",
					"previous_cursor": context["search_results"].previous_cursor,
					"next_cursor": context["search_results"].next_cursor,
				}
			)
		else:
			pagination.update(
				{
					"page": context["search_results"].number,
					"total_pages": context["search_results"].paginator.num_pages,
				}
			)
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
					"pagination": pagination,
					"canonical_url": get_search_canonical_url(context),
				},
			}
		)
	except SearchResultWindowError as error:
		response = JsonResponse(
			{"ok": False, "error": {"code": "result_window_exceeded", "message": str(error)}},
			status=400,
		)
	except SearchCursorRequestError:
		response = JsonResponse(
			{"ok": False, "error": {"code": "invalid_cursor", "message": "搜索游标无效或已过期，请重新搜索。"}},
			status=400,
		)
	except SearchUnavailableError:
		response = JsonResponse(
			{"ok": False, "error": {"code": "search_unavailable", "message": "搜索服务暂时不可用，请稍后重试。"}},
			status=503,
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
			status=503,
		)

	response["Cache-Control"] = "private, no-store"
	return response


def search_ajax(
	request: Any,
	search_results: Any,
	search_query: str | None,
	search_type: str | None = None,
	start_date: Any = None,
	end_date: Any = None,
	order_by: str | None = None,
) -> JsonResponse:
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
		
		cursor_mode = bool(getattr(search_results, "cursor_mode", False))
		response_data = {
			'query': search_query or "",
			'results': results_data,
			'has_next': search_results.has_next(),
			'has_previous': search_results.has_previous(),
			'total_count': search_results.paginator.count,
			'current_page': None if cursor_mode else search_results.number,
			'total_pages': None if cursor_mode else search_results.paginator.num_pages,
			'search_type': search_type,
			'start_date': start_date or "",
			'end_date': end_date or "",
			'order_by': order_by or "",
		}
		if cursor_mode:
			response_data.update(
				{
					'pagination_mode': 'cursor',
					'previous_cursor': search_results.previous_cursor,
					'next_cursor': search_results.next_cursor,
				}
			)
		return JsonResponse(response_data)
	except Exception as e:
		logger.error(f"AJAX搜索响应错误: {e}", exc_info=True)
		return JsonResponse({
			'error': {
				'code': 'search_unavailable',
				'message': '搜索服务暂时不可用，请稍后重试。',
			},
			'query': search_query or "",
			'results': []
		}, status=503)


def search_suggestions(request: Any) -> JsonResponse:
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
