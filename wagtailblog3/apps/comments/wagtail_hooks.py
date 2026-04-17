# comments/wagtail_hooks.py
import logging
from django.urls import path, reverse
from django.utils.translation import gettext_lazy as _
from django.utils.html import format_html
from django.contrib import messages
from django.shortcuts import redirect

from wagtail import hooks
from wagtail_modeladmin.options import (
	ModelAdmin, ModelAdminGroup, modeladmin_register
)
from wagtail_modeladmin.helpers import ButtonHelper, PermissionHelper

from .models import BlogPageComment, CommentReaction

logger = logging.getLogger(__name__)


# 自定义按钮助手
class CommentButtonHelper(ButtonHelper):
	def approve_button(self, pk, classnames_add=None, classnames_exclude=None):
		"""审核通过按钮"""
		if classnames_add is None:
			classnames_add = []
		if classnames_exclude is None:
			classnames_exclude = []
		
		classnames = self.edit_button_classnames + classnames_add
		cn = self.finalise_classname(classnames, classnames_exclude)
		
		return {
			'url': reverse('admin_approve_comment', args=(pk,)),
			'label': _('审核通过'),
			'classname': cn,
			'title': _('将此评论标记为已审核'),
		}
	
	def soft_delete_button(self, pk, classnames_add=None, classnames_exclude=None):
		"""软删除按钮"""
		if classnames_add is None:
			classnames_add = []
		if classnames_exclude is None:
			classnames_exclude = []
		
		classnames = self.delete_button_classnames + classnames_add
		cn = self.finalise_classname(classnames, classnames_exclude)
		
		return {
			'url': reverse('admin_soft_delete_comment', args=(pk,)),
			'label': _('软删除'),
			'classname': cn,
			'title': _('标记此评论为已删除（不从数据库删除）'),
		}
	
	def real_delete_button(self, pk, classnames_add=None, classnames_exclude=None):
		"""真删除按钮"""
		if classnames_add is None:
			classnames_add = ['serious']
		else:
			classnames_add.append('serious')
		if classnames_exclude is None:
			classnames_exclude = []
		
		classnames = self.delete_button_classnames + classnames_add
		cn = self.finalise_classname(classnames, classnames_exclude)
		
		return {
			'url': reverse('admin_real_delete_comment', args=(pk,)),
			'label': _('彻底删除'),
			'classname': cn,
			'title': _('从数据库彻底删除此评论及关联数据'),
		}
	
	def get_buttons_for_obj(self, obj, exclude=None, classnames_add=None, classnames_exclude=None):
		"""获取对象的操作按钮"""
		if exclude is None:
			exclude = []
		if classnames_add is None:
			classnames_add = []
		if classnames_exclude is None:
			classnames_exclude = []
		
		btns = super().get_buttons_for_obj(obj, exclude, classnames_add, classnames_exclude)
		
		# 添加额外按钮
		pk = getattr(obj, self.opts.pk.attname)
		
		# 添加审核按钮（如果不是已审核状态）
		if obj.status != 'approved' and 'approve' not in exclude:
			btns.append(self.approve_button(pk, classnames_add, classnames_exclude))
		
		# 添加软删除按钮（如果不是已删除状态）
		if obj.status != 'deleted' and 'soft_delete' not in exclude:
			btns.append(self.soft_delete_button(pk, classnames_add, classnames_exclude))
		
		# 添加真删除按钮
		if 'real_delete' not in exclude:
			btns.append(self.real_delete_button(pk, classnames_add, classnames_exclude))
		
		return btns


# 自定义权限助手
class CommentPermissionHelper(PermissionHelper):
	def user_can_approve(self, user, obj=None):
		"""用户是否可以审核评论"""
		return self.user_can_edit_obj(user, obj)
	
	def user_can_soft_delete(self, user, obj=None):
		"""用户是否可以软删除评论"""
		return self.user_can_delete_obj(user, obj)
	
	def user_can_real_delete(self, user, obj=None):
		"""用户是否可以真删除评论"""
		# 只有管理员可以真删除
		return user.is_superuser


