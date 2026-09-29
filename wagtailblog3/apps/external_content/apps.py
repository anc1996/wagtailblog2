"""
Wagtail Page 正文外置通用协议 - Django AppConfig。
"""

from django.apps import AppConfig


class ExternalContentConfig(AppConfig):
    """外置正文通用协议应用配置。"""

    default_auto_field = "django.db.models.BigAutoField"
    name = "external_content"
    verbose_name = "Wagtail Page 正文外置通用协议"

    def ready(self) -> None:
        """应用就绪时载入启动检查。"""
        # 导入 checks 以触发 @register(Tags.models)
        from . import checks  # noqa: F401