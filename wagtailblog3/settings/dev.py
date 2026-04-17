# wagtailblog3/settings/dev.py

from .base import *

# 安全警告：请勿在生产环境中开启调试的情况下运行！
DEBUG = True

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = "django-insecure-k@nr)u9ylv@5(i_cdp0za#$ofi0764)9(6r9*2^30(-7cwz)h="

# 开发环境表单调试设置
FORM_DEBUG = True  # 启用表单调试模式
WAGTAILFORMS_HELP_TEXT_ALLOW_HTML = True  # 允许在帮助文本中使用HTML

# 开发环境CSRF设置
CSRF_COOKIE_SECURE = False
CSRF_COOKIE_HTTPONLY = False  # 开发环境可设为False便于调试

# 安全警告：在生产环境中定义正确的主机！
ALLOWED_HOSTS = ["*"]

# JWT 配置
from datetime import timedelta

SIMPLE_JWT = {
	'ACCESS_TOKEN_LIFETIME': timedelta(hours=1),  # 访问令牌的有效期，设为1小时
	'REFRESH_TOKEN_LIFETIME': timedelta(days=1),  # 刷新令牌的有效期，设为1天
	'ROTATE_REFRESH_TOKENS': False,  # 是否在刷新访问令牌时同时更新刷新令牌
	'BLACKLIST_AFTER_ROTATION': True,  # 令牌轮换后是否将旧的刷新令牌加入黑名单
	'UPDATE_LAST_LOGIN': False,  # 是否在认证成功后更新用户的最后登录时间
	
	'ALGORITHM': 'HS256',  # JWT签名算法，HMAC-SHA256是常用的对称算法
	'SIGNING_KEY': SECRET_KEY,  # 签名密钥，使用Django的SECRET_KEY
	'VERIFYING_KEY': None,  # 验证密钥，对称算法不需要单独指定
	'AUDIENCE': None,  # 令牌的目标接收者，None表示不验证此声明
	'ISSUER': None,  # 令牌的签发者，None表示不验证此声明
	
	'AUTH_HEADER_TYPES': ('Bearer',),  # 认证头类型，使用Bearer标准格式
	'AUTH_HEADER_NAME': 'HTTP_AUTHORIZATION',  # 认证头名称，对应HTTP的Authorization头
	'USER_ID_FIELD': 'id',  # 用户模型中用于标识用户的字段名
	'USER_ID_CLAIM': 'user_id',  # JWT中存储用户ID的声明名称
	
	'AUTH_TOKEN_CLASSES': ('rest_framework_simplejwt.tokens.AccessToken',),  # 允许的令牌类型
	'TOKEN_TYPE_CLAIM': 'token_type',  # 在JWT中标识令牌类型的声明名称
	
	'JTI_CLAIM': 'jti',  # JWT ID声明字段名，用于唯一标识令牌
	
	'SLIDING_TOKEN_REFRESH_EXP_CLAIM': 'refresh_exp',  # 滑动令牌中刷新过期时间的声明字段名
	'SLIDING_TOKEN_LIFETIME': timedelta(minutes=5),  # 滑动令牌的有效期，5分钟
	'SLIDING_TOKEN_REFRESH_LIFETIME': timedelta(days=1),  # 滑动刷新令牌的有效期，1天
}

# ===========================================================
# 增强调试配置 - 集成邮件和频率限制日志
# ===========================================================

import os

LOG_DIR = os.path.join(BASE_DIR, 'logs')

# 引入更新后的日志配置
from wagtailblog3.logging_config import get_logging_config, get_email_debug_config

LOGGING = get_logging_config()

# 从环境变量获取日志级别和模块
log_level = os.environ.get('DJANGO_LOG_LEVEL', 'WARNING')
log_module = os.environ.get('DJANGO_LOG_MODULE', '')

# 更新日志配置
if log_module:
	LOGGING = get_logging_config(modules_filter=[log_module])

# 如果指定了日志级别，更新控制台处理器
if log_level != 'WARNING':
	LOGGING['handlers']['console']['level'] = log_level

# MongoDB调试信息
MONGO_DEBUG = True  # 在MongoDB适配器中使用此设置来控制详细日志输出

# ===========================================================
# 邮件系统专用调试配置
# ===========================================================

# 启用邮件系统详细日志记录
EMAIL_DEBUG_ENABLED = DEBUG

