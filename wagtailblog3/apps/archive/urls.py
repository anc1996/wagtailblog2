# archive/urls.py
from django.urls import path
from . import views

app_name = 'archive'

urlpatterns = [
    # API接口
    path('api/archives/', views.archives_api, name='archives_api'),
    # year_archive视图
    path('year/<int:year>/', views.year_archive, name='year_archive'),
    # month_archive视图
    path('year/<int:year>/month/<int:month>/', views.month_archive, name='month_archive'),
]