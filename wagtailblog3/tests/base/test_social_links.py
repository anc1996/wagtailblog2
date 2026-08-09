"""社交链接规范化和结构化读取测试。"""

from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from base.blocks import SocialLinkBlock
from base.models import NavigationSettings
from base.services.social_links import (
    normalize_social_url,
    resolve_navigation_social_links,
    resolve_social_platform,
)
from base.templatetags.navigation_tags import get_navigation_social_links


class SocialLinkServiceTests(SimpleTestCase):
    """验证公开页脚只使用安全且可识别的链接。"""

    def test_normalize_url_removes_www_default_port_and_trailing_slash(self):
        url = normalize_social_url("HTTPS://www.GitHub.com:443/anc1996/")

        self.assertEqual(url, "https://github.com/anc1996")

    def test_structured_links_use_detected_platform_and_remove_duplicates(self):
        settings = SimpleNamespace(
            social_links=[
                SimpleNamespace(
                    block_type="social_link",
                    value={"url": "https://github.com/anc1996/", "label": ""},
                ),
                SimpleNamespace(
                    block_type="social_link",
                    value={"url": "https://www.github.com/anc1996", "label": "重复链接"},
                ),
                SimpleNamespace(
                    block_type="social_link",
                    value={"url": "https://zhuanlan.zhihu.com/", "label": "专栏"},
                ),
            ]
        )

        links = resolve_navigation_social_links(settings)

        self.assertEqual([(link.platform, link.label) for link in links], [("github", "GitHub"), ("zhihu", "专栏")])

    def test_empty_structured_links_do_not_read_retired_fields(self):
        settings = SimpleNamespace(social_links=[])

        self.assertEqual(resolve_navigation_social_links(settings), [])

    def test_unknown_http_site_uses_the_website_fallback(self):
        settings = SimpleNamespace(
            social_links=[
                SimpleNamespace(
                    block_type="social_link",
                    value={"url": "https://example.org/profile", "label": ""},
                )
            ]
        )

        link = resolve_navigation_social_links(settings)[0]

        self.assertEqual((link.platform, link.label, link.icon_name), ("website", "网站", "website"))

    def test_supported_platform_domains_resolve_to_expected_icons(self):
        cases = (
            ("https://github.com/example", "github"),
            ("https://www.linkedin.com/in/example", "linkedin"),
            ("https://zhuanlan.zhihu.com/example", "zhihu"),
            ("https://b23.tv/example", "bilibili"),
            ("https://weixin.qq.com/example", "wechat"),
            ("https://m.facebook.com/example", "facebook"),
            ("https://www.google.com/example", "google"),
            ("https://instagram.com/example", "instagram"),
        )

        for url, expected_platform in cases:
            with self.subTest(url=url):
                self.assertEqual(
                    resolve_social_platform(normalize_social_url(url))[0],
                    expected_platform,
                )

    def test_template_tag_uses_generic_setting_load_api(self):
        request = SimpleNamespace()
        settings = SimpleNamespace(social_links=[])
        with patch.object(NavigationSettings, "load", return_value=settings) as load:
            self.assertEqual(get_navigation_social_links({"request": request}), [])
        load.assert_called_once_with(request)


class SocialLinkBlockTests(SimpleTestCase):
    """验证后台不能保存非HTTP(S)社交链接。"""

    def test_block_rejects_non_http_scheme(self):
        block = SocialLinkBlock()

        with self.assertRaises(ValidationError):
            block.clean({"url": "mailto:admin@example.com", "label": "邮箱"})

    def test_navigation_settings_rejects_normalized_duplicate_urls(self):
        settings = NavigationSettings(
            social_links=[
                {"type": "social_link", "value": {"url": "https://github.com/anc1996", "label": ""}},
                {"type": "social_link", "value": {"url": "https://www.github.com/anc1996/", "label": ""}},
            ]
        )

        with self.assertRaises(ValidationError):
            settings.clean()
