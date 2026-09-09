"""??????????????????? (test_listing_invalidation).

?????
1. ?????????????????????
2. ????site_id??????locale_id??????index_id????????
3. ???????????????????????? scope ? INCR ???
4. ?????Rollback?? on_commit ?????generation ?????
5. ????????? scope ???????????????????
6. ?? 20 ?????????????
7. Redis ??? Fail-Open ???????????
"""

import threading
import uuid
from unittest.mock import patch

from django.conf import settings
from django.core.cache import caches
from django.db import transaction
from django.test import TestCase, TransactionTestCase
from wagtail.models import Locale, Page, Site
from wagtail.signals import page_published, page_unpublished

from blog.models import Author, BlogIndexPage, BlogPage
from blog.services.listing_invalidation import (
    DeleteScope,
    ListingInvalidationService,
    ListingScope,
)


class ListingInvalidationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.locale, _ = Locale.objects.get_or_create(language_code=settings.LANGUAGE_CODE)
        root = Page.get_first_root_node()
        if root is None:
            root = Page(title="???", slug="test-invalidation-root", locale=cls.locale)
            Page.add_root(instance=root)

        cls.site = Site.objects.filter(is_default_site=True).first()
        if cls.site is None:
            cls._orig_root_page = None
            cls.site = Site.objects.create(hostname="localhost", port=80, root_page=root, is_default_site=True)
        else:
            cls._orig_root_page = cls.site.root_page
            cls.site.root_page = root
            cls.site.save()

        cls.site_id = cls.site.pk
        cls.locale_id = cls.locale.pk

        cls.index_a = root.add_child(
            instance=BlogIndexPage(title="??A", slug=f"cat-a-{uuid.uuid4().hex[:6]}", locale=cls.locale)
        )
        cls.index_b = root.add_child(
            instance=BlogIndexPage(title="??B", slug=f"cat-b-{uuid.uuid4().hex[:6]}", locale=cls.locale)
        )

        cls.author = Author.objects.create(name="??????", slug="inv-author")

        cls.post_a = cls.index_a.add_child(
            instance=BlogPage(
                title="??A",
                slug=f"post-a-{uuid.uuid4().hex[:6]}",
                date="2026-09-09",
                intro="??",
                locale=cls.locale,
            )
        )
        cls.post_a.authors.add(cls.author)
        cls.post_a.save()

    def setUp(self):
        self.cache = caches["default"]
        self.cache.clear()

    def test_scope_isolation_and_atomic_bump(self):
        """?????????????????????????."""
        scope_a = ListingScope(site_id=self.site_id, locale_id=self.locale_id, index_page_id=self.index_a.pk)
        scope_b = ListingScope(site_id=self.site_id, locale_id=self.locale_id, index_page_id=self.index_b.pk)
        scope_other_site = ListingScope(site_id=9999, locale_id=self.locale_id, index_page_id=self.index_a.pk)

        # ?????? 1
        self.assertEqual(ListingInvalidationService.get_generation(scope_a.site_id, scope_a.locale_id, scope_a.index_page_id), 1)
        self.assertEqual(ListingInvalidationService.get_generation(scope_b.site_id, scope_b.locale_id, scope_b.index_page_id), 1)

        # ?? scope_a
        gen_a1 = ListingInvalidationService.bump_generation(scope_a, reason="test_bump")
        self.assertEqual(gen_a1, 2)
        self.assertEqual(ListingInvalidationService.get_generation(scope_a.site_id, scope_a.locale_id, scope_a.index_page_id), 2)

        # scope_b ? scope_other_site ??? 1
        self.assertEqual(ListingInvalidationService.get_generation(scope_b.site_id, scope_b.locale_id, scope_b.index_page_id), 1)
        self.assertEqual(ListingInvalidationService.get_generation(scope_other_site.site_id, scope_other_site.locale_id, scope_other_site.index_page_id), 1)

    def test_archive_and_author_generation_bump(self):
        """???????????????."""
        self.assertEqual(ListingInvalidationService.get_archive_generation(self.site_id, self.locale_id), 1)
        self.assertEqual(ListingInvalidationService.get_author_generation(self.site_id, self.locale_id), 1)

        ListingInvalidationService.bump_archive_generation(self.site_id, self.locale_id, reason="archive_test")
        self.assertEqual(ListingInvalidationService.get_archive_generation(self.site_id, self.locale_id), 2)
        # ????????????
        self.assertEqual(ListingInvalidationService.get_author_generation(self.site_id, self.locale_id), 1)

    def test_capture_delete_scope(self):
        """???????????????????????."""
        del_scope = ListingInvalidationService.capture_delete_scope(self.post_a)
        self.assertIsInstance(del_scope, DeleteScope)
        target_index_ids = [s.index_page_id for s in del_scope.scopes]
        self.assertIn(self.index_a.pk, target_index_ids)
        self.assertIn((self.site_id, self.locale_id), del_scope.archive_scopes)

    def test_single_transaction_deduplication(self):
        """??????????????????? INCR ??."""
        scope_a = ListingScope(site_id=self.site_id, locale_id=self.locale_id, index_page_id=self.index_a.pk)

        with self.captureOnCommitCallbacks(execute=True):
            # ???? 3 ?
            ListingInvalidationService.schedule_scopes_bump({scope_a}, set(), set(), reason="dup1")
            ListingInvalidationService.schedule_scopes_bump({scope_a}, set(), set(), reason="dup2")
            ListingInvalidationService.schedule_scopes_bump({scope_a}, set(), set(), reason="dup3")

        # ??? 1 ? (?? 1 -> 2)
        current_gen = ListingInvalidationService.get_generation(scope_a.site_id, scope_a.locale_id, scope_a.index_page_id)
        self.assertEqual(current_gen, 2)

    def test_fail_open_on_redis_error(self):
        """?? Redis ?????????????????."""
        scope_a = ListingScope(site_id=self.site_id, locale_id=self.locale_id, index_page_id=self.index_a.pk)
        with patch.object(self.cache, "get", side_effect=Exception("Redis read timeout")):
            gen = ListingInvalidationService.get_generation(scope_a.site_id, scope_a.locale_id, scope_a.index_page_id)
            self.assertEqual(gen, 1)

        with patch.object(self.cache, "incr", side_effect=Exception("Redis write timeout")):
            gen = ListingInvalidationService.bump_generation(scope_a, reason="error_test")
            self.assertEqual(gen, 1)


