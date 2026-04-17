# wagtailblog3/celery.py
import os
from celery import Celery

"""
Celery 配置说明：
- 所有 Celery 配置统一在 settings/database.py 中的 get_celery_config() 函数管理
- 本文件只负责创建 Celery 应用实例并自动加载配置
- 不要在本文件中重复定义配置，保持单一配置源

启动命令示例：
    # 启动默认 worker（处理所有队列）
    celery -A wagtailblog3 worker -l info

    # 启动指定队列的 worker
    celery -A wagtailblog3 worker -l info --queues=email,default

    # 启动 worker 并指定并发数
    celery -A wagtailblog3 worker -l info --concurrency=4 --pool=threads

    # 启动 Celery Beat 定时任务调度器
    celery -A wagtailblog3 beat -l info

    # 同时启动 worker 和 beat
    celery -A wagtailblog3 worker -l info -B

监控命令：
    # 查看活动任务
    celery -A wagtailblog3 inspect active

    # 查看已注册的任务
    celery -A wagtailblog3 inspect registered

    # 查看队列状态
    celery -A wagtailblog3 inspect stats
"""

# ==========================================================
# 设置 Django 默认设置模块
# ==========================================================
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'wagtailblog3.settings.dev')

# ==========================================================
# 创建 Celery 应用实例
# ==========================================================
app = Celery('wagtailblog3')

# ==========================================================
# 从 Django settings 加载 Celery 配置
# ==========================================================
# namespace='CELERY' 表示只加载以 CELERY_ 开头的配置项
# 例如：CELERY_BROKER_URL、CELERY_TASK_ROUTES 等
# 这些配置在 settings/database.py 的 get_celery_config() 中定义
app.config_from_object('django.conf:settings', namespace='CELERY')

# ==========================================================
# 自动发现任务
# ==========================================================
# 自动发现所有已安装应用中的 tasks.py 文件
app.autodiscover_tasks()


# ==========================================================
# 调试任务（用于测试 Celery 是否正常工作）
# ==========================================================
@app.task(bind=True, ignore_result=True)
def debug_task(self):
	"""
	调试任务，用于测试 Celery 配置是否正确

	使用方法：
		from wagtailblog3.celery import debug_task
		debug_task.delay()
	"""
	print(f'Request: {self.request!r}')
	return f'Debug task executed successfully'


@app.task(name='health_check', ignore_result=False)
def health_check():
	"""
	Celery 健康检查任务

	使用方法：
		from wagtailblog3.celery import health_check
		result = health_check.delay()
		print(result.get())

	返回值：
		str: 健康检查状态信息
	"""
	return "Celery is working properly!"


@app.task(name='test_redis_connection', ignore_result=False)
def test_redis_connection():
	"""
	测试 Redis 连接是否正常

	使用方法：
		from wagtailblog3.celery import test_redis_connection
		result = test_redis_connection.delay()
		print(result.get())
	"""
	try:
		from django.core.cache import cache
		# 测试缓存读写
		cache.set('celery_test_key', 'test_value', 10)
		value = cache.get('celery_test_key')
		if value == 'test_value':
			return "Redis connection is working!"
		else:
			return "Redis connection test failed: value mismatch"
	except Exception as e:
		return f"Redis connection test failed: {str(e)}"


# ==========================================================
# Celery 应用启动时的信号
# ==========================================================
@app.on_after_configure.connect
def setup_periodic_tasks(sender, **kwargs):
	"""
	配置周期性任务（可选）

	注意：定时任务的主要配置在 settings/database.py 的
	CELERY_BEAT_SCHEDULE 中定义

	这里可以添加动态的定时任务或一些启动时的初始化逻辑
	"""
	# 示例：每 30 秒执行一次健康检查（取消注释以启用）
	# sender.add_periodic_task(
	#     30.0,  # 每 30 秒
	#     health_check.s(),
	#     name='periodic health check'
	# )
	pass


# ==========================================================
# Celery Worker 启动时的信号
# ==========================================================
from celery.signals import worker_ready


@worker_ready.connect
def on_worker_ready(sender, **kwargs):
	"""
	Worker 启动完成后的回调

	可以在这里执行一些初始化操作，例如：
	- 记录 Worker 启动日志
	- 清理过期的任务结果
	- 发送通知等
	"""
	print("=" * 60)
	print("Celery Worker started successfully!")
	print("Queues:", sender.app.conf.task_queues)
	print("=" * 60)