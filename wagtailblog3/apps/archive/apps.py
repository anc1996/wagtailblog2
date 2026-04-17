# archive/apps.py
from django.apps import AppConfig


class ArchiveConfig(AppConfig):
    
    # Archive应用配置类
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'archive' # 应用名称
    verbose_name = '文章归档' # 应用的可读名称