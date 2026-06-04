# blog/forms.py


from django import forms
from .models import PageViewCount


class PageViewCountForm(forms.ModelForm):
	"""页面访问统计编辑表单"""
	
	class Meta:
		
		model = PageViewCount
		fields = ['count', 'unique_count']
		widgets = {
			'count': forms.NumberInput(attrs={'min': 0}),
			'unique_count': forms.NumberInput(attrs={'min': 0}),
		}
	
	def clean(self):
		cleaned_data = super().clean()
		count = cleaned_data.get('count', 0)
		unique_count = cleaned_data.get('unique_count', 0)
		
		# 逻辑验证：唯一访问量不能大于总访问量
		if unique_count > count:
			raise forms.ValidationError("唯一访问量不能大于总访问量")
		
		return cleaned_data


