# blog/middleware.py
from django.utils.deprecation import MiddlewareMixin
import logging

logger = logging.getLogger(__name__)


class PageViewMiddleware(MiddlewareMixin):
	"""记录页面访问的中间件"""
	
	def process_response(self, request, response):
		# 检查是否为博客页面且状态码为200
		if hasattr(request, 'wagtailpage') and response.status_code == 200:
			from .models import BlogPage
			if isinstance(request.wagtailpage.specific, BlogPage):
				page = request.wagtailpage
				
				# 确保有会话键
				if not request.session.session_key:
					request.session.save()
				
				session_key = request.session.session_key
				
				# 获取IP地址
				x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
				if x_forwarded_for:
					ip = x_forwarded_for.split(',')[0]
				else:
					ip = request.META.get('REMOTE_ADDR')
				
				user = request.user if request.user.is_authenticated else None
				user_agent = request.META.get('HTTP_USER_AGENT', '')
				
				# 检查是否为唯一访问
				is_unique = self._is_unique_visit(request, page.id)
				
				# 使用Redis缓存增加访问计数
				try:
					self._increment_view_count(page.id, session_key, is_unique)
					
					# 同时存储详细记录到数据库
					from .models import PageView
					PageView.objects.create(
						page=page,
						session_key=session_key,
						user=user,
						ip_address=ip,
						user_agent=user_agent,
						is_unique=is_unique
					)
				except Exception as e:
					logger.error(f"记录页面访问出错: {e}")
		
		return response
	
	def _is_unique_visit(self, request, page_id):
		"""判断是否为唯一访问"""
		from django.core.cache import cache
		
		# 生成访客标识
		visitor = request.user.id if request.user.is_authenticated else request.session.session_key
		key = f"viewed_{page_id}_{visitor}"
		
		if cache.get(key):
			return False
		
		# 设置访问记录，30分钟内再次访问不计为唯一
		cache.set(key, True, 30 * 60)
		return True
	
	def _increment_view_count(self, page_id, session_key, is_unique):
		"""增加页面访问计数"""
		import redis
		from django.conf import settings
		import datetime
		
		try:
			# 使用Redis连接
			redis_client = redis.Redis(
				host=getattr(settings, 'REDIS_HOST', 'localhost'),
				port=getattr(settings, 'REDIS_PORT', 6379),
				password=getattr(settings, 'REDIS_PASSWORD', None),
				db=getattr(settings, 'REDIS_DB', 0)
			)
			
			# 增加总计数
			redis_client.incr(f"page_views:{page_id}")
			
			# 记录唯一访问
			if is_unique:
				today = datetime.date.today().isoformat()
				redis_client.sadd(f"page_unique_views:{page_id}:{today}", session_key)
		except Exception as e:
			logger.error(f"访问计数Redis操作失败: {e}")