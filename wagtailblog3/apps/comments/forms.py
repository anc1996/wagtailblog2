# 评论表单定义。
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
        # 从参数字典移出扩展参数，避免它们被 ModelForm 基类误认为字段选项。
        self.user = kwargs.pop('user', None)
        self.page = kwargs.pop('page', None)
        self.parent_id = kwargs.pop('parent_id', None)
        super().__init__(*args, **kwargs)

        # 评论表单现在仅供登录用户使用，因此不需要处理作者、邮箱、网站或验证码字段。
        # 这些字段在 CommentForm 的 Meta.fields 中不包含，所以它们不再是表单的一部分
        # 已登录用户，其信息会从 request.user 获取，无需表单填写

    def clean_content(self):
        """在保存前清理评论文本，并限制不可信 Markdown 的长度。"""
        content = (self.cleaned_data.get('content') or '').strip()
        if not content:
            raise forms.ValidationError('评论内容不能为空')
        # 长度限制既避免异常大的请求占用资源，也与编辑接口保持一致。
        if len(content) > 10000:
            raise forms.ValidationError('评论内容不能超过 10000 个字符')
        return content

    def clean(self):
        """验证表单数据，并拒绝填写了隐藏蜜罐字段的自动提交。"""
        cleaned_data = super().clean()

        # 检查蜜罐字段
        if cleaned_data.get('honeypot'):
            raise forms.ValidationError('检测到自动提交，请重试')

        return cleaned_data
