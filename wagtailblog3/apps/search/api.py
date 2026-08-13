# 搜索应用的接口视图
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.conf import settings
from .core import MAX_RESULT_WINDOW, SearchUnavailableError, format_search_results_for_api, get_search_suggestions, perform_search
from .cache import SearchCache
from .services.content_query import ContentSearchResults
from .services.federated_query import FederatedSearchResults
from .services.cursor import ContentSearchCursorError
import logging

logger = logging.getLogger(__name__)


def clean_search_param(value):
	if value in [None, 'None', 'null', '']:
		return None
	return value.strip() if isinstance(value, str) else value


@api_view(['GET'])
def search_api(request):
	"""
	面向前端 jQuery 或现代化网关的 REST 搜索入口
	"""
	query = request.GET.get('q', '')
	search_type = request.GET.get('type', 'all')
	cursor = clean_search_param(request.GET.get('cursor'))
	query_for_mode = clean_search_param(query)
	cursor_mode = bool(
		query_for_mode
		and search_type in {'blog', 'all'}
		and getattr(settings, 'CONTENT_SEARCH_CURSOR_ENABLED', False)
	)
	try:
		page = int(request.GET.get('page', 1))
		per_page = int(request.GET.get('per_page', 10))
	except (TypeError, ValueError):
		return Response({'error': {'code': 'invalid_pagination', 'message': '分页参数无效。'}}, status=400)
	if page < 1 or not 1 <= per_page <= 100:
		return Response({'error': {'code': 'invalid_pagination', 'message': '分页参数无效。'}}, status=400)
	start = (page - 1) * per_page
	if not cursor_mode and (start >= MAX_RESULT_WINDOW or start + per_page > MAX_RESULT_WINDOW):
		return Response({'error': {'code': 'result_window_exceeded', 'message': '搜索结果最多支持前 10000 条。'}}, status=400)
	
	start_date = clean_search_param(request.GET.get('start_date', None))
	end_date = clean_search_param(request.GET.get('end_date', None))
	order_by = clean_search_param(request.GET.get('order_by', None))
	query = clean_search_param(query)
	if cursor and not cursor_mode:
		return Response({'error': {'code': 'invalid_cursor', 'message': '搜索游标不适用于当前查询。'}}, status=400)
	
	if not query:
		return Response({
			'query': '', 'total': 0, 'page': page, 'per_page': per_page, 'results': []
		})
	
	# 游标携带查询边界和排序锚点，不能复用按页码缓存。
	cached_results = None if cursor_mode else SearchCache.get_cached_results(
		query, search_type, page, start_date, end_date, order_by
	)
	if cached_results:
		logger.info(f"命中 Elasticsearch 8 上游业务缓存，返回数据: '{query}'")
		return Response(cached_results)
	
	try:
		# 2. 在公开 QuerySet 范围内构造并执行搜索。
		search_results = perform_search(
			query, search_type,
			start_date=start_date,
			end_date=end_date,
			order_by=order_by
		)
		
		if cursor_mode and isinstance(search_results, (ContentSearchResults, FederatedSearchResults)):
			paginated_results = search_results.cursor_page(
				cursor, per_page, search_type=search_type,
				locale=getattr(request, 'LANGUAGE_CODE', ''),
			)
			total_count = paginated_results.paginator.count
		else:
			# 旧 page= 协议继续受窗口限制，并复用既有缓存。
			total_count = search_results.count()
			paginated_results = search_results[start:start + per_page]
		
		# 4. 转换为接口格式并写入缓存，供相同条件的请求复用。
		results_data = format_search_results_for_api(paginated_results)
		
		data = {
			'query': query,
			'total': total_count,
			'page': None if cursor_mode else page,
			'per_page': per_page,
			'start_date': start_date or "",
			'end_date': end_date or "",
			'order_by': order_by or "",
			'results': results_data
		}
		if cursor_mode:
			data['pagination'] = {
				'mode': 'cursor',
				'has_previous': paginated_results.has_previous(),
				'has_next': paginated_results.has_next(),
				'previous_cursor': paginated_results.previous_cursor,
				'next_cursor': paginated_results.next_cursor,
			}
		
		SearchCache.set_cached_results(
			query, data, search_type, page, start_date, end_date, order_by
		)
		
		return Response(data)
	except SearchUnavailableError:
		logger.error("API 搜索后端不可用", exc_info=True)
		return Response({'error': {'code': 'search_unavailable', 'message': '搜索服务暂时不可用，请稍后重试。'}}, status=503)
	except ContentSearchCursorError as error:
		return Response({'error': {'code': error.code, 'message': '搜索游标无效或已过期，请重新搜索。'}}, status=400)
	except Exception as e:
		logger.error(f"API搜索网关层发生故障: {e}", exc_info=True)
		return Response({'error': {'code': 'search_unavailable', 'message': '搜索服务暂时不可用，请稍后重试。'}}, status=503)


@api_view(['GET'])
def search_suggestions_api(request):
	"""
	REST API 搜索建议端点 (用于前端搜索框下拉联想)
	"""
	query = request.GET.get('q', '')
	query = clean_search_param(query)
	
	# 如果搜索词为空或者太短，直接返回空列表
	if not query or len(query) < 2:
		return Response({'suggestions': []})
	
	try:
		# 调用核心模块统一的搜索建议逻辑。
		suggestions = get_search_suggestions(query)
		return Response({'suggestions': suggestions})
	except Exception as e:
		logger.error(f"获取搜索建议时发生错误: {e}", exc_info=True)
		return Response({'suggestions': []})
