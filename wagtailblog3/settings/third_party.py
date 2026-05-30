#!/user/bin/env python3
# -*- coding: utf-8 -*-


"""
第三方服务配置文件,
包含 REST Framework、CORS 等第三方集成配置
"""
import os
# ============================================
# Wagtail AI Configuration (方法一：)
# ============================================
# WAGTAIL_AI = {
# 	"PROVIDERS": {
# 		"default": {
# 			"provider": "deepseek",
# 			"model": os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
# 			"api_key": os.getenv("DEEPSEEK_API_KEY"),
#
# 			# ← 关键修复：增加超时时间
# 			# "timeout": 120,  # 120 秒超时（默认可能只有 30 秒）
#
# 			# 可选：添加重试配置
# 			# "max_retries": 3,  # 最多重试 3 次
# 		}
# 	},
# 	'BACKENDS': {
# 		'default': {
# 			# ⬇️ 1. 切换到 LLMBackend
# 			'CLASS': 'wagtail_ai.ai.llm.LLMBackend',
# 			'CONFIG': {
# 				# ⬇️ 2. MODEL_ID 必须匹配 YAML 文件中的 'model_id'
# 				'MODEL_ID': 'deepseek-chat',
#
# 				# ⬇️ 3. TOKEN_LIMIT 必须明确定义，因为它是一个自定义模型
# 				'TOKEN_LIMIT': int(os.getenv('AI_MAX_TOKENS', '2000')),
# 			}
# 		},
# 		# (我们暂时移除了 'qwen'，专注于让 'default' 工作)
# 	},
#
# 	# ⬇️ 4. 这一行仍然是必须的
# 	"TEXT_COMPLETION_BACKEND": "default",
# }

WAGTAIL_AI = {
	# 1. 新版配置 (Agents / Providers) - 3.0 核心功能
	"PROVIDERS": {
		"default": {
			"provider": "deepseek",
			"model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
			"api_key": os.environ.get("DEEPSEEK_API_KEY"),
		},
		"qwen": {
			"provider": "openai",
			"model": os.environ.get("QWEN_MODEL", "qwen-plus"),
			"api_key": os.environ.get("QWEN_API_KEY"),
			"base_url": os.environ.get("QWEN_BASE_URL"),
		}
	},
	
	# 2. [新增] 旧版兼容配置 (Backends) - 修复 KeyError 报错
	# 这里我们利用 ai_backends.py 让旧接口也能连上 DeepSeek
	"BACKENDS": {
		"default": {
			# 指向您自定义的类
			"CLASS": "wagtailblog3.ai_backends.OpenAICompatibleBackend",
			"CONFIG": {
				"MODEL_ID": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
				# 旧版逻辑需要手动传入 BASE_URL
				"BASE_URL": "https://api.deepseek.com/v1",
				"TOKEN_LIMIT": 4096,
				# 旧版类内部寻找 OPENAI_API_KEY，我们把 DeepSeek Key 传给它
				"OPENAI_API_KEY": os.environ.get("DEEPSEEK_API_KEY"),
			},
		}
	},
	
	"IMAGE_DESCRIPTION_PROVIDER": "default",
	"AGENT_SETTINGS_MODEL": "wagtail_ai.models.AgentSettings",
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

import mimetypes

# 强制让 Django 认识 .mjs 是合法的 JavaScript 文件
mimetypes.add_type("application/javascript", ".mjs", True)