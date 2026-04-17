# search/api.py
from rest_framework.decorators import api_view
from rest_framework.response import Response
from .core import perform_search, get_search_suggestions, format_search_results_for_api
from .cache import SearchCache
import logging

logger = logging.getLogger(__name__)


def clean_search_param(value):
	"""清理搜索参数，移除None值和'None'字符串"""
	if value in [None, 'None', 'null', '']:
		return None
	return value.strip() if isinstance(value, str) else value


@api_view(['GET'])
def search_api(request):
	"""REST API搜索端点"""
	# 获取参数
	query = request.GET.get('q', '')
	search_type = request.GET.get('type', 'all')
	page = int(request.GET.get('page', 1))
	per_page = int(request.GET.get('per_page', 10))
	
	# 获取新增的时间范围参数并清理
	start_date = clean_search_param(request.GET.get('start_date', None))
	end_date = clean_search_param(request.GET.get('end_date', None))
	order_by = clean_search_param(request.GET.get('order_by', None))
	
	# 清理查询参数
	query = clean_search_param(query)
	
	if not query:
		return Response({'error': '请提供搜索查询'}, status=400)
	
	try:
		# 尝试获取缓存结果
		cached_results = SearchCache.get_cached_results(
			query, search_type, page, start_date, end_date, order_by
		)
		
		if cached_results:
			logger.info(f"返回缓存的搜索结果: '{query}'")
			return Response(cached_results)
		
		# 执行搜索
		search_results = perform_search(
			query, search_type,
			start_date=start_date,
			end_date=end_date,
			order_by=order_by
		)
		
		# 计算总数
		total_count = search_results.count()
		
		# 计算分页
		start = (page - 1) * per_page
		end = start + per_page
		paginated_results = search_results[start:end]
		
		# 格式化结果
		results_data = format_search_results_for_api(paginated_results)
		
		# 构建响应 - 确保返回的值是清理过的
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
		
		# 缓存结果
		SearchCache.set_cached_results(
			query, data, search_type, page, start_date, end_date, order_by
		)
		
		return Response(data)
	except Exception as e:
		logger.error(f"API搜索出错: {e}")
		return Response({
			'error': f"搜索处理错误: {str(e)}",
			'query': query or "",
			'results': []
		}, status=500)


@api_view(['GET'])
def search_suggestions_api(request):
	"""搜索建议API"""
	
	query = clean_search_param(request.GET.get('q', ''))
	
	try:
		# 获取搜索建议
		suggestions = get_search_suggestions(query)
		
		# 返回结果
		return Response({'suggestions': suggestions})
	except Exception as e:
		logger.error(f"API搜索建议出错: {e}")
		return Response({
			'error': f"获取搜索建议错误: {str(e)}",
			'suggestions': []
		}, status=500)