class TransactionRollbackAndConcurrencyTests(TransactionTestCase):
    """????????????."""

    def setUp(self):
        self.cache = caches["default"]
        self.cache.clear()

    def test_transaction_rollback_prevents_invalidation(self):
        """??????? on_commit ???????????????."""
        scope = ListingScope(site_id=1, locale_id=1, index_page_id=1001)

        try:
            with transaction.atomic():
                ListingInvalidationService.schedule_scopes_bump({scope}, set(), set(), reason="will_rollback")
                # ????????????
                raise ValueError("Simulated DB Error")
        except ValueError:
            pass

        # ??????? 1?????
        self.assertEqual(ListingInvalidationService.get_generation(1, 1, 1001), 1)

    def test_nested_atomic_rollback_does_not_leak_to_subsequent_commit(self):
        """嵌套事务回滚时，被回滚的 scope 不会在后续成功提交的事务中泄漏被推进."""
        scope_rolled_back = ListingScope(site_id=1, locale_id=1, index_page_id=8881)
        scope_committed = ListingScope(site_id=1, locale_id=1, index_page_id=8882)

        with transaction.atomic():
            ListingInvalidationService.schedule_scopes_bump({scope_committed}, set(), set(), reason="outer_commit")
            try:
                with transaction.atomic():
                    ListingInvalidationService.schedule_scopes_bump({scope_rolled_back}, set(), set(), reason="inner_rollback")
                    raise ValueError("Inner Rollback")
            except ValueError:
                pass

        self.assertEqual(ListingInvalidationService.get_generation(1, 1, 8882), 2)
        self.assertEqual(ListingInvalidationService.get_generation(1, 1, 8881), 1)

    def test_celery_task_postrun_cleans_thread_state(self):
        """Celery 任务完成后触发 task_postrun 信号清理线程状态."""
        from blog.services.listing_invalidation import _get_tx_pending_scopes, clear_tx_pending_state
        _get_tx_pending_scopes().add(ListingScope(site_id=1, locale_id=1, index_page_id=9999))
        self.assertEqual(len(_get_tx_pending_scopes()), 1)

        try:
            from celery.signals import task_postrun
            task_postrun.send(sender=None)
        except Exception:
            clear_tx_pending_state()

        self.assertEqual(len(_get_tx_pending_scopes()), 0)

    def test_concurrent_atomic_increments(self):
        """?? 20 ????????????????????."""
        scope = ListingScope(site_id=2, locale_id=1, index_page_id=2002)
        threads = []
        errors = []

        def worker():
            try:
                ListingInvalidationService.bump_generation(scope, reason="concurrent_bump")
            except Exception as e:
                errors.append(e)

        for _ in range(20):
            t = threading.Thread(target=worker)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        final_gen = ListingInvalidationService.get_generation(2, 1, 2002)
        # ??? 1 ????? 20 ??? 21
        self.assertEqual(final_gen, 21)
    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "_orig_root_page", None):
            site = Site.objects.filter(is_default_site=True).first()
            if site and cls._orig_root_page:
                site.root_page = cls._orig_root_page
                site.save()
        super().tearDownClass()
