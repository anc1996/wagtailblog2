from django.apps import AppConfig


class BaseConfig(AppConfig):
    """基础应用配置。"""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'base'
