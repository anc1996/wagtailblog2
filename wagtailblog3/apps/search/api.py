# apps/search/api.py
from rest_framework.decorators import api_view
from rest_framework.response import Response
from .core import perform_search, format_search_results_for_api, get_search_suggestions
from .cache import SearchCache
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
	page = int(request.GET.get('page', 1))
	per_page = int(request.GET.get('per_page', 10))
	
	start_date = clean_search_param(request.GET.get('start_date', None))
	end_date = clean_search_param(request.GET.get('end_date', None))
	order_by = clean_search_param(request.GET.get('order_by', None))
	query = clean_search_param(query)
	
	if not query:
		return Response({
			'query': '', 'total': 0, 'page': page, 'per_page': per_page, 'results': []
		})
	
	# 1. 校验并读取 Redis 缓存
	cached_results = SearchCache.get_cached_results(
		query, search_type, page, start_date, end_date, order_by
	)
	if cached_results:
		logger.info(f"命中 Elasticsearch 8 上游业务缓存，返回数据: '{query}'")
		return Response(cached_results)
	
	try:
		# 2. 触发原生 ES 编译检索
		search_results = perform_search(
			query, search_type,
			start_date=start_date,
			end_date=end_date,
			order_by=order_by
		)
		
		# 3. 针对 ES8 SearchResults 执行安全的延迟翻页切片
		# 【架构避坑提示】不要频繁调用 search_results.count()，在大型并发下，使用切片和总数缓存更健壮
		total_count = search_results.count()
		
		start = (page - 1) * per_page
		end = start + per_page
		
		# 针对返回的抽象类型进行弹性截断
		paginated_results = search_results[start:end]
		
		# 4. 格式化并灌入缓存
		results_data = format_search_results_for_api(paginated_results)
		
		data = {
			'query': query,
			'total': total_count,
			'page': page,
			'per_page': per_page,
			'start_date': start_date or "",
			'end_date': end_date or "",
			'order_by': order_by or "",
			'results': results_data
		}
		
		SearchCache.set_cached_results(
			query, data, search_type, page, start_date, end_date, order_by
		)
		
		return Response(data)
	except Exception as e:
		logger.error(f"API搜索网关层发生故障: {e}")
		return Response({'error': '搜索服务由于异构切换产生短暂毛刺，请稍后再试'}, status=500)


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
		# 调用 core.py 中保留的 get_search_suggestions 方法
		suggestions = get_search_suggestions(query)
		return Response({'suggestions': suggestions})
	except Exception as e:
		logger.error(f"获取搜索建议时发生错误: {e}")
		return Response({'suggestions': []})