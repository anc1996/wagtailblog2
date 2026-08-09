"""文章阅读参与度的受保护上报接口。"""

from __future__ import annotations

import json
import logging
from uuid import UUID

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import F
from django.http import Http404, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST
from wagtail.models import Site

from .models import ArticleEngagementSession, BlogPage, PageView, PageViewCount
from .page_view_counter import visitor_key_for_request

logger = logging.getLogger(__name__)


def _payload(request):
    if request.content_type.startswith("multipart/form-data"):
        return request.POST.dict()
    try:
        return json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _limited(request, page_id, visitor_key):
    """Redis不可用时宁可放行少量事件，也不能让阅读页面因统计失败不可用。"""

    key = f"blog-engagement-rate:{page_id}:{visitor_key}"
    try:
        if cache.add(key, 1, timeout=60):
            return False
        return cache.incr(key) > 12
    except Exception:
        logger.warning("engagement_rate_limit_failed", exc_info=True)
        return False


@require_POST
@csrf_protect
def record_engagement(request):
    """接收绝对阅读状态，以会话序号防止Beacon重试或乱序造成重复计数。"""

    data = _payload(request)
    if not isinstance(data, dict):
        return JsonResponse({"detail": "请求格式无效。"}, status=400)
    try:
        page_id = int(data["page_id"])
        session_id = UUID(str(data["session_id"]))
        sequence = int(data["sequence"])
        active_seconds = int(data["active_reading_seconds"])
        max_scroll = int(data["max_scroll_percent"])
        client_engaged = data["engaged"] is True or str(data["engaged"]).lower() == "true"
    except (KeyError, TypeError, ValueError):
        return JsonResponse({"detail": "阅读状态字段无效。"}, status=400)
    if sequence < 1 or not 0 <= max_scroll <= 100 or not 0 <= active_seconds <= 1800:
        return JsonResponse({"detail": "阅读状态超出允许范围。"}, status=400)
    # 参与度由服务端时间门槛约束，客户端布尔值只能表示其已观察到状态，不能单独触发计数。
    engaged = client_engaged and active_seconds >= 10

    page = BlogPage.objects.live().public().filter(pk=page_id).first()
    if page is None or page.get_site() != Site.find_for_request(request):
        raise Http404
    visit_date = timezone.localdate()
    visitor_key = visitor_key_for_request(request, visit_date)
    if _limited(request, page_id, visitor_key):
        return JsonResponse({"accepted": True, "limited": True}, status=202)

    try:
        with transaction.atomic():
            page_view = PageView.objects.select_for_update().filter(
                page=page, date=visit_date, visitor_key=visitor_key
            ).first()
            # 阅读事件没有对应的成功页面访问审计记录时不创建新访客，避免接口被直接刷量。
            if page_view is None:
                return JsonResponse({"accepted": False}, status=202)

            try:
                with transaction.atomic():
                    session, _ = ArticleEngagementSession.objects.get_or_create(
                        page=page,
                        session_id=session_id,
                        defaults={"date": visit_date, "visitor_key": visitor_key},
                    )
            except IntegrityError:
                session = ArticleEngagementSession.objects.get(page=page, session_id=session_id)
            session = ArticleEngagementSession.objects.select_for_update().get(pk=session.pk)
            if session.visitor_key != visitor_key or session.date != visit_date:
                return JsonResponse({"detail": "阅读会话与当前访客不匹配。"}, status=409)
            if sequence <= session.sequence:
                return JsonResponse({"accepted": True, "duplicate": True})

            previous_seconds = session.active_reading_seconds
            previous_scroll = session.max_scroll_percent
            was_engaged = session.engaged
            session.sequence = sequence
            session.engaged = session.engaged or engaged
            session.max_scroll_percent = max(previous_scroll, max_scroll)
            session.active_reading_seconds = max(previous_seconds, active_seconds)
            session.save(update_fields=[
                "sequence", "engaged", "max_scroll_percent", "active_reading_seconds", "updated_at"
            ])

            try:
                with transaction.atomic():
                    aggregate, _ = PageViewCount.objects.get_or_create(
                        page=page, date=visit_date
                    )
            except IntegrityError:
                aggregate = PageViewCount.objects.get(page=page, date=visit_date)
            updates = {}
            page_view_updates = {}
            seconds_delta = max(0, active_seconds - previous_seconds)
            if seconds_delta:
                updates["active_reading_seconds"] = F("active_reading_seconds") + seconds_delta
                page_view_updates["active_reading_seconds"] = F("active_reading_seconds") + seconds_delta
            if engaged and not was_engaged and not page_view.engaged:
                updates["engaged_visitor_count"] = F("engaged_visitor_count") + 1
                page_view_updates["engaged"] = True
            if active_seconds >= 15 and max_scroll >= 50 and not page_view.scroll_50_reached:
                updates["scroll_50_visitor_count"] = F("scroll_50_visitor_count") + 1
                page_view_updates["scroll_50_reached"] = True
            if active_seconds >= 30 and max_scroll >= 90 and not page_view.scroll_90_reached:
                updates["scroll_90_visitor_count"] = F("scroll_90_visitor_count") + 1
                page_view_updates["scroll_90_reached"] = True
            if max_scroll > page_view.max_scroll_percent:
                page_view_updates["max_scroll_percent"] = max_scroll
            if updates:
                PageViewCount.objects.filter(pk=aggregate.pk).update(**updates)
            if page_view_updates:
                PageView.objects.filter(pk=page_view.pk).update(**page_view_updates)
    except Exception:
        logger.warning("engagement_record_failed page_id=%s", page_id, exc_info=True)
        return JsonResponse({"accepted": False}, status=202)
    return JsonResponse({"accepted": True})
