#!/user/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
from urllib.parse import urlparse

try:
	from .search_runtime import validate_production_content_search_runtime_namespace
except ImportError:
	# WP6 配置测试会以独立脚本载入本文件，此时保留同一校验而不改变运行时行为。
	from wagtailblog3.settings.search_runtime import validate_production_content_search_runtime_namespace


def _env_bool(name, default=False):
	"""Parse a boolean environment value without failing Django startup."""
	value = os.environ.get(name)
	if value is None:
		return default
	value = value.strip().lower()
	if value in {"1", "true", "yes", "on"}:
		return True
	if value in {"0", "false", "no", "off"}:
		return False
	return default


def _env_int(name, default):
	"""Parse an integer environment value with a safe fallback."""
	try:
		return int(os.environ.get(name, default))
	except (TypeError, ValueError):
		return default


def _env_float(name, default):
	"""Parse a float environment value with a safe fallback."""
	try:
		return float(os.environ.get(name, default))
	except (TypeError, ValueError):
		return default


# ==========================================================
# 远程开发依赖服务
# ==========================================================
# 本地项目运行在 20.5，数据库与基础设施服务运行在 20.2。
SERVICE_HOST = os.environ.get("SERVICE_HOST", "192.168.20.2")

# 分析明细清理默认关闭；生产必须在完成备份和独立授权后才可通过环境变量启用。
BLOG_ANALYTICS_CLEANUP_ENABLED = _env_bool("BLOG_ANALYTICS_CLEANUP_ENABLED", False)
BLOG_PAGEVIEW_RETENTION_DAYS = _env_int("BLOG_PAGEVIEW_RETENTION_DAYS", 30)
BLOG_ANALYTICS_CLEANUP_BATCH_SIZE = _env_int("BLOG_ANALYTICS_CLEANUP_BATCH_SIZE", 500)
# 只读对账每轮最多扫描的页面数，避免 Beat 占满 maintenance Worker。
BLOG_PUBLICATION_CONSISTENCY_BATCH_SIZE = max(
	1, min(_env_int("BLOG_PUBLICATION_CONSISTENCY_BATCH_SIZE", 100), 5000)
)
BLOG_PUBLICATION_CONSISTENCY_LEASE_SECONDS = max(
	1, min(_env_int("BLOG_PUBLICATION_CONSISTENCY_LEASE_SECONDS", 240), 3600)
)

# ==========================================================
# MySQL 数据库配置
# ==========================================================
DATABASES = {
	'default': {
		'ENGINE': 'django.db.backends.mysql',
		'NAME': os.environ.get('MYSQL_NAME', 'wagtailsoftblog_test'),
		'USER': os.environ.get('MYSQL_USER', 'root'),
		'PASSWORD': os.environ.get('MYSQL_PASSWORD', ''),
		'HOST': os.environ.get('MYSQL_HOST', SERVICE_HOST),
		'PORT': os.environ.get('MYSQL_PORT', '3306'),
		'OPTIONS': {
			'charset': 'utf8mb4',
			'collation': 'utf8mb4_general_ci',
		}
	}
}

# ==========================================================
# Redis 配置
# ==========================================================
REDIS_HOST = os.environ.get('REDIS_HOST', SERVICE_HOST)
REDIS_PORT = _env_int('REDIS_PORT', 6379)
REDIS_PASSWORD = os.environ.get('REDIS_PASSWORD', '')
REDIS_DB = _env_int('REDIS_DB', 1)  # 用于计数器的数据库
REDIS_CACHE_DB = _env_int('REDIS_CACHE_DB', 1)
REDIS_COMMENT_CACHE_DB = _env_int('REDIS_COMMENT_CACHE_DB', 2)

# Redis 缓存配置
CACHES = {
	'default': {
		'BACKEND': 'django_redis.cache.RedisCache',
		'LOCATION': f'redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_CACHE_DB}',
		'OPTIONS': {
			'CLIENT_CLASS': 'django_redis.client.DefaultClient',
			'PASSWORD': REDIS_PASSWORD
		}
	},
	'comment_rate_limit_cache': {  # 新的缓存实例，专门用于评论频率限制
		'BACKEND': 'django_redis.cache.RedisCache',
		'LOCATION': f'redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_COMMENT_CACHE_DB}',
		'OPTIONS': {
			'CLIENT_CLASS': 'django_redis.client.DefaultClient',
			'PASSWORD': REDIS_PASSWORD
		}
	}
}

# ==========================================================
# MongoDB 配置
# ==========================================================
MONGO_DB = {
	'NAME': os.environ.get('MONGO_NAME', 'wagtailblog_test'),
	'HOST': os.environ.get('MONGO_HOST', SERVICE_HOST),
	'PORT': _env_int('MONGO_PORT', 27017),
}

# MongoDB调试信息开关
MONGO_DEBUG = _env_bool('MONGO_DEBUG', False)

# ==========================================================
# MinIO 存储配置
# ==========================================================
AWS_STORAGE_BUCKET_NAME = os.environ.get('MINIO_BUCKET', 'wagtail-test-bucket')
AWS_S3_ENDPOINT_URL = os.environ.get('MINIO_ENDPOINT', f'http://{SERVICE_HOST}:9000')
AWS_ACCESS_KEY_ID = os.environ.get('MINIO_ACCESS_KEY', '')
AWS_SECRET_ACCESS_KEY = os.environ.get('MINIO_SECRET_KEY', '')
AWS_S3_OBJECT_PARAMETERS = {
	'CacheControl': 'max-age=86400',
}
AWS_QUERYSTRING_AUTH = _env_bool('MINIO_QUERYSTRING_AUTH', False)
AWS_S3_FILE_OVERWRITE = _env_bool('MINIO_FILE_OVERWRITE', False)
AWS_DEFAULT_ACL = os.environ.get('MINIO_DEFAULT_ACL', 'public-read')
AWS_S3_VERIFY = _env_bool('MINIO_VERIFY', False)

