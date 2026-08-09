# 博客应用的 Wagtail 后台扩展

import logging

from django.conf import settings
from django.templatetags.static import static
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.db.models import Sum
from django.shortcuts import render, get_object_or_404
from django.urls import path, reverse
from django.contrib import messages
from django.views.generic.edit import UpdateView
from django.utils.decorators import method_decorator
from wagtail import hooks

from wagtail.models import Page
from wagtail.admin.views.reports import ReportView
from wagtail.admin.menu import MenuItem
from wagtail.admin.ui.tables import Column, Table
from wagtail.admin.auth import require_admin_access
from wagtail.admin.rich_text.editors.draftail import features as draftail_features
from wagtail.admin.rich_text.converters.html_to_contentstate import InlineStyleElementHandler
from wagtail.snippets.models import register_snippet

from .admin import PageViewSnippetViewSet, TagsSnippetViewSet
from .models import PageViewCount
from .forms import PageViewCountForm
from .admin_image_upload import upload_vditor_image
from . import widget_adapters  # noqa: F401

# 设置日志记录器
logger = logging.getLogger(__name__)


@hooks.register("register_admin_urls")
def register_vditor_image_upload_url():
	return [
		path(
			"blog/vditor/images/upload/",
			upload_vditor_image,
			name="blog_vditor_image_upload",
		),
	]


# 在 Wagtail 后台所有页面加载图标样式。
@hooks.register("insert_global_admin_css")
def global_admin_css():
	"""
	在 Wagtail 后台所有页面加载 Font Awesome 5 的 CSS。
	项目的其他后台组件仍使用其中的图标类。
	"""
	# 通过 static() 获取带缓存版本的路径，再用 format_html 安全插入 HTML 属性。
	return format_html('<link rel="stylesheet" href="{}">', static("css/all.min.css"))


@hooks.register('insert_global_admin_css')
def fix_wagtail_ai_zindex():
	"""
	架构师前端补丁：
	解决 Wagtail-AI 的星号魔法棒被 RichTextField 工具栏物理遮挡导致无法点击的问题。
	"""
	# 这段样式没有用户输入，使用 mark_safe 输出固定的后台补丁。
	return mark_safe(
		"<style>\n"
		".w-field--draftail_rich_text_area .wai-dropdown {\n"
		"    z-index: 100 !important;\n"
		"    top: -5px !important;\n"
		"    right: 0px !important;\n"
		"}\n"
		"</style>"
	)


# 为编辑器加载诊断和 AI 上下文脚本。
@hooks.register('insert_editor_js')
def editor_js():
	"""添加JavaScript支持到编辑器"""
	return format_html(
		'<script src="{}"></script>\n'
		'<script src="{}"></script>\n'
		'<script src="{}" data-blog-rich-text-image-paste '
		'data-upload-url="{}" data-max-image-size="{}"></script>',
		f"{static('blog/js/editor-enhancements.js')}?blog_editor=20260801.2",
		static('blog/js/wagtail_ai_context.js'),
		f"{static('blog/js/rich_text_image_paste.js')}?blog_editor=20260804.1",
		reverse('blog_vditor_image_upload'),
		getattr(settings, 'WAGTAILIMAGES_MAX_UPLOAD_SIZE', 10 * 1024 * 1024),
	)
	# 编辑页面后清空 body 字段，避免正文再次写入 MySQL。
@hooks.register('after_edit_page')
def after_edit_page(request, page):
	"""编辑页面后清空body字段，避免存入MySQL"""
	if hasattr(page, 'mongo_content_id') and hasattr(page, 'body'):
		try:
			# save() 已负责 Mongo 持久化，这里只修正关系库中的轻量占位值。
			if page.id:
				type(page).objects.filter(id=page.id).update(body=[])
		except Exception as e:
			logger.error(f"清空页面body字段时出错: {e}", exc_info=True)


