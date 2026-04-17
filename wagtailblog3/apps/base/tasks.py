# base/tasks.py
import logging
from typing import Dict, List, Any
from celery import shared_task
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_form_confirmation_email(self, form_data: Dict[str, Any], email_config: Dict[str, Any]):
	"""
	异步发送表单确认邮件任务
	Args:
		form_data: 表单提交的数据
		email_config: 邮件配置信息
	"""
	try:
		# 提取邮件配置
		user_email = email_config.get('user_email')
		admin_emails = email_config.get('admin_emails', [])
		subject = email_config.get('subject', '表单提交确认')
		page_title = email_config.get('page_title', '联系表单')
		confirmation_text = email_config.get('confirmation_text', '')
		site_name = email_config.get('site_name', '网站')
		
		if not user_email:
			logger.error("发送确认邮件失败：缺少用户邮箱地址")
			return {'status': 'error', 'message': '缺少用户邮箱地址'}
		
		# 准备收件人列表
		recipient_list = [user_email]
		if admin_emails:
			recipient_list.extend(admin_emails)
		
		# 去除重复邮箱
		recipient_list = list(set(recipient_list))
		
		logger.info(f"准备发送邮件到: {recipient_list}")
		
		# 准备邮件上下文
		context = {
			'form': form_data,
			'page': {'title': page_title},
			'site': {'site_name': site_name},
			'user_email': user_email,
			'admin_emails': admin_emails,
			'submission_time': timezone.now(),
			'confirmation_text': confirmation_text,
		}
		
		# 渲染HTML邮件内容
		html_message = render_to_string('emails/form_confirmation.html', context)
		
		# 准备纯文本内容
		plain_message = (confirmation_text or
		                 f"""感谢您通过 {page_title} 联系我们！我们已经收到您的留言，将在24小时内给予回复。提交的信息：							""")
		
		for field_name, field_value in form_data.items():
			if field_value:
				plain_message += f"{field_name}: {field_value}\n"
		
		plain_message += f"\n此邮件发送给：{', '.join(recipient_list)}"
		
		# 创建邮件对象
		from_email = settings.DEFAULT_FROM_EMAIL
		msg = EmailMultiAlternatives(
			subject=subject,
			body=plain_message,
			from_email=from_email,
			to=recipient_list
		)
		msg.attach_alternative(html_message, "text/html")
		
		# 发送邮件
		send_result = msg.send(fail_silently=False)
		
		if send_result:
			logger.info(f"确认邮件发送成功 - 收件人: {', '.join(recipient_list)}")
			return {
				'status': 'success',
				'message': '邮件发送成功',
				'recipients': recipient_list,
				'send_count': send_result
			}
		else:
			logger.error("邮件发送失败，send返回值为0")
			raise Exception("邮件发送失败")
	
	except Exception as exc:
		logger.error(f"发送确认邮件时发生异常: {str(exc)}", exc_info=True)
		
		# 重试机制
		if self.request.retries < self.max_retries:
			logger.info(f"邮件发送失败，准备重试 (第{self.request.retries + 1}次)")
			raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1))
		
		return {
			'status': 'error',
			'message': f'邮件发送失败: {str(exc)}',
			'retries': self.request.retries
		}

