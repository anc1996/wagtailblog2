"""博客订阅源的根级路由。"""

from django.urls import path

from .feeds import (
    AuthorBlogAtomFeed,
    AuthorBlogRssFeed,
    BlogAtomFeed,
    BlogRssFeed,
    TagBlogAtomFeed,
    TagBlogRssFeed,
)

app_name = "blog_feed"

urlpatterns = [
    path("rss/", BlogRssFeed(), name="rss"),
    path("atom/", BlogAtomFeed(), name="atom"),
    # 标签slug可能为Unicode，必须使用str转换器而非slug转换器。
    path("tag/<str:tag_slug>/rss/", TagBlogRssFeed(), name="tag_rss"),
    path("tag/<str:tag_slug>/atom/", TagBlogAtomFeed(), name="tag_atom"),
    path("author/<str:author_slug>/rss/", AuthorBlogRssFeed(), name="author_rss"),
    path("author/<str:author_slug>/atom/", AuthorBlogAtomFeed(), name="author_atom"),
]
