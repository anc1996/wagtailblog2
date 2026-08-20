"""大批量 Markdown 导入会话的状态推进与最终组装。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta

from django.apps import apps
from django.conf import settings
from django.core.files.storage import storages
from django.db import transaction
from django.utils import timezone

from blog.models import (
    MarkdownImportArtifact,
    MarkdownImportArtifactCleanupStatus,
    MarkdownImportArtifactStatus,
    MarkdownImportBatchStatus,
    MarkdownImportSession,
    MarkdownImportSessionStatus,
)
from blog.services.markdown_import_media import (
    MediaImportResult,
    cleanup_artifact_object,
)
from blog.services.markdown_import_service import (
    assemble_import_body,
    compensate_draft_failure,
    create_unpublished_blog_draft,
)


TERMINAL_SESSION_STATUSES = {
    MarkdownImportSessionStatus.SUCCESS,
    MarkdownImportSessionStatus.PARTIAL_SUCCESS,
    MarkdownImportSessionStatus.FAILED,
    MarkdownImportSessionStatus.EXPIRED,
}


def session_payload(session: MarkdownImportSession) -> dict[str, object]:
    """返回客户端可轮询的稳定状态，不暴露正文或存储凭据。"""

    missing = list(
        session.artifacts.filter(status=MarkdownImportArtifactStatus.FAILED_MISSING)
        .order_by("position")
        .values("normalized_source", "error_code", "media_type")
    )
    details = []
    manifest_by_id = {
        str(item.get("artifact_id")): item
        for item in session.manifest.get("artifacts", [])
        if isinstance(item, Mapping)
    }
    for artifact in session.artifacts.filter(
        status=MarkdownImportArtifactStatus.FAILED_MISSING
    ).order_by("position"):
        item = manifest_by_id.get(str(artifact.artifact_id), {})
        details.append(
            {
                "source": artifact.normalized_source,
                "error_code": artifact.error_code,
                "reference_scope": item.get("reference_scope", ""),
                "reference_sources": item.get("reference_sources", []),
                "occurrence_ids": item.get("occurrence_ids", []),
            }
        )
    artifacts = list(
        session.artifacts.order_by("position").values(
            "artifact_id",
            "media_type",
            "source_kind",
            "normalized_source",
            "status",
        )
    )
    for artifact in artifacts:
        artifact["artifact_id"] = str(artifact["artifact_id"])
    batch = session.batch
    return {
        "status": session.status,
        "session_id": str(session.session_id),
        "batch_id": str(batch.batch_id),
        "page_id": batch.result_page_id,
        "revision_id": batch.result_revision_id,
        "total_artifacts": session.total_artifacts,
        "completed_artifacts": session.completed_artifacts,
        "total_bytes": session.total_bytes,
        "expires_at": session.expires_at.isoformat(),
        "assembly_requested_at": (
            session.assembly_requested_at.isoformat()
            if session.assembly_requested_at else None
        ),
        "batch_status": batch.status,
        "missing": missing,
        "missing_details": details,
        "artifacts": artifacts,
        "error_code": batch.error_code or "",
    }


def session_expired(session: MarkdownImportSession) -> bool:
    return session.expires_at <= timezone.now()


def _missing_result(artifact: MarkdownImportArtifact) -> MediaImportResult:
    reference = artifact.normalized_source or artifact.safe_filename or "未知文件"
    marker = (
        f"[导入缺失：{artifact.media_type} 原始引用：{reference} "
        f"原因：{artifact.error_code or 'media_missing'}]"
    )
    return MediaImportResult("markdown_block", marker)


def _media_result(artifact: MarkdownImportArtifact) -> MediaImportResult:
    if artifact.status == MarkdownImportArtifactStatus.FAILED_MISSING:
        return _missing_result(artifact)
    if artifact.status != MarkdownImportArtifactStatus.SUCCEEDED:
        raise ValueError("session_artifact_incomplete")
    if not artifact.media_model or artifact.media_object_id is None:
        raise ValueError("session_media_object_missing")
    model = apps.get_model(artifact.media_model)
    if model is None:
        raise ValueError("session_media_model_invalid")
    instance = model._default_manager.filter(pk=artifact.media_object_id).first()
    if instance is None:
        raise ValueError("session_media_object_missing")
    return MediaImportResult(f"{artifact.media_type}_block", instance)


def _cleanup_import_artifact(artifact: MarkdownImportArtifact) -> bool:
    try:
        storage_registry = {artifact.storage_alias: storages[artifact.storage_alias]}
    except Exception:
        storage_registry = {}
    return cleanup_artifact_object(
        artifact,
        storages=storage_registry,
        reference_guard=lambda current: MarkdownImportArtifact.objects.filter(
            media_model=current.media_model,
            media_object_id=current.media_object_id,
        ).exclude(pk=current.pk).exists(),
    )


def _mark_expired(session: MarkdownImportSession) -> None:
    session.status = MarkdownImportSessionStatus.EXPIRED
    session.save(update_fields=["status", "updated_at"])
    # 会话没有生成页面时，已成功上传的媒体必须进入精确补偿队列，避免孤儿对象。
    session.artifacts.filter(
        status=MarkdownImportArtifactStatus.SUCCEEDED,
        cleanup_status=MarkdownImportArtifactCleanupStatus.NONE,
    ).update(cleanup_status=MarkdownImportArtifactCleanupStatus.PENDING, updated_at=timezone.now())
    batch = session.batch
    if batch.status not in {
        MarkdownImportBatchStatus.SUCCESS,
        MarkdownImportBatchStatus.PARTIAL_SUCCESS,
    }:
        batch.status = MarkdownImportBatchStatus.FAILED
        batch.error_code = "session_expired"
        batch.completed_at = timezone.now()
        batch.save(update_fields=["status", "error_code", "completed_at", "updated_at"])


def assemble_session(session_id: str) -> dict[str, object]:
    """在 worker 中组装页面；重复投递只返回已完成结果。"""

    with transaction.atomic():
        session = (
            MarkdownImportSession.objects.select_for_update()
            .select_related("batch", "batch__user")
            .filter(session_id=session_id)
            .first()
        )
        if session is None:
            return {"status": "not_found", "session_id": str(session_id)}
        if session.status in TERMINAL_SESSION_STATUSES:
            return session_payload(session)
        if session.status == MarkdownImportSessionStatus.ASSEMBLING:
            # 已有 worker 正在创建唯一草稿，重复投递只能观察状态，不能并行组装。
            return session_payload(session)
        if session_expired(session):
            _mark_expired(session)
            return session_payload(session)
        artifacts = list(session.artifacts.order_by("position"))
        completed = sum(
            artifact.status
            in {
                MarkdownImportArtifactStatus.SUCCEEDED,
                MarkdownImportArtifactStatus.FAILED_MISSING,
            }
            for artifact in artifacts
        )
        session.completed_artifacts = completed
        if completed != len(artifacts):
            session.status = MarkdownImportSessionStatus.UPLOADING
            session.save(update_fields=["status", "completed_artifacts", "updated_at"])
            return session_payload(session)
        session.status = MarkdownImportSessionStatus.ASSEMBLING
        session.save(update_fields=["status", "completed_artifacts", "updated_at"])

    draft = None
    artifacts = list(
        MarkdownImportArtifact.objects.filter(session__session_id=session_id).order_by("position")
    )
    session = MarkdownImportSession.objects.select_related("batch", "batch__user").get(
        session_id=session_id
    )
    try:
        # 解析器只在 worker 执行，上传请求不需要再次加载整篇正文。
        from blog.markdown_import_api import _blocks, _date_value, _intro, _tags

        manifest = session.manifest
        blocks = _blocks(manifest)
        media_results: dict[str, MediaImportResult] = {}
        for artifact in artifacts:
            result = _media_result(artifact)
            item = next(
                item
                for item in manifest.get("artifacts", [])
                if str(item.get("artifact_id")) == str(artifact.artifact_id)
            )
            for source in item.get("reference_sources", [artifact.normalized_source]):
                media_results[str(source)] = result
        body_values = assemble_import_body(blocks, media_results=media_results)
        draft = create_unpublished_blog_draft(
            session.batch.target_parent,
            title=str(manifest.get("title") or "未命名导入"),
            date=_date_value(manifest),
            intro=_intro(manifest),
            body_values=body_values,
            tags=_tags(manifest),
            user=session.batch.user,
        )
        partial = any(
            artifact.status == MarkdownImportArtifactStatus.FAILED_MISSING
            for artifact in artifacts
        )
        session.status = (
            MarkdownImportSessionStatus.PARTIAL_SUCCESS
            if partial
            else MarkdownImportSessionStatus.SUCCESS
        )
        session.assembly_requested_at = session.assembly_requested_at or timezone.now()
        session.completed_artifacts = len(artifacts)
        session.save(
            update_fields=[
                "status",
                "completed_artifacts",
                "assembly_requested_at",
                "updated_at",
            ]
        )
        batch = session.batch
        batch.status = (
            MarkdownImportBatchStatus.PARTIAL_SUCCESS
            if partial
            else MarkdownImportBatchStatus.SUCCESS
        )
        batch.result_page_id = draft.page.pk
        batch.result_revision_id = draft.revision.id
        batch.mongo_content_id = draft.mongo_draft_pointer
        batch.completed_at = timezone.now()
        batch.save(
            update_fields=[
                "status",
                "result_page",
                "result_revision_id",
                "mongo_content_id",
                "completed_at",
                "updated_at",
            ]
        )
        return session_payload(session)
    except Exception as exc:
        error_code = str(getattr(exc, "code", "session_assembly_failed"))[:64]
        media_artifacts = [
            artifact
            for artifact in artifacts
            if artifact.storage_alias and artifact.object_name
        ]
        compensation = compensate_draft_failure(
            page=draft.page if draft is not None else None,
            mongo_draft_pointer=draft.mongo_draft_pointer if draft is not None else "",
            media_artifacts=media_artifacts,
            delete_page=lambda page: page.delete(),
            delete_mongo_pointer=lambda pointer: __import__(
                "wagtailblog3.mongo", fromlist=["MongoManager"]
            ).MongoManager().delete_single_revision(pointer),
            cleanup_media=_cleanup_import_artifact,
        )
        session.status = MarkdownImportSessionStatus.FAILED
        session.save(update_fields=["status", "updated_at"])
        batch = session.batch
        batch.status = (
            MarkdownImportBatchStatus.CLEANUP_RETRY
            if not compensation.cleaned
            else MarkdownImportBatchStatus.FAILED
        )
        batch.error_code = error_code
        batch.error_message = ";".join(compensation.errors)[:2000]
        batch.completed_at = timezone.now()
        batch.save(update_fields=["status", "error_code", "error_message", "completed_at", "updated_at"])
        return session_payload(session)


def session_expiry_delta() -> timedelta:
    return timedelta(
        seconds=max(60, int(getattr(settings, "MARKDOWN_IMPORT_SESSION_TTL_SECONDS", 86400)))
    )
