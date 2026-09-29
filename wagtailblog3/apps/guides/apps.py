"""专题指南应用配置。"""

from django.apps import AppConfig


class GuidesConfig(AppConfig):
    """专题指南应用配置。"""

    default_auto_field = "django.db.models.BigAutoField"
    name = "guides"
    verbose_name = "专题指南"

    def ready(self) -> None:
        """应用就绪时注册信号监听器。"""
        import guides.signals  # noqa: F401
