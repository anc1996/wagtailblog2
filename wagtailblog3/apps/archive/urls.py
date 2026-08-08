# 归档应用的 URL 路由
from django.urls import path
from . import views

app_name = 'archive'

urlpatterns = [
    # 归档数据接口
    path('api/archives/', views.archives_api, name='archives_api'),
    path(
        'api/year/<int:year>/results/',
        views.year_archive_results_api,
        name='year_archive_results_api',
    ),
    path(
        'api/year/<int:year>/month/<int:month>/results/',
        views.month_archive_results_api,
        name='month_archive_results_api',
    ),
    # 按年份查看文章
    path('year/<int:year>/', views.year_archive, name='year_archive'),
    # 按月份查看文章
    path('year/<int:year>/month/<int:month>/', views.month_archive, name='month_archive'),
]
