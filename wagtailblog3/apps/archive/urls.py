# 归档应用的 URL 路由
from django.urls import path  # 声明归档页面和归档结果接口路由

from . import views  # 归档页面及其异步结果视图

app_name = 'archive'  # 归档路由的反向解析命名空间

urlpatterns = [
    # GET /archive/api/archives/：返回归档年份、月份及文章数量等聚合数据。
    path('api/archives/', views.archives_api, name='archives_api'),
    # GET /archive/api/year/<year>/results/：按年份返回服务端渲染的文章结果片段。
    path(
        'api/year/<int:year>/results/',
        views.year_archive_results_api,
        name='year_archive_results_api',
    ),
    # GET /archive/api/year/<year>/month/<month>/results/：按年份和月份返回结果片段。
    path(
        'api/year/<int:year>/month/<int:month>/results/',
        views.month_archive_results_api,
        name='month_archive_results_api',
    ),
    # GET /archive/year/<year>/：渲染指定年份的归档页面。
    path('year/<int:year>/', views.year_archive, name='year_archive'),
    # GET /archive/year/<year>/month/<month>/：渲染指定月份的归档页面。
    path('year/<int:year>/month/<int:month>/', views.month_archive, name='month_archive'),
]
