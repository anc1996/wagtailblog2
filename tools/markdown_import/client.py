import argparse
import hashlib
import html
import json
import re
import sys
import tempfile
import time
import uuid
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit

import requests
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APPS_ROOT = PROJECT_ROOT / "wagtailblog3" / "apps"
if str(APPS_ROOT) not in sys.path:
    sys.path.insert(0, str(APPS_ROOT))

from blog.services.markdown_import_parser import parse_markdown_blocks
from blog.services.markdown_import_paths import resolve_local_media_path
from blog.services.markdown_import_remote import (
    RemoteImageDownloadError,
    download_remote_image,
)


MEDIA_BLOCKS = {"image_block": "image", "audio_block": "audio", "video_block": "video"}
AI_CONTEXT_MAX_CHARS = 24000


class _TextOnlyHTMLParser(HTMLParser):
    """只保留 HTML 文本节点，避免把媒体地址和属性发送给外部模型。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data):
        self.parts.append(data)


def _file_digest(path: Path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _session_checkpoint_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.markdown-import-session.json")


def _document_digest(path: Path) -> str:
    return _file_digest(path)[1]


def build_request_fingerprint(manifest: dict, target_parent_id: int) -> str:
    """生成不包含临时 UUID 和 multipart 字段的本地请求指纹。"""
    stable_manifest = {
        "target_parent_id": int(target_parent_id),
        "title": manifest.get("title", ""),
        "intro": manifest.get("intro", ""),
        "date": manifest.get("date", ""),
        "tags": list(manifest.get("tags") or []),
        "options": manifest.get("options") or {},
        "blocks": manifest.get("blocks") or [],
        "artifacts": [
            {
                key: value
                for key, value in item.items()
                if key not in {"artifact_id", "upload_field"}
            }
            for item in manifest.get("artifacts", [])
        ],
    }
    canonical = json.dumps(
        stable_manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _load_session_checkpoint(path: Path, target_parent_id: int):
    checkpoint_path = _session_checkpoint_path(path)
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if (
        checkpoint.get("version") not in {1, 2}
        or checkpoint.get("target_parent_id") != target_parent_id
        or checkpoint.get("document_sha256") != _document_digest(path)
        or not isinstance(checkpoint.get("idempotency_key"), str)
    ):
        return None
    checkpoint["legacy"] = checkpoint.get("version") == 1
    return checkpoint


def _save_session_checkpoint(
    path: Path,
    target_parent_id: int,
    manifest,
    session_id: str,
    *,
    request_fingerprint: str,
    session_status: str,
):
    """状态文件不保存 JWT、正文或存储凭据，只保存恢复所需的最小映射。"""

    payload = {
        "version": 2,
        "session_id": session_id,
        "idempotency_key": manifest["idempotency_key"],
        "target_parent_id": target_parent_id,
        "document_sha256": _document_digest(path),
        "request_fingerprint": request_fingerprint,
        "session_status": session_status,
        "artifacts": [
            {
                "media_type": item["media_type"],
                "source_kind": item["source_kind"],
                "normalized_source": item["normalized_source"],
                "artifact_id": item["artifact_id"],
            }
            for item in manifest["artifacts"]
        ],
    }
    checkpoint_path = _session_checkpoint_path(path)
    temporary = checkpoint_path.with_suffix(checkpoint_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(checkpoint_path)


def _checkpoint_can_resume(checkpoint: dict | None, request_fingerprint: str) -> bool:
    """只有活跃会话且请求指纹一致时才允许断点续传。"""
    if not checkpoint or checkpoint.get("legacy"):
        return False
    if checkpoint.get("request_fingerprint") != request_fingerprint:
        return False
    return checkpoint.get("session_status") in {
        "created",
        "uploading",
        "ready",
        "assembling",
    }


def load_session_checkpoint(path: Path, target_parent_id: int):
    """返回脱敏 checkpoint，供 GUI 决定继续或新建；不返回正文和凭据。"""
    return _load_session_checkpoint(path, target_parent_id)


def _clear_session_checkpoint(path: Path):
    _session_checkpoint_path(path).unlink(missing_ok=True)


def _artifact_key(item):
    return (
        str(item["media_type"]),
        str(item["source_kind"]),
        str(item["normalized_source"]),
    )


def _block_payload(block):
    return {
        "block_type": block.block_type,
        "value": block.value,
        "source_start_line": block.source_start_line,
        "source_end_line": block.source_end_line,
        "inline_images": [
            {
                "occurrence_id": image.occurrence_id,
                "source": image.source,
                "source_kind": image.source_kind,
                "syntax": image.syntax,
                "table_index": image.table_index,
                "row_index": image.row_index,
                "cell_index": image.cell_index,
            }
            for image in block.inline_images
        ],
    }


def _normalized_remote_source(source: str) -> str:
    parsed = urlsplit(source)
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("remote_image_url_invalid")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("remote_image_url_invalid") from exc
    if port not in (None, 443):
        raise ValueError("remote_image_url_invalid")
    return urlunsplit(("https", parsed.hostname.casefold(), parsed.path or "/", parsed.query, ""))


def _media_references(blocks):
    references = []
    for position, block in enumerate(blocks):
        if block.block_type in MEDIA_BLOCKS:
            references.append(
                {
                    "position": position,
                    "source": str(block.value.get("source") or ""),
                    "media_type": MEDIA_BLOCKS[block.block_type],
                    "scope": "block_media",
                    "occurrence_id": "",
                }
            )
        for image in block.inline_images:
            references.append(
                {
                    "position": position,
                    "source": image.source,
                    "media_type": "image",
                    "scope": "inline_image",
                    "occurrence_id": image.occurrence_id,
                }
            )
    return references


def _document(path: Path):
    source = path.read_text(encoding="utf-8")
    metadata = {}
    if source.startswith("---\n") or source.startswith("---\r\n"):
        lines = source.splitlines(keepends=True)
        closing = None
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() in {"---", "..."}:
                closing = index
                break
        if closing is None:
            raise ValueError("front_matter_invalid")
        try:
            loaded = yaml.safe_load("".join(lines[1:closing])) or {}
        except yaml.YAMLError as exc:
            raise ValueError("front_matter_invalid") from exc
        if not isinstance(loaded, dict):
            raise ValueError("front_matter_invalid")
        metadata = loaded
        source = "".join(lines[closing + 1 :])
    return metadata, source


def _parser_blocks(path: Path):
    _, source = _document(path)
    return parse_markdown_blocks(source)


def _markdown_to_ai_text(value: str) -> str:
    text = re.sub(r"```[\s\S]*?```|~~~[\s\S]*?~~~", " ", value)
    text = re.sub(r"^\s*\[[^\]]+\]:\s*\S+.*$", " ", text, flags=re.MULTILINE)
    text = re.sub(r"!\[\[[^\]]+\]\]", " ", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<https?://[^>]+>", " ", text, flags=re.IGNORECASE)
    parser = _TextOnlyHTMLParser()
    parser.feed(text)
    parser.close()
    text = " ".join(parser.parts)
    text = re.sub(r"https?://\S+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<!\w)(?:[A-Za-z]:[\\/]|\\\\)[^\s<>\"']+", " ", text)
    text = re.sub(
        r"(?i)(?<!\w)(?:\.\.?[\\/])?(?:[^\s<>\"']+[\\/])+[^\s<>\"']+\."
        r"(?:png|jpe?g|gif|webp|bmp|mp3|m4a|wav|ogg|mp4|webm|mov|pdf|md)\b",
        " ",
        text,
    )
    text = re.sub(r"(^|\n)\s{0,3}#{1,6}\s*", r"\1", text)
    text = re.sub(r"[>*_`~]", " ", html.unescape(text))
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def build_ai_context(path: Path, *, max_chars: int = AI_CONTEXT_MAX_CHARS) -> str:
    """从本地解析计划提取纯文本，不包含媒体块、URL、HTML 属性或本地绝对路径。"""

    parts = [
        _markdown_to_ai_text(str(block.value or ""))
        for block in _parser_blocks(path)
        if block.block_type == "markdown_block"
    ]
    context = "\n\n".join(part for part in parts if part).strip()
    if not context:
        raise ValueError("ai_context_empty")
    return context[:max_chars]


def fetch_ai_templates(url: str, token: str, target_parent_id: int, *, timeout: int = 15) -> list[dict]:
    response = requests.get(
        url.rstrip("/") + "/blog/api/markdown-import/ai/templates/",
        headers={"Authorization": f"Bearer {token}"},
        params={"target_parent_id": target_parent_id},
        timeout=timeout,
    )
    payload = _response_payload(response)
    templates = payload.get("templates")
    if not isinstance(templates, list):
        raise RuntimeError("ai_templates_invalid")
    return templates


def generate_ai_metadata(
    path: Path,
    *,
    url: str,
    token: str,
    target_parent_id: int,
    template_id: int,
    timeout: int = 75,
) -> dict:
    payload = _post_json(
        url.rstrip("/") + "/blog/api/markdown-import/ai/suggest/",
        {"Authorization": f"Bearer {token}"},
        {
            "target_parent_id": target_parent_id,
            "template_id": template_id,
            "language": "zh-hans",
            "context": build_ai_context(path),
        },
        timeout,
        0,
    )
    suggestion = payload.get("suggestion")
    if not isinstance(suggestion, dict):
        raise RuntimeError("ai_suggestion_invalid")
    intro = suggestion.get("intro")
    tags = suggestion.get("tags")
    if not isinstance(intro, str) or not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise RuntimeError("ai_suggestion_invalid")
    return {"intro": intro, "tags": tags}


def _front_matter_metadata(path: Path):
    metadata, _ = _document(path)
    title = metadata.get("title")
    intro = metadata.get("intro")
    date_value = metadata.get("date")
    tags = metadata.get("tags", [])
    if title is not None and not isinstance(title, str):
        raise ValueError("front_matter_title_invalid")
    if intro is not None and not isinstance(intro, str):
        raise ValueError("front_matter_intro_invalid")
    if date_value is not None and hasattr(date_value, "isoformat"):
        date_value = date_value.isoformat()
    elif date_value is not None and not isinstance(date_value, str):
        raise ValueError("front_matter_date_invalid")
    if isinstance(tags, str):
        tags = [item.strip() for item in tags.split(",") if item.strip()]
    if not isinstance(tags, list) or not all(isinstance(item, str) for item in tags):
        raise ValueError("front_matter_tags_invalid")
    return {
        "title": title or path.stem,
        "intro": intro or "",
        "date": date_value or time.strftime("%Y-%m-%d"),
        "tags": [item.strip() for item in tags if item.strip()],
    }


def build_import_manifest(
    path: Path,
    source_root: Path,
    *,
    allow_external_images: bool,
    metadata_overrides=None,
):
    """读取文件并生成不含本地绝对路径的 multipart manifest。"""

    metadata = _front_matter_metadata(path)
    if metadata_overrides:
        for key in ("title", "intro", "date", "tags"):
            if key in metadata_overrides and metadata_overrides[key] is not None:
                metadata[key] = metadata_overrides[key]
    blocks = _parser_blocks(path)
    artifacts = []
    files = []
    temporary_paths: list[Path] = []
    source_media_types = {}
    grouped = {}
    for reference in _media_references(blocks):
        source = reference["source"]
        media_type = reference["media_type"]
        previous_type = source_media_types.get(source)
        if previous_type is not None and previous_type != media_type:
            _remove_temporary_directory(source_root)
            raise ValueError("media_source_type_conflict")
        source_media_types[source] = media_type
        source_is_remote = urlsplit(source).scheme.casefold() == "https"
        if source_is_remote:
            normalized_source = _normalized_remote_source(source)
        else:
            resolved = resolve_local_media_path(source_root, source)
            normalized_source = resolved.normalized_source
        key = (
            media_type,
            "remote_https" if source_is_remote else "local",
            normalized_source,
        )
        group = grouped.setdefault(
            key,
            {
                "media_type": media_type,
                "source_kind": key[1],
                "normalized_source": normalized_source,
                "download_source": source,
                "position": reference["position"],
                "reference_sources": [],
                "scopes": set(),
                "occurrence_ids": [],
                "resolved_path": None if source_is_remote else resolved.path,
                "safe_filename": (
                    PurePosixPath(urlsplit(source).path).name or "remote-image.bin"
                    if source_is_remote
                    else resolved.safe_filename
                ),
            },
        )
        group["position"] = min(group["position"], reference["position"])
        if source not in group["reference_sources"]:
            group["reference_sources"].append(source)
        group["scopes"].add(reference["scope"])
        if reference["occurrence_id"]:
            group["occurrence_ids"].append(reference["occurrence_id"])

    for group in grouped.values():
        media_type = group["media_type"]
        source_is_remote = group["source_kind"] == "remote_https"
        preflight_error_code = ""
        if source_is_remote:
            if not allow_external_images or media_type != "image":
                raise ValueError("external_image_confirmation_required")
            temp_dir = source_root / ".markdown-import-tmp"
            temp_dir.mkdir(mode=0o700, exist_ok=True)
            try:
                downloaded = download_remote_image(
                    group["download_source"],
                    temp_dir,
                    allow_external_images=True,
                )
            except Exception:
                # 远程单张图片失败不应阻断同批本地媒体；以无文件 artifact 交给服务端生成缺失标记。
                downloaded = None
                preflight_error_code = "client_download_failed"
                _remove_temporary_directory(source_root)
            if downloaded is not None:
                resolved_path = downloaded.path
                safe_filename = downloaded.safe_filename
                temporary_paths.append(resolved_path)
            else:
                resolved_path = None
                safe_filename = group["safe_filename"]
        else:
            resolved_path = group["resolved_path"]
            safe_filename = group["safe_filename"]
        artifact_id = uuid.uuid4()
        upload_field = f"artifact_{artifact_id}"
        if resolved_path is None:
            size_bytes = 0
            sha256 = hashlib.sha256(b"").hexdigest()
        else:
            size_bytes, sha256 = _file_digest(resolved_path)
        artifacts.append(
            {
                "artifact_id": str(artifact_id),
                "position": group["position"],
                "media_type": media_type,
                "source_kind": group["source_kind"],
                "normalized_source": group["normalized_source"],
                "reference_sources": group["reference_sources"],
                "reference_scope": (
                    "mixed" if len(group["scopes"]) > 1 else next(iter(group["scopes"]))
                ),
                "occurrence_ids": group["occurrence_ids"],
                "safe_filename": safe_filename,
                "upload_field": upload_field,
                "size_bytes": size_bytes,
                "sha256": sha256,
                "preflight_error_code": preflight_error_code,
            }
        )
        if resolved_path is not None:
            files.append((upload_field, resolved_path))
    manifest = {
        "title": metadata["title"],
        "intro": metadata["intro"],
        "date": metadata["date"],
        "tags": metadata["tags"],
        "blocks": [_block_payload(block) for block in blocks],
        "artifacts": artifacts,
    }
    return manifest, files, temporary_paths


def inspect_markdown(path: Path, source_root: Path, *, allow_external_images: bool):
    metadata = _front_matter_metadata(path)
    blocks = _parser_blocks(path)
    references = _media_references(blocks)
    media = [block for block in blocks if block.block_type in MEDIA_BLOCKS]
    inline_images = [
        image
        for block in blocks
        for image in block.inline_images
    ]
    external = [
        reference["source"]
        for reference in references
        if urlsplit(reference["source"]).scheme.casefold() == "https"
    ]
    local_files = []
    errors = []
    seen_local = set()
    for reference in references:
        source = reference["source"]
        if source in external:
            continue
        try:
            resolved = resolve_local_media_path(source_root, source)
            if resolved.normalized_source not in seen_local:
                seen_local.add(resolved.normalized_source)
                local_files.append({"source": resolved.normalized_source, "size": resolved.path.stat().st_size})
        except Exception as exc:
            errors.append({"source": source, "code": str(exc)})
    return {
        "status": "preview",
        "title": metadata["title"],
        "intro": metadata["intro"],
        "date": metadata["date"],
        "tags": metadata["tags"],
        "block_count": len(blocks),
        "media_count": len(references),
        "block_media_count": len(media),
        "inline_image_count": len(inline_images),
        "inline_local_image_count": sum(image.source_kind == "local" for image in inline_images),
        "inline_remote_image_count": sum(image.source_kind == "remote_https" for image in inline_images),
        "inline_images": [
            {
                "occurrence_id": image.occurrence_id,
                "source": image.source,
                "source_kind": image.source_kind,
                "table_index": image.table_index,
                "row_index": image.row_index,
                "cell_index": image.cell_index,
            }
            for image in inline_images
        ],
        "external_images": external,
        "external_images_allowed": allow_external_images,
        "local_files": local_files,
        "errors": errors,
    }


def import_markdown(
    path: Path,
    source_root: Path,
    *,
    url: str,
    token: str,
    target_parent_id: int,
    idempotency_key: str,
    allow_external_images: bool,
    metadata_overrides=None,
    timeout: float = 30.0,
    retries: int = 2,
    progress_callback=None,
    assembly_timeout: float = 1800.0,
    force_new: bool = False,
):
    try:
        manifest, files, temporary_paths = build_import_manifest(
            path,
            source_root,
            allow_external_images=allow_external_images,
            metadata_overrides=metadata_overrides,
        )
    except Exception:
        _remove_temporary_directory(source_root)
        raise
    manifest["target_parent_id"] = target_parent_id
    manifest["options"] = {"allow_external_images": allow_external_images}
    request_fingerprint = build_request_fingerprint(manifest, target_parent_id)
    checkpoint = _load_session_checkpoint(path, target_parent_id)
    if not force_new and _checkpoint_can_resume(checkpoint, request_fingerprint):
        idempotency_key = checkpoint["idempotency_key"]
    try:
        uuid_value = uuid.UUID(idempotency_key)
    except (TypeError, ValueError) as exc:
        raise ValueError("idempotency_key_invalid") from exc
    if uuid_value.version != 4:
        raise ValueError("idempotency_key_not_uuid4")
    manifest["idempotency_key"] = str(uuid_value)
    base_endpoint = url.rstrip("/") + "/blog/api/markdown-import/"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        session = _post_json(base_endpoint + "sessions/", headers, manifest, timeout, retries)
        session_id = session["session_id"]
        server_artifacts = {
            _artifact_key(item): item
            for item in session.get("artifacts", [])
            if isinstance(item, dict)
        }
        for artifact in manifest["artifacts"]:
            server_artifact = server_artifacts.get(_artifact_key(artifact))
            if server_artifact is not None:
                artifact["artifact_id"] = str(server_artifact["artifact_id"])
        _save_session_checkpoint(
            path,
            target_parent_id,
            manifest,
            session_id,
            request_fingerprint=request_fingerprint,
            session_status=str(session.get("status") or "created"),
        )
        file_by_field = dict(files)
        total = len(manifest["artifacts"])
        completed = int(session.get("completed_artifacts", 0))
        if progress_callback:
            progress_callback("session_created", completed, total, session)
        for artifact in manifest["artifacts"]:
            if artifact.get("preflight_error_code"):
                continue
            server_artifact = server_artifacts.get(_artifact_key(artifact))
            if server_artifact and server_artifact.get("status") == "succeeded":
                continue
            file_path = file_by_field.get(artifact["upload_field"])
            if file_path is None:
                raise RuntimeError("upload_missing")
            endpoint = (
                base_endpoint
                + f"sessions/{session_id}/artifacts/{artifact['artifact_id']}/upload/"
            )
            response = _post_file(
                endpoint,
                headers,
                file_path,
                artifact["safe_filename"],
                timeout,
                retries,
            )
            completed = int(response.get("completed_artifacts", completed))
            _save_session_checkpoint(
                path,
                target_parent_id,
                manifest,
                session_id,
                request_fingerprint=request_fingerprint,
                session_status=str(response.get("status") or "uploading"),
            )
            if progress_callback:
                progress_callback("uploading", completed, total, response)
        final = _post_json(
            base_endpoint + f"sessions/{session_id}/finalize/",
            headers,
            {},
            timeout,
            retries,
        )
        if progress_callback:
            progress_callback("assembling", completed, total, final)
        _save_session_checkpoint(
            path,
            target_parent_id,
            manifest,
            session_id,
            request_fingerprint=request_fingerprint,
            session_status=str(final.get("status") or "ready"),
        )
        result = _wait_for_session(
            base_endpoint + f"sessions/{session_id}/",
            headers,
            timeout,
            assembly_timeout,
            progress_callback,
            total,
            checkpoint_update=lambda status: _save_session_checkpoint(
                path,
                target_parent_id,
                manifest,
                session_id,
                request_fingerprint=request_fingerprint,
                session_status=status,
            ),
        )
        if result.get("status") in {"success", "partial_success"}:
            _clear_session_checkpoint(path)
        return result
    finally:
        for temporary_path in temporary_paths:
            temporary_path.unlink(missing_ok=True)
        _remove_temporary_directory(source_root)


def _response_payload(response):
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400:
        code = payload.get("code") if isinstance(payload, dict) else None
        raise RuntimeError(str(code) if code else f"http_{response.status_code}")
    if not isinstance(payload, dict):
        raise RuntimeError("response_invalid")
    return payload


def _post_json(endpoint, headers, payload, timeout, retries):
    last_error = "request_failed"
    for attempt in range(retries + 1):
        try:
            response = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)
            if response.status_code < 500:
                return _response_payload(response)
            last_error = f"http_{response.status_code}"
        except requests.RequestException:
            last_error = "request_failed"
        if attempt < retries:
            time.sleep(min(2**attempt, 4))
    raise RuntimeError(last_error)


def _post_file(endpoint, headers, path, filename, timeout, retries):
    last_error = "request_failed"
    for attempt in range(retries + 1):
        try:
            with path.open("rb") as handle:
                response = requests.post(
                    endpoint,
                    headers=headers,
                    files={"file": (filename, handle, "application/octet-stream")},
                    timeout=timeout,
                )
            if response.status_code < 500:
                return _response_payload(response)
            last_error = f"http_{response.status_code}"
        except requests.RequestException:
            last_error = "request_failed"
        if attempt < retries:
            time.sleep(min(2**attempt, 4))
    raise RuntimeError(last_error)


def _wait_for_session(
    endpoint,
    headers,
    timeout,
    assembly_timeout,
    progress_callback,
    total,
    checkpoint_update=None,
):
    deadline = time.monotonic() + assembly_timeout
    terminal = {"success", "partial_success", "failed", "expired"}
    while time.monotonic() < deadline:
        try:
            response = requests.get(endpoint, headers=headers, timeout=timeout)
            payload = _response_payload(response)
        except requests.RequestException:
            time.sleep(1)
            continue
        status = str(payload.get("status") or "")
        if checkpoint_update:
            checkpoint_update(status)
        if progress_callback:
            progress_callback("assembling", int(payload.get("completed_artifacts", 0)), total, payload)
        if status in terminal:
            return payload
        time.sleep(1)
    raise RuntimeError("session_assembly_timeout")


def _remove_temporary_directory(source_root: Path):
    temporary_directory = source_root / ".markdown-import-tmp"
    try:
        temporary_directory.rmdir()
    except OSError:
        pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="导入 Markdown 为 Wagtail BlogPage 草稿")
    subparsers = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("markdown", type=Path)
    common.add_argument("--source-root", type=Path, required=True)
    common.add_argument("--allow-external-images", action="store_true")
    inspect = subparsers.add_parser("inspect", parents=[common])
    inspect.set_defaults(handler=lambda args: inspect_markdown(args.markdown, args.source_root, allow_external_images=args.allow_external_images))
    importing = subparsers.add_parser("import", parents=[common])
    importing.add_argument("--url", required=True)
    importing.add_argument("--token", required=True)
    importing.add_argument("--target-parent-id", required=True, type=int)
    importing.add_argument("--idempotency-key", required=True)
    importing.set_defaults(handler=lambda args: import_markdown(args.markdown, args.source_root, url=args.url, token=args.token, target_parent_id=args.target_parent_id, idempotency_key=args.idempotency_key, allow_external_images=args.allow_external_images))
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        print(json.dumps(args.handler(args), ensure_ascii=False, indent=2))
    except (ValueError, RemoteImageDownloadError, RuntimeError) as exc:
        print(json.dumps({"status": "failed", "code": str(exc)}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