# 页面统计报告
@hooks.register('register_admin_urls')
def register_page_views_report_url():
	@method_decorator(require_admin_access, name='dispatch')
	class PageViewsReportView(ReportView):
		template_name = 'wagtailadmin/reports/page_views_report.html'
		title = "页面访问统计"
		header_icon = "site"
		
		def get_queryset(self):
			# 只查询存在聚合记录的页面，并在数据库层汇总总访问量和唯一访问量。
			queryset = Page.objects.filter(
				id__in=PageViewCount.objects.values('page').distinct()
			).annotate(
				total_views=Sum('view_counts__count'),
				total_unique_views=Sum('view_counts__unique_count')
			)
			
			# 标题筛选保持在数据库层执行，避免报告页加载全部页面后再过滤。
			search_query = self.request.GET.get('q', '')
			if search_query:
				# 标题搜索
				queryset = queryset.filter(title__icontains=search_query)
			
			# 只接受数字范围，忽略非法输入，避免把无效参数传给 ORM。
			min_views = self.request.GET.get('min_views', '')
			max_views = self.request.GET.get('max_views', '')
			
			if min_views and min_views.isdigit():
				queryset = queryset.filter(total_views__gte=int(min_views))
			
			if max_views and max_views.isdigit():
				queryset = queryset.filter(total_views__lte=int(max_views))
			
			# 日期过滤使用页面首次发布时间，与报告中的时间维度保持一致。
			start_date = self.request.GET.get('start_date', '')
			end_date = self.request.GET.get('end_date', '')
			
			if start_date:
				queryset = queryset.filter(first_published_at__gte=start_date)
			
			if end_date:
				queryset = queryset.filter(first_published_at__lte=end_date)
			
			# 排序字段采用白名单，防止用户直接控制 ORM order_by 表达式。
			sort_by = self.request.GET.get('sort', '-total_views')
			valid_sort_fields = ['total_views', '-total_views', 'total_unique_views',
			                     '-total_unique_views', 'first_published_at', '-first_published_at', 'title', '-title']
			
			if sort_by in valid_sort_fields:
				queryset = queryset.order_by(sort_by)
			else:
				queryset = queryset.order_by('-total_views')
			
			return queryset
		
		def get_table(self, parent_context=None):
			# 报告模板自行读取分页对象，这里提供符合 Wagtail 报告接口的空表格结构。
			headers = [
				Column('title', label="页面标题"),
				Column('total_views', label="总访问量"),
				Column('total_unique_views', label="唯一访问量"),
			]
			return Table(headers, [], caption=self.title)
		
		def get_context_data(self, **kwargs):
			context = super().get_context_data(**kwargs)
			
			# 保留 Wagtail 已构造的分页对象，并把筛选条件传给模板。
			paginator = context['paginator']
			page_obj = context['page_obj']
			
			# 添加搜索信息
			context['search_query'] = self.request.GET.get('q', '')
			context['min_views'] = self.request.GET.get('min_views', '')
			context['max_views'] = self.request.GET.get('max_views', '')
			context['start_date'] = self.request.GET.get('start_date', '')
			context['end_date'] = self.request.GET.get('end_date', '')
			context['sort'] = self.request.GET.get('sort', '-total_views')
			
			# 移除旧页码后重新编码其余参数，翻页时不会丢失当前筛选条件。
			query_params = self.request.GET.copy()
			if 'page' in query_params:
				del query_params['page']
			context['query_string'] = query_params.urlencode()
			
			return context
	
	@method_decorator(require_admin_access, name='dispatch')
	class PageViewCountEditView(UpdateView):
		model = PageViewCount
		form_class = PageViewCountForm
		template_name = 'wagtailadmin/reports/edit_page_view_count.html'
		pk_url_kwarg = 'count_id'
		
		def get_context_data(self, **kwargs):
			context = super().get_context_data(**kwargs)
			context['page_title'] = f"编辑 {self.object.page.title} 的访问数据"
			return context
		
		def form_valid(self, form):
			response = super().form_valid(form)
			messages.success(self.request, f"已成功更新 {self.object.page.title} 的访问统计")
			return response
		
		def get_success_url(self):
			return reverse('page_views_report')
	
	@require_admin_access
	def page_view_counts_for_page(request, page_id):
		"""查看某个页面的所有访问统计记录"""
		page = get_object_or_404(Page, id=page_id)
		counts = PageViewCount.objects.filter(page=page).order_by('-date')
		
		return render(request, 'wagtailadmin/reports/page_view_counts_detail.html', {
			'page': page,
			'counts': counts,
			'total_views': counts.aggregate(Sum('count'))['count__sum'] or 0,
			'total_unique_views': counts.aggregate(Sum('unique_count'))['unique_count__sum'] or 0,
		})
	
	return [
		path('reports/page-views/', PageViewsReportView.as_view(), name='page_views_report'),
		path('reports/page-views/edit/<int:count_id>/', PageViewCountEditView.as_view(), name='edit_page_view_count'),
		path('reports/page-views/page/<int:page_id>/', page_view_counts_for_page, name='page_view_counts_detail'),
	]


# 注册自定义报告菜单项
@hooks.register('register_reports_menu_item')
def register_page_views_report_menu_item():
	return MenuItem(
		label="页面访问统计",
		url='/admin/reports/page-views/',
		icon_name="site",
		order=700
	)


# 注册“下划线”富文本功能。
@hooks.register('register_rich_text_features')
def register_underline_feature(features):
	"""
	注册 `underline` (下划线) 功能.
	它使用 `UNDERLINE` Draft.js 类型，并存储为 `<u>` 标签。
	"""
	feature_name = 'underline'
	type_ = 'UNDERLINE'
	tag = 'u'  # HTML 下划线标签
	
	# 1. 配置工具栏按钮
	control = {
		'type': type_,
		'label': 'U',
		'description': '下划线',
		# 不额外设置 style，Draftail 已经提供 UNDERLINE 的默认样式。
	}
	
	# 第二步：注册 Draftail 工具栏插件。
	features.register_editor_plugin(
		'draftail', feature_name, draftail_features.InlineStyleFeature(control)
	)
	
	# 第三步：声明 Draft.js 数据与 HTML 之间的转换规则。
	db_conversion = {
		'from_database_format': {tag: InlineStyleElementHandler(type_)},
		'to_database_format': {'style_map': {type_: tag}},
	}
	
	# 第四步：把转换规则登记到 contentstate 转换器。
	features.register_converter_rule('contentstate', feature_name, db_conversion)




# 注册这个视图集，生成管理 UI
register_snippet(TagsSnippetViewSet)
register_snippet(PageViewSnippetViewSet)
