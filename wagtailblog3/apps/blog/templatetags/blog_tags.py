# 博客应用的模板标签
import logging
from collections import Counter
from django import template
from django.db.models import Count
from taggit.models import Tag
from django.core.exceptions import ValidationError
from django.utils.encoding import smart_str
from django.utils.html import format_html
from wagtail.models import Site  # Site 用于获取站点根页面

# 确保导入你项目中实际使用的页面模型
from blog.models import BlogPage, BlogTagIndexPage, Author  # 假设这些是你已有的导入
from blog.inline_title_renderer import InlineTitleRenderer

register = template.Library()
logger = logging.getLogger(__name__)


@register.simple_tag
def render_display_title(page):
	"""Render the native Wagtail Page.title as safe inline Markdown."""
	if page is None:
		return ""
	title = getattr(page, "title", "") or ""
	try:
		if getattr(page, "search_title_highlight", "") and getattr(page, "search_title_query", ""):
			rendered = InlineTitleRenderer.render_highlighted(
				title,
				page.search_title_query,
			)
		else:
			rendered = InlineTitleRenderer.render(title)
	except ValidationError:
		logger.warning(
			"invalid_markdown_title_fallback page_id=%s",
			getattr(page, "pk", None),
		)
		return format_html('<span class="markdown-title">{}</span>', title)
	has_math = 'class="arithmatex"' in str(rendered)
	if has_math:
		return format_html(
			'<span class="markdown-title" data-title-math="true">{}</span>',
			rendered,
		)
	return format_html('<span class="markdown-title">{}</span>', rendered)


@register.filter
def inline_title_text(value):
	"""Return the semantic plain-text form of a Markdown Page.title."""
	title = value if isinstance(value, str) else getattr(value, "title", value)
	title = title or ""
	try:
		return InlineTitleRenderer.plain_text(title)
	except ValidationError:
		return smart_str(title)

@register.simple_tag
def get_user_reaction(page, request):
	"""获取当前用户对页面的反应"""
	from blog.models import Reaction  # 假设 Reaction 模型在 blog.models 中

	if request.user.is_authenticated:
		reaction = Reaction.objects.filter(
			page=page,
			user=request.user
		).values_list('reaction_type_id', flat=True).first()
		return reaction
	elif request.session.session_key:
		reaction = Reaction.objects.filter(
			page=page,
			session_key=request.session.session_key
		).values_list('reaction_type_id', flat=True).first()
		return reaction
	return None


@register.filter
def specific_class_name(page):
	"""返回页面的具体类名"""
	return page.specific.__class__.__name__


@register.inclusion_tag('blog/tags/top_tags_sidebar.html', takes_context=True)
def top_tags_sidebar(context):
	"""
	获取并显示最热门的6个博客标签。
	架构优化（P1）：使用单条 ORM 反向聚合查询替代全量博客页面遍历（从 160 次 SQL 骤降至 1 次）。
	"""
	blog_tag_index_page_instance = None
	top_tags_final_list = []

	try:
		blog_tag_index_page_instance = BlogTagIndexPage.objects.live().public().first()
		# 核心优化：聚合统计绑定在已上线博客页面上的前6个标签，按关联次数倒序并保持名称排序稳定
		top_tags_final_list = list(
			Tag.objects.filter(
				blog_blogpagetag_items__content_object__live=True
			).annotate(
				num_times=Count('blog_blogpagetag_items')
			).order_by('-num_times', 'name')[:6]
		)
	except Exception as e:
		# 异常降级保护：标签统计异常时记录日志并保持列表为空，确保全页稳定渲染
		logger.error(f"top_tags_sidebar 聚合查询异常: {e}", exc_info=True)

	return {
		'top_tags': top_tags_final_list,
		'blog_tag_index_page': blog_tag_index_page_instance,
		'request': context.get('request'),
	}


@register.inclusion_tag('blog/tags/random_author_sidebar.html', takes_context=True)
def random_author_sidebar(context):
	"""获取并显示一个随机作者的信息。"""
	random_author_instance = Author.objects.order_by('?').first()

	return {
		'random_author': random_author_instance,
		'request': context.get('request'),
	}




