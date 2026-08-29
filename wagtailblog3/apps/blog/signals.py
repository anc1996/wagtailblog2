# 博客正文与 Revision 的清理信号
import json
import logging
from django.db import transaction
from django.db.models.signals import pre_delete, post_save
from django.dispatch import receiver
from taggit.models import Tag
from wagtail.models import PageViewRestriction, Revision, Site
from wagtail.signals import page_published, page_unpublished

from .models import Author, BlogCategory, BlogPage, MongoCleanupIntent
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
	_record_page_cleanup_intents(instance)


# =============================================================================
# 天网防线 2：当用户在后台手动单独删除“某一条历史草稿”时触发
# =============================================================================
@receiver(
	pre_delete,
	sender=Revision,
	dispatch_uid="blog.cascade_delete_single_mongo_revision",
)
def cascade_delete_single_mongo_revision(sender, instance, **kwargs):
	_record_revision_cleanup_intent(instance)


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
	# 缓存只能在权限变更提交后清理，避免回滚或并发读取重新写入旧公开结果。
	def clear_public_search_cache():
		try:
			from search.cache import SearchCache
			SearchCache.clear_search_cache()
		except Exception as error:
			logger.error(f"访问限制变更后的搜索缓存清理失败: {error}", exc_info=True)

	transaction.on_commit(clear_public_search_cache)


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


def _schedule_mongo_cleanup(intent_id):
	"""事务提交后将 Mongo 清理意图投递到 maintenance 队列。"""
	from .tasks import cleanup_mongo_intent
	def dispatch() -> None:
		try:
			cleanup_mongo_intent.apply_async(args=(str(intent_id),), queue="maintenance")
		except Exception:
			# 意图已经随事务提交，队列唤醒失败只能等待补偿扫描，不能反向影响删除结果。
			logger.exception("blog_mongo_cleanup_dispatch_failed intent_id=%s", intent_id)
	transaction.on_commit(dispatch)


def _record_page_cleanup_intents(instance):
	"""页面删除阶段仅记录正式正文；Revision 信号负责每个历史快照。"""
	entries = []
	if instance.mongo_content_id:
		entries.append(("formal", str(instance.mongo_content_id), f"formal:{instance.mongo_content_id}"))
	for kind, pointer, dedupe_key in entries:
		intent, _ = MongoCleanupIntent.objects.get_or_create(
			dedupe_key=dedupe_key,
			defaults={"page_id": instance.pk, "pointer": pointer, "kind": kind},
		)
		_schedule_mongo_cleanup(intent.intent_id)


def _record_revision_cleanup_intent(instance):
	"""单 Revision 删除记录幂等意图，任务执行前再检查共享引用。"""
	try:
		content = json.loads(instance.content) if isinstance(instance.content, str) else instance.content
	except (TypeError, ValueError, json.JSONDecodeError):
		return
	pointer = content.get("mongo_draft_pointer") if isinstance(content, dict) else None
	if not pointer:
		return
	intent, _ = MongoCleanupIntent.objects.get_or_create(
		dedupe_key=f"revision:{pointer}",
		defaults={"page_id": int(instance.object_id), "pointer": str(pointer), "kind": "revision"},
	)
	_schedule_mongo_cleanup(intent.intent_id)
