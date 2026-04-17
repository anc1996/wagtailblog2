"""
Django settings for wagtailblog3 project.

由 'django-admin startproject' 使用 Django 5.1.7 生成。
"""

# 在项目中构建路径，如下所示： os.path.join（BASE_DIR， ...）
import os,sys
from django.contrib import messages


# 当前文件的目录,/xxx/xx/wagtailblog3/wagtailblog3
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
print(PROJECT_DIR)

# 项目的根目录:/xx/xx/wagtailblog3
BASE_DIR = os.path.dirname(PROJECT_DIR)

# ===== 新增：将 apps 目录添加到 Python 路径 =====
# 这样 Django 就能找到移动到 apps 目录下的应用
sys.path.insert(0, os.path.join(PROJECT_DIR, 'apps'))
# ===============================================

# 应用定义
INSTALLED_APPS = [
    "home",  # 首页应用
    "search",  # 搜索应用
    "blog",  # 添加博客应用
    "comments", # 添加评论系统
    "archive",  # 添加归档应用
    "base", # 添加基础应用
    "portfolio", # 添加作品集应用
    
    # 第三方应用
    "storages",  # 添加 Django Storages
	"django_mermaid",  # django-mermaid 包
    "wagtailmarkdown", # wagtail-markdown包
    "wagtailmedia", # wagtail-media 包
    'wagtailcodeblock', #  Wagtail CMS 源代码的语法高亮器块
    "rest_framework",  # Django REST Framework
    "rest_framework_simplejwt",  # JWT 认证
    "drf_yasg",  # API 文档生成
    "corsheaders",  # 如果需要跨域支持
    "wagtail_modeladmin", # Wagtail ModelAdmin,模块允许您将项目中的任何模型添加到 Wagtail 管理界面
	'wagtail_ai',  # ← wagtail-ai库
    
    "wagtail.contrib.forms",  # Wagtail 表单贡献模块
    "wagtail.contrib.redirects",  # Wagtail 重定向贡献模块
    "wagtail.contrib.table_block",  # Wagtail 表格块模块
    "wagtail.contrib.search_promotions",  # 添加搜索推广功能
    "wagtail.contrib.settings",  #保存所有网页通用设置的模型
	'wagtail.locales',# wagtail locales 本地化
    "wagtail.embeds",  # Wagtail 嵌入内容模块
    "wagtail.sites",  # Wagtail 站点管理模块
    "wagtail.users",  # Wagtail 用户管理模块
    "wagtail.snippets",  # Wagtail 代码片段模块
    "wagtail.documents",  # Wagtail 文档管理模块
    "wagtail.images",  # Wagtail 图片管理模块
    "wagtail.search",  # Wagtail 搜索模块
    "wagtail.admin",  # Wagtail 管理后台模块
    "wagtail",  # Wagtail 核心模块
    "modelcluster",  # Django 模型集群模块
    "taggit",  # Django 标签模块
    "django.contrib.admin",  # Django 管理后台模块
    "django.contrib.auth",  # Django 认证模块
    "django.contrib.contenttypes",  # Django 内容类型模块
    "django.contrib.sessions",  # Django 会话模块
    "django.contrib.messages",  # Django 消息模块
    "django.contrib.staticfiles",  # Django 静态文件模块
]



# ==========================================================
# CORS 跨域配置 (依赖django-cors-headers包)
# ==========================================================
CORS_ALLOWED_ORIGINS = [
    "http://localhost:8080",  # Vue.js 开发服务器
    "http://0.0.0.0:8080",  # 本地IP访问
	"http://0.0.0.0:8000",  # 本地IP访问
]

CORS_ALLOW_METHODS = [  # 允许的HTTP请求方法
    'DELETE',  # 删除资源
    'GET',     # 获取资源
    'OPTIONS', # 预检请求
    'PATCH',   # 部分更新资源
    'POST',    # 创建资源
    'PUT',     # 完全更新资源
]

CORS_ALLOW_HEADERS = [  # 允许的HTTP请求头
    'accept',          # 指定客户端能够接收的内容类型
    'accept-encoding', # 指定客户端能够理解的编码方式
    'authorization',   # 包含身份验证信息
    'content-type',    # 指定请求体的媒体类型
    'dnt',            # Do Not Track请求头
    'origin',         # 指示请求来自哪个站点
    'user-agent',     # 客户端应用类型
    'x-csrftoken',    # CSRF防护令牌
    'x-requested-with', # 用于标识AJAX请求
]



