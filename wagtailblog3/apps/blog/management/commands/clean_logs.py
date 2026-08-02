"""按文件修改时间清理旧的日志轮转备份。"""

from django.core.management.base import BaseCommand
import os
from django.conf import settings
import datetime


class Command(BaseCommand):
	"""扫描日志目录并按需删除过期的轮转文件。"""
	help = '清理旧日志文件备份'
	
	def add_arguments(self, parser):
		parser.add_argument('--days', type=int, default=30,
		                    help='删除超过指定天数的日志文件备份')
		parser.add_argument('--dry-run', action='store_true',
		                    help='仅显示将被删除的文件，不实际删除')
	
	def handle(self, *args, **options):
		# 模拟运行只统计并展示目标文件，默认命令才真正删除文件。
		days = options['days']
		dry_run = options['dry_run']
		
		log_dir = settings.LOG_DIR
		now = datetime.datetime.now()
		cutoff_date = now - datetime.timedelta(days=days)
		
		count = 0
		total_size = 0
		
		# 递归遍历日志目录，只处理轮转产生的备份文件。
		for root, dirs, files in os.walk(log_dir):
			for file in files:
				# 仅匹配类似 xxx.log.1、xxx.log.2 的备份文件，不触碰当前活动日志。
				if '.log.' in file:
					file_path = os.path.join(root, file)
					file_time = datetime.datetime.fromtimestamp(os.path.getmtime(file_path))
					
					if file_time < cutoff_date:
						file_size = os.path.getsize(file_path)
						total_size += file_size
						count += 1
						
						if dry_run:
							self.stdout.write(f"将删除: {file_path} ({file_size / 1024:.1f} KB)")
						else:
							os.remove(file_path)
							self.stdout.write(f"已删除: {file_path} ({file_size / 1024:.1f} KB)")
		
		mode = "模拟" if dry_run else "实际"
		self.stdout.write(self.style.SUCCESS(
			f"{mode}清理完成: {count} 个日志文件, 共 {total_size / 1024 / 1024:.2f} MB"))