# ==========================================================
# 存储后端配置
# ==========================================================
# 默认存储设置，以及更新后的静态文件存储。
# 参见 https://docs.djangoproject.com/en/5.1/ref/settings/#std-setting-STORAGES
STORAGES = {
	"default": {
		"BACKEND": "wagtailblog3.storage_backends.MinioMediaStorage",
	},
	"staticfiles": {
		"BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage",
	},
	"images": {
		"BACKEND": "wagtailblog3.storage_backends.MinioImageStorage",
	},
	"original_images": {
		"BACKEND": "wagtailblog3.storage_backends.MinioOriginalImageStorage",
	},
	"documents": {
		"BACKEND": "wagtailblog3.storage_backends.MinioDocumentStorage",
	},
	# 添加媒体文件存储后端
	"wagtailmedia": {
		"BACKEND": "wagtailblog3.storage_backends.MinioMediaStorage",
	},
}


# ==========================================================
# Celery 异步任务队列配置
# ==========================================================

def get_celery_config(time_zone, redis_host, redis_port, redis_password):
	"""
	生成 Celery 异步任务队列的完整配置

	该函数集中管理所有 Celery 相关的配置项，包括消息代理、任务序列化、
	队列路由、重试策略、超时控制等核心功能配置。

	参数说明:
		time_zone (str): 时区设置，例如 'Asia/Shanghai'，用于任务调度的时间计算
		redis_host (str): Redis 服务器主机地址，例如 'localhost' 或 '192.168.1.100'
		redis_port (int): Redis 服务器端口号，默认为 6379
		redis_password (str): Redis 服务器认证密码

	返回值:
		dict: 包含所有 Celery 配置项的字典，可直接用于更新 Django settings
	"""
	# 测试环境可通过独立队列隔离不同代码版本的 Worker；生产默认值保持兼容。
	maintenance_queue = os.environ.get('CELERY_MAINTENANCE_QUEUE', 'maintenance').strip() or 'maintenance'
	try:
		broker_db = int(os.environ.get('CELERY_BROKER_DB', '2'))
		result_db = int(os.environ.get('CELERY_RESULT_DB', '3'))
	except ValueError:
		broker_db, result_db = 2, 3
	return {
		# 所有显式任务投递都必须读取同一个队列名，测试环境才能与生产 Redis 队列隔离。
		'CELERY_MAINTENANCE_QUEUE': maintenance_queue,
		# --------------------------------------------------
		# 基础时区配置
		# --------------------------------------------------
		'CELERY_TIMEZONE': time_zone,
		# 设置 Celery 使用的时区，确保定时任务按照指定时区执行
		# 例如：'Asia/Shanghai' 表示使用中国标准时间
		
		'CELERY_ENABLE_UTC': True,
		# 启用 UTC 时间存储，内部统一使用 UTC 时间，避免时区转换问题
		# 建议始终设为 True，让 Celery 自动处理时区转换
		
		# --------------------------------------------------
		# 消息代理和结果后端配置
		# --------------------------------------------------
		'CELERY_BROKER_URL': f'redis://:{redis_password}@{redis_host}:{redis_port}/{broker_db}',
		# 消息代理地址，用于存储待执行的任务
		# 格式：redis://[:password]@host:port/db_number
		# 这里使用 Redis DB 2 作为消息队列存储
		
		'CELERY_RESULT_BACKEND': f'redis://:{redis_password}@{redis_host}:{redis_port}/{result_db}',
		# 任务结果存储后端，用于保存任务执行结果
		# 这里使用 Redis DB 3 专门存储任务执行结果
		# 将消息代理和结果后端分开使用不同的 DB，提高性能和数据隔离性
		
		# --------------------------------------------------
		# 任务序列化配置
		# --------------------------------------------------
		'CELERY_TASK_SERIALIZER': 'json',
		# 任务参数的序列化格式，使用 JSON 格式
		# 可选值：'json', 'pickle', 'yaml', 'msgpack'
		# JSON 格式安全性高，推荐用于生产环境
		
		'CELERY_ACCEPT_CONTENT': ['json'],
		# 允许接收的内容类型白名单，只接受 JSON 格式
		# 这是一个安全设置，防止接收不安全的序列化数据（如 pickle）
		
		'CELERY_RESULT_SERIALIZER': 'json',
		# 任务结果的序列化格式，与任务序列化保持一致
		
		# --------------------------------------------------
		# 任务执行模式配置
		# --------------------------------------------------
		'CELERY_TASK_ALWAYS_EAGER': False,
		# 是否启用即时执行模式（同步执行）
		# False：任务异步执行，发送到消息队列由 Worker 处理（生产环境设置）
		# True：任务同步执行，不经过消息队列，直接在当前进程执行（仅用于测试）
		
		'CELERY_TASK_EAGER_PROPAGATES': True,
		# 在即时执行模式下，是否传播任务异常
		# True：异常会直接抛出，便于调试
		# False：异常被捕获并存储在结果后端
		
		'CELERY_WORKER_PREFETCH_MULTIPLIER': 1,
		# Worker 预取任务的数量倍数
		# 1 表示 Worker 每次只预取 1 个任务
		# 较小的值适合执行时间长、资源消耗大的任务，避免任务堆积
		# 较大的值适合执行时间短的任务，提高吞吐量
		
		'CELERY_TASK_ACKS_LATE': True,
		# 延迟任务确认模式
		# True：任务执行完成后才向消息代理确认（推荐设置）
		# False：任务被 Worker 接收后立即确认
		# 设为 True 可以防止 Worker 崩溃导致任务丢失
		
		# --------------------------------------------------
		# 任务路由配置
		# --------------------------------------------------
		'CELERY_TASK_ROUTES': {
			# 将不同类型的任务路由到不同的队列，实现任务优先级和资源隔离
			
			'base.tasks.send_form_confirmation_email': {'queue': 'email'},
			# 表单确认邮件发送任务 → email 队列
			
			'base.tasks.send_admin_notification_email': {'queue': 'email'},
			# 管理员通知邮件发送任务 → email 队列
			
			'base.tasks.send_bulk_email': {'queue': 'email'},
			# 批量邮件发送任务 → email 队列
			
			'base.tasks.cleanup_email_logs': {'queue': maintenance_queue},
			'blog.tasks.cleanup_analytics_details': {'queue': maintenance_queue},
			'blog.tasks.cleanup_markdown_import_artifact': {'queue': maintenance_queue},
			'blog.tasks.dispatch_markdown_import_cleanup_retries': {'queue': maintenance_queue},
			'blog.tasks.dispatch_pending_mongo_cleanup_retries': {'queue': maintenance_queue},
			'blog.tasks.assemble_markdown_import_session': {'queue': maintenance_queue},
			'blog.tasks.expire_markdown_import_sessions': {'queue': maintenance_queue},
			'search.tasks.wake_content_search_delivery': {'queue': maintenance_queue},
			'search.tasks.consume_content_search_delivery': {'queue': maintenance_queue},
			'search.tasks.dispatch_pending_content_search_deliveries': {'queue': maintenance_queue},
			# 邮件日志清理任务 → maintenance 队列
			# 维护类任务使用独立队列，避免影响业务任务
		},
		
		# --------------------------------------------------
		# 队列定义配置
		# --------------------------------------------------
		'CELERY_TASK_DEFAULT_QUEUE': 'default',
		# 默认队列名称，未指定队列的任务会进入此队列
		
		'CELERY_TASK_QUEUES': {
			# 定义三个队列，每个队列有独立的交换器和路由键
			
			'default': {
				'exchange': 'default',  # 交换器名称
				'exchange_type': 'direct',  # 交换器类型：直连模式
				'routing_key': 'default',  # 路由键
			},
			# 默认队列：处理常规业务任务
			
			'email': {
				'exchange': 'email',
				'exchange_type': 'direct',
				'routing_key': 'email',
			},
			# 邮件队列：专门处理邮件发送任务
			# 可以为此队列配置专门的 Worker，优化邮件发送性能
			
			maintenance_queue: {
				'exchange': maintenance_queue,
				'exchange_type': 'direct',
				'routing_key': maintenance_queue,
			},
			# 维护队列：处理数据清理、定期维护等低优先级任务
			# 建议在系统空闲时段处理这类任务
		},
		
		# --------------------------------------------------
		# 任务重试配置
		# --------------------------------------------------
		'CELERY_TASK_DEFAULT_RETRY_DELAY': 60,
		# 任务失败后的默认重试延迟时间（秒）
		# 60 秒后重试，避免立即重试导致资源浪费
		# 可以根据具体任务类型调整，例如网络请求可能需要更长延迟
		
		'CELERY_TASK_MAX_RETRIES': 3,
		# 任务的最大重试次数
		# 重试 3 次后仍失败，任务将被标记为失败
		# 防止无限重试占用资源
		
		# --------------------------------------------------
		# 任务超时配置
		# --------------------------------------------------
		'CELERY_TASK_SOFT_TIME_LIMIT': 300,
		# 任务软超时限制（秒）- 5 分钟
		# 达到软超时后，会向任务发送 SoftTimeLimitExceeded 异常
		# 任务可以捕获此异常进行清理工作，然后优雅退出
		
		'CELERY_TASK_TIME_LIMIT': 600,
		# 任务硬超时限制（秒）- 10 分钟
		# 达到硬超时后，Worker 进程会被强制终止
		# 硬超时应该大于软超时，给任务留出清理时间
		# 防止任务长时间运行占用 Worker
		
		# --------------------------------------------------
		# 结果过期配置
		# --------------------------------------------------
		'CELERY_RESULT_EXPIRES': 3600,
		# 任务结果的过期时间（秒）- 1 小时
		# 超过此时间后，结果会从结果后端自动删除
		# 避免结果数据无限累积占用存储空间
		
		# --------------------------------------------------
		# 错误处理配置
		# --------------------------------------------------
		'CELERY_TASK_REJECT_ON_WORKER_LOST': True,
		# Worker 意外终止时，是否拒绝（重新排队）正在执行的任务
		# True：任务会重新进入队列，由其他 Worker 处理
		# False：任务会丢失
		# 配合 CELERY_TASK_ACKS_LATE=True 使用，提高任务可靠性
		
		'CELERY_TASK_IGNORE_RESULT': False,
		# 是否忽略任务结果
		# False：保存任务结果到结果后端
		# True：不保存结果，适用于不需要获取返回值的任务
		# 保存结果可以方便任务状态追踪和调试
		
		# --------------------------------------------------
		# 监控和事件配置
		# --------------------------------------------------
		'CELERY_SEND_TASK_EVENTS': True,
		# 是否发送任务事件
		# True：Worker 会发送任务开始、成功、失败等事件
		# 这些事件可以被 Flower 等监控工具使用
		
		'CELERY_SEND_EVENTS': True,
		# 是否发送所有类型的事件（包括 Worker 心跳等）
		# True：发送完整的事件信息，便于系统监控
		# 注意：事件发送会增加少量网络开销
		
		'CELERY_TASK_SEND_SENT_EVENT': True,
		# 是否在任务发送到队列时发送事件
		# True：可以追踪任务从创建到执行的完整生命周期
		# 适用于需要详细审计和监控的场景
		
		# --------------------------------------------------
		# 定时任务配置（Celery Beat）
		# --------------------------------------------------
		'CELERY_BEAT_SCHEDULE': {
			# 定义周期性执行的任务
			
			'cleanup-email-logs': {
				# 定时任务唯一标识符
				
				'task': 'base.tasks.cleanup_email_logs',
				# 要执行的任务路径
				
				'schedule': 60 * 60 * 24,
				# 执行间隔：24 小时（86400 秒）
				# 可以使用 crontab 对象实现更复杂的调度
				# 例如：crontab(hour=2, minute=0) 表示每天凌晨 2 点执行
				
				'options': {'queue': maintenance_queue}
				# 任务选项：指定使用 maintenance 队列
				# 维护任务在低优先级队列执行，不影响核心业务
			},
			'dispatch-pending-log-index-sync': {
				'task': 'observability.tasks.dispatch_pending_log_index_sync_jobs',
				'schedule': 30,
				'options': {'queue': maintenance_queue},
			},
			'dispatch-pending-content-search-deliveries': {
				'task': 'search.tasks.dispatch_pending_content_search_deliveries',
				'schedule': 30,
				'options': {'queue': maintenance_queue},
			},
			'dispatch-pending-content-search-scope-jobs': {
				'task': 'search.tasks.dispatch_pending_content_search_scope_jobs',
				'schedule': 30,
				'options': {'queue': maintenance_queue},
			},
			'cleanup-blog-analytics-details': {
				'task': 'blog.tasks.cleanup_analytics_details',
				'schedule': 60 * 60 * 24,
				'options': {'queue': maintenance_queue},
			},
			'dispatch-markdown-import-cleanup-retries': {
				'task': 'blog.tasks.dispatch_markdown_import_cleanup_retries',
				'schedule': 60,
				'options': {'queue': maintenance_queue},
			},
			'dispatch-pending-mongo-cleanup-retries': {
				'task': 'blog.tasks.dispatch_pending_mongo_cleanup_retries',
				'schedule': 60,
				'options': {'queue': maintenance_queue},
			},
			'dispatch-page-deletion-retries': {
				'task': 'blog.tasks.dispatch_page_deletion_retries',
				'schedule': 30,
				'options': {'queue': maintenance_queue},
			},
			'check-blog-publication-consistency': {
				'task': 'blog.tasks.check_publication_consistency',
				'schedule': 300,
				'options': {'queue': maintenance_queue},
			},
			'expire-markdown-import-sessions': {
				'task': 'blog.tasks.expire_markdown_import_sessions',
				'schedule': 300,
				'options': {'queue': maintenance_queue},
			},
			
			# 可以在这里添加更多定时任务
			# 例如：
			# 'send-daily-report': {
			#     'task': 'base.tasks.send_daily_report',
			#     'schedule': crontab(hour=9, minute=0),  # 每天上午 9 点
			#     'options': {'queue': 'email'}
			# },
		},
	}


