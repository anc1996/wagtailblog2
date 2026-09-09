# 首页模型

from django.utils import timezone
from django.db import models
from django.db.models import Sum  # 导入 Sum

from wagtail.admin.panels import FieldPanel, MultiFieldPanel
from wagtail.models import Page
from wagtail.search import index
from wagtail.fields import RichTextField

# 确保导入路径正确
from blog.models import BlogIndexPage, BlogPage, Author,BlogTagIndexPage


class HomePage(Page):
	# body 是一个 RichTextField，一种特殊的 Wagtail 字段。blank=True 表示这个字段不是必须的，可以留空。你可以使用任何 Django 的核心字段。

	body = RichTextField(blank=True)  # 主页的正文内容

	date = models.DateField("发布日期", default=timezone.now) # 发布日期

	banner_image = models.ForeignKey(
		'blog.BlogImage',  # 使用项目自定义的图片模型
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name='+'
	)  # 特色图片

	# 添加首屏行动号召区域相关字段
	hero_text = models.CharField(
		blank=True,
		max_length=255,
		help_text="为网站撰写简介"
	)  # 首屏展示文本

	hero_cta = models.CharField(
		blank=True,
		verbose_name="Hero CTA",
		max_length=255,
		help_text="在行动动员按钮上显示的文本"
	)  # 行动号召按钮文本

	hero_cta_link = models.ForeignKey(
		'wagtailcore.Page',
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name='+',
		verbose_name="主召 CTA 链接",
		help_text="选择要链接到行动号召的页面"
	)  # 行动号召链接到的页面

	# content_panels 定义后台编辑界面的字段和布局。
	content_panels = Page.content_panels + [
		# 将首屏区域的相关字段分组，便于编辑者集中配置。
		MultiFieldPanel(
			[
				FieldPanel('banner_image'),
				FieldPanel('hero_text'),
				FieldPanel('hero_cta'),
				FieldPanel('hero_cta_link'),
			],
			heading="英雄区",  # 后台编辑界面的分组标题
		),
		FieldPanel('date'),  # 发布日期
		FieldPanel('body'),  # <-- 添加特色图片到编辑面板
	]

	search_fields = Page.search_fields + [
		index.SearchField('hero_text', boost=2),
		index.SearchField('hero_cta'),
		index.SearchField('body'),
	]

	# 在首页上下文中提供访问量最高的文章和文章索引页。
	def get_context(self, request, *args, **kwargs):

		context = super().get_context(request, *args, **kwargs)

		# 聚合访问统计后排序，避免在模板中逐篇计算访问量。
		# 性能优化（P1）：增加 select_related 与 prefetch_related，杜绝卡片渲染中的分类与封面二次 SQL 查询
		popular_posts = list(
			BlogPage.objects.live().public().select_related(
				'featured_image'
			).prefetch_related(
				'categories'
			).annotate(
				total_views=Sum('view_counts__count')
			).order_by('-total_views')[:5]
		)

		if popular_posts:
			# 批量预取父级专栏（BlogIndexPage）信息，杜绝在模板循环中触发 N+1 SQL 查询
			parent_paths = {p.path[:-Page.steplen] for p in popular_posts if len(p.path) > Page.steplen}
			parents_by_path = {
				page.path: page
				for page in Page.objects.filter(path__in=parent_paths).specific()
			}
			for post in popular_posts:
				first_cat = post.categories.all()[0] if post.categories.all() else None
				parent_path = post.path[:-Page.steplen] if len(post.path) > Page.steplen else ''
				parent_page = parents_by_path.get(parent_path)

				if first_cat:
					post.category_label = first_cat.name
					post.category_url = parent_page.url if parent_page else post.url
				elif parent_page and parent_page.id != self.id:
					post.category_label = parent_page.title
					post.category_url = parent_page.url
				else:
					post.category_label = "博客"
					post.category_url = post.url

		context['popular_posts'] = popular_posts

		# 获取首页目录下的文章索引页，供首页文章区域使用。
		# 性能优化（P1/P2）：保持 QuerySet 延迟求值特性，避免在 Python 中提前触发 SQL，配合模板片段缓存完全跳过查询
		context['blog_indexs'] = self.get_children().live().public().type(BlogIndexPage).specific().select_related('featured_image')

		return context

	class Meta:
		verbose_name = "首页"
		verbose_name_plural = "首页"
