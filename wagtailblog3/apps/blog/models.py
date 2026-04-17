# blog/models.py

import traceback, logging

from django.db.models.functions import Coalesce, Lower
from django.utils import timezone
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db import models
from django import forms
from django.db.models import Count, Sum, Subquery, OuterRef, F
from markdown import markdown
from django.conf import settings

from django.utils.html import strip_tags  # 用于去除HTML标签
from django.utils.safestring import mark_safe  # 用于标记HTML安全

from datetime import datetime, date
from modelcluster.fields import ParentalKey, ParentalManyToManyField
from modelcluster.contrib.taggit import ClusterTaggableManager

from taggit.models import TaggedItemBase, Tag

from wagtail.embeds.blocks import EmbedBlock
from wagtail.models import Page, Orderable
from wagtail.fields import StreamField, RichTextField
from wagtail.admin.panels import FieldPanel, MultiFieldPanel, InlinePanel
from wagtail.search import index

from wagtail.images.models import Image, AbstractImage, AbstractRendition
from wagtail.blocks import RichTextBlock, RawHTMLBlock
from wagtail.images.blocks import ImageChooserBlock
from wagtail.documents.blocks import DocumentChooserBlock
from wagtail.snippets.models import register_snippet
from wagtail.blocks.stream_block import StreamValue
from wagtail_ai.panels import AITitleFieldPanel, AIDescriptionFieldPanel

from wagtailmarkdown.blocks import MarkdownBlock
from wagtailcodeblock.blocks import CodeBlock

from blog.blocks import AudioBlock, VideoBlock, CustomTableBlock, MermaidBlock
from wagtailblog3.mongo import MongoManager
from wagtailblog3.mongodb import MongoDBStreamFieldAdapter

logger = logging.getLogger(__name__)


# 自定义图片模型
class BlogImage(AbstractImage):
	"""自定义博客图片模型"""
	caption = models.CharField(max_length=255, blank=True)
	admin_form_fields = Image.admin_form_fields + ('caption',)  # 添加caption字段到后台表单
	
	@property
	def default_alt_text(self):
		# 如果没有指定alt文本，使用caption作为替代
		return self.caption or self.title


class BlogRendition(AbstractRendition):
	"""博客图片渲染模型"""
	image = models.ForeignKey(
		'BlogImage',
		on_delete=models.CASCADE,
		related_name='renditions'
	)
	
	class Meta:
		# 确保每个图片的渲染是唯一的
		unique_together = (
			('image', 'filter_spec', 'focal_point_key'),
		)


