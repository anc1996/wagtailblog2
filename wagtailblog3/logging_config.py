import os
from django.conf import settings


def ensure_log_dirs():
	"""确保日志目录存在"""
	log_dir = getattr(settings, 'LOG_DIR', os.path.join(settings.BASE_DIR, 'logs'))
	"""
	getattr() 是 Python 的一个内置函数，它用于获取一个对象的属性值。
		object: 你想要获取属性的对象。在这里是 settings 对象。
		name: 属性的名称（一个字符串）。在这里是 'LOG_DIR'。
		default: 可选参数。如果该属性不存在，getattr() 会返回这个默认值。如果没有提供默认值并且属性不存在，程序会抛出 AttributeError 异常。
	"""
	
	# 创建日志主目录
	if not os.path.exists(log_dir):
		os.makedirs(log_dir)
	
	# 创建模块子目录
	subdirs = [
		'blog', 'comments', 'search', 'archive', 'system',
		'base', 'email', 'celery'  # 新增base、email和celery目录
	]
	
	for subdir in subdirs:
		subdir_path = os.path.join(log_dir, subdir)
		if not os.path.exists(subdir_path):
			os.makedirs(subdir_path)
	
	return log_dir


def get_logging_config(modules_filter=None):
	"""获取完整的日志配置，集成邮件和频率限制日志"""
	log_dir = ensure_log_dirs()
	
	config = {
		'version': 1,
		'disable_existing_loggers': False,
		'formatters': {
			'verbose': {
				'format': '[{asctime}] {levelname} [{name}:{funcName}:{lineno}] {message}',
				'style': '{',
				'datefmt': '%Y-%m-%d %H:%M:%S',
			},
			'simple': {
				'format': '[{levelname}] {message}',
				'style': '{',
			},
			'colored': {
				'()': 'colorlog.ColoredFormatter',
				'format': '%(log_color)s[%(levelname)s] %(name)s: %(message)s',
				'log_colors': {
					'DEBUG': 'cyan',
					'INFO': 'green',
					'WARNING': 'yellow',
					'ERROR': 'red',
					'CRITICAL': 'bold_red',
				},
			},
			# 邮件专用格式器，包含更多上下文信息
			'email_verbose': {
				'format': '[{asctime}] {levelname} [{name}] [{process}:{thread}] {message}',
				'style': '{',
				'datefmt': '%Y-%m-%d %H:%M:%S',
			},
		},
		'handlers': {
			'console': {
				'level': 'WARNING',
				'class': 'logging.StreamHandler',
				'formatter': 'colored',
			},
			# 现有模块的错误日志处理器
			'blog_error_file': {
				'level': 'WARNING',
				'class': 'logging.handlers.RotatingFileHandler',
				'filename': os.path.join(log_dir, 'blog/blog_error.log'),
				'maxBytes': 10485760,  # 10MB
				'backupCount': 5,
				'formatter': 'verbose',
			},
			'comments_error_file': {
				'level': 'WARNING',
				'class': 'logging.handlers.RotatingFileHandler',
				'filename': os.path.join(log_dir, 'comments/comments_error.log'),
				'maxBytes': 10485760,
				'backupCount': 5,
				'formatter': 'verbose',
			},
			'search_error_file': {
				'level': 'WARNING',
				'class': 'logging.handlers.RotatingFileHandler',
				'filename': os.path.join(log_dir, 'search/search_error.log'),
				'maxBytes': 10485760,
				'backupCount': 5,
				'formatter': 'verbose',
			},
			'archive_error_file': {
				'level': 'WARNING',
				'class': 'logging.handlers.RotatingFileHandler',
				'filename': os.path.join(log_dir, 'archive/archive_error.log'),
				'maxBytes': 10485760,
				'backupCount': 5,
				'formatter': 'verbose',
			},
			'system_error_file': {
				'level': 'WARNING',
				'class': 'logging.handlers.RotatingFileHandler',
				'filename': os.path.join(log_dir, 'system/error.log'),
				'maxBytes': 10485760,
				'backupCount': 5,
				'formatter': 'verbose',
			},
			'wagtail_error_file': {
				'level': 'WARNING',
				'class': 'logging.handlers.RotatingFileHandler',
				'filename': os.path.join(log_dir, 'system/wagtail_error.log'),
				'maxBytes': 10485760,
				'backupCount': 5,
				'formatter': 'verbose',
			},
			
			# ============= 新增：Base模块日志处理器 =============
			'base_error_file': {
				'level': 'WARNING',
				'class': 'logging.handlers.RotatingFileHandler',
				'filename': os.path.join(log_dir, 'base/base_error.log'),
				'maxBytes': 10485760,
				'backupCount': 5,
				'formatter': 'verbose',
			},
			'base_info_file': {
				'level': 'INFO',
				'class': 'logging.handlers.RotatingFileHandler',
				'filename': os.path.join(log_dir, 'base/base_info.log'),
				'maxBytes': 5242880,  # 5MB
				'backupCount': 3,
				'formatter': 'verbose',
			},
			
			# ============= 新增：邮件相关日志处理器 =============
			'email_operations_file': {
				'level': 'INFO',
				'class': 'logging.handlers.RotatingFileHandler',
				'filename': os.path.join(log_dir, 'email/email_operations.log'),
				'maxBytes': 10485760,  # 10MB
				'backupCount': 5,
				'formatter': 'email_verbose',
			},
			'email_rate_limit_file': {
				'level': 'INFO',
				'class': 'logging.handlers.RotatingFileHandler',
				'filename': os.path.join(log_dir, 'email/rate_limit.log'),
				'maxBytes': 5242880,  # 5MB
				'backupCount': 3,
				'formatter': 'email_verbose',
			},
			'email_tasks_file': {
				'level': 'INFO',
				'class': 'logging.handlers.RotatingFileHandler',
				'filename': os.path.join(log_dir, 'email/email_tasks.log'),
				'maxBytes': 10485760,
				'backupCount': 5,
				'formatter': 'email_verbose',
			},
			'email_error_file': {
				'level': 'ERROR',
				'class': 'logging.handlers.RotatingFileHandler',
				'filename': os.path.join(log_dir, 'email/email_error.log'),
				'maxBytes': 10485760,
				'backupCount': 5,
				'formatter': 'email_verbose',
			},
			
			# ============= 新增：Celery日志处理器 =============
			'celery_worker_file': {
				'level': 'INFO',
				'class': 'logging.handlers.RotatingFileHandler',
				'filename': os.path.join(log_dir, 'celery/celery_worker.log'),
				'maxBytes': 10485760,
				'backupCount': 5,
				'formatter': 'verbose',
			},
			'celery_beat_file': {
				'level': 'INFO',
				'class': 'logging.handlers.RotatingFileHandler',
				'filename': os.path.join(log_dir, 'celery/celery_beat.log'),
				'maxBytes': 5242880,
				'backupCount': 3,
				'formatter': 'verbose',
			},
			'celery_error_file': {
				'level': 'ERROR',
				'class': 'logging.handlers.RotatingFileHandler',
				'filename': os.path.join(log_dir, 'celery/celery_error.log'),
				'maxBytes': 10485760,
				'backupCount': 5,
				'formatter': 'verbose',
			},
		},
		'loggers': {
			# 现有系统日志记录器
			'django': {
				'handlers': ['console', 'system_error_file'],
				'level': 'WARNING',
				'propagate': False,
			},
			'wagtail': {
				'handlers': ['console', 'wagtail_error_file'],
				'level': 'WARNING',
				'propagate': False,
			},
			'blog': {
				'handlers': ['console', 'blog_error_file'],
				'level': 'WARNING',
				'propagate': False,
			},
			'search': {
				'handlers': ['console', 'search_error_file'],
				'level': 'WARNING',
				'propagate': False,
			},
			'comments': {
				'handlers': ['console', 'comments_error_file'],
				'level': 'WARNING',
				'propagate': False,
			},
			'archive': {
				'handlers': ['console', 'archive_error_file'],
				'level': 'WARNING',
				'propagate': False,
			},
			'wagtailblog3': {
				'handlers': ['console', 'system_error_file'],
				'level': 'WARNING',
				'propagate': False,
			},
			
			# ============= 新增：Base模块日志记录器 =============
			'base': {
				'handlers': ['console', 'base_error_file', 'base_info_file'],
				'level': 'INFO',
				'propagate': False,
			},
			
			# ============= 新增：邮件相关日志记录器 =============
			'base.models': {
				'handlers': ['console', 'email_operations_file', 'email_error_file'],
				'level': 'INFO',
				'propagate': False,
			},
			'base.tasks': {
				'handlers': ['console', 'email_tasks_file', 'email_error_file'],
				'level': 'INFO',
				'propagate': False,
			},
			'base.rate_limit': {
				'handlers': ['console', 'email_rate_limit_file', 'email_error_file'],
				'level': 'INFO',
				'propagate': False,
			},
			'base.utils': {
				'handlers': ['console', 'email_operations_file', 'email_error_file'],
				'level': 'INFO',
				'propagate': False,
			},
			
			# ============= Celery日志记录器 =============
			'celery': {
				'handlers': ['console', 'celery_worker_file', 'celery_error_file'],
				'level': 'INFO',
				'propagate': False,
			},
			'celery.worker': {
				'handlers': ['celery_worker_file', 'celery_error_file'],
				'level': 'INFO',
				'propagate': False,
			},
			'celery.beat': {
				'handlers': ['celery_beat_file', 'celery_error_file'],
				'level': 'INFO',
				'propagate': False,
			},
			'celery.task': {
				'handlers': ['email_tasks_file', 'celery_error_file'],
				'level': 'INFO',
				'propagate': False,
			},
			
			# ============= 邮件发送核心组件日志 =============
			'django.core.mail': {
				'handlers': ['email_operations_file', 'email_error_file'],
				'level': 'INFO',
				'propagate': False,
			},
			
			# ============= 表单相关日志 =============
			'wagtail.contrib.forms': {
				'handlers': ['console', 'email_operations_file'],
				'level': 'INFO',
				'propagate': False,
			},
		}
	}
	
	# 如果提供了模块过滤器
	if modules_filter:
		config['filters'] = {
			'module_filter': {
				'()': 'wagtailblog3.logging_filters.ModuleFilter', # 自定义过滤器
				'modules': modules_filter,
			}
		}
		
		# 添加过滤器到控制台处理器
		config['handlers']['console']['filters'] = ['module_filter']
	
	return config


