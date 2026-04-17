# comments/models.py
from django.utils import timezone
from django.db import models
from django.conf import settings
from modelcluster.fields import ParentalKey
from wagtail.admin.panels import FieldPanel
from wagtail.models import Orderable
import logging

logger = logging.getLogger(__name__)

class Comment(models.Model):
	"""评论基础模型"""
	
	# 内容与作者信息
	content = models.TextField('评论内容')
	author_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='comments')
	
	# 评论元数据
	created_at = models.DateTimeField('创建时间', auto_now_add=True)
	updated_at = models.DateTimeField('更新时间', auto_now=True)
	
	# 二级回复结构
	parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.CASCADE, related_name='replies')
	replied_to_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
	                                    related_name='+')
	
	# 评论状态
	STATUS_CHOICES = [('approved', '已批准'), ('deleted', '已删除'), ('pending', '待审核')]
	status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='approved')
	
	# 统计字段
	like_count = models.IntegerField('点赞数', default=0)
	dislike_count = models.IntegerField('点踩数', default=0)
	reply_count = models.IntegerField('回复数', default=0)
	
	# 编辑时间限制（用于判断是否可以编辑）
	can_edit_until = models.DateTimeField('可编辑截止时间', null=True, blank=True)
	
	# IP和用户代理
	ip_address = models.GenericIPAddressField('IP地址', null=True, blank=True)
	user_agent = models.TextField('用户代理', blank=True)
	
	class Meta:
		abstract = True
		ordering = ['-like_count', '-created_at']  # 默认按点赞数和时间排序
	
	def save(self, *args, **kwargs):
		# 设置可编辑截止时间（发布后5分钟）
		if not self.id:
			self.can_edit_until = timezone.now() + timezone.timedelta(minutes=5)
		super().save(*args, **kwargs)
	
	@property
	def is_editable(self):
		"""判断评论是否可编辑"""
		return self.can_edit_until and timezone.now() <= self.can_edit_until
	
	@property
	def is_root_comment(self):
		"""判断是否为根评论（非回复）"""
		return self.parent is None
	
	@property
	def has_replies(self):
		"""判断是否有回复"""
		return self.reply_count > 0


class BlogPageComment(Orderable, Comment):
	"""博客页面评论实现"""
	
	page = ParentalKey('blog.BlogPage', on_delete=models.CASCADE, related_name='comments')
	
	panels = [
		FieldPanel('status'),
		FieldPanel('content'),
		FieldPanel('like_count'),
		FieldPanel('dislike_count'),
		FieldPanel('reply_count'),
		FieldPanel('author_user'),
	]
	
	def __str__(self):
		return f"评论 #{self.id} - {self.content[:30]}..." if len(self.content) > 30 else self.content
	
	def real_delete(self):
		"""真实删除评论及其关联数据"""
		try:
			# 如果有父评论，更新父评论的回复计数
			if self.parent:
				# 使用F表达式防止并发问题
				self.parent.reply_count = models.F('reply_count') - 1
				self.parent.save(update_fields=['reply_count'])
				
				# 刷新父评论对象以获取更新后的值
				self.parent.refresh_from_db()
				
				# 如果父评论回复数变为0，可能需要更新UI状态
				if self.parent.reply_count <= 0:
					# 确保不会出现负数
					self.parent.reply_count = 0
					self.parent.save(update_fields=['reply_count'])
			
			# 删除与此评论关联的所有反应
			CommentReaction.objects.filter(comment=self).delete()
			
			# 查找并递归删除所有子评论
			children = type(self).objects.filter(parent=self)
			for child in children:
				child.real_delete()  # 递归删除每个子评论
			
			# 删除评论本身
			super().delete()
			
			return True
		except Exception as e:
			logger.error(f"删除评论时出错: {e}", exc_info=True)
			return False
	
	@property
	def formatted_created_time(self):
		"""格式化创建时间"""
		return self.created_at.strftime("%Y-%m-%d %H:%M")
	
	@property
	def is_edited(self):
		"""判断评论是否被编辑过"""
		# 使用时间差大于1分钟来判断是否编辑过（避免保存时自动更新的影响）
		time_diff = self.updated_at - self.created_at
		return time_diff.total_seconds() > 60
	
	@property
	def actual_reply_count(self):
		"""获取实际回复数量"""
		return type(self).objects.filter(parent=self, status='approved').count()


class CommentReaction(models.Model):
	"""评论点赞/踩记录"""
	
	comment = models.ForeignKey('comments.BlogPageComment', on_delete=models.CASCADE, related_name='reactions')
	user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='comment_reactions')
	created_at = models.DateTimeField(auto_now_add=True)
	
	# 反应类型（1=赞, -1=踩）
	REACTION_CHOICES = [(1, '赞'), (-1, '踩')]
	reaction_type = models.SmallIntegerField(choices=REACTION_CHOICES)
	
	class Meta:
		unique_together = ('comment', 'user')  # 每个用户对每条评论只能有一个反应
		verbose_name = '评论反应'
		verbose_name_plural = '评论反应'
	
	def __str__(self):
		reaction_name = '点赞' if self.reaction_type == 1 else '点踩'
		return f"{self.user.username} {reaction_name} 评论 #{self.comment.id}"