from django.test import SimpleTestCase

from observability.forms import LogClearForm, LogClearSelectionForm, LogFilterForm


class LogFilterFormTests(SimpleTestCase):
    def _data(self, **overrides):
        data = {
            "domain": "",
            "kind": "error",
            "level": "",
            "period": "custom",
            "custom_start": "2026-07-29T10:00",
            "custom_end": "2026-07-29T11:00",
            "keyword": "",
            "page_size": "100",
            "cursor": "",
        }
        data.update(overrides)
        return data

    def test_custom_period_requires_both_boundaries(self):
        form = LogFilterForm(self._data(custom_end=""))
        self.assertFalse(form.is_valid())
        self.assertIn("custom_start", form.errors)

    def test_custom_period_rejects_reversed_range(self):
        form = LogFilterForm(
            self._data(custom_start="2026-07-29T12:00", custom_end="2026-07-29T11:00")
        )
        self.assertFalse(form.is_valid())
        self.assertIn("custom_end", form.errors)

    def test_custom_period_returns_local_naive_datetimes(self):
        form = LogFilterForm(self._data())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.since().tzinfo)
        self.assertIsNone(form.until().tzinfo)


class LogClearFormTests(SimpleTestCase):
    def test_all_target_is_generated_by_server(self):
        form = LogClearForm(
            {
                "target_type": "all",
                "target": "blog",
                "kind": "",
                "scope": "all",
                "confirmation": "清空全部日志",
                "idempotency_key": "80902755-30ed-4530-ae2b-26ebc12ef3fc",
                "preview_token": "signed-preview",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["target"], "")

    def test_all_target_still_rejects_arbitrary_choice(self):
        form = LogClearForm(
            {
                "target_type": "all",
                "target": "伪造目标",
                "kind": "",
                "scope": "all",
                "confirmation": "清空全部日志",
                "idempotency_key": "80902755-30ed-4530-ae2b-26ebc12ef3fc",
                "preview_token": "signed-preview",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("target", form.errors)

    def test_arbitrary_file_target_is_rejected(self):
        form = LogClearForm(
            {
                "target_type": "file",
                "target": "/etc/passwd",
                "kind": "",
                "scope": "all",
                "confirmation": "确认清理日志",
                "idempotency_key": "80902755-30ed-4530-ae2b-26ebc12ef3fc",
                "preview_token": "signed-preview",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("target", form.errors)

    def test_non_all_targets_require_strengthened_confirmation(self):
        form = LogClearForm(
            {
                "target_type": "file",
                "target": "blog_error",
                "kind": "error",
                "scope": "current",
                "confirmation": "",
                "idempotency_key": "80902755-30ed-4530-ae2b-26ebc12ef3fc",
                "preview_token": "signed-preview",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("confirmation", form.errors)

    def test_selection_form_normalizes_business_target(self):
        form = LogClearSelectionForm(
            {
                "target_type": "business",
                "target": "blog",
                "kind": "activity",
                "scope": "rotated",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["target"], "")