def get_email_debug_config():
	"""获取邮件调试专用的日志配置"""
	log_dir = ensure_log_dirs()
	
	return {
		'handlers': {
			'email_debug_file': {
				'level': 'DEBUG',
				'class': 'logging.handlers.RotatingFileHandler',
				'filename': os.path.join(log_dir, 'email/email_debug.log'),
				'maxBytes': 5242880,  # 5MB
				'backupCount': 2,
				'formatter': 'email_verbose',
			},
		},
		'loggers': {
			'base.models': {
				'handlers': ['email_debug_file'],
				'level': 'DEBUG',
				'propagate': True,
			},
			'base.tasks': {
				'handlers': ['email_debug_file'],
				'level': 'DEBUG',
				'propagate': True,
			},
			'base.rate_limit': {
				'handlers': ['email_debug_file'],
				'level': 'DEBUG',
				'propagate': True,
			},
		}
	}


def get_performance_logging_config():
	"""获取性能监控日志配置"""
	log_dir = ensure_log_dirs()
	
	return {
		'handlers': {
			'performance_file': {
				'level': 'INFO',
				'class': 'logging.handlers.RotatingFileHandler',
				'filename': os.path.join(log_dir, 'system/performance.log'),
				'maxBytes': 10485760,
				'backupCount': 3,
				'formatter': 'verbose',
			},
		},
		'loggers': {
			'performance': {
				'handlers': ['performance_file'],
				'level': 'INFO',
				'propagate': False,
			},
		}
	}