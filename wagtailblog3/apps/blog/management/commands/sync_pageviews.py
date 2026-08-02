"""将 Redis 中的页面访问计数同步到数据库聚合表。"""
from django.core.management.base import BaseCommand
import datetime
import redis
from django.conf import settings
from blog.models import PageViewCount
from wagtail.models import Page
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
	"""逐页导入当天访问数，并在成功后清理 Redis 计数键。"""
	help = '从Redis同步页面访问数据到数据库'
	
	def handle(self, *args, **options):
		# Redis 只作为临时计数来源，数据库的按日聚合记录是最终持久化结果。
		redis_client = redis.Redis(
			host=getattr(settings, 'REDIS_HOST', 'localhost'),
			port=getattr(settings, 'REDIS_PORT', 6379),
			password=getattr(settings, 'REDIS_PASSWORD', None),
			db=getattr(settings, 'REDIS_DB', 0)
		)
		
		today = datetime.date.today()
		
		# 批量读取缓存中的页面计数键，逐页写入每日聚合表。
		view_keys = redis_client.keys("page_views:*")
		
		synced_count = 0
		
		for key in view_keys:
			page_id = key.decode().split(":")[-1]
			count = int(redis_client.get(key) or 0)
			
			# 唯一访客集合按页面和日期隔离，避免跨天重复累计。
			unique_key = f"page_unique_views:{page_id}:{today.isoformat()}"
			unique_count = redis_client.scard(unique_key)
			
			# 页面不存在或数据格式异常时跳过当前键，继续同步其他页面。
			try:
				page_id = int(page_id)
				page_obj = Page.objects.get(id=page_id)
				
			# 更新或创建计数记录，使命令重复执行时得到幂等的当天统计。
				view_count, created = PageViewCount.objects.get_or_create(
					page=page_obj,
					date=today,
					defaults={
						'count': count,
						'unique_count': unique_count
					}
				)
				
				if not created:
					# 更新现有记录
					view_count.count = count
					view_count.unique_count = unique_count
					view_count.save()
				
				# 同步成功后删除计数键，避免下一次任务重复导入。
				redis_client.delete(key)
				
				# 保留唯一访客集合，当天结束后自动过期
				redis_client.expire(unique_key, 86400)  # 24小时
				
				synced_count += 1
				
				self.stdout.write(
					f'同步页面 ID {page_id} 的访问数据: {count} 次访问, {unique_count} 个唯一访客'
				)
			
			except Exception as e:
				logger.error(f"同步页面 {page_id} 访问数据出错: {e}", exc_info=True)
				continue
		
		self.stdout.write(self.style.SUCCESS(f'成功同步 {synced_count} 个页面的访问数据'))