@register.simple_tag(takes_context=True)
def get_site_root_details(context):  #
	"""
	获取当前站点的根页面，并判断其与当前页面的关系。
	返回一个包含 page 对象和 is_current_or_ancestor 布尔值的字典。
	"""  #
	request = context.get('request')  #
	current_page_from_context = context.get('page')  # 获取模板上下文中的 'page' 对象 #
	site_root_page_obj = None  # 修改变量名以区分 #
	is_current_or_ancestor_of_root = False  # 初始化布尔值 #

	if request:  # 确保 request 存在 #
		site = Site.find_for_request(request)  # 获取当前请求的站点 #
		if site and site.root_page:  #
			site_root_page_obj = site.root_page.specific  # 获取 specific 实例 #

			# 判断根页面是否就是当前页，或是否为当前页的祖先。
			if site_root_page_obj and current_page_from_context:  #
				# 只有具备主键的页面对象才参与关系判断。
				if hasattr(current_page_from_context, 'pk'):  #
					is_current = (site_root_page_obj.pk == current_page_from_context.pk)  #
					is_ancestor = False  #
					# 利用 Wagtail Treebeard 树路径判定祖先关系（纯内存比较，零 SQL 查询）
					if hasattr(current_page_from_context, 'is_descendant_of'):  #
						is_ancestor = current_page_from_context.is_descendant_of(site_root_page_obj)  #
					is_current_or_ancestor_of_root = is_current or is_ancestor  #

	return {  #
		'page_obj': site_root_page_obj,  # 修改键名以清晰 #
		'is_current_or_ancestor': is_current_or_ancestor_of_root  #
	}


@register.inclusion_tag('blog/tags/recursive_menu_level.html', takes_context=True)  #
def generate_menu_items(context, parent_for_children, current_page_from_context):  #
	"""
		为给定的 'parent_for_children' 页面的子页面生成菜单项数据。
		'current_page_from_context' 是网站上当前正在查看的页面。
		这个标签会渲染 'blog/tags/recursive_menu_level.html' 模板。
	"""
	menu_items_to_render = []  # 初始化菜单项列表

	if not parent_for_children:  #
		return {'menu_items_list': []}  # 如果没有父页面，返回空列表 #

	# 性能优化（P1）：使用通用 Page 查询替代 specific()，避免每个子页面触发多表联查
	children_of_parent = parent_for_children.get_children().live().in_menu()  # 获取父页面的子页面 #

	for child_page in children_of_parent:  # 遍历每个子页面 #
		# 性能优化：利用 Wagtail 树结构原生字段 numchild，若为 0 则直接跳过递归探测 SQL
		has_sub_items = (child_page.numchild > 0) and child_page.get_children().live().in_menu().exists()  #

		is_current = False  #
		is_ancestor = False  #

		if current_page_from_context:  # 确保 current_page_from_context 存在
			if hasattr(current_page_from_context, 'pk'):  #
				is_current = (child_page.pk == current_page_from_context.pk)  #
				# 利用 Wagtail Treebeard 树路径判定祖先关系（纯内存比较，零 SQL 查询）
				if hasattr(current_page_from_context, 'is_descendant_of'):  #
					is_ancestor = current_page_from_context.is_descendant_of(child_page)  #

		is_current_or_ancestor_val = is_current or is_ancestor  # 修改变量名 #

		menu_items_to_render.append({  #
			'page_object': child_page,  #
			'is_current_or_ancestor': is_current_or_ancestor_val,  #
			'has_dropdown': has_sub_items,  #
		})

	return {  #
		'menu_items_list': menu_items_to_render,  #
		'current_page_for_recursion': current_page_from_context,  #
		'request': context.get('request'),  #
	}

@register.simple_tag
def get_tag_index_page():
    """
    一个简单的模板标签，用于获取项目中第一个公开的 BlogTagIndexPage 实例。
    """
	# 返回第一个已发布的标签索引页；不存在时返回 None。
    return BlogTagIndexPage.objects.live().first()