MIDDLEWARE = [
    'blog.middleware.PageViewMiddleware',
    "django.contrib.sessions.middleware.SessionMiddleware",  # 处理会话数据，确保每个请求都有会话功能
	'django.middleware.locale.LocaleMiddleware',
    "django.middleware.common.CommonMiddleware",  # 处理常见的HTTP功能，如URL重写、主机验证等
    "django.middleware.csrf.CsrfViewMiddleware",  # 提供CSRF保护，防止跨站请求伪造攻击
    "django.contrib.auth.middleware.AuthenticationMiddleware",  # 将用户与请求关联，提供request.user对象
    "django.contrib.messages.middleware.MessageMiddleware",  # 启用基于cookie或会话的消息系统
    "django.middleware.clickjacking.XFrameOptionsMiddleware",  # 防止点击劫持攻击，添加X-Frame-Options头
    "django.middleware.security.SecurityMiddleware",  # 提供多种安全增强，如HTTPS重定向、XSS保护等
    "wagtail.contrib.redirects.middleware.RedirectMiddleware",  # 处理Wagtail中设置的URL重定向规则
]


ROOT_URLCONF = "wagtailblog3.urls" # 项目的 URL 配置模块

# 模板
TEMPLATES = [  # Django 模板系统配置列表，可配置多个模板引擎
    {  # 主模板引擎配置（通常只配置一个）
        "BACKEND": "django.template.backends.django.DjangoTemplates",  # 使用 Django 内置的模板引擎
        "DIRS": [  # 模板搜索目录列表，Django 会按顺序在这些目录中查找模板
            os.path.join(PROJECT_DIR, "templates"),  # 项目级模板目录，通常放置全站通用模板
        ],
        "APP_DIRS": True,  # 设为 True 时会自动在每个已安装应用的 templates 子目录中查找模板
        "OPTIONS": {  # 模板引擎的附加选项
            "context_processors": [  # 上下文处理器，用于向所有模板添加变量
                "django.template.context_processors.debug",  # 添加 debug 和 sql_queries 变量
                "django.template.context_processors.request",  # 将当前的 request 对象添加到上下文，所以你在模板中可以直接使用 {{ request }}。
                "django.contrib.auth.context_processors.auth",  # 添加 user 变量（当前登录用户）
                "django.contrib.messages.context_processors.messages",  # 添加 messages 变量（消息框架）
                "wagtail.contrib.settings.context_processors.settings",# 告诉 Django 模板引擎：“嘿，每次渲染模板的时候，请调用 Wagtail 的这个上下文处理器，把网站的全局设置取出来，放到一个叫做 settings 的变量里，让我在模板里可以直接用。”
            ],
        },
    },
]

"""
wagtail.contrib.settings ：提供的一个上下文处理器。
    1、定义一些网站级别的配置项（例如，社交媒体链接、联系电话、网站名称等），这些配置项不属于某个特定的页面，而是应用于整个网站。
    2、通过 @register_setting 装饰器和继承 BaseGenericSetting 来创建这样的设置模型（就像你创建的 NavigationSettings）。
    
wagtail.contrib.settings.context_processors.settings 的作用：
    在每次渲染模板之前，它会去数据库中查找你通过 wagtail.contrib.settings 定义的所有全局设置（比如 NavigationSettings）。
    然后，它会将这些设置对象打包到一个名为 settings 的变量中，并添加到模板的上下文中。
    这个 settings 变量是一个特殊的结构，你可以通过 settings.app_label.SettingModelName.field_name 的方式来访问具体的设置值。
        例如，你创建的 NavigationSettings 模型在 base 应用中，所以你在模板中可以通过 settings.base.NavigationSettings.linkedin_url 来访问 LinkedIn URL。
"""

# WSGI应用程序
WSGI_APPLICATION = "wagtailblog3.wsgi.application"

# ===============================================================

