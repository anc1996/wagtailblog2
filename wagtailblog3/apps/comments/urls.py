# comments/urls.py
from django.urls import path
from . import views

app_name = 'comments'

urlpatterns = [
    path('post/<int:page_id>/', views.post_comment, name='post_comment'),
    path('react/', views.react_to_comment, name='react_to_comment'),
    path('delete/', views.delete_comment, name='delete_comment'),
    path('edit/', views.edit_comment, name='edit_comment'),
    path('load/<int:page_id>/', views.load_comments, name='load_comments'),
    path('load-replies/<int:comment_id>/', views.load_replies, name='load_replies'),
]