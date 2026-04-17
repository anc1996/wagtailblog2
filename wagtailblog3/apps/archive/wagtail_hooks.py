# archive/wagtail_hooks.py
from wagtail import hooks
from wagtail.admin.menu import MenuItem
from django.urls import reverse, path
from django.shortcuts import render
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.utils.dateparse import parse_date
from django.http import JsonResponse
from blog.models import BlogPage
from .views import get_archive_data


@hooks.register('register_admin_menu_item')
def register_archive_menu_item():
	"""在 Wagtail 管理后台注册归档菜单项"""
	return MenuItem(
		'博客归档',
		reverse('archive_admin_dashboard'),
		icon_name='folder-open-inverse',
		order=950
	)


@hooks.register('register_admin_urls')
def register_archive_admin_urls():
	"""注册归档管理的 URL"""
	
	def archive_dashboard(request):
		"""归档管理仪表板视图"""
		# 获取博客文章总数
		total_posts_count = BlogPage.objects.live().count()
		
		# 获取归档统计分页参数
		archive_page_number = request.GET.get('archive_page', 1)
		
		# 获取归档统计数据（用于左侧边栏）
		archive_data_dict = get_archive_data()
		
		# 将归档数据转换为列表并按年份降序排序
		archive_list = [
			{'year': year, 'data': data}
			for year, data in sorted(archive_data_dict.items(), key=lambda x: x[0], reverse=True)
		]
		
		# 对归档统计进行分页（每页10个年份）
		archive_paginator = Paginator(archive_list, 10)
		try:
			paginated_archive = archive_paginator.page(archive_page_number)
		except PageNotAnInteger:
			paginated_archive = archive_paginator.page(1)
		except EmptyPage:
			paginated_archive = archive_paginator.page(archive_paginator.num_pages)
		
		# 重新构建分页后的归档数据字典
		archive_data_for_display = {
			item['year']: item['data']
			for item in paginated_archive.object_list
		}
		
		# 预取相关对象以优化查询性能
		posts_prefetch = ['authors', 'categories', 'tags']
		
		# 默认显示最近10篇发布的文章（不分页）
		context_posts = BlogPage.objects.live().order_by('-date').prefetch_related(*posts_prefetch)[:10]
		
		# 构建模板上下文
		context = {
			'archive_data_for_sidebar': archive_data_for_display,
			'archive_pagination': paginated_archive,
			'total_posts_count': total_posts_count,
			'posts_to_display': context_posts,
			'is_date_filter_active': False,
			'is_paginated_list': False,
			'selected_start_date': '',
			'selected_end_date': '',
			'range_1_to_12': range(1, 13),
		}
		
		return render(request, 'archive/admin/dashboard.html', context)
	
	def archive_stats_ajax(request):
		"""归档统计的AJAX接口"""
		archive_page_number = request.GET.get('archive_page', 1)
		
		# 获取归档统计数据
		archive_data_dict = get_archive_data()
		
		# 将归档数据转换为列表并按年份降序排序
		archive_list = [
			{'year': year, 'data': data}
			for year, data in sorted(archive_data_dict.items(), key=lambda x: x[0], reverse=True)
		]
		
		# 对归档统计进行分页
		archive_paginator = Paginator(archive_list, 10)
		try:
			paginated_archive = archive_paginator.page(archive_page_number)
		except (PageNotAnInteger, EmptyPage):
			paginated_archive = archive_paginator.page(1)
		
		# 重新构建分页后的归档数据字典
		archive_data_for_display = {
			item['year']: item['data']
			for item in paginated_archive.object_list
		}
		
		# 渲染归档统计表格部分
		from django.template.loader import render_to_string
		html = render_to_string(
			'archive/admin/_archive_stats_table.html',
			{
				'archive_data_for_sidebar': archive_data_for_display,
				'archive_pagination': paginated_archive,
				'range_1_to_12': range(1, 13),
			},
			request=request
		)
		
		return JsonResponse({
			'html': html,
			'has_previous': paginated_archive.has_previous(),
			'has_next': paginated_archive.has_next(),
			'current_page': paginated_archive.number,
			'total_pages': paginated_archive.paginator.num_pages,
		})
	
	def posts_filter_ajax(request):
		"""文章筛选的AJAX接口"""
		start_date_str = request.GET.get('start_date')
		end_date_str = request.GET.get('end_date')
		page_number = request.GET.get('page', 1)
		
		posts_prefetch = ['authors', 'categories', 'tags']
		
		# 处理日期筛选
		if start_date_str and end_date_str:
			selected_start_date_obj = parse_date(start_date_str)
			selected_end_date_obj = parse_date(end_date_str)
			
			# 验证日期有效性
			if selected_start_date_obj and selected_end_date_obj and selected_start_date_obj <= selected_end_date_obj:
				# 查询指定日期范围内的文章
				date_filtered_posts_qs = BlogPage.objects.live().filter(
					date__range=(selected_start_date_obj, selected_end_date_obj)
				).order_by('-date').prefetch_related(*posts_prefetch)
				
				# 分页处理
				paginator = Paginator(date_filtered_posts_qs, 20)
				try:
					paginated_posts = paginator.page(page_number)
				except PageNotAnInteger:
					paginated_posts = paginator.page(1)
				except EmptyPage:
					paginated_posts = paginator.page(paginator.num_pages)
				
				# 渲染文章列表部分
				from django.template.loader import render_to_string
				html = render_to_string(
					'archive/admin/_posts_list_section.html',
					{
						'posts_to_display': paginated_posts,
						'is_date_filter_active': True,
						'is_paginated_list': True,
						'selected_start_date': start_date_str,
						'selected_end_date': end_date_str,
					},
					request=request
				)
				
				return JsonResponse({
					'html': html,
					'success': True,
					'total_count': paginator.count,
				})
		
		# 如果日期无效，返回错误
		return JsonResponse({
			'success': False,
			'error': '请选择有效的日期范围'
		})
	
	return [
		path('archive/dashboard/', archive_dashboard, name='archive_admin_dashboard'),
		path('archive/stats-ajax/', archive_stats_ajax, name='archive_stats_ajax'),
		path('archive/posts-filter-ajax/', posts_filter_ajax, name='posts_filter_ajax'),
	]