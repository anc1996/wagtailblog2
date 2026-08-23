"""博客应用的 Wagtail 页面、媒体、统计和导入状态模型。

BlogPage 的 StreamField 负责后台结构和校验，正文快照通过 MongoManager 保存，MySQL
页面/Revision 只保存指针和检索所需元数据。模型方法必须保持 Wagtail 页面生命周期、
Mongo 正文指针和补偿顺序，注释重点说明这些跨存储边界，不重复框架字段定义。
"""

# 博客应用的页面、媒体和统计模型
import logging,uuid,json,re,time,hashlib,secrets
from typing import Any

from django.db.models.functions import Coalesce, Lower
from django.utils import timezone
from django.utils.text import slugify
from django.utils.dateparse import parse_date
from django.core.paginator import Paginator
from django.db import models, transaction
from django import forms
from django.db.models import Count, Subquery, OuterRef, F
from django.conf import settings
from django.utils.html import strip_tags  # 用于去除HTML标签
from django.utils.safestring import mark_safe  # 用于标记HTML安全


from modelcluster.fields import ParentalKey, ParentalManyToManyField
from modelcluster.contrib.taggit import ClusterTaggableManager

from taggit.models import TaggedItemBase

from wagtail.admin.forms import WagtailAdminPageForm
from wagtail.embeds.blocks import EmbedBlock
from wagtail.models import Page, Orderable
from wagtail.fields import StreamField, RichTextField
from wagtail.admin.panels import FieldPanel, HelpPanel, InlinePanel, MultiFieldPanel, TitleFieldPanel
from wagtail.search import index
from wagtail.images.models import Image, AbstractImage, AbstractRendition
from wagtail.blocks import RichTextBlock, RawHTMLBlock
from wagtail.images.blocks import ImageChooserBlock
from wagtail.documents.blocks import DocumentChooserBlock
from wagtail.snippets.models import register_snippet
from wagtail.blocks.stream_block import StreamValue
from blog.page_view_counter import PageViewCounter
from blog.blocks import (
	AudioBlock,
	VideoBlock,
	CustomTableBlock,
	MermaidBlock,
	PureCodeBlock,
	CustomEmbedBlock,
	VditorMarkdownBlock,
)
from wagtailblog3.mongodb import MongoDBStreamFieldAdapter
from wagtailblog3.mongo import MongoManager


logger = logging.getLogger(__name__)


# 自定义图片模型
class BlogImage(AbstractImage):
	"""自定义博客图片模型，使用 caption 作为缺省替代文本。"""
	caption = models.CharField(max_length=255, blank=True)
	admin_form_fields = Image.admin_form_fields + ('caption',)  # 添加caption字段到后台表单
	
	@property
	def default_alt_text(self) -> str:
		"""返回图片 caption，缺失时回退到图片标题。"""
		# 如果没有指定alt文本，使用caption作为替代
		return self.caption or self.title


