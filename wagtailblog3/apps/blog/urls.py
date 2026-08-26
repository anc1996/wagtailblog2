# 博客应用的 URL 路由
from django.urls import path  # 声明博客页面、内容导入和互动 API 路由

from . import views  # 博客索引、反应和作者文章视图
from .views import AuthorListView, AuthorDetailView  # 作者列表和作者详情页面视图
from .markdown_import_api import (
    MarkdownImportDestinationsView,
    MarkdownImportDuplicateTitlesView,
    MarkdownImportLimitsView,
    MarkdownImportMetadataSuggestionView,
    MarkdownImportMetadataTemplatesView,
    MarkdownImportPreviewView,
    MarkdownImportUserscriptPrepareView,
    MarkdownImportView,
    MarkdownImportSessionCreateView,
    MarkdownImportSessionDetailView,
    MarkdownImportSessionArtifactUploadView,
    MarkdownImportSessionFinalizeView,
)
from .analytics_views import record_engagement  # 记录前端互动事件的接口视图

app_name = 'blog'  # 博客路由的反向解析命名空间

urlpatterns = [
    # GET /blog/api/markdown-import/limits/：返回 Markdown 导入允许的大小、块数和超时限制。
    path('api/markdown-import/limits/', MarkdownImportLimitsView.as_view(), name='markdown_import_limits'),
    # GET /blog/api/markdown-import/destinations/：返回当前用户可选择的导入目标页面。
    path('api/markdown-import/destinations/', MarkdownImportDestinationsView.as_view(), name='markdown_import_destinations'),
    # GET /blog/api/markdown-import/duplicate-titles/：检查目标位置是否存在重复标题。
    path('api/markdown-import/duplicate-titles/', MarkdownImportDuplicateTitlesView.as_view(), name='markdown_import_duplicate_titles'),
    # GET /blog/api/markdown-import/ai/templates/：返回 AI 元数据建议可用的模板列表。
    path('api/markdown-import/ai/templates/', MarkdownImportMetadataTemplatesView.as_view(), name='markdown_import_ai_templates'),
    # POST /blog/api/markdown-import/ai/suggest/：根据导入内容请求 AI 元数据建议。
    path('api/markdown-import/ai/suggest/', MarkdownImportMetadataSuggestionView.as_view(), name='markdown_import_ai_suggest'),
    # POST /blog/api/markdown-import/preview/：解析并预览 Markdown，不直接创建正式页面。
    path('api/markdown-import/preview/', MarkdownImportPreviewView.as_view(), name='markdown_import_preview'),
    # POST /blog/api/markdown-import/userscript/prepare/：准备浏览器 userscript 所需的导入参数。
    path('api/markdown-import/userscript/prepare/', MarkdownImportUserscriptPrepareView.as_view(), name='markdown_import_userscript_prepare'),
    # POST /blog/api/markdown-import/import/：提交一次 Markdown 导入请求并创建页面草稿流程。
    path('api/markdown-import/import/', MarkdownImportView.as_view(), name='markdown_import'),
    # POST /blog/api/markdown-import/sessions/：创建分阶段 Markdown 导入会话。
    path('api/markdown-import/sessions/', MarkdownImportSessionCreateView.as_view(), name='markdown_import_session_create'),
    # GET/PATCH /blog/api/markdown-import/sessions/<session_id>/：读取或更新导入会话状态。
    path('api/markdown-import/sessions/<uuid:session_id>/', MarkdownImportSessionDetailView.as_view(), name='markdown_import_session_detail'),
    # POST /blog/api/markdown-import/sessions/<session_id>/artifacts/<artifact_id>/upload/：上传会话附件。
    path('api/markdown-import/sessions/<uuid:session_id>/artifacts/<uuid:artifact_id>/upload/', MarkdownImportSessionArtifactUploadView.as_view(), name='markdown_import_session_artifact_upload'),
    # POST /blog/api/markdown-import/sessions/<session_id>/finalize/：校验并完成分阶段导入会话。
    path('api/markdown-import/sessions/<uuid:session_id>/finalize/', MarkdownImportSessionFinalizeView.as_view(), name='markdown_import_session_finalize'),
    # POST /blog/api/analytics/engagement/：接收页面阅读或互动统计事件。
    path('api/analytics/engagement/', record_engagement, name='engagement'),
    # POST /blog/api/reactions/<page_id>/toggle/：切换当前用户对博客页面的反应状态。
    path('api/reactions/<int:page_id>/toggle/', views.toggle_reaction, name='toggle_reaction'),
    # GET /blog/api/reactions/<page_id>/counts/：读取博客页面各类反应数量。
    path('api/reactions/<int:page_id>/counts/', views.get_reaction_counts, name='get_reaction_counts'),
    # GET /blog/api/index-pages/<pk>/results/：返回博客索引页的异步结果片段。
    path('api/index-pages/<int:pk>/results/', views.blog_index_results_api, name='blog_index_results_api'),
    # GET /blog/api/tag-index-pages/<pk>/results/：返回标签索引页的异步结果片段。
    path('api/tag-index-pages/<int:pk>/results/', views.tag_index_results_api, name='tag_index_results_api'),
    # GET /blog/api/authors/<pk>/posts/：返回指定作者的文章结果片段。
    path('api/authors/<int:pk>/posts/', views.author_posts_api, name='author_posts_api'),
    # GET /blog/api/gallery/<pk>/items/：返回公开文章画廊的下一批图片。
    path('api/gallery/<int:pk>/items/', views.gallery_items_api, name='gallery_items_api'),

    # GET /blog/authors/：渲染作者列表页面。
    path('authors/', AuthorListView.as_view(), name='author_list'),
    # GET /blog/authors/<pk>/：渲染指定作者的详情页面。
    path('authors/<int:pk>/', AuthorDetailView.as_view(), name='author_detail'),
]
