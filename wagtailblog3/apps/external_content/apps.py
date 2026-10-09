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
        # 包装 Wagtail 发布、下架与工作流完成动作以保证整体验收处于原子事务与单调锁序保护内
        from .publication import (
            wrap_publish_page_revision_action,
            wrap_unpublish_page_action,
            wrap_workflow_state_finish,
        )
        wrap_publish_page_revision_action()
        wrap_unpublish_page_action()
        wrap_workflow_state_finish()

        # 严格自检断言：确保 5 大原生动作与状态类已被成功包装，严禁静默 Fail-Open
        from wagtail.actions.publish_revision import PublishRevisionAction
        from wagtail.actions.publish_page_revision import PublishPageRevisionAction
        from wagtail.actions.unpublish import UnpublishAction
        from wagtail.actions.unpublish_page import UnpublishPageAction
        from wagtail.models import WorkflowState

        for target_cls in [
            PublishRevisionAction,
            PublishPageRevisionAction,
            UnpublishAction,
            UnpublishPageAction,
            WorkflowState,
        ]:
            if not getattr(target_cls, "_external_content_atomic_wrapped", False):
                raise RuntimeError(f"{target_cls.__name__} 未被 external_content 成功包装，Fail-Closed")

        from .copying import (
            wrap_convert_alias_page_action,
            wrap_copy_page_action,
            wrap_create_page_alias_action,
        )
        wrap_copy_page_action()
        wrap_create_page_alias_action()
        wrap_convert_alias_page_action()

        from .deletion import wrap_native_page_deletion
        wrap_native_page_deletion()

        # 防重接入只包装单页 Web 入口，启动时不读取缓存或调用业务动作。
        from .request_bridge import install_request_bridge
        install_request_bridge()
