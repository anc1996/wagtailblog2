# archive/templatetags/archive_tags.py
from django import template
from django.db.models import Count
from django.db.models.functions import TruncYear, TruncMonth
from blog.models import BlogPage
from django.urls import reverse

register = template.Library()

@register.inclusion_tag('archive/tags/archive_sidebar.html', takes_context=True)
def archive_sidebar(context, current_year=None, current_month=None):
	"""生成归档侧边栏"""
	# 获取所有已发布的博客页面
	blog_pages = BlogPage.objects.live()

	# 按年份分组统计
	yearly_archives = blog_pages.annotate(
		year=TruncYear('date')
	).values('year').annotate(
		count=Count('id')
	).order_by('-year')

	# 按月份分组统计
	monthly_archives = blog_pages.annotate(
		year=TruncYear('date'),
		month=TruncMonth('date')
	).values('year', 'month').annotate(
		count=Count('id')
	).order_by('-year', '-month')

	# 组织成树形结构
	archive_tree = {}

	for item in yearly_archives:
		year = item['year'].year
		archive_tree[year] = {
			'count': item['count'],
			'months': {}
		}

	for item in monthly_archives:
		year = item['year'].year
		month = item['month'].month
		month_name = item['month'].strftime('%B')

		if year in archive_tree:
			archive_tree[year]['months'][month] = {
				'count': item['count'],
				'name': month_name,
				'display_name': f"{month}月"
			}

	# 创建 URL，并标记默认显示的最近年份。当前归档年份始终可见。
	total_posts = 0
	hidden_year_count = 0
	for index, year in enumerate(archive_tree):
		year_data = archive_tree[year]
		year_data['url'] = reverse('archive:year_archive', args=[year])
		year_data['is_initially_hidden'] = index >= 5 and year != current_year
		if year_data['is_initially_hidden']:
			hidden_year_count += 1
		total_posts += year_data['count']
		month_grid = []
		for month in range(1, 13):
			month_data = year_data['months'].get(month)
			if month_data:
				month_data['url'] = reverse(
					'archive:month_archive',
					args=[year, month]
				)
				month_grid.append({
					'month': month,
					'display_name': f'{month}月',
					'count': month_data['count'],
					'url': month_data['url'],
					'has_posts': True,
				})
			else:
				month_grid.append({
					'month': month,
					'display_name': f'{month}月',
					'count': 0,
					'url': None,
					'has_posts': False,
				})
		year_data['month_grid'] = month_grid

	years = list(archive_tree)
	return {
		'archive_tree': archive_tree,
		'archive_year_count': len(years),
		'archive_total_posts': total_posts,
		'archive_latest_year': years[0] if years else None,
		'archive_earliest_year': years[-1] if years else None,
		'hidden_year_count': hidden_year_count,
		'current_year': current_year,
		'current_month': current_month,
		'request': context.get('request'),
	}

# 用于转换日期格式的过滤器
@register.filter
def get_item(dictionary, key):
    """允许在模板中通过变量访问字典的键值"""

    # 检查字典是否有get方法
    if hasattr(dictionary, 'get'):
        return dictionary.get(key)
    return None
