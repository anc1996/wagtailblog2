# base/management/commands/email_status.py
from django.core.management.base import BaseCommand
from django.utils import timezone
from celery.result import AsyncResult
from celery import current_app
import redis
from django.conf import settings

# 命令行工具：查看Celery邮件发送任务状态和系统监控信息
"""
命令：
python manage.py email_status [--task-id <ID>] [--queue-status] [--workers] [--stats]
如：
第一种：python manage.py email_status --task-id 1234567890abcdef --queue-status --workers --stats
第二种：python manage.py email_status --queue-status
第三种：python manage.py email_status --workers

"""

class Command(BaseCommand):
	help = '查看邮件发送任务状态和系统监控信息'
	
	def add_arguments(self, parser):
		parser.add_argument(
			'--task-id',
			type=str,
			help='查看特定任务的状态'
		)
		parser.add_argument(
			'--queue-status',
			action='store_true',
			help='查看队列状态'
		)
		parser.add_argument(
			'--workers',
			action='store_true',
			help='查看工作进程状态'
		)
		parser.add_argument(
			'--stats',
			action='store_true',
			help='查看邮件发送统计'
		)
	
	def handle(self, *args, **options):
		self.stdout.write(
			self.style.SUCCESS('=== Celery 邮件系统状态监控 ===\n')
		)
		
		# 检查特定任务状态
		if options['task_id']:
			self.check_task_status(options['task_id'])
		
		# 检查队列状态
		if options['queue_status']:
			self.check_queue_status()
		
		# 检查工作进程状态
		if options['workers']:
			self.check_workers_status()
		
		# 查看统计信息
		if options['stats']:
			self.show_statistics()
		
		# 如果没有指定参数，显示概览
		if not any([options['task_id'], options['queue_status'],
		            options['workers'], options['stats']]):
			self.show_overview()
	
	def check_task_status(self, task_id):
		"""检查特定任务状态"""
		self.stdout.write(f"\n--- 任务状态: {task_id} ---")
		
		try:
			result = AsyncResult(task_id)
			self.stdout.write(f"状态: {result.status}")
			self.stdout.write(f"结果: {result.result}")
			self.stdout.write(f"成功: {result.successful()}")
			self.stdout.write(f"失败: {result.failed()}")
			if result.date_done:
				self.stdout.write(f"完成时间: {result.date_done}")
		except Exception as e:
			self.stdout.write(
				self.style.ERROR(f"查看任务状态失败: {str(e)}")
			)
	
	def check_queue_status(self):
		"""检查队列状态"""
		self.stdout.write("\n--- 队列状态 ---")
		
		try:
			# 连接Redis获取队列信息
			redis_client = redis.Redis(
				host=settings.REDIS_HOST,
				port=settings.REDIS_PORT,
				password=settings.REDIS_PASSWORD,
				db=2  # Celery broker使用的数据库
			)
			
			queues = ['default', 'email', 'maintenance']
			for queue in queues:
				queue_key = f'celery.{queue}'
				queue_length = redis_client.llen(queue_key)
				self.stdout.write(f"{queue} 队列: {queue_length} 个待处理任务")
		
		except Exception as e:
			self.stdout.write(
				self.style.ERROR(f"获取队列状态失败: {str(e)}")
			)
	
	def check_workers_status(self):
		"""检查工作进程状态"""
		self.stdout.write("\n--- 工作进程状态 ---")
		
		try:
			# 获取活跃的工作进程
			inspect = current_app.control.inspect()
			
			# 活跃工作进程
			active_workers = inspect.active()
			if active_workers:
				self.stdout.write(f"活跃工作进程数量: {len(active_workers)}")
				for worker, tasks in active_workers.items():
					self.stdout.write(f"  {worker}: {len(tasks)} 个正在执行的任务")
			else:
				self.stdout.write(
					self.style.WARNING("没有检测到活跃的工作进程")
				)
			
			# 注册的工作进程
			registered = inspect.registered()
			if registered:
				self.stdout.write(f"\n注册的任务类型:")
				for worker, tasks in registered.items():
					self.stdout.write(f"  {worker}:")
					for task in tasks:
						if 'email' in task or 'base.tasks' in task:
							self.stdout.write(f"    - {task}")
		
		except Exception as e:
			self.stdout.write(
				self.style.ERROR(f"获取工作进程状态失败: {str(e)}")
			)
	
	def show_statistics(self):
		"""显示邮件发送统计"""
		self.stdout.write("\n--- 邮件发送统计 ---")
		
		try:
			# 这里可以添加从数据库或Redis获取统计信息的逻辑
			# 例如：成功发送数量、失败数量、重试次数等
			
			redis_client = redis.Redis(
				host=settings.REDIS_HOST,
				port=settings.REDIS_PORT,
				password=settings.REDIS_PASSWORD,
				db=3  # Celery result backend使用的数据库
			)
			
			# 获取所有结果键的数量作为总任务数
			result_keys = redis_client.keys('celery-task-meta-*')
			total_tasks = len(result_keys)
			
			self.stdout.write(f"总任务数: {total_tasks}")
		
		# 可以根据需要添加更详细的统计信息
		
		except Exception as e:
			self.stdout.write(
				self.style.ERROR(f"获取统计信息失败: {str(e)}")
			)
	
	def show_overview(self):
		"""显示系统概览"""
		self.stdout.write("\n--- 系统概览 ---")
		
		current_time = timezone.now()
		self.stdout.write(f"当前时间: {current_time}")
		
		# 检查Redis连接
		try:
			redis_client = redis.Redis(
				host=settings.REDIS_HOST,
				port=settings.REDIS_PORT,
				password=settings.REDIS_PASSWORD,
			)
			redis_client.ping()
			self.stdout.write(
				self.style.SUCCESS("✓ Redis连接正常")
			)
		except Exception:
			self.stdout.write(
				self.style.ERROR("✗ Redis连接失败")
			)
		
		# 检查Celery应用配置
		self.stdout.write(f"Celery应用名称: {current_app.main}")
		self.stdout.write(f"消息代理: {current_app.conf.broker_url}")
		self.stdout.write(f"结果后端: {current_app.conf.result_backend}")
		
		self.stdout.write("\n使用以下参数获取更多信息:")
		self.stdout.write("  --queue-status : 查看队列状态")
		self.stdout.write("  --workers      : 查看工作进程状态")
		self.stdout.write("  --stats        : 查看邮件发送统计")
		self.stdout.write("  --task-id <ID> : 查看特定任务状态")