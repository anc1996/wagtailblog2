# 博客正文使用的 StreamField 块

"""博客正文使用的 StreamField 块及其兼容渲染逻辑。"""

import mimetypes
from typing import Any

from django import forms
from django.db import models
from django.utils.functional import cached_property
from wagtail.embeds.blocks import EmbedBlock
from wagtailmedia.blocks import AudioChooserBlock, VideoChooserBlock
from wagtail.contrib.table_block.blocks import TableBlock as WagtailTableBlock
from wagtail.documents.models import Document, AbstractDocument
from django.template.loader import render_to_string
from wagtail import blocks


from wagtailcodeblock.blocks import CodeBlock
from .widgets import VditorMarkdownWidget
from .markdown_renderer import MarkdownRenderer


class VditorMarkdownBlock(blocks.TextBlock):
    """使用项目自有 Vditor 编辑器的 Markdown 块。"""

    @cached_property
    def field(self) -> forms.CharField:
        # 每个块实例复用同一个表单字段，隐藏文本域仍是提交和存储的唯一来源。
        field_kwargs = {
            "widget": VditorMarkdownWidget(attrs={"rows": self.rows})
        }
        field_kwargs.update(self.field_options)
        return forms.CharField(**field_kwargs)

    def render_basic(self, value: str, context: Any = None) -> str:
        # 渲染时才把 Markdown 转成 HTML，避免改变 MongoDB 中保存的原始字符串。
        return MarkdownRenderer.render(value, context)

# 纯净前端代码块：只替换前台模板，不改变后台编辑行为。
class PureCodeBlock(CodeBlock):
    """使用博客专用模板渲染代码，同时保留 Wagtail 原生编辑字段。"""
    class Meta:
        # 强制指定前台输出的模板路径
        template = 'blog/streams/code_block.html'
        icon = 'code'
        label = '代码块(纯净版)'



MERMAID_RENDERER_LEGACY = "legacy-v11-current"
MERMAID_RENDERER_MODERN = "modern-v11.12"


class MermaidBlock(blocks.StructBlock):
	"""
	一个专门用于 Mermaid 图表代码的 StreamField Block。
	"""
	code = blocks.TextBlock(
		label="Mermaid 代码",
		required=True,
		help_text="在此处粘贴您的 Mermaid.js 语法代码 (例如: graph TD; A-->B;)"
	)
	# 渲染器标识属于内容契约；旧 Mongo 数据缺少该字段时由 to_python 归入兼容渲染器。
	renderer = blocks.ChoiceBlock(
		label="渲染器",
		required=False,
		default=MERMAID_RENDERER_MODERN,
		choices=(
			("", "未标记（按旧版兼容）"),
			(MERMAID_RENDERER_LEGACY, "旧版 Mermaid（兼容历史内容）"),
			(MERMAID_RENDERER_MODERN, "Modern Mermaid 11.12"),
		),
		help_text="新图表使用 Modern Mermaid；历史图表保持旧版，确认后再升级。",
	)

	def to_python(self, value: Any) -> Any:
		# 只在读取已有正文时补充内存中的兼容标识，不对 MongoDB 做批量迁移或写回。
		if isinstance(value, dict) and "code" in value and "renderer" not in value:
			value = dict(value)
			value["renderer"] = MERMAID_RENDERER_LEGACY
		return super().to_python(value)

	class Meta:
		icon = 'code'  # 在编辑器中显示一个代码图标
		label = 'Mermaid 图表'

		# 1. 前台展示模板（读者看文章时调用，带放大缩小按钮）
		template = 'blog/streams/mermaid_block.html'

		# 2. 后台编辑模板（作者写文章时调用，左边代码右边预览）
		form_template = 'blog/admin/mermaid_block_form.html'


# 自定义文档模型
class BlogDocument(AbstractDocument):
	"""扩展 Wagtail 文档模型，增加可在后台编辑的文档描述。"""

	description = models.TextField(blank=True)  # 文档描述
	admin_form_fields = Document.admin_form_fields + ('description',)  # 添加描述字段到后台表单


class AudioBlock(AudioChooserBlock):
	"""音频选择器块，使用外部模板渲染并向模板传递完整媒体对象。"""

	def render(self, value: Any, context: Any = None) -> str:
		# 调用 render_to_string，将渲染工作交给模板文件
		return render_to_string(
			"blog/streams/audio_block.html",
			{
				'value': value,  # 将完整的音频对象传递给模板
			}
		)