# 博客标签模型
class BlogTagIndexPage(Page):
	"""
	页面用于展示按标签筛选的文章列表，或所有标签的列表。
	支持对文章标题（在特定标签下）或标签名称进行搜索和分页。
	"""
	subpage_types = []  # 通常标签索引页不应有子页面
	parent_page_types = ['wagtailcore.Page', 'home.HomePage', 'blog.BlogIndexPage']  # 根据你的实际情况调整
	subpage_types = ['blog.BlogIndexPage']  # 根据你的实际情况调整
	
	# 每页显示的标签显示数
	items_tag_page = 50
	# 每页显示的文章数
	items_per_page = 20
	
	def get_context(self, request, *args, **kwargs):
		context = super().get_context(request, *args, **kwargs)
		
		tag_slug_filter = request.GET.get('tag')  # ?tag=<slug>
		search_query = request.GET.get('q', '').strip()  # ?q=<query>
		page_number = request.GET.get('page')  # ?page=<number>
		
		start_date = request.GET.get('start_date', '')
		end_date = request.GET.get('end_date', '')
		
		context['search_query'] = search_query
		context['start_date'] = start_date
		context['end_date'] = end_date
		context['current_tag'] = None
		context['paged_items'] = None  # 将用于文章分页或标签分页
		context['mode'] = None  # "tag_detail" 或 "tag_list"
		
		if tag_slug_filter:
			# --- 模式 A: 标签详情模式 (查看特定标签下的文章) ---
			context['mode'] = "tag_detail"
			try:
				tag_object = Tag.objects.get(slug=tag_slug_filter)
				context['current_tag'] = tag_object
				
				# 获取该标签下的所有已发布、公开的文章
				articles_queryset = BlogPage.objects.live().public().filter(tags=tag_object)
				
				# 如果有文章标题搜索词 (q)，则进一步过滤
				if search_query:
					articles_queryset = articles_queryset.filter(title__icontains=search_query)
				
				if start_date:
					start_date_obj = datetime.strptime(start_date, '%Y-%m-%d').date()
					articles_queryset = articles_queryset.filter(date__gte=start_date_obj)
				
				if end_date:
					end_date_obj = datetime.strptime(end_date, '%Y-%m-%d').date()
					articles_queryset = articles_queryset.filter(date__lte=end_date_obj)
				
				articles_queryset = articles_queryset.order_by('-date')  # 或其他排序
				
				# 对文章列表进行分页
				paginator = Paginator(articles_queryset, self.items_per_page)
				try:
					context['paged_items'] = paginator.page(page_number)
				except PageNotAnInteger:
					context['paged_items'] = paginator.page(1)
				except EmptyPage:
					context['paged_items'] = paginator.page(paginator.num_pages)
			
			except Tag.DoesNotExist:
				context['paged_items'] = []  # 或者设置为空列表以避免错误
		else:
			# --- 模式 B: 标签列表模式 ---
			context['mode'] = "tag_list"
			
			# 1. 获取所有使用过的标签的基础查询集
			#    确保只获取那些至少被一篇 BlogPage 使用的标签 (live & public)
			active_blog_pages_tags_ids = BlogPage.objects.live().public().values_list('tags', flat=True).distinct()
			used_tags_queryset = Tag.objects.filter(pk__in=active_blog_pages_tags_ids)
			
			# 2. 应用标签名称搜索查询 (如果存在)
			if search_query:
				used_tags_queryset = used_tags_queryset.filter(name__icontains=search_query)
			
			used_tags_queryset = used_tags_queryset.order_by('name')
			
			# 3. 为每个标签附加文章数量
			tags_with_counts = []
			for tag_item in used_tags_queryset:
				# 确保计数也只计算 live 和 public 的文章
				count = BlogPage.objects.live().public().filter(tags=tag_item).count()
				if count > 0:  # 只显示实际有文章的标签
					tags_with_counts.append({'tag': tag_item, 'count': count})
			
			# 4. 对带有计数的标签列表进行分页
			paginator = Paginator(tags_with_counts, self.items_tag_page)
			try:
				context['paged_items'] = paginator.page(page_number)
			except PageNotAnInteger:
				context['paged_items'] = paginator.page(1)
			except EmptyPage:
				context['paged_items'] = paginator.page(paginator.num_pages)
		
		return context


# 标签模型
class BlogPageTag(TaggedItemBase):
	"""博客页面标签"""
	
	# TaggedItemBase 是一个抽象模型，用于定义标签与模型的关联关系。
	
	content_object = ParentalKey(
		'BlogPage',
		related_name='tagged_items',
		on_delete=models.CASCADE
	)  # 关联到BlogPage模型


# 博客分类
@register_snippet
class BlogCategory(models.Model):
	"""博客分类模型"""
	name = models.CharField(max_length=255)
	slug = models.SlugField(unique=True, max_length=80)
	
	panels = [
		FieldPanel('name'),
		FieldPanel('slug'),
	]
	
	def __str__(self):
		return self.name
	
	class Meta:
		verbose_name = "博客分类"
		verbose_name_plural = "博客分类"


