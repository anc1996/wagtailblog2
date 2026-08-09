"""博客文章访问统计服务。"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from django.apps import apps
from django.db import IntegrityError, transaction
from django.db.models import F, Sum
from django.utils import timezone
from django.utils.crypto import salted_hmac

logger = logging.getLogger(__name__)


def get_client_ip(request) -> str:
    """在受信任反向代理已规范转发头部的前提下取得客户端地址。"""

    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.META.get("REMOTE_ADDR", "0.0.0.0")


def visitor_key_for_request(request, visit_date=None) -> str:
    """生成按日轮换的 HMAC 摘要，避免在分析表保存长期可关联标识。"""

    visit_date = visit_date or timezone.localdate()
    if getattr(request, "user", None) and request.user.is_authenticated:
        value = f"user:{request.user.pk}:{visit_date.isoformat()}"
    else:
        value = ":".join(
            (
                "anonymous",
                get_client_ip(request),
                request.META.get("HTTP_USER_AGENT", ""),
                visit_date.isoformat(),
            )
        )
    return salted_hmac("blog.analytics.visitor", value, algorithm="sha256").hexdigest()


def source_for_request(request) -> tuple[str, str]:
    """仅保留来源域名与有限分类，避免把路径和查询参数写入审计数据。"""

    referrer = request.META.get("HTTP_REFERER", "")
    host = (urlparse(referrer).hostname or "").lower()
    if not host:
        return "direct", ""
    request_host = request.get_host().split(":", 1)[0].lower()
    if host == request_host:
        return "internal", host
    if any(name in host for name in ("google.", "bing.", "baidu.", "sogou.", "yahoo.")):
        return "search", host
    if any(name in host for name in ("github.", "zhihu.", "bilibili.", "facebook.", "twitter.", "x.com", "linkedin.")):
        return "social", host
    return "referral", host


class PageViewCounter:
    """记录公开文章的 V2 访问量，并提供模板所需的聚合统计。"""

    def __init__(self, page_id: int):
        self.page_id = page_id
        self.today = timezone.localdate()

    @staticmethod
    def _model(name: str):
        return apps.get_model("blog", name)

    def record(self, request) -> bool:
        """在成功响应之后写入；分析故障只能降级为日志，绝不能中断读者访问。"""

        PageView = self._model("PageView")
        PageViewCount = self._model("PageViewCount")
        Traffic = self._model("PageTrafficSourceDaily")
        now = timezone.now()
        key = visitor_key_for_request(request, self.today)
        source_category, referrer_host = source_for_request(request)
        user = request.user if request.user.is_authenticated else None

        try:
            with transaction.atomic():
                try:
                    with transaction.atomic():
                        page_view, created = PageView.objects.get_or_create(
                            page_id=self.page_id,
                            date=self.today,
                            visitor_key=key,
                            defaults={
                                "user": user,
                                "ip_address": get_client_ip(request),
                                "user_agent": request.META.get("HTTP_USER_AGENT", "")[:255],
                                "view_count": 1,
                                "first_viewed_at": now,
                                "last_viewed_at": now,
                                "source_category": source_category,
                                "referrer_host": referrer_host,
                            },
                        )
                except IntegrityError:
                    # 唯一约束处理两个并发首访，第二个事务读取已写入的审计行。
                    page_view = PageView.objects.get(
                        page_id=self.page_id, date=self.today, visitor_key=key
                    )
                    created = False

                if not created:
                    PageView.objects.filter(pk=page_view.pk).update(
                        view_count=F("view_count") + 1,
                        last_viewed_at=now,
                    )

                try:
                    with transaction.atomic():
                        aggregate, _ = PageViewCount.objects.get_or_create(
                            page_id=self.page_id,
                            date=self.today,
                            defaults={"v2_started_at": now},
                        )
                except IntegrityError:
                    aggregate = PageViewCount.objects.get(
                        page_id=self.page_id, date=self.today
                    )
                update = {"view_count_v2": F("view_count_v2") + 1}
                if created:
                    update["unique_visitor_count_v2"] = F("unique_visitor_count_v2") + 1
                PageViewCount.objects.filter(pk=aggregate.pk).update(**update)

                try:
                    with transaction.atomic():
                        traffic, _ = Traffic.objects.get_or_create(
                            page_id=self.page_id,
                            date=self.today,
                            source_category=source_category,
                        )
                except IntegrityError:
                    traffic = Traffic.objects.get(
                        page_id=self.page_id,
                        date=self.today,
                        source_category=source_category,
                    )
                traffic_update = {"view_count": F("view_count") + 1}
                if created:
                    traffic_update["unique_visitor_count"] = F("unique_visitor_count") + 1
                Traffic.objects.filter(pk=traffic.pk).update(**traffic_update)
            return created
        except Exception:
            logger.warning("page_view_record_failed page_id=%s", self.page_id, exc_info=True)
            return False

    def get(self) -> dict:
        """仅查询每日聚合；返回新旧口径的分离值，调用方不得相加。"""

        try:
            PageViewCount = self._model("PageViewCount")
            rows = PageViewCount.objects.filter(page_id=self.page_id)
            today = rows.filter(date=self.today).values(
                "view_count_v2", "unique_visitor_count_v2", "count", "unique_count"
            ).first() or {}
            totals = rows.aggregate(
                total=Sum("view_count_v2"),
                total_unique=Sum("unique_visitor_count_v2"),
                legacy_total=Sum("count"),
                legacy_total_unique=Sum("unique_count"),
            )
            return {
                "today": today.get("view_count_v2", 0),
                "today_unique": today.get("unique_visitor_count_v2", 0),
                "total": totals["total"] or 0,
                "total_unique": totals["total_unique"] or 0,
                "legacy_today": today.get("count", 0),
                "legacy_today_unique": today.get("unique_count", 0),
                "legacy_total": totals["legacy_total"] or 0,
                "legacy_total_unique": totals["legacy_total_unique"] or 0,
            }
        except Exception:
            logger.warning("page_view_stats_failed page_id=%s", self.page_id, exc_info=True)
            return {
                "today": 0, "today_unique": 0, "total": 0, "total_unique": 0,
                "legacy_today": 0, "legacy_today_unique": 0,
                "legacy_total": 0, "legacy_total_unique": 0,
            }
