# 搜索结果缓存工具
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from django.conf import settings
from django.core.cache import cache
import hashlib
import json


class SearchCache:
	"""通过实现版本隔离不同搜索链路的缓存结果。"""
	"""搜索缓存管理"""
	SEARCH_IMPLEMENTATION_VERSION = "v5"

	@staticmethod
	def get_implementation_namespace(search_type: str = 'all') -> str:
		"""固定缓存到当前正式搜索路径，禁止复用已退役旧流程的结果。"""
		if search_type == 'blog':
			return 'content'
		if search_type == 'all':
			return 'federated-all'
		return 'pages'

	@staticmethod
	def get_cache_key(
		query: str, search_type: str = 'all', page: int = 1,
		start_date: date | datetime | None = None,
		end_date: date | datetime | None = None,
		order_by: str | None = None,
	) -> str:
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
	def get_cached_results(
		query: str, search_type: str = 'all', page: int = 1,
		start_date: date | datetime | None = None,
		end_date: date | datetime | None = None,
		order_by: str | None = None,
	) -> Any:
		"""获取缓存的搜索结果"""
		cache_key = SearchCache.get_cache_key(query, search_type, page, start_date, end_date, order_by)
		return cache.get(cache_key)
	
	@staticmethod
	def set_cached_results(
		query: str, results: Any, search_type: str = 'all', page: int = 1,
		start_date: date | datetime | None = None,
		end_date: date | datetime | None = None,
		order_by: str | None = None, timeout: int = 300,
	) -> None:
		"""设置搜索结果缓存，默认5分钟过期"""
		cache_key = SearchCache.get_cache_key(query, search_type, page, start_date, end_date, order_by)
		cache.set(cache_key, results, timeout)
	
	@staticmethod
	def clear_search_cache() -> None:
		"""清除当前搜索实现版本的全部结果缓存。"""
		cache.delete_pattern(f"search:{SearchCache.SEARCH_IMPLEMENTATION_VERSION}:*")
