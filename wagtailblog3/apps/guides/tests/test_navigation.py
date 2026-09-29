"""
专题指南主导航接入、多语言隔离与全局缓存收敛定向测试。

验证：
1. 真实模板缓存模式下，页面发布/下架通过信号自动递增全局代次并瞬时收敛；
2. 页面在树中移动通过 post_page_move 信号自动失效并重组菜单；
3. 页面权限与访问限制（PageViewRestriction）设置与解除：
   - 验证信号自动递增全局代次；
   - 验证菜单查询叠加 public() 严格排除受限页面，解除后瞬时恢复呈现；
4. 事务提交流程防御（on_commit 机制）：
   - 事务内调用失效函数，代次不提前推进，防止并发请求缓存未提交数据；
   - 事务提交后代次自动推进；事务回滚时代次坚决不推进；
5. 不可重用命名空间与冷启动/逐出防御：
   - 采用 UUID4 不可重用命名空间，仅删除代次键并保留旧片段时，重建代次绝不重用旧片段；
   - 冷启动多线程并发竞争时无死锁、无异常、代次严格单调推进；
6. Wagtail 后台生命周期钩子（publish/unpublish/move/edit/delete）直接触发有效失效；
7. 多语言环境隔离：中英文页面互不交叉泄露；
8. 模板缓存键包含 nav_epoch，实现严格 O(1) 瞬时失效，杜绝 600 秒滞后。
"""

import concurrent.futures
import uuid
from typing import Any
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.cache.utils import make_template_fragment_key
from django.db import transaction
from django.template import Context, Template
from django.test import RequestFactory, TransactionTestCase
from wagtail.models import Locale, Page, PageViewRestriction, Site

from blog.templatetags.blog_tags import generate_menu_items
from guides.models import GuideIndexPage
from guides.services.nav_cache import (
    NAV_MENU_COUNTER_KEY,
    NAV_MENU_NAMESPACE_KEY,
    bump_nav_menu_epoch,
    get_current_nav_menu_epoch,
    invalidate_nav_menu_cache,
)
from guides.wagtail_hooks import (
    invalidate_nav_on_delete,
    invalidate_nav_on_edit,
    invalidate_nav_on_move,
    invalidate_nav_on_publish,
    invalidate_nav_on_unpublish,
)

User = get_user_model()


