"""Vditor 剪贴板图片上传使用的 Wagtail 管理端接口。

该模块只负责把一次上传请求转换为 Wagtail 图片对象和结构化 JSON 响应。
权限、图片格式、替代文本、collection 归属和 Wagtail 表单校验均在保存前完成；
缩略图 rendition 属于保存原图后的派生步骤，失败时只记录日志，不让客户端重试整次上传。
"""

import logging
import secrets
import time
from pathlib import Path
from typing import Any

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from wagtail.admin.auth import require_admin_access
from wagtail.images import get_image_model
from wagtail.images.formats import get_image_format
from wagtail.images.forms import get_image_form
from wagtail.permissions import policy_registry


logger = logging.getLogger(__name__)


class _ImagePermissionPolicyProxy:
    """延迟读取图片权限策略，避免 Wagtail 8 应用初始化阶段访问注册表。"""

    def __getattr__(self, name: str) -> Any:
        policy = policy_registry.get_by_type(get_image_model())
        return getattr(policy, name)


permission_policy = _ImagePermissionPolicyProxy()


def _error_response(
    code: str,
    message: str,
    status: int,
    *,
    details: Any = None,
) -> JsonResponse:
    """构造统一的 JSON 错误响应。

    参数：
        code：供前端按程序分支处理的稳定错误编码。
        message：面向调用方的简短错误说明。
        status：HTTP 状态码，例如参数错误使用 400、权限错误使用 403。
        details：可选的结构化补充信息，通常来自 Wagtail 表单字段错误。

    返回：包含 ``error.code`` 和 ``error.message`` 的 :class:`JsonResponse`；
        只有传入非空 ``details`` 时才附加 ``error.details``。
    """
    payload = {"error": {"code": code, "message": message}}
    if details:
        payload["error"]["details"] = details
    return JsonResponse(payload, status=status)


def _resolve_upload_collection(
    request: Any,
) -> tuple[Any, JsonResponse | None]:
    """解析当前用户可写的图片 collection，并返回错误响应（如有）。

    参数：
        request：包含 ``user`` 和 ``POST`` 的 Django 请求对象；``collection_id``
            来自请求参数，默认 collection 则来自配置项。

    返回：``(collection, None)`` 表示可以继续保存；``(None, response)`` 表示
        已构造好 400/403/409 错误响应，调用方应立即返回。

    选择算法按“配置优先、请求其次、单一权限集合自动选择”的顺序执行。查询集合
    本身已经限制为用户拥有 ``add`` 权限的 collection，因此即使请求伪造了 ID，
    也只能命中授权范围内的对象；多个可写集合而没有明确选择时拒绝请求，避免图片
    被静默放入错误目录。
    """
    collections = permission_policy.collections_user_has_permission_for(
        request.user, "add"
    )
    configured_id = getattr(
        settings, "BLOG_VDITOR_IMAGE_UPLOAD_COLLECTION_ID", None
    )
    requested_id = request.POST.get("collection_id")
    collection_id = configured_id if configured_id not in (None, "") else requested_id

    if collection_id not in (None, ""):
        collection_id = str(collection_id)
        if not collection_id.isdigit() or int(collection_id) < 1:
            return None, _error_response(
                "invalid_collection",
                "The image collection ID is invalid.",
                400,
            )
        collection = collections.filter(pk=int(collection_id)).first()
        if collection is None:
            return None, _error_response(
                "collection_forbidden",
                "You cannot add images to this collection.",
                403,
            )
        return collection, None

    collection_count = collections.count()
    if collection_count == 1:
        return collections.first(), None
    if collection_count == 0:
        return None, _error_response(
            "image_add_forbidden",
            "You do not have permission to add images.",
            403,
        )
    return None, _error_response(
        "collection_required",
        "Configure a default collection for automatic image uploads.",
        409,
    )


def _random_title_suffix() -> str:
    """生成固定九位的随机数字后缀。

    返回：零填充到九位的数字字符串。使用 ``secrets`` 而不是普通伪随机模块，
        使并发上传时的碰撞概率足够低；该后缀只用于标题去重，不承担安全令牌职责。
    """
    return f"{secrets.randbelow(1_000_000_000):09d}"


def _safe_upload_title(uploaded_file: Any, image_model: Any) -> str:
    """从文件名生成不会主动覆盖已有图片标题的候选值。

    参数：
        uploaded_file：Django 上传文件对象，读取其 ``name`` 属性。
        image_model：当前 Wagtail 图片模型，用于查询标题是否已存在。

    返回：可写入图片模型的标题字符串。

    算法先去掉扩展名并截断到模型允许的 255 个字符；无冲突时保留原名。若原名为空
    或已占用，则最多生成五个“基础标题-九位后缀”候选，并逐个用数据库 ``exists``
    检查。基础标题在拼接前按 ``254 - len(suffix)`` 截断，为连字符预留一个字符，
    从而保证最终长度不超过 255。五次都碰撞时返回最后一个候选，交由后续 Wagtail
    表单/数据库约束处理，避免在这里无限重试。
    """
    base_title = Path(uploaded_file.name or "").stem.strip()[:255]
    if base_title and not image_model.objects.filter(title=base_title).exists():
        return base_title

    candidate = base_title
    for _attempt in range(5):
        suffix = _random_title_suffix()
        if base_title:
            candidate = f"{base_title[: 254 - len(suffix)]}-{suffix}"
        else:
            candidate = f"pasted-image-{suffix}"
        if not image_model.objects.filter(title=candidate).exists():
            return candidate
    return candidate


