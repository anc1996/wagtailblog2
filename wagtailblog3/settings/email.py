#!/user/bin/env python3
# -*- coding: utf-8 -*-



# ==========================================================
# 邮件服务配置
# ==========================================================

import os

# QQ邮箱SMTP配置（推荐配置）
EMAIL_BACKEND = os.environ.get(
	'EMAIL_BACKEND', 'django.core.mail.backends.smtp.EmailBackend'
)
EMAIL_HOST = os.environ.get('EMAIL_HOST', 'localhost')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '25'))
EMAIL_USE_SSL = os.environ.get('EMAIL_USE_SSL', 'false').lower() in {'1', 'true', 'yes', 'on'}
EMAIL_USE_TLS = os.environ.get('EMAIL_USE_TLS', 'false').lower() in {'1', 'true', 'yes', 'on'}
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', EMAIL_HOST_USER)

# 邮件发送相关设置
EMAIL_TIMEOUT = int(os.environ.get('EMAIL_TIMEOUT', '30'))
EMAIL_MAX_RECIPIENTS = int(os.environ.get('EMAIL_MAX_RECIPIENTS', '50'))

# 异步邮件发送开关（可在管理后台动态控制）
ASYNC_EMAIL_ENABLED = True

# 邮件发送统计和日志配置
EMAIL_LOGGING_ENABLED = True
EMAIL_STATS_ENABLED = True


# 启用邮件发送频率限制
EMAIL_RATE_LIMIT_ENABLED = True
# 全局邮件发送频率限制时间（秒）- 默认5分钟
EMAIL_RATE_LIMIT_SECONDS = 300
# 针对特定表单页面的个性化频率限制配置
# 格式：{表单页面ID: 限制时间（秒）}
EMAIL_RATE_LIMIT_PER_FORM = {
    # 示例：表单ID为1的页面设置为3分钟限制
    # 1: 180,
    # 表单ID为2的页面设置为10分钟限制
    # 2: 600,
}

# 表单邮件发送配置
FORM_EMAIL_SETTINGS = {
    'CONFIRMATION_EMAIL_ENABLED': True,
    'ADMIN_NOTIFICATION_ENABLED': True,
    'DEFAULT_PRIORITY': 'normal',
    'RETRY_ATTEMPTS': 3,
    'RETRY_DELAY': 60,
}

# 表单设置
WAGTAILFORMS_CONFIRMATION_EMAIL_TEMPLATE = 'emails/form_confirmation.html'


