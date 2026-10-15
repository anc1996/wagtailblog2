"""公开搜索的共享授权边界；Scope 是请求身份，不是缓存授权证明。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from typing import Any
from zoneinfo import ZoneInfo

from django.conf import settings
from wagtail.models import Locale, Page, Site
from external_content.models import ExternalContentDeletionIntent


class SearchRequestError(ValueError):
    """仅包含脱敏参数错误代码。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class SearchSafetyUnavailable(RuntimeError):
    """SQL、Mongo 或必要来源不可用时禁止生成成功响应。"""

    def __init__(self, code: str = "search_unavailable"):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SearchScope:
    site_id: int
    root_page_id: int
    root_path: str
    locale_id: int
    search_type: str = "all"
    order_by: str = ""
    registry_epoch: str = ""
    index_epoch: str = ""
    cache_epoch: str = ""
    start_at: datetime | None = None
    end_at: datetime | None = None

    def __post_init__(self) -> None:
        if any(type(value) is not int or value <= 0 for value in (
            self.site_id, self.root_page_id, self.locale_id
        )) or not self.root_path:
            raise SearchRequestError("invalid_scope")
        if self.order_by not in ("", "date", "-date"):
            raise SearchRequestError("invalid_sort")
        for value in (self.start_at, self.end_at):
            if value is not None and (not isinstance(value, datetime) or value.utcoffset() is None):
                raise SearchRequestError("invalid_date_range")

    def as_dict(self) -> dict[str, Any]:
        values = asdict(self)
        values["visibility"] = "public"
        for name in ("start_at", "end_at"):
            values[name] = values[name].isoformat() if values[name] else None
        return values

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(
            self.as_dict(), sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()


def parse_date_range(
    start: str | None, end: str | None, timezone_name: str
) -> tuple[datetime | None, datetime | None]:
    """日期用站点配置时区解释；结束日使用次日开区间，不丢当日尾部。"""
    try:
        start_day = date.fromisoformat(start) if start else None
        end_day = date.fromisoformat(end) if end else None
        if start_day and end_day and start_day > end_day:
            raise ValueError
        zone = ZoneInfo(timezone_name)
        def boundary(day: date | None) -> datetime | None:
            return datetime.combine(day, time.min, zone).astimezone(timezone.utc) if day else None
        return boundary(start_day), boundary(end_day + timedelta(days=1) if end_day else None)
    except (TypeError, ValueError, OverflowError):
        raise SearchRequestError("invalid_date_range") from None


def scope_from_request(request: Any, registry: Any, *, index_epoch: str, cache_epoch: str) -> SearchScope:
    """Site 必须来自当前 Host 的 Wagtail 解析；客户端 site 只可断言，不能选站。"""
    params = request.GET
    search_type = params.get("type") or "all"
    if search_type not in dict(registry.categories):
        raise SearchRequestError("invalid_search_type")
    try:
        site = Site.find_for_request(request)
        if site is None:
            raise SearchSafetyUnavailable("site_unavailable")
        # Site/root 都重读，拒绝使用进程或请求中陈旧的树路径。
        site = Site.objects.get(pk=site.pk)
        root = Page.objects.get(pk=site.root_page_id)
        supplied_site = params.get("site")
        if supplied_site and str(site.pk) != str(supplied_site):
            raise SearchRequestError("invalid_scope")
        supplied_locale = params.get("locale")
        if supplied_locale:
            locale = Locale.objects.get(pk=int(supplied_locale))
        else:
            locale = Locale.get_active()
        if locale is None:
            raise SearchRequestError("invalid_locale")
    except SearchRequestError:
        raise
    except (Locale.DoesNotExist, TypeError, ValueError):
        raise SearchRequestError("invalid_locale") from None
    except Exception:
        raise SearchSafetyUnavailable("scope_unavailable") from None
    start, end = parse_date_range(
        params.get("start_date"), params.get("end_date"), settings.TIME_ZONE
    )
    return SearchScope(
        site.pk, root.pk, root.path, locale.pk, search_type,
        params.get("order_by") or "", registry.epoch, index_epoch, cache_epoch,
        start, end,
    )


def public_queryset(scope: SearchScope) -> Any:
    """每次响应前重查 live/public；Wagtail public() 包含祖先访问限制。"""
    frozen = ExternalContentDeletionIntent.objects.exclude(status="cancelled").values("page_id")
    qs = Page.objects.live().public().filter(
        path__startswith=scope.root_path, locale_id=scope.locale_id
    ).exclude(pk__in=frozen)
    if scope.start_at:
        qs = qs.filter(first_published_at__gte=scope.start_at)
    if scope.end_at:
        qs = qs.filter(first_published_at__lt=scope.end_at)
    return qs
