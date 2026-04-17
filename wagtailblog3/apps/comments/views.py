# comments/views.py
import logging,time

from django.utils import timezone
from django.http import JsonResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST
from django.template.loader import render_to_string
from django.contrib.auth.decorators import login_required
from django.db.models import F
from django.db import transaction
from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.conf import settings
from django.core.cache import caches
from blog.models import BlogPage
from comments.forms import CommentForm
from comments.models import BlogPageComment, CommentReaction
from comments.templatetags.comment_tags import render_markdown

logger = logging.getLogger(__name__)
User = get_user_model()


@require_POST
@login_required(login_url='/admin/login/')  # 强制要求登录，请确保 '/accounts/login/' 是正确的登录URL
def post_comment(request, page_id):
	"""处理评论提交，已优化错误处理"""
	
	# 获取页面
	try:
		page = get_object_or_404(BlogPage, id=page_id)
		
		# 判断是AJAX请求还是常规表单提交
		is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
		
		# --- 评论频率限制 (新增) ---
		# 确保 settings.COMMENT_RATE_LIMIT 存在且 'ENABLED' 为 True
		if hasattr(settings, 'COMMENT_RATE_LIMIT') and settings.COMMENT_RATE_LIMIT['ENABLED']:
			rate_limit_cache = caches[settings.COMMENT_RATE_LIMIT['CACHE_ALIAS']]  # 获取指定的缓存实例
			
			user_identifier = request.user.username  # 现在只关心登录用户
			cache_key = f'comment_rate_limit_{user_identifier}'
			
			timestamps = rate_limit_cache.get(cache_key, [])  # 使用指定的缓存实例
			
			current_time = time.time()
			timestamps = [ts for ts in timestamps if
			              current_time - ts < settings.COMMENT_RATE_LIMIT['INTERVAL_SECONDS']]
			
			if len(timestamps) >= settings.COMMENT_RATE_LIMIT['MAX_COMMENTS']:
				if is_ajax:
					return JsonResponse({'status': 'error', 'message': '您评论太快了，请稍后再试。'},
					                    status=429)  # 429 Too Many Requests
				return HttpResponseRedirect(page.get_url())
			
			timestamps.append(current_time)
			# 缓存过期时间设置为频率限制的间隔时间，确保过期后记录自动清理
			rate_limit_cache.set(cache_key, timestamps, settings.COMMENT_RATE_LIMIT['INTERVAL_SECONDS'])  # 使用指定的缓存实例
		# --- 评论频率限制 结束 ---
		
		try:
			# 准备表单
			form = CommentForm(
				request.POST,
				user=request.user,  # 传递用户对象给表单
				page=page,
				parent_id=request.POST.get('parent_id')
			)
			
			if form.is_valid():
				# 检查蜜罐字段
				if form.cleaned_data.get('honeypot'):
					logger.warning("检测到蜜罐字段被填写")
					if is_ajax:
						return JsonResponse({'status': 'error', 'message': '检测到垃圾评论'}, status=400)
					return HttpResponseRedirect(page.get_url())
				
				# 创建评论但不保存
				comment = form.save(commit=False)
				
				# 设置关联
				comment.page = page
				
				# --- 用户信息设置 (精简为登录用户) ---
				comment.author_user = request.user  # 强制为当前登录用户
				comment.status = 'approved'  # 登录用户评论直接批准
				# 移除 guest_name 和 guest_email 的赋值
				# --- 用户信息设置 结束 ---
				
				# 设置父评论 (保持不变)
				parent_id = request.POST.get('parent_id')
				if parent_id:
					try:
						parent_comment = BlogPageComment.objects.get(id=parent_id)
						comment.parent = parent_comment
					except BlogPageComment.DoesNotExist:
						logger.warning(f"父评论不存在: id={parent_id}")
				
				# 设置被回复的用户 (保持不变)
				replied_to_user_id = request.POST.get('replied_to_user_id')
				if replied_to_user_id:
					try:
						replied_to_user = User.objects.get(id=replied_to_user_id)
						comment.replied_to_user = replied_to_user
					except User.DoesNotExist:
						logger.warning(f"被回复的用户不存在: id={replied_to_user_id}")
				
				# 记录IP和用户代理 (保持不变)
				x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
				if x_forwarded_for:
					comment.ip_address = x_forwarded_for.split(',')[0]
				else:
					comment.ip_address = request.META.get('REMOTE_ADDR')
				comment.user_agent = request.META.get('HTTP_USER_AGENT', '')
				
				# 保存评论
				comment.save()
				
				# --- 更新父评论的回复计数 ---
				# 仅当此评论是回复且父评论为批准状态时，才增加父评论的回复计数
				if comment.parent and comment.parent.status == 'approved':
					comment.parent.reply_count = F('reply_count') + 1
					comment.parent.save(update_fields=['reply_count'])
					comment.parent.refresh_from_db()  # 刷新父评论对象以获取更新后的值
				# --- 更新父评论回复计数 结束 ---
				
				# 返回结果 (修改)
				if is_ajax:
					html = render_to_string('comments/comment.html', {
						'comment': comment,
						'user': request.user,
					})
					return JsonResponse({
						'status': 'success',
						'html': html,
						'message': '评论已发布',  # 登录用户评论直接发布，无需审核
						'comment_status': comment.status  # 返回评论的实际状态 (现在总是 'approved')
					})
				return HttpResponseRedirect(page.get_url())
			else:
				logger.warning(f"表单验证失败: {form.errors}")
				# 表单无效
				if is_ajax:
					return JsonResponse({
						'status': 'error',
						'message': '表单验证失败',
						'errors': form.errors
					}, status=400)
				return HttpResponseRedirect(page.get_url())
		
		except Exception as e:
			logger.error(f"评论处理异常: {str(e)}", exc_info=True)
			if is_ajax:
				return JsonResponse({
					'status': 'error',
					'message': '服务器处理评论时出错，请稍后重试'
				}, status=500)
			return HttpResponseRedirect(page.get_url())
	
	except Exception as e:
		logger.error(f"评论提交总体异常: {str(e)}", exc_info=True)
		if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
			return JsonResponse({
				'status': 'error',
				'message': '发生未知错误，请刷新页面重试'
			}, status=500)
		return HttpResponseRedirect('/')