# 搜索
# 高亮异常时可通过环境变量回退到 WP1 的公开 QuerySet 搜索，不修改索引或内容数据。
SEARCH_HIGHLIGHTS_ENABLED = _env_bool("SEARCH_HIGHLIGHTS_ENABLED", True)

# 内容搜索同步骨架默认关闭；WP3B 之前不得生产事件、投递任务或切换读取路径。
CONTENT_SEARCH_PRODUCER_ENABLED = _env_bool("CONTENT_SEARCH_PRODUCER_ENABLED", False)
CONTENT_SEARCH_CONSUMER_ENABLED = _env_bool("CONTENT_SEARCH_CONSUMER_ENABLED", False)
CONTENT_SEARCH_CURSOR_ENABLED = _env_bool("CONTENT_SEARCH_CURSOR_ENABLED", False)
CONTENT_SEARCH_CURSOR_MAX_AGE_SECONDS = max(
	60,
	min(3600, _env_int("CONTENT_SEARCH_CURSOR_MAX_AGE_SECONDS", 900)),
)
CONTENT_SEARCH_PIT_ENABLED = _env_bool("CONTENT_SEARCH_PIT_ENABLED", False)
CONTENT_SEARCH_PIT_KEEP_ALIVE = os.environ.get("CONTENT_SEARCH_PIT_KEEP_ALIVE", "1m")
SEARCH_SUGGESTIONS_V2_ENABLED = _env_bool("SEARCH_SUGGESTIONS_V2_ENABLED", False)
SEARCH_POPULAR_SUGGESTIONS_ENABLED = _env_bool("SEARCH_POPULAR_SUGGESTIONS_ENABLED", False)
SEARCH_TITLE_SUGGESTIONS_ENABLED = _env_bool("SEARCH_TITLE_SUGGESTIONS_ENABLED", False)
CONTENT_SEARCH_RECONCILE_ENABLED = _env_bool("CONTENT_SEARCH_RECONCILE_ENABLED", False)
CONTENT_SEARCH_DELIVERY_BATCH_SIZE = max(1, _env_int("CONTENT_SEARCH_DELIVERY_BATCH_SIZE", 100))
CONTENT_SEARCH_LEASE_SECONDS = max(1, _env_int("CONTENT_SEARCH_LEASE_SECONDS", 120))
CONTENT_SEARCH_MAX_ATTEMPTS = max(1, _env_int("CONTENT_SEARCH_MAX_ATTEMPTS", 10))
CONTENT_SEARCH_RETRY_BASE_SECONDS = max(1, _env_int("CONTENT_SEARCH_RETRY_BASE_SECONDS", 5))
CONTENT_SEARCH_RETRY_MAX_SECONDS = max(1, _env_int("CONTENT_SEARCH_RETRY_MAX_SECONDS", 3600))
CONTENT_SEARCH_INDEX_PREFIX = os.environ.get(
	"CONTENT_SEARCH_INDEX_PREFIX",
	"wagtailblog-test-content",
)
CONTENT_SEARCH_TITLE_SUGGESTIONS_READ_ALIAS = os.environ.get(
	"CONTENT_SEARCH_TITLE_SUGGESTIONS_READ_ALIAS",
	f"{CONTENT_SEARCH_INDEX_PREFIX}-title-read",
)
CONTENT_SEARCH_CONNECTION_NAME = os.environ.get("CONTENT_SEARCH_CONNECTION_NAME", "default")
# 生产空索引创建必须使用独立连接和显式前缀，避免误写入既有 Wagtail 或日志索引。
CONTENT_SEARCH_PRODUCTION_CONNECTION_NAME = os.environ.get(
	"CONTENT_SEARCH_PRODUCTION_CONNECTION_NAME", ""
).strip()
CONTENT_SEARCH_PRODUCTION_INDEX_PREFIX = os.environ.get(
	"CONTENT_SEARCH_PRODUCTION_INDEX_PREFIX", ""
).strip()
CONTENT_SEARCH_PRODUCTION_BACKUP_ROOT = os.environ.get(
	"CONTENT_SEARCH_PRODUCTION_BACKUP_ROOT", ""
).strip()
CONTENT_SEARCH_PRODUCTION_INDEX_CREATE_ENABLED = _env_bool(
	"CONTENT_SEARCH_PRODUCTION_INDEX_CREATE_ENABLED", False
)
# 生产回填会读取正式 Mongo 正文并写入 ES/MySQL 审计状态，必须在独立窗口显式放行。
CONTENT_SEARCH_PRODUCTION_REBUILD_ENABLED = _env_bool(
	"CONTENT_SEARCH_PRODUCTION_REBUILD_ENABLED", False
)
# 前台读切换必须先完成生产 alias/状态门禁，再由独立开关显式放行。
CONTENT_SEARCH_PRODUCTION_QUERY_SWITCH_ENABLED = _env_bool(
	"CONTENT_SEARCH_PRODUCTION_QUERY_SWITCH_ENABLED", False
)
# 生产可复用现有 ES 单节点，但必须使用独立逻辑连接名和内容索引前缀。
CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_ENABLED = _env_bool(
	"CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_ENABLED", False
)
CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_URL = os.environ.get(
	"CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_URL", ""
).strip()
CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_VERIFY_CERTS = _env_bool(
	"CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_VERIFY_CERTS", True
)
CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_AUTH_MODE = os.environ.get(
	"CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_AUTH_MODE", "none"
).strip().lower()
CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_API_KEY = os.environ.get(
	"CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_API_KEY", ""
)
CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_USERNAME = os.environ.get(
	"CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_USERNAME", ""
)
CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_PASSWORD = os.environ.get(
	"CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_PASSWORD", ""
)
CONTENT_SEARCH_SECONDARY_CONNECTION_ENABLED = _env_bool(
	"CONTENT_SEARCH_SECONDARY_CONNECTION_ENABLED",
	False,
)
CONTENT_SEARCH_SECONDARY_CONNECTION_NAME = os.environ.get(
	"CONTENT_SEARCH_SECONDARY_CONNECTION_NAME",
	"content_secondary",
).strip()
CONTENT_SEARCH_SECONDARY_URL = os.environ.get("CONTENT_SEARCH_SECONDARY_URL", "").strip()
CONTENT_SEARCH_SECONDARY_INDEX_PREFIX = os.environ.get(
	"CONTENT_SEARCH_SECONDARY_INDEX_PREFIX",
	"wagtailblog-secondary",
).strip()
CONTENT_SEARCH_SECONDARY_VERIFY_CERTS = _env_bool(
	"CONTENT_SEARCH_SECONDARY_VERIFY_CERTS",
	True,
)
CONTENT_SEARCH_SECONDARY_CA_CERTS = os.environ.get(
	"CONTENT_SEARCH_SECONDARY_CA_CERTS",
	"",
).strip()
CONTENT_SEARCH_SECONDARY_AUTH_MODE = os.environ.get(
	"CONTENT_SEARCH_SECONDARY_AUTH_MODE",
	"",
).strip().lower()
CONTENT_SEARCH_SECONDARY_API_KEY = os.environ.get("CONTENT_SEARCH_SECONDARY_API_KEY", "")
CONTENT_SEARCH_SECONDARY_USERNAME = os.environ.get("CONTENT_SEARCH_SECONDARY_USERNAME", "")
CONTENT_SEARCH_SECONDARY_PASSWORD = os.environ.get("CONTENT_SEARCH_SECONDARY_PASSWORD", "")
CONTENT_SEARCH_READ_ALIAS = os.environ.get(
	"CONTENT_SEARCH_READ_ALIAS",
	f"{CONTENT_SEARCH_INDEX_PREFIX}-read",
)

