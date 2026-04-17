from django.contrib import admin
from .models import PageView, PageViewCount, ReactionType, Reaction

@admin.register(PageView)
class PageViewAdmin(admin.ModelAdmin):
    list_display = ('page', 'user', 'ip_address', 'is_unique', 'viewed_at')
    list_filter = ('is_unique', 'viewed_at')
    search_fields = ('page__title', 'ip_address', 'user_agent')
    date_hierarchy = 'viewed_at'
    
    def has_add_permission(self, request):
        # 不允许手动添加，应通过中间件自动生成
        return False

@admin.register(PageViewCount)
class PageViewCountAdmin(admin.ModelAdmin):
    list_display = ('page', 'date', 'count', 'unique_count')
    list_filter = ('date',)
    search_fields = ('page__title',)
    date_hierarchy = 'date'
    
    def has_add_permission(self, request):
        # 不允许手动添加，应通过数据同步生成
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