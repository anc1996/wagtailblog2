# home/models.py

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
		'blog.BlogImage',  # <-- 使用你自定义的 BlogImage 模型
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name='+'
	)  # 特色图片
	
	# 添加 CTA 相关的字段
	hero_text = models.CharField(
		blank=True,
		max_length=255,
		help_text="为网站撰写简介"
	)  # 英雄区文本
	
	hero_cta = models.CharField(
		blank=True,
		verbose_name="Hero CTA",
		max_length=255,
		help_text="在行动动员按钮上显示的文本"
	)  # CTA 按钮文本
	
	hero_cta_link = models.ForeignKey(
		'wagtailcore.Page',
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name='+',
		verbose_name="主召 CTA 链接",
		help_text="选择要链接到行动号召的页面"
	)  # CTA 链接到的页面
	
	# content_panels 定义了编辑界面的功能和布局。将字段添加到 content_panels 让你可以在 Wagtail 后台编辑它们。
	content_panels = Page.content_panels + [
		# 使用 MultiFieldPanel 将 Hero 区的字段分组
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
	
	# 修改此方法以获取最热门的5篇文章
	def get_context(self, request, *args, **kwargs):
		
		context = super().get_context(request, *args, **kwargs)
		
		# 获取所有 BlogPage，并聚合 PageViewCount 中的 count 字段作为总访问量
		# 热门文章-我们按总访问量（total_views）降序排序，并取前5篇
		context['popular_posts'] =BlogPage.objects.live().public().annotate(
			total_views=Sum('view_counts__count')  # 假设 PageViewCount 中关联 BlogPage 的 related_name 是 'view_counts'
			# 并且 PageViewCount 有一个 'count' 字段记录访问次数
		).order_by('-total_views')[:5]
		
		# 改获取homepage目录下的所有 BlogIndexPage，用于轮播
		blog_indexs = self.get_children().live().public().type(BlogIndexPage).specific()
		if blog_indexs:
			context['blog_indexs'] = blog_indexs
		
		return context
	
	class Meta:
		verbose_name = "首页"
		verbose_name_plural = "首页"