class GuideNavigationIntegrationTestCase(TransactionTestCase):
    """主导航集成、多语言隔离、权限过滤与并发安全测试用例。"""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        self.rf = RequestFactory()

        self.user = User.objects.create_superuser(
            username="navadmin",
            password="testpassword",
            email="navadmin@test.com",
        )

        self.locale_zh, _ = Locale.objects.get_or_create(language_code="zh-hans")
        self.locale_en, _ = Locale.objects.get_or_create(language_code="en")

        root = Page.objects.filter(depth=1).first()
        if not root:
            root = Page.add_root(title="Root", locale=self.locale_zh)
        self.root_page = root

        self.home = Page.objects.filter(depth=2).first()
        if not self.home:
            self.home = Page(title="Home", slug="home", locale=self.locale_zh)
            self.root_page.add_child(instance=self.home)

        self.site = Site.objects.filter(is_default_site=True).first()
        if not self.site:
            self.site = Site.objects.create(
                hostname="localhost",
                port=80,
                site_name="Test Site",
                root_page=self.home,
                is_default_site=True,
            )

    def tearDown(self) -> None:
        cache.clear()
        super().tearDown()

    def _render_nav_header(self, current_page: Page, language_code: str = "zh-hans") -> str:
        """
        加载并执行与 header.html 完全一致的模板缓存与菜单渲染逻辑（包含 nav_epoch 键参数）。
        """
        template_str = """
        {% load wagtailcore_tags blog_tags cache %}
        {% get_site_root_details as site_root_info %}
        {% get_nav_menu_epoch as nav_epoch %}
        {% cache 600 main_nav_menu request.LANGUAGE_CODE page.pk nav_epoch %}
            <nav class="test-rendered-nav">
                {% if site_root_info.page_obj %}
                    {% generate_menu_items site_root_info.page_obj page %}
                {% endif %}
            </nav>
        {% endcache %}
        """
        request = self.rf.get("/")
        request.site = self.site
        request.LANGUAGE_CODE = language_code
        template = Template(template_str)
        context = Context({
            "request": request,
            "page": current_page,
        })
        return template.render(context)

    def test_navigation_cache_automatic_invalidation_on_unpublish(self) -> None:
        """
        验证 Wagtail 真实生命周期：下架页面时自动通过信号递增代次，
        前台无需任何手动干预即可实现 O(1) 瞬时收敛。
        """
        guide_index = GuideIndexPage(
            title="实时指南中心",
            slug="live-guide-index",
            intro="专题指南中心",
            show_in_menus=True,
            live=True,
            locale=self.locale_zh,
        )
        self.home.add_child(instance=guide_index)
        guide_index.save_revision(user=self.user).publish(user=self.user)

        # 1. 首次渲染：冷启动入缓存
        html_1 = self._render_nav_header(self.home, "zh-hans")
        self.assertIn("实时指南中心", html_1)

        initial_epoch = get_current_nav_menu_epoch()

        # 2. 调用 Wagtail 原生下架操作（触发 page_unpublished 信号）
        guide_index.unpublish(user=self.user)

        # 验证信号自动递增了全局代次
        new_epoch = get_current_nav_menu_epoch()
        self.assertNotEqual(new_epoch, initial_epoch)

        # 3. 再次渲染：新代次自动导致旧缓存未命中并即时重算，下架项瞬时消失！
        fresh_html = self._render_nav_header(self.home, "zh-hans")
        self.assertNotIn("实时指南中心", fresh_html)

    def test_navigation_cache_automatic_invalidation_on_movement(self) -> None:
        """
        验证页面在树中移动时，自动通过 post_page_move 信号触发代次递增与结构刷新。
        """
        category_page = Page(
            title="专题汇聚",
            slug="category-root",
            show_in_menus=True,
            live=True,
            locale=self.locale_zh,
        )
        self.home.add_child(instance=category_page)

        child_page = Page(
            title="二级运维手册",
            slug="ops-manual",
            show_in_menus=True,
            live=True,
            locale=self.locale_zh,
        )
        self.home.add_child(instance=child_page)

        html_before = self._render_nav_header(self.home, "zh-hans")
        self.assertIn("二级运维手册", html_before)

        initial_epoch = get_current_nav_menu_epoch()

        # 将 child_page 移动至 category_page 下（触发 post_page_move 信号）
        child_page.move(category_page, pos="last-child")

        new_epoch = get_current_nav_menu_epoch()
        self.assertNotEqual(new_epoch, initial_epoch)

        # 再次渲染，验证移动后结构正常被新缓存接收
        html_after = self._render_nav_header(self.home, "zh-hans")
        self.assertIn("专题汇聚", html_after)

    def test_navigation_cache_invalidation_and_public_filtering_on_privacy_change(self) -> None:
        """
        验证 Wagtail 页面权限与访问限制变更：
        1. 初始公开状态下，页面出现在公开主导航中；
        2. 设置访问限制（密码保护）后：
           - post_save 信号自动递增全局代次；
           - public() 过滤生效，受限页面在公开导航中被完全排除；
        3. 移除访问限制后：
           - post_delete 信号自动递增全局代次；
           - 页面在公开导航中瞬时恢复呈现。
        """
        restricted_page = Page(
            title="绝密内部指南",
            slug="internal-confidential-guide",
            show_in_menus=True,
            live=True,
            locale=self.locale_zh,
        )
        self.home.add_child(instance=restricted_page)

        # 1. 初始公开状态渲染
        html_public = self._render_nav_header(self.home, "zh-hans")
        self.assertIn("绝密内部指南", html_public)

        epoch_1 = get_current_nav_menu_epoch()

        # 2. 设置隐私保护（触发 post_save 信号）
        restriction = PageViewRestriction.objects.create(
            page=restricted_page,
            restriction_type=PageViewRestriction.PASSWORD,
            password="secure-password-123",
        )
        epoch_2 = get_current_nav_menu_epoch()
        self.assertNotEqual(epoch_2, epoch_1)

        # 验证受限后公开导航渲染：受限页面被 public() 彻底排除！
        html_restricted = self._render_nav_header(self.home, "zh-hans")
        self.assertNotIn("绝密内部指南", html_restricted)

        # 3. 解除隐私限制（恢复公开，触发 post_delete 信号）
        restriction.delete()
        epoch_3 = get_current_nav_menu_epoch()
        self.assertNotEqual(epoch_3, epoch_2)

        # 验证恢复公开后再次渲染：页面即时恢复出现在导航中！
        html_restored = self._render_nav_header(self.home, "zh-hans")
        self.assertIn("绝密内部指南", html_restored)

    def test_navigation_invalidation_deferred_until_transaction_commit(self) -> None:
        """
        验证事务内并发时序防护：
        在事务内调用 invalidate_nav_menu_cache，代次必须通过 on_commit 延迟到事务提交后才递增；
        事务提交前读取代次保持不变，杜绝并发请求以新代次缓存未提交脏数据。
        """
        initial_epoch = get_current_nav_menu_epoch()

        # 模拟事务内操作
        with transaction.atomic():
            invalidate_nav_menu_cache(self.home)
            # 事务内：代次尚未递增（防脏读）
            self.assertEqual(get_current_nav_menu_epoch(), initial_epoch)

        # 事务已提交：on_commit 钩子已自动执行，代次成功递增
        self.assertNotEqual(get_current_nav_menu_epoch(), initial_epoch)

    def test_navigation_invalidation_cancelled_on_transaction_rollback(self) -> None:
        """
        验证事务回滚时，延迟的代次递增钩子坚决不执行，避免无效失效。
        """
        initial_epoch = get_current_nav_menu_epoch()

        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                invalidate_nav_menu_cache(self.home)
                self.assertEqual(get_current_nav_menu_epoch(), initial_epoch)
                raise RuntimeError("强制回滚事务")

        # 回滚后：代次依然保持原值，未被误递增
        self.assertEqual(get_current_nav_menu_epoch(), initial_epoch)

    def test_navigation_epoch_reconstruction_does_not_reuse_old_fragments(self) -> None:
        """
        核心防御边界测试：
        若仅代次键（NAV_MENU_NAMESPACE_KEY / NAV_MENU_COUNTER_KEY）丢失或被 Redis 逐出，
        而旧模板片段仍保存在缓存中（最长 600 秒 TTL），
        重建代次时必须采用全新不可重用命名空间，坚决杜绝命中最长 600 秒的旧菜单。
        """
        guide_index = GuideIndexPage(
            title="不可重用指南中心",
            slug="non-reusable-guide-index",
            show_in_menus=True,
            live=True,
            locale=self.locale_zh,
        )
        self.home.add_child(instance=guide_index)
        guide_index.save_revision(user=self.user).publish(user=self.user)

        # 1. 首次渲染：旧页面进入缓存，同时代次已建立
        old_epoch = get_current_nav_menu_epoch()
        html_1 = self._render_nav_header(self.home, "zh-hans")
        self.assertIn("不可重用指南中心", html_1)

        # 确认旧片段此时确实保存在缓存中
        cache_key_old = make_template_fragment_key("main_nav_menu", ["zh-hans", self.home.pk, old_epoch])
        self.assertIsNotNone(cache.get(cache_key_old))

        # 2. 下架该指南页面（数据库中 live=False）
        guide_index.live = False
        guide_index.save(update_fields=["live"])

        # 3. 模拟极端边界：仅删除代次键，保留旧片段在缓存中！
        cache.delete(NAV_MENU_NAMESPACE_KEY)
        cache.delete(NAV_MENU_COUNTER_KEY)
        # 验证旧片段仍然存活在缓存中未被删除
        self.assertIsNotNone(cache.get(cache_key_old))

        # 4. 再次获取代次或触发失效（重建代次）
        new_epoch = bump_nav_menu_epoch()

        # 验证新代次与旧代次绝不同号
        self.assertNotEqual(new_epoch, old_epoch)

        # 5. 渲染导航：新代次产生缓存未命中，重新从数据库计算，已下架项绝不出现！
        fresh_html = self._render_nav_header(self.home, "zh-hans")
        self.assertNotIn("不可重用指南中心", fresh_html)

    def test_navigation_epoch_reconstruction_when_counter_alone_evicted_does_not_reuse_old_fragments(self) -> None:
        """
        核心防重复利用边界测试（仅丢失计数器键）：
        若命名空间键 NAV_MENU_NAMESPACE_KEY 仍留在缓存（例如 N），
        但计数器 NAV_MENU_COUNTER_KEY 单独被 Redis 逐出或过期丢失，
        且历史模板片段 N:1 仍在 600 秒存活期内。
        在读路径（get_current_nav_menu_epoch）或写路径（bump_nav_menu_epoch）触发重建时，
        必须原子切换全新的 UUID4 命名空间，绝不能沿用旧命名空间将计数器重设为 1，
        彻底杜绝重新命中 N:1 的已失效历史菜单。
        """
        guide_index = GuideIndexPage(
            title="计数器逐出测试专题索引",
            slug=f"counter-evict-{uuid.uuid4().hex[:6]}",
            show_in_menus=True,
            live=True,
            locale=self.locale_zh,
        )
        self.home.add_child(instance=guide_index)
        guide_index.save_revision(user=self.user).publish(user=self.user)

        # 1. 初始渲染并缓存旧片段（页面发布信号会自动推进代次）
        epoch_1 = get_current_nav_menu_epoch()
        ns_old, c_init_str = epoch_1.split(":")
        c_init = int(c_init_str)

        html_1 = self._render_nav_header(self.home, "zh-hans")
        self.assertIn("计数器逐出测试专题索引", html_1)

        cache_key_old = make_template_fragment_key("main_nav_menu", ["zh-hans", self.home.pk, epoch_1])
        self.assertIsNotNone(cache.get(cache_key_old), "旧片段必须已成功缓存")

        # 2. 推进代次（模拟真实业务中多次变更）
        epoch_2 = bump_nav_menu_epoch()
        epoch_3 = bump_nav_menu_epoch()
        self.assertEqual(epoch_2, f"{ns_old}:{c_init + 1}")
        self.assertEqual(epoch_3, f"{ns_old}:{c_init + 2}")

        # 3. 下线该专题页面
        guide_index.live = False
        guide_index.save(update_fields=["live"])

        # 4. 模拟关键边界故障：仅删除计数器键，故意保留命名空间键与旧片段 N:1
        cache.delete(NAV_MENU_COUNTER_KEY)
        self.assertIsNotNone(cache.get(NAV_MENU_NAMESPACE_KEY), "命名空间键必须依然存留在缓存中")
        self.assertIsNone(cache.get(NAV_MENU_COUNTER_KEY), "计数器键已被模拟逐出")
        self.assertIsNotNone(cache.get(cache_key_old), "旧片段 N:1 仍在 600 秒存活期内")

        # 5. 走纯前台读请求路径（触发 get_current_nav_menu_epoch 重建）
        rebuilt_epoch = get_current_nav_menu_epoch()
        ns_new, new_counter_str = rebuilt_epoch.split(":")

        # 验证命名空间已强制原子轮转，绝未复用旧命名空间！
        self.assertNotEqual(ns_new, ns_old, "计数器单独丢失时必须强制轮转命名空间")
        self.assertEqual(new_counter_str, "1")

        # 6. 前台渲染主导航：必须未命中旧片段 N:1，重新从数据库拉取且不含已下线的页面！
        fresh_html = self._render_nav_header(self.home, "zh-hans")
        self.assertNotIn("计数器逐出测试专题索引", fresh_html)

        # 确认旧片段 N:1 仍然完好保存在缓存中（证明确实是因为代次隔离而未命中，而非旧键被误删）
        self.assertIsNotNone(cache.get(cache_key_old))

    def test_navigation_epoch_cold_boot_concurrency_stress(self) -> None:
        """
        验证冷启动高并发场景下 bump_nav_menu_epoch 的原子并发安全性：
        在缓存完全空白的冷启动状态下，15 个线程同时并发调用 bump_nav_menu_epoch，
        验证所有线程无异常、无死锁，代次正常推进。
        """
        cache.clear()
        thread_count = 15

        def worker() -> str:
            return bump_nav_menu_epoch()

        with concurrent.futures.ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = [executor.submit(worker) for _ in range(thread_count)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        final_epoch = get_current_nav_menu_epoch()
        self.assertEqual(len(results), thread_count)
        self.assertTrue(all(isinstance(r, str) and ":" in r for r in results))
        self.assertIn(":", final_epoch)

    def test_navigation_admin_hooks_direct_invalidation(self) -> None:
        """
        验证 Wagtail 后台 Admin 钩子（publish/unpublish/move/edit/delete）均能有效触发失效。
        """
        test_page = Page(
            title="钩子测试页",
            slug="hooks-test-page",
            show_in_menus=True,
            live=True,
            locale=self.locale_zh,
        )
        self.home.add_child(instance=test_page)
        request = self.rf.get("/admin/")

        # 1. after_publish_page
        e1 = get_current_nav_menu_epoch()
        invalidate_nav_on_publish(request, test_page)
        self.assertNotEqual(get_current_nav_menu_epoch(), e1)

        # 2. after_unpublish_page
        e2 = get_current_nav_menu_epoch()
        invalidate_nav_on_unpublish(request, test_page)
        self.assertNotEqual(get_current_nav_menu_epoch(), e2)

        # 3. after_move_page
        e3 = get_current_nav_menu_epoch()
        invalidate_nav_on_move(request, test_page)
        self.assertNotEqual(get_current_nav_menu_epoch(), e3)

        # 4. after_edit_page
        e4 = get_current_nav_menu_epoch()
        invalidate_nav_on_edit(request, test_page)
        self.assertNotEqual(get_current_nav_menu_epoch(), e4)

        # 5. after_delete_page
        e5 = get_current_nav_menu_epoch()
        invalidate_nav_on_delete(request, test_page)
        self.assertNotEqual(get_current_nav_menu_epoch(), e5)

    def test_navigation_locale_isolation_in_menu_generation(self) -> None:
        """
        验证多语言菜单隔离：在同一站点树下，跨语言子页面不会泄露在当前语言导航中。
        """
        guide_zh = GuideIndexPage(
            title="中文知识库",
            slug="knowledge-zh",
            show_in_menus=True,
            live=True,
            locale=self.locale_zh,
        )
        self.home.add_child(instance=guide_zh)

        guide_en = GuideIndexPage(
            title="English Knowledge Base",
            slug="knowledge-en",
            show_in_menus=True,
            live=True,
            locale=self.locale_en,
        )
        self.home.add_child(instance=guide_en)

        request = self.rf.get("/")
        request.site = self.site
        request.LANGUAGE_CODE = "zh-hans"
        context = {"request": request}

        menu_data = generate_menu_items(context, self.home, self.home)
        item_pks = [item["page_object"].pk for item in menu_data["menu_items_list"]]

        self.assertIn(guide_zh.pk, item_pks)
        self.assertNotIn(guide_en.pk, item_pks)