# 博客索引页面
class BlogIndexPage(Page):
	"""博客索引页面"""
	
	date = models.DateField("发布日期", default=timezone.now)  # 添加日期字段
	intro = RichTextField("页面介绍", blank=True)
	
	featured_image = models.ForeignKey(
		'BlogImage',
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name='+'
	)  # 特色图片
	
	content_panels = Page.content_panels + [
		FieldPanel('date'),
		FieldPanel('intro'),
		FieldPanel('featured_image'),
	]
	
	def get_context(self, request):
		"""
		更新上下文。
		使用数据库注解(Annotation)实现对不同类型子页面的高性能筛选和排序，
		从根本上解决 FieldError。
		"""
		context = super().get_context(request)
		
		# --- 参数获取 ---
		search_query = request.GET.get('search', '')
		start_date_str = request.GET.get('start_date', '')
		end_date_str = request.GET.get('end_date', '')
		page_number = request.GET.get('page')
		sort_primary = request.GET.get('sort_primary', 'date_desc')
		sort_secondary = request.GET.get('sort_secondary', 'title_asc')
		
		# --- 核心优化：数据库注解 ---
		
		# 1. 为每种带 'date' 字段的子页面类型准备一个子查询
		#    OuterRef('pk') 指的是父查询（Page）的主键
		blog_page_date_subquery = Subquery(
			BlogPage.objects.filter(page_ptr_id=OuterRef('pk')).values('date')[:1]
		)
		blog_index_page_date_subquery = Subquery(
			BlogIndexPage.objects.filter(page_ptr_id=OuterRef('pk')).values('date')[:1]
		)
		
		# 2. 开始构建惰性查询集，并使用 annotate 添加虚拟字段
		child_pages = self.get_children().live().annotate(
			# a. 创建 'sort_date' 虚拟字段：
			#    使用 Coalesce 函数按顺序查找可用的日期，这会生成高效的 SQL CASE WHEN 语句
			sort_date=Coalesce(
				blog_page_date_subquery,
				blog_index_page_date_subquery,
				F('first_published_at'),  # 最后的备用选项
				output_field=models.DateField()
			),
			
			# b. 创建 'sort_title' 虚拟字段，并转换为小写以便排序
			sort_title=Lower('title')
		)
		
		# --- 数据库层面的筛选 (现在可以安全地使用了) ---
		if search_query:
			child_pages = child_pages.filter(title__icontains=search_query)
		
		# 【关键】现在我们可以直接在注解后的虚拟字段上进行过滤，不会再报错
		if start_date_str:
			child_pages = child_pages.filter(sort_date__gte=datetime.strptime(start_date_str, '%Y-%m-%d').date())
		if end_date_str:
			child_pages = child_pages.filter(sort_date__lte=datetime.strptime(end_date_str, '%Y-%m-%d').date())
		
		# --- 数据库层面的排序 ---
		valid_sort_fields = {
			'date_asc': 'sort_date',
			'date_desc': '-sort_date',
			'title_asc': 'sort_title',
			'title_desc': '-sort_title',
		}
		
		ordering = []
		if sort_primary in valid_sort_fields:
			ordering.append(valid_sort_fields[sort_primary])
		if sort_secondary in valid_sort_fields and valid_sort_fields[sort_secondary].strip('-') != valid_sort_fields[
			sort_primary].strip('-'):
			ordering.append(valid_sort_fields[sort_secondary])
		
		if ordering:
			child_pages = child_pages.order_by(*ordering)
		
		# --- 高效分页 ---
		paginator = Paginator(child_pages, 20)
		try:
			paginated_pages = paginator.page(page_number)
		except PageNotAnInteger:
			paginated_pages = paginator.page(1)
		except EmptyPage:
			paginated_pages = paginator.page(paginator.num_pages)
		
		# --- 更新上下文 ---
		# 在获取了分页结果后，再调用.specific()，开销最小
		context['blog_pages'] = paginated_pages.object_list.specific()
		context['search_query'] = search_query
		context['start_date'] = start_date_str
		context['end_date'] = end_date_str
		context['sort_primary'] = sort_primary
		context['sort_secondary'] = sort_secondary
		context['page_obj'] = paginated_pages
		
		return context
	
	class Meta:
		verbose_name = "博客索引页"
		verbose_name_plural = "博客索引页"


