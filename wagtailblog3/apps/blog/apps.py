# blog/apps.py
from django.apps import AppConfig

class BlogConfig(AppConfig):
	default_auto_field = 'django.db.models.BigAutoField'
	name = 'blog'
	
	def ready(self):
		"""当应用准备好时执行初始化"""
		# 导入信号处理器
		import blog.signals
