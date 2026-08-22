# 搜索应用的 URL 路由
"""搜索前台页面和 API 路由。"""

from django.urls import path  # 使用 path 声明不带正则表达式的 URL 路由

from . import api  # 搜索 REST API 视图集合
from . import views  # 搜索页面、片段和传统 AJAX 视图集合

app_name = 'search'  # 为反向解析提供 search 命名空间，避免与其他应用的路由重名

urlpatterns = [
	# GET /search/：渲染完整搜索页面；也负责接收传统 AJAX 搜索请求并返回 JSON。
	path('', views.search, name='search'),
	# GET /search/results/：返回服务端渲染的搜索结果片段、分页信息和规范 URL。
	path('results/', views.search_results_api, name='search_results_api'),
	# GET /search/suggestions/：返回传统前端搜索框使用的联想建议 JSON。
	path('suggestions/', views.search_suggestions, name='search_suggestions'),

	# GET /search/api/：面向现代前端或外部网关的主搜索 REST API。
	path('api/', api.search_api, name='search_api'),
	# GET /search/api/suggestions/：面向现代前端的搜索建议 REST API。
	path('api/suggestions/', api.search_suggestions_api, name='search_suggestions_api'),
]
