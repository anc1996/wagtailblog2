#!/usr/bin/env python
import os
import sys


def main():
	"""Run administrative tasks."""
	os.environ.setdefault("DJANGO_SETTINGS_MODULE", "wagtailblog3.settings.dev")
	
	# 添加命令行参数解析
	import argparse
	parser = argparse.ArgumentParser(add_help=False)
	parser.add_argument('--log-level', choices=['WARNING', 'ERROR', 'CRITICAL'],
	                    help='设置控制台日志级别')
	parser.add_argument('--log-module', help='仅显示指定模块的日志')
	
	# 解析已知参数，但不拦截Django命令
	args, remaining = parser.parse_known_args()
	
	# 设置日志级别
	if args.log_level:
		os.environ['DJANGO_LOG_LEVEL'] = args.log_level
	
	# 设置日志模块过滤
	if args.log_module:
		os.environ['DJANGO_LOG_MODULE'] = args.log_module
	
	# 继续Django命令处理
	from django.core.management import execute_from_command_line
	execute_from_command_line(sys.argv)


if __name__ == "__main__":
	main()