"""Wagtail admin endpoint used by Vditor clipboard image uploads."""

import logging
import secrets
import time
from pathlib import Path

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from wagtail.admin.auth import require_admin_access
from wagtail.images import get_image_model
from wagtail.images.formats import get_image_format
from wagtail.images.forms import get_image_form
from wagtail.images.permissions import permission_policy


logger = logging.getLogger(__name__)


def _error_response(code, message, status, *, details=None):
    payload = {"error": {"code": code, "message": message}}
    if details:
        payload["error"]["details"] = details
    return JsonResponse(payload, status=status)


def _resolve_upload_collection(request):
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


def _random_title_suffix():
    return f"{secrets.randbelow(1_000_000_000):09d}"


def _safe_upload_title(uploaded_file, image_model):
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


def _upload_vditor_image(request):
    """Create a Wagtail image from a Vditor clipboard upload."""

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

    preview = None
    try:
        rendition = image.get_rendition(image_format.filter_spec)
        preview = {
            "url": rendition.url,
            "width": rendition.width,
            "height": rendition.height,
        }
    except Exception:
        # The original image is already valid and stored. A missing preview must not
        # cause a retry that creates a duplicate image.
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
def upload_vditor_image(request):
    return _upload_vditor_image(request)