@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def send_admin_notification_email(self, form_data: Dict[str, Any], admin_config: Dict[str, Any]):
	"""
	异步发送管理员通知邮件任务
	Args:
		form_data: 表单提交的数据
		admin_config: 管理员邮件配置
	"""
	try:
		admin_emails = admin_config.get('admin_emails', [])
		subject = admin_config.get('subject', '新的表单提交')
		page_title = admin_config.get('page_title', '联系表单')
		user_email = form_data.get('email', '未提供邮箱')
		
		if not admin_emails:
			logger.warning("没有配置管理员邮箱，跳过管理员通知")
			return {'status': 'skipped', 'message': '没有配置管理员邮箱'}
		
		# 准备邮件内容
		plain_message = f"""
收到新的表单提交 - {page_title}

提交时间: {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}
用户邮箱: {user_email}

提交内容:
"""
		
		for field_name, field_value in form_data.items():
			if field_value:
				plain_message += f"{field_name}: {field_value}\n"
		
		plain_message += f"\n请及时处理用户提交的信息。"
		
		# 发送邮件给每个管理员
		from_email = settings.DEFAULT_FROM_EMAIL
		sent_count = 0
		
		for admin_email in admin_emails:
			try:
				msg = EmailMultiAlternatives(
					subject=f"[管理员通知] {subject}",
					body=plain_message,
					from_email=from_email,
					to=[admin_email]
				)
				
				if msg.send(fail_silently=False):
					sent_count += 1
					logger.info(f"管理员通知邮件发送成功: {admin_email}")
				else:
					logger.warning(f"管理员通知邮件发送失败: {admin_email}")
			
			except Exception as e:
				logger.error(f"发送管理员通知邮件失败 {admin_email}: {str(e)}")
		
		return {
			'status': 'success' if sent_count > 0 else 'partial_failure',
			'message': f'成功发送 {sent_count}/{len(admin_emails)} 封管理员通知邮件',
			'sent_count': sent_count,
			'total_admins': len(admin_emails)
		}
	
	except Exception as exc:
		logger.error(f"发送管理员通知邮件时发生异常: {str(exc)}", exc_info=True)
		
		# 重试机制
		if self.request.retries < self.max_retries:
			logger.info(f"管理员通知邮件发送失败，准备重试 (第{self.request.retries + 1}次)")
			raise self.retry(exc=exc, countdown=30 * (self.request.retries + 1))
		
		return {
			'status': 'error',
			'message': f'管理员通知邮件发送失败: {str(exc)}',
			'retries': self.request.retries
		}


@shared_task
def send_bulk_email(email_list: List[str], subject: str, message: str, html_message: str = None):
	"""
	异步批量发送邮件任务

	Args:
		email_list: 收件人邮箱列表
		subject: 邮件主题
		message: 纯文本邮件内容
		html_message: HTML邮件内容
	"""
	try:
		from_email = settings.DEFAULT_FROM_EMAIL
		sent_count = 0
		failed_emails = []
		
		for email in email_list:
			try:
				msg = EmailMultiAlternatives(
					subject=subject,
					body=message,
					from_email=from_email,
					to=[email]
				)
				
				if html_message:
					msg.attach_alternative(html_message, "text/html")
				
				if msg.send(fail_silently=False):
					sent_count += 1
					logger.info(f"批量邮件发送成功: {email}")
				else:
					failed_emails.append(email)
					logger.warning(f"批量邮件发送失败: {email}")
			
			except Exception as e:
				failed_emails.append(email)
				logger.error(f"发送邮件失败 {email}: {str(e)}")
		
		return {
			'status': 'completed',
			'sent_count': sent_count,
			'failed_count': len(failed_emails),
			'failed_emails': failed_emails,
			'total_emails': len(email_list)
		}
	
	except Exception as exc:
		logger.error(f"批量发送邮件时发生异常: {str(exc)}", exc_info=True)
		return {
			'status': 'error',
			'message': f'批量邮件发送失败: {str(exc)}'
		}


@shared_task
def cleanup_email_logs():
	"""
	清理邮件日志任务（定期执行）
	"""
	try:
		# 这里可以添加清理过期邮件日志的逻辑
		logger.info("执行邮件日志清理任务")
		return {'status': 'success', 'message': '邮件日志清理完成'}
	except Exception as exc:
		logger.error(f"清理邮件日志时发生异常: {str(exc)}")
		return {'status': 'error', 'message': f'邮件日志清理失败: {str(exc)}'}


# 任务状态查询帮助函数
def get_task_status(task_id):
	"""
	获取任务执行状态

	Args:
		task_id: 任务ID

	Returns:
		dict: 任务状态信息
	"""
	from celery.result import AsyncResult
	
	try:
		result = AsyncResult(task_id)
		return {
			'task_id': task_id,
			'status': result.status,
			'result': result.result,
			'successful': result.successful(),
			'failed': result.failed(),
			'date_done': result.date_done,
		}
	except Exception as e:
		return {
			'task_id': task_id,
			'status': 'ERROR',
			'error': str(e)
		}