def _upload_vditor_image(request: Any) -> JsonResponse:
    """将 Vditor 剪贴板上传转换为 Wagtail 图片并返回 JSON 结果。

    参数：
        request：包含上传文件 ``FILES['file']``、可选格式/替代文本/collection 参数，
            以及已通过管理端认证的 ``user`` 的 Django 请求对象。

    返回：成功时返回 201 和图片元数据/预览地址；输入、权限或格式错误返回 400/403/409；
        保存原图失败返回 500。响应结构由现有前端契约固定，不能在注释批次中改动。

    处理边界：
        ``time.monotonic`` 用于耗时日志，因为它不会受系统时钟校准影响；权限检查、
        格式解析和表单 ``is_valid`` 都在 ``form.save`` 前完成。原图保存成功后才尝试
        生成 rendition，预览是可重建的派生资源；即使预览失败，也必须返回已保存原图的
        结果并记录异常，否则客户端重试会再写入一张重复原图。
    """

    started_at = time.monotonic()
    uploaded_file = request.FILES.get("file")
    if uploaded_file is None:
        return _error_response(
            "file_required", "An image file is required.", 400
        )

    if not permission_policy.user_has_permission(request.user, "add"):
        return _error_response(
            "image_add_forbidden",
            "You do not have permission to add images.",
            403,
        )

    image_format_name = request.POST.get("format") or "fullwidth_web"
    try:
        image_format = get_image_format(image_format_name)
    except KeyError:
        return _error_response(
            "invalid_format", "The image format is not registered.", 400
        )

    alt_text = request.POST.get("alt", "")
    if len(alt_text) > 2048:
        return _error_response(
            "invalid_alt", "Image alternative text is too long.", 400
        )

    collection, collection_error = _resolve_upload_collection(request)
    if collection_error is not None:
        return collection_error

    image_model = get_image_model()
    image_form_class = get_image_form(image_model)
    form_data = request.POST.copy()
    form_data["title"] = form_data.get("title") or _safe_upload_title(
        uploaded_file, image_model
    )
    form_data["collection"] = str(collection.pk)
    image = image_model(uploaded_by_user=request.user)
    form = image_form_class(
        data=form_data,
        files=request.FILES,
        user=request.user,
        instance=image,
    )

    # Wagtail 表单统一处理文件类型、图片内容、标题和 collection 等模型约束；
    # 在这里先完成校验，可以保证无效上传不会进入原图保存路径。
    if not form.is_valid():
        logger.warning(
            "blog_vditor_image_upload_invalid file_type=%s file_size=%s elapsed_ms=%s",
            uploaded_file.content_type or "",
            uploaded_file.size,
            round((time.monotonic() - started_at) * 1000),
        )
        return _error_response(
            "invalid_image",
            "Wagtail rejected the uploaded image.",
            400,
            details=form.errors.get_json_data(escape_html=True),
        )

    try:
        image = form.save()
    except Exception:
        logger.exception(
            "blog_vditor_image_upload_save_failed file_type=%s file_size=%s elapsed_ms=%s",
            uploaded_file.content_type or "",
            uploaded_file.size,
            round((time.monotonic() - started_at) * 1000),
        )
        return _error_response(
            "image_save_failed", "The image could not be saved.", 500
        )

    # 原图已经保存成功，下面只生成可重建的派生 rendition；预览失败不回滚原图，
    # 避免客户端因一次派生资源故障重试而产生重复图片。
    preview = None
    try:
        rendition = image.get_rendition(image_format.filter_spec)
        preview = {
            "url": rendition.url,
            "width": rendition.width,
            "height": rendition.height,
        }
    except Exception:
        logger.exception(
            "blog_vditor_image_upload_preview_failed image_id=%s format=%s",
            image.pk,
            image_format.name,
        )

    logger.info(
        "blog_vditor_image_upload_complete image_id=%s collection_id=%s format=%s "
        "file_type=%s file_size=%s elapsed_ms=%s",
        image.pk,
        collection.pk,
        image_format.name,
        uploaded_file.content_type or "",
        uploaded_file.size,
        round((time.monotonic() - started_at) * 1000),
    )
    return JsonResponse(
        {
            "image": {
                "id": image.pk,
                "title": image.title,
                "alt": alt_text,
                "format": image_format.name,
            },
            "preview": preview,
        },
        status=201,
    )


@require_admin_access
@require_POST
def upload_vditor_image(request: Any) -> JsonResponse:
    """公开注册 Vditor 图片上传视图。

    参数：
        request：Django HTTP 请求对象。

    返回：委托给 :func:`_upload_vditor_image` 的 JSON 响应。

    ``require_admin_access`` 和 ``require_POST`` 形成双重入口保护：前者限制为已登录
    且具备 Wagtail 管理访问权的用户，后者拒绝 GET 等非上传方法；函数内部仍会再次检查
    图片 ``add`` 权限，因为管理访问权不等于对图片 collection 的写权限。
    """
    return _upload_vditor_image(request)
