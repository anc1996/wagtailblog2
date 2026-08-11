"""WP0：验证脱敏搜索评测样本生成器。"""

import json
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase


class SearchEvaluationFixtureTests(SimpleTestCase):
    """评测样本不包含真实正文、页面标题或生产标识。"""

    def test_default_fixture_contains_one_hundred_pending_synthetic_cases(self):
        output = StringIO()

        call_command("generate_search_evaluation_fixture", stdout=output)

        fixture = json.loads(output.getvalue())
        self.assertTrue(fixture["synthetic"])
        self.assertEqual(fixture["case_count"], 100)
        self.assertEqual(len(fixture["cases"]), 100)
        self.assertTrue(all(case["judgement_status"] == "pending" for case in fixture["cases"]))
        self.assertTrue(all(case["expected_top_10"] == [] for case in fixture["cases"]))

    def test_fixture_count_is_limited_to_the_wp0_evaluation_range(self):
        with self.assertRaises(CommandError):
            call_command("generate_search_evaluation_fixture", "--count", "99")
