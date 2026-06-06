# blog/page_view_counter.py
"""
页面访问计数服务 —— 纯 MySQL 版本。

PageView      : 审计日志，记录"谁、哪天、从哪个IP访问了哪篇文章"。
                每个 (page, date, ip, user) 组合保存一条，
                当天重复访问只更新 last_viewed_at，不新增行。
                表可能很大，【绝不用于聚合展示】。

PageViewCount : 按天聚合的小表，每次真正新增 PageView 时同步 +1。
                【所有展示统计都从这里读，永远快】。
"""
import logging
from datetime import date

from django.utils import timezone

logger = logging.getLogger(__name__)


def _get_client_ip(request) -> str:
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '0.0.0.0')


@staticmethod
def _get_model(model_name: str):
    from django.apps import apps
    return apps.get_model('blog', model_name)


class PageViewCounter:
    """
    用法：
        counter = PageViewCounter(page_id)
        counter.record(request)   # 在 serve() 里调用
        stats   = counter.get()   # 在模板里调用
    """

    def __init__(self, page_id: int):
        self.page_id = page_id
        self.today   = date.today()

    @staticmethod
    def _get_model(model_name: str):
        from django.apps import apps
        return apps.get_model('blog', model_name)

    def record(self, request) -> bool:
        """
        记录一次访问。
        - 当天该 (page, date, ip, user) 首次访问 → 写 PageView + PageViewCount +1
        - 当天重复访问 → 只更新 PageView.last_viewed_at，PageViewCount 不变
        返回 True = 新访客已计数；False = 今天已访问过。
        """
        ip   = _get_client_ip(request)
        user = request.user if request.user.is_authenticated else None
        now  = timezone.now()

        try:
            PageView      = self._get_model('PageView')
            PageViewCount = self._get_model('PageViewCount')

            lookup = {
                'page_id':    self.page_id,
                'date':       self.today,
                'ip_address': ip,
                'user':       user,
            }

            existing = PageView.objects.filter(**lookup).first()

            if existing:
                # 今天已访问过，只刷新时间戳
                PageView.objects.filter(pk=existing.pk).update(last_viewed_at=now)
                return False

            # 首次访问：写审计日志
            PageView.objects.create(
                page_id       = self.page_id,
                date          = self.today,
                user          = user,
                ip_address    = ip,
                user_agent    = request.META.get('HTTP_USER_AGENT', ''),
                last_viewed_at = now,
            )
            # 同步更新聚合表
            self._increment_count(PageViewCount)
            return True

        except Exception as e:
            logger.error(f"[PageViewCounter] record 失败 page={self.page_id}: {e}")
            return False

    def _increment_count(self, PageViewCount):
        """新增 PageView 时，同步更新 PageViewCount 当天聚合行。"""
        from django.db.models import F
        try:
            obj, created = PageViewCount.objects.get_or_create(
                page_id  = self.page_id,
                date     = self.today,
                defaults = {'count': 1, 'unique_count': 1}
            )
            if not created:
                # F() 表达式在数据库层原子累加，避免并发竞态
                PageViewCount.objects.filter(pk=obj.pk).update(
                    count        = F('count') + 1,
                    unique_count = F('unique_count') + 1,
                )
        except Exception as e:
            logger.error(f"[PageViewCounter] PageViewCount 更新失败 page={self.page_id}: {e}")

    def get(self) -> dict:
        """
        读取访问统计。
        【只读 PageViewCount，永远不碰 PageView 大表】
        PageViewCount 每天每篇文章只有 1 行，无论 PageView 有多少条都不影响速度。
        """
        try:
            from django.db.models import Sum
            PageViewCount = self._get_model('PageViewCount')

            # 今日统计：直接读今天那一行的 count 字段
            today_row = PageViewCount.objects.filter(
                page_id = self.page_id,
                date    = self.today,
            ).values('count', 'unique_count').first()

            today_count        = today_row['count']        if today_row else 0
            today_unique_count = today_row['unique_count'] if today_row else 0

            # 历史总计：对 PageViewCount 按 page 聚合，行数 = 文章天数，几百行而已
            totals = PageViewCount.objects.filter(
                page_id = self.page_id,
            ).aggregate(
                total        = Sum('count'),
                total_unique = Sum('unique_count'),
            )

            return {
                'today':        today_count,
                'today_unique': today_unique_count,
                'total':        totals['total']        or 0,
                'total_unique': totals['total_unique'] or 0,
            }

        except Exception as e:
            logger.error(f"[PageViewCounter] get 失败 page={self.page_id}: {e}")
            return {'today': 0, 'today_unique': 0, 'total': 0, 'total_unique': 0}