if DEBUG and EMAIL_DEBUG_ENABLED:
	# 合并邮件调试配置
	email_debug_config = get_email_debug_config()
	
	# 更新处理器
	LOGGING['handlers'].update(email_debug_config['handlers'])
	
	# 更新或添加邮件相关的日志记录器
	for logger_name, logger_config in email_debug_config['loggers'].items():
		if logger_name in LOGGING['loggers']:
			# 如果日志记录器已存在，合并处理器
			existing_handlers = LOGGING['loggers'][logger_name].get('handlers', [])
			new_handlers = logger_config.get('handlers', [])
			LOGGING['loggers'][logger_name]['handlers'] = list(set(existing_handlers + new_handlers))
			# 设置更详细的日志级别
			LOGGING['loggers'][logger_name]['level'] = 'DEBUG'
		else:
			# 新增日志记录器
			LOGGING['loggers'][logger_name] = logger_config

# 表单调试设置 - 增强版
if DEBUG:
	# 启用详细的表单提交日志
	LOGGING['loggers']['wagtail.contrib.forms'] = {
		'handlers': ['console', 'email_operations_file'],
		'level': 'DEBUG',
		'propagate': True,
	}
	
	# 启用Django邮件后端的详细日志
	LOGGING['loggers']['django.core.mail.backends'] = {
		'handlers': ['console', 'email_operations_file'],
		'level': 'DEBUG',
		'propagate': False,
	}

# ===========================================================
# 开发环境专用的邮件频率限制配置
# ===========================================================

# 开发环境可以设置更短的频率限制时间便于测试
if DEBUG:
	# 开发环境设置为1分钟，便于测试
	EMAIL_RATE_LIMIT_SECONDS = 60
	
	# 可以通过环境变量快速调整
	EMAIL_RATE_LIMIT_SECONDS = int(os.environ.get('DEV_EMAIL_RATE_LIMIT', 60))

# ===========================================================
# Celery开发环境配置
# ===========================================================

# 开发环境Celery设置
CELERY_TASK_ALWAYS_EAGER = os.environ.get('CELERY_ALWAYS_EAGER', 'False').lower() == 'true'
CELERY_TASK_EAGER_PROPAGATES = True

# 开发环境启用更详细的任务日志
if DEBUG:
	LOGGING['loggers']['celery.task'] = {
		'handlers': ['console', 'email_tasks_file'],
		'level': 'DEBUG',
		'propagate': False,
	}

# ===========================================================
# 开发环境监控和调试工具
# ===========================================================

# 启用SQL查询日志（仅在需要时取消注释）
# LOGGING['loggers']['django.db.backends'] = {
#     'handlers': ['console'],
#     'level': 'DEBUG',
#     'propagate': False,
# }

# 开发环境性能监控
PERFORMANCE_MONITORING_ENABLED = os.environ.get('PERFORMANCE_MONITORING', 'False').lower() == 'true'

if PERFORMANCE_MONITORING_ENABLED:
	from wagtailblog3.logging_config import get_performance_logging_config
	
	performance_config = get_performance_logging_config()
	LOGGING['handlers'].update(performance_config['handlers'])
	LOGGING['loggers'].update(performance_config['loggers'])

# ===========================================================
# 开发环境邮件后端配置
# ===========================================================

# 开发环境可以选择使用控制台邮件后端进行测试
USE_CONSOLE_EMAIL_BACKEND = os.environ.get('USE_CONSOLE_EMAIL', 'False').lower() == 'true'

if USE_CONSOLE_EMAIL_BACKEND:
	EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
	# 仍然启用异步任务，但邮件会输出到控制台
	CELERY_TASK_ALWAYS_EAGER = True

# ===========================================================
# 开发环境日志管理命令
# ===========================================================

# 可以通过环境变量控制特定模块的日志级别
CUSTOM_LOG_LEVELS = {
	'BASE_LOG_LEVEL': os.environ.get('BASE_LOG_LEVEL', 'INFO'),
	'EMAIL_LOG_LEVEL': os.environ.get('EMAIL_LOG_LEVEL', 'INFO'),
	'CELERY_LOG_LEVEL': os.environ.get('CELERY_LOG_LEVEL', 'INFO'),
}

# 应用自定义日志级别
for module_group, level in CUSTOM_LOG_LEVELS.items():
	if module_group == 'BASE_LOG_LEVEL':
		modules = ['base', 'base.models', 'base.tasks', 'base.utils', 'base.rate_limit']
	elif module_group == 'EMAIL_LOG_LEVEL':
		modules = ['django.core.mail', 'django.core.mail.backends']
	elif module_group == 'CELERY_LOG_LEVEL':
		modules = ['celery', 'celery.worker', 'celery.beat', 'celery.task']
	else:
		continue
	
	for module in modules:
		if module in LOGGING['loggers']:
			LOGGING['loggers'][module]['level'] = level

try:
	from .local import *
except ImportError:
	pass