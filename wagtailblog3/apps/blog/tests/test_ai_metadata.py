import json
from pathlib import Path

from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from wagtail.admin.panels import FieldPanel, TitleFieldPanel

from content_ai.models import BlogMetadataPromptTemplate

from blog.ai_metadata import (
    MetadataGenerationError,
    MetadataResponseError,
    MetadataConfigurationError,
    OpenAIResponsesClient,
    extract_body_context,
    generate_metadata,
    validate_suggestion,
)


class FakeResponsesClient:
    def __init__(self, output):
        self.output = output
        self.calls = []

    def generate(self, *, instructions, content):
        self.calls.append({"instructions": instructions, "content": content})
        return self.output


class BlogMetadataTests(SimpleTestCase):
    def test_blog_page_uses_native_metadata_panels_without_wagtail_ai_settings(self):
        from django.conf import settings
        from base.models import FormPage
        from blog.models import BlogPage

        def field_names(panels):
            names = []
            for panel in panels:
                if isinstance(panel, FieldPanel):
                    names.append(panel.field_name)
                names.extend(field_names(getattr(panel, "children", [])))
            return names

        names = field_names([*BlogPage.content_panels, *BlogPage.promote_panels])

        self.assertTrue({"title", "tags", "intro", "seo_title", "search_description"}.issubset(names))
        self.assertIsInstance(BlogPage.content_panels[1], TitleFieldPanel)
        self.assertTrue(BlogPage.content_panels[1].apply_if_live)
        self.assertNotIn("wagtail_ai", settings.INSTALLED_APPS)
        self.assertFalse(hasattr(settings, "WAGTAIL_AI"))
        for field_name in ("intro", "thank_you_text", "confirmation_email_text"):
            self.assertNotIn("ai", FormPage._meta.get_field(field_name).features)

    def test_metadata_script_uses_project_owned_editor_context(self):
        script = Path(__file__).resolve().parents[3] / "static" / "blog" / "js" / "blog_editor_context.js"
        source = script.read_text(encoding="utf-8")

        self.assertIn("window.blogEditorContext", source)
        self.assertNotIn("window.wagtailAI", source)
        self.assertNotIn("ContextProvider", source)

    def test_extract_body_context_uses_supported_blocks_without_ids(self):
        body = [
            {"id": "secret-id", "type": "rich_text", "value": json.dumps({"blocks": [{"text": "架构设计"}]})},
            {"type": "markdown_block", "value": "# 标题\n\n[链接](https://example.test)"},
            {"type": "image_block", "value": 42},
        ]

        context = extract_body_context(body)

        self.assertIn("架构设计", context)
        self.assertIn("标题", context)
        self.assertIn("链接", context)
        self.assertNotIn("secret-id", context)
        self.assertNotIn("42", context)

    def test_extract_body_context_rejects_empty_or_invalid_body(self):
        with self.assertRaises(MetadataGenerationError):
            extract_body_context([])
        with self.assertRaises(MetadataGenerationError):
            extract_body_context({})

    def test_validate_suggestion_deduplicates_tags(self):
        suggestion = validate_suggestion(
            {"title": "Python 架构实践", "intro": "介绍服务分层与测试策略。", "tags": ["Python", "python", "Django", "测试"]}
        )

        self.assertEqual(suggestion.tags, ["Python", "Django", "测试"])

    def test_validate_suggestion_accepts_string_description_alias(self):
        suggestion = validate_suggestion(
            {"title": "有效标题", "description": "模型使用 description 字段时仍可生成简介。", "tags": ["Python", "Django", "测试"]}
        )

        self.assertEqual(suggestion.intro, "模型使用 description 字段时仍可生成简介。")

    def test_validate_suggestion_rejects_invalid_tag_count(self):
        with self.assertRaises(MetadataResponseError):
            validate_suggestion({"title": "有效标题", "intro": "有效简介", "tags": ["Python"]})

    def test_generate_metadata_uses_fake_responses_client(self):
        client = FakeResponsesClient(
            json.dumps({"title": "Django 服务分层", "intro": "介绍可维护的服务设计。", "tags": ["Django", "Python", "架构"]})
        )

        suggestion = generate_metadata(
            [{"type": "markdown_block", "value": "# Django 服务\n本文介绍服务层。"}],
            client=client,
        )

        self.assertEqual(suggestion.title, "Django 服务分层")
        self.assertEqual(len(client.calls), 1)
        self.assertIn("本文介绍服务层", client.calls[0]["content"])

    def test_generate_metadata_retries_one_invalid_response(self):
        class RetryingClient:
            def __init__(self):
                self.calls = 0

            def generate(self, *, instructions, content):
                self.calls += 1
                if self.calls == 1:
                    return json.dumps({"title": "有效标题", "intro": None, "tags": ["Python", "Django", "测试"]})
                return json.dumps({"title": "有效标题", "intro": "可用于验证一次有限重试。", "tags": ["Python", "Django", "测试"]})

        client = RetryingClient()

        suggestion = generate_metadata(
            [{"type": "markdown_block", "value": "# 测试正文\n用于验证重试。"}],
            client=client,
        )

        self.assertEqual(suggestion.intro, "可用于验证一次有限重试。")
        self.assertEqual(client.calls, 2)

    def test_generate_metadata_accepts_a_valid_third_response(self):
        class RetryingClient:
            def __init__(self):
                self.calls = 0

            def generate(self, *, instructions, content):
                self.calls += 1
                if self.calls < 3:
                    return json.dumps({"title": "有效标题", "intro": None, "tags": ["Python", "Django", "测试"]})
                return json.dumps({"title": "有效标题", "intro": "第三次响应通过校验。", "tags": ["Python", "Django", "测试"]})

        client = RetryingClient()

        suggestion = generate_metadata(
            [{"type": "markdown_block", "value": "# 测试正文\n用于验证有限重试。"}],
            client=client,
        )

        self.assertEqual(suggestion.intro, "第三次响应通过校验。")
        self.assertEqual(client.calls, 3)

    @override_settings(
        AI_METADATA_API_KEY="test-key",
        AI_METADATA_BASE_URL="https://api.example.test/v1",
        AI_METADATA_MODEL="test-model",
        AI_METADATA_RESPONSE_STORAGE=True,
    )
    def test_responses_client_rejects_response_storage(self):
        with self.assertRaises(MetadataConfigurationError):
            OpenAIResponsesClient()


class BlogMetadataViewTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.template = BlogMetadataPromptTemplate.objects.create(
            name="测试模板",
            description="测试用模板",
            title_prompt="突出正文主题",
            intro_prompt="面向读者概括正文",
            tags_prompt="提取具体技术主题",
            is_active=True,
        )

    @override_settings(AI_METADATA_API_KEY="", AI_METADATA_BASE_URL="", AI_METADATA_MODEL="")
    def test_unconfigured_service_returns_safe_error(self):
        from blog.ai_metadata_views import _generate_blog_metadata

        request = self.factory.post(
            "/admin/blog/ai/generate-metadata/",
            data=json.dumps({
                "body": [{"type": "markdown_block", "value": "测试正文"}],
                "template_id": self.template.pk,
            }),
            content_type="application/json",
        )

        response = _generate_blog_metadata(request)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.content)["error"]["code"], "service_unconfigured")

    def test_disabled_template_is_rejected_before_model_call(self):
        self.template.is_active = False
        self.template.save(update_fields=["is_active"])
        from blog.ai_metadata_views import _generate_blog_metadata

        request = self.factory.post(
            "/admin/blog/ai/generate-metadata/",
            data=json.dumps({
                "body": [{"type": "markdown_block", "value": "测试正文"}],
                "template_id": self.template.pk,
            }),
            content_type="application/json",
        )

        response = _generate_blog_metadata(request)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)["error"]["code"], "invalid_prompt_template")

    def test_invalid_json_returns_bad_request(self):
        from blog.ai_metadata_views import _generate_blog_metadata

        request = self.factory.post(
            "/admin/blog/ai/generate-metadata/",
            data="not-json",
            content_type="application/json",
        )

        response = _generate_blog_metadata(request)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)["error"]["code"], "invalid_json")
