# 评论模板标签和安全 Markdown 过滤器。
from django import template
from django.utils.safestring import mark_safe
from django.utils.html import conditional_escape
from comments.models import BlogPageComment, CommentReaction
from django.core.paginator import Paginator

from comments.markdown import render_comment_markdown, render_reply_markdown as render_reply

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

	# 只查询当前页评论的反应，避免把整篇文章的点赞记录加载到内存。
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
	"""渲染评论使用的受限且经过清理的 Markdown 方言。"""
	return mark_safe(render_comment_markdown(value))


@register.filter(name='render_reply_markdown')
def render_reply_markdown(value, replied_to_username):
	"""渲染回复正文；结构化的 @提及由模板单独展示。"""
	return mark_safe(render_reply(value, replied_to_username))


@register.filter(name='escape_attr')
def escape_attr(value):
	"""将原始 Markdown 转义后再放入 HTML 数据属性。"""
	return conditional_escape(value or "")


@register.filter(name='get_item')
def get_item(dictionary, key):
	"""按字符串键读取模板中传入的字典。"""
	return dictionary.get(str(key))
