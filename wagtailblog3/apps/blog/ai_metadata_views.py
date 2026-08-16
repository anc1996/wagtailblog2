"""BlogPage 元数据建议的 Wagtail 后台端点。"""

import json
import logging
import time

from django.http import JsonResponse
from django.views.decorators.http import require_POST
from wagtail.admin.auth import require_admin_access

from .ai_metadata import (
    MetadataConfigurationError,
    MetadataGenerationError,
    MetadataResponseError,
    extract_body_context,
)
from content_ai.services.blog_metadata import (
    PromptTemplateError,
    generate_blog_metadata as generate_template_metadata,
    list_active_blog_metadata_templates,
)


logger = logging.getLogger(__name__)


def _error_response(code: str, message: str, status: int) -> JsonResponse:
    return JsonResponse({"error": {"code": code, "message": message}}, status=status)


def _generate_blog_metadata(request):
    """返回建议值；调用方仍需通过现有 Wagtail 表单显式保存。"""
    try:
        payload = json.loads(request.body)
    except (TypeError, ValueError, json.JSONDecodeError):
        return _error_response("invalid_json", "请求格式无效。", 400)
    if not isinstance(payload, dict):
        return _error_response("invalid_payload", "请求内容必须是对象。", 400)

    body = payload.get("body")
    language = str(payload.get("language") or "zh-hans")[:32]
    template_id = payload.get("template_id")
    started_at = time.monotonic()
    try:
        context = extract_body_context(body)
        suggestion = generate_template_metadata(body, language=language, template_id=template_id)
    except PromptTemplateError as error:
        return _error_response("invalid_prompt_template", str(error), 400)
    except MetadataConfigurationError as error:
        return _error_response("service_unconfigured", str(error), 503)
    except MetadataResponseError as error:
        return _error_response("invalid_model_response", str(error), 502)
    except MetadataGenerationError as error:
        return _error_response("generation_failed", str(error), 400)

    logger.info(
        "blog_ai_metadata_generated chars=%s elapsed_ms=%s",
        len(context),
        round((time.monotonic() - started_at) * 1000, 1),
    )
    return JsonResponse({"suggestion": suggestion.as_dict()})


@require_admin_access
@require_POST
def generate_blog_metadata(request):
    """管理员入口；建议值仍须通过既有 Wagtail 表单显式保存。"""
    return _generate_blog_metadata(request)


@require_admin_access
def list_blog_metadata_templates(request):
    if request.method != "GET":
        return _error_response("method_not_allowed", "只支持读取启用的提示词列表。", 405)
    return JsonResponse(
        {"templates": [template.as_dict() for template in list_active_blog_metadata_templates()]}
    )
