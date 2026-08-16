from dataclasses import dataclass

from ..models import BlogMetadataPromptTemplate
from blog.ai_metadata import MetadataGenerationError, generate_metadata


class PromptTemplateError(MetadataGenerationError):
	"""模板不存在、停用或未通过完整性校验。"""


@dataclass(frozen=True)
class PromptTemplateSummary:
	id: int
	name: str
	description: str
	version: int

	def as_dict(self):
		return {
			"id": self.id,
			"name": self.name,
			"description": self.description,
			"version": self.version,
		}


def list_active_blog_metadata_templates():
	return [
		PromptTemplateSummary(
			id=template.pk,
			name=template.name,
			description=template.description,
			version=template.version,
		)
		for template in BlogMetadataPromptTemplate.objects.filter(is_active=True)
		.order_by("name", "pk")
	]


def _get_active_template(template_id):
	try:
		template = BlogMetadataPromptTemplate.objects.get(pk=template_id)
	except (BlogMetadataPromptTemplate.DoesNotExist, TypeError, ValueError) as error:
		raise PromptTemplateError("请选择有效的博客 AI 提示词。") from error
	if not template.is_active:
		raise PromptTemplateError("所选博客 AI 提示词已停用，请重新选择。")
	if not all((template.title_prompt.strip(), template.intro_prompt.strip(), template.tags_prompt.strip())):
		raise PromptTemplateError("所选博客 AI 提示词不完整，请联系管理员修正。")
	return template


def generate_blog_metadata(body, *, language="zh-hans", template_id=None, client=None):
	template = _get_active_template(template_id)
	prompts = {
		"title": template.title_prompt.strip(),
		"intro": template.intro_prompt.strip(),
		"tags": template.tags_prompt.strip(),
	}
	return generate_metadata(
		body,
		language=language,
		client=client,
		prompt_template=prompts,
	)
