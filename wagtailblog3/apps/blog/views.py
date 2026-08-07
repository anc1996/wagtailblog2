# 博客应用的接口和作者视图
from urllib.parse import urlencode, urlsplit

from django.http import JsonResponse
from django.template.loader import render_to_string
from django.urls import reverse
from wagtail.models import Page
from wagtail.search.backends import get_search_backend
from django.views.decorators.http import require_POST
from django.shortcuts import get_object_or_404
from django.db import models
from django.core.paginator import Paginator
from django.views.generic import ListView, DetailView

from blog.models import (
	Author,
	BlogIndexPage,
	BlogPage,
	BLOG_INDEX_DEFAULT_SORT_PRIMARY,
	BLOG_INDEX_DEFAULT_SORT_SECONDARY,
	BLOG_INDEX_ITEMS_PER_PAGE,
)


AUTHOR_POSTS_PER_PAGE = 10


def get_client_ip(request):
	"""获取客户端IP地址"""
	x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
	if x_forwarded_for:
		ip = x_forwarded_for.split(',')[0]
	else:
		ip = request.META.get('REMOTE_ADDR')
	return ip


@require_POST
def toggle_reaction(request, page_id):
	"""
	处理用户反应 (点赞/喜爱等) - 优化版
	返回全量计数，便于前端直接覆盖更新
	"""
	page = get_object_or_404(Page, id=page_id)
	
	# 同时兼容表单和 JSON 请求，确保不同前端调用方式得到同一反应类型。
	reaction_type_id = request.POST.get('reaction_type') or request.POST.get('reaction_id')
	
	if not reaction_type_id:
		import json
		try:
			data = json.loads(request.body)
			reaction_type_id = data.get('reaction_type') or data.get('reaction_id')
		except:
			pass
	
	if not reaction_type_id:
		return JsonResponse({'error': '缺少反应类型ID'}, status=400)
	
	from .models import ReactionType, Reaction
	reaction_type = get_object_or_404(ReactionType, id=reaction_type_id)
	
	# 登录用户按用户 ID 识别；匿名用户确保有会话，再按会话键识别。
	user = request.user if request.user.is_authenticated else None
	if not user and not request.session.session_key:
		request.session.save()
	session_key = request.session.session_key if not user else None
	
	ip = get_client_ip(request)
	
	# 一个主体对同一页面只保留一条反应，后续点击表现为取消或切换。
	if user:
		existing = Reaction.objects.filter(page=page, user=user).first()
	else:
		existing = Reaction.objects.filter(page=page, session_key=session_key).first()
	
	action = ''
	
	# 按当前反应是否存在以及类型是否相同，分别执行删除、更新或新增。
	if existing:
		if existing.reaction_type_id == int(reaction_type_id):
			# 点击了同一个 -> 取消
			existing.delete()
			action = 'removed'
		else:
			# 点击了不同的 -> 切换
			existing.reaction_type = reaction_type
			existing.save()
			action = 'changed'
	else:
		# 没有反应 -> 新增
		Reaction.objects.create(
			page=page,
			reaction_type=reaction_type,
			user=user,
			session_key=session_key,
			ip_address=ip
		)
		action = 'added'
	
	# 返回该页面所有反应类型的完整计数，让前端一次性刷新所有按钮。
	reaction_counts_query = Reaction.objects.filter(page=page).values('reaction_type').annotate(
		count=models.Count('id'))
	
	# 转换为 {反应类型 ID: 数量}，缺失类型由前端按 0 处理。
	counts = {r['reaction_type']: r['count'] for r in reaction_counts_query}
	
	return JsonResponse({
		'success': True,
		'action': action,
		'counts': counts,  # 返回包含所有按钮计数的字典
		'current_reaction_id': int(reaction_type_id)
	})


