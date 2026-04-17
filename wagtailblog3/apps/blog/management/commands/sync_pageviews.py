# blog/management/commands/sync_pageviews.py
from django.core.management.base import BaseCommand
import datetime
import redis
from django.conf import settings
from blog.models import PageViewCount
from wagtail.models import Page
import logging

logger = logging.getLogger(__name__)

# 创建数据同步命令
class Command(BaseCommand):
	help = '从Redis同步页面访问数据到数据库'
	
	def handle(self, *args, **options):
		redis_client = redis.Redis(
			host=getattr(settings, 'REDIS_HOST', 'localhost'),
			port=getattr(settings, 'REDIS_PORT', 6379),
			password=getattr(settings, 'REDIS_PASSWORD', None),
			db=getattr(settings, 'REDIS_DB', 0)
		)
		
		today = datetime.date.today()
		
		# 查找所有带计数的页面
		view_keys = redis_client.keys("page_views:*")
		
		synced_count = 0
		
		for key in view_keys:
			page_id = key.decode().split(":")[-1]
			count = int(redis_client.get(key) or 0)
			
			# 获取唯一访客数
			unique_key = f"page_unique_views:{page_id}:{today.isoformat()}"
			unique_count = redis_client.scard(unique_key)
			
			# 更新数据库
			try:
				page_id = int(page_id)
				page_obj = Page.objects.get(id=page_id)
				
				# 更新或创建计数记录
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
				
				# 清除Redis计数
				redis_client.delete(key)
				
				# 保留唯一访客集合，当天结束后自动过期
				redis_client.expire(unique_key, 86400)  # 24小时
				
				synced_count += 1
				
				self.stdout.write(
					f'同步页面 ID {page_id} 的访问数据: {count} 次访问, {unique_count} 个唯一访客'
				)
			
			except Exception as e:
				logger.error(f"同步页面 {page_id} 访问数据出错: {e}")
				continue
		
		self.stdout.write(self.style.SUCCESS(f'成功同步 {synced_count} 个页面的访问数据'))