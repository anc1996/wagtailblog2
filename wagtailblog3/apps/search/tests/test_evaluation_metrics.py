"""WP0：验证搜索效果和延迟指标计算。"""

from django.test import SimpleTestCase

from search.evaluation import calculate_metrics


class SearchEvaluationMetricsTests(SimpleTestCase):
    """固定少量合成判断，避免依赖真实正文或页面数据。"""

    def test_calculates_ranking_and_latency_metrics(self):
        result = calculate_metrics(
            [
                {
                    "expected_top_10": [
                        {"page_id": "1", "relevance": 3},
                        {"page_id": "2", "relevance": 1},
                    ],
                    "returned_page_ids": ["1", "3", "2"],
                    "latency_ms": 10,
                },
                {
                    "expected_top_10": [{"page_id": "4", "relevance": 2}],
                    "returned_page_ids": [],
                    "latency_ms": 30,
                },
            ]
        )

        self.assertEqual(result["case_count"], 2)
        self.assertEqual(result["recall_at_10"], 0.5)
        self.assertEqual(result["mrr"], 0.5)
        self.assertEqual(result["zero_result_rate"], 0.5)
        self.assertEqual(result["latency_ms"], {"p50": 10, "p95": 30, "p99": 30})

    def test_requires_a_relevance_judgement(self):
        with self.assertRaisesMessage(ValueError, "至少需要一条带相关性标注的评测样本"):
            calculate_metrics(
                [
                    {
                        "expected_top_10": [],
                        "returned_page_ids": [],
                        "latency_ms": 10,
                    }
                ]
            )
