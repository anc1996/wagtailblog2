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
        """应用就绪时载入启动检查与生命周期信号。"""
        # 导入 checks 以触发 @register(Tags.models)
        from . import checks  # noqa: F401
        # 导入 signals 以注册生命周期接收器
        from . import signals  # noqa: F401
        # 包装 Wagtail 发布与下架动作以保证整体验收处于原子事务内
        from .publication import wrap_publish_page_revision_action, wrap_unpublish_page_action
        wrap_publish_page_revision_action()
        wrap_unpublish_page_action()