@require_POST
@login_required(login_url='/admin/login/')
def react_to_comment(request):
	"""处理评论点赞/踩"""
	comment_id = request.POST.get('comment_id')
	reaction_type = int(request.POST.get('reaction_type', 0))  # 1=赞, -1=踩
	
	if reaction_type not in [1, -1]:
		return JsonResponse({'status': 'error', 'message': '无效的操作'}, status=400)
	
	try:
		comment = BlogPageComment.objects.get(id=comment_id)
		
		# 检查评论状态
		if comment.status != 'approved':
			return JsonResponse({'status': 'error', 'message': '不能对已删除或待审核的评论进行操作'}, status=400)
		
		# 检查用户之前是否已经对此评论有反应
		reaction, created = CommentReaction.objects.get_or_create(
			comment=comment,
			user=request.user,
			defaults={'reaction_type': reaction_type}
		)
		
		if not created:
			# 如果点击相同类型，则取消反应
			if reaction.reaction_type == reaction_type:
				# 更新评论计数
				if reaction_type == 1:
					comment.like_count = F('like_count') - 1
				else:
					comment.dislike_count = F('dislike_count') - 1
				comment.save(update_fields=['like_count', 'dislike_count'])
				
				# 删除反应记录
				reaction.delete()
				
				# 刷新对象以获取最新计数
				comment.refresh_from_db()
				
				return JsonResponse({
					'status': 'success',
					'message': '已取消',
					'like_count': comment.like_count,
					'dislike_count': comment.dislike_count,
					'action': 'removed'
				})
			else:
				# 如果点击不同类型，则切换反应
				if reaction.reaction_type == 1:
					comment.like_count = F('like_count') - 1
					comment.dislike_count = F('dislike_count') + 1
				else:
					comment.like_count = F('like_count') + 1
					comment.dislike_count = F('dislike_count') - 1
				
				reaction.reaction_type = reaction_type
				reaction.save()
		else:
			# 新增反应
			if reaction_type == 1:
				comment.like_count = F('like_count') + 1
			else:
				comment.dislike_count = F('dislike_count') + 1
		
		comment.save(update_fields=['like_count', 'dislike_count'])
		comment.refresh_from_db()  # 获取更新后的数值
		
		return JsonResponse({
			'status': 'success',
			'message': '操作成功',
			'like_count': comment.like_count,
			'dislike_count': comment.dislike_count,
			'action': 'added',
			'reaction_type': reaction_type
		})
	
	except BlogPageComment.DoesNotExist:
		logger.error(f"评论反应处理异常: {str(BlogPageComment.DoesNotExist)}", exc_info=True)
		return JsonResponse({'status': 'error', 'message': '评论不存在'}, status=404)
	except Exception as e:
		logger.error(f"评论反应处理异常: {str(e)}", exc_info=True)
		return JsonResponse({'status': 'error', 'message': str(e)}, status=500)


