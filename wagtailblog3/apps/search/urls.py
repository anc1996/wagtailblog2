# search/urls.py
from django.urls import path
from . import views
from . import api

app_name = 'search'

urlpatterns = [
	# 标准搜索视图
	path('', views.search, name='search'),
	path('suggestions/', views.search_suggestions, name='search_suggestions'),
	
	
	# REST API 接口
	# 搜索内容api
	path('api/', api.search_api, name='search_api'),
	# 搜索热词频率
	path('api/suggestions/', api.search_suggestions_api, name='search_suggestions_api'),
]