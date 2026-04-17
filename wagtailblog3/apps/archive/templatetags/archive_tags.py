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
	
	# 创建URL
	for year in archive_tree:
		archive_tree[year]['url'] = reverse('archive:year_archive', args=[year])
		for month in archive_tree[year]['months']:
			archive_tree[year]['months'][month]['url'] = reverse(
				'archive:month_archive',
				args=[year, month]
			)
	
	return {
		'archive_tree': archive_tree,
		'current_year': current_year,
		'current_month': current_month,
		'request': context.get('request'),
		'always_show_months': True,  # 新增这个变量，并设置为 True
	}

# 用于转换日期格式的过滤器
@register.filter
def get_item(dictionary, key):
    """允许在模板中通过变量访问字典的键值"""
    
    # 检查字典是否有get方法
    if hasattr(dictionary, 'get'):
        return dictionary.get(key)
    return None