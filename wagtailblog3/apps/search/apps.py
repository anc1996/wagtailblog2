from django.apps import AppConfig


class SearchConfig(AppConfig):
    """注册搜索应用，并在应用就绪时加载信号处理器。"""
    default_auto_field = "django.db.models.BigAutoField"
    name = "search"
    label = "search"
    verbose_name = "搜索"

    def ready(self) -> None:
        """导入信号模块，保持 Django 启动时的注册行为。"""
        from . import signals