class BlogRendition(AbstractRendition):
	"""博客图片派生 rendition，按图片、过滤规格和焦点键保持唯一。"""
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
	parent_page_types = ['wagtailcore.Page', 'home.HomePage', 'blog.BlogIndexPage']
	subpage_types = []
	
	# 每页显示的标签显示数
	items_tag_page = 50
	# 每页显示的文章数
	items_per_page = 20
	
	def get_context(self, request: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
		"""复用标签查询服务构造 HTML 页面上下文，保证页面和 JSON 端点规则一致。"""
		from blog.services.tag_listing import get_tag_index_context

		context = super().get_context(request, *args, **kwargs)
		context.update(
			get_tag_index_context(
				query_params=request.GET,
				tag_page_size=self.items_tag_page,
				article_page_size=self.items_per_page,
			)
		)
		return context


# 标签模型
class BlogPageTag(TaggedItemBase):
	"""博客页面与 taggit 标签的父子关联模型。"""
	
	# TaggedItemBase 是一个抽象模型，用于定义标签与模型的关联关系。
	
	content_object = ParentalKey(
		'BlogPage',
		related_name='tagged_items',
		on_delete=models.CASCADE
	)  # 关联到BlogPage模型


# 博客分类
@register_snippet
class BlogCategory(models.Model):
	"""博客分类片段，使用唯一 slug 作为稳定筛选标识。"""
	name = models.CharField(max_length=255)
	slug = models.SlugField(unique=True, max_length=80)
	
	panels = [
		FieldPanel('name'),
		FieldPanel('slug'),
	]
	
	def __str__(self) -> str:
		return self.name
	
	class Meta:
		verbose_name = "博客分类"
		verbose_name_plural = "博客分类"


BLOG_INDEX_ITEMS_PER_PAGE = 20
BLOG_INDEX_DEFAULT_SORT_PRIMARY = 'date_desc'
BLOG_INDEX_DEFAULT_SORT_SECONDARY = 'title_asc'
BLOG_INDEX_SORT_FIELDS = {
	'date_asc': 'sort_date',
	'date_desc': '-sort_date',
	'title_asc': 'sort_title',
	'title_desc': '-sort_title',
}


def _normalise_blog_index_date(value: object) -> tuple[str, Any]:
	"""规范化索引页日期筛选，返回展示字符串和解析日期或空值。"""
	value = (value or '').strip()
	if not value:
		return '', None
	try:
		parsed_value = parse_date(value)
	except ValueError:
		parsed_value = None
	return (value, parsed_value) if parsed_value else ('', None)


# 博客索引页面
class BlogIndexPage(Page):
	"""博客索引页面，提供公开子页筛选、排序和分页上下文。"""
	
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
	
	def get_listing_context(self, query_params: Any) -> dict[str, Any]:
		"""构造 HTML 和 JSON 共用的子页面列表上下文。

		日期排序使用 BlogPage/BlogIndexPage 日期子查询并以发布时间兜底；随后应用公开状态、
		关键词、日期范围和稳定主键排序，再由 Paginator 分页。筛选只读取 live/public 子页，
		不会把草稿或受限页面暴露到列表。
		"""
		search_query = (query_params.get('search') or '').strip()
		start_date_str, start_date = _normalise_blog_index_date(
			query_params.get('start_date')
		)
		end_date_str, end_date = _normalise_blog_index_date(
			query_params.get('end_date')
		)

		sort_primary = query_params.get(
			'sort_primary', BLOG_INDEX_DEFAULT_SORT_PRIMARY
		)
		if sort_primary not in BLOG_INDEX_SORT_FIELDS:
			sort_primary = BLOG_INDEX_DEFAULT_SORT_PRIMARY

		if sort_primary.startswith('date_'):
			secondary_sort_options = (
				('title_asc', '标题 (A→Z)'),
				('title_desc', '标题 (Z→A)'),
			)
		else:
			secondary_sort_options = (
				('date_desc', '时间 (新→旧)'),
				('date_asc', '时间 (旧→新)'),
			)
		valid_secondary_values = {value for value, _ in secondary_sort_options}
		sort_secondary = query_params.get('sort_secondary')
		if sort_secondary not in valid_secondary_values:
			sort_secondary = secondary_sort_options[0][0]

		blog_page_date_subquery = Subquery(
			BlogPage.objects.filter(page_ptr_id=OuterRef('pk')).values('date')[:1]
		)
		blog_index_page_date_subquery = Subquery(
			BlogIndexPage.objects.filter(page_ptr_id=OuterRef('pk')).values('date')[:1]
		)
		
		child_pages = self.get_children().live().public().annotate(
			sort_date=Coalesce(
				blog_page_date_subquery,
				blog_index_page_date_subquery,
				F('first_published_at'),
				output_field=models.DateField()
			),
			sort_title=Lower('title')
		)

		if search_query:
			child_pages = child_pages.filter(title__icontains=search_query)
		if start_date:
			child_pages = child_pages.filter(sort_date__gte=start_date)
		if end_date:
			child_pages = child_pages.filter(sort_date__lte=end_date)

		child_pages = child_pages.order_by(
			BLOG_INDEX_SORT_FIELDS[sort_primary],
			BLOG_INDEX_SORT_FIELDS[sort_secondary],
			'pk',
		)

		paginator = Paginator(child_pages, BLOG_INDEX_ITEMS_PER_PAGE)
		page_obj = paginator.get_page(query_params.get('page'))

		return {
			'blog_pages': page_obj.object_list.specific(),
			'search_query': search_query,
			'start_date': start_date_str,
			'end_date': end_date_str,
			'sort_primary': sort_primary,
			'sort_secondary': sort_secondary,
			'secondary_sort_options': secondary_sort_options,
			'page_obj': page_obj,
			'total_results': paginator.count,
			'has_active_filters': bool(
				search_query
				or start_date_str
				or end_date_str
				or sort_primary != BLOG_INDEX_DEFAULT_SORT_PRIMARY
				or sort_secondary != BLOG_INDEX_DEFAULT_SORT_SECONDARY
			),
			'blog_tag_index_page': BlogTagIndexPage.objects.live().public().first(),
		}

	def get_context(self, request: Any) -> dict[str, Any]:
		"""把列表查询结果合并到 Wagtail 页面上下文。"""
		context = super().get_context(request)
		context.update(self.get_listing_context(request.GET))
		return context
	
	class Meta:
		verbose_name = "博客索引页"
		verbose_name_plural = "博客索引页"


class BlogPageForm(WagtailAdminPageForm):
	"""BlogPage 后台表单，负责从 Mongo 恢复正文并记录安全校验诊断。"""

	@classmethod
	def _summarize_validation_error(cls: type["BlogPageForm"], error: Any) -> Any:
		"""递归展开 Wagtail 块错误，只记录路径、消息和校验代码。"""
		# StreamField 错误可能嵌套多层；递归转换后日志可以定位块和字段，但不暴露正文。
		if isinstance(error, (list, tuple)):
			return [cls._summarize_validation_error(item) for item in error]

		details = {}
		block_errors = getattr(error, 'block_errors', None)
		if block_errors:
			details['children'] = {
				str(key): cls._summarize_validation_error(child)
				for key, child in block_errors.items()
			}

		non_block_errors = getattr(error, 'non_block_errors', None)
		if non_block_errors:
			details['non_block'] = cls._summarize_validation_error(
				non_block_errors.as_data()
				if hasattr(non_block_errors, 'as_data')
				else list(non_block_errors)
			)

		if not details:
			messages = [str(message)[:300] for message in getattr(error, 'messages', [])]
			details['messages'] = messages
			code = getattr(error, 'code', None)
			if code:
				details['code'] = code
		return details

	def __init__(self, *args: Any, **kwargs: Any) -> None:
		"""初始化编辑表单；页面正文为空壳时按 Revision 指针恢复 Mongo 草稿。"""
		instance = kwargs.get('instance')
		
		# 只要当前表单有关联的真实页面实例，立即启动拦截
		if instance and instance.pk:
			# 强行透视：检查当前关系型数据库（MySQL）吐出来的 body 是不是空壳
			is_body_empty = not instance.body or (hasattr(instance.body, '__len__') and len(instance.body) == 0)
			
			if is_body_empty:
				mongo_manager = MongoManager()
				content = None
				content_source = "none"
				
			# 第一阶段：优先从最新 Revision 取得草稿指针，恢复编辑者最近保存的版本。
				latest_revision = instance.revisions.order_by('-created_at').first()
				if latest_revision:
					try:
						# 从 Revision 元数据中读取 Mongo 草稿指针，而不是读取被清空的 body。
						rev_data = latest_revision.content
						if isinstance(rev_data, str):
							rev_data = json.loads(rev_data)
						
						if isinstance(rev_data, dict):
							draft_pointer = rev_data.get('mongo_draft_pointer')
							if draft_pointer:
								# 根据指针读取草稿正文；该路径优先于正式发布内容。
								content = mongo_manager.get_blog_revision_body(draft_pointer)
								if content:
									content_source = "revision"
					except Exception as e:
						logger.error(f"BlogPageForm 穿透解析历史快照遭遇异常: {e}", exc_info=True)
				
			# 第二阶段：草稿缺失时回退到 Mongo 正式内容，兼容新发布页面和旧数据。
				if (not content or 'body' not in content) and getattr(instance, 'mongo_content_id', None):
					content = mongo_manager.get_blog_content(instance.mongo_content_id)
					if content:
						content_source = "live"
				
			# 第三阶段：把 Mongo 字典转换为带块 ID 的 StreamValue，供后台编辑器渲染。
				if content and 'body' in content:
					# 直接调用模型内封装好的 UUID 补齐与 StreamValue 重建方法
					raw_body = content['body']
					raw_types = [
						block.get('type', '<missing>')
						for block in raw_body
						if isinstance(block, dict)
					]
					logger.info(
						"blog_body_admin_source page_id=%s source=%s raw_blocks=%s types=%s",
						instance.pk,
						content_source,
						len(raw_body) if isinstance(raw_body, list) else -1,
						raw_types,
					)
					instance.body = instance._hydrate_streamfield_from_mongo(raw_body)
					logger.info(
						"blog_body_admin_hydrated page_id=%s hydrated_blocks=%s types=%s",
						instance.pk,
						len(instance.body) if hasattr(instance.body, '__len__') else -1,
						[block.block_type for block in instance.body],
					)
				else:
					logger.warning(
						"blog_body_admin_missing page_id=%s mongo_content_id=%s",
						instance.pk,
						getattr(instance, 'mongo_content_id', None),
					)
		
		# 正文恢复完成后再绑定表单，保证 Wagtail 初始字段读取到完整内容。
		super().__init__(*args, **kwargs)
		if instance and instance.pk:
			logger.info(
				"blog_body_admin_form_bound page_id=%s blocks=%s types=%s",
				instance.pk,
				len(instance.body) if hasattr(instance.body, '__len__') else -1,
				[block.block_type for block in instance.body],
			)

	def full_clean(self) -> None:
		"""执行表单校验并记录不含正文原文的结构化错误摘要。"""
		"""记录绑定表单的校验过程，不记录提交的正文内容。"""
		if not self.is_bound:
			return super().full_clean()

		started_at = time.monotonic()
		page_id = getattr(self.instance, 'pk', None)
		body_count = self.data.get('body-count')
		block_types = []
		active_blocks = 0
		try:
			parsed_count = int(body_count or 0)
		except (TypeError, ValueError):
			parsed_count = 0
		# 仅从管理表单元数据提取块类型和删除标记，用于诊断校验失败。
		for index in range(parsed_count):
			block_type = self.data.get(f'body-{index}-type', '<missing>')
			deleted = bool(self.data.get(f'body-{index}-deleted'))
			block_types.append(f'{block_type}:deleted' if deleted else block_type)
			if not deleted:
				active_blocks += 1

		logger.info(
			"blog_body_validation_start page_id=%s body_count=%s active_blocks=%s types=%s",
			page_id,
			body_count,
			active_blocks,
			block_types,
		)
		try:
			super().full_clean()
		except Exception:
			logger.exception(
				"blog_body_validation_exception page_id=%s elapsed_ms=%s",
				page_id,
				round((time.monotonic() - started_at) * 1000, 1),
			)
			raise

		errors = {
			field_name: [
				self._summarize_validation_error(error)
				for error in error_list
			]
			for field_name, error_list in self._errors.as_data().items()
		} if self._errors else {}
		log_method = logger.warning if errors else logger.info
		log_method(
			"blog_body_validation_done page_id=%s valid=%s elapsed_ms=%s errors=%s",
			page_id,
			not bool(errors),
			round((time.monotonic() - started_at) * 1000, 1),
			errors,
		)
		

# 博客页面
class BlogPage(Page):
	"""博客页面模型。

	StreamField 仅作为编辑器结构和校验入口；正文快照写入 MongoDB，MySQL Page/Revision
	保存轻量元数据和正文指针。保存、发布、取消发布和删除方法必须维持两个存储之间的
	顺序与补偿边界，前台读取失败时只能降级为空正文，不能伪造已发布内容。
	"""
	
	date = models.DateField("发布日期")  # 发布日期
	
	# 将 CharField 更改为 RichTextField，并指定允许的功能
	intro = RichTextField(
		"简介",
		features=[
			'bold',  # 加粗
			'italic',  # 斜体
			'strikethrough',  # 删除线
			'superscript',  # 上标
			'subscript',  # 下标
			'link',  # 内部和外部链接
			'code',  # 行内代码
			'blockquote'  # 引用块
		]
	)
	
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
	
	# StreamField 只负责后台结构和校验，正文实际持久化到 MongoDB。
	body = StreamField([
		# 富文本块 - 使用Wagtail内置编辑器
		('rich_text', RichTextBlock(
			features=['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'bold', 'italic',
			          'ol', 'ul', 'hr', 'link', 'document-link', 'image',
			          'embed', 'code', 'superscript', 'subscript', 'strikethrough',
			          'blockquote',
			          'underline',  # <--- 下划线
			          ],
			label="富文本"
		)),
		
		# 代码块 - 使用wagtail-codeblock
		("code_block", PureCodeBlock(default_language='python')),
		
		
		# Markdown块 - 使用项目自有 Vditor 编辑器和安全渲染器
		('markdown_block', VditorMarkdownBlock(
			icon='code',
			label="Markdown (支持代码高亮和数学公式)",
			help_text="支持标准Markdown、代码高亮、数学公式"
		)),
		
		# StreamField 中注册我们的 MermaidBlock ---
		('mermaid_chart', MermaidBlock()),
		
		# 嵌入块 - 将原来的 EmbedBlock 整体升级为我们的 CustomEmbedBlock
		('embed_block', CustomEmbedBlock(
			label="嵌入媒体",
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
	
	# 绑定自定义表单，使后台打开页面时能从 MongoDB 恢复正文并记录校验诊断。
	base_form_class = BlogPageForm
	
	# 索引字段：body_text 会按需从 MongoDB 拼接纯文本供搜索后端使用。
	# Wagtail 核心 Page 字段必须完整继承；移除本模型重复声明的标题自动补全字段。
	search_fields = Page.search_fields + [
		index.SearchField('title', boost=10, partial_match=False),
		index.SearchField('intro', boost=5),
		index.SearchField('body_text', boost=2),  # ← 改名，独立字段
		# index.SearchField('subtitle', boost=8), # 👉 仅需在这里加一行
		index.FilterField('date'),
		index.FilterField('tags'),
		index.FilterField('categories'),
	]
	
	content_panels = [
		HelpPanel(
			mark_safe(
				'<section id="blog-ai-metadata" class="help-block">'
				'<p>点击生成会将当前未保存正文发送到测试环境配置的外部 AI 服务。生成结果不会自动保存或发布。</p>'
				'<label for="blog-ai-template">提示词模板</label>'
				'<select id="blog-ai-template" data-template-select disabled>'
				'<option value="">正在加载可用提示词...</option>'
				'</select>'
				'<button type="button" class="button button-secondary" data-action="generate">生成元数据建议</button>'
				'<div data-status role="status" aria-live="polite"></div>'
				'<div data-preview hidden></div>'
				'</section>'
			)
		),
		# 1. 标题
		TitleFieldPanel('title', apply_if_live=True),
		# 2. 组合信息
		MultiFieldPanel([
			FieldPanel('date'),
			FieldPanel("tags"),
			FieldPanel("authors", widget=forms.CheckboxSelectMultiple),
			FieldPanel('categories', widget=forms.CheckboxSelectMultiple),
		], heading="博客信息"),
		
		# 3. 简介及其他字段
		FieldPanel('intro'),
		FieldPanel('featured_image'),
		FieldPanel('body'),
		InlinePanel('gallery_images', label="Gallery images"),
	]
	
	promote_panels = [
		MultiFieldPanel([
			FieldPanel('slug'),
			FieldPanel('seo_title'),
			FieldPanel('search_description'),
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
	
	def _hydrate_streamfield_from_mongo(self, body_data: Any) -> Any:
		"""从 Mongo 字典重建后台编辑器需要的 StreamValue。

		历史块缺少 Wagtail 动态 ID 时只在内存副本补 UUID；适配器失败则保留惰性
		StreamValue，避免编辑页面因单个历史块无法转换而完全打不开。
		"""
		if not body_data or not isinstance(body_data, list):
			logger.info(
				"blog_body_hydrate_empty page_id=%s value_type=%s",
				self.pk,
				type(body_data).__name__,
			)
			return []
		
		# Wagtail 动态块依赖稳定 ID；历史 Mongo 数据缺少 ID 时先补齐，避免前端组件无法挂载。
		missing_ids = 0
		for block in body_data:
			if isinstance(block, dict) and 'id' not in block:
				block['id'] = str(uuid.uuid4())
				missing_ids += 1
		block_types = [
			block.get('type', '<missing>')
			for block in body_data
			if isinstance(block, dict)
		]
		logger.info(
			"blog_body_hydrate_start page_id=%s raw_blocks=%s missing_ids=%s types=%s",
			self.pk,
			len(body_data),
			missing_ids,
			block_types,
		)
		
		try:
			# 适配器负责把 Mongo 的普通字典转换成 Wagtail 块值；失败时保留原始数据做惰性回退。
			stream_value = MongoDBStreamFieldAdapter.from_mongodb(body_data, self.body.stream_block)
			logger.info(
				"blog_body_hydrate_done page_id=%s hydrated_blocks=%s types=%s",
				self.pk,
				len(stream_value),
				[block.block_type for block in stream_value],
			)
			return stream_value
		except Exception as e:
			logger.error(f"StreamField 反序列化降级: {e}", exc_info=True)
			return StreamValue(self.body.stream_block, body_data, is_lazy=True)
	
	# =========================================================================
	# 网关 1：拦截快照序列化 (保存草稿、生成历史记录时自动触发)
	# =========================================================================
	def serializable_data(self) -> dict[str, Any]:
		"""生成 Revision 快照时把正文写入 Mongo 草稿集合，仅在 Revision 中保存指针。

		先把当前 StreamField 转为 Mongo 结构，再写草稿指针，最后将 MySQL Revision 的
		``body`` 置为空数组；这样历史版本可恢复正文，同时避免关系库保存大段正文。
		"""
		started_at = time.monotonic()
		logger.info(
			"blog_body_revision_start page_id=%s blocks=%s types=%s",
			self.pk,
			len(self.body) if hasattr(self.body, '__len__') else -1,
			[block.block_type for block in self.body],
		)
		data = super().serializable_data()
		
		if hasattr(self.body, 'raw_data') and self.body.raw_data:
			draft_content = self.body.raw_data
		else:
			draft_content = MongoDBStreamFieldAdapter.to_mongodb(self.body)
		
		# 先把当前 StreamField 转成 Mongo 结构，确保 Revision 与编辑器看到的是同一版本。
		mongo_manager = MongoManager()
		draft_pointer = mongo_manager.save_blog_revision_body(self.pk, draft_content)
		
		# MySQL Revision 只保存指针，避免把大段正文写入关系数据库。
		data['mongo_draft_pointer'] = str(draft_pointer)
		# 同时把 Revision 的 body 置为空字符串表示，保持历史行轻量。
		data['body'] = '[]'
		logger.info(
			"blog_body_revision_done page_id=%s pointer=%s elapsed_ms=%s",
			self.pk,
			draft_pointer,
			round((time.monotonic() - started_at) * 1000, 1),
		)
		
		return data
	
	# =========================================================================
	# 网关 2：反序列化还原 (后台点击预览、查看历史记录时自动触发)
	# =========================================================================
	@classmethod
	def from_serializable_data(cls: type["BlogPage"], data: dict[str, Any]) -> "BlogPage":
		"""反序列化 Revision，按草稿指针优先、正式内容回退的顺序恢复正文。"""
		obj = super().from_serializable_data(data)
		mongo_manager = MongoManager()
		content = None
		
		# 第一阶段：优先读取 Revision 中的 Mongo 草稿指针。
		draft_pointer = data.get('mongo_draft_pointer')
		if draft_pointer:
			content = mongo_manager.get_blog_revision_body(draft_pointer)
		
		# 第二阶段：草稿不存在或为空时，必须继续回退到正式内容，不能被 elif 截断。
		is_content_empty = not content or 'body' not in content or not content['body']
		if is_content_empty and obj.mongo_content_id:
			content = mongo_manager.get_blog_content(obj.mongo_content_id)
		
		# 第三阶段：把恢复出的正文重新注入 StreamValue，供预览和历史页面使用。
		if content and 'body' in content:
			obj.body = obj._hydrate_streamfield_from_mongo(content['body'])
		
		return obj
	
	def get_latest_revision_as_object(self) -> "BlogPage":
		"""
		拦截 EditView 初始化表单。
		升级铁娘子级空值防线，防止因 StreamValue 对象的 truthy 判定历史残留导致逃过拦截。
		"""
		obj = super().get_latest_revision_as_object()
		
		# 同时检查布尔值和块数量，规避不同 Wagtail 版本对空 StreamValue 的 truthy 差异。
		is_body_empty = not obj.body or (hasattr(obj.body, '__len__') and len(obj.body) == 0)
		
		if is_body_empty and self.mongo_content_id:
			mongo_manager = MongoManager()
			content = mongo_manager.get_blog_content(self.mongo_content_id)
			if content and 'body' in content:
				obj.body = self._hydrate_streamfield_from_mongo(content['body'])
		
		return obj
	
	# =========================================================================
	# 网关 3：正式线上保存防线 (点击发布、或更新状态时触发)
	# =========================================================================
	def save(self, *args: Any, **kwargs: Any) -> None:
		"""保存页面时同步 Mongo 正文，并确保 MySQL body 始终为空。

		只有全量保存且非导入草稿模式才写正式 Mongo 内容；随后临时清空 body 调用
		Wagtail 父类保存，finally 恢复内存 StreamField。新页面取得 MySQL 主键后再回填
		Mongo ``page_id``，避免正式文档引用不存在的页面 ID。
		"""
		started_at = time.monotonic()
		is_new = self.pk is None
		update_fields = kwargs.get('update_fields')
		draft_only = bool(getattr(self, '_markdown_import_draft_only', False))
		
		# update_fields 不含 body 时视为元数据更新，避免把不完整的表单正文覆盖到正式 Mongo 内容。
		is_draft_metadata_update = update_fields is not None and 'body' not in update_fields
		logger.info(
			"blog_body_save_start page_id=%s is_new=%s draft_metadata=%s update_fields=%s",
			self.pk,
			is_new,
			is_draft_metadata_update,
			list(update_fields) if update_fields is not None else None,
		)
		
		if not is_draft_metadata_update and not draft_only:
			# 全量保存才写入正式集合；草稿正文已经由 serializable_data 保存到历史集合。
			temp_body_raw = self.body.raw_data if hasattr(self.body, 'raw_data') else None
			if temp_body_raw is None and self.body:
				temp_body_raw = MongoDBStreamFieldAdapter.to_mongodb(self.body)
			
			content_data = {
				'page_id': self.pk,
				'title': self.title,
				'intro': self.intro,
				'last_updated': self.last_published_at.isoformat() if getattr(self, 'last_published_at',
				                                                              None) else None,
				'body': temp_body_raw or []
			}
			try:
				mongo_started_at = time.monotonic()
				mongo_manager = MongoManager()
				content_id = mongo_manager.save_blog_content(content_data, getattr(self, 'mongo_content_id', None))
				self.mongo_content_id = content_id
				logger.info(
					"blog_body_save_mongo_done page_id=%s content_id=%s blocks=%s elapsed_ms=%s",
					self.pk,
					content_id,
					len(temp_body_raw or []),
					round((time.monotonic() - mongo_started_at) * 1000, 1),
				)
			except Exception as e:
				logger.error(f"保存线上主内容至 MongoDB 失败: {e}", exc_info=True)
		
		# 临时清空 body 后调用父类保存，确保关系数据库不落正文；finally 恢复内存对象供后续流程继续使用。
		real_body = self.body
		self.body = []
		try:
			super().save(*args, **kwargs)
		finally:
			# 立刻满血复原内存态，保障本次请求周期后续逻辑拿到的是完好的 body
			self.body = real_body
		logger.info(
			"blog_body_save_mysql_done page_id=%s elapsed_ms=%s",
			self.pk,
			round((time.monotonic() - started_at) * 1000, 1),
		)
		
		# 新页面先完成 MySQL 自增主键，再把 page_id 回填到 Mongo 正式文档。
		if is_new and self.pk and not is_draft_metadata_update and not draft_only:
			type(self).objects.filter(pk=self.pk).update(mongo_content_id=self.mongo_content_id)
			try:
				mongo_manager = MongoManager()
				if getattr(mongo_manager, 'blog_content', None) is not None:
					mongo_manager.blog_content.update_one(
						{'_id': self.mongo_content_id},
						{'$set': {'page_id': self.pk}}
					)
			except Exception:
				pass
	
	def publish(self, *args: Any, **kwargs: Any) -> Any:
		"""将 Wagtail 发布和搜索事件置于同一 MySQL 事务。"""
		with transaction.atomic():
			return super().publish(*args, **kwargs)

	def unpublish(self, *args: Any, **kwargs: Any) -> Any:
		"""将取消发布和墓碑事件置于同一 MySQL 事务，事件只在提交后被唤醒。"""
		with transaction.atomic():
			return super().unpublish(*args, **kwargs)

	# =========================================================================
	# 核心网关 4：物理删除与异构集群同步 (在后台点击“删除页面”时触发)
	# =========================================================================
	def delete(self, *args: Any, **kwargs: Any) -> Any:
		"""删除页面实体后同步清理 Mongo 正式内容和历史草稿。

		页面行删除前先在同一事务记录搜索墓碑，防止迟到 upsert 复活页面；关系库删除
		完成后再按已知 content ID 和 page ID 清理 Mongo，清理失败只记录日志，不删除未知对象。
		"""
		page_id = self.pk
		mongo_content_id = getattr(self, 'mongo_content_id', None)
		
		# 墓碑 State 和 Outbox 必须在 Page 行删除前写入同一事务，避免迟到 upsert 复活已删除页面。
		with transaction.atomic():
			if settings.CONTENT_SEARCH_PRODUCER_ENABLED:
				from search.services.outbox import ContentSearchOutboxService
				ContentSearchOutboxService.record_delete(self)
			deletion_result = super().delete(*args, **kwargs)
		
		# 再清理两个 Mongo 集合，保证正式内容和 Revision 快照都不残留。
		try:
			mongo_manager = MongoManager()
			
			# 清理线上正式版主内容
			if mongo_content_id:
				mongo_manager.delete_blog_content(mongo_content_id)
			
			# 【优雅调用】清理该页面对应的所有草稿历史快照
			if page_id and hasattr(mongo_manager, 'delete_page_revisions'):
				mongo_manager.delete_page_revisions(page_id)
		
		except Exception as e:
			logger.error(f"级联清理 MongoDB 关联数据时遭遇异常: {e}", exc_info=True)
		return deletion_result
	
	# =========================================================================
	# 网关 4：前台数据读取网关 (用于博客详情页 serve 渲染时提取真实数据)
	# =========================================================================
	def get_content_from_mongodb(self) -> dict[str, Any] | None:
		"""读取正式正文，并在内存副本补齐前端 StreamField 所需的块 ID 和 value。"""
		if not getattr(self, 'mongo_content_id', None):
			return None
		try:
			mongo_manager = MongoManager()
			content = mongo_manager.get_blog_content(self.mongo_content_id)
			
			if not content or 'body' not in content or not isinstance(content['body'], list):
				return None
			
			# Mongo 中的历史块可能没有 id/value；这里仅补齐内存副本，不改变数据库原文。
			for block in content['body']:
				if isinstance(block, dict):
					if 'id' not in block or not block['id']:
						block['id'] = str(uuid.uuid4())
					if 'value' not in block:
						block['value'] = ""
			return content
		except Exception as e:
			return None
	
	# =========================================================================
	# 核心安全补丁：修复 Django 5.x 严格类型校验，防止未发布页面预览引发 ValueError
	# =========================================================================
	def get_prev_post(self) -> Any:
		"""返回同分类中较早发布的文章；没有分类时回退到全站文章。"""
		if not self.pk or not getattr(self, 'first_published_at', None): return None
		if not self.categories.exists():
			return BlogPage.objects.live().filter(first_published_at__lt=self.first_published_at).order_by(
				'-first_published_at').first()
		return BlogPage.objects.live().filter(categories__id__in=self.categories.values_list('id', flat=True),
		                                      first_published_at__lt=self.first_published_at).distinct().order_by(
			'-first_published_at').first()
	
	def get_next_post(self) -> Any:
		"""返回同分类中较晚发布的文章；没有分类时回退到全站文章。"""
		if not self.pk or not getattr(self, 'first_published_at', None): return None
		if not self.categories.exists():
			return BlogPage.objects.live().filter(first_published_at__gt=self.first_published_at).order_by(
				'first_published_at').first()
		return BlogPage.objects.live().filter(categories__id__in=self.categories.values_list('id', flat=True),
		                                      first_published_at__gt=self.first_published_at).distinct().order_by(
			'first_published_at').first()
	
	@staticmethod
	def _strip_markdown_code(text: object) -> str:
		"""移除围栏代码和行内代码，避免把代码中的美元符误判为数学公式。"""
		text = str(text or "")
		text = re.sub(r"(?ms)^[ \t]*(`{3,}|~{3,}).*?^\s*\1\s*$", "", text)
		return re.sub(r"`+[^`\n]*`+", "", text)

	@classmethod
	def _contains_math_markup(cls: type["BlogPage"], text: object) -> bool:
		# 先排除代码，再识别块级公式、转义公式和常见运算符，降低误报带来的 KaTeX 资源加载。
		text = cls._strip_markdown_code(text)
		if re.search(r"\$\$[\s\S]+?\$\$|\\\([\s\S]+?\\\)|\\\[[\s\S]+?\\\]", text):
			return True
		for match in re.finditer(r"(?<!\\)\$([^$\n]+?)(?<!\\)\$", text):
			formula = match.group(1).strip()
			if re.search(r"\\[A-Za-z]+|[=+*/^_<>]|\d\s*[-+]\s*\d", formula):
				return True
		return False

	@classmethod
	def get_frontend_resource_features(
		cls: type["BlogPage"], body_data: Any, has_gallery: bool = False
	) -> dict[str, bool]:
		"""扫描正文块一次，推导代码高亮、KaTeX、Mermaid 和媒体资源开关。"""
		features = {
			'has_code': False, 'has_katex': False, 'has_mermaid': False,
			'has_image': False, 'has_gallery': bool(has_gallery),
			'has_video': False, 'has_audio': False, 'has_table': False,
			'has_embed': False, 'has_document': False,
		}
		type_flags = {
			'mermaid_chart': 'has_mermaid', 'image_block': 'has_image',
			'video_block': 'has_video', 'audio_block': 'has_audio',
			'table_block': 'has_table', 'embed_block': 'has_embed',
			'document_block': 'has_document',
		}
		# 只扫描一次原始块列表，模板即可按开关决定是否加载代码高亮、公式和媒体资源。
		for block in body_data if isinstance(body_data, list) else []:
			if not isinstance(block, dict):
				continue
			block_type = block.get('type')
			value = block.get('value', '')
			flag = type_flags.get(block_type)
			if flag:
				features[flag] = True
			if block_type == 'code_block':
				features['has_code'] = True
			elif block_type in {'rich_text', 'raw_html'}:
				plain = str(value or '')
				features['has_code'] |= bool(re.search(r'<(?:pre|code)\b', plain, re.I))
				features['has_katex'] |= cls._contains_math_markup(strip_tags(plain))
			elif block_type == 'markdown_block':
				plain = str(value or '')
				features['has_code'] |= bool(re.search(r'(?m)^[ \t]*(`{3,}|~{3,})', plain) or re.search(r'(?m)^(?: {4}|\t)\S', plain))
				features['has_katex'] |= cls._contains_math_markup(plain)
		return features

	def get_context(self, request: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
		"""把标签页和正文资源开关加入页面模板上下文。"""
		context = super().get_context(request, *args, **kwargs)
		# 注入标签索引页，供模板生成标签跳转链接
		context['blog_tag_index_page'] = BlogTagIndexPage.objects.live().first()
		# 模板只消费一次查询结果，避免文章页为同一组导航重复访问数据库。
		context['related_posts'] = self.get_related_posts_by_tags()
		context['prev_post'] = self.get_prev_post()
		context['next_post'] = self.get_next_post()
		context['is_article_page'] = True
		article_url = request.build_absolute_uri(self.url) if self.url else request.build_absolute_uri()
		context['article_structured_data'] = {
			'@context': 'https://schema.org',
			'@type': 'Article',
			'headline': self.title,
			'description': self.search_description or strip_tags(str(self.intro or ''))[:180],
			'url': article_url,
			'datePublished': self.first_published_at.isoformat() if self.first_published_at else self.date.isoformat(),
			'dateModified': self.last_published_at.isoformat() if self.last_published_at else self.date.isoformat(),
		}
		context.update(getattr(self, '_frontend_resource_features', {}))
		return context

	def get_publish_quality_issues(self) -> list[str]:
		"""返回发布前可人工处理的内容质量问题，不阻断既有草稿保存流程。"""
		issues: list[str] = []
		if not self.title.strip():
			issues.append('缺少文章标题')
		if not strip_tags(str(self.intro or '')).strip():
			issues.append('缺少文章摘要')
		if not self.body:
			issues.append('正文为空')
		if not (self.search_description or '').strip():
			issues.append('未设置搜索摘要，将使用文章摘要作为 SEO 描述')
		if self.featured_image and not self.featured_image.default_alt_text.strip():
			issues.append('封面图缺少替代文本')
		return issues

	def serve(self, request: Any) -> Any:
		"""读取一次 Mongo 正文、计算资源开关并交给 Wagtail 渲染。"""
		# 读取一次 Mongo 正文，同时计算前端资源需求，避免模板阶段重复访问数据库。
		mongo_content = self.get_content_from_mongodb()
		body_data = mongo_content.get('body', []) if mongo_content else []
		self._frontend_resource_features = self.get_frontend_resource_features(
			body_data,
			has_gallery=self.gallery_images.exists(),
		)
		
		if mongo_content and 'body' in mongo_content:
			
			# 保持 Mongo 原始值不变，Markdown 由块在输出阶段渲染。
			source_body_data = mongo_content['body']
			
			# 只在内存中重建 StreamField，不修改 Mongo 中保存的值。
			try:
				self.body = MongoDBStreamFieldAdapter.from_mongodb(source_body_data, self.body.stream_block)
			except Exception as e:
				from wagtail.blocks.stream_block import StreamValue
				logger.error(f"使用适配器创建StreamValue失败: {e}", exc_info=True)
				self.body = StreamValue(self.body.stream_block, source_body_data, is_lazy=True)
		
		# 父类负责站点、预览和模板选择；此时 self.body 已经是可渲染的 StreamValue。
		response = super().serve(request)
		# 只在页面成功交给响应链路后标记统计对象，避免正文读取或模板渲染失败也被计入浏览量。
		if self.pk:
			request._blog_analytics_page_id = self.pk
		return response
	
	@property
	def body_text(self) -> str:
		"""ES 索引专用：从 MongoDB 拉取并拼接纯文本。"""
		return self.get_full_text_for_search()
	
	def get_full_text_for_search(self, content: Any = None) -> str:
		"""按块类型提取可搜索纯文本，不把 HTML 或 Markdown 标记送入索引。"""
		if content is None:
			content = self.get_content_from_mongodb()
		if not content or 'body' not in content:
			return ""
		body = content['body']
		if not isinstance(body, list):
			return ""
		text_parts = []
		# 不同块的 value 结构不同，分别提取标题、代码、表格单元格等有意义文本。
		for block in body:
			if not isinstance(block, dict):
				continue
			block_type = block.get('type')
			block_value = block.get('value')
			if not block_value:
				continue
			if block_type == 'rich_text':
				text_parts.append(strip_tags(str(block_value)))
			elif block_type == 'markdown_block':
				raw = block_value if isinstance(block_value, str) else str(block_value)
				raw = re.sub(r'[#*`>\-_[\](){}|]', ' ', raw)
				text_parts.append(raw)
			elif block_type == 'code_block':
				if isinstance(block_value, dict):
					text_parts.append(str(block_value.get('code', '')))
			elif block_type == 'mermaid_chart':
				if isinstance(block_value, dict):
					text_parts.append(str(block_value.get('code', '')))
			elif block_type == 'table_block':
				if isinstance(block_value, dict) and 'data' in block_value:
					for row in block_value['data']:
						if isinstance(row, list):
							text_parts.append(' '.join(str(cell) for cell in row if cell))
			elif block_type == 'raw_html':
				text_parts.append(strip_tags(str(block_value)))
			elif block_type == 'embed_block':
				if isinstance(block_value, dict) and block_value.get('title'):
					text_parts.append(str(block_value['title']))
		return ' '.join(filter(None, text_parts))
	
	
	def get_related_posts_by_tags(self, max_posts: int = 5) -> Any:
		"""按重合标签数排序获取公开相关文章；预览或无标签页面返回空 QuerySet。"""
		
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
		
		# 先按重合标签数，再按发布时间排序，让关联度最高且较新的文章靠前。
		related_posts = related_posts.annotate(
			same_tags=models.Count('tagged_items', filter=models.Q(tagged_items__tag_id__in=tag_ids))
		).order_by('-same_tags', '-first_published_at')[:max_posts]
		
		return related_posts
	
	
	def get_view_count(self) -> dict[str, int]:
		"""获取访问统计（委托给 PageViewCounter）"""
		if not self.pk:
			return {'today': 0, 'today_unique': 0, 'total': 0, 'total_unique': 0}
		return PageViewCounter(self.pk).get()

	def get_reactions(self) -> list[dict[str, Any]]:
		"""获取页面的反应统计"""
		
		if not self.pk:
			return []
		
		# 先取完整反应类型列表，再用聚合结果补齐没有记录的类型为 0。
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
	
	def user_has_reacted(self, request: Any) -> bool:
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
class PageView(models.Model):
    page = models.ForeignKey('wagtailcore.Page', on_delete=models.CASCADE, related_name='page_views')
    date = models.DateField()  # 访问日期，用于按天查询
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    ip_address = models.GenericIPAddressField()
    user_agent = models.CharField(max_length=255, blank=True)
    visitor_key = models.CharField(max_length=64, null=True, blank=True)
    view_count = models.PositiveIntegerField(default=0)
    first_viewed_at = models.DateTimeField(null=True, blank=True)
    last_viewed_at = models.DateTimeField()  # 当天最后一次访问时间，可更新
    source_category = models.CharField(max_length=20, default='direct')
    referrer_host = models.CharField(max_length=255, blank=True)
    engaged = models.BooleanField(default=False)
    max_scroll_percent = models.PositiveSmallIntegerField(default=0)
    scroll_50_reached = models.BooleanField(default=False)
    scroll_90_reached = models.BooleanField(default=False)
    active_reading_seconds = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "页面访问记录"
        verbose_name_plural = "页面访问记录"
        indexes = [
            models.Index(fields=['page', 'date']),
            models.Index(fields=['page', 'date', 'ip_address']),  # 兼容旧审计检索
            models.Index(fields=['date', 'source_category']),
        ]
        constraints = [
            # NULL 兼容尚未回填的历史记录；新记录始终写入HMAC摘要，因此可由数据库保证并发去重。
            models.UniqueConstraint(
                fields=['page', 'date', 'visitor_key'],
                name='blog_pageview_page_date_visitor_uniq',
            ),
        ]

    def admin_page_title(self) -> str:
        """返回只读 Wagtail 列表中显示的页面标题。"""
        return self.page.title

    admin_page_title.short_description = "访问页面"
    admin_page_title.admin_order_field = "page__title"

    def admin_user(self) -> str:
        """为已登录用户和访客返回稳定的后台显示标签。"""
        return self.user.get_username() if self.user_id else "访客"

    admin_user.short_description = "用户"
    admin_user.admin_order_field = "user__username"

    def __str__(self) -> str:
        """返回页面、访问者和 IP 组成的访问记录摘要。"""
        page_title = self.page.title
        user_label = self.user.get_username() if self.user_id else "访客"
        last_viewed = timezone.localtime(self.last_viewed_at).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        return (
            f"{page_title} | 用户：{user_label} | IP：{self.ip_address} "
            f"| 最后访问：{last_viewed}"
        )

# 访问统计聚合模型
@register_snippet
class PageViewCount(models.Model):
	"""按页面和日期汇总访问指标；旧字段与 v2 字段并存以保持历史报表兼容。"""
	page = models.ForeignKey('wagtailcore.Page', on_delete=models.CASCADE, related_name='view_counts')
	date = models.DateField()
	# 下列两个字段保留旧口径，不能与V2字段相加或回填。
	count = models.PositiveIntegerField(default=0)
	unique_count = models.PositiveIntegerField(default=0)
	view_count_v2 = models.PositiveBigIntegerField(default=0)
	unique_visitor_count_v2 = models.PositiveBigIntegerField(default=0)
	engaged_visitor_count = models.PositiveBigIntegerField(default=0)
	scroll_50_visitor_count = models.PositiveBigIntegerField(default=0)
	scroll_90_visitor_count = models.PositiveBigIntegerField(default=0)
	active_reading_seconds = models.PositiveBigIntegerField(default=0)
	v2_started_at = models.DateTimeField(null=True, blank=True)
	
	class Meta:
		verbose_name = "页面访问统计"
		verbose_name_plural = "页面访问统计"
		unique_together = ('page', 'date')
	
	def __str__(self) -> str:
		"""返回便于后台识别页面、日期和访问次数的摘要。"""
		return f"{self.page.title} - {self.date} - {self.view_count_v2}次浏览"


class PageTrafficSourceDaily(models.Model):
	"""按日保存来源聚合，避免后台报表扫描短期访问审计明细。"""

	page = models.ForeignKey('wagtailcore.Page', on_delete=models.CASCADE, related_name='traffic_sources')
	date = models.DateField()
	source_category = models.CharField(max_length=20)
	view_count = models.PositiveBigIntegerField(default=0)
	unique_visitor_count = models.PositiveBigIntegerField(default=0)

	class Meta:
		verbose_name = "页面流量来源统计"
		verbose_name_plural = "页面流量来源统计"
		constraints = [
			models.UniqueConstraint(
				fields=['page', 'date', 'source_category'],
				name='blog_traffic_source_daily_uniq',
			),
		]
		indexes = [models.Index(fields=['date', 'source_category'])]


class ArticleEngagementSession(models.Model):
	"""保存短期阅读会话的最新绝对状态，使Beacon重试不会重复累计。"""

	page = models.ForeignKey('wagtailcore.Page', on_delete=models.CASCADE, related_name='engagement_sessions')
	date = models.DateField()
	visitor_key = models.CharField(max_length=64)
	session_id = models.UUIDField()
	sequence = models.PositiveIntegerField(default=0)
	engaged = models.BooleanField(default=False)
	max_scroll_percent = models.PositiveSmallIntegerField(default=0)
	active_reading_seconds = models.PositiveIntegerField(default=0)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		verbose_name = "文章阅读会话"
		verbose_name_plural = "文章阅读会话"
		constraints = [
			models.UniqueConstraint(
				fields=['page', 'session_id'],
				name='blog_engagement_page_session_uniq',
			),
		]
		indexes = [models.Index(fields=['date', 'visitor_key'])]


class FeedRequestDaily(models.Model):
	"""按订阅源范围聚合真实的RSS和Atom响应，不把请求数称为订阅人数。"""

	SCOPE_GLOBAL = 'global'
	SCOPE_TAG = 'tag'
	SCOPE_AUTHOR = 'author'
	SCOPE_CHOICES = [
		(SCOPE_GLOBAL, '全站'),
		(SCOPE_TAG, '标签'),
		(SCOPE_AUTHOR, '作者'),
	]
	FORMAT_RSS = 'rss'
	FORMAT_ATOM = 'atom'
	FORMAT_CHOICES = [(FORMAT_RSS, 'RSS'), (FORMAT_ATOM, 'Atom')]

	site = models.ForeignKey('wagtailcore.Site', on_delete=models.CASCADE, related_name='feed_request_counts')
	locale = models.ForeignKey('wagtailcore.Locale', on_delete=models.CASCADE, related_name='feed_request_counts')
	date = models.DateField()
	scope_type = models.CharField(max_length=10, choices=SCOPE_CHOICES)
	scope_id = models.PositiveIntegerField(default=0)
	scope_slug = models.CharField(max_length=255, blank=True)
	scope_label = models.CharField(max_length=255, blank=True)
	feed_format = models.CharField(max_length=10, choices=FORMAT_CHOICES)
	response_200_count = models.PositiveBigIntegerField(default=0)
	response_304_count = models.PositiveBigIntegerField(default=0)
	estimated_client_count = models.PositiveBigIntegerField(default=0)

	class Meta:
		verbose_name = "订阅源请求统计"
		verbose_name_plural = "订阅源请求统计"
		constraints = [
			models.UniqueConstraint(
				fields=['site', 'locale', 'date', 'scope_type', 'scope_id', 'feed_format'],
				name='blog_feed_request_daily_uniq',
			),
		]
		indexes = [models.Index(fields=['date', 'scope_type', 'scope_id'])]


class FeedClientDaily(models.Model):
	"""短期保留每日客户端摘要，仅用于估算活跃客户端。"""

	site = models.ForeignKey('wagtailcore.Site', on_delete=models.CASCADE)
	locale = models.ForeignKey('wagtailcore.Locale', on_delete=models.CASCADE)
	date = models.DateField()
	scope_type = models.CharField(max_length=10, choices=FeedRequestDaily.SCOPE_CHOICES)
	scope_id = models.PositiveIntegerField(default=0)
	feed_format = models.CharField(max_length=10, choices=FeedRequestDaily.FORMAT_CHOICES)
	client_key = models.CharField(max_length=64)

	class Meta:
		verbose_name = "订阅源客户端摘要"
		verbose_name_plural = "订阅源客户端摘要"
		constraints = [
			models.UniqueConstraint(
				fields=['site', 'locale', 'date', 'scope_type', 'scope_id', 'feed_format', 'client_key'],
				name='blog_feed_client_daily_uniq',
			),
		]



# 反应类型模型
@register_snippet
class ReactionType(models.Model):
	"""定义可供前台选择的反应类型及其展示顺序。"""
	name = models.CharField("反应名称", max_length=50)
	icon = models.CharField("图标CSS类", max_length=50)
	display_order = models.PositiveSmallIntegerField("显示顺序", default=0)
	
	class Meta:
		verbose_name = "反应类型"
		verbose_name_plural = "反应类型"
		ordering = ['display_order']
	
	def __str__(self) -> str:
		"""返回反应类型名称，供后台选择器和日志使用。"""
		return self.name


# 用户反应模型
@register_snippet
class Reaction(models.Model):
	"""记录页面反应；登录用户按用户唯一，匿名用户按会话和 IP 约束重复提交。"""
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
	
	def __str__(self) -> str:
		"""返回用户、反应类型和页面标题组成的后台摘要。"""
		user_str = self.user.username if self.user else f"匿名({self.session_key[:10]})"
		return f"{user_str} - {self.reaction_type.name} - {self.page.title}"


@register_snippet
class Author(models.Model):
	"""保存作者资料、头像和可选的富文本简介，供文章元数据复用。"""
	
	name = models.CharField(max_length=255)  # 作者名称
	slug = models.SlugField(max_length=255, unique=True, blank=True, allow_unicode=True)
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
		FieldPanel('slug'),
		FieldPanel('author_image'),
		FieldPanel('bio', heading="个人简介"),  # 使用 StreamFieldPanel 显示富文本简介
	]  # 在管理界面中显示的字段
	
	def __str__(self) -> str:
		"""返回作者名称作为后台显示文本。"""
		return self.name

	def save(self, *args: object, **kwargs: object) -> None:
		"""新作者生成稳定地址；改名不改变已有订阅地址。"""
		if not self.slug:
			base_slug = slugify(self.name, allow_unicode=True) or "author"
			candidate = base_slug
			counter = 2
			while type(self).objects.exclude(pk=self.pk).filter(slug=candidate).exists():
				candidate = f"{base_slug}-{counter}"
				counter += 1
			self.slug = candidate
		return super().save(*args, **kwargs)
	
	# 在 Author 类中添加这个方法
	def get_bio_preview(self, word_limit: int = 3) -> str:
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
	
	def get_bio_preview_html(self, word_limit: int = 3) -> str:
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


class MarkdownImportBatchStatus(models.TextChoices):
	"""Markdown 批次在幂等处理和失败补偿流程中的状态集合。"""
	PENDING = 'pending', '待处理'
	PROCESSING = 'processing', '处理中'
	SUCCESS = 'success', '成功'
	PARTIAL_SUCCESS = 'partial_success', '部分成功'
	FAILED = 'failed', '失败'
	CLEANUP_RETRY = 'cleanup_retry', '等待补偿清理'


class MarkdownImportSessionStatus(models.TextChoices):
	"""Markdown 分片上传会话的生命周期状态集合。"""
	CREATED = 'created', '已创建'
	UPLOADING = 'uploading', '上传中'
	READY = 'ready', '待组装'
	ASSEMBLING = 'assembling', '组装中'
	SUCCESS = 'success', '成功'
	PARTIAL_SUCCESS = 'partial_success', '部分成功'
	FAILED = 'failed', '失败'
	EXPIRED = 'expired', '已过期'


class MarkdownImportArtifactStatus(models.TextChoices):
	"""单个导入媒体从待处理到成功或缺失失败的状态集合。"""
	PENDING = 'pending', '待处理'
	PROCESSING = 'processing', '处理中'
	SUCCEEDED = 'succeeded', '成功'
	FAILED_MISSING = 'failed_missing', '失败并插入缺失标记'


class MarkdownImportArtifactCleanupStatus(models.TextChoices):
	"""导入媒体对象清理任务的重试状态集合。"""
	NONE = 'none', '无需清理'
	PENDING = 'pending', '等待清理'
	RETRY = 'retry', '清理重试'
	CLEANED = 'cleaned', '已清理'


class MarkdownImportBatch(models.Model):
	"""保存一次导入的幂等归属与补偿边界，不复制文章正文。"""

	batch_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
	user = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.PROTECT,
		related_name='markdown_import_batches',
	)
	idempotency_key = models.UUIDField()
	request_fingerprint = models.CharField(max_length=64)
	status = models.CharField(
		max_length=24,
		choices=MarkdownImportBatchStatus.choices,
		default=MarkdownImportBatchStatus.PENDING,
	)
	target_parent = models.ForeignKey(
		'wagtailcore.Page',
		on_delete=models.PROTECT,
		related_name='markdown_import_batches',
	)
	result_page = models.ForeignKey(
		'wagtailcore.Page',
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name='+',
	)
	result_revision_id = models.PositiveBigIntegerField(null=True, blank=True)
	mongo_content_id = models.CharField(max_length=64, blank=True, default='')
	test_run_id = models.UUIDField(null=True, blank=True)
	error_code = models.CharField(max_length=64, blank=True, default='')
	# 错误信息只能保存脱敏且截断后的诊断，不能写正文、凭据或本地绝对路径。
	error_message = models.TextField(max_length=2000, blank=True, default='')
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)
	completed_at = models.DateTimeField(null=True, blank=True)

	class Meta:
		verbose_name = 'Markdown 导入批次'
		verbose_name_plural = 'Markdown 导入批次'
		indexes = [
			models.Index(
				fields=('status', 'updated_at'),
				name='blog_md_batch_stat_upd_idx',
			),
		]
		constraints = [
			models.UniqueConstraint(
				fields=('user', 'idempotency_key'),
				name='blog_md_import_user_key_uq',
			),
		]


class MarkdownImportToken(models.Model):
	"""保存客户端导入密钥的哈希；明文只在创建后的后台提示中出现一次。"""

	name = models.CharField(max_length=120)
	user = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.CASCADE,
		related_name='markdown_import_tokens',
	)
	token_prefix = models.CharField(max_length=24, editable=False)
	token_hash = models.CharField(max_length=64, unique=True, editable=False)
	scopes = models.JSONField(default=list)
	expires_at = models.DateTimeField(
		null=True,
		blank=True,
		help_text='可选；填写 YYYY-MM-DD HH:MM，留空表示不过期。',
	)
	revoked_at = models.DateTimeField(null=True, blank=True)
	last_used_at = models.DateTimeField(null=True, blank=True)
	created_at = models.DateTimeField(auto_now_add=True)

	class Meta:
		verbose_name = 'Markdown 导入 Token'
		verbose_name_plural = 'Markdown 导入 Token'
		ordering = ('-created_at',)

	def issue_plaintext(self) -> str:
		"""生成一次性明文 Token，并仅将前缀和 SHA-256 摘要写入模型。"""
		value = 'mdimp_' + secrets.token_urlsafe(32)
		self.token_prefix = value[:16]
		self.token_hash = hashlib.sha256(value.encode('utf-8')).hexdigest()
		return value

	def is_valid(self) -> bool:
		"""判断 Token 当前未撤销且未超过可选的过期时间。"""
		return not self.revoked_at and (self.expires_at is None or self.expires_at > timezone.now())


class MarkdownImportSession(models.Model):
	"""保存大批量导入会话的清单与恢复状态，正文只保留在受控 JSON 字段。"""

	session_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
	batch = models.OneToOneField(
		MarkdownImportBatch,
		on_delete=models.CASCADE,
		related_name='upload_session',
	)
	manifest = models.JSONField()
	status = models.CharField(
		max_length=24,
		choices=MarkdownImportSessionStatus.choices,
		default=MarkdownImportSessionStatus.CREATED,
	)
	total_artifacts = models.PositiveIntegerField(default=0)
	total_bytes = models.BigIntegerField(default=0)
	completed_artifacts = models.PositiveIntegerField(default=0)
	expires_at = models.DateTimeField()
	assembly_requested_at = models.DateTimeField(null=True, blank=True)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		verbose_name = 'Markdown 大批量导入会话'
		verbose_name_plural = 'Markdown 大批量导入会话'
		indexes = [
			models.Index(
				fields=('status', 'expires_at'),
				name='blog_md_session_stat_exp_idx',
			),
		]


class MarkdownImportArtifact(models.Model):
	"""记录单个媒体的精确对象证据，补偿时禁止按前缀扫描删除。"""

	artifact_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
	batch = models.ForeignKey(
		MarkdownImportBatch,
		on_delete=models.CASCADE,
		related_name='artifacts',
	)
	session = models.ForeignKey(
		MarkdownImportSession,
		null=True,
		blank=True,
		on_delete=models.CASCADE,
		related_name='artifacts',
	)
	position = models.PositiveIntegerField()
	media_type = models.CharField(max_length=16)
	source_kind = models.CharField(max_length=24)
	normalized_source = models.CharField(max_length=2048)
	# MySQL utf8mb4 无法为 2048 字符来源直接建立唯一索引，使用摘要约束而保留完整来源证据。
	normalized_source_hash = models.CharField(max_length=64)
	safe_filename = models.CharField(max_length=255)
	status = models.CharField(
		max_length=24,
		choices=MarkdownImportArtifactStatus.choices,
		default=MarkdownImportArtifactStatus.PENDING,
	)
	storage_alias = models.CharField(max_length=64, blank=True, default='')
	object_name = models.CharField(max_length=1024, blank=True, default='')
	sha256 = models.CharField(max_length=64, blank=True, default='')
	media_model = models.CharField(max_length=100, blank=True, default='')
	media_object_id = models.PositiveBigIntegerField(null=True, blank=True)
	error_code = models.CharField(max_length=64, blank=True, default='')
	cleanup_status = models.CharField(
		max_length=16,
		choices=MarkdownImportArtifactCleanupStatus.choices,
		default=MarkdownImportArtifactCleanupStatus.NONE,
	)
	cleanup_error_code = models.CharField(max_length=64, blank=True, default='')
	cleanup_attempts = models.PositiveIntegerField(default=0)
	cleanup_next_attempt_at = models.DateTimeField(null=True, blank=True)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)
	uploaded_at = models.DateTimeField(null=True, blank=True)

	class Meta:
		verbose_name = 'Markdown 导入媒体'
		verbose_name_plural = 'Markdown 导入媒体'
		constraints = [
			models.UniqueConstraint(
				fields=('batch', 'source_kind', 'normalized_source_hash'),
				name='blog_md_artifact_source_uq',
			),
		]
		indexes = [
			models.Index(
				fields=('status', 'updated_at'),
				name='blog_md_art_stat_upd_idx',
			),
			models.Index(
				fields=('cleanup_status', 'updated_at'),
				name='blog_md_art_clean_upd_idx',
			),
		]
