"""
Redis 键名全域统一协议与业务工厂 (RedisKeyProtocol)

遵循六段式全域安全标识契约：{app}:{env}:{module}:{entity}:{identifier}:{version}
集中收拢博客系统内所有业务缓存、分布式锁与代次计数器的键名生成，
杜绝在业务代码中散落硬编码拼接字符串，防止多系统共用 Redis 时的踩踏事故。
"""
from __future__ import annotations


class RedisKeyProtocol:
	"""
	Redis 全域键名构造协议工厂。

	本工厂集中管理博客系统所有业务层缓存、锁与代次计数器键，
	与 Django Cache 统一网关 (unified_key_maker) 深度配合：
	1. 生成标准业务键（如 listing:page:...、sidebar:archive:...）；
	2. 经 Django Cache 网关自动包装为当前环境的全域安全键（如 wblog:prod:listing:page:...）；
	3. 支持分布式互斥锁与代次计数器的安全生成。
	"""

	# ------------------------------------------------------------------
	# 1. 博客列表与分类聚合 (Module: listing)
	# ------------------------------------------------------------------
	@staticmethod
	def listing_cache_key(
		site_id: int,
		locale_id: int,
		index_id: int,
		query_hash: str,
		generation: int,
	) -> str:
		"""
		生成博客列表与分类聚合页 DTO 缓存键。
		标准格式: wblog:listing:v2:s_{site}:loc_{locale}:idx_{index}:gen_{gen}:q_{query_hash}
		"""
		return f"wblog:listing:v2:s_{site_id}:loc_{locale_id}:idx_{index_id}:gen_{generation}:q_{query_hash}"

	@staticmethod
	def listing_lock_key(index_page_id: int, lock_hash: str) -> str:
		"""
		生成列表页 Single-Flight 防击穿分布式互斥锁键。
		标准格式: wblog:listing:v2:lock:{index_page_id}:{lock_hash}
		"""
		return f"wblog:listing:v2:lock:{index_page_id}:{lock_hash}"

	@staticmethod
	def listing_generation_key(site_id: int, locale_id: int, index_page_id: int) -> str:
		"""
		生成列表页/分类页代次计数器键。
		标准格式: wblog:list:v2:generation:{site}:{locale}:{index_page_id}
		"""
		return f"wblog:list:v2:generation:{site_id}:{locale_id}:{index_page_id}"

	# ------------------------------------------------------------------
	# 2. 侧边栏归档与作者聚合 (Module: sidebar)
	# ------------------------------------------------------------------
	@staticmethod
	def sidebar_archive_aggregate_key(
		site_id: int,
		locale_id: int,
		generation: int,
		policy_version: str,
	) -> str:
		"""
		生成侧边栏归档聚合树缓存键。
		标准格式: wblog:sidebar:v2:archive:aggregate:{site}:{locale}:{gen}:{policy_version}
		"""
		return f"wblog:sidebar:v2:archive:aggregate:{site_id}:{locale_id}:{generation}:{policy_version}"

	@staticmethod
	def sidebar_archive_lock_key(
		site_id: int,
		locale_id: int,
		generation: int,
		policy_version: str,
	) -> str:
		"""
		生成侧边栏归档防击穿互斥锁键。
		标准格式: wblog:sidebar:v2:archive:lock:{site}:{locale}:{gen}:{policy_version}
		"""
		return f"wblog:sidebar:v2:archive:lock:{site_id}:{locale_id}:{generation}:{policy_version}"

	@staticmethod
	def sidebar_archive_generation_key(site_id: int, locale_id: int) -> str:
		"""
		生成侧边栏归档代次计数器键。
		标准格式: wblog:sidebar:v2:generation:archive:{site}:{locale}
		"""
		return f"wblog:sidebar:v2:generation:archive:{site_id}:{locale_id}"

	@staticmethod
	def sidebar_author_candidates_key(site_id: int, locale_id: int, generation: int) -> str:
		"""
		生成侧边栏活跃作者候选集合缓存键。
		标准格式: wblog:sidebar:v2:author:candidates:{site}:{locale}:{gen}
		"""
		return f"wblog:sidebar:v2:author:candidates:{site_id}:{locale_id}:{generation}"

	@staticmethod
	def sidebar_author_pick_key(
		site_id: int,
		locale_id: int,
		generation: int,
		time_bucket: str,
	) -> str:
		"""
		生成侧边栏轮播展示作者候选键。
		标准格式: wblog:sidebar:v2:author:pick:{site}:{locale}:{gen}:{time_bucket}
		"""
		return f"wblog:sidebar:v2:author:pick:{site_id}:{locale_id}:{generation}:{time_bucket}"

	@staticmethod
	def sidebar_author_generation_key(site_id: int, locale_id: int) -> str:
		"""
		生成侧边栏作者代次计数器键。
		标准格式: wblog:sidebar:v2:generation:author:{site}:{locale}
		"""
		return f"wblog:sidebar:v2:generation:author:{site_id}:{locale_id}"

	# ------------------------------------------------------------------
	# 3. 博客详情页 (Module: detail)
	# ------------------------------------------------------------------
	@staticmethod
	def detail_body_key(
		namespace: str,
		site_id: int,
		locale_id: int,
		page_id: int,
		body_version_id: str,
		schema_version: int,
	) -> str:
		"""
		生成博客正文详情多级缓存键。
		标准格式: {namespace}:body:{site}:{locale}:{page}:{body_version}:{schema_version}
		"""
		return f"{namespace}:body:{site_id}:{locale_id}:{page_id}:{body_version_id}:{schema_version}"

	@staticmethod
	def detail_body_lock_key(namespace: str, page_id: int, body_version_id: str) -> str:
		"""
		生成正文并发加载互斥锁键。
		标准格式: {namespace}:lock:body:{page}:{body_version}
		"""
		return f"{namespace}:lock:body:{page_id}:{body_version_id}"

	@staticmethod
	def detail_generation_key(namespace: str, page_id: int) -> str:
		"""
		生成正文页面代次键。
		标准格式: {namespace}:generation:{page}
		"""
		return f"{namespace}:generation:{page_id}"

	@staticmethod
	def detail_nav_key(
		namespace: str,
		site_id: int,
		locale_id: int,
		page_id: int,
		generation: str,
	) -> str:
		"""
		生成上一篇/下一篇导航缓存键。
		标准格式: {namespace}:nav:{site}:{locale}:{page}:{generation}
		"""
		return f"{namespace}:nav:{site_id}:{locale_id}:{page_id}:{generation}"

	# ------------------------------------------------------------------
	# 4. 频率限制 (Module: rate)
	# ------------------------------------------------------------------
	@staticmethod
	def comment_rate_key(user_identifier: str) -> str:
		"""
		生成评论频率限制键。
		标准格式: comment:{user_identifier}
		"""
		return f"comment:{user_identifier}"
