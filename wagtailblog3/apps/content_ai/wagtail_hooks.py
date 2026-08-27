from wagtail import hooks
from wagtail.admin.ui.tables import Column
from wagtail.permission_policies import ModelPermissionPolicy
from wagtail.permissions import register_permission_policy
from wagtail.snippets.views.snippets import SnippetViewSet

from .models import BlogMetadataPromptTemplate


class PromptTemplatePermissionPolicy(ModelPermissionPolicy):
	"""提示词属于管理配置，仅 superuser 可以维护。"""

	def user_has_permission(self, user, action):
		return user.is_superuser and super().user_has_permission(user, action)


# Wagtail 8 要求自定义权限策略显式注册，确保策略不依赖 ViewSet 的弃用属性。
register_permission_policy(
	BlogMetadataPromptTemplate,
	PromptTemplatePermissionPolicy(BlogMetadataPromptTemplate),
	exact_class=True,
)


class BlogMetadataPromptTemplateViewSet(SnippetViewSet):
	model = BlogMetadataPromptTemplate
	icon = "form"
	menu_label = "博客 AI 提示词"
	menu_name = "blog-ai-prompt-templates"
	add_to_admin_menu = True
	list_display = ["name", "version", "is_active", Column("updated_at", label="更新时间", sort_key="updated_at")]
	search_fields = ("name", "description", "title_prompt", "intro_prompt", "tags_prompt")
	ordering = ("name", "pk")

@hooks.register("register_admin_viewset")
def register_blog_metadata_prompt_template_viewset():
	return BlogMetadataPromptTemplateViewSet()


def _record_prompt_template_actor(request, template):
	if not isinstance(template, BlogMetadataPromptTemplate):
		return
	if not request.user.is_authenticated:
		return
	fields = {"updated_by": request.user}
	if template.created_by_id is None:
		fields["created_by"] = request.user
	BlogMetadataPromptTemplate.objects.filter(pk=template.pk).update(**fields)


@hooks.register("after_create_snippet")
def record_prompt_template_creator(request, instance):
	_record_prompt_template_actor(request, instance)


@hooks.register("after_edit_snippet")
def record_prompt_template_editor(request, instance):
	_record_prompt_template_actor(request, instance)
