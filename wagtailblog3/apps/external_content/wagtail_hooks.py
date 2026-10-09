"""仅加载局部资产、捕获实际 after-hook 完成证据，不执行业务或缓存 IO。"""

from django.http import HttpRequest
from django.templatetags.static import static
from django.utils.html import format_html
from wagtail import hooks


@hooks.register("insert_global_admin_js")
def request_guard_asset() -> str:
    """脚本无受控响应标记时不接管任何表单。"""
    return format_html('<script src="{}" defer></script>',
                       static("external_content/js/request_guard.js"))


@hooks.register("after_copy_page", order=-10000)
def capture_copy_result(request: HttpRequest, source: object, result: object) -> None:
    """复制视图不保留 new_page 属性；仅在当前请求捕获结果，不保存正文。"""
    _capture_result(request, result, "copy")


def _capture_result(request: HttpRequest, page: object, effect: str) -> None:
    """只捕获本次已领取请求的预期动作；删除后主键取删除前身份。"""
    if getattr(request, "_ec_expected_effect", None) != effect:
        return
    pk = getattr(page, "pk", None)
    if effect == "delete" and pk is None:
        pk = getattr(request, "_ec_source_page_id", None)
    if type(pk) is int and pk > 0:
        request._ec_completed_page_id = pk


@hooks.register("after_edit_page", order=-10000)
def capture_edit_result(request: HttpRequest, page: object) -> None:
    _capture_result(request, page, "edit")


@hooks.register("after_create_page", order=-10000)
def capture_create_result(request: HttpRequest, page: object) -> None:
    _capture_result(request, page, "create")


@hooks.register("after_publish_page", order=-10000)
def capture_publish_result(request: HttpRequest, page: object) -> None:
    _capture_result(request, page, "publish")


@hooks.register("after_unpublish_page", order=-10000)
def capture_unpublish_result(request: HttpRequest, page: object) -> None:
    _capture_result(request, page, "unpublish")


@hooks.register("after_delete_page", order=-10000)
def capture_delete_result(request: HttpRequest, page: object) -> None:
    _capture_result(request, page, "delete")


@hooks.register("after_convert_alias_page", order=-10000)
def capture_convert_alias_result(request: HttpRequest, page: object) -> None:
    _capture_result(request, page, "convert_alias")
