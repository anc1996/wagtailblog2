import hashlib
import json
import re
import uuid
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.core.files.storage import storages
from django.db import transaction
from django.http import QueryDict
from django.utils import timezone
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication

from blog.models import (
    BlogIndexPage,
    BlogPage,
    MarkdownImportArtifact,
    MarkdownImportArtifactStatus,
    MarkdownImportBatchStatus,
    MarkdownImportSession,
    MarkdownImportSessionStatus,
)
from wagtailblog3.mongo import MongoManager
from blog.services.markdown_import_idempotency import (
    IdempotencyConflictError,
    IdempotencyKeyError,
    build_request_fingerprint,
    claim_import_batch,
)
from blog.services.markdown_import_media import (
    MediaImportError,
    cleanup_artifact_object,
    import_media_artifacts,
    import_media_artifact,
    validate_media_upload,
    probe_media_content,
)
from blog.services.markdown_import_service import (
    assemble_import_body,
    compensate_draft_failure,
    create_unpublished_blog_draft,
)
from blog.services.markdown_import_parser import attach_table_inline_images
from blog.services.markdown_import_types import MarkdownImportBlock
from blog.services.markdown_import_sessions import (
    session_expiry_delta,
    session_expired,
    session_payload,
)
from blog.services.markdown_import_auth import MarkdownImportTokenAuthentication
from blog.ai_metadata import (
    MetadataConfigurationError,
    MetadataGenerationError,
    MetadataResponseError,
)
from content_ai.services.blog_metadata import (
    PromptTemplateError,
    generate_blog_metadata as generate_template_metadata,
    list_active_blog_metadata_templates,
)


class MarkdownImportRequestError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _error(code: str, status: int) -> Response:
    return Response({"status": "failed", "code": code}, status=status)


def _payload(request) -> dict[str, Any]:
    data = request.data
    if isinstance(data, QueryDict):
        raw_manifest = data.get("manifest")
        if raw_manifest:
            try:
                data = json.loads(raw_manifest)
            except (TypeError, ValueError) as exc:
                raise MarkdownImportRequestError("manifest_invalid") from exc
    if not isinstance(data, dict):
        raise MarkdownImportRequestError("manifest_invalid")
    return data


def _target_parent(payload, user):
    try:
        parent_id = int(payload.get("target_parent_id"))
    except (TypeError, ValueError) as exc:
        raise MarkdownImportRequestError("target_parent_invalid") from exc
    if not user.has_perm("blog.add_blogpage"):
        raise MarkdownImportRequestError("import_permission_denied")
    parent = BlogIndexPage.objects.filter(pk=parent_id).first()
    if parent is None:
        raise MarkdownImportRequestError("target_parent_not_found")
    permission = parent.permissions_for_user(user)
    if not permission.can_add_subpage():
        raise MarkdownImportRequestError("target_parent_forbidden")
    return parent


def _blocks(payload) -> tuple[MarkdownImportBlock, ...]:
    raw_blocks = payload.get("blocks")
    if not isinstance(raw_blocks, list):
        raise MarkdownImportRequestError("blocks_invalid")
    blocks: list[MarkdownImportBlock] = []
    for item in raw_blocks:
        if not isinstance(item, dict):
            raise MarkdownImportRequestError("block_invalid")
        try:
            block_type = str(item["block_type"])
            if block_type not in {
                "markdown_block",
                "image_block",
                "audio_block",
                "video_block",
                "embed_block",
                "mermaid_chart",
            }:
                raise MarkdownImportRequestError("block_type_invalid")
            blocks.append(
                MarkdownImportBlock(
                    block_type=block_type,
                    value=item.get("value", ""),
                    source_start_line=int(item.get("source_start_line", 0)),
                    source_end_line=int(item.get("source_end_line", 0)),
                )
            )
            if block_type in {"image_block", "audio_block", "video_block"}:
                value = blocks[-1].value
                if not isinstance(value, dict) or not str(value.get("source") or ""):
                    raise MarkdownImportRequestError("media_source_invalid")
        except (KeyError, TypeError, ValueError) as exc:
            raise MarkdownImportRequestError("block_invalid") from exc
    return attach_table_inline_images(blocks)


def _date_value(payload):
    raw_value = str(payload.get("date") or date.today().isoformat())
    try:
        return date.fromisoformat(raw_value)
    except ValueError as exc:
        raise MarkdownImportRequestError("date_invalid") from exc


