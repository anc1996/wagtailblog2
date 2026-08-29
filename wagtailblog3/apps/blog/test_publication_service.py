"""M4.1 发布候选锁定与 Mongo 正文版本校验测试。"""

import json

from django.db import transaction
from django.test import TestCase
from unittest.mock import patch
from django.utils import timezone
from datetime import timedelta

from blog.models import BlogPage, BlogPublicationState
from blog.services.publication import (
	BlogPublicationService,
	PublicationBodyUnavailableError,
	PublicationRevisionInvalidError,
)
from search.tests.test_lifecycle_baseline import BlogLifecycleFixtureMixin


class BlogPublicationServiceTests(BlogLifecycleFixtureMixin, TestCase):
	"""验证发布候选只保存已校验的 Mongo 版本元数据。"""

	def _revision(self, text="待发布正文"):
		page = self._create_draft_page(text)
		return page, page.save_revision()

	def test_missing_body_version_metadata_is_rejected(self):
		page, revision = self._revision()
		revision.content.pop("mongo_body_version_id")
		revision.save(update_fields=["content"])

		with self.assertRaises(PublicationRevisionInvalidError):
			BlogPublicationService.lock_and_validate_revision(page.pk, revision.pk)
		self.assertFalse(BlogPublicationState.objects.filter(page_id=page.pk).exists())

	def test_missing_mongo_body_version_is_rejected_without_state_write(self):
		page, revision = self._revision()
		self.mongo.body_versions.clear()

		with self.assertRaises(PublicationBodyUnavailableError):
			BlogPublicationService.lock_and_validate_revision(page.pk, revision.pk)
		self.assertFalse(BlogPublicationState.objects.filter(page_id=page.pk).exists())

	def test_success_persists_draft_pointer_and_approved_revision_metadata(self):
		page, revision = self._revision()

		candidate = BlogPublicationService.lock_and_validate_revision(
			page.pk,
			revision.pk,
			approved_revision_id=revision.pk,
		)

		self.assertEqual(candidate.page.pk, page.pk)
		self.assertEqual(candidate.revision.pk, revision.pk)
		self.assertEqual(candidate.body_document["body"], self.mongo.body_versions[next(iter(self.mongo.body_versions))]["body"])
		state = BlogPublicationState.objects.get(page_id=page.pk)
		self.assertEqual(state.draft_body_version_id, revision.content["mongo_body_version_id"])
		self.assertEqual(state.draft_body_sha256, revision.content["body_sha256"])
		self.assertEqual(state.approved_revision_id, revision.pk)
		self.assertEqual(state.publication_generation, 0)

	def test_outer_transaction_rollback_removes_state_update(self):
		page, revision = self._revision()

		with self.assertRaises(RuntimeError):
			with transaction.atomic():
				BlogPublicationService.lock_and_validate_revision(page.pk, revision.pk)
				raise RuntimeError("force rollback")

		self.assertFalse(BlogPublicationState.objects.filter(page_id=page.pk).exists())

	def test_blog_page_publish_validates_and_publishes_successfully(self):
		page, revision = self._revision("可发布正文")

		page.publish(revision)
		page.refresh_from_db()

		self.assertTrue(page.live)
		self.assertEqual(page.live_revision_id, revision.pk)
		state = BlogPublicationState.objects.get(page_id=page.pk)
		self.assertEqual(state.draft_body_version_id, revision.content["mongo_body_version_id"])

	def test_blog_page_publish_rejects_missing_mongo_body_before_wagtail_publish(self):
		page, revision = self._revision("Mongo 缺失正文")
		body_version_id = revision.content["mongo_body_version_id"]
		self.mongo.body_versions.clear()

		with self.assertRaises(PublicationBodyUnavailableError):
			page.publish(revision)

		page.refresh_from_db()
		revision.refresh_from_db()
		self.assertFalse(page.live)
		self.assertIsNone(page.live_revision_id)
		self.assertFalse(BlogPublicationState.objects.filter(page_id=page.pk).exists())
		self.assertEqual(revision.content["mongo_body_version_id"], body_version_id)

	def test_blog_page_publish_rejects_damaged_version_metadata_before_wagtail_publish(self):
		page, revision = self._revision("损坏版本元数据")
		revision.content["body_sha256"] = "bad"
		revision.save(update_fields=["content"])

		with self.assertRaises(PublicationRevisionInvalidError):
			page.publish(revision)

		page.refresh_from_db()
		self.assertFalse(page.live)
		self.assertIsNone(page.live_revision_id)
		self.assertFalse(BlogPublicationState.objects.filter(page_id=page.pk).exists())

	def test_blog_page_publish_outer_transaction_rollback_restores_page_and_state(self):
		page, revision = self._revision("回滚发布正文")

		with self.assertRaises(RuntimeError):
			with transaction.atomic():
				page.publish(revision)
				raise RuntimeError("force rollback")

		page.refresh_from_db()
		self.assertFalse(page.live)
		self.assertIsNone(page.live_revision_id)
		self.assertFalse(BlogPublicationState.objects.filter(page_id=page.pk).exists())

	def test_scheduled_publish_uses_due_approved_revision_even_when_newer_draft_exists(self):
		page, scheduled_revision = self._revision("定时正文")
		scheduled_revision.approved_go_live_at = timezone.now() - timedelta(minutes=1)
		scheduled_revision.save(update_fields=["approved_go_live_at"])

		page.body = self._markdown_body("排期后的新草稿")
		page.save_revision()

		page.publish(scheduled_revision, log_action="wagtail.publish.scheduled")
		page.refresh_from_db()
		self.assertTrue(page.live)
		self.assertEqual(page.live_revision_id, scheduled_revision.pk)

	def test_future_schedule_with_string_revision_content_does_not_promote_state(self):
		"""Wagtail 8.0 JSONField 返回字符串时，未来排期不得提前切换正式正文指针。"""
		page, revision = self._revision("字符串排期正文")
		go_live_at = timezone.now() + timedelta(minutes=10)
		page.go_live_at = go_live_at
		page.save(update_fields=["go_live_at"])

		content = dict(revision.content)
		content["go_live_at"] = go_live_at.isoformat()
		revision.content = json.dumps(content, default=str)
		revision.save(update_fields=["content"])

		with patch("wagtail.models.Page.publish", return_value=None) as wagtail_publish:
			with patch.object(BlogPublicationService, "promote_published_candidate") as promote:
				page.publish(revision)

		wagtail_publish.assert_called_once()
		promote.assert_not_called()
		state = BlogPublicationState.objects.get(page_id=page.pk)
		self.assertIsNone(state.published_body_version_id)
		self.assertEqual(state.publication_generation, 0)

	def test_scheduled_publish_rejects_revision_without_wagtail_approval_marker(self):
		page, revision = self._revision("未批准定时正文")

		with self.assertRaisesMessage(PublicationRevisionInvalidError, "scheduled_revision_not_approved"):
			page.publish(revision, log_action="wagtail.publish.scheduled")

		page.refresh_from_db()
		self.assertFalse(page.live)
		self.assertIsNone(page.live_revision_id)

	def test_scheduled_publish_rejects_revision_before_due_time(self):
		page, revision = self._revision("尚未到期正文")
		revision.approved_go_live_at = timezone.now() + timedelta(minutes=5)
		revision.save(update_fields=["approved_go_live_at"])

		with self.assertRaisesMessage(PublicationRevisionInvalidError, "scheduled_revision_not_due"):
			page.publish(revision, log_action="wagtail.publish.scheduled")

		page.refresh_from_db()
		self.assertFalse(page.live)
		self.assertIsNone(page.live_revision_id)
