"""验证发布对账周期的 high-water 边界不会被孤儿 State 绕过。"""

from django.test import TestCase

from blog.models import BlogPublicationState
from blog.services.publication_consistency import check_blog_publication_consistency
from search.tests.test_lifecycle_baseline import BlogLifecycleFixtureMixin


class BlogPublicationConsistencyUpperBoundTests(BlogLifecycleFixtureMixin, TestCase):
    """覆盖页面批次为空时 State 回退查询的周期边界。"""

    def test_empty_page_batch_does_not_read_state_beyond_high_water(self):
        """周期结束位置不应把 high-water 之后新建的孤儿 State 计入当前报告。"""
        page = self._create_draft_page("high-water boundary")
        BlogPublicationState.objects.create(page_id=page.pk + 1000)

        report = check_blog_publication_consistency(
            after_page_id=page.pk,
            upper_bound_page_id=page.pk,
            limit=10,
            check_mongo=False,
        )

        self.assertEqual(report["scanned"], 0)
        self.assertEqual(report["next_after_page_id"], page.pk)
        self.assertNotIn("page_missing", report["counts"])