@require_POST
@login_required(login_url='/admin/login/')
def delete_comment(request):
	"""删除评论（优化版本）"""
	comment_id = request.POST.get('comment_id')
	
	try:
		comment = BlogPageComment.objects.get(id=comment_id)
		
		# 检查权限（只有评论作者或管理员可以删除）
		if comment.author_user == request.user or request.user.is_staff:
			
			# 判断是否为一级评论（根评论）
			is_root_comment = comment.parent is None
			
			if is_root_comment:
				# 一级评论：批量软删除所有相关评论
				with transaction.atomic():  # 确保数据一致性
					# 获取所有子回复的ID和数量
					child_ids = list(BlogPageComment.objects.filter(
						parent=comment,
						status='approved'
					).values_list('id', flat=True))
					
					deleted_replies_count = len(child_ids)
					
					# 批量软删除所有子回复
					BlogPageComment.objects.filter(
						id__in=child_ids
					).update(
						status='deleted',
						content='此评论已被删除'
					)
					
					# 删除主评论
					comment.status = 'deleted'
					comment.content = "此评论已被删除"
					comment.save(update_fields=['status', 'content'])
					
					# 删除所有相关的点赞/踩记录（可选）
					CommentReaction.objects.filter(
						comment__in=[comment.id] + child_ids
					).delete()
				
				logger.info(f"用户 {request.user.username} 删除了评论 {comment_id} 及其 {deleted_replies_count} 条回复")
				
				return JsonResponse({
					'status': 'success',
					'message': f'评论已删除（包含 {deleted_replies_count} 条回复）',
					'deleted_replies': deleted_replies_count,
					'is_root_comment': True
				})
			else:
				# 二级评论：只删除自己
				with transaction.atomic():
					comment.status = 'deleted'
					comment.content = "此评论已被删除"
					comment.save(update_fields=['status', 'content'])
					
					# 更新父评论的回复计数
					if comment.parent:
						BlogPageComment.objects.filter(
							id=comment.parent.id
						).update(
							reply_count=F('reply_count') - 1
						)
						
						# 确保回复计数不为负数
						parent = BlogPageComment.objects.get(id=comment.parent.id)
						if parent.reply_count < 0:
							parent.reply_count = 0
							parent.save(update_fields=['reply_count'])
					
					# 删除相关的点赞/踩记录（可选）
					CommentReaction.objects.filter(comment=comment).delete()
				
				logger.info(f"用户 {request.user.username} 删除了回复 {comment_id}")
				
				return JsonResponse({
					'status': 'success',
					'message': '回复已删除',
					'deleted_replies': 0,
					'is_root_comment': False
				})
		
		else:
			return JsonResponse({
				'status': 'error',
				'message': '权限不足'
			}, status=403)
	
	except BlogPageComment.DoesNotExist:
		return JsonResponse({
			'status': 'error',
			'message': '评论不存在'
		}, status=404)
	except Exception as e:
		logger.error(f"删除评论时出错: {e}", exc_info=True)
		return JsonResponse({
			'status': 'error',
			'message': '删除失败，请重试'
		}, status=500)


@require_POST
@login_required(login_url='/admin/login/')
def edit_comment(request):
	"""编辑评论"""
	comment_id = request.POST.get('comment_id')
	new_content = request.POST.get('content')
	
	if not new_content:
		return JsonResponse({'status': 'error', 'message': '评论内容不能为空'}, status=400)
	
	try:
		comment = BlogPageComment.objects.get(id=comment_id)
		
		# 检查权限和时间限制
		if comment.author_user != request.user and not request.user.is_staff:
			return JsonResponse({'status': 'error', 'message': '权限不足'}, status=403)
		
		# 普通用户需要遵守编辑时间限制，管理员不受限制
		if not request.user.is_staff and not comment.is_editable:
			return JsonResponse({'status': 'error', 'message': '编辑时间已过'}, status=400)
		
		
		# 更新评论内容
		
		comment.content = new_content
		comment.updated_at = timezone.now()
		comment.save(update_fields=['content', 'updated_at'])
		
		return JsonResponse({
			'status': 'success',
			'message': '评论已更新',
			'content': render_markdown(comment.content)  # 返回渲染后的HTML
		})
	
	except BlogPageComment.DoesNotExist:
		return JsonResponse({'status': 'error', 'message': '评论不存在'}, status=404)


