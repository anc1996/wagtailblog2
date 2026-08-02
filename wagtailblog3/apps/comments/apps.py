# 评论应用配置。
from django.apps import AppConfig


class CommentsConfig(AppConfig):
	"""评论应用的基础配置。"""
	default_auto_field = 'django.db.models.BigAutoField'
	name = 'comments'
	verbose_name = "评论系统"
