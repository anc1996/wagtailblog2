# base/utils.py
import hashlib
import time
from typing import Optional
from django.core.cache import cache
from django.conf import settings
import logging

# 使用专门的日志记录器用于频率限制功能
logger = logging.getLogger('base.rate_limit')


class EmailRateLimit:
	"""邮件发送频率限制工具类"""
	
	# 默认限制时间（秒）
	DEFAULT_LIMIT_SECONDS = 300  # 5分钟
	
	# Redis缓存键前缀
	CACHE_KEY_PREFIX = "email_rate_limit"
	
	@classmethod
	def _get_cache_key(cls, email: str, form_page_id: int) -> str:
		"""
		生成缓存键

		Args:
			email: 用户邮箱
			form_page_id: 表单页面ID

		Returns:
			str: 缓存键
		"""
		# 使用邮箱和表单页面ID生成唯一键，避免不同表单之间的冲突
		key_data = f"{email}:{form_page_id}"
		# 对邮箱进行哈希处理，保护用户隐私
		email_hash = hashlib.md5(key_data.encode()).hexdigest()
		return f"{cls.CACHE_KEY_PREFIX}:{email_hash}"
	
	@classmethod
	def check_rate_limit(cls, email: str, form_page_id: int,
	                     limit_seconds: Optional[int] = None) -> dict:
		"""
		检查邮件发送频率限制

		Args:
			email: 用户邮箱
			form_page_id: 表单页面ID
			limit_seconds: 限制时间（秒），默认为5分钟

		Returns:
			dict: 检查结果
				- allowed: bool 是否允许发送
				- remaining_seconds: int 剩余等待时间（秒）
				- last_sent_time: float 上次发送时间戳
		"""
		if not email or not form_page_id:
			return {
				'allowed': True,
				'remaining_seconds': 0,
				'last_sent_time': None
			}
		
		limit_seconds = limit_seconds or cls.DEFAULT_LIMIT_SECONDS
		cache_key = cls._get_cache_key(email, form_page_id)
		
		try:
			# 获取上次发送时间
			last_sent_time = cache.get(cache_key)
			
			if last_sent_time is None:
				# 没有发送记录，允许发送
				logger.debug(f"邮件频率检查: {email} 没有发送记录，允许发送")
				return {
					'allowed': True,
					'remaining_seconds': 0,
					'last_sent_time': None
				}
			
			current_time = time.time()
			elapsed_seconds = current_time - last_sent_time
			
			if elapsed_seconds >= limit_seconds:
				# 超过限制时间，允许发送
				logger.debug(f"邮件频率检查: {email} 距离上次发送已过 {elapsed_seconds:.1f} 秒，允许发送")
				return {
					'allowed': True,
					'remaining_seconds': 0,
					'last_sent_time': last_sent_time
				}
			else:
				# 未超过限制时间，拒绝发送
				remaining_seconds = int(limit_seconds - elapsed_seconds)
				logger.info(
					f"邮件频率检查: {email} 距离上次发送仅 {elapsed_seconds:.1f} 秒，需等待 {remaining_seconds} 秒")
				return {
					'allowed': False,
					'remaining_seconds': remaining_seconds,
					'last_sent_time': last_sent_time
				}
		
		except Exception as e:
			logger.error(f"邮件频率检查异常: {str(e)}", exc_info=True)
			# 发生异常时允许发送，避免影响正常功能
			return {
				'allowed': True,
				'remaining_seconds': 0,
				'last_sent_time': None
			}
	
	@classmethod
	def record_sent_time(cls, email: str, form_page_id: int,
	                     limit_seconds: Optional[int] = None) -> bool:
		"""
		记录邮件发送时间

		Args:
			email: 用户邮箱
			form_page_id: 表单页面ID
			limit_seconds: 限制时间（秒），用于设置缓存过期时间

		Returns:
			bool: 是否记录成功
		"""
		if not email or not form_page_id:
			return False
		
		limit_seconds = limit_seconds or cls.DEFAULT_LIMIT_SECONDS
		cache_key = cls._get_cache_key(email, form_page_id)
		current_time = time.time()
		
		try:
			# 设置缓存，过期时间稍微长于限制时间，确保数据不会过早清除
			cache_timeout = limit_seconds + 60  # 多给60秒缓冲时间
			cache.set(cache_key, current_time, timeout=cache_timeout)
			
			logger.info(
				f"邮件发送时间记录: {email} 在 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(current_time))}")
			return True
		
		except Exception as e:
			logger.error(f"记录邮件发送时间异常: {str(e)}", exc_info=True)
			return False
	
	@classmethod
	def clear_rate_limit(cls, email: str, form_page_id: int) -> bool:
		"""
		清除特定用户的频率限制记录（管理员功能）

		Args:
			email: 用户邮箱
			form_page_id: 表单页面ID

		Returns:
			bool: 是否清除成功
		"""
		if not email or not form_page_id:
			return False
		
		cache_key = cls._get_cache_key(email, form_page_id)
		
		try:
			cache.delete(cache_key)
			logger.info(f"邮件频率限制清除: {email}")
			return True
		
		except Exception as e:
			logger.error(f"清除邮件频率限制异常: {str(e)}", exc_info=True)
			return False
	
	@classmethod
	def get_remaining_time_message(cls, remaining_seconds: int) -> str:
		"""
		生成剩余等待时间的友好提示信息

		Args:
			remaining_seconds: 剩余等待时间（秒）

		Returns:
			str: 友好的提示信息
		"""
		if remaining_seconds <= 0:
			return "现在可以发送邮件"
		
		if remaining_seconds < 60:
			return f"请等待 {remaining_seconds} 秒后再次提交"
		else:
			minutes = remaining_seconds // 60
			seconds = remaining_seconds % 60
			if seconds == 0:
				return f"请等待 {minutes} 分钟后再次提交"
			else:
				return f"请等待 {minutes} 分钟 {seconds} 秒后再次提交"


class EmailRateLimitSettings:
	"""邮件频率限制配置类"""
	
	@classmethod
	def get_limit_seconds(cls, form_page_id: Optional[int] = None) -> int:
		"""
		获取邮件发送限制时间

		Args:
			form_page_id: 表单页面ID（可用于不同表单的个性化配置）

		Returns:
			int: 限制时间（秒）
		"""
		# 可以从Django设置中获取配置
		default_limit = getattr(settings, 'EMAIL_RATE_LIMIT_SECONDS', 300)  # 默认5分钟
		
		# 可以根据不同的表单页面设置不同的限制时间
		if form_page_id:
			form_specific_setting = getattr(settings, 'EMAIL_RATE_LIMIT_PER_FORM', {})
			return form_specific_setting.get(form_page_id, default_limit)
		
		return default_limit
	
	@classmethod
	def is_rate_limit_enabled(cls) -> bool:
		"""
		检查是否启用邮件频率限制

		Returns:
			bool: 是否启用
		"""
		return getattr(settings, 'EMAIL_RATE_LIMIT_ENABLED', True)