# 评论管理类
class CommentAdmin(ModelAdmin):
	model = BlogPageComment
	menu_label = _("评论管理")
	menu_icon = "comment"
	menu_order = 200
	add_to_settings_menu = False
	exclude_from_explorer = False
	list_display = ('content_preview', 'author_name', 'page_title', 'status_label', 'created_at', 'like_count',
	                'dislike_count')
	list_filter = ('status', 'created_at')
	search_fields = ('content', 'author_user__username', 'page__title')
	ordering = ('-created_at',)
	button_helper_class = CommentButtonHelper
	permission_helper_class = CommentPermissionHelper
	
	# 批量操作
	actions = ['approve_comments', 'soft_delete_comments', 'real_delete_comments']
	
	def approve_comments(self, request, queryset):
		"""批量通过评论"""
		queryset.update(status='approved')
		messages.success(request, _("已批量审核通过 {} 条评论").format(queryset.count()))
	
	approve_comments.short_description = _("批量审核通过")
	
	def soft_delete_comments(self, request, queryset):
		"""批量软删除评论"""
		queryset.update(status='deleted', content='此评论已被删除')
		messages.success(request, _("已批量软删除 {} 条评论").format(queryset.count()))
	
	soft_delete_comments.short_description = _("批量软删除")
	
	def real_delete_comments(self, request, queryset):
		"""批量真删除评论"""
		count = 0
		for comment in queryset:
			try:
				comment.real_delete()
				count += 1
			except Exception as e:
				logger.error(f"批量删除评论时出错: ID={comment.id}, 错误={e}")
		
		messages.success(request, _("已批量永久删除 {} 条评论").format(count))
	
	real_delete_comments.short_description = _("批量永久删除")
	
	def content_preview(self, obj):
		"""内容预览，限制长度"""
		return obj.content[:50] + "..." if len(obj.content) > 50 else obj.content
	
	content_preview.short_description = _("评论内容")
	
	def author_name(self, obj):
		"""作者名称"""
		return obj.author_user.username if obj.author_user else "匿名"
	
	author_name.short_description = _("作者")
	
	def page_title(self, obj):
		"""页面标题"""
		if not obj.page:
			return "未知页面"
		# 创建链接到页面编辑
		url = reverse('wagtailadmin_pages:edit', args=(obj.page.id,))
		return format_html('<a href="{}">{}</a>', url, obj.page.title)
	
	page_title.short_description = _("所属页面")
	page_title.admin_order_field = 'page__title'
	
	def status_label(self, obj):
		"""格式化状态显示"""
		status_map = {'approved': '已批准', 'deleted': '已删除'}
		status = status_map.get(obj.status, obj.status)
		if obj.status == 'approved':
			return format_html('<span style="color: green;">{}</span>', status)
		elif obj.status == 'deleted':
			return format_html('<span style="color: red;">{}</span>', status)
		return status
	
	status_label.short_description = _("状态")
	status_label.admin_order_field = 'status'


# 评论反应管理类
class ReactionAdmin(ModelAdmin):
	model = CommentReaction
	menu_label = _("评论反应")
	menu_icon = "thumbs-up"
	menu_order = 300
	add_to_settings_menu = False
	exclude_from_explorer = False
	list_display = ('id', 'comment_preview', 'user', 'reaction_type_display', 'created_at')
	list_filter = ('reaction_type', 'created_at')
	search_fields = ('comment__content', 'user__username')
	
	def comment_preview(self, obj):
		"""评论内容预览"""
		return obj.comment.content[:50] + "..." if len(obj.comment.content) > 50 else obj.comment.content
	
	comment_preview.short_description = _("评论内容")
	
	def reaction_type_display(self, obj):
		"""反应类型显示"""
		if obj.reaction_type == 1:
			return format_html('<span style="color: green;">👍 赞</span>')
		elif obj.reaction_type == -1:
			return format_html('<span style="color: red;">👎 踩</span>')
		return obj.get_reaction_type_display()
	
	reaction_type_display.short_description = _("反应类型")
	reaction_type_display.admin_order_field = 'reaction_type'


