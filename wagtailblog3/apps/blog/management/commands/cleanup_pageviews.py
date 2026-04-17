# blog/management/commands/cleanup_pageviews.py
from django.core.management.base import BaseCommand
from django.utils import timezone
import datetime
from blog.models import PageView, PageViewCount
from django.db.models import Count, Sum
import logging

logger = logging.getLogger(__name__)

# 创建数据清理命令
class Command(BaseCommand):
	help = '清理旧的详细访问记录并保留聚合数据'
	
	def add_arguments(self, parser):
		parser.add_argument(
			'--days',
			type=int,
			default=30,
			help='保留详细记录的天数'
		)
	
	def handle(self, *args, **options):
		retention_days = options['days']
		cutoff_date = timezone.now() - datetime.timedelta(days=retention_days)
		
		# 确保有聚合数据
		self._ensure_aggregations(cutoff_date)
		
		# 删除旧记录
		try:
			deleted_count = PageView.objects.filter(
				viewed_at__lt=cutoff_date
			).count()
			
			if deleted_count > 0:
				# 先获取总数再删除，避免大规模删除操作超时
				PageView.objects.filter(
					viewed_at__lt=cutoff_date
				).delete()
				
				self.stdout.write(self.style.SUCCESS(
					f'已删除 {deleted_count} 条旧的访问记录'
				))
			else:
				self.stdout.write('没有需要删除的旧访问记录')
		except Exception as e:
			logger.error(f"删除旧记录时出错: {e}")
	
	def _ensure_aggregations(self, cutoff_date):
		"""确保所有要删除的数据都有对应的聚合记录"""
		# 获取最早的记录日期
		try:
			earliest = PageView.objects.filter(
				viewed_at__lt=cutoff_date
			).earliest('viewed_at')
			
			earliest_date = earliest.viewed_at.date()
			latest_date = cutoff_date.date() - datetime.timedelta(days=1)  # 不包括截止日期当天
			
			self.stdout.write(f'开始聚合从 {earliest_date} 到 {latest_date} 的访问数据')
			
			# 对每一天进行聚合
			current_date = earliest_date
			while current_date <= latest_date:
				# 获取当天的记录
				day_views = PageView.objects.filter(
					viewed_at__date=current_date
				).values('page').annotate(
					count=Count('id'),
					unique_count=Count('session_key', distinct=True)
				)
				
				# 创建或更新聚合记录
				created_count = 0
				updated_count = 0
				
				for dv in day_views:
					if 'page' in dv and dv['page'] is not None:
						obj, created = PageViewCount.objects.update_or_create(
							page_id=dv['page'],
							date=current_date,
							defaults={
								'count': dv['count'],
								'unique_count': dv['unique_count']
							}
						)
						if created:
							created_count += 1
						else:
							updated_count += 1
				
				self.stdout.write(
					f'{current_date}: 创建了 {created_count} 条新记录，更新了 {updated_count} 条记录'
				)
				
				# 移到下一天
				current_date += datetime.timedelta(days=1)
			
			self.stdout.write(
				f'已完成从 {earliest_date} 到 {latest_date} 的聚合'
			)
		
		except PageView.DoesNotExist:
			self.stdout.write('没有需要聚合的旧记录')
		except Exception as e:
			logger.error(f"聚合数据时出错: {e}")