def get_reaction_counts(request, page_id):
	"""获取页面的反应计数"""
	page = get_object_or_404(Page, id=page_id)
	
	# 先取得完整类型列表，确保没有任何反应记录的类型也会出现在响应中。
	from .models import ReactionType
	reaction_types = ReactionType.objects.all().order_by('display_order')
	
	# 获取该页面的反应计数
	from .models import Reaction
	reaction_counts = Reaction.objects.filter(page=page).values(
		'reaction_type'
	).annotate(
		count=models.Count('id')
	)
	
	# 转换为字典格式
	counts = {r['reaction_type']: r['count'] for r in reaction_counts}
	
	# 构建完整响应
	result = []
	for rt in reaction_types:
		result.append({
			'id': rt.id,
			'name': rt.name,
			'icon': rt.icon,
			'count': counts.get(rt.id, 0)
		})
	
	# 登录用户和匿名会话使用与切换接口相同的识别规则。
	user_reaction = None
	if request.user.is_authenticated:
		reaction = Reaction.objects.filter(
			page=page,
			user=request.user
		).first()
	else:
		if not request.session.session_key:
			request.session.save()
		reaction = Reaction.objects.filter(
			page=page,
			session_key=request.session.session_key
		).first()
	
	if reaction:
		user_reaction = reaction.reaction_type_id
	
	return JsonResponse({
		'reactions': result,
		'user_reaction': user_reaction
	})

def test_search_backend(request):
	"""测试 CustomSearchBackend 搜索功能"""
	query = request.GET.get('q', '')
	if not query:
		return JsonResponse({'error': '请提供搜索关键词'})
	
	# 获取搜索后端
	search_backend = get_search_backend()
	
	# 执行搜索 - 移除可能不兼容的参数
	search_results = search_backend.search(
		query,
		Page.objects.live(),
		operator='or'  # 使用更通用的参数
	)
	
	# 格式化结果
	results = []
	for page in search_results:
		results.append({
			'id': page.id,
			'title': page.title,
			'url': page.url if hasattr(page, 'url') else None,
			'type': page.specific_class.__name__
		})
	
	return JsonResponse({
		'query': query,
		'results_count': len(results),
		'results': results
	})


class AuthorListView(ListView):
	"""
	显示作者列表，支持搜索和分页。
	"""
	model = Author
	template_name = 'blog/author_list.html'
	context_object_name = 'authors'
	paginate_by = 10  # 每页显示 10 位作者
	
	def get_queryset(self):
		# 过滤在数据库层完成，分页器只读取当前页的作者。
		queryset = super().get_queryset()
		search_query = self.request.GET.get('q')  # 获取搜索参数 'q'
		page_number = self.request.GET.get('page')
		
		if search_query:
			# 如果有搜索参数，则根据姓名过滤
			queryset = queryset.filter(name__icontains=search_query)
		# 分页
		
		
		
		return queryset.order_by('name')  # 按姓名排序
	
	def get_context_data(self, **kwargs):
		# 获取上下文数据
		context = super().get_context_data(**kwargs)
		# 将搜索查询传递给模板，以便在搜索框中显示
		context['search_query'] = self.request.GET.get('q', '')
		return context


def get_blog_index_canonical_url(*, page, context, request):
	"""Return a same-origin URL for the normalized index-listing state."""
	params = {}
	if context['search_query']:
		params['search'] = context['search_query']
	if context['start_date']:
		params['start_date'] = context['start_date']
	if context['end_date']:
		params['end_date'] = context['end_date']
	if (
		context['sort_primary'] != BLOG_INDEX_DEFAULT_SORT_PRIMARY
		or context['sort_secondary'] != BLOG_INDEX_DEFAULT_SORT_SECONDARY
	):
		params['sort_primary'] = context['sort_primary']
		params['sort_secondary'] = context['sort_secondary']
	if context['page_obj'].number > 1:
		params['page'] = context['page_obj'].number

	page_url = page.get_url(request=request) or page.url or '/'
	page_path = urlsplit(page_url).path or '/'
	return f'{page_path}?{urlencode(params)}' if params else page_path