# 评论统计仪表板
class CommentDashboardAdmin(ModelAdmin):
	model = BlogPageComment
	menu_label = _("评论统计")
	menu_icon = "analytics"
	menu_order = 400
	add_to_settings_menu = False
	exclude_from_explorer = False
	
	def get_admin_urls_for_registration(self):
		
		urls = super().get_admin_urls_for_registration()
		urls += (
			path(
				self.url_helper.get_action_url_pattern('dashboard'),
				self.dashboard_view,
				name=self.url_helper.get_action_url_name('dashboard')
			),
		)
		return urls
	
	def dashboard_view(self, request):
		"""仪表板视图"""
		from django.shortcuts import render
		from django.utils import timezone
		from datetime import timedelta
		from django.db.models import Count
		from blog.models import BlogPage
		
		today = timezone.now().date()
		last_week = today - timedelta(days=7)
		last_month = today - timedelta(days=30)
		
		# 评论统计数据
		context = {
			'total_comments': BlogPageComment.objects.count(),
			'approved_comments': BlogPageComment.objects.filter(status='approved').count(),
			'deleted_comments': BlogPageComment.objects.filter(status='deleted').count(),
			'today_comments': BlogPageComment.objects.filter(created_at__date=today).count(),
			'week_comments': BlogPageComment.objects.filter(created_at__date__gte=last_week).count(),
			'month_comments': BlogPageComment.objects.filter(created_at__date__gte=last_month).count(),
			'popular_comments': BlogPageComment.objects.filter(status='approved').order_by('-like_count')[:5],
			'most_commented_pages': BlogPage.objects.annotate(
				comment_count=Count('comments')
			).order_by('-comment_count')[:5],
			'page_header': _("评论统计"),
			'header_icon': "analytics",
		}
		
		return render(request, 'comments/admin/dashboard.html', context)


# 评论管理组
class CommentManagementGroup(ModelAdminGroup):
	menu_label = _("评论系统")
	menu_icon = "comment"
	menu_order = 300
	items = (CommentAdmin, ReactionAdmin, CommentDashboardAdmin)


# 注册管理组到Wagtail管理界面
modeladmin_register(CommentManagementGroup)


# 注册自定义操作URLs
@hooks.register('register_admin_urls')
def register_comment_actions_urls():
	
	def approve_comment(request, pk):
		"""审核通过评论"""
		try:
			comment = BlogPageComment.objects.get(pk=pk)
			comment.status = 'approved'
			comment.save(update_fields=['status'])
			messages.success(request, _("评论 #{} 已审核通过").format(pk))
		except BlogPageComment.DoesNotExist:
			messages.error(request, _("评论 #{} 不存在").format(pk))
		except Exception as e:
			logger.error(f"审核评论失败: ID={pk}, 错误={e}")
			messages.error(request, _("审核评论 #{} 时发生错误").format(pk))
		
		# 使用绝对URL而不是命名空间
		return redirect('/admin/comments/blogpagecomment/')
	
	def soft_delete_comment(request, pk):
		"""软删除评论"""
		try:
			comment = BlogPageComment.objects.get(pk=pk)
			comment.status = 'deleted'
			comment.content = "此评论已被删除"
			comment.save(update_fields=['status', 'content'])
			messages.success(request, _("评论 #{} 已标记为删除").format(pk))
		except BlogPageComment.DoesNotExist:
			messages.error(request, _("评论 #{} 不存在").format(pk))
		except Exception as e:
			logger.error(f"软删除评论失败: ID={pk}, 错误={e}")
			messages.error(request, _("软删除评论 #{} 时发生错误").format(pk))
		# 使用绝对URL而不是命名空间
		return redirect('/admin/comments/blogpagecomment/')
	
	def real_delete_comment(request, pk):
		"""真删除评论"""
		try:
			comment = BlogPageComment.objects.get(pk=pk)
			
			# 使用real_delete方法进行彻底删除
			comment.real_delete()
			
			messages.success(request, _("评论 #{} 已永久删除").format(pk))
		except BlogPageComment.DoesNotExist:
			messages.error(request, _("评论 #{} 不存在").format(pk))
		except Exception as e:
			logger.error(f"真删除评论失败: ID={pk}, 错误={e}")
			messages.error(request, _("永久删除评论 #{} 时发生错误").format(pk))
		
		# 使用绝对URL而不是命名空间
		return redirect('/admin/comments/blogpagecomment/')
	
	return [
		path('comments/approve/<int:pk>/', approve_comment, name='admin_approve_comment'),
		path('comments/soft-delete/<int:pk>/', soft_delete_comment, name='admin_soft_delete_comment'),
		path('comments/real-delete/<int:pk>/', real_delete_comment, name='admin_real_delete_comment'),
	]