def _tags(payload) -> tuple[str, ...]:
    raw_tags = payload.get("tags", [])
    if raw_tags is None:
        return ()
    if not isinstance(raw_tags, list) or not all(isinstance(tag, str) for tag in raw_tags):
        raise MarkdownImportRequestError("tags_invalid")
    tags = tuple(tag.strip() for tag in raw_tags if tag.strip())
    if len(tags) > 50 or any(len(tag) > 50 for tag in tags):
        raise MarkdownImportRequestError("tags_invalid")
    return tags


def _upload_digest(upload):
    digest = hashlib.sha256()
    size = 0
    upload.seek(0)
    for chunk in iter(lambda: upload.read(1024 * 1024), b""):
        size += len(chunk)
        digest.update(chunk)
    upload.seek(0)
    return size, digest.hexdigest()


def _intro(payload) -> str:
    raw_intro = payload.get("intro")
    if not isinstance(raw_intro, str):
        raise MarkdownImportRequestError("intro_invalid")
    intro = raw_intro.strip()
    if not intro:
        raise MarkdownImportRequestError("intro_required")
    if len(intro) > 5000:
        raise MarkdownImportRequestError("intro_invalid")
    return intro


def _artifact_manifest(payload, blocks):
    raw = payload.get("artifacts", [])
    if not isinstance(raw, list):
        raise MarkdownImportRequestError("artifacts_invalid")
    source_types = {}
    source_scopes = {}
    source_occurrences = {}
    for block in blocks:
        references = []
        if block.block_type in {"image_block", "audio_block", "video_block"}:
            if isinstance(block.value, dict):
                references.append(
                    (
                        str(block.value.get("source") or ""),
                        block.block_type.removesuffix("_block"),
                        "block_media",
                        "",
                    )
                )
        references.extend(
            (image.source, "image", "inline_image", image.occurrence_id)
            for image in block.inline_images
        )
        for source, media_type, scope, occurrence_id in references:
            previous = source_types.get(source)
            if previous is not None and previous != media_type:
                raise MarkdownImportRequestError("artifact_media_type_conflict")
            source_types[source] = media_type
            source_scopes.setdefault(source, set()).add(scope)
            if occurrence_id:
                source_occurrences.setdefault(source, set()).add(occurrence_id)
    manifests = []
    seen_normalized = set()
    covered_sources = set()
    for position, item in enumerate(raw):
        if not isinstance(item, dict):
            raise MarkdownImportRequestError("artifact_invalid")
        source = str(item.get("normalized_source") or "")
        if not source:
            raise MarkdownImportRequestError("artifact_source_invalid")
        try:
            artifact_id = uuid.UUID(str(item.get("artifact_id")))
        except (AttributeError, TypeError, ValueError) as exc:
            raise MarkdownImportRequestError("artifact_id_invalid") from exc
        if artifact_id.version != 4:
            raise MarkdownImportRequestError("artifact_id_invalid")
        media_type = str(item.get("media_type") or "")
        if media_type not in {"image", "audio", "video"}:
            raise MarkdownImportRequestError("media_type_invalid")
        source_kind = str(item.get("source_kind") or "local")
        if source_kind not in {"local", "remote_https"}:
            raise MarkdownImportRequestError("source_kind_invalid")
        scheme = urlsplit(source).scheme.casefold()
        if scheme and scheme != "https":
            raise MarkdownImportRequestError("source_scheme_invalid")
        if source_kind != ("remote_https" if scheme == "https" else "local"):
            raise MarkdownImportRequestError("source_kind_mismatch")
        normalized_key = (source_kind, source)
        if normalized_key in seen_normalized:
            raise MarkdownImportRequestError("artifact_source_invalid")
        seen_normalized.add(normalized_key)

        raw_references = item.get("reference_sources", [source])
        if (
            not isinstance(raw_references, list)
            or not raw_references
            or not all(isinstance(reference, str) and reference for reference in raw_references)
            or len(set(raw_references)) != len(raw_references)
        ):
            raise MarkdownImportRequestError("artifact_references_invalid")
        reference_sources = tuple(raw_references)
        if any(reference not in source_types for reference in reference_sources):
            raise MarkdownImportRequestError("artifact_source_unreferenced")
        if any(source_types[reference] != media_type for reference in reference_sources):
            raise MarkdownImportRequestError("artifact_media_type_mismatch")
        if covered_sources.intersection(reference_sources):
            raise MarkdownImportRequestError("artifact_source_duplicate")
        covered_sources.update(reference_sources)

        expected_scopes = set().union(
            *(source_scopes[reference] for reference in reference_sources)
        )
        expected_scope = "mixed" if len(expected_scopes) > 1 else next(iter(expected_scopes))
        reference_scope = str(item.get("reference_scope") or expected_scope)
        if reference_scope != expected_scope:
            raise MarkdownImportRequestError("artifact_scope_mismatch")
        raw_occurrences = item.get("occurrence_ids", [])
        if (
            not isinstance(raw_occurrences, list)
            or not all(isinstance(value, str) and value for value in raw_occurrences)
            or len(set(raw_occurrences)) != len(raw_occurrences)
        ):
            raise MarkdownImportRequestError("artifact_occurrences_invalid")
        expected_occurrences = set().union(
            *(source_occurrences.get(reference, set()) for reference in reference_sources)
        )
        if set(raw_occurrences) != expected_occurrences:
            raise MarkdownImportRequestError("artifact_occurrences_mismatch")
        safe_filename = str(item.get("safe_filename") or "media.bin")
        if (
            len(safe_filename) > 255
            or any(ord(char) < 32 for char in safe_filename)
            or "/" in safe_filename
            or "\\" in safe_filename
        ):
            raise MarkdownImportRequestError("safe_filename_invalid")
        upload_field = str(item.get("upload_field") or f"artifact_{artifact_id}")
        if len(upload_field) > 128 or not upload_field.startswith("artifact_"):
            raise MarkdownImportRequestError("upload_field_invalid")
        expected_sha256 = str(item.get("sha256") or "").casefold()
        if len(expected_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in expected_sha256
        ):
            raise MarkdownImportRequestError("artifact_hash_invalid")
        try:
            expected_size = int(item["size_bytes"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MarkdownImportRequestError("artifact_size_invalid") from exc
        if expected_size < 0:
            raise MarkdownImportRequestError("artifact_size_invalid")
        try:
            artifact_position = int(item.get("position", position))
        except (TypeError, ValueError) as exc:
            raise MarkdownImportRequestError("artifact_position_invalid") from exc
        if artifact_position < 0:
            raise MarkdownImportRequestError("artifact_position_invalid")
        manifests.append(
            {
                "artifact_id": artifact_id,
                "position": artifact_position,
                "media_type": media_type,
                "source_kind": source_kind,
                "normalized_source": source,
                "reference_sources": reference_sources,
                "reference_scope": reference_scope,
                "occurrence_ids": tuple(raw_occurrences),
                "safe_filename": safe_filename,
                "upload_field": upload_field,
                "size_bytes": expected_size,
                "sha256": expected_sha256,
                "preflight_error_code": (
                    "client_download_failed"
                    if item.get("preflight_error_code") == "client_download_failed"
                    else ""
                ),
            }
        )
    if set(source_types) - covered_sources:
        raise MarkdownImportRequestError("artifact_missing")
    return manifests


def _fingerprint_payload(payload, manifests):
    """幂等指纹只包含业务内容，忽略每次传输都会变化的 artifact UUID 和字段名。"""

    return {
        "target_parent_id": payload.get("target_parent_id"),
        "title": str(payload.get("title") or ""),
        "date": str(payload.get("date") or ""),
        "intro": str(payload.get("intro") or ""),
        "tags": list(payload.get("tags") or []),
        "blocks": payload.get("blocks", []),
        "artifacts": [
            {
                key: list(value) if isinstance(value, tuple) else value
                for key, value in item.items()
                if key not in {"artifact_id", "upload_field"}
            }
            for item in manifests
        ],
    }


def _batch_response(batch):
    return {
        "status": getattr(batch, "status", MarkdownImportBatchStatus.PROCESSING),
        "batch_id": str(batch.batch_id),
        "page_id": getattr(batch, "result_page_id", None),
        "revision_id": getattr(batch, "result_revision_id", None),
    }


def _session_manifest(payload, manifests):
    """把 UUID/tuple 转为 JSON 标量，确保会话可在 worker 重启后恢复。"""

    serialized = dict(payload)
    serialized["artifacts"] = [
        {
            key: (
                str(value)
                if isinstance(value, uuid.UUID)
                else list(value)
                if isinstance(value, tuple)
                else value
            )
            for key, value in item.items()
        }
        for item in manifests
    ]
    return serialized


def _cleanup_import_artifact(artifact):
    alias = artifact.storage_alias
    storage_registry = {}
    if alias:
        try:
            storage_registry = {alias: storages[alias]}
        except Exception:
            # 让 cleanup 服务把未知 alias 记为 retry，而不是丢失补偿证据。
            storage_registry = {}

    def reference_guard(current):
        # 导入对象名含 artifact UUID，页面删除和 Mongo 草稿删除后只需阻止其他审计行复用同一模型对象。
        return MarkdownImportArtifact.objects.filter(
            media_model=current.media_model,
            media_object_id=current.media_object_id,
        ).exclude(pk=current.pk).exists()

    return cleanup_artifact_object(
        artifact,
        storages=storage_registry,
        reference_guard=reference_guard,
    )


class MarkdownImportBaseView(APIView):
    authentication_classes = (
        MarkdownImportTokenAuthentication,
        JWTAuthentication,
        SessionAuthentication,
    )
    permission_classes = (IsAuthenticated,)

    def dispatch(self, request, *args, **kwargs):
        try:
            return super().dispatch(request, *args, **kwargs)
        except MarkdownImportRequestError as exc:
            status = 403 if exc.code.endswith("denied") or exc.code.endswith("forbidden") else 400
            # 自定义异常发生在 APIView.dispatch 外层时，必须补做 DRF 响应协商，否则 Django 渲染会报 accepted_renderer 缺失并变成 500。
            response = _error(exc.code, status)
            return self.finalize_response(request, response, *args, **kwargs)


class MarkdownImportLimitsView(MarkdownImportBaseView):
    def get(self, request):
        return Response(
            {
                "max_image_size": getattr(settings, "WAGTAILIMAGES_MAX_UPLOAD_SIZE", None),
                "media_deep_probe": True,
                "allowed_remote_image_scheme": "https",
                "storage_alias": getattr(settings, "MARKDOWN_IMPORT_STORAGE_ALIAS", "default"),
                "session_protocol": True,
                "session_max_artifacts": getattr(settings, "MARKDOWN_IMPORT_SESSION_MAX_ARTIFACTS", 10000),
                "session_max_bytes": getattr(settings, "MARKDOWN_IMPORT_SESSION_MAX_BYTES", 20 * 1024**3),
                "session_upload_max_bytes": getattr(settings, "MARKDOWN_IMPORT_SESSION_UPLOAD_MAX_BYTES", 512 * 1024**2),
            }
        )


class MarkdownImportDestinationsView(MarkdownImportBaseView):
    def get(self, request):
        if not request.user.has_perm("blog.add_blogpage"):
            return _error("import_permission_denied", 403)
        destinations = []
        for parent in BlogIndexPage.objects.all():
            try:
                allowed = parent.permissions_for_user(request.user).can_add_subpage()
            except Exception:
                allowed = False
            if allowed:
                destinations.append({"id": parent.pk, "title": parent.title})
        return Response({"destinations": destinations})


class MarkdownImportDuplicateTitlesView(MarkdownImportBaseView):
    """只读检查目标索引页下的同标题页面，不阻断新草稿创建。"""

    def post(self, request):
        payload = _payload(request)
        parent = _target_parent(payload, request.user)
        titles = payload.get("titles", [])
        if not isinstance(titles, list) or not all(isinstance(title, str) for title in titles):
            raise MarkdownImportRequestError("titles_invalid")
        normalized_titles = {title.strip() for title in titles if title.strip()}
        matches = (
            BlogPage.objects.filter(path__startswith=parent.path, title__in=normalized_titles)
            .exclude(depth__lte=parent.depth)
            .values("pk", "title", "slug", "live", "has_unpublished_changes")
            .order_by("title", "pk")
        )
        return Response(
            {
                "status": "ok",
                "duplicates": [
                    {
                        "page_id": item["pk"],
                        "title": item["title"],
                        "slug": item["slug"],
                        "live": item["live"],
                        "has_unpublished_changes": item["has_unpublished_changes"],
                    }
                    for item in matches
                ],
            }
        )


class MarkdownImportMetadataTemplatesView(MarkdownImportBaseView):
    """返回当前可选模板；目标索引页权限仍是导入客户端的访问边界。"""

    def get(self, request):
        _target_parent(request.query_params, request.user)
        return Response(
            {"templates": [template.as_dict() for template in list_active_blog_metadata_templates()]}
        )


class MarkdownImportMetadataSuggestionView(MarkdownImportBaseView):
    """按显式模板生成建议，不保存页面、批次或正文。"""

    def post(self, request):
        payload = _payload(request)
        _target_parent(payload, request.user)
        context = payload.get("context")
        if not isinstance(context, str) or not context.strip():
            raise MarkdownImportRequestError("ai_context_invalid")
        max_chars = int(getattr(settings, "AI_METADATA_MAX_CONTEXT_CHARS", 24000))
        if len(context) > max_chars:
            raise MarkdownImportRequestError("ai_context_too_long")
        forbidden_reference = re.search(
            r"(?i)(?:https?://|file://|[A-Za-z]:[\\/]|\\\\|"
            r"(?:^|\s)/(?:mnt|home|root|tmp|var|Users)/|"
            r"(?:\.\.?[\\/])?(?:[^\s<>\"']+[\\/])+[^\s<>\"']+\."
            r"(?:png|jpe?g|gif|webp|bmp|mp3|m4a|wav|ogg|mp4|webm|mov|pdf|md)\b)",
            context,
        )
        if forbidden_reference:
            raise MarkdownImportRequestError("ai_context_contains_forbidden_reference")
        language = str(payload.get("language") or "zh-hans")[:32]
        body = [{"type": "markdown_block", "value": context.strip()}]
        try:
            suggestion = generate_template_metadata(
                body,
                language=language,
                template_id=payload.get("template_id"),
            )
        except PromptTemplateError:
            return _error("ai_template_invalid", 400)
        except MetadataConfigurationError:
            return _error("ai_service_unconfigured", 503)
        except MetadataResponseError:
            return _error("ai_response_invalid", 502)
        except MetadataGenerationError:
            return _error("ai_generation_failed", 502)
        return Response(
            {
                "status": "ok",
                "suggestion": {
                    "intro": suggestion.intro,
                    "tags": suggestion.tags,
                },
            }
        )


class MarkdownImportPreviewView(MarkdownImportBaseView):
    def post(self, request):
        payload = _payload(request)
        _target_parent(payload, request.user)
        blocks = _blocks(payload)
        inline_images = [image for block in blocks for image in block.inline_images]
        block_media_count = sum(
            block.block_type in {"image_block", "audio_block", "video_block"}
            for block in blocks
        )
        return Response(
            {
                "status": "preview",
                "block_count": len(blocks),
                "media_count": block_media_count + len(inline_images),
                "block_media_count": block_media_count,
                "inline_image_count": len(inline_images),
                "inline_images": [
                    {
                        "occurrence_id": image.occurrence_id,
                        "source_kind": image.source_kind,
                        "table_index": image.table_index,
                        "row_index": image.row_index,
                        "cell_index": image.cell_index,
                    }
                    for image in inline_images
                ],
                "blocks": [block.block_type for block in blocks],
            }
        )


class MarkdownImportSessionCreateView(MarkdownImportBaseView):
    """创建大批量会话；正文清单只入库一次，媒体随后逐个上传。"""

    def post(self, request):
        payload = _payload(request)
        parent = _target_parent(payload, request.user)
        blocks = _blocks(payload)
        tags = _tags(payload)
        intro = _intro(payload)
        manifests = _artifact_manifest(payload, blocks)
        max_artifacts = int(getattr(settings, "MARKDOWN_IMPORT_SESSION_MAX_ARTIFACTS", 10000))
        max_bytes = int(getattr(settings, "MARKDOWN_IMPORT_SESSION_MAX_BYTES", 20 * 1024**3))
        total_bytes = sum(int(item["size_bytes"]) for item in manifests)
        if len(manifests) > max_artifacts:
            raise MarkdownImportRequestError("session_artifact_limit_exceeded")
        if total_bytes > max_bytes:
            raise MarkdownImportRequestError("session_size_limit_exceeded")
        key = payload.get("idempotency_key")
        if not key:
            raise MarkdownImportRequestError("idempotency_key_required")
        normalized_payload = {
            **payload,
            "target_parent_id": parent.pk,
            "intro": intro,
            "tags": list(tags),
        }
        try:
            fingerprint = build_request_fingerprint(_fingerprint_payload(normalized_payload, manifests))
            claim = claim_import_batch(
                user_id=request.user.pk,
                idempotency_key=key,
                request_fingerprint=fingerprint,
                target_parent_id=parent.pk,
            )
        except (IdempotencyKeyError, IdempotencyConflictError) as exc:
            return _error(str(exc), 409 if isinstance(exc, IdempotencyConflictError) else 400)
        if not claim.created:
            session = MarkdownImportSession.objects.filter(batch=claim.batch).first()
            if session is None:
                return _error("idempotency_conflict", 409)
            return Response(session_payload(session), status=200)

        manifest = _session_manifest(normalized_payload, manifests)
        try:
            with transaction.atomic():
                batch = claim.batch
                batch.status = MarkdownImportBatchStatus.PENDING
                batch.save(update_fields=["status", "updated_at"])
                session = MarkdownImportSession.objects.create(
                    batch=batch,
                    manifest=manifest,
                    total_artifacts=len(manifests),
                    total_bytes=total_bytes,
                    expires_at=timezone.now() + session_expiry_delta(),
                    completed_artifacts=sum(bool(item["preflight_error_code"]) for item in manifests),
                )
                for item in manifests:
                    status = (
                        MarkdownImportArtifactStatus.FAILED_MISSING
                        if item["preflight_error_code"]
                        else MarkdownImportArtifactStatus.PENDING
                    )
                    MarkdownImportArtifact.objects.create(
                        artifact_id=item["artifact_id"],
                        batch=batch,
                        session=session,
                        position=item["position"],
                        media_type=item["media_type"],
                        source_kind=item["source_kind"],
                        normalized_source=item["normalized_source"],
                        normalized_source_hash=hashlib.sha256(
                            item["normalized_source"].encode("utf-8")
                        ).hexdigest(),
                        safe_filename=item["safe_filename"],
                        status=status,
                        error_code=item["preflight_error_code"],
                    )
        except Exception:
            # 仅删除本次刚认领且尚未产生任何页面或媒体的空批次，避免幂等键永久卡死。
            claim.batch.delete()
            raise
        return Response(session_payload(session), status=201)


def _owned_session(session_id, user):
    try:
        session_uuid = uuid.UUID(str(session_id))
    except (TypeError, ValueError) as exc:
        raise MarkdownImportRequestError("session_id_invalid") from exc
    session = (
        MarkdownImportSession.objects.select_related("batch", "batch__user")
        .filter(session_id=session_uuid, batch__user=user)
        .first()
    )
    if session is None:
        raise MarkdownImportRequestError("session_not_found")
    return session


class MarkdownImportSessionDetailView(MarkdownImportBaseView):
    def get(self, request, session_id):
        return Response(session_payload(_owned_session(session_id, request.user)))


class MarkdownImportSessionArtifactUploadView(MarkdownImportBaseView):
    """每个请求最多接收一个文件，避免 Django multipart 文件计数成为瓶颈。"""

    def post(self, request, session_id, artifact_id):
        session = _owned_session(session_id, request.user)
        if session.status in {
            MarkdownImportSessionStatus.EXPIRED,
            MarkdownImportSessionStatus.SUCCESS,
            MarkdownImportSessionStatus.PARTIAL_SUCCESS,
            MarkdownImportSessionStatus.FAILED,
        }:
            raise MarkdownImportRequestError("session_not_uploadable")
        if session_expired(session):
            from blog.services.markdown_import_sessions import _mark_expired

            _mark_expired(session)
            raise MarkdownImportRequestError("session_expired")
        try:
            artifact_uuid = uuid.UUID(str(artifact_id))
        except (TypeError, ValueError) as exc:
            raise MarkdownImportRequestError("artifact_id_invalid") from exc
        if len(request.FILES) != 1:
            raise MarkdownImportRequestError("session_single_upload_required")
        upload = next(iter(request.FILES.values()), None)
        if upload is None:
            raise MarkdownImportRequestError("upload_missing")
        max_upload = int(getattr(settings, "MARKDOWN_IMPORT_SESSION_UPLOAD_MAX_BYTES", 512 * 1024**2))
        size_bytes, sha256 = _upload_digest(upload)
        item = next(
            item
            for item in session.manifest.get("artifacts", [])
            if str(item.get("artifact_id")) == str(artifact_uuid)
        )
        if size_bytes > max_upload or size_bytes != int(item["size_bytes"]):
            raise MarkdownImportRequestError("upload_size_mismatch")
        if sha256 != str(item["sha256"]).casefold():
            raise MarkdownImportRequestError("upload_hash_mismatch")
        with transaction.atomic():
            artifact = session.artifacts.select_for_update().filter(artifact_id=artifact_uuid).first()
            if artifact is None:
                raise MarkdownImportRequestError("artifact_not_found")
            if artifact.status == MarkdownImportArtifactStatus.SUCCEEDED:
                return Response(session_payload(session), status=200)
            if artifact.status == MarkdownImportArtifactStatus.PROCESSING:
                return _error("artifact_processing", 409)
            artifact.status = MarkdownImportArtifactStatus.PROCESSING
            artifact.save(update_fields=["status", "updated_at"])
        try:
            form = validate_media_upload(
                artifact.media_type,
                upload,
                form_data={"title": artifact.safe_filename, "collection": session.manifest.get("collection_id", "")},
                user=request.user,
                content_probe=probe_media_content,
            )
            result = import_media_artifact(
                artifact,
                upload,
                validated_form=form,
                storage=storages[getattr(settings, "MARKDOWN_IMPORT_STORAGE_ALIAS", "default")],
                storage_alias=getattr(settings, "MARKDOWN_IMPORT_STORAGE_ALIAS", "default"),
                storage_registry=storages,
            )
        except MediaImportError as exc:
            artifact.status = MarkdownImportArtifactStatus.FAILED_MISSING
            artifact.error_code = exc.code
            artifact.save(update_fields=["status", "error_code", "updated_at"])
        except Exception:
            # 单媒体校验或存储异常不能中止该会话的其他媒体。
            artifact.status = MarkdownImportArtifactStatus.FAILED_MISSING
            artifact.error_code = "media_import_failed"
            artifact.save(update_fields=["status", "error_code", "updated_at"])
        else:
            if result.block_type == "markdown_block":
                artifact.status = MarkdownImportArtifactStatus.FAILED_MISSING
                artifact.error_code = "media_import_failed"
                artifact.save(update_fields=["status", "error_code", "updated_at"])
        artifact.uploaded_at = timezone.now()
        artifact.save(update_fields=["uploaded_at", "updated_at"])
        session.status = MarkdownImportSessionStatus.UPLOADING
        session.completed_artifacts = session.artifacts.filter(
            status__in=[MarkdownImportArtifactStatus.SUCCEEDED, MarkdownImportArtifactStatus.FAILED_MISSING]
        ).count()
        session.save(update_fields=["status", "completed_artifacts", "updated_at"])
        return Response(session_payload(session), status=200)


class MarkdownImportSessionFinalizeView(MarkdownImportBaseView):
    def post(self, request, session_id):
        session = _owned_session(session_id, request.user)
        if session_expired(session):
            from blog.services.markdown_import_sessions import _mark_expired

            _mark_expired(session)
            return Response(session_payload(session), status=410)
        completed = session.artifacts.filter(
            status__in=[MarkdownImportArtifactStatus.SUCCEEDED, MarkdownImportArtifactStatus.FAILED_MISSING]
        ).count()
        session.completed_artifacts = completed
        if completed != session.total_artifacts:
            session.save(update_fields=["completed_artifacts", "updated_at"])
            return _error("session_artifacts_incomplete", 409)
        session.status = MarkdownImportSessionStatus.READY
        session.assembly_requested_at = timezone.now()
        session.save(update_fields=["status", "assembly_requested_at", "completed_artifacts", "updated_at"])
        from blog.tasks import assemble_markdown_import_session

        assemble_markdown_import_session.delay(str(session.session_id))
        return Response(session_payload(session), status=202)


class MarkdownImportView(MarkdownImportBaseView):
    def post(self, request):
        payload = _payload(request)
        parent = _target_parent(payload, request.user)
        blocks = _blocks(payload)
        tags = _tags(payload)
        intro = _intro(payload)
        manifests = _artifact_manifest(payload, blocks)
        key = payload.get("idempotency_key")
        if not key:
            raise MarkdownImportRequestError("idempotency_key_required")
        fingerprint_payload = _fingerprint_payload(
            {
                **payload,
                "target_parent_id": parent.pk,
                "intro": intro,
                "tags": list(tags),
            },
            manifests,
        )
        try:
            fingerprint = build_request_fingerprint(fingerprint_payload)
            claim = claim_import_batch(
                user_id=request.user.pk,
                idempotency_key=key,
                request_fingerprint=fingerprint,
                target_parent_id=parent.pk,
            )
        except (IdempotencyKeyError, IdempotencyConflictError) as exc:
            return _error(str(exc), 409 if isinstance(exc, IdempotencyConflictError) else 400)
        batch = claim.batch
        if not claim.created:
            return Response(_batch_response(batch), status=200)

        media_results = {}
        artifacts = []
        draft = None
        try:
            batch.status = MarkdownImportBatchStatus.PROCESSING
            batch.save(update_fields=["status", "updated_at"])
            with transaction.atomic():
                for item in manifests:
                    artifact = MarkdownImportArtifact.objects.create(
                        artifact_id=item["artifact_id"],
                        batch=batch,
                        position=item["position"],
                        media_type=item["media_type"],
                        source_kind=item["source_kind"],
                        normalized_source=item["normalized_source"],
                        normalized_source_hash=hashlib.sha256(
							item["normalized_source"].encode("utf-8")
						).hexdigest(),
                        safe_filename=item["safe_filename"],
                        error_code=item["preflight_error_code"],
                    )
                    artifacts.append(artifact)

            def importer(artifact):
                item = next(item for item in manifests if item["artifact_id"] == artifact.artifact_id)
                upload = request.FILES.get(item["upload_field"])
                if upload is None:
                    raise MediaImportError(artifact.error_code or "upload_missing")
                size_bytes, sha256 = _upload_digest(upload)
                if size_bytes != item["size_bytes"]:
                    raise MediaImportError("upload_size_mismatch")
                if sha256 != item["sha256"]:
                    raise MediaImportError("upload_hash_mismatch")
                form = validate_media_upload(
                    artifact.media_type,
                    upload,
                    form_data={"title": artifact.safe_filename, "collection": payload.get("collection_id", "")},
                    user=request.user,
                    content_probe=probe_media_content,
                )
                return import_media_artifact(
                    artifact,
                    upload,
                    validated_form=form,
                    storage=storages[getattr(settings, "MARKDOWN_IMPORT_STORAGE_ALIAS", "default")],
                    storage_alias=getattr(settings, "MARKDOWN_IMPORT_STORAGE_ALIAS", "default"),
                    storage_registry=storages,
                )

            results = import_media_artifacts(artifacts, importer=importer)
            media_results = {}
            for artifact, result in zip(artifacts, results):
                item = next(
                    item
                    for item in manifests
                    if item["artifact_id"] == artifact.artifact_id
                )
                for reference_source in item["reference_sources"]:
                    media_results[reference_source] = result
            body_values = assemble_import_body(blocks, media_results=media_results)
            draft = create_unpublished_blog_draft(
                parent,
                title=str(payload.get("title") or "未命名导入"),
                date=_date_value(payload),
                intro=intro,
                body_values=body_values,
                tags=tags,
                user=request.user,
            )
            batch.status = (
                MarkdownImportBatchStatus.PARTIAL_SUCCESS
                if any(result.block_type == "markdown_block" and artifact.status == MarkdownImportArtifactStatus.FAILED_MISSING for artifact, result in zip(artifacts, results))
                else MarkdownImportBatchStatus.SUCCESS
            )
            batch.result_page_id = draft.page.pk
            batch.result_revision_id = draft.revision.id
            batch.mongo_content_id = draft.mongo_draft_pointer
            batch.completed_at = timezone.now()
            batch.save(update_fields=["status", "result_page", "result_revision_id", "mongo_content_id", "completed_at", "updated_at"])
            return Response(
                {
                    "status": batch.status,
                    "batch_id": str(batch.batch_id),
                    "page_id": draft.page.pk,
                    "revision_id": draft.revision.id,
                    "missing": [artifact.normalized_source for artifact in artifacts if artifact.status == MarkdownImportArtifactStatus.FAILED_MISSING],
                    "missing_details": [
                        {
                            "source": artifact.normalized_source,
                            "error_code": artifact.error_code,
                            "reference_scope": item["reference_scope"],
                            "reference_sources": list(item["reference_sources"]),
                            "occurrence_ids": list(item["occurrence_ids"]),
                        }
                        for artifact, item in (
                            (
                                artifact,
                                next(
                                    manifest
                                    for manifest in manifests
                                    if manifest["artifact_id"] == artifact.artifact_id
                                ),
                            )
                            for artifact in artifacts
                            if artifact.status == MarkdownImportArtifactStatus.FAILED_MISSING
                        )
                    ],
                },
                status=201,
            )
        except Exception as exc:
            error_code = str(getattr(exc, "code", "import_failed"))
            if not error_code or not error_code.replace("_", "").isalnum() or len(error_code) > 64:
                error_code = "import_failed"
            media_artifacts = [
                artifact
                for artifact in artifacts
                if artifact.storage_alias and artifact.object_name
            ]
            mongo_pointer = draft.mongo_draft_pointer if draft is not None else ""
            compensation = compensate_draft_failure(
                page=draft.page if draft is not None else None,
                mongo_draft_pointer=mongo_pointer,
                media_artifacts=media_artifacts,
                delete_page=lambda page: page.delete(),
                delete_mongo_pointer=lambda pointer: MongoManager().delete_single_revision(pointer),
                cleanup_media=_cleanup_import_artifact,
            )
            batch.status = (
                MarkdownImportBatchStatus.CLEANUP_RETRY
                if not compensation.cleaned
                else MarkdownImportBatchStatus.FAILED
            )
            batch.error_code = error_code
            batch.error_message = ";".join(compensation.errors)[:2000]
            batch.completed_at = timezone.now()
            batch.save(update_fields=["status", "error_code", "error_message", "completed_at", "updated_at"])
            return _error(batch.error_code, 422)
