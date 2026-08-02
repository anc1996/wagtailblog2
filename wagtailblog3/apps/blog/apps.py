# 博客应用配置
from django.apps import AppConfig

class BlogConfig(AppConfig):
	"""注册博客应用，并在启动时加载信号处理器。"""
	default_auto_field = 'django.db.models.BigAutoField'
	name = 'blog'
	
	def ready(self):
		"""当应用准备好时执行初始化"""
		# 只在 Django 应用完成加载后导入信号，避免模型尚未注册时提前连接接收器。
		import blog.signals
