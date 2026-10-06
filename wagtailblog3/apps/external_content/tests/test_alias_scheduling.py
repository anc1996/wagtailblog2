"""别名不得提前公开首次排期正文，并验证到期后的正式同步。"""

import uuid
from datetime import timedelta, timezone as datetime_timezone
from typing import Any
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from wagtail.models import Locale, Page, Revision

from external_content.models import ExternalContentPublicationState
from external_content.exceptions import PublicationRevisionInvalidError
from external_content.locking import preserve_threadlocal_context
from external_content.publication import _controlled_save_context
from external_content.tests.test_alias_contract import _create_test_user
from guides.models import GuideIndexPage, GuidePage, GuideTopic


class ExternalContentAliasSchedulingTests(TestCase):
    """在真实发布 Action 上验证排期和别名的公开边界。"""

    def setUp(self) -> None:
        self.user = _create_test_user("alias_schedule")
        self.locale, _ = Locale.objects.get_or_create(language_code="zh-hans")
        root = Page.objects.filter(depth=1).first()
        if root is None:
            root = Page.add_root(title="Root", locale=self.locale)
        self.index = GuideIndexPage(
            title="排期指南",
            slug=f"schedule-index-{uuid.uuid4().hex[:8]}",
            locale=self.locale,
            live=False,
        )
        root.add_child(instance=self.index)
        self.topic = GuideTopic.objects.create(
            name="排期",
            slug=f"schedule-topic-{uuid.uuid4().hex[:8]}",
        )
        self.source = GuidePage(
            title="尚未发布的指南",
            slug=f"schedule-guide-{uuid.uuid4().hex[:8]}",
            summary="排期正文边界",
            topic=self.topic,
            difficulty="intermediate",
            locale=self.locale,
            body=[
                {"type": "lead", "value": "<p>排期之前的草稿</p>"},
                {
                    "type": "step",
                    "value": {"title": "准备", "description": "<p>执行准备步骤</p>", "image": None},
                },
            ],
            live=False,
        )
        self.index.add_child(instance=self.source)
        self.source.save_revision(user=self.user)

    def _state_snapshot(self, page_id: int) -> dict[str, Any]:
        return ExternalContentPublicationState.objects.filter(page_id=page_id).values(
            "published_revision_id",
            "published_manifest",
            "source_revision_id",
            "publication_generation",
        ).get()

    def _assert_future_schedule_remains_private(self, scheduled_at: Any) -> None:
        """合法时区和 JSON 毫秒截断不应拒绝首次排期，更不能提前同步正文。"""
        alias = self.source.create_alias(
            parent=self.index,
            update_slug=f"time-alias-{uuid.uuid4().hex[:8]}",
            user=self.user,
        )
        source_before = self._state_snapshot(self.source.pk)
        alias_before = self._state_snapshot(alias.pk)
        self.source.go_live_at = scheduled_at
        revision = self.source.save_revision(user=self.user)
        revision.publish(user=self.user)
        revision.refresh_from_db()
        self.assertIsNotNone(revision.approved_go_live_at)
        self.assertFalse(Page.objects.get(pk=self.source.pk).live)
        self.assertFalse(Page.objects.get(pk=alias.pk).live)
        self.assertEqual(self._state_snapshot(self.source.pk), source_before)
        self.assertEqual(self._state_snapshot(alias.pk), alias_before)
        self.assertEqual(Revision.objects.for_instance(alias).count(), 0)

    def test_first_schedule_accepts_equivalent_timezone_offset(self) -> None:
        scheduled_at = (timezone.now() + timedelta(days=1)).replace(microsecond=123000)
        self._assert_future_schedule_remains_private(
            scheduled_at.astimezone(datetime_timezone(timedelta(hours=8)))
        )

    def test_first_schedule_accepts_submillisecond_precision(self) -> None:
        scheduled_at = (timezone.now() + timedelta(days=1)).replace(microsecond=456)
        self._assert_future_schedule_remains_private(scheduled_at)

    def test_first_schedule_accepts_stored_offset_snapshot(self) -> None:
        self.source.go_live_at = (timezone.now() + timedelta(days=1)).replace(microsecond=123000)
        revision = self.source.save_revision(user=self.user)
        revision.content["go_live_at"] = self.source.go_live_at.astimezone(
            datetime_timezone(timedelta(hours=8))
        ).isoformat()
        revision.approved_go_live_at = self.source.go_live_at
        revision.save(update_fields=["content", "approved_go_live_at"])
        revision.refresh_from_db()
        before = self._state_snapshot(self.source.pk)
        with preserve_threadlocal_context(_controlled_save_context, "in_publication_action", True):
            self.assertTrue(
                self.source._defer_first_scheduled_alias_update(revision, revision.content)
            )
        self.assertEqual(self._state_snapshot(self.source.pk), before)
        self.assertFalse(Page.objects.get(pk=self.source.pk).live)

    def test_first_schedule_rejects_adjacent_millisecond(self) -> None:
        self.source.go_live_at = (timezone.now() + timedelta(days=1)).replace(microsecond=456)
        revision = self.source.save_revision(user=self.user)
        revision.approved_go_live_at = self.source.go_live_at + timedelta(milliseconds=1)
        revision.save(update_fields=["approved_go_live_at"])
        before = self._state_snapshot(self.source.pk)
        with preserve_threadlocal_context(_controlled_save_context, "in_publication_action", True):
            with self.assertRaises(PublicationRevisionInvalidError):
                self.source._defer_first_scheduled_alias_update(revision, revision.content)
        self.assertEqual(self._state_snapshot(self.source.pk), before)
        self.assertFalse(Page.objects.get(pk=self.source.pk).live)

    def test_first_future_schedule_keeps_alias_private_until_due(self) -> None:
        alias = self.source.create_alias(
            parent=self.index,
            update_slug=f"private-alias-{uuid.uuid4().hex[:8]}",
            user=self.user,
        )
        source_before = self._state_snapshot(self.source.pk)
        alias_before = self._state_snapshot(alias.pk)
        self.source.body = [
            {"type": "lead", "value": "<p>到期才能公开的正文</p>"},
            {
                "type": "step",
                "value": {"title": "执行", "description": "<p>到期后执行</p>", "image": None},
            },
        ]
        self.source.go_live_at = timezone.now() + timedelta(days=1)
        scheduled_revision = self.source.save_revision(user=self.user)
        scheduled_revision.publish(user=self.user)

        self.assertFalse(Page.objects.get(pk=self.source.pk).live)
        self.assertFalse(Page.objects.get(pk=alias.pk).live)
        self.assertEqual(self._state_snapshot(self.source.pk), source_before)
        self.assertEqual(self._state_snapshot(alias.pk), alias_before)
        self.assertEqual(Revision.objects.for_instance(alias).count(), 0)

        due = self.source.go_live_at + timedelta(seconds=1)
        scheduled_revision.refresh_from_db()
        with patch("django.utils.timezone.now", return_value=due):
            scheduled_revision.publish(
                user=self.user,
                log_action="wagtail.publish.scheduled",
            )

        alias.refresh_from_db()
        self.assertTrue(alias.live)
        self.assertEqual(alias.body[0].value.source, "<p>到期才能公开的正文</p>")
        state = ExternalContentPublicationState.objects.get(page_id=alias.pk)
        self.assertEqual(state.source_revision_id, scheduled_revision.pk)
        self.assertEqual(state.published_manifest["page_id"], self.source.pk)
        self.assertEqual(Revision.objects.for_instance(alias).count(), 0)