# 博客页面
class BlogPage(Page):
	"""博客页面，内容存储在 MongoDB"""
	
	date = models.DateField("发布日期")  # 发布日期
	
	# 将 CharField 更改为 RichTextField，并指定允许的功能
	intro = RichTextField(
		"简介",
		features=[
			'ai', # wagtail-ai的功能
			'bold',  # 加粗
			'italic',  # 斜体
			'strikethrough',  # 删除线
			'superscript',  # 上标
			'subscript',  # 下标
			'link',  # 内部和外部链接
			'code',  # 行内代码
			'blockquote'  # 引用块
		]
	)  # 简介
	
	# 作者字段
	authors = ParentalManyToManyField('blog.Author', blank=True)
	
	# 分类
	categories = ParentalManyToManyField('blog.BlogCategory', blank=True)
	
	# 标签
	tags = ClusterTaggableManager(through=BlogPageTag, blank=True)
	
	featured_image = models.ForeignKey(
		'BlogImage',
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name='+'
	)  # 特色图片
	
	mongo_content_id = models.CharField("MongoDB内容ID", max_length=50, blank=True, null=True)
	
	# StreamField定义，用于编辑界面，实际内容存储在MongoDB
	body = StreamField([
		# 富文本块 - 使用Wagtail内置编辑器
		('rich_text', RichTextBlock(
			features=['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'bold', 'italic',
			          'ol', 'ul', 'hr', 'link', 'document-link', 'image',
			          'embed', 'code', 'superscript', 'subscript', 'strikethrough',
			          'blockquote',
			          'underline',  # <--- 下划线
			          'ai' # wagtail-ai的功能
			          ],
			label="富文本"
		)),
		
		# 代码块 - 使用wagtail-codeblock
		("code_block", CodeBlock(label='Code', default_language='python')),
		
		# Markdown块 - 使用wagtail-markdown (包含代码高亮和数学公式支持)
		('markdown_block', MarkdownBlock(
			icon='code',
			label="Markdown (支持代码高亮和数学公式)",
			help_text="支持标准Markdown、代码高亮、数学公式"
		)),
		
		# StreamField 中注册我们的 MermaidBlock ---
		('mermaid_chart', MermaidBlock()),
		
		# 嵌入块 - 用于嵌入外部内容
		('embed_block', EmbedBlock(
			label="嵌入媒体",
			help_text="从YouTube、Vimeo等插入内容"
		)),
		
		# 表格块
		('table_block', CustomTableBlock(
			label="表格"
		)),
		
		# 原始HTML - 高级用户使用
		('raw_html', RawHTMLBlock(
			label="原始HTML",
			help_text="适用于高级用户的HTML代码插入"
		)),
		
		# 媒体文件
		('document_block', DocumentChooserBlock(icon='doc-full', label="文档块")),
		('image_block', ImageChooserBlock(icon='image', label="图片块")),
		('audio_block', AudioBlock(icon='media', label="音频块")),
		('video_block', VideoBlock(icon='media', label="视频块")),
	], use_json_field=True, blank=True, null=True)
	
	# 索引字段
	search_fields = Page.search_fields + [
		index.SearchField('intro'),
		index.FilterField('date'),
		index.FilterField('tags'),
		index.FilterField('categories'),
	]
	
	# 后台编辑面板
	content_panels = [
         # 1. 使用 AITitleFieldPanel 替换默认的 Page.content_panels 里的标题
         AITitleFieldPanel('title'),
	                 ] + [
		MultiFieldPanel([
			FieldPanel('date'),
			FieldPanel('tags'),
			FieldPanel("authors", widget=forms.CheckboxSelectMultiple),  # 添加作者字段到面板中
			FieldPanel('categories', widget=forms.CheckboxSelectMultiple),
		], heading="博客信息"),
		AIDescriptionFieldPanel('intro'),
		FieldPanel('featured_image'),
		FieldPanel('body'),
		InlinePanel('gallery_images', label="Gallery images"),
	]
	
	promote_panels = [
		MultiFieldPanel([
			FieldPanel('slug'),
			# 辅助生成 SEO 标题
			AITitleFieldPanel('seo_title'),
			# 辅助生成 SEO 描述
			AIDescriptionFieldPanel('search_description'),
		], heading="For Search Engines"),
		
		MultiFieldPanel([
			FieldPanel('show_in_menus'),
		], heading="Display options"),
	]
	# FieldPanel： FieldPanel 用于在 Wagtail 后台编辑界面中显示和编辑单个字段。这个字段通常是直接定义在当前模型上的 Django 模型字段。
	# InlinePanel： 用于在 Wagtail 后台编辑界面中管理与当前模型实例有关联的一组子级模型实例。它通常用于管理通过 ParentalKey 建立的父子关系。
	
	class Meta:
		verbose_name = "博客页面"
		verbose_name_plural = "博客页面"
		indexes = [
			models.Index(fields=['date']),  # 为博客发布日期添加索引，优化时间筛选查询
		]
	
	def delete(self, *args, **kwargs):
		"""重写删除方法，同时删除 MongoDB 中的内容"""
		if self.mongo_content_id:
			mongo_manager = MongoManager()
			mongo_manager.delete_blog_content(self.mongo_content_id)
		super().delete(*args, **kwargs)
	
	# 在BlogPage类中
	def save(self, *args, **kwargs):
		is_new = self.pk is None
		temp_body = self.body
		
		# 临时清空body以避免存储到MySQL
		temp_body_raw = self.body.raw_data if hasattr(self.body, 'raw_data') else None
		self.body = []
		super().save(*args, **kwargs)
		
		# 恢复body
		if temp_body_raw is not None:
			self.body = StreamValue(self.body.stream_block, temp_body_raw, is_lazy=True)
		else:
			self.body = temp_body
		
		# 准备MongoDB内容
		content_data = {
			'page_id': self.pk,
			'title': self.title,
			'intro': self.intro,
			'last_updated': self.last_published_at.isoformat() if self.last_published_at else None,
		}
		
		try:
			if hasattr(self.body, 'raw_data') and self.body.raw_data:
				content_data['body'] = self.body.raw_data
			else:
				content_data['body'] = MongoDBStreamFieldAdapter.to_mongodb(self.body)
		except Exception as e:
			logger.error(f"转换StreamField数据出错: {e}, {traceback.format_exc()}")
			if temp_body_raw:
				content_data['body'] = temp_body_raw
		
		# 保存到MongoDB
		mongo_manager = MongoManager()
		content_id = mongo_manager.save_blog_content(content_data, self.mongo_content_id)
		
		# 更新mongo_content_id
		if is_new or self.mongo_content_id != content_id:
			self.mongo_content_id = content_id
			type(self).objects.filter(pk=self.pk).update(mongo_content_id=content_id, body=[])
	
	def get_content_from_mongodb(self):
		if not self.mongo_content_id:
			return None
		try:
			mongo_manager = MongoManager()
			content = mongo_manager.get_blog_content(self.mongo_content_id)
			if not content or 'body' not in content or not isinstance(content['body'], list):
				return None
			
			for i, block in enumerate(content['body']):
				if isinstance(block, dict):
					if 'id' not in block or not block['id']:
						import uuid
						block['id'] = str(uuid.uuid4())
					if 'value' not in block:
						block['value'] = ""
			return content
		except Exception as e:
			logger.error(f"从MongoDB获取内容时出错: {e}, {traceback.format_exc()}")
			return None
	
	def _render_markdown_in_body(self, body_data):
		"""
		一个辅助函数，接收从MongoDB获取的body原始数据列表，
		找到其中的 'markdown_block' 并就地进行渲染。

		:param body_data: 从MongoDB获取的body字段的原始列表。
		:return: 经过处理的body列表，其中Markdown已渲染为HTML。
		"""
		try:
			# 导入需要的模块
			# 获取WAGTAILMARKDOWN的配置
			WAGTAILMARKDOWN = settings.WAGTAILMARKDOWN
			
			# 遍历body数据列表
			for block_data in body_data:
				# 检查是否是我们要处理的 markdown_block
				if isinstance(block_data, dict) and block_data.get('type') == 'markdown_block':
					# 获取原始的Markdown文本
					raw_markdown_text = block_data.get('value', '')
					
					# 使用settings.py中的配置进行渲染
					rendered_html = markdown(
						raw_markdown_text,
						extensions=WAGTAILMARKDOWN.get('extensions', []),
						extension_configs=WAGTAILMARKDOWN.get('extension_configs', {})
					)
					
					# 用渲染好的、安全的HTML替换掉原来的原始文本
					block_data['value'] = mark_safe(rendered_html)
			
			return body_data
		
		except Exception as e:
			import traceback
			logger.error(f"渲染Markdown时出错: {e}")
			logger.error(traceback.format_exc())
			# 如果出错，返回原始数据，避免整个页面崩溃
			return body_data
	
	def serve(self, request):
		"""
		重写serve方法，在将数据传递给模板前，对MongoDB中的Markdown内容进行渲染。
		"""
		# 1. 从MongoDB获取最原始、最纯净的内容
		mongo_content = self.get_content_from_mongodb()
		
		if mongo_content and 'body' in mongo_content:
			
			# 2. 调用独立的辅助函数来渲染Markdown
			rendered_body_data = self._render_markdown_in_body(mongo_content['body'])
			
			# 3. 将处理过的数据设置到页面body中
			try:
				self.body = MongoDBStreamFieldAdapter.from_mongodb(rendered_body_data, self.body.stream_block)
			except Exception as e:
				from wagtail.blocks.stream_block import StreamValue
				logger.error(f"使用适配器创建StreamValue失败: {e}")
				self.body = StreamValue(self.body.stream_block, rendered_body_data, is_lazy=True)
		
		# 4. 调用父类的serve方法，使用我们准备好的body内容去渲染模板
		return super().serve(request)
	
	def get_prev_post(self):
		"""获取同类别的上一篇文章"""
		# ✅ 预览模式保护
		if not self.pk:
			return None
		
		if not self.categories.exists():
			# 没有分类时，按时间获取
			return BlogPage.objects.live().filter(
				first_published_at__lt=self.first_published_at
			).order_by('-first_published_at').first()
		# 获取当前文章的分类
		category_ids = self.categories.values_list('id', flat=True)
		# 同类别且发布日期早于当前文章的最新一篇
		return BlogPage.objects.live().filter(
			categories__id__in=category_ids,
			first_published_at__lt=self.first_published_at
		).distinct().order_by('-first_published_at').first()
	
	def get_next_post(self):
		"""获取同类别的下一篇文章"""
		# ✅ 预览模式保护
		if not self.pk:
			return None
		
		if not self.categories.exists():
			# 没有分类时，按时间获取
			return BlogPage.objects.live().filter(
				first_published_at__gt=self.first_published_at
			).order_by('first_published_at').first()
		# 获取当前文章的分类
		category_ids = self.categories.values_list('id', flat=True)
		# 同类别且发布日期晚于当前文章的最早一篇
		return BlogPage.objects.live().filter(
			categories__id__in=category_ids,
			first_published_at__gt=self.first_published_at
		).distinct().order_by('first_published_at').first()
	
	def get_related_posts_by_tags(self, max_posts=5):
		"""根据标签获取相关文章"""
		
		#  预览模式保护
		if not self.pk:
			return BlogPage.objects.none()
		
		# 获取当前文章的所有标签
		if not self.tags.exists():
			return BlogPage.objects.none()
		
		tag_ids = [tag.tag_id for tag in self.tagged_items.all()]
		
		# 查找至少有一个相同标签的其他文章
		related_posts = BlogPage.objects.live().filter(
			tagged_items__tag_id__in=tag_ids
		).exclude(
			id=self.id  # 排除当前文章
		).distinct()
		
		# 按相同标签数量和发布日期排序
		related_posts = related_posts.annotate(
			same_tags=models.Count('tagged_items', filter=models.Q(tagged_items__tag_id__in=tag_ids))
		).order_by('-same_tags', '-first_published_at')[:max_posts]
		
		return related_posts
	
	# 在BlogPage类中添加这些方法
	def get_view_count(self):
		"""获取页面的访问量统计"""
		
		#  预览模式保护
		if not self.pk:
			return {
				'today': 0,
				'today_unique': 0,
				'total': 0,
				'total_unique': 0
			}
		
		today = date.today()
		
		# 尝试从数据库获取今日数据
		try:
			view_count = PageViewCount.objects.get(page=self, date=today)
			count = view_count.count
			unique_count = view_count.unique_count
		except PageViewCount.DoesNotExist:
			# 从Redis检查是否有今日计数
			try:
				import redis
				from django.conf import settings
				
				# 使用Redis连接
				redis_client = redis.Redis(
					host=getattr(settings, 'REDIS_HOST', 'localhost'),
					port=getattr(settings, 'REDIS_PORT', 6379),
					password=getattr(settings, 'REDIS_PASSWORD', None),
					db=getattr(settings, 'REDIS_DB', 0)
				)
				
				# 获取今日数据
				count = int(redis_client.get(f"page_views:{self.id}") or 0)
				unique_count = redis_client.scard(f"page_unique_views:{self.id}:{today.isoformat()}")
			except Exception:
				count = 0
				unique_count = 0
		
		# 获取总计数（历史累计）
		total_counts = PageViewCount.objects.filter(page=self).aggregate(
			total=Sum('count'),
			total_unique=Sum('unique_count')
		)
		
		total_count = total_counts.get('total') or 0
		total_unique_count = total_counts.get('total_unique') or 0
		
		# 添加Redis中可能未同步的计数
		total_count += count
		total_unique_count += unique_count
		
		return {
			'today': count,
			'today_unique': unique_count,
			'total': total_count,
			'total_unique': total_unique_count
		}
	
	def get_reactions(self):
		"""获取页面的反应统计"""
		
		if not self.pk:
			return []
		
		# 获取所有反应类型
		reaction_types = ReactionType.objects.all()
		
		# 获取该页面的反应计数
		reaction_counts = Reaction.objects.filter(page=self).values(
			'reaction_type'
		).annotate(
			count=Count('id')
		)
		
		# 转换为字典格式
		counts = {r['reaction_type']: r['count'] for r in reaction_counts}
		
		# 构建完整结果
		result = []
		for rt in reaction_types:
			result.append({
				'id': rt.id,
				'name': rt.name,
				'icon': rt.icon,
				'count': counts.get(rt.id, 0)
			})
		
		return result
	
	def user_has_reacted(self, request):
		"""检查当前用户是否对页面有反应"""
		if request.user.is_authenticated:
			return Reaction.objects.filter(
				page=self,
				user=request.user
			).values_list('reaction_type_id', flat=True).first()
		elif request.session.session_key:
			return Reaction.objects.filter(
				page=self,
				session_key=request.session.session_key
			).values_list('reaction_type_id', flat=True).first()
		return None


