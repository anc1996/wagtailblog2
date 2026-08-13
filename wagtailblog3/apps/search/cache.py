# 搜索结果缓存工具
from django.conf import settings
from django.core.cache import cache
import hashlib
import json


class SearchCache:
	"""搜索缓存管理"""
	SEARCH_IMPLEMENTATION_VERSION = "v5"

	@staticmethod
	def get_implementation_namespace(search_type='all'):
		"""区分旧搜索与内容索引，避免切换或回退时复用另一实现的结果。"""
		if search_type == 'all' and getattr(settings, 'CONTENT_SEARCH_FEDERATED_ALL_ENABLED', False):
			return 'federated-all'
		if search_type == 'blog' and getattr(settings, 'CONTENT_SEARCH_QUERY_ENABLED', False):
			return 'content'
		return 'legacy'

	@staticmethod
	def get_cache_key(query, search_type='all', page=1, start_date=None, end_date=None, order_by=None):
		"""生成包含实现、时间范围和排序条件的缓存键。"""
		# 切换或回退前台查询实现时，绝不能让旧结果以相同查询条件命中新实现缓存。
		implementation = SearchCache.get_implementation_namespace(search_type)
		key_elements = [
			f"search:{SearchCache.SEARCH_IMPLEMENTATION_VERSION}:{implementation}:{query}:{search_type}:{page}"
		]
		
		if start_date:
			key_elements.append(f"start:{start_date}")
		if end_date:
			key_elements.append(f"end:{end_date}")
		if order_by:
			key_elements.append(f"order:{order_by}")
		
		key_string = ":".join(key_elements)
		digest = hashlib.md5(key_string.encode()).hexdigest()
		return f"search:{SearchCache.SEARCH_IMPLEMENTATION_VERSION}:{digest}"
	
	@staticmethod
	def get_cached_results(query, search_type='all', page=1, start_date=None, end_date=None, order_by=None):
		"""获取缓存的搜索结果"""
		cache_key = SearchCache.get_cache_key(query, search_type, page, start_date, end_date, order_by)
		return cache.get(cache_key)
	
	@staticmethod
	def set_cached_results(query, results, search_type='all', page=1, start_date=None, end_date=None, order_by=None,
	                       timeout=300):
		"""设置搜索结果缓存，默认5分钟过期"""
		cache_key = SearchCache.get_cache_key(query, search_type, page, start_date, end_date, order_by)
		cache.set(cache_key, results, timeout)
	
	@staticmethod
	def clear_search_cache():
		"""清除当前搜索实现版本的全部结果缓存。"""
		cache.delete_pattern(f"search:{SearchCache.SEARCH_IMPLEMENTATION_VERSION}:*")
