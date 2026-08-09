# 博客正文与 Revision 的清理信号
import json
import logging
from django.db.models.signals import pre_delete, post_save
from django.dispatch import receiver
from taggit.models import Tag
from wagtail.models import PageViewRestriction, Revision, Site
from wagtail.signals import page_published, page_unpublished

from .models import Author, BlogCategory, BlogPage
from .services.feed_cache import BlogFeedInvalidationService
from wagtailblog3.mongo import MongoManager

logger = logging.getLogger(__name__)


# =============================================================================
# 天网防线 1：当整篇博客被彻底删除（或者批量勾选删除）时触发
# =============================================================================
@receiver(
	pre_delete,
	sender=BlogPage,
	dispatch_uid="blog.delete_blog_content_from_mongodb",
)
def delete_blog_content_from_mongodb(sender, instance, **kwargs):
	"""当 BlogPage 实体被删除时，级联擦除 MongoDB 线上主集合与全量历史快照"""
	page_id = instance.pk
	mongo_content_id = instance.mongo_content_id
	
	try:
		mongo_manager = MongoManager()
		
		# 先删除正式内容，再删除该页面所有草稿快照，避免页面删除后留下孤儿正文。
		if mongo_content_id:
			result_content = mongo_manager.delete_blog_content(mongo_content_id)
			if not result_content:
				logger.warning(f"信号防线提示：尝试删除正式 MongoDB 内容失败，ID: {mongo_content_id}")
		
		# 草稿集合按 page_id 批量清理，不需要从每条 Revision 再次解析正文。
		if page_id:
			deleted_snapshots = mongo_manager.delete_page_revisions(page_id)
			if deleted_snapshots > 0:
				logger.info(
					f"天网防线成功拦截：跟随页面物理销毁，同步从 MongoDB 中连坐擦除了该页面的 {deleted_snapshots} 条草稿快照。")
	
	except Exception as e:
		logger.error(f"信号清理异构集群残留遭遇致命异常: {e}", exc_info=True)


# =============================================================================
# 天网防线 2：当用户在后台手动单独删除“某一条历史草稿”时触发
# =============================================================================
@receiver(
	pre_delete,
	sender=Revision,
	dispatch_uid="blog.cascade_delete_single_mongo_revision",
)
def cascade_delete_single_mongo_revision(sender, instance, **kwargs):
	"""当 Wagtail 单条 Revision 记录被抹除时，通过嵌入的暗号，联动引爆 MongoDB 里的单体大文本"""
	try:
		content = instance.content
		if isinstance(content, str):
			content = json.loads(content)
		
		# 读取 serializable_data 写入 Revision 的 Mongo 指针，定位对应草稿快照。
		pointer = content.get('mongo_draft_pointer')
		
		if pointer:
			mongo_manager = MongoManager()
			# 优雅调用：清剿单条记录
			success = mongo_manager.delete_single_revision(pointer)
			if success:
				logger.info(f"单体拦截成功：跟随 MySQL 历史行，同步清剿了 MongoDB 的历史快照实体 [{pointer}]")
	
	except Exception as e:
		logger.error(f"信号防线清理单体 Revision 失败: {e}", exc_info=True)


# =============================================================================
# 辅助保护防线
# =============================================================================
@receiver(
	post_save,
	sender=BlogPage,
	dispatch_uid="blog.clear_body_after_save",
)
def clear_body_after_save(sender, instance, **kwargs):
	"""强制卡死，防止任何意外导致的重复内容写入 MySQL 实体表"""
	if instance.body and hasattr(instance.body, 'raw_data') and instance.body.raw_data:
		sender.objects.filter(pk=instance.pk).update(body=[])


# Feed缓存只反映公开内容：保存草稿不会触发，真正发布、取消发布或删除才刷新。
@receiver(
	page_published,
	sender=BlogPage,
	dispatch_uid="blog.invalidate_feed_on_page_published",
)
def invalidate_feed_on_page_published(sender, instance, **kwargs):
	"""文章首次发布或重新发布后刷新对应站点和语言的订阅源。"""
	BlogFeedInvalidationService.schedule_scope(
		BlogFeedInvalidationService.scope_for_page(instance)
	)


@receiver(
	page_unpublished,
	sender=BlogPage,
	dispatch_uid="blog.invalidate_feed_on_page_unpublished",
)
def invalidate_feed_on_page_unpublished(sender, instance, **kwargs):
	"""取消发布后使文章立即从下一次Feed查询中消失。"""
	BlogFeedInvalidationService.schedule_scope(
		BlogFeedInvalidationService.scope_for_page(instance)
	)


@receiver(
	pre_delete,
	sender=BlogPage,
	dispatch_uid="blog.invalidate_feed_on_page_deleted",
)
def invalidate_feed_on_page_deleted(sender, instance, **kwargs):
	"""删除不触发page_unpublished，必须在页面树仍存在时保存失效范围。"""
	BlogFeedInvalidationService.schedule_scope(
		BlogFeedInvalidationService.scope_for_page(instance)
	)


@receiver(
	post_save,
	sender=Author,
	dispatch_uid="blog.invalidate_feed_on_author_saved",
)
@receiver(
	pre_delete,
	sender=Author,
	dispatch_uid="blog.invalidate_feed_on_author_deleted",
)
@receiver(
	post_save,
	sender=BlogCategory,
	dispatch_uid="blog.invalidate_feed_on_category_saved",
)
@receiver(
	pre_delete,
	sender=BlogCategory,
	dispatch_uid="blog.invalidate_feed_on_category_deleted",
)
@receiver(
	post_save,
	sender=Tag,
	dispatch_uid="blog.invalidate_feed_on_tag_saved",
)
@receiver(
	pre_delete,
	sender=Tag,
	dispatch_uid="blog.invalidate_feed_on_tag_deleted",
)
def invalidate_feed_on_related_content_changed(sender, instance, **kwargs):
	"""作者、分类和标签可影响多篇文章，保守刷新全部Feed范围。"""
	BlogFeedInvalidationService.schedule_all()


@receiver(
	post_save,
	sender=PageViewRestriction,
	dispatch_uid="blog.invalidate_feed_on_restriction_saved",
)
@receiver(
	pre_delete,
	sender=PageViewRestriction,
	dispatch_uid="blog.invalidate_feed_on_restriction_deleted",
)
def invalidate_feed_on_restriction_changed(sender, instance, **kwargs):
	"""访问限制会改变public()结果，无法安全精确反查时刷新全部范围。"""
	BlogFeedInvalidationService.schedule_all()


@receiver(
	post_save,
	sender=Site,
	dispatch_uid="blog.invalidate_feed_on_site_saved",
)
@receiver(
	pre_delete,
	sender=Site,
	dispatch_uid="blog.invalidate_feed_on_site_deleted",
)
def invalidate_feed_on_site_changed(sender, instance, **kwargs):
	"""站点域名或根页面变更后，刷新该站点的所有语言Feed。"""
	BlogFeedInvalidationService.schedule_site(instance.pk)