class BlogPageGalleryImage(Orderable):
	"""博客页面画廊图片模型"""
	
	# Orderable 是 Wagtail 提供的一个 Mixin 类。Mixin 是一种在 Python 中复用代码的方式，您可以将一个或多个 Mixin 类与其他类一起继承，从而将 Mixin 中的功能“混合”到您的类中。
	# Orderable Mixin 的主要作用是为您的模型添加一个 sort_order 字段。这个字段是一个整数，用于记录模型实例的排序顺序。
	
	# 关联到BlogPage
	page = ParentalKey(BlogPage, on_delete=models.CASCADE, related_name='gallery_images')
	
	# 关联您自定义的图片模型
	image = models.ForeignKey(
		'blog.BlogImage',  # <-- 使用您自定义的图片模型
		on_delete=models.CASCADE,
		related_name='+'
	)
	caption = models.CharField(blank=True, max_length=250)
	
	panels = [
		FieldPanel('image'),
		FieldPanel('caption'),
	]


# 页面访问记录模型
@register_snippet
class PageView(models.Model):
	page = models.ForeignKey('wagtailcore.Page', on_delete=models.CASCADE, related_name='page_views')
	session_key = models.CharField(max_length=100, blank=True, null=True)
	user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
	ip_address = models.GenericIPAddressField()
	user_agent = models.CharField(max_length=255, blank=True)
	viewed_at = models.DateTimeField(auto_now_add=True)
	is_unique = models.BooleanField(default=False)
	
	class Meta:
		verbose_name = "页面访问记录"
		verbose_name_plural = "页面访问记录"
		indexes = [
			models.Index(fields=['page', 'viewed_at']),
			models.Index(fields=['viewed_at']),
		]


