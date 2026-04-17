# base/models.py
from django.db import models
from django.core.exceptions import ValidationError  # 添加 ValidationError 导入
from django.core.validators import validate_email  # 添加 validate_email 导入
from django.template.response import TemplateResponse
from django.contrib import messages
import logging  # 添加日志模块导入

from modelcluster.fields import ParentalKey
from wagtail.admin.panels import (
	FieldPanel,  # 字段面板：用于在 Wagtail 管理界面中显示单个字段的面板。
	MultiFieldPanel,  # 多字段面板：用于在 Wagtail 管理界面中显示多个字段的面板。
	PublishingPanel, InlinePanel, FieldRowPanel  # 发布面板：用于处理与发布相关的操作和信息的面板。
)
from wagtail.contrib.forms.models import AbstractEmailForm, AbstractFormField
from wagtail.contrib.forms.panels import FormSubmissionsPanel
from wagtail.contrib.settings.models import (
	BaseGenericSetting,  # 定义适用于所有网页（而不仅仅是一个页面）的设置模型。
	register_setting,  # 注册设置模型的装饰器。
)
from wagtail.fields import RichTextField
from wagtail.models import (
	DraftStateMixin,  # 草稿状态混合类：用于处理草稿状态的模型混合类。
	PreviewableMixin,  # 可预览混合类：用于处理预览功能的模型混合类。
	RevisionMixin,  # 修订混合类：用于处理版本控制和修订的模型混合类。
	TranslatableMixin,  # 可翻译混合类：用于处理多语言翻译的模型混合类。
)

from wagtail.snippets.models import register_snippet

# 初始化日志记录器
logger = logging.getLogger(__name__)


# register_setting 是一个装饰器，用于将设置模型注册到 Wagtail 的设置系统中。
@register_setting
class NavigationSettings(BaseGenericSetting):
	# 定义导航设置的字段
	linkedin_url = models.URLField(verbose_name="LinkedIn URL", blank=True)  # LinkedIn URL
	github_url = models.URLField(verbose_name="GitHub URL", blank=True)  # GitHub URL
	bilibili_url = models.URLField(verbose_name="Bilibili URL", blank=True)  # 哔哩哔哩 URL
	wechat_url = models.URLField(verbose_name="WeChat URL", blank=True)  # 微信 URL
	instagram_url = models.URLField(verbose_name="Instagram URL", blank=True)  # Instagram URL
	
	panels = [
		MultiFieldPanel(
			[
				FieldPanel('linkedin_url'),
				FieldPanel('github_url'),
				FieldPanel('bilibili_url'),
				FieldPanel('wechat_url'),
				FieldPanel('instagram_url'),
			],
			"社交链接",
		)
	]
	
	class Meta:
		verbose_name = "社交链接"


@register_snippet
class FooterText(
	DraftStateMixin,
	RevisionMixin,
	PreviewableMixin,
	TranslatableMixin,
	models.Model,
):
	body = RichTextField()  # 页脚文本字段
	
	panels = [
		FieldPanel("body"),
		PublishingPanel(),  # 发布面板：用于处理与发布相关的操作和信息的面板。
	]
	
	def __str__(self):
		return "Footer text"  # 返回字符串表示形式
	
	def get_preview_template(self, request, mode_name):
		# 返回预览模板的路径
		return "base.html"
	
	def get_preview_context(self, request, mode_name):
		# 返回预览上下文
		return {"footer_text": self.body}
	
	class Meta(TranslatableMixin.Meta):
		verbose_name_plural = "页脚文本"


#  保留 NavigationSettings 和 FooterText 的定义。添加 FormField 和 FormPage：
class FormField(AbstractFormField):
	"""表单字段模型"""
	
	FIELD_TYPE_CHOICES = [
		('singleline', '单行文本'),
		('multiline', '多行文本'),
		('email', '邮箱'),
		('number', '数字'),
		('url', 'URL'),
		('checkbox', '复选框'),
		('checkboxes', '多选框'),
		('dropdown', '下拉选择'),
		('radio', '单选按钮'),
		('date', '日期'),
		('datetime', '日期时间'),
		('hidden', '隐藏字段'),
	]
	
	page = ParentalKey('FormPage', on_delete=models.CASCADE, related_name='form_fields')