# 生产独立搜索一旦参与读写，运行时命名空间必须明确绑定生产配置，禁止静默回退到测试默认值。
validate_production_content_search_runtime_namespace(
	environment=os.environ.get("WAGTAILBLOG_ENV", "test").strip().lower(),
	feature_flags=(
		CONTENT_SEARCH_PRODUCER_ENABLED,
		CONTENT_SEARCH_CONSUMER_ENABLED,
		CONTENT_SEARCH_CURSOR_ENABLED,
		CONTENT_SEARCH_PIT_ENABLED,
		SEARCH_SUGGESTIONS_V2_ENABLED,
		SEARCH_TITLE_SUGGESTIONS_ENABLED,
		CONTENT_SEARCH_RECONCILE_ENABLED,
	),
	runtime_connection_name=CONTENT_SEARCH_CONNECTION_NAME,
	runtime_index_prefix=CONTENT_SEARCH_INDEX_PREFIX,
	runtime_read_alias=CONTENT_SEARCH_READ_ALIAS,
	production_connection_name=CONTENT_SEARCH_PRODUCTION_CONNECTION_NAME,
	production_index_prefix=CONTENT_SEARCH_PRODUCTION_INDEX_PREFIX,
)

# 独立内容索引原型默认单主分片、零副本，生产容量参数必须在单独发布门禁中重新确认。
CONTENT_SEARCH_INDEX_SHARDS = max(1, _env_int("CONTENT_SEARCH_INDEX_SHARDS", 1))
CONTENT_SEARCH_INDEX_REPLICAS = max(0, _env_int("CONTENT_SEARCH_INDEX_REPLICAS", 0))
CONTENT_SEARCH_INDEX_REFRESH_INTERVAL = os.environ.get(
	"CONTENT_SEARCH_INDEX_REFRESH_INTERVAL",
	"30s",
)
# 在线回填默认从较小批次开始，生产容量参数必须依据实测重新确认。
CONTENT_SEARCH_REBUILD_BATCH_SIZE = max(
	1,
	min(_env_int("CONTENT_SEARCH_REBUILD_BATCH_SIZE", 200), 1000),
)
CONTENT_SEARCH_REBUILD_MAX_BATCH_BYTES = max(
	1024,
	_env_int("CONTENT_SEARCH_REBUILD_MAX_BATCH_BYTES", 4 * 1024 * 1024),
)

