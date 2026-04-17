# blog/urls.py
from django.urls import path
from . import views
from .views import AuthorListView, AuthorDetailView

app_name = 'blog'

urlpatterns = [
    path('api/reactions/<int:page_id>/toggle/', views.toggle_reaction, name='toggle_reaction'),
    path('api/reactions/<int:page_id>/counts/', views.get_reaction_counts, name='get_reaction_counts'),
    
    # 添加作者相关的 URL 路径
    path('authors/', AuthorListView.as_view(), name='author_list'),
    path('authors/<int:pk>/', AuthorDetailView.as_view(), name='author_detail'),
]