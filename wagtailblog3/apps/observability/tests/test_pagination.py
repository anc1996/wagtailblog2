import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from observability.pagination import read_log_page


TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "observability-pagination-tests",
    }
}


def _line(second):
    return (
        f"[2026-07-29 15:00:{second:02d}] ERROR [blog.views:run:10] "
        f"[pid=1 thread=MainThread] record {second}\n"
    )


@override_settings(CACHES=TEST_CACHES)
class LogPaginationTests(SimpleTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "blog").mkdir()
        (self.root / "blog/blog_error.log").write_text(
            "".join(_line(i) for i in range(12)), encoding="utf-8"
        )
        self.filters = {
            "domain": "blog",
            "kind": "error",
            "level": "",
            "keyword": "",
            "since": None,
            "until": None,
            "include_rotated": False,
        }

    def _page(self, page, token=""):
        with override_settings(LOG_DIR=self.root):
            return read_log_page(
                owner_id=7,
                requested_page=page,
                page_size=2,
                session_token=token,
                filters=self.filters,
            )

    def test_can_jump_to_page_and_move_back_and_forward_without_duplicates(self):
        third = self._page(3)
        second = self._page(2, third.session_token)
        fourth = self._page(4, third.session_token)

        self.assertEqual([item.message for item in third.records], ["record 7", "record 6"])
        self.assertEqual([item.message for item in second.records], ["record 9", "record 8"])
        self.assertEqual([item.message for item in fourth.records], ["record 5", "record 4"])
        self.assertEqual(third.previous_page, 2)
        self.assertEqual(third.next_page, 4)
        self.assertEqual(second.session_token, third.session_token)

    def test_total_pages_becomes_known_only_after_end_is_reached(self):
        first = self._page(1)
        self.assertIsNone(first.total_pages)
        last = self._page(6, first.session_token)
        self.assertEqual(last.page, 6)
        self.assertEqual(last.total_pages, 6)
        self.assertIsNone(last.next_page)

    def test_deep_jump_is_progressive_and_resumes_from_cached_checkpoint(self):
        with patch("observability.pagination.MAX_JUMP_STEPS", 2), patch(
            "observability.pagination.MAX_JUMP_SECONDS", 60
        ):
            first_attempt = self._page(6)
            second_attempt = self._page(6, first_attempt.session_token)

        self.assertTrue(first_attempt.jump_pending)
        self.assertEqual(first_attempt.page, 3)
        self.assertFalse(second_attempt.jump_pending)
        self.assertEqual(second_attempt.page, 6)
        self.assertEqual(
            [item.message for item in second_attempt.records], ["record 1", "record 0"]
        )

    def test_deep_jump_obeys_time_budget(self):
        with patch("observability.pagination.MAX_JUMP_SECONDS", 0):
            attempt = self._page(6)
        self.assertTrue(attempt.jump_pending)
        self.assertEqual(attempt.page, 2)
        self.assertLess(attempt.page, attempt.requested_page)

    def test_page_session_is_bound_to_user_and_filters(self):
        first = self._page(1)
        changed_filters = {**self.filters, "keyword": "record 1"}
        with override_settings(LOG_DIR=self.root):
            changed = read_log_page(
                owner_id=8,
                requested_page=1,
                page_size=2,
                session_token=first.session_token,
                filters=changed_filters,
            )
        self.assertNotEqual(changed.session_token, first.session_token)

    def test_invalidated_snapshot_falls_back_to_new_first_page(self):
        second = self._page(2)
        path = self.root / "blog/blog_error.log"
        path.write_text(_line(59), encoding="utf-8")
        refreshed = self._page(2, second.session_token)
        self.assertEqual(refreshed.page, 1)
        self.assertEqual([item.message for item in refreshed.records], ["record 59"])