WAGTAILSEARCH_BACKENDS = {
	'default': {
		# 1. 搜索引擎底层的通用适配器驱动
		# 使用 Wagtail 原生支持的 Elasticsearch 8.x 版本官方后端
		'BACKEND': 'wagtail.search.backends.elasticsearch8',
		
		# 2. ES 集群的物理连接地址
		# 本地单节点或集群网关的 REST API 端点
		'URLS': [os.environ.get('ELASTICSEARCH_URL', f'http://{SERVICE_HOST}:9200')],
		
		# 3. 隔离命名空间（索引前缀）
		# 在多套环境（如开发/测试/生产）公用同一个 ES 集群时，防止索引冲突
		# 实际生成的索引名形如: wagtailblog__apps_blog_blogpage 等
		'INDEX_PREFIX': os.environ.get('ELASTICSEARCH_INDEX_PREFIX', 'wagtailblog-test'),
		
		# 4. HTTP 网络超时时间（单位：秒）
		# 限制 Django 向 ES 发送检索或批量建立索引（Bulk Indexing）时的最大等待时间
		'TIMEOUT': _env_int('ELASTICSEARCH_TIMEOUT', 10),
		
		# =========================================================================
		# 核心高级配置：深度定制 Elasticsearch 索引级别的分词行为 (IK Analyzer)
		# =========================================================================
		'INDEX_SETTINGS': {
			'settings': {
				'analysis': {
					'analyzer': {
						# ---------------------------------------------------------
						# 【A. 索引构建分词器 (Index Time Analyzer)】
						# ---------------------------------------------------------
						# 作用：在文章“保存、发布或批量跑 rebuild_index”时使用。
						# 原理：Wagtail 默认会调用这个 analyzer 来为索引字段生成词项倒排索引。
						# 策略：这里强行绑定 `ik_max_word`（最细粒度拆分）。
						# 举例："Python的生态力量" 会被切分为：python, 生态, 力量, 生态力量...
						# 目的：将分词的“网”撒到最大，极限榨干 L1 阶段的全局召回率，确保长尾数据绝不漏搜。
						'default': {
							'type': 'custom',
							'tokenizer': 'ik_max_word'
						},
						
						# ---------------------------------------------------------
						# 【B. 前台搜索分词器 (Query Time Analyzer)】
						# ---------------------------------------------------------
						# 作用：在前台用户输入关键词进行检索（Search Time）时使用。
						# 原理：ES 会自动拦截用户输入的 query 字符串，先过一遍分词，再去倒排索引匹配。
						# 策略：这里强行绑定 `ik_smart`（最粗/智能粒度拆分）。
						# 举例：用户输入 "Python的生态力量"，会被切分为："python", "生态", "力量"（不拆出重复词）。
						# 目的：防止在搜索阶段产生由于碎词过多导致的无意义召回和算分噪音（提高搜索准确度度）。
						'default_search': {
							'type': 'custom',
							'tokenizer': 'ik_smart'
						}
					}
				}
			}
		}
	}
}

