# 博客应用的 URL 路由
from django.urls import path
from . import views
from .views import AuthorListView, AuthorDetailView

app_name = 'blog'

urlpatterns = [
    path('api/reactions/<int:page_id>/toggle/', views.toggle_reaction, name='toggle_reaction'),
    path('api/reactions/<int:page_id>/counts/', views.get_reaction_counts, name='get_reaction_counts'),
    
    # 作者列表和作者详情路由
    path('authors/', AuthorListView.as_view(), name='author_list'),
    path('authors/<int:pk>/', AuthorDetailView.as_view(), name='author_detail'),
]
