"""博客分析的 Celery 维护任务。"""

from celery import shared_task
from django.conf import settings
from django.core.management import call_command


@shared_task(name="blog.tasks.cleanup_analytics_details")
def cleanup_analytics_details():
    """按保留期清理短期明细；默认关闭，生产启用前必须取得数据清理授权。"""

    if not getattr(settings, "BLOG_ANALYTICS_CLEANUP_ENABLED", False):
        return {"status": "disabled"}
    call_command(
        "cleanup_pageviews",
        confirm=True,
        days=getattr(settings, "BLOG_PAGEVIEW_RETENTION_DAYS", 30),
        batch_size=getattr(settings, "BLOG_ANALYTICS_CLEANUP_BATCH_SIZE", 500),
    )
    return {"status": "completed"}
