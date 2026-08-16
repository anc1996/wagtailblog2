#!/user/bin/env python3
# -*- coding: utf-8 -*-

# wagtailblog3/settings/third_party.py
"""
第三方服务配置文件,
包含 REST Framework、CORS 和内容 AI 等第三方集成配置
"""

import os
import mimetypes


# 博客元数据生成独立使用 Responses API。
# 仅从当前环境文件读取，生产默认不配置该服务；响应存储必须保持关闭。
AI_METADATA_PROVIDER = os.environ.get("AI_METADATA_PROVIDER", "openai")
AI_METADATA_API_KEY = os.environ.get("AI_METADATA_API_KEY", "")
AI_METADATA_BASE_URL = os.environ.get("AI_METADATA_BASE_URL", "")
AI_METADATA_MODEL = os.environ.get("AI_METADATA_MODEL", "")
AI_METADATA_REASONING_EFFORT = os.environ.get("AI_METADATA_REASONING_EFFORT", "")
AI_METADATA_RESPONSE_STORAGE = os.environ.get("AI_METADATA_RESPONSE_STORAGE", "false").lower() == "true"
AI_METADATA_TIMEOUT_SECONDS = int(os.environ.get("AI_METADATA_TIMEOUT_SECONDS", 60))
AI_METADATA_MAX_CONTEXT_CHARS = int(os.environ.get("AI_METADATA_MAX_CONTEXT_CHARS", 24000))

# ==========================================================
# Django REST Framework 配置
# ==========================================================
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',  # 使用JWT令牌认证
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticatedOrReadOnly',  # 默认权限：认证用户可读写，匿名用户只读
    ),
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',  # 使用分页页码
    'PAGE_SIZE': 10,  # 每页显示10条记录
    'DEFAULT_RENDERER_CLASSES': (
        'rest_framework.renderers.JSONRenderer',  # 默认使用JSON渲染器
    ),
}


# ==========================================================
# Swagger API 文档配置
# ==========================================================
SWAGGER_SETTINGS = {
    'SECURITY_DEFINITIONS': {
        'Bearer': {
            'type': 'apiKey',
            'name': 'Authorization',
            'in': 'header'
        }
    }
}


# 强制让 Django 认识 .mjs 是合法的 JavaScript 文件
mimetypes.add_type("application/javascript", ".mjs", True)

WAGTAILEMBEDS_FINDERS = [
    {'class': 'wagtailblog3.apps.blog.embeds.BilibiliFinder'},
    {'class': 'wagtailblog3.apps.blog.embeds.TencentVideoFinder'},
    {'class': 'wagtailblog3.apps.blog.embeds.YoukuFinder'},
    {'class': 'wagtailblog3.apps.blog.embeds.NetEaseMusicFinder'},
    {'class': 'wagtailblog3.apps.blog.embeds.QQMusicFinder'},
    {'class': 'wagtailblog3.apps.blog.embeds.KugouMusicFinder'},
    {'class': 'wagtailblog3.apps.blog.embeds.MiguMusicFinder'},
    
    # 原生 OEmbed 兜底一定要放在最后一行
    {'class': 'wagtail.embeds.finders.oembed'},
]
