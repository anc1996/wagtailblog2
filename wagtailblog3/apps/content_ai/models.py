from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from wagtail.admin.panels import FieldPanel, MultiFieldPanel


class BlogMetadataPromptTemplate(models.Model):
	"""供博客元数据建议使用的、可人工维护的提示词模板。"""

	name = models.CharField("模板名称", max_length=100, unique=True)
	description = models.CharField("用途说明", max_length=500, blank=True)
	title_prompt = models.TextField("标题提示词", max_length=2000)
	intro_prompt = models.TextField("简介提示词", max_length=2000)
	tags_prompt = models.TextField("标签提示词", max_length=2000)
	is_active = models.BooleanField("启用", default=False)
	version = models.PositiveIntegerField(
		"版本",
		default=1,
		validators=[MinValueValidator(1)],
	)
	created_at = models.DateTimeField("创建时间", auto_now_add=True)
	updated_at = models.DateTimeField("更新时间", auto_now=True)
	created_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		verbose_name="创建人",
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="content_ai_prompt_templates_created",
	)
	updated_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		verbose_name="最后修改人",
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name="content_ai_prompt_templates_updated",
	)

	panels = [
		MultiFieldPanel(
			[
				FieldPanel("name"),
				FieldPanel("description"),
				FieldPanel("is_active"),
				FieldPanel("version", read_only=True),
			],
			heading="基本信息",
		),
		MultiFieldPanel(
			[
				FieldPanel("title_prompt"),
				FieldPanel("intro_prompt"),
				FieldPanel("tags_prompt"),
			],
			heading="博客元数据提示词",
		),
	]

	class Meta:
		verbose_name = "博客 AI 提示词"
		verbose_name_plural = "博客 AI 提示词"
		ordering = ["name", "pk"]

	def __str__(self):
		return f"{self.name}（v{self.version}）"

	def clean(self):
		if self.is_active:
			empty_fields = {
				field: "启用前必须填写完整提示词。"
				for field in ("title_prompt", "intro_prompt", "tags_prompt")
				if not getattr(self, field, "").strip()
			}
			if empty_fields:
				raise ValidationError(empty_fields)

	def save(self, *args, **kwargs):
		if self.pk:
			previous = type(self).objects.filter(pk=self.pk).values(
				"title_prompt", "intro_prompt", "tags_prompt", "version"
			).first()
			if previous and any(
				getattr(self, field) != previous[field]
				for field in ("title_prompt", "intro_prompt", "tags_prompt")
			):
				self.version = previous["version"] + 1
				if kwargs.get("update_fields") is not None:
					kwargs["update_fields"] = set(kwargs["update_fields"]) | {"version"}
		super().save(*args, **kwargs)
