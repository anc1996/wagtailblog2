# blog/templatetags/blog_tags.py
from collections import Counter
from django import template
from wagtail.models import Site  # Site 用于获取站点根页面

# 确保导入你项目中实际使用的页面模型
from blog.models import BlogPage, BlogTagIndexPage, Author  # 假设这些是你已有的导入

register = template.Library()

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
	获取并显示最热门的6个博客标签，使用 Counter 方法。
	"""
	blog_tag_index_page_instance = None
	top_tags_final_list = []
	
	try:
		blog_tag_index_page_instance = BlogTagIndexPage.objects.live().public().first()
		all_blog_tags = []
		# 确保 BlogPage.objects.live().public() 能正确执行
		for page_item in BlogPage.objects.live().public():
			all_blog_tags.extend(list(page_item.tags.all()))
		
		if all_blog_tags:
			tag_counts = Counter(all_blog_tags)
			popular_tags_with_counts = tag_counts.most_common(6)
			for tag_instance, count_num in popular_tags_with_counts:
				tag_instance.num_times = count_num
				top_tags_final_list.append(tag_instance)
	except Exception as e:
		# print(f"Error in top_tags_sidebar: {e}")
		pass
	
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
			
			# 判断与当前页面的关系 #
			if site_root_page_obj and current_page_from_context:  #
				# 确保 current_page_from_context 是一个有效的 Page 对象 #
				if hasattr(current_page_from_context, 'pk'):  #
					is_current = (site_root_page_obj.pk == current_page_from_context.pk)  #
					is_ancestor = False  #
					# 确保 site_root_page_obj 有 is_ancestor_of 方法 #
					if hasattr(site_root_page_obj, 'is_ancestor_of'):  #
						is_ancestor = site_root_page_obj.is_ancestor_of(current_page_from_context)  #
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
	
	children_of_parent = parent_for_children.get_children().live().in_menu().specific()  # 获取父页面的子页面 #
	
	for child_page in children_of_parent:  # 遍历每个子页面 #
		has_sub_items = child_page.get_children().live().in_menu().exists()  #
		
		is_current = False  #
		is_ancestor = False  #
		
		if current_page_from_context:  # 确保 current_page_from_context 存在
			if hasattr(current_page_from_context, 'pk'):  #
				is_current = (child_page.pk == current_page_from_context.pk)  #
				if hasattr(child_page, 'is_ancestor_of'):  #
					is_ancestor = child_page.is_ancestor_of(current_page_from_context)  #
		
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
    # 返回第一个处于“live”状态的 BlogTagIndexPage 页面对象
    # 如果不存在，则返回 None
    return BlogTagIndexPage.objects.live().first()
