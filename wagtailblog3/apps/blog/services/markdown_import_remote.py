"""Markdown 导入远程图片的 HTTPS、DNS、大小和图像内容安全校验。"""

import hashlib
import http.client
import ipaddress
import socket
import ssl
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

from PIL import Image, UnidentifiedImageError


Resolver = Callable[..., Sequence[tuple]]
Fetcher = Callable[[str, str, float], "RemoteFetchResponse"]


class RemoteImageDownloadError(ValueError):
    """表示远程图片不满足下载或内容安全策略。"""


@dataclass(frozen=True, slots=True)
class RemoteImagePolicy:
    """远程图片下载的字节数、像素数、重定向次数和超时上限。"""

    max_bytes: int = 10 * 1024 * 1024
    max_pixels: int = 40_000_000
    max_redirects: int = 3
    timeout_seconds: float = 10.0


@dataclass(frozen=True, slots=True)
class RemoteFetchResponse:
    """可流式读取的远程响应及其关闭回调，不持有完整响应正文。"""

    status: int
    headers: Mapping[str, str]
    chunks: Iterable[bytes]
    close: Callable[[], None] | None = None


@dataclass(frozen=True, slots=True)
class DownloadedRemoteImage:
    """远程图片落盘并通过内容校验后的不可变结果。"""

    path: Path
    safe_filename: str
    source_url: str
    image_format: str
    width: int
    height: int
    size_bytes: int
    sha256: str


_FORMAT_EXTENSIONS = {
    "GIF": ".gif",
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
}
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def _normalized_parts(url: str) -> SplitResult:
    """规范化 HTTPS URL，并拒绝凭据、缺失主机和控制字符。"""
    if not url or any(ord(character) < 32 for character in url):
        raise RemoteImageDownloadError("remote_url_invalid")
    try:
        parsed = urlsplit(url.strip())
        port = parsed.port
    except ValueError as exc:
        raise RemoteImageDownloadError("remote_url_invalid") from exc
    if parsed.scheme.casefold() != "https":
        raise RemoteImageDownloadError("remote_scheme_forbidden")
    if parsed.username is not None or parsed.password is not None:
        raise RemoteImageDownloadError("remote_credentials_forbidden")
    if not parsed.hostname:
        raise RemoteImageDownloadError("remote_host_missing")

    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise RemoteImageDownloadError("remote_host_invalid") from exc
    host_for_url = f"[{hostname}]" if ":" in hostname else hostname
    netloc = host_for_url if port in (None, 443) else f"{host_for_url}:{port}"
    return SplitResult(
        scheme="https",
        netloc=netloc,
        path=parsed.path or "/",
        query=parsed.query,
        fragment="",
    )


def normalize_remote_image_url(url: str) -> str:
    """规范化可用于同批去重的 HTTPS 图片 URL。"""

    return urlunsplit(_normalized_parts(url))


def resolve_public_addresses(
    hostname: str,
    port: int,
    *,
    resolver: Resolver = socket.getaddrinfo,
) -> tuple[str, ...]:
    """解析主机全部地址；混入任意非公网地址时整体拒绝。"""

    try:
        answers = resolver(hostname, port, 0, socket.SOCK_STREAM)
    except OSError as exc:
        raise RemoteImageDownloadError("remote_dns_failed") from exc
    addresses: list[str] = []
    for answer in answers:
        try:
            address = str(ipaddress.ip_address(answer[4][0]))
        except (IndexError, TypeError, ValueError) as exc:
            raise RemoteImageDownloadError("remote_dns_invalid") from exc
        if not ipaddress.ip_address(address).is_global:
            raise RemoteImageDownloadError("remote_address_forbidden")
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise RemoteImageDownloadError("remote_dns_empty")
    return tuple(addresses)


def _default_fetcher(url: str, address: str, timeout: float) -> RemoteFetchResponse:
    parsed = _normalized_parts(url)
    hostname = parsed.hostname
    if hostname is None:
        raise RemoteImageDownloadError("remote_host_missing")
    port = parsed.port or 443
    raw_socket = socket.create_connection((address, port), timeout=timeout)
    connection = http.client.HTTPSConnection(
        hostname, port=port, timeout=timeout, context=ssl.create_default_context()
    )
    try:
        # 连接固定到已校验的地址，TLS 仍使用原域名验证证书和 SNI。
        connection.sock = connection._context.wrap_socket(
            raw_socket, server_hostname=hostname
        )
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        connection.request(
            "GET",
            target,
            headers={
                "Accept": "image/*",
                "Host": parsed.netloc,
                "User-Agent": "wagtailblog-markdown-import/1",
            },
        )
        response = connection.getresponse()
    except Exception:
        connection.close()
        raise

    def chunks() -> Iterable[bytes]:
        while chunk := response.read(64 * 1024):
            yield chunk

    def close() -> None:
        response.close()
        connection.close()

    return RemoteFetchResponse(
        status=response.status,
        headers={key.casefold(): value for key, value in response.getheaders()},
        chunks=chunks(),
        close=close,
    )


def _close_response(response: RemoteFetchResponse) -> None:
    close_chunks = getattr(response.chunks, "close", None)
    if callable(close_chunks):
        close_chunks()
    if response.close is not None:
        response.close()


