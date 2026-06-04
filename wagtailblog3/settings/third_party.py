#!/user/bin/env python3
# -*- coding: utf-8 -*-

# wagtailblog3/settings/third_party.py
"""
第三方服务配置文件,
包含 REST Framework、CORS 、ai-backends等第三方集成配置
"""

import os
import mimetypes


# ==========================================================
# 异构多模型网关凭证解析
# ==========================================================
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

QWEN_API_KEY = os.environ.get("QWEN_API_KEY")
QWEN_BASE_URL = os.environ.get("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
QWEN_MODEL = os.environ.get("QWEN_MODEL", "qwen-plus-latest")

# 业务层通用超参数设定
AI_MAX_TOKENS = int(os.environ.get("AI_MAX_TOKENS", 4096))
AI_TEMPERATURE = float(os.environ.get("AI_TEMPERATURE", 0.7))

# ==========================================================
# Wagtail-AI v3.1.0 智能化调度总线
# ==========================================================
WAGTAIL_AI = {
    # === 核心 1：V3.1.0 魔法面板 (Agent) 专属引擎 ===
    "PROVIDERS": {
        "default": {
            "provider": "openai",
            "model": DEEPSEEK_MODEL,
            "api_key": DEEPSEEK_API_KEY,
            "api_base": DEEPSEEK_BASE_URL,
            # (移除 temperature 和 max_tokens，使用模型默认的极佳生成策略)
        },
        "qwen": {
            "provider": "openai",
            "model": QWEN_MODEL,
            "api_key": QWEN_API_KEY,
            "api_base": QWEN_BASE_URL,
        }
    },
    
    # === 核心 2：旧版草稿编辑器 (Draftail) 兼容引擎 ===
    "BACKENDS": {
        "default": {
            "CLASS": "wagtailblog3.ai_backends.FlexibleOpenAIBackend",
            "CONFIG": {
                "MODEL_ID": DEEPSEEK_MODEL,
                "API_BASE": DEEPSEEK_BASE_URL,
                "OPENAI_API_KEY": DEEPSEEK_API_KEY,
                "TOKEN_LIMIT": AI_MAX_TOKENS,
                "TEMPERATURE": AI_TEMPERATURE,
                "TIMEOUT_SECONDS": 60,
            },
        },
        "qwen": {
            "CLASS": "wagtailblog3.ai_backends.FlexibleOpenAIBackend",
            "CONFIG": {
                "MODEL_ID": QWEN_MODEL,
                "API_BASE": QWEN_BASE_URL,
                "OPENAI_API_KEY": QWEN_API_KEY,
                "TOKEN_LIMIT": AI_MAX_TOKENS,
                "TEMPERATURE": AI_TEMPERATURE,
                "TIMEOUT_SECONDS": 60,
            },
        },
    },
    
    # 默认无障碍视觉辅助复用主算力通道
    "IMAGE_DESCRIPTION_PROVIDER": "default",
}

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