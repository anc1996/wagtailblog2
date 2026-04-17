# blog/management/commands/view_logs.py

from django.core.management.base import BaseCommand
import os
from django.conf import settings


class Command(BaseCommand):
	help = '查看应用日志'
	
	def add_arguments(self, parser):
		parser.add_argument('--module', type=str,
		                    choices=['blog', 'comments', 'search', 'archive', 'system', 'wagtail'],
		                    default='system', help='要查看的模块日志')
		parser.add_argument('--lines', type=int, default=50,
		                    help='要显示的行数')
	
	def handle(self, *args, **options):
		module = options['module']
		lines = options['lines']
		
		log_dir = settings.LOG_DIR
		
		# 确定日志文件路径
		if module == 'wagtail':
			log_file = os.path.join(log_dir, 'system/wagtail_error.log')
		elif module == 'system':
			log_file = os.path.join(log_dir, 'system/error.log')
		else:
			log_file = os.path.join(log_dir, f'{module}/{module}_error.log')
		
		if not os.path.exists(log_file):
			self.stderr.write(f"日志文件不存在: {log_file}")
			return
		
		# 使用tail读取最后N行
		try:
			from collections import deque
			with open(log_file, 'r') as f:
				last_lines = deque(f, lines)
			
			self.stdout.write(f"=== {module} 最新 {lines} 行日志 ===\n")
			for line in last_lines:
				self.stdout.write(line, ending='')
		except Exception as e:
			self.stderr.write(f"读取日志失败: {e}")