def _download_to_path(
    response: RemoteFetchResponse, path: Path, max_bytes: int
) -> tuple[int, str]:
    """流式写入临时文件并同步计算大小和 SHA-256，超过上限立即失败。"""
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except ValueError as exc:
            raise RemoteImageDownloadError("remote_content_length_invalid") from exc
        if declared_size < 0 or declared_size > max_bytes:
            raise RemoteImageDownloadError("remote_response_too_large")

    size = 0
    digest = hashlib.sha256()
    with path.open("wb") as output:
        for chunk in response.chunks:
            if not isinstance(chunk, bytes):
                raise RemoteImageDownloadError("remote_response_invalid")
            size += len(chunk)
            if size > max_bytes:
                raise RemoteImageDownloadError("remote_response_too_large")
            output.write(chunk)
            digest.update(chunk)
    if size == 0:
        raise RemoteImageDownloadError("remote_response_empty")
    return size, digest.hexdigest()


def _inspect_image(path: Path, policy: RemoteImagePolicy) -> tuple[str, int, int]:
    """验证图片格式、尺寸和完整性，防止伪造扩展名或解压炸弹。"""
    try:
        with Image.open(path) as image:
            image_format = str(image.format or "").upper()
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > policy.max_pixels:
                raise RemoteImageDownloadError(
                    "remote_image_dimensions_invalid"
                )
            image.verify()
    except RemoteImageDownloadError:
        raise
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise RemoteImageDownloadError("remote_image_invalid") from exc
    if image_format not in _FORMAT_EXTENSIONS:
        raise RemoteImageDownloadError("remote_image_format_unsupported")
    return image_format, width, height


def download_remote_image(
    url: str,
    destination: Path,
    *,
    allow_external_images: bool,
    policy: RemoteImagePolicy | None = None,
    resolver: Resolver = socket.getaddrinfo,
    fetcher: Fetcher = _default_fetcher,
) -> DownloadedRemoteImage:
    """安全下载并解码远程图片，失败时不保留半文件。

    参数：目标 URL、临时目录、是否允许外部图片及可选策略/解析器/抓取器。
    返回：包含最终文件、格式、尺寸、字节数和摘要的 :class:`DownloadedRemoteImage`。
    异常：协议、DNS、非公网地址、重定向、HTTP 状态、大小或图片内容不满足策略时抛出
        :class:`RemoteImageDownloadError`。

    算法在每次重定向后重新规范化 URL 并解析全部 DNS 地址；只要混入一个非公网地址
    就拒绝，降低 SSRF 风险。响应先写入随机临时文件并检查大小/摘要，再验证图片内容，
    最后按真实格式改名；任何异常都会关闭响应并删除临时文件，避免留下半文件。
    """

    if not allow_external_images:
        raise RemoteImageDownloadError("external_images_not_allowed")
    active_policy = policy or RemoteImagePolicy()
    if not destination.is_dir():
        raise RemoteImageDownloadError("temporary_directory_invalid")

    temporary = tempfile.NamedTemporaryFile(
        mode="wb", prefix=".markdown-import-", suffix=".part", dir=destination, delete=False
    )
    temporary_path = Path(temporary.name)
    temporary.close()
    current_url = normalize_remote_image_url(url)
    response: RemoteFetchResponse | None = None
    try:
        for redirect_count in range(active_policy.max_redirects + 1):
            parsed = _normalized_parts(current_url)
            hostname = parsed.hostname
            if hostname is None:
                raise RemoteImageDownloadError("remote_host_missing")
            addresses = resolve_public_addresses(
                hostname, parsed.port or 443, resolver=resolver
            )
            try:
                response = fetcher(
                    current_url, addresses[0], active_policy.timeout_seconds
                )
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                raise RemoteImageDownloadError("remote_fetch_failed") from exc

            headers = {key.casefold(): value for key, value in response.headers.items()}
            if response.status in _REDIRECT_STATUSES:
                location = headers.get("location")
                _close_response(response)
                response = None
                if not location:
                    raise RemoteImageDownloadError("remote_redirect_invalid")
                if redirect_count >= active_policy.max_redirects:
                    raise RemoteImageDownloadError("remote_redirect_limit")
                current_url = normalize_remote_image_url(urljoin(current_url, location))
                continue
            if response.status != 200:
                raise RemoteImageDownloadError("remote_http_status")
            response = RemoteFetchResponse(
                status=response.status,
                headers=headers,
                chunks=response.chunks,
                close=response.close,
            )
            size, digest = _download_to_path(
                response, temporary_path, active_policy.max_bytes
            )
            _close_response(response)
            response = None
            break
        else:
            raise RemoteImageDownloadError("remote_redirect_limit")

        image_format, width, height = _inspect_image(temporary_path, active_policy)
        extension = _FORMAT_EXTENSIONS[image_format]
        final_path = temporary_path.with_suffix(extension)
        temporary_path.replace(final_path)
        return DownloadedRemoteImage(
            path=final_path,
            safe_filename=f"remote-image{extension}",
            source_url=current_url,
            image_format=image_format,
            width=width,
            height=height,
            size_bytes=size,
            sha256=digest,
        )
    except Exception:
        if response is not None:
            _close_response(response)
        temporary_path.unlink(missing_ok=True)
        raise
