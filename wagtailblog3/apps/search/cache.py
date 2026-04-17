# search/cache.py
from django.core.cache import cache
import hashlib
import json


class SearchCache:
	"""搜索缓存管理"""
	
	@staticmethod
	def get_cache_key(query, search_type='all', page=1, start_date=None, end_date=None, order_by=None):
		"""生成包含时间范围的缓存键"""
		# 构建包含所有参数的键字符串
		key_elements = [
			f"search:{query}:{search_type}:{page}"
		]
		
		if start_date:
			key_elements.append(f"start:{start_date}")
		if end_date:
			key_elements.append(f"end:{end_date}")
		if order_by:
			key_elements.append(f"order:{order_by}")
		
		key_string = ":".join(key_elements)
		return hashlib.md5(key_string.encode()).hexdigest()
	
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
		"""清除所有搜索缓存"""
		cache.delete_pattern("search:*")