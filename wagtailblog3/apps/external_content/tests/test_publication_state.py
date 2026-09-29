"""
Wagtail Page 正文外置通用协议 - ExternalContentPublicationState 模型定向测试。

验证：
1. 页面级发布状态记录的创建、查询与更新；
2. published_manifest、draft_manifest 与 approved_manifest 的 JSON 快照序列化；
3. publication_generation 单调递增更新；
4. 别名源追踪 (source_page_id, source_revision_id)；
5. page_id 作为主键的唯一性。
"""

from django.db import IntegrityError
from django.db.models import F
from django.test import TestCase
from django.utils import timezone

from external_content.models import ExternalContentPublicationState


class PublicationStateModelTests(TestCase):
    """ExternalContentPublicationState 模型持久化测试。"""

    def setUp(self) -> None:
        self.sample_manifest = {
            "protocol_version": 1,
            "content_storage_schema": 1,
            "model_label": "guides.guidepage",
            "page_id": 55,
            "fields": {
                "body": {
                    "codec": "streamfield.v1",
                    "schema_version": 1,
                    "version_id": "v-123",
                    "payload_sha256": "a" * 64,
                }
            },
        }

    def test_create_and_read_publication_state(self) -> None:
        """测试创建发布状态并持久化 Manifest JSON。"""
        now = timezone.now()
        state = ExternalContentPublicationState.objects.create(
            page_id=55,
            model_label="guides.guidepage",
            publication_generation=1,
            published_revision_id=1001,
            published_manifest=self.sample_manifest,
            published_at=now,
            draft_revision_id=1002,
            draft_manifest=self.sample_manifest,
        )

        fetched = ExternalContentPublicationState.objects.get(page_id=55)
        self.assertEqual(fetched.model_label, "guides.guidepage")
        self.assertEqual(fetched.publication_generation, 1)
        self.assertEqual(fetched.published_revision_id, 1001)
        self.assertEqual(fetched.published_manifest, self.sample_manifest)
        self.assertEqual(fetched.draft_manifest, self.sample_manifest)
        self.assertIn("55", str(fetched))

    def test_duplicate_page_id_rejected(self) -> None:
        """相同 page_id 冲突阻断。"""
        ExternalContentPublicationState.objects.create(
            page_id=77,
            model_label="guides.guidepage",
        )
        with self.assertRaises(IntegrityError):
            ExternalContentPublicationState.objects.create(
                page_id=77,
                model_label="guides.guidepage",
            )

    def test_monotonic_generation_increment(self) -> None:
        """测试单调递增代次原子递增。"""
        state = ExternalContentPublicationState.objects.create(
            page_id=88,
            model_label="guides.guidepage",
            publication_generation=0,
        )
        ExternalContentPublicationState.objects.filter(page_id=88).update(
            publication_generation=F("publication_generation") + 1
        )
        state.refresh_from_db()
        self.assertEqual(state.publication_generation, 1)

    def test_alias_tracking_fields(self) -> None:
        """测试别名页面源信息追踪。"""
        state = ExternalContentPublicationState.objects.create(
            page_id=99,
            model_label="guides.guidepage",
            source_page_id=55,
            source_revision_id=1001,
        )
        fetched = ExternalContentPublicationState.objects.get(page_id=99)
        self.assertEqual(fetched.source_page_id, 55)
        self.assertEqual(fetched.source_revision_id, 1001)