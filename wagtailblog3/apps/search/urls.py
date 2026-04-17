# search/urls.py
from django.urls import path
from . import views
from . import api

app_name = 'search'

urlpatterns = [
	# 标准搜索视图
	path('', views.search, name='search'),
	
	# 搜索建议 API
	path('suggestions/', views.search_suggestions, name='search_suggestions'),
	
	# REST API 接口
	path('api/', api.search_api, name='search_api'),
	path('api/suggestions/', api.search_suggestions_api, name='search_suggestions_api'),
]