# 访问统计聚合模型
@register_snippet
class PageViewCount(models.Model):
	page = models.ForeignKey('wagtailcore.Page', on_delete=models.CASCADE, related_name='view_counts')
	date = models.DateField()
	count = models.PositiveIntegerField(default=0)  # 总访问量
	unique_count = models.PositiveIntegerField(default=0)  # 唯一访问量
	
	class Meta:
		verbose_name = "页面访问统计"
		verbose_name_plural = "页面访问统计"
		unique_together = ('page', 'date')
	
	def __str__(self):
		return f"{self.page.title} - {self.date} - {self.count}次访问"


# 反应类型模型
@register_snippet
class ReactionType(models.Model):
	name = models.CharField("反应名称", max_length=50)
	icon = models.CharField("图标CSS类", max_length=50)
	display_order = models.PositiveSmallIntegerField("显示顺序", default=0)
	
	class Meta:
		verbose_name = "反应类型"
		verbose_name_plural = "反应类型"
		ordering = ['display_order']
	
	def __str__(self):
		return self.name


# 用户反应模型
@register_snippet
class Reaction(models.Model):
	page = models.ForeignKey('wagtailcore.Page', on_delete=models.CASCADE, related_name='reactions')
	reaction_type = models.ForeignKey(ReactionType, on_delete=models.CASCADE, related_name='reactions')
	user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
	session_key = models.CharField(max_length=100, blank=True, null=True)
	ip_address = models.GenericIPAddressField()
	created_at = models.DateTimeField(auto_now_add=True)
	
	class Meta:
		verbose_name = "用户反应"
		verbose_name_plural = "用户反应"
		unique_together = (
			('page', 'user'),  # 每个用户对每个页面只能有一个反应
			('page', 'session_key', 'ip_address')  # 对于匿名用户，按会话和IP限制
		)
	
	def __str__(self):
		user_str = self.user.username if self.user else f"匿名({self.session_key[:10]})"
		return f"{user_str} - {self.reaction_type.name} - {self.page.title}"


