from django.core.management.base import BaseCommand
import os
from django.conf import settings
import datetime


class Command(BaseCommand):
	help = '清理旧日志文件备份'
	
	def add_arguments(self, parser):
		parser.add_argument('--days', type=int, default=30,
		                    help='删除超过指定天数的日志文件备份')
		parser.add_argument('--dry-run', action='store_true',
		                    help='仅显示将被删除的文件，不实际删除')
	
	def handle(self, *args, **options):
		days = options['days']
		dry_run = options['dry_run']
		
		log_dir = settings.LOG_DIR
		now = datetime.datetime.now()
		cutoff_date = now - datetime.timedelta(days=days)
		
		count = 0
		total_size = 0
		
		# 遍历日志目录
		for root, dirs, files in os.walk(log_dir):
			for file in files:
				# 只处理日志备份文件（格式如xxx.log.1, xxx.log.2等）
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