from django.contrib import admin
from django.db.models import Count
from taggit.models import Tag
from wagtail.permission_policies import ModelPermissionPolicy
from wagtail.snippets.views.snippets import SnippetViewSet
from wagtail.admin.panels import FieldPanel
from .models import PageView, PageViewCount, ReactionType, Reaction
from wagtail.admin.ui.tables import Column

@admin.register(PageView)
class PageViewAdmin(admin.ModelAdmin):
    list_display = ('page', 'user', 'ip_address', 'date', 'last_viewed_at')
    list_filter = ('date',)
    search_fields = ('page__title', 'ip_address', 'user__username')
    date_hierarchy = 'last_viewed_at'
    
    def has_add_permission(self, request):
        # 不允许手动添加，应通过中间件自动生成
        return False

    def has_change_permission(self, request, obj=None):
        # 访问记录属于审计数据，只能由访问计数服务写入。
        return False

    def has_delete_permission(self, request, obj=None):
        # 防止后台操作破坏访问审计记录。
        return False

@admin.register(PageViewCount)
class PageViewCountAdmin(admin.ModelAdmin):
    list_display = ('page', 'date', 'view_count_v2', 'unique_visitor_count_v2')
    list_filter = ('date',)
    search_fields = ('page__title',)
    date_hierarchy = 'date'
    
    def has_add_permission(self, request):
        # 不允许手动添加，应通过数据同步生成
        return False

    def has_change_permission(self, request, obj=None):
        # 聚合统计只能由服务端原子写入，后台不得手工篡改统计口径。
        return False

    def has_delete_permission(self, request, obj=None):
        return False

@admin.register(ReactionType)
class ReactionTypeAdmin(admin.ModelAdmin):
    list_display = ('name', 'icon', 'display_order')
    search_fields = ('name',)
    ordering = ('display_order',)

@admin.register(Reaction)
class ReactionAdmin(admin.ModelAdmin):
    list_display = ('page', 'reaction_type', 'user', 'ip_address', 'created_at')
    list_filter = ('reaction_type', 'created_at')
    search_fields = ('page__title', 'user__username', 'ip_address')
    date_hierarchy = 'created_at'


# =========================================================
# 高级标签管理面板（收纳至“片段”菜单）
# =========================================================
class TagsSnippetViewSet(SnippetViewSet):
    model = Tag
    icon = "tag"
    menu_label = "博客标签"  # 在片段列表中显示的中文名称
    
    # 关闭强制挂载到主菜单，让标签自动归入 Wagtail 的“片段”菜单。
    add_to_admin_menu = False
    
    # 后台新建或修改标签时只显示名称字段，避免编辑者直接修改派生 slug。
    panels = [FieldPanel("name")]
    
    # 显式指定列标题和数据库排序字段，确保计数列可以正确排序。
    list_display = [
        "name",
        "slug",
        Column("post_count", label="文章引用数量", sort_key="post_count")
    ]
    search_fields = ("name",)
    
    def get_queryset(self, request):
        """
		重写查询集：在数据库层面直接计算每个标签被 BlogPage 引用的次数
		"""
        qs = self.model.objects.all()
        qs = qs.annotate(post_count=Count('blog_blogpagetag_items'))
        return qs


class ReadOnlyPageViewPermissionPolicy(ModelPermissionPolicy):
    """Allow viewing PageView rows while blocking all mutations."""

    MUTATING_ACTIONS = {"add", "change", "delete"}

    def user_has_permission(self, user, action):
        if action in self.MUTATING_ACTIONS:
            return False
        return super().user_has_permission(user, action)


class PageViewSnippetViewSet(SnippetViewSet):
    """Read-only Wagtail listing for the page-view audit table."""

    model = PageView
    icon = "date"
    menu_label = "页面访问记录"
    add_to_admin_menu = False
    ordering = ("-last_viewed_at", "-pk")
    list_display = [
        "admin_page_title",
        "admin_user",
        Column("ip_address", label="IP 地址", sort_key="ip_address"),
        Column("date", label="访问日期", sort_key="date"),
        Column(
            "last_viewed_at",
            label="最后访问时间",
            sort_key="last_viewed_at",
        ),
    ]
    search_fields = ("page__title", "ip_address", "user__username")
    list_filter = ("date",)
    inspect_view_enabled = True
    inspect_view_fields = (
        "page",
        "user",
        "ip_address",
        "date",
        "last_viewed_at",
        "user_agent",
    )

    @property
    def permission_policy(self):
        return ReadOnlyPageViewPermissionPolicy(self.model)

    def get_queryset(self, request):
        return self.model.objects.select_related("page", "user")