if CONTENT_SEARCH_SECONDARY_CONNECTION_ENABLED:
	if not CONTENT_SEARCH_SECONDARY_CONNECTION_NAME:
		raise ValueError("独立 Elasticsearch 连接名不能为空")
	secondary_url = urlparse(CONTENT_SEARCH_SECONDARY_URL)
	if (
		secondary_url.scheme != "https"
		or not secondary_url.netloc
		or secondary_url.username
		or secondary_url.password
	):
		raise ValueError("独立 Elasticsearch 集群必须使用不含凭据的 HTTPS URL")
	if not CONTENT_SEARCH_SECONDARY_VERIFY_CERTS:
		raise ValueError("独立 Elasticsearch 集群必须校验证书")
	if CONTENT_SEARCH_SECONDARY_AUTH_MODE not in {"api_key", "basic"}:
		raise ValueError("独立 Elasticsearch 必须配置 api_key 或 basic 认证模式")
	if CONTENT_SEARCH_SECONDARY_AUTH_MODE == "api_key" and not CONTENT_SEARCH_SECONDARY_API_KEY:
		raise ValueError("独立 Elasticsearch API Key 未配置")
	if CONTENT_SEARCH_SECONDARY_AUTH_MODE == "basic" and not (
		CONTENT_SEARCH_SECONDARY_USERNAME and CONTENT_SEARCH_SECONDARY_PASSWORD
	):
		raise ValueError("独立 Elasticsearch basic 认证信息未配置")
	if CONTENT_SEARCH_SECONDARY_CONNECTION_NAME in WAGTAILSEARCH_BACKENDS:
		raise ValueError("独立 Elasticsearch 连接名不能覆盖已有连接")
	secondary_options = {
		"verify_certs": CONTENT_SEARCH_SECONDARY_VERIFY_CERTS,
	}
	if CONTENT_SEARCH_SECONDARY_CA_CERTS:
		secondary_options["ca_certs"] = CONTENT_SEARCH_SECONDARY_CA_CERTS
	if CONTENT_SEARCH_SECONDARY_AUTH_MODE == "api_key":
		secondary_options["api_key"] = CONTENT_SEARCH_SECONDARY_API_KEY
	else:
		secondary_options["basic_auth"] = (
			CONTENT_SEARCH_SECONDARY_USERNAME,
			CONTENT_SEARCH_SECONDARY_PASSWORD,
		)
	WAGTAILSEARCH_BACKENDS[CONTENT_SEARCH_SECONDARY_CONNECTION_NAME] = {
		"BACKEND": "wagtail.search.backends.elasticsearch8",
		"URLS": [CONTENT_SEARCH_SECONDARY_URL],
		"INDEX_PREFIX": CONTENT_SEARCH_SECONDARY_INDEX_PREFIX,
		"TIMEOUT": _env_int("CONTENT_SEARCH_SECONDARY_TIMEOUT", 10),
		# 独立内容索引由 Outbox/Delivery 维护，禁止 Wagtail signal 额外创建页面索引。
		"AUTO_UPDATE": False,
		"OPTIONS": secondary_options,
	}

if CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_ENABLED:
	if not CONTENT_SEARCH_PRODUCTION_CONNECTION_NAME:
		raise ValueError("生产内容搜索逻辑连接名不能为空")
	if CONTENT_SEARCH_PRODUCTION_CONNECTION_NAME in WAGTAILSEARCH_BACKENDS:
		raise ValueError("生产内容搜索逻辑连接名不能覆盖已有连接")
	production_cluster_url = urlparse(CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_URL)
	if (
		production_cluster_url.scheme not in {"http", "https"}
		or not production_cluster_url.netloc
		or production_cluster_url.username
		or production_cluster_url.password
	):
		raise ValueError("生产现有 ES 集群必须配置不含凭据的 HTTP(S) URL")
	if production_cluster_url.scheme == "https" and not CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_VERIFY_CERTS:
		raise ValueError("生产 HTTPS ES 集群必须校验证书")
	if CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_AUTH_MODE not in {"none", "api_key", "basic"}:
		raise ValueError("生产现有 ES 集群认证模式必须为 none、api_key 或 basic")
	if (
		CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_AUTH_MODE == "api_key"
		and not CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_API_KEY
	):
		raise ValueError("生产现有 ES 集群 API Key 未配置")
	if CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_AUTH_MODE == "basic" and not (
		CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_USERNAME
		and CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_PASSWORD
	):
		raise ValueError("生产现有 ES 集群 basic 认证信息未配置")
	production_options = {
		"verify_certs": CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_VERIFY_CERTS,
	}
	if CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_AUTH_MODE == "api_key":
		production_options["api_key"] = CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_API_KEY
	elif CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_AUTH_MODE == "basic":
		production_options["basic_auth"] = (
			CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_USERNAME,
			CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_PASSWORD,
		)
	WAGTAILSEARCH_BACKENDS[CONTENT_SEARCH_PRODUCTION_CONNECTION_NAME] = {
		"BACKEND": "wagtail.search.backends.elasticsearch8",
		"URLS": [CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_URL],
		"INDEX_PREFIX": CONTENT_SEARCH_PRODUCTION_INDEX_PREFIX,
		"TIMEOUT": _env_int("CONTENT_SEARCH_PRODUCTION_CONNECTION_TIMEOUT", 10),
		# 生产精简索引只接受外部版本写入，避免自动更新绕过公开边界和幂等协议。
		"AUTO_UPDATE": False,
		"OPTIONS": production_options,
	}

