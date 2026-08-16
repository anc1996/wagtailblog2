import json

from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import BlogMetadataPromptTemplate
from .services.blog_metadata import PromptTemplateError, generate_blog_metadata
from .wagtail_hooks import PromptTemplatePermissionPolicy


class FakeResponsesClient:
	def __init__(self):
		self.calls = []

	def generate(self, *, instructions, content):
		self.calls.append({"instructions": instructions, "content": content})
		return json.dumps({
			"title": "模板生成标题",
			"intro": "模板生成简介",
			"tags": ["Django", "Python", "测试"],
		})


class BlogMetadataPromptTemplateTests(TestCase):
	def create_template(self, **kwargs):
		values = {
			"name": "测试模板",
			"title_prompt": "标题突出正文主题",
			"intro_prompt": "简介面向读者概括正文",
			"tags_prompt": "标签提取具体技术主题",
			"is_active": True,
		}
		values.update(kwargs)
		return BlogMetadataPromptTemplate.objects.create(**values)

	def test_prompt_change_increments_version(self):
		template = self.create_template()
		self.assertEqual(template.version, 1)
		template.title_prompt = "改用更具体的标题"
		template.save()
		template.refresh_from_db()
		self.assertEqual(template.version, 2)

	def test_service_uses_one_selected_template(self):
		template = self.create_template()
		client = FakeResponsesClient()

		suggestion = generate_blog_metadata(
			[{"type": "markdown_block", "value": "Django 正文"}],
			template_id=template.pk,
			client=client,
		)

		self.assertEqual(suggestion.title, "模板生成标题")
		self.assertIn(template.title_prompt, client.calls[0]["instructions"])
		self.assertIn(template.intro_prompt, client.calls[0]["instructions"])
		self.assertIn(template.tags_prompt, client.calls[0]["instructions"])

	def test_service_rejects_inactive_template(self):
		template = self.create_template(is_active=False)

		with self.assertRaises(PromptTemplateError):
			generate_blog_metadata(
				[{"type": "markdown_block", "value": "Django 正文"}],
				template_id=template.pk,
				client=FakeResponsesClient(),
			)

	def test_active_template_requires_all_prompt_fields(self):
		template = self.create_template(tags_prompt="")
		with self.assertRaises(ValidationError):
			template.full_clean()

	def test_only_superuser_can_manage_prompt_templates(self):
		user_model = get_user_model()
		editor = user_model.objects.create_user(username="editor", is_staff=True)
		administrator = user_model.objects.create_superuser(
			username="administrator",
			email="administrator@example.test",
			password="test-password",
		)
		policy = PromptTemplatePermissionPolicy(BlogMetadataPromptTemplate)

		self.assertFalse(policy.user_has_permission(editor, "change"))
		self.assertTrue(policy.user_has_permission(administrator, "change"))