def load_comments(request, page_id):
	"""加载评论（支持分页和排序）- 修复认证状态返回"""
	page = get_object_or_404(BlogPage, id=page_id)
	
	# 分页参数
	page_num = int(request.GET.get('page', 1))
	per_page = int(request.GET.get('per_page', 20))
	
	# 排序方式（热门/最新）
	sort_by = request.GET.get('sort', 'hot')
	if sort_by == 'hot':
		order_by = ['-like_count', '-created_at']
	else:  # 最新
		order_by = ['-created_at']
	
	# 获取一级评论（不包括回复）
	root_comments = BlogPageComment.objects.filter(
		page=page,
		parent__isnull=True,
		status='approved'
	).select_related('author_user').order_by(*order_by)
	
	# 分页
	paginator = Paginator(root_comments, per_page)
	comments_page = paginator.get_page(page_num)
	
	# 获取用户反应状态
	user_reactions = {}
	if request.user.is_authenticated:
		reactions = CommentReaction.objects.filter(
			comment__in=comments_page,
			user=request.user
		)
		user_reactions = {str(r.comment_id): r.reaction_type for r in reactions}
	
	# 渲染评论HTML
	html = render_to_string('comments/comments_list.html', {
		'comments': comments_page,
		'page_obj': comments_page,
		'user': request.user,
		'user_reactions': user_reactions,
		'sort_by': sort_by
	})
	
	# 明确返回用户认证状态
	return JsonResponse({
		'status': 'success',
		'html': html,
		'has_next': comments_page.has_next(),
		'total_comments': paginator.count,
		'is_authenticated': request.user.is_authenticated,
		'current_page': page_num,
		'total_pages': paginator.num_pages,
		'sort_by': sort_by
	})


def load_replies(request, comment_id):
	"""加载评论的所有回复"""
	try:
		parent_comment = BlogPageComment.objects.get(id=comment_id, status='approved')
		
		# 获取所有回复
		replies = BlogPageComment.objects.filter(
			parent=parent_comment,
			status='approved'
		).select_related('author_user', 'replied_to_user').order_by('created_at')
		
		# 获取用户反应状态
		user_reactions = {}
		if request.user.is_authenticated:
			reactions = CommentReaction.objects.filter(
				comment__in=replies,
				user=request.user
			)
			user_reactions = {str(r.comment_id): r.reaction_type for r in reactions}
		
		# 渲染回复HTML
		html = render_to_string('comments/replies_list.html', {
			'replies': replies,
			'user': request.user,
			'user_reactions': user_reactions
		})
		
		# 返回实际的回复数量
		actual_reply_count = replies.count()
		
		return JsonResponse({
			'status': 'success',
			'html': html,
			'reply_count': actual_reply_count
		})
	
	except BlogPageComment.DoesNotExist:
		return JsonResponse({'status': 'error', 'message': '评论不存在或已被删除'}, status=404)


# 后台管理视图
@login_required(login_url='/admin/login/')
def admin_approve_comment(request, comment_id):
	"""管理员审核通过评论"""
	if not request.user.is_staff:
		return redirect('/admin/')
	
	try:
		comment = BlogPageComment.objects.get(id=comment_id)
		comment.status = 'approved'
		comment.save(update_fields=['status'])
		
		# 如果是回复，需要更新父评论回复计数
		if comment.parent and comment.parent.status == 'approved':
			comment.parent.reply_count = F('reply_count') + 1
			comment.parent.save(update_fields=['reply_count'])
		
		return JsonResponse({
			'status': 'success',
			'message': f'评论 #{comment_id} 已审核通过'
		})
	except BlogPageComment.DoesNotExist:
		return JsonResponse({
			'status': 'error',
			'message': f'评论 #{comment_id} 不存在'
		}, status=404)