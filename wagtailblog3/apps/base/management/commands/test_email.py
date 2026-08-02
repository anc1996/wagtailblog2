# 管理命令包初始化文件
# 此文件为空，但必须存在

# 管理命令子包初始化文件
# 此文件为空，但必须存在

"""发送纯文本、HTML 或多格式测试邮件的管理命令。"""
from django.core.management.base import BaseCommand
from django.core.mail import send_mail, EmailMultiAlternatives
from django.conf import settings
from django.template.loader import render_to_string
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
	"""根据命令参数构造并发送测试邮件。"""
	help = '测试邮件发送功能'
	
	def add_arguments(self, parser):
		parser.add_argument(
			'--to',
			type=str,
			help='收件人邮箱地址，多个邮箱用逗号分隔',
			required=True
		)
		parser.add_argument(
			'--test-type',
			type=str,
			choices=['simple', 'html', 'multi'],
			default='multi',
			help='测试类型：simple(纯文本), html(HTML), multi(多格式)'
		)
	
	def handle(self, *args, **options):
		# 收件人由逗号分隔并逐项去除空白，便于一次测试多个地址。
		recipients = [email.strip() for email in options['to'].split(',')]
		test_type = options['test_type']
		
		self.stdout.write(f"开始测试邮件发送...")
		self.stdout.write(f"发件人: {settings.DEFAULT_FROM_EMAIL}")
		self.stdout.write(f"收件人: {', '.join(recipients)}")
		self.stdout.write(f"测试类型: {test_type}")
		
		try:
			if test_type == 'simple':
				self.test_simple_email(recipients)
			elif test_type == 'html':
				self.test_html_email(recipients)
			else:
				self.test_multi_email(recipients)
		
		except Exception as e:
			self.stdout.write(
				self.style.ERROR(f'邮件发送失败: {str(e)}')
			)
			logger.error(f"邮件测试失败: {e}", exc_info=True)
	
	def test_simple_email(self, recipients):
		"""发送仅包含纯文本正文的测试邮件。"""
		subject = "Django邮件系统测试 - 纯文本"
		message = """
这是一封测试邮件。

如果您收到此邮件，说明Django邮件系统配置正确。

测试信息：
- 发送时间：现在
- 邮件类型：纯文本
- 收件人数量：{}

请不要回复此邮件。
        """.format(len(recipients))
		
		result = send_mail(
			subject=subject,
			message=message,
			from_email=settings.DEFAULT_FROM_EMAIL,
			recipient_list=recipients,
			fail_silently=False
		)
		
		if result:
			self.stdout.write(
				self.style.SUCCESS(f'纯文本邮件发送成功，返回值: {result}')
			)
		else:
			self.stdout.write(
				self.style.ERROR('纯文本邮件发送失败，返回值为0')
			)
	
	def test_html_email(self, recipients):
		"""发送带纯文本备选内容的 HTML 测试邮件。"""
		html_content = """
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <title>邮件测试</title>
        </head>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <h2 style="color: #2c3e50;">Django邮件系统测试</h2>
                <p>这是一封<strong>HTML格式</strong>的测试邮件。</p>
                <div style="background-color: #f0f0f0; padding: 15px; border-radius: 5px;">
                    <h3>测试信息：</h3>
                    <ul>
                        <li>发送时间：现在</li>
                        <li>邮件类型：HTML</li>
                        <li>收件人数量：{}</li>
                    </ul>
                </div>
                <p style="color: #7f8c8d; font-size: 12px;">请不要回复此邮件。</p>
            </div>
        </body>
        </html>
        """.format(len(recipients))
		
		result = send_mail(
			subject="Django邮件系统测试 - HTML",
			message="这是HTML邮件的纯文本备选内容",
			html_message=html_content,
			from_email=settings.DEFAULT_FROM_EMAIL,
			recipient_list=recipients,
			fail_silently=False
		)
		
		if result:
			self.stdout.write(
				self.style.SUCCESS(f'HTML邮件发送成功，返回值: {result}')
			)
		else:
			self.stdout.write(
				self.style.ERROR('HTML邮件发送失败，返回值为0')
			)
	
	def test_multi_email(self, recipients):
		"""发送同时包含纯文本和 HTML 正文的多格式测试邮件。"""
		subject = "Django邮件系统测试 - 多格式"
		
		# 纯文本内容
		text_content = """
这是一封测试邮件。

如果您收到此邮件，说明Django邮件系统配置正确。

测试信息：
- 发送时间：现在
- 邮件类型：多格式
- 收件人数量：{}

请不要回复此邮件。
        """.format(len(recipients))
		
		# 构造 HTML 内容
		html_content = """
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <title>邮件测试</title>
        </head>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <h2 style="color: #2c3e50;">Django邮件系统测试</h2>
                <p>这是一封<strong>多格式</strong>的测试邮件。</p>
                <div style="background-color: #e8f5e8; padding: 15px; border-radius: 5px; border-left: 4px solid #27ae60;">
                    <h3 style="margin-top: 0;">✅ 测试信息：</h3>
                    <ul>
                        <li>发送时间：现在</li>
                        <li>邮件类型：多格式（HTML+纯文本）</li>
                        <li>收件人数量：{}</li>
                        <li>收件人列表：{}</li>
                    </ul>
                </div>
                <div style="background-color: #fff3cd; padding: 15px; border-radius: 5px; border-left: 4px solid #ffc107; margin-top: 20px;">
                    <p><strong>💡 说明：</strong></p>
                    <p>如果您能看到这个带颜色的区域，说明您的邮件客户端支持HTML格式。</p>
                    <p>如果您只能看到纯文本内容，说明您的邮件客户端使用了纯文本模式。</p>
                </div>
                <p style="color: #7f8c8d; font-size: 12px; margin-top: 30px;">请不要回复此邮件。</p>
            </div>
        </body>
        </html>
        """.format(len(recipients), ', '.join(recipients))
		
		# 使用 EmailMultiAlternatives 保留纯文本兼容性，并附加 HTML 版本。
		msg = EmailMultiAlternatives(
			subject=subject,
			body=text_content,
			from_email=settings.DEFAULT_FROM_EMAIL,
			to=recipients
		)
		msg.attach_alternative(html_content, "text/html")
		
		try:
			result = msg.send(fail_silently=False)
			
			if result:
				self.stdout.write(
					self.style.SUCCESS(f'多格式邮件发送成功，返回值: {result}')
				)
				self.stdout.write(f"详细信息：")
				self.stdout.write(f"  - 主题: {subject}")
				self.stdout.write(f"  - 发件人: {settings.DEFAULT_FROM_EMAIL}")
				self.stdout.write(f"  - 收件人: {', '.join(recipients)}")
				self.stdout.write(f"  - 内容类型: 纯文本 + HTML")
			else:
				self.stdout.write(
					self.style.ERROR('多格式邮件发送失败，返回值为0')
				)
		
		except Exception as e:
			self.stdout.write(
				self.style.ERROR(f'多格式邮件发送异常: {str(e)}')
			)
			raise
