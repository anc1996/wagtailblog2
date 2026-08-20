# 博客应用的 URL 路由
from django.urls import path
from . import views
from .views import AuthorListView, AuthorDetailView
from .markdown_import_api import (
    MarkdownImportDestinationsView,
    MarkdownImportDuplicateTitlesView,
    MarkdownImportLimitsView,
    MarkdownImportMetadataSuggestionView,
    MarkdownImportMetadataTemplatesView,
    MarkdownImportPreviewView,
    MarkdownImportView,
    MarkdownImportSessionCreateView,
    MarkdownImportSessionDetailView,
    MarkdownImportSessionArtifactUploadView,
    MarkdownImportSessionFinalizeView,
)
from .analytics_views import record_engagement

app_name = 'blog'

urlpatterns = [
    path('api/markdown-import/limits/', MarkdownImportLimitsView.as_view(), name='markdown_import_limits'),
    path('api/markdown-import/destinations/', MarkdownImportDestinationsView.as_view(), name='markdown_import_destinations'),
    path('api/markdown-import/duplicate-titles/', MarkdownImportDuplicateTitlesView.as_view(), name='markdown_import_duplicate_titles'),
    path('api/markdown-import/ai/templates/', MarkdownImportMetadataTemplatesView.as_view(), name='markdown_import_ai_templates'),
    path('api/markdown-import/ai/suggest/', MarkdownImportMetadataSuggestionView.as_view(), name='markdown_import_ai_suggest'),
    path('api/markdown-import/preview/', MarkdownImportPreviewView.as_view(), name='markdown_import_preview'),
    path('api/markdown-import/import/', MarkdownImportView.as_view(), name='markdown_import'),
    path('api/markdown-import/sessions/', MarkdownImportSessionCreateView.as_view(), name='markdown_import_session_create'),
    path('api/markdown-import/sessions/<uuid:session_id>/', MarkdownImportSessionDetailView.as_view(), name='markdown_import_session_detail'),
    path('api/markdown-import/sessions/<uuid:session_id>/artifacts/<uuid:artifact_id>/upload/', MarkdownImportSessionArtifactUploadView.as_view(), name='markdown_import_session_artifact_upload'),
    path('api/markdown-import/sessions/<uuid:session_id>/finalize/', MarkdownImportSessionFinalizeView.as_view(), name='markdown_import_session_finalize'),
    path('api/analytics/engagement/', record_engagement, name='engagement'),
    path('api/reactions/<int:page_id>/toggle/', views.toggle_reaction, name='toggle_reaction'),
    path('api/reactions/<int:page_id>/counts/', views.get_reaction_counts, name='get_reaction_counts'),
    path('api/index-pages/<int:pk>/results/', views.blog_index_results_api, name='blog_index_results_api'),
    path('api/tag-index-pages/<int:pk>/results/', views.tag_index_results_api, name='tag_index_results_api'),
    path('api/authors/<int:pk>/posts/', views.author_posts_api, name='author_posts_api'),

    # 作者列表和作者详情路由
    path('authors/', AuthorListView.as_view(), name='author_list'),
    path('authors/<int:pk>/', AuthorDetailView.as_view(), name='author_detail'),
]
