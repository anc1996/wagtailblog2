# wagtailblog3/apps/comments/templatetags/comment_tags.py
from django import template
from django.utils.safestring import mark_safe
from comments.models import BlogPageComment, CommentReaction
from django.core.paginator import Paginator

from wagtailmarkdown.utils import render_markdown as wagtail_render_markdown

register = template.Library()


@register.inclusion_tag('comments/tags/comment_block.html', takes_context=True)
def render_comments(context, page):
	"""渲染评论区块 - 修复版本"""
	request = context['request']
	
	# 获取一级评论（按热门排序）
	comments = BlogPageComment.objects.filter(
		page=page,
		parent__isnull=True,
		status='approved'
	).select_related('author_user').order_by('-like_count', '-created_at')
	
	# 分页
	paginator = Paginator(comments, 20)  # 每页20条
	comments_page = paginator.get_page(1)  # 默认第一页
	
	# 获取用户反应状态
	user_reactions = {}
	if request.user.is_authenticated:
		reactions = CommentReaction.objects.filter(
			comment__in=comments_page,
			user=request.user
		)
		user_reactions = {r.comment_id: r.reaction_type for r in reactions}
	
	# 评论总数
	comment_count = BlogPageComment.objects.filter(
		page=page,
		status='approved'
	).count()
	
	return {
		'page': page,
		'comments': comments_page,
		'comment_count': comment_count,
		'paginator': paginator,
		'user_reactions': user_reactions,
		'sort_by': 'hot',  # 默认热门排序
		'request': request,
		'user': request.user,
		'is_authenticated': request.user.is_authenticated,  # 新增：明确传递认证状态
	}


@register.filter(name='render_markdown')
def render_markdown(value):
	"""
	将 Markdown 文本渲染为安全的 HTML，使用 wagtailmarkdown 的渲染器。
	"""
	if not value:
		return ""
	# 调用 wagtailmarkdown 提供的渲染函数
	html = wagtail_render_markdown(value)
	return mark_safe(html)


@register.filter(name='get_item')
def get_item(dictionary, key):
	return dictionary.get(str(key))