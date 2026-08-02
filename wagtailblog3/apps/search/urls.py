# 搜索应用的 URL 路由
from django.urls import path
from . import views
from . import api

app_name = 'search'

urlpatterns = [
	# 标准搜索视图
	path('', views.search, name='search'),
	path('suggestions/', views.search_suggestions, name='search_suggestions'),
	
	
	# 搜索数据接口
	path('api/', api.search_api, name='search_api'),
	# 搜索建议接口
	path('api/suggestions/', api.search_suggestions_api, name='search_suggestions_api'),
]
