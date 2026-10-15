"""任意外置字段页面的后台桥接；不包含 Guide 的业务校验或 body 假设。"""
from functools import wraps
from typing import Any

from django.db import transaction
from wagtail.admin.forms.pages import WagtailAdminPageForm


class ExternalContentPageForm(WagtailAdminPageForm):
    """只授权原生中间元数据保存，正文仍只能由 save_revision 写入版本。"""

    def save(self, commit: bool = True) -> Any:
        from external_content.mixins import controlled_external_content_save

        with controlled_external_content_save():
            return super().save(commit=commit)


def _wrap_external_content_create_post(view_class: type[Any]) -> None:
    """围住原生 add_child → save_revision，失败回滚 SQL 身份和树变动。

    不改树创建顺序、不预先插入页面、不把 Mongo 孤儿等同于 SQL 回滚。
    范围限于外置正文 CreateView POST，普通页面透明透传，安装幂等。
    """
    original = view_class.post
    if getattr(original, "_external_content_create_atomic", False):
        return

    @wraps(original)
    def post(self: Any, *args: Any, **kwargs: Any) -> Any:
        from external_content.mixins import ExternalContentPageMixin

        if not isinstance(self.page, ExternalContentPageMixin):
            return original(self, *args, **kwargs)
        with transaction.atomic():
            return original(self, *args, **kwargs)

    post._external_content_create_atomic = True
    view_class.post = post


def install_external_content_create_bridge() -> None:
    """仅 registry ready 后安装，模型导入期间不可加载后台 View/load_model。"""
    from django.apps import apps

    if not apps.ready:
        return
    from wagtail.admin.views.pages.create import CreateView

    _wrap_external_content_create_post(CreateView)
