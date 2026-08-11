"""独立内容索引的受限影子查询和脱敏观测。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import logging
import threading
import time

from django.conf import settings

from search.services.content_query import query_content_search_page
from search.services.elasticsearch import ContentSearchElasticsearchError


logger = logging.getLogger(__name__)


def classify_shadow_difference(expected, actual):
    """按结果集合和排序差异生成稳定、可聚合的分类。"""

    expected = list(expected)
    actual = list(actual)
    if expected == actual:
        return "same"
    if not actual:
        return "new_empty"
    if not expected:
        return "old_empty"
    expected_set = set(expected)
    actual_set = set(actual)
    if expected_set == actual_set:
        return "order_changed"
    if expected_set - actual_set and actual_set - expected_set:
        return "missing_and_extra"
    if expected_set - actual_set:
        return "missing_from_new"
    return "extra_in_new"


@dataclass(frozen=True)
class ShadowSearchRequest:
    query_string: str
    search_type: str
    start_date: object
    end_date: object
    order_by: str | None
    start: int
    size: int
    expected_page_ids: tuple[int, ...]
    target: object


class ContentSearchShadowObserver:
    """进程内有界影子执行器；它不等待 ES 结果，也不持久化查询原文。"""

    def __init__(self):
        self._executor = None
        self._semaphore = None
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._opened_until = 0.0

    def _ensure_runtime(self):
        with self._lock:
            if self._executor is None:
                workers = max(1, int(getattr(settings, "CONTENT_SEARCH_SHADOW_MAX_WORKERS", 1)))
                self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="content-search-shadow")
                self._semaphore = threading.BoundedSemaphore(
                    max(1, int(getattr(settings, "CONTENT_SEARCH_SHADOW_MAX_IN_FLIGHT", workers)))
                )

    @staticmethod
    def query_hash(request):
        identity = {
            "query": request.query_string,
            "type": request.search_type,
            "start_date": str(request.start_date or ""),
            "end_date": str(request.end_date or ""),
            "order_by": request.order_by or "",
            "start": request.start,
            "size": request.size,
        }
        serialized = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _sampled(self, query_hash):
        rate = min(max(float(getattr(settings, "CONTENT_SEARCH_SHADOW_SAMPLE_RATE", 0.0)), 0.0), 1.0)
        if rate <= 0:
            return False
        if rate >= 1:
            return True
        return int(query_hash[:12], 16) / float(16**12) < rate

    def _breaker_open(self):
        with self._lock:
            return time.monotonic() < self._opened_until

    def _record_success(self):
        with self._lock:
            self._consecutive_failures = 0

    def _record_failure(self):
        with self._lock:
            self._consecutive_failures += 1
            threshold = max(1, int(getattr(settings, "CONTENT_SEARCH_SHADOW_FAILURE_THRESHOLD", 3)))
            if self._consecutive_failures >= threshold:
                cooldown = max(1, int(getattr(settings, "CONTENT_SEARCH_SHADOW_COOLDOWN_SECONDS", 30)))
                self._opened_until = time.monotonic() + cooldown

    @staticmethod
    def _difference(expected, actual):
        return classify_shadow_difference(expected, actual)

    def _run(self, request, query_hash):
        started = time.perf_counter()
        try:
            result = query_content_search_page(
                request.target,
                request.query_string,
                start=request.start,
                size=request.size,
                start_date=request.start_date,
                end_date=request.end_date,
                order_by=request.order_by,
                index_name=request.target.index_name,
                request_timeout=getattr(settings, "CONTENT_SEARCH_SHADOW_TIMEOUT_SECONDS", 0.25),
            )
            self._record_success()
            logger.info(
                "content_search_shadow query_hash=%s result_page_ids=%s difference=%s latency_ms=%.2f error_code=",
                query_hash,
                list(result.page_ids),
                self._difference(request.expected_page_ids, result.page_ids),
                (time.perf_counter() - started) * 1000,
            )
        except Exception as error:
            self._record_failure()
            error_code = getattr(error, "code", "shadow_query_failed")
            if not isinstance(error_code, str):
                error_code = "shadow_query_failed"
            logger.warning(
                "content_search_shadow query_hash=%s result_page_ids=[] difference=unavailable latency_ms=%.2f error_code=%s",
                query_hash,
                (time.perf_counter() - started) * 1000,
                error_code[:64],
            )

    def submit(self, request):
        query_hash = self.query_hash(request)
        if not self._sampled(query_hash) or self._breaker_open():
            return False
        self._ensure_runtime()
        if not self._semaphore.acquire(blocking=False):
            return False
        future = self._executor.submit(self._run, request, query_hash)
        future.add_done_callback(lambda _future: self._semaphore.release())
        return True

    def reset_for_tests(self):
        with self._lock:
            self._consecutive_failures = 0
            self._opened_until = 0.0


shadow_observer = ContentSearchShadowObserver()


class ShadowObservedResults:
    """在用户读取旧结果当前页后异步发起一次影子比较。"""

    def __init__(self, wrapped, request_factory):
        self._wrapped = wrapped
        self._request_factory = request_factory
        self._submitted = set()

    def count(self):
        return self._wrapped.count()

    def __len__(self):
        return len(self._wrapped)

    def __bool__(self):
        return bool(self._wrapped)

    def __getitem__(self, key):
        result = self._wrapped[key]
        if isinstance(key, slice):
            start = key.start or 0
            stop = key.stop if key.stop is not None else start + 20
            request_key = (start, stop)
            items = result
        else:
            request_key = (int(key), int(key) + 1)
            items = [result] if result is not None else []
        if request_key not in self._submitted:
            self._submitted.add(request_key)
            expected_ids = tuple(getattr(item, "pk", None) for item in items)
            expected_ids = tuple(item_id for item_id in expected_ids if item_id is not None)
            request = self._request_factory(request_key[0], max(request_key[1] - request_key[0], 0), expected_ids)
            shadow_observer.submit(request)
        return result

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


def wrap_search_results(wrapped, request_factory):
    return ShadowObservedResults(wrapped, request_factory)
