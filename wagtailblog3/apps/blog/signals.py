# blog/signals.py
import json
import logging
from django.db.models.signals import pre_delete, post_save
from django.dispatch import receiver
from wagtail.models import Revision

from .models import BlogPage
from wagtailblog3.mongo import MongoManager

logger = logging.getLogger(__name__)


# =============================================================================
# 天网防线 1：当整篇博客被彻底删除（或者批量勾选删除）时触发
# =============================================================================
@receiver(pre_delete, sender=BlogPage)
def delete_blog_content_from_mongodb(sender, instance, **kwargs):
	"""当 BlogPage 实体被删除时，级联擦除 MongoDB 线上主集合与全量历史快照"""
	page_id = instance.pk
	mongo_content_id = instance.mongo_content_id
	
	try:
		mongo_manager = MongoManager()
		
		# 1. 精准删除线上供普通用户访问的正式内容记录
		if mongo_content_id:
			result_content = mongo_manager.delete_blog_content(mongo_content_id)
			if not result_content:
				logger.warning(f"信号防线提示：尝试删除正式 MongoDB 内容失败，ID: {mongo_content_id}")
		
		# 2. 【核心升级】：调用封装好的经理类，批量擦除该页面积攒的所有历史草稿大文本
		if page_id:
			deleted_snapshots = mongo_manager.delete_page_revisions(page_id)
			if deleted_snapshots > 0:
				logger.info(
					f"天网防线成功拦截：跟随页面物理销毁，同步从 MongoDB 中连坐擦除了该页面的 {deleted_snapshots} 条草稿快照。")
	
	except Exception as e:
		import traceback
		logger.error(f"信号清理异构集群残留遭遇致命异常: {e}, {traceback.format_exc()}")


# =============================================================================
# 天网防线 2：当用户在后台手动单独删除“某一条历史草稿”时触发
# =============================================================================
@receiver(pre_delete, sender=Revision)
def cascade_delete_single_mongo_revision(sender, instance, **kwargs):
	"""当 Wagtail 单条 Revision 记录被抹除时，通过嵌入的暗号，联动引爆 MongoDB 里的单体大文本"""
	try:
		content = instance.content
		if isinstance(content, str):
			content = json.loads(content)
		
		# 提取我们在序列化阶段（serializable_data）精心埋藏的草稿外键探针
		pointer = content.get('mongo_draft_pointer')
		
		if pointer:
			mongo_manager = MongoManager()
			# 优雅调用：清剿单条记录
			success = mongo_manager.delete_single_revision(pointer)
			if success:
				logger.info(f"单体拦截成功：跟随 MySQL 历史行，同步清剿了 MongoDB 的历史快照实体 [{pointer}]")
	
	except Exception as e:
		logger.error(f"信号防线清理单体 Revision 失败: {e}")


# =============================================================================
# 辅助保护防线
# =============================================================================
@receiver(post_save, sender=BlogPage)
def clear_body_after_save(sender, instance, **kwargs):
	"""强制卡死，防止任何意外导致的重复内容写入 MySQL 实体表"""
	if instance.body and hasattr(instance.body, 'raw_data') and instance.body.raw_data:
		sender.objects.filter(pk=instance.pk).update(body=[])