class FormPage(AbstractEmailForm):
	"""联系人表单页面模型 - 异步邮件发送版本"""
	
	template = "base/form_page.html"
	landing_page_template = "base/form_page_landing.html"
	
	intro = RichTextField(
		blank=True,
		help_text="表单上方的介绍文本",
		features=['ai','bold', 'italic', 'link', 'ol', 'ul']
	)
	
	thank_you_text = RichTextField(
		blank=True,
		help_text="表单提交成功后显示的感谢文本",
		features=['ai','bold', 'italic', 'link', 'ol', 'ul']
	)
	
	feature_image = models.ForeignKey(
		'blog.BlogImage',
		null=True,
		blank=True,
		on_delete=models.SET_NULL,
		related_name='+',
		help_text="表单页面的特色图片"
	)
	
	send_confirmation_email = models.BooleanField(
		default=True,
		help_text="是否向提交者发送确认邮件（异步发送，同时抄送管理员）"
	)
	
	confirmation_email_subject = models.CharField(
		max_length=255,
		blank=True,
		help_text="确认邮件主题"
	)
	
	confirmation_email_text = RichTextField(
		blank=True,
		help_text="确认邮件内容",
		features=['ai','bold', 'italic', 'link']
	)
	
	content_panels = AbstractEmailForm.content_panels + [
		FormSubmissionsPanel(),
		
		MultiFieldPanel([
			FieldPanel('intro'),
			FieldPanel('feature_image'),
			FieldPanel('thank_you_text'),
		], "页面内容设置"),
		
		InlinePanel('form_fields', label="表单字段", help_text="注意：用户邮箱字段已自动添加，无需重复配置"),
		
		MultiFieldPanel([
			FieldPanel('to_address',
			           help_text="管理员邮箱地址，支持多个邮箱用逗号分隔。例如：admin@example.com, support@example.com"),
			FieldPanel('subject', help_text="发送给管理员的邮件主题，用于通知有新的表单提交"),
		], "管理员邮件通知"),
		
		MultiFieldPanel([
			FieldPanel('send_confirmation_email', help_text="开启后，用户提交表单时会异步收到确认邮件，同时抄送给管理员"),
			FieldPanel('confirmation_email_subject', help_text="用户确认邮件的主题"),
			FieldPanel('confirmation_email_text', help_text="用户确认邮件的正文内容"),
		], "用户确认邮件"),
		
		MultiFieldPanel([
			FieldPanel('from_address',
			           help_text="⚠️ 此字段已弃用。系统将自动使用 settings.DEFAULT_FROM_EMAIL 作为发件人地址"),
		], "系统设置（仅供参考）", classname="collapsed"),
	]
	
	def clean(self):
		"""表单验证"""
		super().clean()
		
		if self.to_address:
			emails = [email.strip() for email in self.to_address.split(',')]
			
			for email in emails:
				if email:
					try:
						validate_email(email)
					except ValidationError:
						raise ValidationError(f"邮箱地址格式不正确：{email}")
		
		if self.send_confirmation_email and not self.confirmation_email_subject:
			self.confirmation_email_subject = f"表单提交确认 - {self.title}"
	
	def get_email_recipients(self):
		"""获取所有邮件收件人列表"""
		recipients = []
		
		if self.to_address:
			admin_emails = [email.strip() for email in self.to_address.split(',') if email.strip()]
			recipients.extend(admin_emails)
		
		return recipients
	
	def extract_user_email_from_request(self, request):
		"""从HTTP请求中提取用户邮箱"""
		if request.method == 'POST':
			user_email = request.POST.get('email', '').strip()
			if user_email:
				logger.info(f"FormPage {self.id}: 从POST数据获取邮箱: {user_email}")
				return user_email
		
		logger.warning(f"FormPage {self.id}: 无法从请求中获取邮箱地址")
		return None
	
	def get_user_email_from_form_data(self, form_data):
		"""从表单数据中获取用户邮箱"""
		if not form_data:
			return None
		
		data_dict = {}
		try:
			if hasattr(form_data, 'items'):
				data_dict = dict(form_data)
			elif hasattr(form_data, '__dict__'):
				data_dict = form_data.__dict__
			else:
				return None
		except Exception:
			return None
		
		# 查找邮箱字段
		user_email = data_dict.get('email')
		if user_email and isinstance(user_email, str) and user_email.strip():
			return user_email.strip()
		
		# 查找包含email关键词的字段
		for field_name, field_value in data_dict.items():
			if isinstance(field_name, str) and isinstance(field_value, str):
				field_name_lower = field_name.lower()
				if ('email' in field_name_lower or 'mail' in field_name_lower) and field_value.strip():
					return field_value.strip()
		
		return None
	
	def send_confirmation_email_async(self, form_data, submission=None):
		"""异步发送确认邮件 - 完全保持原有的邮件发送逻辑"""
		try:
			from base.tasks import send_form_confirmation_email
			
			user_email = form_data.get('email')
			if not user_email:
				logger.error(f"FormPage {self.id}: 确认邮件发送失败 - 缺少用户邮箱")
				return None
			
			# 验证邮箱格式
			try:
				validate_email(user_email)
			except ValidationError as e:
				logger.error(f"FormPage {self.id}: 邮箱格式无效 - {user_email}: {e}")
				return None
			
			# 准备邮件配置，与原有逻辑完全一致
			email_config = {
				'user_email': user_email,
				'admin_emails': self.get_email_recipients(),
				'subject': self.confirmation_email_subject or f"表单提交确认 - {self.title}",
				'page_title': self.title,
				'confirmation_text': self.confirmation_email_text,
				'site_name': self.get_site().site_name if self.get_site() else '网站',
			}
			
			logger.info(f"FormPage {self.id}: 准备提交异步邮件任务")
			logger.info(f"收件人: {user_email}")
			logger.info(f"管理员抄送: {email_config['admin_emails']}")
			
			# 检查邮件后端配置
			from django.conf import settings
			if not hasattr(settings, 'EMAIL_BACKEND'):
				logger.error("未配置EMAIL_BACKEND，无法发送邮件")
				return None
			
			if settings.EMAIL_BACKEND == 'django.core.mail.backends.console.EmailBackend':
				logger.info("使用控制台邮件后端，邮件内容将显示在控制台")
			
			# 提交异步任务
			task_result = send_form_confirmation_email.apply_async(
				args=[form_data, email_config],
				queue='email'
			)
			
			logger.info(f"FormPage {self.id}: 确认邮件任务已提交 - 任务ID: {task_result.id}")
			
			return task_result.id
		
		except ImportError as e:
			logger.error(f"FormPage {self.id}: 无法导入邮件任务模块: {str(e)}")
			return None
		except Exception as e:
			logger.error(f"FormPage {self.id}: 提交确认邮件任务失败: {str(e)}", exc_info=True)
			return None
	
	def process_form_submission(self, form):
		"""处理表单提交 - 使用异步邮件发送，完全保持原有逻辑"""
		submission = super().process_form_submission(form)
		
		if self.send_confirmation_email:
			# 准备表单数据
			form_data = {}
			user_email = None
			
			# 多种方式获取用户邮箱和表单数据，与原有逻辑保持一致
			if hasattr(form, 'cleaned_data') and form.cleaned_data:
				form_data = form.cleaned_data.copy()
				user_email = self.get_user_email_from_form_data(form_data)
				if user_email:
					logger.info(f"FormPage {self.id}: 从cleaned_data获取邮箱成功")
			
			if not user_email and submission:
				submission_data = submission.get_data()
				user_email = self.get_user_email_from_form_data(submission_data)
				if user_email:
					form_data.update(submission_data)
					logger.info(f"FormPage {self.id}: 从submission获取邮箱成功")
			
			if not user_email and hasattr(form, 'data'):
				user_email = form.data.get('email')
				if user_email:
					logger.info(f"FormPage {self.id}: 从form.data获取邮箱成功")
					if not form_data:
						form_data = dict(form.data)
			
			if user_email:
				form_data['email'] = user_email
				# 异步发送确认邮件（同时发送给用户和管理员，保持原有逻辑）
				self.send_confirmation_email_async(form_data, submission)
			else:
				logger.error(f"FormPage {self.id}: 无法获取用户邮箱地址，跳过邮件发送")
		
		return submission
	
	def get_context(self, request, *args, **kwargs):
		"""获取模板上下文"""
		context = super().get_context(request, *args, **kwargs)
		context.update({
			'form_title': self.title,
			'show_feature_image': bool(self.feature_image),
			'confirmation_enabled': self.send_confirmation_email,
			'async_email_enabled': True,
		})
		return context
	
	def serve(self, request, *args, **kwargs):
		"""处理页面请求"""
		if request.method == 'POST':
			# 验证邮箱字段
			user_email = request.POST.get('email', '').strip()
			if not user_email:
				messages.error(request, '请填写您的邮箱地址')
				form = self.get_form(request.POST, request.FILES, page=self, user=request.user)
				return self.render_form_page(request, form, *args, **kwargs)
			
			try:
				validate_email(user_email)
			except ValidationError:
				messages.error(request, '邮箱地址格式不正确，请重新输入')
				form = self.get_form(request.POST, request.FILES, page=self, user=request.user)
				return self.render_form_page(request, form, *args, **kwargs)
			
			# 处理表单
			form = self.get_form(request.POST, request.FILES, page=self, user=request.user)
			
			if form.is_valid():
				logger.info(f"FormPage {self.id}: 表单验证成功，用户邮箱: {user_email}")
				
				try:
					submission = self.process_form_submission(form)
					
					success_message = '您的留言已成功提交！'
					if self.send_confirmation_email:
						# 检查邮件配置状态
						from django.conf import settings
						if hasattr(settings, 'EMAIL_BACKEND'):
							if 'console' in settings.EMAIL_BACKEND.lower():
								success_message += f' 确认邮件内容已在服务器控制台显示。'
							else:
								success_message += f' 确认邮件正在发送到 {user_email}，请稍后查收。'
						else:
							success_message += ' 注意：邮件系统配置不完整，请联系管理员。'
					
					messages.success(request, success_message)
					return self.render_landing_page(request, form, *args, **kwargs)
				
				except Exception as e:
					logger.error(f"FormPage {self.id}: 处理表单提交时出错: {str(e)}", exc_info=True)
					messages.error(request, '表单提交时出现系统错误，请稍后重试或联系管理员')
					return self.render_form_page(request, form, *args, **kwargs)
			else:
				messages.error(request, '表单提交失败，请检查输入内容')
				return self.render_form_page(request, form, *args, **kwargs)
		else:
			form = self.get_form(page=self, user=request.user)
			return self.render_form_page(request, form, *args, **kwargs)
	
	
	def render_form_page(self, request, form, *args, **kwargs):
		"""渲染表单页面"""
		context = self.get_context(request, *args, **kwargs)
		context['form'] = form
		return TemplateResponse(
			request,
			self.get_template(request, *args, **kwargs),
			context
		)
	
	
	
	def render_landing_page(self, request, form, *args, **kwargs):
		"""渲染感谢页面"""
		context = self.get_context(request, *args, **kwargs)
		context['form'] = form
		
		return TemplateResponse(
			request,
			self.landing_page_template,
			context
		)
	
	def get_admin_display_title(self):
		"""在管理界面显示的标题"""
		return f"{self.title} - 联系表单（异步邮件）"
	
	def get_form_submission_email_context(self, form_submission):
		"""为管理员邮件准备上下文"""
		context = super().get_form_submission_email_context(form_submission)
		
		context.update({
			'submission_time': form_submission.submit_time,
			'page_title': self.title,
			'admin_recipients': self.get_email_recipients(),
			'user_email': self.get_user_email_from_form_data(form_submission.get_data()),
			'async_processing': True,
		})
		
		return context
	
	class Meta:
		verbose_name = "联系表单页面（异步邮件）"
		verbose_name_plural = "联系表单页面（异步邮件）"