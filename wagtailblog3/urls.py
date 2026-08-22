# wagtailblog3/urls.py：项目根 URL 配置，负责挂载管理后台、应用路由和 Wagtail 页面服务。

from django.conf import settings  # 读取 DEBUG、媒体 URL 等运行环境配置
from django.conf.urls.i18n import i18n_patterns  # 为面向用户的路由增加语言前缀
from django.urls import include, path, re_path  # 组合应用路由并声明路径/正则路由
from django.contrib import admin  # 提供 Django 原生管理后台路由

from wagtail.admin import urls as wagtailadmin_urls  # Wagtail 管理后台路由集合
from wagtail import urls as wagtail_urls  # Wagtail 页面树通配服务路由
from wagtail.documents import urls as wagtaildocs_urls  # Wagtail 文档下载路由
from wagtail.images.views.serve import ServeView  # Wagtail 图片源文件重定向视图

from blog.views import test_search_backend  # 开发环境搜索后端连通性测试视图

urlpatterns = [
    # /django-admin/：Django 原生管理后台，供项目基础模型和系统功能管理使用。
    path("django-admin/", admin.site.urls),
    # /admin/：Wagtail 内容编辑、发布、用户和站点配置后台。
    path("admin/", include(wagtailadmin_urls)),
    # /documents/：Wagtail 文档选择器和文档下载服务。
    path("documents/", include(wagtaildocs_urls)),
]

if settings.DEBUG:
    from django.conf.urls.static import static  # 开发模式下提供媒体文件映射
    from django.contrib.staticfiles.urls import staticfiles_urlpatterns  # 开发模式下提供静态文件映射

    # DEBUG 模式才启用本地静态文件服务，生产环境由 Nginx/CDN 等专用服务承担。
    urlpatterns += staticfiles_urlpatterns()
    # DEBUG 模式才启用本地媒体文件服务，避免生产 Django 进程直接暴露媒体目录。
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

urlpatterns = urlpatterns + i18n_patterns(
    # /<language>/test-search/：开发/诊断用搜索后端连通性检查，不承担正式搜索请求。
    path('test-search/', test_search_backend, name='test_search_backend'),
    # /<language>/feed/：站点根级订阅入口，必须在 Wagtail 通配路由之前注册。
    path('feed/', include('blog.feed_urls', namespace='blog_feed')),
    # /<language>/comments/：挂载评论创建、编辑、删除和加载接口。
    path('comments/', include('comments.urls', namespace='comments')),
    # /<language>/blog/：挂载博客 Markdown 导入、互动、作者和索引 API。
    path('blog/', include('blog.urls', namespace='blog')),
    # /<language>/archive/：挂载年份、月份归档页面及异步结果 API。
    path('archive/', include('archive.urls')),
    # /<language>/search/：挂载搜索页面、结果片段和两个版本的搜索 API。
    path('search/', include('search.urls', namespace='search')),
    # /<language>/images/...：允许 Markdown 导入附件路径包含嵌套文件名，并交由 Wagtail 图片服务处理。
    # 图片源文件可能位于 markdown-import/<artifact_id>/<filename>，路由必须允许嵌套文件名。
    re_path(r'^images/([^/]*)/(\d*)/([^/]*)/.*$', ServeView.as_view(action='redirect'), name='wagtailimages_serve'),
    
    # /<language>/...：最后的 Wagtail 通配路由，负责将未被应用路由捕获的路径解析为页面。
    path("", include(wagtail_urls)),
    # 如需从站点子路径提供页面，可改用下面的 /pages/ 挂载方式；当前保持禁用。
    #    path("pages/", include(wagtail_urls)),
)
