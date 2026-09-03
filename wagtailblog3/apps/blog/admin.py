from django.contrib import admin
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import path
from django.db.models import Count
from taggit.models import Tag
from wagtail.permission_policies import ModelPermissionPolicy
from wagtail.permissions import register_permission_policy
from wagtail.snippets.views.snippets import CreateView, SnippetViewSet
from wagtail.admin.panels import FieldPanel
from wagtail.admin import messages
from .models import PageView, PageViewCount, ReactionType, Reaction, MarkdownImportToken
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


class MarkdownImportTokenCreateView(CreateView):
    """在片段创建成功后仅提示一次明文 Token，数据库始终只保留哈希。"""

    def save_instance(self):
        self.form.instance.user = self.request.user
        self.form.instance.scopes = ["markdown_import"]
        self.plaintext_token = self.form.instance.issue_plaintext()
        return super().save_instance()

    def save_action(self):
        response = super().save_action()
        if not self.expects_json_response:
            messages.success(
                self.request,
                f"Markdown 导入 Token 创建成功：{self.plaintext_token}（已安全加密存储，后续可在列表操作菜单中随时点击【复制 Token】）",
            )
        return response


class MarkdownImportTokenSnippetViewSet(SnippetViewSet):
    model = MarkdownImportToken
    icon = "key"
    menu_label = "Markdown 导入 Token"
    add_to_admin_menu = False
    add_view_class = MarkdownImportTokenCreateView
    # 禁用 Wagtail 原生复制对象跳转动作，避免误导为复制 Token 密钥
    copy_view_enabled = False
    ordering = ("-created_at",)
    panels = [FieldPanel("name"), FieldPanel("expires_at")]
    list_display = [
        "name",
        "token_prefix",
        "expires_at",
        "revoked_at",
        "last_used_at",
        "created_at",
    ]
    search_fields = ("name", "token_prefix")
    list_filter = ("revoked_at", "expires_at")

    def get_urlpatterns(self):
        """扩展 SnippetViewSet 路由，增加异步复制与重新生成 Token 端点。"""
        urlpatterns = super().get_urlpatterns()
        conv = self.pk_path_converter
        return urlpatterns + [
            path(f"copy-token/<{conv}:pk>/", self.copy_token_view, name="copy_token"),
            path(f"rotate-token/<{conv}:pk>/", self.rotate_token_view, name="rotate_token"),
        ]

    def copy_token_view(self, request, pk):
        """异步解密并返回 Token 明文供前端直接写入剪贴板（免页面跳转）。

        权限要求：对当前 Token 实例具有查看权限。
        请求方式：仅支持 POST 请求（结合 CSRF 防护）。
        """
        if request.method != "POST":
            return JsonResponse({"success": False, "message": "仅支持 POST 请求"}, status=405)

        if not request.user.is_authenticated or not request.user.is_active:
            return JsonResponse({"success": False, "message": "未登录或登录已失效"}, status=401)

        instance = get_object_or_404(self.model, pk=pk)
        if not self.permission_policy.user_has_permission_for_instance(request.user, "view", instance):
            return JsonResponse({"success": False, "message": "无权限查看或复制该 Token"}, status=403)

        plaintext = instance.get_plaintext()
        if not plaintext:
            return JsonResponse(
                {
                    "success": False,
                    "can_rotate": True,
                    "message": "该 Token 创建于历史旧版本，未保存加密密文，无法直接复制。请点击【重新生成 Token】刷新并复制。",
                },
                status=400,
            )

        return JsonResponse(
            {
                "success": True,
                "token": plaintext,
                "message": "Token 复制成功",
            }
        )

    def rotate_token_view(self, request, pk):
        """重新生成并更新 Token 密钥（旧密钥失效），返回新明文供前端直接复制。

        权限要求：对当前 Token 实例具有变更权限。
        请求方式：仅支持 POST 请求（结合 CSRF 防护）。
        """
        if request.method != "POST":
            return JsonResponse({"success": False, "message": "仅支持 POST 请求"}, status=405)

        if not request.user.is_authenticated or not request.user.is_active:
            return JsonResponse({"success": False, "message": "未登录或登录已失效"}, status=401)

        instance = get_object_or_404(self.model, pk=pk)
        if not self.permission_policy.user_has_permission_for_instance(request.user, "change", instance):
            return JsonResponse({"success": False, "message": "无权限修改或重新生成该 Token"}, status=403)

        new_token = instance.rotate_token()
        return JsonResponse(
            {
                "success": True,
                "token": new_token,
                "token_prefix": instance.token_prefix,
                "message": f"Token 已成功重新生成（新前缀：{instance.token_prefix}），并已复制到剪贴板！",
            }
        )


class ReadOnlyPageViewPermissionPolicy(ModelPermissionPolicy):
    """Allow viewing PageView rows while blocking all mutations."""

    MUTATING_ACTIONS = {"add", "change", "delete"}

    def user_has_permission(self, user, action):
        if action in self.MUTATING_ACTIONS:
            return False
        return super().user_has_permission(user, action)


# Wagtail 8 要求自定义权限策略显式注册，保持审计记录只读约束。
register_permission_policy(
    PageView,
    ReadOnlyPageViewPermissionPolicy(PageView),
    exact_class=True,
)


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

    def get_queryset(self, request):
        return self.model.objects.select_related("page", "user")
