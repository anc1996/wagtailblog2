# 搜索结果缓存工具
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from django.conf import settings
from django.core.cache import cache
import hashlib
import json
from uuid import uuid4


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
		*, per_page: int = 10, locale: str = "",
	) -> str:
		"""所有后端使用随机代次；分页大小与语言共同限定缓存身份。"""
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
		generation_key = f"search:{SearchCache.SEARCH_IMPLEMENTATION_VERSION}:generation"
		generation = cache.get_or_set(
			generation_key, lambda: uuid4().hex, timeout=None
		)
		key_elements.extend([
			f"generation:{generation}", f"per_page:{per_page}", f"locale:{locale}",
		])

		key_string = ":".join(key_elements)
		digest = hashlib.md5(key_string.encode()).hexdigest()
		return f"search:{SearchCache.SEARCH_IMPLEMENTATION_VERSION}:{digest}"
	
	@staticmethod
	def get_cached_results(
		query: str, search_type: str = 'all', page: int = 1,
		start_date: date | datetime | None = None,
		end_date: date | datetime | None = None,
		order_by: str | None = None,
		*, cache_key: str | None = None,
	) -> Any:
		"""获取缓存的搜索结果"""
		cache_key = cache_key or SearchCache.get_cache_key(query, search_type, page, start_date, end_date, order_by)
		return cache.get(cache_key)
	
	@staticmethod
	def set_cached_results(
		query: str, results: Any, search_type: str = 'all', page: int = 1,
		start_date: date | datetime | None = None,
		end_date: date | datetime | None = None,
		order_by: str | None = None, timeout: int = 300,
		*, cache_key: str | None = None,
	) -> None:
		"""查询入口传入冻结的键，防止权限失效后将旧结果写入新代次。"""
		cache_key = cache_key or SearchCache.get_cache_key(query, search_type, page, start_date, end_date, order_by)
		cache.set(cache_key, results, timeout)
	
	@staticmethod
	def clear_search_cache() -> None:
		"""只失效搜索结果；LocMem 以随机代次切换，不清空其他业务缓存。"""
		delete_pattern = getattr(cache, "delete_pattern", None)
		if callable(delete_pattern):
			delete_pattern(f"search:{SearchCache.SEARCH_IMPLEMENTATION_VERSION}:*")
		cache.set(
			f"search:{SearchCache.SEARCH_IMPLEMENTATION_VERSION}:generation",
			uuid4().hex,
			timeout=None,
		)