# 日志检索使用独立的 Elasticsearch 命名空间，不与 Wagtail 内容索引混用。
# 默认关闭，保持现有文件读取链路；配置索引和采集器后再通过环境变量启用。
ELASTICSEARCH_LOGGING = {
	"ENABLED": _env_bool("ELASTICSEARCH_LOG_ENABLED", False),
	"URLS": [
		os.environ.get(
			"ELASTICSEARCH_LOG_URL",
			f"http://{SERVICE_HOST}:9200",
		)
	],
	"READ_INDEX": os.environ.get(
		"ELASTICSEARCH_LOG_READ_INDEX",
		"wagtailblog-test-logs-read",
	),
	"WRITE_INDEX": os.environ.get(
		"ELASTICSEARCH_LOG_WRITE_INDEX",
		"wagtailblog-test-logs-write",
	),
	"INGEST_PIPELINE": os.environ.get(
		"ELASTICSEARCH_LOG_INGEST_PIPELINE",
		"wagtailblog-test-logs-normalize-v2",
	),
	"TIMEZONE": os.environ.get("ELASTICSEARCH_LOG_TIMEZONE", "Asia/Shanghai"),
	"TIMEOUT": _env_float("ELASTICSEARCH_LOG_TIMEOUT", 2.0),
	"VERIFY_CERTS": _env_bool("ELASTICSEARCH_LOG_VERIFY_CERTS", True),
	"FAILURE_COOLDOWN": _env_int("ELASTICSEARCH_LOG_FAILURE_COOLDOWN", 5),
	"DELETE_SYNC_MAX_BYTES": _env_int(
		"ELASTICSEARCH_LOG_DELETE_SYNC_MAX_BYTES",
		10 * 1024 * 1024,
	),
	"DELETE_SYNC_MAX_SELECTORS": _env_int(
		"ELASTICSEARCH_LOG_DELETE_SYNC_MAX_SELECTORS",
		12,
	),
	"CA_CERTS": os.environ.get("ELASTICSEARCH_LOG_CA_CERTS", ""),
	"AUTH_MODE": os.environ.get("ELASTICSEARCH_LOG_AUTH_MODE", "").strip().lower(),
	"API_KEY": os.environ.get("ELASTICSEARCH_LOG_API_KEY", ""),
	"USERNAME": os.environ.get("ELASTICSEARCH_LOG_USERNAME", ""),
	"PASSWORD": os.environ.get("ELASTICSEARCH_LOG_PASSWORD", ""),
	"NUMBER_OF_SHARDS": _env_int("ELASTICSEARCH_LOG_SHARDS", 1),
	"NUMBER_OF_REPLICAS": _env_int("ELASTICSEARCH_LOG_REPLICAS", 0),
	"ILM_POLICY": os.environ.get("ELASTICSEARCH_LOG_ILM_POLICY", ""),
}

# ==========================================================
# 数据库连接信息打印函数
# ==========================================================
def print_database_config(stream=None):
	"""打印当前数据库配置信息；诊断输出默认走 stderr。"""
	stream = stream or sys.stderr
	write = lambda value="": print(value, file=stream)
	write("=" * 60)
	write("     系统核心引擎与数据库配置     ")
	write(f"  [MySQL]  数据库: {DATABASES['default']['NAME']}")
	
	# 假设你定义了 MONGO_DB 字典，如果没有请根据你的实际变量名调整
	mongo_db_name = globals().get('MONGO_DB', {}).get('NAME', '未配置')
	write(f"  [MongoDB] 数据库: {mongo_db_name}")
	write(f"  [Redis]  主机: {REDIS_HOST}:{REDIS_PORT}")
	
	# 假设你定义了 AWS_STORAGE_BUCKET_NAME 变量
	minio_bucket = globals().get('AWS_STORAGE_BUCKET_NAME', '未配置')
	write(f"  [MinIO]  Bucket: {minio_bucket}")
	
	# 🌟 新增：动态侦测并打印 Wagtail 搜索引擎 (Elasticsearch) 的加载状态
	es_config = WAGTAILSEARCH_BACKENDS.get('default', {})
	# 截取 backend 字符串的最后一部分 (例如: wagtail.search.backends.elasticsearch8 -> elasticsearch8)
	es_backend = es_config.get('BACKEND', '未加载').split('.')[-1]
	es_url = es_config.get('URLS', ['未配置'])[0]
	es_index = es_config.get('INDEX_PREFIX', '未配置')
	
	write(f"  [Search] 引擎: {es_backend.upper()}")
	write(f"           节点: {es_url}")
	write(f"           索引前缀: {es_index}")
	write("=" * 60)