class VideoBlock(VideoChooserBlock):
	"""
	视频选择器块 - 使用外部模板进行Gretzia风格渲染
	"""

	def render(self, value: Any, context: Any = None) -> str:
		"""
		使用视频块模板渲染，并保留文章渲染上下文。
		"""
		# 模板内部处理空值，避免旧文章中的空视频块直接消失。
		template_context = context.copy() if context else {}
		template_context['value'] = value
		template_context['video_type'] = self._get_video_type(value)
		return render_to_string(
			"blog/streams/video_block.html",
			template_context,
		)

	@staticmethod
	def _get_video_type(value: Any) -> str:
		"""在旧媒体记录缺少 content_type 时，根据文件名推断视频类型。"""
		if not value:
			return 'video/mp4'

		content_type = getattr(getattr(value, 'file', None), 'content_type', None)
		if content_type and content_type.startswith('video/'):
			return content_type

		file_name = getattr(getattr(value, 'file', None), 'name', '')
		media_path = (file_name or getattr(value, 'url', '')).lower().split('?', 1)[0]
		extension_types = {
			'.mp4': 'video/mp4',
			'.m4v': 'video/x-m4v',
			'.webm': 'video/webm',
			'.mov': 'video/quicktime',
			'.ogv': 'video/ogg',
			'.ogg': 'video/ogg',
		}
		# 优先使用扩展名映射，覆盖对象元数据缺失但文件名可靠的历史数据。
		for extension, video_type in extension_types.items():
			if media_path.endswith(extension):
				return video_type

		guessed_type, _ = mimetypes.guess_type(media_path)
		return guessed_type if guessed_type and guessed_type.startswith('video/') else 'video/mp4'


# 添加自定义TableBlock类
class CustomTableBlock(WagtailTableBlock):
	"""自定义表格块，继承自 Wagtail 的 TableBlock。"""

	def render(self, value: Any, context: Any = None) -> str:
		"""
		覆盖render方法，使用自定义模板。
		这个实现参考了 Wagtail 官方 TableBlock 的 render 方法，
		以确保所有需要的数据（包括单元格合并信息）都被传递到模板。
		"""
		template = getattr(self.meta, "template", None)
		if not template or not value:
			return ""

		# 表格块的 value 同时包含数据、表头和合并单元格元数据，不能只传二维数组。
		table_header = (
			value["data"][0]
			if value.get("data") and len(value["data"]) > 0 and value.get("first_row_is_table_header")
			else None
		)
		table_data = (
			value["data"][1:] if table_header else value.get("data", [])
		)

		# 复制调用方上下文，避免向 Wagtail 共享的上下文对象写入临时变量。
		new_context = context.copy() if context else {}

		# 更新上下文，加入表格所需的所有变量
		new_context.update({
			'self': value,
			self.TEMPLATE_VAR: value,
			'table_header': table_header,
			'data': table_data,
			'first_col_is_header': value.get("first_col_is_header", False),
			'html_renderer': self.is_html_renderer(),
			'table_caption': value.get("table_caption"),
		})

		# 补充单元格样式和合并范围，模板据此还原编辑器中的视觉结构。
		# 处理单元格的 CSS 类名
		if value.get("cell"):
			new_context["classnames"] = {
				(meta["row"], meta["col"]): meta["className"]
				for meta in value["cell"] if "className" in meta
			}

		# 处理合并单元格的行跨度和列跨度。
		if value.get("mergeCells"):
			new_context["spans"] = {
				(merge["row"], merge["col"]): {
					"rowspan": merge["rowspan"],
					"colspan": merge["colspan"],
				}
				for merge in value["mergeCells"]
			}

		return render_to_string(template, new_context)

	class Meta:
		template = "blog/streams/table_block.html"
		icon = "table"
		label = "表格"
		help_text = "创建一个包含标题和表头的表格。"


# 声明一个高级自定义嵌入块类
class CustomEmbedBlock(blocks.StructBlock):
    """统一保存媒体标题和嵌入地址，并兼容旧版纯字符串 URL 数据。"""
    title = blocks.CharBlock(
        required=True,
        label="媒体标题 / 无障碍描述",
        help_text="请输入此视频或音频的真实名称。"
    )
    embed_url = EmbedBlock(
        label="流媒体链接",
        help_text="直接粘贴 B站、YouTube、优酷、腾讯、网易云、QQ音乐等平台的单页链接"
    )

    # 兼容旧数据：历史版本可能只保存了一个纯字符串 URL。
    def to_python(self, value: Any) -> Any:
        # 如果从数据库读出来的数据是一个纯字符串（旧版的纯 URL 数据）
        if isinstance(value, str):
            # 包装成当前 StructBlock 所需的字典结构，避免迁移历史正文。
            value = {
                'title': '历史媒体档案',  # 给以前的老视频一个默认的兜底标题
                'embed_url': value
            }
        # 交给 Wagtail 完成字段级反序列化和校验。
        return super().to_python(value)

    class Meta:
        template = 'blog/streams/embed_block.html'
        icon = 'media'
        label = '高级多媒体嵌入'
