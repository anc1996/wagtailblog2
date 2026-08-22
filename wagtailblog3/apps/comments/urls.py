# 评论相关接口路由。
from django.urls import path  # 声明评论页面和异步操作路由

from . import views  # 评论创建、编辑、删除、加载和互动视图

app_name = 'comments'  # 评论路由的反向解析命名空间

urlpatterns = [
    # POST /comments/post/<page_id>/：向指定页面提交一条新评论。
    path('post/<int:page_id>/', views.post_comment, name='post_comment'),
    # POST /comments/react/：切换当前用户对评论的互动状态。
    path('react/', views.react_to_comment, name='react_to_comment'),
    # POST /comments/delete/：删除当前用户有权限操作的评论。
    path('delete/', views.delete_comment, name='delete_comment'),
    # POST /comments/edit/：编辑当前用户有权限操作的评论内容。
    path('edit/', views.edit_comment, name='edit_comment'),
    # GET /comments/load/<page_id>/：加载指定页面的顶层评论列表。
    path('load/<int:page_id>/', views.load_comments, name='load_comments'),
    # GET /comments/load-replies/<comment_id>/：加载指定评论的回复列表。
    path('load-replies/<int:comment_id>/', views.load_replies, name='load_replies'),
]
