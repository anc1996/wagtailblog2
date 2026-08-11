"""搜索评测指标计算。"""

import math


def calculate_metrics(cases):
    """根据已人工标注的 Top 10 相关性和实际返回 ID 计算基线指标。"""
    recalls = []
    reciprocal_ranks = []
    ndcgs = []
    latencies = []
    zero_result_count = 0

    for case in cases:
        expected = {
            str(item["page_id"]): item["relevance"]
            for item in case["expected_top_10"]
            if item["relevance"] > 0
        }
        returned = [str(item) for item in case["returned_page_ids"][:10]]
        latencies.append(case["latency_ms"])
        if not returned:
            zero_result_count += 1

        if expected:
            relevant_returned = [page_id for page_id in returned if page_id in expected]
            recalls.append(len(relevant_returned) / len(expected))
            reciprocal_ranks.append(
                next(
                    (1 / position for position, page_id in enumerate(returned, start=1) if page_id in expected),
                    0,
                )
            )
            dcg = sum(
                (2 ** expected.get(page_id, 0) - 1) / math.log2(position + 1)
                for position, page_id in enumerate(returned, start=1)
            )
            ideal = sorted(expected.values(), reverse=True)[:10]
            ideal_dcg = sum(
                (2 ** relevance - 1) / math.log2(position + 1)
                for position, relevance in enumerate(ideal, start=1)
            )
            ndcgs.append(dcg / ideal_dcg if ideal_dcg else 0)

    if not cases:
        raise ValueError("评测集不能为空")
    if not recalls:
        raise ValueError("至少需要一条带相关性标注的评测样本")

    return {
        "case_count": len(cases),
        "recall_at_10": sum(recalls) / len(recalls),
        "mrr": sum(reciprocal_ranks) / len(reciprocal_ranks),
        "ndcg_at_10": sum(ndcgs) / len(ndcgs),
        "zero_result_rate": zero_result_count / len(cases),
        "latency_ms": {
            "p50": _percentile(latencies, 50),
            "p95": _percentile(latencies, 95),
            "p99": _percentile(latencies, 99),
        },
    }


def _percentile(values, percentile):
    ordered = sorted(values)
    index = max(0, math.ceil(percentile / 100 * len(ordered)) - 1)
    return ordered[index]
