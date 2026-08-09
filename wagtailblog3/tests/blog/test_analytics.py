"""访问统计的隐私摘要和来源分类测试。"""

from datetime import timedelta

from django.test import RequestFactory, SimpleTestCase
from django.utils import timezone

from blog.page_view_counter import source_for_request, visitor_key_for_request


class AnalyticsRequestTests(SimpleTestCase):
    """这些规则不访问数据库，确保分析不会意外保存完整来源地址。"""

    def setUp(self):
        self.factory = RequestFactory()
        self.today = timezone.localdate()

    def test_anonymous_key_is_daily_and_does_not_contain_ip(self):
        request = self.factory.get("/", HTTP_USER_AGENT="test-agent", REMOTE_ADDR="203.0.113.8")
        key = visitor_key_for_request(request, self.today)

        self.assertEqual(len(key), 64)
        self.assertNotIn("203.0.113.8", key)
        self.assertNotEqual(key, visitor_key_for_request(request, self.today - timedelta(days=1)))

    def test_source_keeps_hostname_without_path_or_query(self):
        request = self.factory.get("/", HTTP_REFERER="https://www.google.com/search?q=private")
        self.assertEqual(source_for_request(request), ("search", "www.google.com"))

    def test_internal_and_direct_sources_are_distinguished(self):
        internal = self.factory.get("/", HTTP_HOST="blog.example.test", HTTP_REFERER="https://blog.example.test/post/?q=x")
        direct = self.factory.get("/", HTTP_HOST="blog.example.test")
        self.assertEqual(source_for_request(internal), ("internal", "blog.example.test"))
        self.assertEqual(source_for_request(direct), ("direct", ""))