def blog_index_results_api(request, pk):
	"""Return a server-rendered index-listing fragment as JSON."""
	if request.method != 'GET':
		response = JsonResponse(
			{
				'ok': False,
				'error': {
					'code': 'method_not_allowed',
					'message': '仅支持 GET 请求。',
				},
			},
			status=405,
		)
		response['Allow'] = 'GET'
		response['Cache-Control'] = 'private, no-store'
		return response

	page = BlogIndexPage.objects.live().public().filter(pk=pk).first()
	if page is None:
		response = JsonResponse(
			{
				'ok': False,
				'error': {
					'code': 'blog_index_not_found',
					'message': '未找到该博客索引页。',
				},
			},
			status=404,
		)
		response['Cache-Control'] = 'private, no-store'
		return response

	context = page.get_listing_context(request.GET)
	response = JsonResponse(
		{
			'ok': True,
			'data': {
				'filters': {
					'search': context['search_query'],
					'start_date': context['start_date'],
					'end_date': context['end_date'],
					'sort_primary': context['sort_primary'],
					'sort_secondary': context['sort_secondary'],
				},
				'result_count': context['total_results'],
				'html': render_to_string(
					'blog/partials/_blog_index_results.html',
					{'page': page, **context},
					request=request,
				),
				'pagination': {
					'page': context['page_obj'].number,
					'page_size': BLOG_INDEX_ITEMS_PER_PAGE,
					'total_pages': context['page_obj'].paginator.num_pages,
					'has_previous': context['page_obj'].has_previous(),
					'has_next': context['page_obj'].has_next(),
				},
				'canonical_url': get_blog_index_canonical_url(
					page=page,
					context=context,
					request=request,
				),
			},
		}
	)
	response['Cache-Control'] = 'private, no-store'
	return response


def get_author_posts_context(*, author, query_params):
	"""Build the public, paginated author-post context used by HTML and JSON views."""
	search_query = (query_params.get('q') or '').strip()
	all_posts = (
		BlogPage.objects.live()
		.public()
		.filter(authors=author)
		.select_related('featured_image')
		.prefetch_related('tags')
	)
	posts = all_posts
	if search_query:
		posts = posts.filter(title__icontains=search_query)
	posts = posts.order_by('-date', '-pk')

	paginator = Paginator(posts, AUTHOR_POSTS_PER_PAGE)
	page_obj = paginator.get_page(query_params.get('page'))

	return {
		'blog_posts': page_obj.object_list,
		'page_obj': page_obj,
		'paginator': paginator,
		'is_paginated': page_obj.has_other_pages(),
		'total_posts': paginator.count,
		'author_post_count': all_posts.count() if search_query else paginator.count,
		'search_query': search_query,
	}


def get_author_posts_canonical_url(*, author, context):
	"""Return the normalized server-rendered page URL for browser history."""
	params = {}
	if context['search_query']:
		params['q'] = context['search_query']
	if context['page_obj'].number > 1:
		params['page'] = context['page_obj'].number

	url = reverse('blog:author_detail', kwargs={'pk': author.pk})
	return f'{url}?{urlencode(params)}' if params else url


def author_posts_api(request, pk):
	"""Return the author article-list fragment for progressive enhancement."""
	if request.method != 'GET':
		response = JsonResponse(
			{
				'ok': False,
				'error': {
					'code': 'method_not_allowed',
					'message': '仅支持 GET 请求。',
				},
			},
			status=405,
		)
		response['Allow'] = 'GET'
		response['Cache-Control'] = 'private, no-store'
		return response

	author = Author.objects.filter(pk=pk).first()
	if author is None:
		response = JsonResponse(
			{
				'ok': False,
				'error': {
					'code': 'author_not_found',
					'message': '未找到该作者。',
				},
			},
			status=404,
		)
		response['Cache-Control'] = 'private, no-store'
		return response

	context = get_author_posts_context(author=author, query_params=request.GET)
	fragment_context = {'author': author, **context}
	response = JsonResponse(
		{
			'ok': True,
			'data': {
				'query': context['search_query'],
				'author_post_count': context['author_post_count'],
				'result_count': context['total_posts'],
				'html': render_to_string(
					'blog/partials/_author_post_results.html',
					fragment_context,
					request=request,
				),
				'pagination': {
					'page': context['page_obj'].number,
					'page_size': AUTHOR_POSTS_PER_PAGE,
					'total_pages': context['paginator'].num_pages,
					'has_previous': context['page_obj'].has_previous(),
					'has_next': context['page_obj'].has_next(),
				},
				'canonical_url': get_author_posts_canonical_url(
					author=author,
					context=context,
				),
			},
		}
	)
	response['Cache-Control'] = 'private, no-store'
	return response


class AuthorDetailView(DetailView):
	"""
	显示单个作者的详细信息及其发表的文章。
	"""
	model = Author
	template_name = 'blog/author_detail.html'
	context_object_name = 'author'
	paginate_by = AUTHOR_POSTS_PER_PAGE
	
	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)
		author = self.object
		context.update(
			get_author_posts_context(author=author, query_params=self.request.GET)
		)
		
		return context
