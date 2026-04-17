# blog/signals.py
from django.db.models.signals import pre_delete, post_save, pre_save
from django.dispatch import receiver
import logging

from .models import BlogPage
from wagtailblog3.mongo import MongoManager

logger = logging.getLogger(__name__)

@receiver(pre_delete, sender=BlogPage)
def delete_blog_content_from_mongodb(sender, instance, **kwargs):
    """当BlogPage被删除时，同步删除MongoDB中的内容"""
    if instance.mongo_content_id:
        try:
            mongo_manager = MongoManager()
            result = mongo_manager.delete_blog_content(instance.mongo_content_id)
            if not result:
                logger.warning(f"删除MongoDB内容失败: ID={instance.mongo_content_id}")
        except Exception as e:
            import traceback
            logger.error(f"删除MongoDB内容时出错: {e}")
            logger.error(traceback.format_exc())


@receiver(post_save, sender=BlogPage)
def clear_body_after_save(sender, instance, **kwargs):
    """保存后清空body，防止重复存入MySQL"""
    # 此功能已经在save方法中实现，这里可以作为备份确保
    if instance.body and instance.body.raw_data:  # 检查是否有内容
        sender.objects.filter(pk=instance.pk).update(body=[])

