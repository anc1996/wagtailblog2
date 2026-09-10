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

# ReactionType 管理已由 Wagtail ReactionTypeSnippetViewSet 接管

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


from django import forms
from django.utils.html import format_html, escape
from django.utils.safestring import mark_safe

# 32 款精选高频博客互动反应图标（涵盖正面表达、爱心、火热、幽默、思考、惊叹与行动）
REACTION_ICON_CHOICES = [
    ("", "-- 请选择预设图标 --"),
    # 常用基础态度
    ("fa-thumbs-up", "👍 点赞 / 赞同 (fa-thumbs-up)"),
    ("fa-thumbs-down", "👎 点踩 / 反对 (fa-thumbs-down)"),
    ("fa-heart", "❤️ 喜欢 / 喜爱 (fa-heart)"),
    ("fa-star", "⭐ 收藏 / 推荐 (fa-star)"),
    ("fa-fire", "🔥 精彩 / 火热 (fa-fire)"),
    ("fa-lightbulb", "💡 思考 / 启发 (fa-lightbulb)"),
    ("fa-surprise", "😮 惊讶 / 震惊 (fa-surprise)"),
    ("fa-check", "✅ 赞同 / 确认 (fa-check)"),
    ("fa-bookmark", "🔖 存签 / 留底 (fa-bookmark)"),
    # 表情与情绪
    ("fa-face-smile", "😊 微笑 / 友善 (fa-face-smile)"),
    ("fa-face-laugh-squint", "😂 搞笑 / 爆笑 (fa-face-laugh-squint)"),
    ("fa-face-smile-beam", "😄 开心 / 灿烂 (fa-face-smile-beam)"),
    ("fa-face-sad-tear", "😢 难过 / 伤感 (fa-face-sad-tear)"),
    ("fa-face-flushed", "😳 害羞 / 汗颜 (fa-face-flushed)"),
    ("fa-circle-question", "❓ 疑问 / 探讨 (fa-circle-question)"),
    ("fa-eye", "👀 围观 / 关注 (fa-eye)"),
    # 鼓励、祝贺与荣誉
    ("fa-hands-clapping", "👏 鼓掌 / 喝彩 (fa-hands-clapping)"),
    ("fa-rocket", "🚀 火箭 / 起飞 (fa-rocket)"),
    ("fa-trophy", "🏆 冠军 / 优秀 (fa-trophy)"),
    ("fa-medal", "🏅 勋章 / 认可 (fa-medal)"),
    ("fa-champagne-glasses", "🥂 庆祝 / 干杯 (fa-champagne-glasses)"),
    ("fa-cake-candles", "🎂 祝福 / 生日 (fa-cake-candles)"),
    ("fa-bullseye", "🎯 击中 / 精准 (fa-bullseye)"),
    ("fa-crown", "👑 精华 / 卓越 (fa-crown)"),
    # 场景与极客生活
    ("fa-mug-hot", "☕ 咖啡 / 赞赏 (fa-mug-hot)"),
    ("fa-beer-mug-empty", "🍺 干杯 / 惬意 (fa-beer-mug-empty)"),
    ("fa-code", "💻 极客 / 代码 (fa-code)"),
    ("fa-pen-nib", "✍️ 好文 / 文笔 (fa-pen-nib)"),
    ("fa-book-open", "📖 研读 / 知识 (fa-book-open)"),
    ("fa-shield-halved", "🛡️ 稳健 / 严谨 (fa-shield-halved)"),
    ("fa-bell", "🔔 提醒 / 关注 (fa-bell)"),
    ("fa-hand-peace", "✌️ 胜利 / 友好 (fa-hand-peace)"),
    ("__custom__", "✏️ [手工输入其他 FontAwesome 类名...]"),
]


