"""社交链接的规范化与平台识别。"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from django.core.exceptions import ValidationError


class SocialLinkValidationError(ValidationError):
    """表示不适合公开渲染的社交链接。"""


@dataclass(frozen=True)
class SocialLink:
    """模板可安全使用的社交链接展示数据。"""

    url: str
    label: str
    platform: str
    icon_name: str


_PLATFORM_RULES = (
    ("github", "GitHub", "github", ("github.com",)),
    ("linkedin", "LinkedIn", "linkedin", ("linkedin.com",)),
    ("zhihu", "知乎", "zhihu", ("zhihu.com",)),
    ("bilibili", "哔哩哔哩", "bilibili", ("bilibili.com", "b23.tv")),
    ("wechat", "微信", "weixin", ("weixin.qq.com",)),
    ("facebook", "Facebook", "facebook", ("facebook.com", "fb.com")),
    ("google", "Google", "google", ("google.com",)),
    ("instagram", "Instagram", "instagram", ("instagram.com",)),
)

def normalize_social_url(value: str) -> str:
    """返回用于渲染和去重的规范URL，只接受公开HTTP链接。"""

    try:
        parsed = urlsplit(value.strip())
    except (AttributeError, ValueError) as error:
        raise SocialLinkValidationError("链接地址无效。") from error

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise SocialLinkValidationError("社交链接仅支持 HTTP 或 HTTPS 协议。")
    if not parsed.hostname or parsed.username or parsed.password:
        raise SocialLinkValidationError("社交链接必须使用有效且不含账号信息的域名。")

    hostname = parsed.hostname.lower().removeprefix("www.")
    try:
        port = parsed.port
    except ValueError as error:
        raise SocialLinkValidationError("社交链接端口无效。") from error

    netloc = hostname
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{hostname}:{port}"

    path = parsed.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, parsed.query, parsed.fragment))


def resolve_social_platform(url: str) -> tuple[str, str, str]:
    """通过规范化后的域名决定受控的平台名称和图标。"""

    hostname = urlsplit(url).hostname or ""
    for platform, label, icon_name, domains in _PLATFORM_RULES:
        if any(hostname == domain or hostname.endswith(f".{domain}") for domain in domains):
            return platform, label, icon_name
    return "website", "网站", "website"


def resolve_navigation_social_links(navigation_settings) -> list[SocialLink]:
    """只读取结构化社交链接，空设置不再回退到已退役字段。"""

    return _build_social_links(_extract_structured_links(navigation_settings))


def _extract_structured_links(navigation_settings) -> list[dict[str, str]]:
    """将StreamField值转换为普通字典，兼容历史上可能保存的无效块。"""

    links = []
    for block in getattr(navigation_settings, "social_links", ()) or ():
        if getattr(block, "block_type", None) != "social_link":
            continue
        value = block.value
        links.append({"url": value.get("url", ""), "label": value.get("label", "")})
    return links


def _build_social_links(candidates: list[dict[str, str]]) -> list[SocialLink]:
    """丢弃无效和重复链接，避免错误设置影响公共页脚。"""

    social_links = []
    seen_urls = set()
    for candidate in candidates:
        try:
            url = normalize_social_url(candidate.get("url", ""))
        except SocialLinkValidationError:
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        platform, detected_label, icon_name = resolve_social_platform(url)
        label = str(candidate.get("label", "")).strip() or detected_label
        social_links.append(
            SocialLink(url=url, label=label, platform=platform, icon_name=icon_name)
        )
    return social_links
