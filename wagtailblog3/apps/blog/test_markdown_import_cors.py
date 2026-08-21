from pathlib import Path

from django.test import SimpleTestCase
from django.urls import reverse


class MarkdownImportCorsTests(SimpleTestCase):
    # 实际 Bearer 响应会经过认证查询，允许该回归测试访问测试数据库。
    databases = {"default"}

    """锁定浏览器脚本预检的跨源认证边界。"""

    endpoint = "/blog/api/markdown-import/userscript/prepare/"

    def preflight(self, origin):
        return self.client.options(
            self.endpoint,
            HTTP_ORIGIN=origin,
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="authorization,content-type",
            HTTP_ACCESS_CONTROL_REQUEST_PRIVATE_NETWORK="true",
        )

    def test_allowed_article_origin_receives_limited_cors_and_pna_headers(self):
        response = self.preflight("https://www.cnblogs.com")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Access-Control-Allow-Origin"], "https://www.cnblogs.com")
        self.assertIn("POST", response["Access-Control-Allow-Methods"])
        self.assertIn("authorization", response["Access-Control-Allow-Headers"].lower())
        self.assertEqual(response["Access-Control-Allow-Private-Network"], "true")
        self.assertNotIn("Access-Control-Allow-Credentials", response)

    def test_new_article_origins_receive_the_same_limited_cors_headers(self):
        origins = (
            "https://www.qstheory.cn",
            "http://theory.people.com.cn",
            "https://theory.people.com.cn",
            "https://www.12371.cn",
            "http://opinion.people.com.cn",
            "https://finance.people.com.cn",
            "http://society.people.com.cn",
            "https://cpc.people.com.cn",
            "http://politics.people.com.cn",
            "http://www.qizhiwang.org.cn",
            "https://tougao.12371.cn",
            "https://www.xuexi.cn",
            "https://www.rmlt.com.cn",
            "http://www.banyuetan.org",
            "http://www.dangjian.cn",
        )

        for origin in origins:
            with self.subTest(origin=origin):
                response = self.preflight(origin)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Access-Control-Allow-Origin"], origin)
                self.assertNotIn("Access-Control-Allow-Credentials", response)

    def test_localized_userscript_endpoint_receives_limited_cors_and_pna_headers(self):
        endpoint = reverse("blog:markdown_import_userscript_prepare")
        self.assertEqual(endpoint, "/zh-hans/blog/api/markdown-import/userscript/prepare/")

        response = self.client.options(
            endpoint,
            HTTP_ORIGIN="https://www.cnblogs.com",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="authorization,content-type",
            HTTP_ACCESS_CONTROL_REQUEST_PRIVATE_NETWORK="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Access-Control-Allow-Origin"], "https://www.cnblogs.com")
        self.assertEqual(response["Access-Control-Allow-Private-Network"], "true")

    def test_localized_session_create_endpoint_receives_the_same_limited_cors_headers(self):
        endpoint = reverse("blog:markdown_import_session_create")
        self.assertEqual(endpoint, "/zh-hans/blog/api/markdown-import/sessions/")

        response = self.client.options(
            endpoint,
            HTTP_ORIGIN="https://www.cnblogs.com",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="authorization,content-type",
            HTTP_ACCESS_CONTROL_REQUEST_PRIVATE_NETWORK="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Access-Control-Allow-Origin"], "https://www.cnblogs.com")
        self.assertEqual(response["Access-Control-Allow-Private-Network"], "true")

    def test_untrusted_or_http_origin_does_not_receive_cors_headers(self):
        for origin in ("https://evil.example.test", "http://www.cnblogs.com"):
            response = self.preflight(origin)

            self.assertNotIn("Access-Control-Allow-Origin", response)
            self.assertNotIn("Access-Control-Allow-Credentials", response)

    def test_people_http_origin_receives_cors_on_actual_bearer_response(self):
        endpoint = reverse("blog:markdown_import_userscript_prepare")
        response = self.client.post(
            endpoint,
            data="{}",
            content_type="application/json",
            HTTP_ORIGIN="http://opinion.people.com.cn",
            HTTP_AUTHORIZATION="Bearer mdimp_invalid",
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["Access-Control-Allow-Origin"], "http://opinion.people.com.cn")

    def test_userscript_uses_fetch_for_bearer_blog_requests(self):
        script_path = Path(__file__).resolve().parents[2] / "static/vendor/Script/downlaod_markdown.js"
        source = script_path.read_text(encoding="utf-8")
        start = source.index("async function requestBlog")
        end = source.index("\n        function setAbsoluteImageSources", start)
        request_blog = source[start:end]

        self.assertIn("fetch(target.url", request_blog)
        self.assertIn("credentials: 'omit'", request_blog)
        self.assertIn("redirect: 'error'", request_blog)
        self.assertNotIn("gmRequest(", request_blog)

    def test_userscript_uses_the_localized_markdown_import_api_path(self):
        script_path = Path(__file__).resolve().parents[2] / "static/vendor/Script/downlaod_markdown.js"
        source = script_path.read_text(encoding="utf-8")

        self.assertIn("new URL(`/zh-hans${path}`, `${origin}/`)", source)

    def test_userscript_renders_a_safe_bootstrap_error_notice(self):
        script_path = Path(__file__).resolve().parents[2] / "static/vendor/Script/downlaod_markdown.js"
        source = script_path.read_text(encoding="utf-8")

        self.assertIn("function renderBootstrapError(error)", source)
        self.assertIn("role: 'alert'", source)
        self.assertIn("text: `博客导入脚本未启动：${message}`", source)
        self.assertIn("renderBootstrapError(error);", source)

    def test_userscript_creates_the_entry_before_extracting_article_content(self):
        script_path = Path(__file__).resolve().parents[2] / "static/vendor/Script/downlaod_markdown.js"
        source = script_path.read_text(encoding="utf-8")
        start = source.index("async function runModernApp()")
        root_creation = source.index("const root = createElement", start)

        self.assertNotIn("articleData()", source[start:root_creation])
        self.assertIn("function loadArticleData()", source[root_creation:])

    def test_userscript_renders_the_site_field_without_self_insertion(self):
        script_path = Path(__file__).resolve().parents[2] / "static/vendor/Script/downlaod_markdown.js"
        source = script_path.read_text(encoding="utf-8")
        start = source.index("async function runModernApp()")
        app_source = source[start:]

        self.assertIn("id: 'zuihuitao-blog-site'", app_source)
        self.assertIn(".before(siteInput)", app_source)
        self.assertNotIn("form.insertBefore(form.lastChild, siteInput)", app_source)

    def test_userscript_isolates_its_form_and_runtime_version(self):
        script_path = Path(__file__).resolve().parents[2] / "static/vendor/Script/downlaod_markdown.js"
        source = script_path.read_text(encoding="utf-8")

        self.assertIn("// @version      0.3.16", source)
        self.assertIn("const blogImportVersion = '0.3.16';", source)
        self.assertIn("#zuihuitao-blog-import form{display:block!important}", source)

    def test_userscript_supports_the_three_new_article_containers(self):
        script_path = Path(__file__).resolve().parents[2] / "static/vendor/Script/downlaod_markdown.js"
        source = script_path.read_text(encoding="utf-8")

        expected_matches = (
            "// @match        *://www.qstheory.cn/*",
            "// @match        *://theory.people.com.cn/*",
            "// @match        *://www.12371.cn/*",
        )
        expected_interfaces = (
            '{ "host": "www.qstheory.cn", "el": ".highlight", "cut_str": "" }',
            '{ "host": "theory.people.com.cn", "el": "#rm_txt_zw", "cut_str": " --" }',
            '{ "host": "www.12371.cn", "el": "#font_area", "cut_str": "_" }',
        )

        for metadata in expected_matches:
            self.assertIn(metadata, source)
        for interface in expected_interfaces:
            self.assertIn(interface, source)
        self.assertNotIn('{ "host": "www.qstheory.cn", "el": "body"', source)
        self.assertNotIn('{ "host": "theory.people.com.cn", "el": "body"', source)
        self.assertNotIn('{ "host": "www.12371.cn", "el": "body"', source)

    def test_userscript_supports_people_and_media_article_containers(self):
        script_path = Path(__file__).resolve().parents[2] / "static/vendor/Script/downlaod_markdown.js"
        source = script_path.read_text(encoding="utf-8")

        expected_matches = (
            "// @match        *://opinion.people.com.cn/*",
            "// @match        *://finance.people.com.cn/*",
            "// @match        *://society.people.com.cn/*",
            "// @match        *://cpc.people.com.cn/*",
            "// @match        *://politics.people.com.cn/*",
            "// @match        *://www.qizhiwang.org.cn/*",
            "// @match        *://tougao.12371.cn/gaojian.php*",
            "// @match        *://www.xuexi.cn/lgpage/detail/*",
            "// @match        *://www.rmlt.com.cn/*",
            "// @match        *://www.banyuetan.org/*",
            "// @match        *://www.dangjian.cn/*",
        )
        expected_interfaces = (
            '{ "host": "opinion.people.com.cn", "el": "#rm_txt_zw", "fallback_els": [".rm_txt_con.cf"], "cut_str": " --" }',
            '{ "host": "finance.people.com.cn", "el": "#rm_txt_zw", "cut_str": " --" }',
            '{ "host": "society.people.com.cn", "el": "#rm_txt_zw", "cut_str": " --" }',
            '{ "host": "cpc.people.com.cn", "el": "#rm_txt_zw", "cut_str": " --" }',
            '{ "host": "politics.people.com.cn", "el": "#rm_txt_zw", "cut_str": " --" }',
            '{ "host": "www.qizhiwang.org.cn", "el": ".w1200.flag-text-con.clearfix", "cut_str": "--旗帜网" }',
            '{ "host": "tougao.12371.cn", "el": "#font_area", "cut_str": "_" }',
            '{ "host": "www.xuexi.cn", "el": ".render-detail-article-content", "title_el": ".render-detail-title", "cut_str": "" }',
            '{ "host": "www.rmlt.com.cn", "el": ".article-content", "cut_str": "_" }',
            '{ "host": "www.banyuetan.org", "el": "#detail_content", "cut_str": "-半月谈" }',
            '{ "host": "www.dangjian.cn", "el": "#tex.article", "cut_str": "" }',
        )

        for metadata in expected_matches:
            self.assertIn(metadata, source)
        for interface in expected_interfaces:
            self.assertIn(interface, source)
        self.assertIn("const titleElement = match.title_el ? document.querySelector(match.title_el) : null;", source)
        self.assertIn("titleElement?.textContent || document.title", source)
        self.assertIn("const selectors = [match.el, ...(match.fallback_els || [])].filter(Boolean);", source)
        self.assertIn(".rm_txt_con.cf", source)
        self.assertNotIn("el: \"body\"", source)

    def test_userscript_keeps_preflight_when_only_the_destination_changes(self):
        script_path = Path(__file__).resolve().parents[2] / "static/vendor/Script/downlaod_markdown.js"
        source = script_path.read_text(encoding="utf-8")
        start = source.index("async function runModernApp()")
        app_source = source[start:]

        self.assertIn("function updatePreparedDestination()", app_source)
        self.assertIn("preparedImport.payload.target_parent_id = targetParentId;", app_source)
        self.assertIn("preparedImport.idempotencyKey = uuidV4();", app_source)
        self.assertIn("destination.addEventListener('change', updatePreparedDestination);", app_source)
        self.assertNotIn("[siteInput, tokenInput, destination, remoteImages", app_source)
        self.assertIn("'/blog/api/markdown-import/duplicate-titles/'", app_source)

    def test_userscript_requires_preflight_and_confirmation_before_session_writes(self):
        script_path = Path(__file__).resolve().parents[2] / "static/vendor/Script/downlaod_markdown.js"
        source = script_path.read_text(encoding="utf-8")

        self.assertIn("createDraft.disabled = true;", source)
        self.assertIn("function clearPreparedImport()", source)
        self.assertIn("function sessionManifest(prepared)", source)
        self.assertIn("async function waitForDraft(next, session)", source)
        self.assertIn("window.confirm(", source)
        self.assertIn("'/blog/api/markdown-import/sessions/'", source)
        self.assertIn("/finalize/", source)
        self.assertIn("正在组装未发布草稿", source)