# 密码验证
# https://docs.djangoproject.com/en/5.1/ref/settings/#auth-password-validators
AUTH_PASSWORD_VALIDATORS = [  # 定义密码验证器列表，用于检查用户输入密码的强度和质量
    {
        # 检查密码是否与用户属性过于相似（如用户名、邮箱等），防止使用容易被猜测的相关信息作密码
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        # 验证密码长度是否达到最小要求（默认至少8个字符），可通过OPTIONS参数自定义长度
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        # 检查是否使用了常见密码（如"password"、"123456"等），基于内置的常见密码列表
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        # 检查密码是否仅包含数字，禁止纯数字密码，提高安全性
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


from dotenv import load_dotenv

# 加载环境变量
load_dotenv()






# 1. 基础 Django 配置
LANGUAGE_CODE = "zh-hans"  # 默认语言设置为简体中文
TIME_ZONE = "Asia/Shanghai"  # 时区设置为上海

USE_I18N = True  # 必须为 True，启用翻译系统
USE_TZ = True    # 启用时区感知

# 2. ★★★ 新增：Wagtail 多语言配置 ★★★

# 开启 Wagtail 的多语言内容支持（这会让后台出现 Locale 列）
WAGTAIL_I18N_ENABLED = True

# 定义站点支持的语言列表
# 建议同时赋值给 Django 的 LANGUAGES 和 Wagtail 的 WAGTAIL_CONTENT_LANGUAGES
# 格式：('语言代码', '显示名称')
WAGTAIL_CONTENT_LANGUAGES = LANGUAGES = [
    ('zh-hans', '简体中文'),  # 默认语言（与 LANGUAGE_CODE 保持一致）
    ('en', 'English'),      # 第二语言
]


# 静态文件 (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.1/howto/static-files/
STATICFILES_FINDERS = [
    # Django 默认的静态文件查找器
    "django.contrib.staticfiles.finders.FileSystemFinder",
    # Django 默认的应用目录查找器
    "django.contrib.staticfiles.finders.AppDirectoriesFinder",
]

# 这个配置定义了静态文件应用在启用 FileSystemFinder 查找器时将穿越的额外位置，
# 例如，如果你使用 collectstatic 或 findstatic 管理命令或使用静态文件服务视图。
STATICFILES_DIRS = [
    os.path.join(PROJECT_DIR, "static"), # 项目级静态文件目录
]

STATIC_ROOT = os.path.join(BASE_DIR, "staticfiles_collected") # 静态文件收集目录
STATIC_URL = "/static/" # 静态文件的 URL 前缀

MEDIA_ROOT = os.path.join(BASE_DIR, "media") # 媒体文件存储目录
MEDIA_URL = "/media/" # 媒体文件的 URL 前缀




# Wagtail 自定义模型配置，admin对后台内容进行编辑
WAGTAILDOCS_DOCUMENT_MODEL = 'blog.BlogDocument' # 自定义文档模型

WAGTAILIMAGES_IMAGE_MODEL = 'blog.BlogImage' # 自定义图片模型

# Django 默认将每个表单的最大字段数设置为 1000，但特别复杂的页面模型
# 可能会在 Wagtail 的页面编辑器中超出此限制。
DATA_UPLOAD_MAX_NUMBER_FIELDS = 10_000

# Wagtail 设置
WAGTAIL_SITE_NAME = "wagtailblog3"

# 搜索
# https://docs.wagtail.org/en/stable/topics/search/backends.html
WAGTAILSEARCH_BACKENDS = {
    'default': {
        'BACKEND': 'wagtailblog3.search.CustomSearchBackend',
    }
}

# 在 Wagtail 管理后端中引用完整 URL 时使用的基本 URL -
# 例如，在通知电子邮件中。 不要包含 '/admin' 或尾部斜杠
WAGTAILADMIN_BASE_URL = "http://example.com"


# 文档库中允许的文件扩展名。
# 可以省略此项以允许所有文件，但请注意，如果允许不受信任的用户上传文件，
# 参见 https://docs.wagtail.org/en/stable/advanced_topics/deploying.html#user-uploaded-files
WAGTAILDOCS_EXTENSIONS = ['csv', 'docx', 'key', 'odt', 'pdf', 'pptx', 'rtf', 'txt', 'xlsx', 'zip','md']

# 消息框架设置（用于表单反馈）
MESSAGE_TAGS = {
    messages.DEBUG: 'debug',
    messages.INFO: 'info',
    messages.SUCCESS: 'success',
    messages.WARNING: 'warning',
    messages.ERROR: 'danger',
}


# ===============================================================
# 评论系统配置
# ===============================================================
COMMENT_RATE_LIMIT = {
    'ENABLED': True,
    'INTERVAL_SECONDS': 60, # 1分钟
    'MAX_COMMENTS': 3,      # 1分钟内最多3条评论
    'CACHE_ALIAS': 'comment_rate_limit_cache', # 指定用于频率限制的缓存别名
}



# ===============================================
# 导入所有配置模块
# ===============================================
# 注意：导入顺序很重要，因为某些配置可能依赖其他配置

# 1. 首先导入数据库配置（包含 Redis、MongoDB、MinIO）
from .database import *

# 2. 应用 Celery 配置（需要使用 TIME_ZONE 和 Redis 配置）
from .database import get_celery_config

celery_config = get_celery_config(
    time_zone=TIME_ZONE,
    redis_host=REDIS_HOST,
    redis_port=REDIS_PORT,
    redis_password=REDIS_PASSWORD
)

# 将 Celery 配置应用到当前模块
globals().update(celery_config)

# 3. 导入邮件服务配置
from .email import *

# 4. 导入文本编辑器配置
from .text_editor import *

# 5. 导入第三方服务配置
from .third_party import *

# ===============================================
# 打印配置信息（用于启动时确认）
# ===============================================
print_database_config()