@register_snippet
class Author(models.Model):
	"""作者模型"""
	
	name = models.CharField(max_length=255)  # 作者名称
	author_image = models.ForeignKey(
		'blog.BlogImage',
		null=True,  # 允许为空
		blank=True,  # 允许在表单中为空
		on_delete=models.SET_NULL,  # 删除图片时设置为空
		related_name='+'  # 不需要反向关系
	)  # 作者图片
	
	# 使用 RichTextField 允许在后台编辑时使用富文本格式
	bio = StreamField([
		('paragraph', RichTextBlock(
			features=['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'bold', 'italic',
			          'ol', 'ul', 'hr', 'link', 'document-link', 'image',
			          'embed', 'code', 'superscript', 'subscript', 'strikethrough',
			          'blockquote'],
			label="段落", icon="pilcrow"
		)),
		('image', ImageChooserBlock(icon="image", label="图片")),  # <-- 添加图片选择块
	],
		use_json_field=True,  # 推荐为新的 StreamFields 使用 JSON 存储
		blank=True,
		verbose_name="个人简介"
	)
	
	# 列表中的每个元素都定义了在Wagtail管理后台中显示的一个字段。 panels列表决定了哪些字段将出现在Snippet的编辑界面中。
	# 您在这里使用的是panels而不是content_panels；由于片段通常不需要诸如slug或发布日期之类的字段，
	# 因此它们的编辑界面不会分为单独的“内容”/“推广”/“设置”选项卡。因此无需区分“内容面板”和“推广面板”。
	panels = [
		FieldPanel('name'),
		FieldPanel('author_image'),
		FieldPanel('bio', heading="个人简介"),  # 使用 StreamFieldPanel 显示富文本简介
	]  # 在管理界面中显示的字段
	
	def __str__(self):
		return self.name
	
	# 在 Author 类中添加这个方法
	def get_bio_preview(self, word_limit=3):
		"""获取简介的预览版本，限制字数"""
		if not self.bio:
			return ""
		
		preview_text = ""
		word_count = 0
		
		for block in self.bio:
			if block.block_type == 'paragraph':
				# 处理段落块
				block_text = strip_tags(str(block.value))
				
				# 分割单词并计算
				words = block_text.split()
				remaining_words = word_limit - word_count
				
				if remaining_words <= 0:
					break
				
				if len(words) <= remaining_words:
					preview_text += block_text + " "
					word_count += len(words)
				else:
					preview_text += " ".join(words[:remaining_words]) + "..."
					break
		
		# 忽略图片块，只处理文本
		
		return preview_text.strip()
	
	def get_bio_preview_html(self, word_limit=3):
		"""获取带HTML格式的简介预览"""
		if not self.bio:
			return ""
		
		preview_html = ""
		word_count = 0
		
		for block in self.bio:
			if block.block_type == 'paragraph':
				block_html = str(block.value)
				block_text = strip_tags(block_html)
				
				words = block_text.split()
				remaining_words = word_limit - word_count
				
				if remaining_words <= 0:
					break
				
				if len(words) <= remaining_words:
					preview_html += f"<p>{block_html}</p>"
					word_count += len(words)
				else:
					# 截断HTML内容
					truncated_text = " ".join(words[:remaining_words]) + "..."
					preview_html += f"<p>{truncated_text}</p>"
					break
		
		return mark_safe(preview_html)
	
	class Meta:
		verbose_name = '作者'
		verbose_name_plural = '作者列表'