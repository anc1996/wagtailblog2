# 博客应用的 URL 路由
from django.urls import path
from . import views
from .views import AuthorListView, AuthorDetailView

app_name = 'blog'

urlpatterns = [
    path('api/reactions/<int:page_id>/toggle/', views.toggle_reaction, name='toggle_reaction'),
    path('api/reactions/<int:page_id>/counts/', views.get_reaction_counts, name='get_reaction_counts'),
    path('api/index-pages/<int:pk>/results/', views.blog_index_results_api, name='blog_index_results_api'),
    path('api/authors/<int:pk>/posts/', views.author_posts_api, name='author_posts_api'),

    # 作者列表和作者详情路由
    path('authors/', AuthorListView.as_view(), name='author_list'),
    path('authors/<int:pk>/', AuthorDetailView.as_view(), name='author_detail'),
]