class ReactionIconSelectWidget(forms.Widget):
    """兼具语义下拉选择、自定义输入与实时图标渲染的复合 Widget。"""

    def render(self, name, value, attrs=None, renderer=None):
        current_val = value or "fa-thumbs-up"
        known_values = {k for k, _ in REACTION_ICON_CHOICES if k and k != "__custom__"}
        is_known = current_val in known_values

        # 构建选项 HTML
        options_html = []
        for val, label in REACTION_ICON_CHOICES:
            selected = ' selected="selected"' if (val == current_val if is_known else val == "__custom__") else ""
            options_html.append(f'<option value="{escape(val)}"{selected}>{escape(label)}</option>')

        # 初始预览图标类名（自动规范化）
        init_preview_class = current_val if any(current_val.startswith(p) for p in ["fa ", "fas ", "far ", "fab ", "fa-solid ", "fa-regular "]) else f"fa-solid {current_val}"
        safe_preview_class = "".join(c for c in init_preview_class if c.isalnum() or c in "-_ ")

        widget_id = (attrs or {}).get("id", f"id_{name}")
        select_id = f"{widget_id}_select"
        input_id = f"{widget_id}"
        preview_id = f"{widget_id}_preview_icon"

        input_display = "none" if is_known else "block"

        html = f"""
        <div class="reaction-icon-widget-container" style="display: flex; flex-direction: column; gap: 8px; max-width: 560px;">
            <div style="display: flex; align-items: center; gap: 12px;">
                <div style="flex: 1;">
                    <select id="{select_id}" class="reaction-preset-select" style="width: 100%; height: 40px; border-radius: 6px; border: 1px solid #ccc; padding: 4px 8px; font-size: 14px;">
                        {''.join(options_html)}
                    </select>
                </div>
                <div style="width: 44px; height: 44px; flex-shrink: 0; display: inline-flex; align-items: center; justify-content: center; border-radius: 8px; border: 1px solid rgba(0, 125, 126, 0.25); background: rgba(0, 125, 126, 0.06);">
                    <i id="{preview_id}" class="{safe_preview_class}" style="font-size: 1.5rem; color: #007d7e; transition: transform 0.15s ease;"></i>
                </div>
            </div>
            <div id="{widget_id}_custom_wrap" style="display: {input_display};">
                <input type="text" id="{input_id}" name="{name}" value="{escape(current_val)}" placeholder="输入自定义 Font Awesome 类名，例如 fa-solid fa-star" style="width: 100%; height: 38px; border-radius: 6px; border: 1px solid #007d7e; padding: 4px 10px; font-size: 14px; font-family: monospace;">
                <small style="color: #666; font-size: 12px; margin-top: 4px; display: block;">提示：可填入任何 Font Awesome 6 类名（如 <code>fa-solid fa-heart</code> 或简写 <code>fa-heart</code>）。</small>
            </div>
        </div>
        <script>
        (function() {{
            var select = document.getElementById("{select_id}");
            var input = document.getElementById("{input_id}");
            var customWrap = document.getElementById("{widget_id}_custom_wrap");
            var preview = document.getElementById("{preview_id}");

            function updatePreview(val) {{
                if (!val) return;
                var trimmed = val.trim();
                var families = ["fa", "fas", "far", "fab", "fa-solid", "fa-regular", "fa-brands"];
                var parts = trimmed.split(" ").filter(Boolean);
                var hasFamily = parts.some(function(p) {{ return families.indexOf(p) !== -1; }});
                var cls = hasFamily ? trimmed : "fa-solid " + trimmed;
                // 清洗非法字符防注入
                cls = cls.replace(/[^a-zA-Z0-9_ -]/g, "");
                preview.className = cls;
                preview.style.transform = "scale(1.2)";
                setTimeout(function() {{ preview.style.transform = "scale(1)"; }}, 150);
            }}

            if (select && input) {{
                select.addEventListener("change", function() {{
                    var val = this.value;
                    if (val === "__custom__") {{
                        customWrap.style.display = "block";
                        input.focus();
                    }} else if (val) {{
                        customWrap.style.display = "none";
                        input.value = val;
                        updatePreview(val);
                    }}
                }});

                input.addEventListener("input", function() {{
                    updatePreview(this.value);
                }});
            }}
        }})();
        </script>
        """
        return mark_safe(html)


class ReactionTypeSnippetViewSet(SnippetViewSet):
    """反应类型定制化 SnippetViewSet：提供列表图标预览、显示顺序排序与可视化图标选型。"""
    model = ReactionType
    icon = "smile"
    menu_label = "反应类型"
    add_to_admin_menu = False
    ordering = ("display_order", "id")
    search_fields = ("name", "icon")

    list_display = [
        "name",
        Column("icon_preview", label="图标预览"),
        Column("icon", label="CSS 类名"),
        Column("display_order", label="显示顺序", sort_key="display_order"),
    ]

    panels = [
        FieldPanel("name"),
        FieldPanel("icon", widget=ReactionIconSelectWidget),
        FieldPanel("display_order"),
    ]

    def icon_preview(self, instance):
        """在 Snippet 列表单元格中渲染图标视觉小卡片与标准色彩。"""
        css_class = instance.get_normalized_icon()
        safe_class = "".join(c for c in css_class if c.isalnum() or c in "-_ ")
        return format_html(
            '<div class="reaction-icon-cell" style="display: inline-flex; align-items: center; justify-content: center; width: 34px; height: 34px; border-radius: 8px; background: rgba(0, 125, 126, 0.08); border: 1px solid rgba(0, 125, 126, 0.2);">'
            '  <i class="{}" style="font-size: 1.25rem; color: #007d7e;"></i>'
            '</div>',
            safe_class,
        )
