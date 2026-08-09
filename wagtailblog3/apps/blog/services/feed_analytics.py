"""RSS/Atom 请求的隐私最小化聚合统计。"""

from __future__ import annotations

import logging

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from django.utils.crypto import salted_hmac

from .feed_cache import BlogFeedScope

logger = logging.getLogger(__name__)


class FeedRequestRecorder:
    """只记录规范Feed的200/304响应，客户端数始终是估算值。"""

    @staticmethod
    def _client_key(request, visit_date) -> str:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        ip_address = forwarded.split(",", 1)[0].strip() if forwarded else request.META.get("REMOTE_ADDR", "")
        value = f"{visit_date.isoformat()}:{ip_address}:{request.META.get('HTTP_USER_AGENT', '')}"
        return salted_hmac("blog.feed.client", value, algorithm="sha256").hexdigest()

    @classmethod
    def record(cls, request, response, scope: BlogFeedScope, feed_format: str) -> None:
        """缓存命中和未命中都在最终条件响应之后记录，避免把301/404计为兴趣。"""

        if request.method != "GET" or response.status_code not in {200, 304}:
            return
        try:
            from blog.models import FeedClientDaily, FeedRequestDaily

            visit_date = timezone.localdate()
            with transaction.atomic():
                daily, _ = FeedRequestDaily.objects.get_or_create(
                    site_id=scope.site_id,
                    locale_id=scope.locale_id,
                    date=visit_date,
                    scope_type=scope.scope_type,
                    scope_id=scope.scope_id,
                    feed_format=feed_format,
                    defaults={"scope_slug": scope.scope_slug, "scope_label": scope.scope_label},
                )
                update = {
                    "response_200_count" if response.status_code == 200 else "response_304_count": F(
                        "response_200_count" if response.status_code == 200 else "response_304_count"
                    ) + 1
                }
                try:
                    with transaction.atomic():
                        _, created = FeedClientDaily.objects.get_or_create(
                            site_id=scope.site_id,
                            locale_id=scope.locale_id,
                            date=visit_date,
                            scope_type=scope.scope_type,
                            scope_id=scope.scope_id,
                            feed_format=feed_format,
                            client_key=cls._client_key(request, visit_date),
                        )
                except IntegrityError:
                    created = False
                if created:
                    update["estimated_client_count"] = F("estimated_client_count") + 1
                FeedRequestDaily.objects.filter(pk=daily.pk).update(**update)
        except Exception:
            logger.warning("feed_request_record_failed format=%s", feed_format, exc_info=True)
