import hashlib
import hashlib
from dataclasses import dataclass
from pathlib import PurePosixPath

from django.apps import apps
from django.utils.functional import empty
from django.utils.text import get_valid_filename

from blog.models import (
    MarkdownImportArtifactCleanupStatus,
    MarkdownImportArtifactStatus,
)


class MediaImportError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class MediaImportResult:
    block_type: str
    value: object


@dataclass(frozen=True, slots=True)
class MediaProbeResult:
    valid: bool
    mime: str
    container: str
    codec: str


_MP3_BITRATES = {
    3: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_MP3_SAMPLE_RATES = {
    3: (44100, 48000, 32000),
    2: (22050, 24000, 16000),
    0: (11025, 12000, 8000),
}
_MP4_CONTAINERS = {
    "moov",
    "trak",
    "mdia",
    "minf",
    "stbl",
    "edts",
    "dinf",
    "udta",
}
_MP4_VIDEO_CODECS = {
    "avc1": "h264",
    "avc3": "h264",
    "hvc1": "h265",
    "hev1": "h265",
    "av01": "av1",
    "vp09": "vp9",
}
_MP4_AUDIO_CODECS = {
    "mp4a": "aac",
    "ac-3": "ac3",
    "ec-3": "eac3",
    "Opus": "opus",
    "fLaC": "flac",
}


def _mp3_frame_length(data: bytes, offset: int) -> int | None:
    if offset + 4 > len(data):
        return None
    header = int.from_bytes(data[offset : offset + 4], "big")
    if (header >> 21) & 0x7FF != 0x7FF:
        return None
    version = (header >> 19) & 0x3
    layer = (header >> 17) & 0x3
    bitrate_index = (header >> 12) & 0xF
    sample_index = (header >> 10) & 0x3
    padding = (header >> 9) & 0x1
    if version == 1 or layer != 1 or bitrate_index in {0, 15} or sample_index == 3:
        return None
    bitrate = _MP3_BITRATES[3 if version == 3 else 2][bitrate_index]
    sample_rate = _MP3_SAMPLE_RATES[version][sample_index]
    multiplier = 144 if version == 3 else 72
    frame_length = multiplier * bitrate * 1000 // sample_rate + padding
    return frame_length if frame_length >= 24 else None


def _probe_mp3(data: bytes) -> MediaProbeResult | None:
    offset = 0
    if data.startswith(b"ID3") and len(data) >= 10:
        tag_size = 0
        for value in data[6:10]:
            if value & 0x80:
                return None
            tag_size = (tag_size << 7) | value
        offset = 10 + tag_size
    while offset + 8 <= len(data):
        frame_length = _mp3_frame_length(data, offset)
        if frame_length is not None:
            next_offset = offset + frame_length
            if _mp3_frame_length(data, next_offset) is not None:
                return MediaProbeResult(True, "audio/mpeg", "mp3", "mpeg-layer3")
        offset += 1
    return None


def _parse_mp4_boxes(data: bytes, start: int, end: int):
    boxes = []
    cursor = start
    while cursor < end:
        if end - cursor < 8:
            return None
        size = int.from_bytes(data[cursor : cursor + 4], "big")
        box_type = data[cursor + 4 : cursor + 8].decode("latin1")
        header_size = 8
        if size == 1:
            if end - cursor < 16:
                return None
            size = int.from_bytes(data[cursor + 8 : cursor + 16], "big")
            header_size = 16
        elif size == 0:
            size = end - cursor
        if size < header_size or cursor + size > end:
            return None
        boxes.append((box_type, cursor + header_size, cursor + size))
        cursor += size
    return boxes


def _nested_mp4_boxes(data: bytes, start: int, end: int):
    boxes = _parse_mp4_boxes(data, start, end)
    if boxes is None:
        return None
    nested = []
    for box in boxes:
        nested.append(box)
        if box[0] in _MP4_CONTAINERS:
            children = _nested_mp4_boxes(data, box[1], box[2])
            if children is None:
                return None
            nested.extend(children)
    return nested


def _probe_mp4(data: bytes) -> MediaProbeResult | None:
    roots = _parse_mp4_boxes(data, 0, len(data))
    if roots is None:
        return None
    ftyp = next((box for box in roots if box[0] == "ftyp"), None)
    moov = next((box for box in roots if box[0] == "moov"), None)
    if ftyp is None or moov is None or ftyp[2] - ftyp[1] < 8:
        return None
    tracks = _parse_mp4_boxes(data, moov[1], moov[2])
    if tracks is None:
        return None
    found = []
    for track in tracks:
        if track[0] != "trak":
            continue
        nested = _nested_mp4_boxes(data, track[1], track[2])
        if nested is None:
            return None
        handler_box = next((box for box in nested if box[0] == "hdlr"), None)
        sample_box = next((box for box in nested if box[0] == "stsd"), None)
        if handler_box is None or sample_box is None or handler_box[2] - handler_box[1] < 12:
            continue
        handler = data[handler_box[1] + 8 : handler_box[1] + 12].decode("latin1")
        if sample_box[2] - sample_box[1] < 16:
            continue
        codec = data[sample_box[1] + 12 : sample_box[1] + 16].decode("latin1")
        if handler == "vide" and codec in _MP4_VIDEO_CODECS:
            found.append(MediaProbeResult(True, "video/mp4", "mp4", _MP4_VIDEO_CODECS[codec]))
        elif handler == "soun" and codec in _MP4_AUDIO_CODECS:
            found.append(MediaProbeResult(True, "audio/mp4", "mp4", _MP4_AUDIO_CODECS[codec]))
    video = next((item for item in found if item.mime.startswith("video/")), None)
    return video or (found[0] if found else None)


def probe_media_content(upload) -> MediaProbeResult:
    """用文件结构和轨道信息探测媒体，不信任扩展名或客户端 MIME。"""
    position = upload.tell()
    try:
        upload.seek(0)
        data = upload.read()
    except Exception:
        return MediaProbeResult(False, "", "", "")
    finally:
        upload.seek(position)
    if not isinstance(data, bytes) or not data:
        return MediaProbeResult(False, "", "", "")
    if data.startswith(b"ID3") or _mp3_frame_length(data, 0) is not None:
        result = _probe_mp3(data)
        if result is not None:
            return result
    result = _probe_mp4(data)
    return result or MediaProbeResult(False, "", "", "")


def validate_media_upload(
    media_type,
    upload,
    *,
    form_factory=None,
    model_instance=None,
    form_data=None,
    user=None,
    content_probe=None,
):
    """执行内容探测和项目真实表单校验，但不写入对象存储。"""

    if media_type not in {"image", "audio", "video"}:
        raise MediaImportError("media_type_invalid")

    if media_type in {"audio", "video"}:
        if content_probe is None:
            # filetype 不能验证容器和编解码器，缺少深度解析器时必须拒绝导入。
            raise MediaImportError("media_deep_probe_unavailable")
        position = upload.tell()
        try:
            upload.seek(0)
            try:
                probe_result = content_probe(upload)
            except Exception as exc:
                raise MediaImportError("media_probe_failed") from exc
        finally:
            upload.seek(position)
        if not isinstance(probe_result, MediaProbeResult):
            raise MediaImportError("media_deep_probe_invalid")
        if not probe_result.mime.startswith(f"{media_type}/"):
            raise MediaImportError("media_content_type_mismatch")
        if (
            not probe_result.valid
            or not probe_result.container
            or not probe_result.codec
        ):
            raise MediaImportError("media_deep_probe_invalid")

    if form_factory is None or model_instance is None:
        if media_type == "image":
            from wagtail.images import get_image_model
            from wagtail.images.forms import get_image_form

            model = get_image_model()
            model_instance = model(uploaded_by_user=user)
            form_factory = get_image_form(model)
        else:
            from wagtailmedia.forms import get_media_form
            from wagtailmedia.models import get_media_model

            model = get_media_model()
            model_instance = model(type=media_type, uploaded_by_user=user)
            form_factory = get_media_form(model)

    try:
        form = form_factory(
            data=form_data or {},
            files={"file": upload},
            instance=model_instance,
            user=user,
        )
    except Exception as exc:
        # collection 权限或表单构造异常不得泄露内部信息，也不能中断同批媒体。
        raise MediaImportError("media_form_invalid") from exc
    if not form.is_valid():
        raise MediaImportError("media_form_invalid")
    upload.seek(0)
    return form


def _persist(artifact, *fields):
    available = [field for field in fields if hasattr(artifact, field)]
    if available:
        artifact.save(update_fields=available)


def _set_cleanup_retry(artifact, error_code: str) -> bool:
    artifact.cleanup_status = MarkdownImportArtifactCleanupStatus.RETRY
    artifact.cleanup_error_code = error_code
    _persist(artifact, "cleanup_status", "cleanup_error_code", "updated_at")
    return False


def _default_model_resolver(model_label, object_id):
    model = apps.get_model(model_label)
    return model._default_manager.filter(pk=object_id).first()


def _storage_from_registry(registry, alias):
    """兼容 Django StorageHandler 与测试/调用方传入的字典注册表。"""

    getter = getattr(registry, "get", None)
    if getter is not None:
        return getter(alias)
    try:
        return registry[alias]
    except (KeyError, TypeError, AttributeError):
        return None


def cleanup_artifact_object(
    artifact,
    *,
    storages,
    reference_guard,
    model_resolver=None,
) -> bool:
    """只按审计行中的精确 alias/name 清理当前 artifact。"""

    if not artifact.storage_alias or not artifact.object_name:
        return _set_cleanup_retry(artifact, "cleanup_invalid_evidence")
    storage = storages.get(artifact.storage_alias)
    if storage is None:
        return _set_cleanup_retry(artifact, "cleanup_unknown_storage")
    try:
        referenced = reference_guard(artifact)
    except Exception:
        return _set_cleanup_retry(artifact, "cleanup_reference_check_failed")
    if referenced:
        return _set_cleanup_retry(artifact, "cleanup_referenced")

    has_model_label = bool(artifact.media_model)
    has_model_id = artifact.media_object_id is not None
    if has_model_label != has_model_id:
        return _set_cleanup_retry(artifact, "cleanup_invalid_model_evidence")
    if has_model_label:
        resolver = model_resolver or _default_model_resolver
        try:
            media_object = resolver(
                artifact.media_model,
                artifact.media_object_id,
            )
        except Exception:
            return _set_cleanup_retry(artifact, "media_model_lookup_failed")
        if media_object is not None:
            try:
                media_object.delete()
            except Exception:
                return _set_cleanup_retry(artifact, "media_model_delete_failed")

    try:
        object_exists = storage.exists(artifact.object_name)
    except Exception:
        return _set_cleanup_retry(artifact, "storage_exists_failed")

    try:
        if object_exists:
            storage.delete(artifact.object_name)
            if storage.exists(artifact.object_name):
                return _set_cleanup_retry(artifact, "storage_delete_incomplete")
    except Exception:
        return _set_cleanup_retry(artifact, "storage_delete_failed")

    artifact.cleanup_status = MarkdownImportArtifactCleanupStatus.CLEANED
    artifact.cleanup_error_code = ""
    _persist(artifact, "cleanup_status", "cleanup_error_code", "updated_at")
    return True


def _safe_object_name(artifact, upload, *, max_length: int = 100) -> str:
    filename = get_valid_filename(PurePosixPath(upload.name or "media.bin").name)
    filename = filename or "media.bin"
    prefix = f"markdown-import/{artifact.artifact_id.hex}/"
    available = max_length - len(prefix)
    if available < 8:
        raise MediaImportError("media_file_field_too_short")
    if len(filename) > available:
        suffix = PurePosixPath(filename).suffix
        stem_length = available - len(suffix)
        filename = f"{PurePosixPath(filename).stem[:stem_length]}{suffix}"
    return f"{prefix}{filename}"


def _upload_sha256(upload) -> str:
    digest = hashlib.sha256()
    upload.seek(0)
    for chunk in iter(lambda: upload.read(1024 * 1024), b""):
        digest.update(chunk)
    upload.seek(0)
    return digest.hexdigest()


def _missing_result(artifact, error_code: str) -> MediaImportResult:
    artifact.status = MarkdownImportArtifactStatus.FAILED_MISSING
    artifact.error_code = error_code
    _persist(artifact, "status", "error_code", "updated_at")
    reference = _safe_missing_reference(artifact)
    marker = (
        f"[导入缺失：{artifact.media_type} 原始引用：{reference} "
        f"原因：{error_code}]"
    )
    return MediaImportResult("markdown_block", marker)


def _safe_missing_reference(artifact) -> str:
    for candidate in (
        getattr(artifact, "normalized_source", ""),
        getattr(artifact, "safe_filename", ""),
    ):
        value = str(candidate or "").replace("\\", "/")
        if not value:
            continue
        path = PurePosixPath(value)
        first_part = path.parts[0] if path.parts else ""
        if path.is_absolute() or ".." in path.parts or ":" in first_part:
            return path.name or "未知文件"
        return path.as_posix()
    return "未知文件"


def _delete_exact_names(storage, names):
    failed = False
    seen = set()
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        try:
            storage.delete(name)
            if storage.exists(name):
                failed = True
        except Exception:
            failed = True
    return not failed


def _resolved_storage(storage):
    wrapped = getattr(storage, "_wrapped", None)
    if wrapped is None:
        return storage
    if wrapped is empty:
        storage._setup()
        wrapped = storage._wrapped
    return wrapped


def _same_storage(left, right) -> bool:
    return left is right or _resolved_storage(left) is _resolved_storage(right)


def import_media_artifact(
    artifact,
    upload,
    *,
    validated_form,
    storage,
    storage_alias,
    storage_registry=None,
):
    """保存单个媒体；任何失败只补偿当前 artifact 的精确对象。"""

    # 当前图片与音视频字段均绑定 default storage，禁止把未验证别名写入审计证据。
    if storage_registry is None:
        raise MediaImportError("storage_registry_required")
    registered_storage = _storage_from_registry(storage_registry, storage_alias)
    if registered_storage is None:
        raise MediaImportError("storage_alias_invalid")
    if not _same_storage(registered_storage, storage):
        raise MediaImportError("storage_alias_mismatch")

    instance = validated_form.save(commit=False)
    # alias 与 FieldFile 的真实 storage 必须指向同一实例，避免审计证据写错存储。
    field_storage = instance.file.storage
    if not _same_storage(field_storage, registered_storage):
        raise MediaImportError("storage_alias_mismatch")
    storage = field_storage

    artifact.storage_alias = storage_alias
    get_field = getattr(instance._meta, "get_field", None)
    file_field = get_field("file") if callable(get_field) else None
    max_name_length = int(getattr(file_field, "max_length", 100) or 100)
    artifact.object_name = _safe_object_name(
        artifact,
        upload,
        max_length=max_name_length,
    )
    artifact.sha256 = _upload_sha256(upload)
    artifact.cleanup_status = MarkdownImportArtifactCleanupStatus.PENDING
    artifact.cleanup_error_code = ""
    _persist(
        artifact,
        "storage_alias",
        "object_name",
        "sha256",
        "cleanup_status",
        "cleanup_error_code",
        "updated_at",
    )

    try:
        object_exists = storage.exists(artifact.object_name)
    except Exception:
        artifact.cleanup_status = MarkdownImportArtifactCleanupStatus.NONE
        artifact.cleanup_error_code = ""
        _persist(artifact, "cleanup_status", "cleanup_error_code", "updated_at")
        return _missing_result(artifact, "storage_exists_failed")
    if object_exists:
        artifact.cleanup_status = MarkdownImportArtifactCleanupStatus.NONE
        artifact.cleanup_error_code = ""
        _persist(artifact, "cleanup_status", "cleanup_error_code", "updated_at")
        return _missing_result(artifact, "storage_name_collision")

    actual_name = None
    try:
        upload.seek(0)
        actual_name = storage.save(artifact.object_name, upload)
    except Exception:
        # save 抛错时无法证明计划名称是否由本次调用创建，保留证据等待后续复核。
        artifact.cleanup_status = MarkdownImportArtifactCleanupStatus.RETRY
        artifact.cleanup_error_code = "storage_save_uncertain"
        _persist(artifact, "cleanup_status", "cleanup_error_code", "updated_at")
        return _missing_result(artifact, "storage_save_failed")

    if actual_name != artifact.object_name:
        # actual_name 可确认由本次调用返回；planned 可能属于并发写入，禁止删除。
        cleaned = _delete_exact_names(storage, (actual_name,))
        artifact.cleanup_status = (
            MarkdownImportArtifactCleanupStatus.CLEANED
            if cleaned
            else MarkdownImportArtifactCleanupStatus.RETRY
        )
        artifact.cleanup_error_code = "" if cleaned else "storage_delete_failed"
        _persist(artifact, "cleanup_status", "cleanup_error_code", "updated_at")
        return _missing_result(artifact, "storage_name_mismatch")

    # 绑定已精确写入的对象名；最终仍走表单保存，以保留 Wagtail 图片元数据、
    # 索引和媒体 collection 等框架行为。
    instance.file.name = artifact.object_name
    instance.file._committed = True
    try:
        instance = validated_form.save(commit=True)
    except Exception:
        # 表单保存可能在数据库行已生成后因索引等后续步骤抛错，必须先保留精确模型证据。
        instance_persisted = not getattr(instance._state, "adding", True)
        if instance_persisted and instance.pk is not None:
            artifact.media_model = instance._meta.label_lower
            artifact.media_object_id = instance.pk
            _persist(
                artifact,
                "media_model",
                "media_object_id",
                "updated_at",
            )
        cleaned = _delete_exact_names(storage, (artifact.object_name,))
        model_cleanup_pending = instance_persisted and instance.pk is not None
        artifact.cleanup_status = (
            MarkdownImportArtifactCleanupStatus.RETRY
            if model_cleanup_pending or not cleaned
            else MarkdownImportArtifactCleanupStatus.CLEANED
        )
        artifact.cleanup_error_code = (
            "media_model_cleanup_pending"
            if model_cleanup_pending
            else ("" if cleaned else "storage_delete_failed")
        )
        _persist(artifact, "cleanup_status", "cleanup_error_code", "updated_at")
        return _missing_result(artifact, "media_model_save_failed")

    artifact.status = MarkdownImportArtifactStatus.SUCCEEDED
    artifact.media_model = instance._meta.label_lower
    artifact.media_object_id = instance.pk
    artifact.error_code = ""
    artifact.cleanup_status = MarkdownImportArtifactCleanupStatus.NONE
    artifact.cleanup_error_code = ""
    _persist(
        artifact,
        "status",
        "media_model",
        "media_object_id",
        "error_code",
        "cleanup_status",
        "cleanup_error_code",
        "updated_at",
    )
    return MediaImportResult(f"{artifact.media_type}_block", instance)


def import_media_artifacts(artifacts, *, importer):
    results = []
    for artifact in artifacts:
        try:
            result = importer(artifact)
        except MediaImportError as exc:
            result = _missing_result(artifact, exc.code)
        except Exception:
            # 单个媒体的未知校验/存储异常不能中断同批其他媒体，详情只保留稳定错误码。
            result = _missing_result(artifact, "media_import_failed")
        results.append(result)
    return results
