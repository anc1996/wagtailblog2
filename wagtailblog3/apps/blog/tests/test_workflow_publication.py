"""M4.3 Workflow 审批 Revision 与 Mongo 正文版本围栏测试。"""

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from wagtail.models import GroupApprovalTask, Workflow, WorkflowTask

from blog.models import BlogPage, BlogPublicationState
from blog.services.publication import PublicationWorkflowRevisionDriftError
from search.tests.test_lifecycle_baseline import BlogLifecycleFixtureMixin


class BlogWorkflowPublicationTests(BlogLifecycleFixtureMixin, TestCase):
    """验证 Workflow 完成时只发布最终审批任务绑定的 Revision。"""

    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_superuser(
            username="workflow-reviewer",
            email="workflow-reviewer@example.invalid",
            password="not-used",
        )
        workflow_content_type = ContentType.objects.get_for_model(
            GroupApprovalTask, for_concrete_model=False
        )
        self.task = GroupApprovalTask.objects.create(
            name="BlogPage 审批",
            content_type=workflow_content_type,
        )
        self.task_2 = GroupApprovalTask.objects.create(
            name="BlogPage 二次审批",
            content_type=workflow_content_type,
        )
        self.workflow = Workflow.objects.create(name="BlogPage Workflow")
        WorkflowTask.objects.create(workflow=self.workflow, task=self.task, sort_order=0)
        WorkflowTask.objects.create(workflow=self.workflow, task=self.task_2, sort_order=1)

    def _page_and_revision(self, text="待审批正文"):
        page = self._create_draft_page(text)
        return page, page.save_revision()

    def test_workflow_approval_freezes_revision_and_body_metadata(self):
        page, revision = self._page_and_revision()
        state = self.workflow.start(page, self.user)

        state.current_task_state.approve(user=self.user)
        state.refresh_from_db()
        state.current_task_state.approve(user=self.user)

        page.refresh_from_db()
        publication_state = BlogPublicationState.objects.get(page_id=page.pk)
        self.assertTrue(page.live)
        self.assertEqual(page.live_revision_id, revision.pk)
        self.assertEqual(publication_state.approved_revision_id, revision.pk)
        self.assertEqual(
            publication_state.approved_body_version_id,
            revision.content["mongo_body_version_id"],
        )
        self.assertEqual(
            publication_state.approved_body_sha256,
            revision.content["body_sha256"],
        )

    def test_workflow_approval_rejects_revision_created_after_task_started(self):
        page, approved_revision = self._page_and_revision("原始审批正文")
        state = self.workflow.start(page, self.user)

        page.body = self._markdown_body("审批后新增正文")
        page.save_revision()

        state.current_task_state.approve(user=self.user)
        state.refresh_from_db()
        with self.assertRaises(PublicationWorkflowRevisionDriftError):
            state.current_task_state.approve(user=self.user)

        page.refresh_from_db()
        state.refresh_from_db()
        self.assertFalse(page.live)
        self.assertEqual(state.status, state.STATUS_IN_PROGRESS)
        self.assertFalse(
            BlogPublicationState.objects.filter(page_id=page.pk).exists()
        )
        self.assertEqual(approved_revision.content["mongo_body_version_id"],
                         state.current_task_state.revision.content["mongo_body_version_id"])
