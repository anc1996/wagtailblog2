# 搜索应用的接口视图
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
	
	# 1. 先读取缓存，命中时直接返回，避免重复访问搜索引擎。
	cached_results = SearchCache.get_cached_results(
		query, search_type, page, start_date, end_date, order_by
	)
	if cached_results:
		logger.info(f"命中 Elasticsearch 8 上游业务缓存，返回数据: '{query}'")
		return Response(cached_results)
	
	try:
		# 2. 构造并执行原生搜索引擎查询。
		search_results = perform_search(
			query, search_type,
			start_date=start_date,
			end_date=end_date,
			order_by=order_by
		)
		
		# 3. 先取总数，再按当前页切片；结果代理会复用总数缓存。
		total_count = search_results.count()
		
		start = (page - 1) * per_page
		end = start + per_page
		
		# 结果代理统一支持切片，超出搜索引擎窗口时由代理截断。
		paginated_results = search_results[start:end]
		
		# 4. 转换为接口格式并写入缓存，供相同条件的请求复用。
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
		logger.error(f"API搜索网关层发生故障: {e}", exc_info=True)
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
		# 调用核心模块统一的搜索建议逻辑。
		suggestions = get_search_suggestions(query)
		return Response({'suggestions': suggestions})
	except Exception as e:
		logger.error(f"获取搜索建议时发生错误: {e}", exc_info=True)
		return Response({'suggestions': []})
