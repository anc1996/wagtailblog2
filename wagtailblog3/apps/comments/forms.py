# wagtailblog3/apps/comments/forms.py
from django import forms
from .models import BlogPageComment


class CommentForm(forms.ModelForm):
    """评论表单，仅限登录用户使用"""

    # 只保留蜜罐字段，用于基本的机器人检测
    honeypot = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'style': 'display:none !important'}),
        label="如果你能看到这个字段，请留空"
    )

    class Meta:
        model = BlogPageComment
        fields = ['content']  # 只包含模型中实际存在的字段
        widgets = {
            'content': forms.Textarea(attrs={
                'rows': 4,
                'placeholder': '请输入您的评论',
                'class': 'form-control'
            }),
        }

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None) # 确保 user 参数被pop
        self.page = kwargs.pop('page', None)
        self.parent_id = kwargs.pop('parent_id', None)
        super().__init__(*args, **kwargs)

        # 评论表单现在仅供登录用户使用，因此不需要处理 author_name, author_email, author_website 或 recaptcha 字段
        # 这些字段在 CommentForm 的 Meta.fields 中不包含，所以它们不再是表单的一部分
        # 已登录用户，其信息会从 request.user 获取，无需表单填写

    def clean(self):
        """验证表单数据"""
        cleaned_data = super().clean()

        # 检查蜜罐字段
        if cleaned_data.get('honeypot'):
            raise forms.ValidationError('检测到自动提交，请重试')

        return cleaned_data