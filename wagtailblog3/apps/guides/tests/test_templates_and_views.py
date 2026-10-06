from unittest import mock
"""
专题指南模板渲染与主导航接入定向测试。

验证：
1. GuideIndexPage 模板渲染：标题、导语、主题标签、难度筛选、列表卡片及空状态；
2. GuidePage 模板渲染：面包屑、摘要、元数据标签、TOC 目录定位、七类块渲染；
3. 主导航接入：通过 generate_menu_items 自动拾取 GuideIndexPage，无硬编码 URL；
4. 移动端与无障碍基础属性（语义化标签与 aria 属性）。
"""

import datetime
from django.contrib.auth import get_user_model
from django.conf import settings
from django.template import Context, Template

TEST_STORAGES = {
    **settings.STORAGES,
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}
from django.test import Client, RequestFactory, TestCase, override_settings
from wagtail.models import Page, Site

import uuid


def _get_test_mongo_db_name() -> str:
    """获取测试 MongoDB 数据库名：统一读取 settings.MONGO_DB['NAME']，兼容开发环境回退至 MONGO_DB_NAME。"""
    from external_content.store import get_mongo_settings

    try:
        mongo_cfg = get_mongo_settings()
        if isinstance(mongo_cfg, dict) and mongo_cfg.get("NAME"):
            return str(mongo_cfg["NAME"])
    except Exception:
        pass
    # 兼容旧版开发环境：回退至 MONGO_DB_NAME 或默认值
    return getattr(settings, "MONGO_DB_NAME", "wagtailblog_test")


from external_content.store import (
    ContentVersionStore,
    ensure_indexes,
    get_mongo_client,
    reset_active_collection_name,
    set_active_collection_name,
)
from guides.models import (
    GuideIndexPage,
    GuidePage,
    GuideRelatedPage,
    GuideTopic,
)

User = get_user_model()


@override_settings(STORAGES=TEST_STORAGES)
class GuideTemplatesAndViewsTestCase(TestCase):
    """专题指南模板与视图集成测试用例。"""

    def setUp(self) -> None:
        self.client = Client()
        self.factory = RequestFactory()
        self.coll_name = f"test_guides_tmpl_{uuid.uuid4().hex[:8]}"
        self.mongo_client = get_mongo_client()
        db_name = _get_test_mongo_db_name()
        self.collection = self.mongo_client[db_name][self.coll_name]
        ensure_indexes(self.collection)
        set_active_collection_name(self.coll_name)
        self.store = ContentVersionStore(collection=self.collection)

        self.user = User.objects.create_superuser(
            username="guidetemplateadmin",
            password="testpassword",
            email="templateadmin@test.com",
        )

        from wagtail.models import Locale
        self.locale, _ = Locale.objects.get_or_create(language_code="zh-hans")
        root = Page.objects.filter(depth=1).first()
        if not root:
            root = Page.add_root(title="Root", locale=self.locale)
        self.root_page = root

        self.home = Page.objects.filter(depth=2).first()
        if not self.home:
            self.home = Page(title="Home", slug="home", locale=self.locale)
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

        self.topic = GuideTopic.objects.create(
            name="安全审计",
            slug="security-audit",
            description="漏洞排查与防御体系",
        )

        self.index_page = GuideIndexPage(
            title="安全与合规指南",
            slug="security-guides",
            intro="企业级系统安全加固与审计操作指南。",
            show_in_menus=True,
        )
        self.home.add_child(instance=self.index_page)
        self.index_page.save_revision().publish()

    def tearDown(self) -> None:
        reset_active_collection_name()
        try:
            self.collection.drop()
        except Exception:
            pass

    def test_guide_index_page_rendering(self) -> None:
        """测试 GuideIndexPage 渲染：包含标题、导语、筛选按钮与指南卡片。"""
        guide = GuidePage(
            title="Linux 账号安全加固指南",
            slug="linux-account-hardening",
            summary="禁用空密码账号并配置双因子认证。",
            topic=self.topic,
            difficulty="intermediate",
            last_reviewed_at=datetime.date(2026, 9, 25),
            body=[
                {
                    "type": "step",
                    "value": {
                        "title": "检查系统空密码账号",
                        "description": "<p>使用 awk 筛选 /etc/shadow 确认锁定状态。</p>",
                        "image": None,
                    },
                }
            ],
        )
        self.index_page.add_child(instance=guide)
        guide._test_content_store = self.store
        rev = guide.save_revision(user=self.user)
        guide.publish(rev, user=self.user)

        response = self.client.get(self.index_page.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "安全与合规指南")
        self.assertContains(response, "企业级系统安全加固与审计操作指南")
        self.assertContains(response, "Linux 账号安全加固指南")
        self.assertContains(response, "安全审计")
        self.assertContains(response, "进阶")
        self.assertContains(response, "2026-09-25")

    def test_guide_detail_page_blocks_rendering(self) -> None:
        """测试 GuidePage 渲染：包含面包屑、TOC、全部七类块与相关阅读。"""
        related_target = GuidePage(
            title="密码复杂度策略指南",
            slug="password-complexity-guide",
            summary="PAM 密码强度配置规范。",
            topic=self.topic,
            difficulty="beginner",
            body=[{"type": "step", "value": {"title": "S1", "description": "<p>D</p>", "image": None}}],
        )
        self.index_page.add_child(instance=related_target)
        related_target._test_content_store = self.store
        r_rev = related_target.save_revision(user=self.user)
        related_target.publish(r_rev, user=self.user)

        full_guide = GuidePage(
            title="生产服务器安全基线加固",
            slug="production-server-baseline",
            summary="端到端服务器基线防线建设。",
            topic=self.topic,
            difficulty="advanced",
            last_reviewed_at=datetime.date(2026, 9, 28),
            body=[
                {
                    "type": "lead",
                    "value": "<p>本篇指南旨在协助系统管理员构建符合等保要求的生产基线。</p>",
                },
                {
                    "type": "step",
                    "value": {
                        "title": "配置 SSH 密钥登录并禁用密码",
                        "description": "<p>修改 /etc/ssh/sshd_config 确保 PasswordAuthentication 为 no。</p>",
                        "image": None,
                    },
                },
                {
                    "type": "code",
                    "value": {
                        "language": "bash",
                        "code": "sed -i 's/^#PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config",
                        "caption": "禁用密码登录命令",
                    },
                },
                {
                    "type": "tip",
                    "value": {
                        "tip_type": "warning",
                        "content": "<p>执行前请确保已开启至少一个保持连接的终端会话以防失联！</p>",
                    },
                },
                {
                    "type": "faq",
                    "value": {
                        "question": "如果重启 sshd 报错无法启动怎么办？",
                        "answer": "<p>请立即使用 sshd -t 命令检查配置文件语法。</p>",
                    },
                },
                {
                    "type": "reference",
                    "value": {
                        "title": "OpenSSH 安全配置规范",
                        "url": "https://www.openssh.com/manual.html",
                        "source": "OpenSSH 官方",
                    },
                },
                {
                    "type": "markdown_block",
                    "value": "### 自动化核查脚本\n\n```bash\ncurl -sSL https://security.local/check.sh | bash\n```",
                },
            ],
        )
        self.index_page.add_child(instance=full_guide)
        GuideRelatedPage.objects.create(page=full_guide, related_page=related_target, sort_order=0)
        full_guide._test_content_store = self.store
        rev = full_guide.save_revision(user=self.user)
        full_guide.publish(rev, user=self.user)

        response = self.client.get(full_guide.url)
        self.assertEqual(response.status_code, 200)

        # 验证面包屑与元数据
        self.assertContains(response, "专题指南")
        self.assertContains(response, "生产服务器安全基线加固")
        self.assertContains(response, "安全审计")
        self.assertContains(response, "深入")
        self.assertContains(response, "2026-09-28")

        # 验证各块内容输出
        self.assertContains(response, "本篇指南旨在协助系统管理员构建符合等保要求的生产基线")
        self.assertContains(response, "配置 SSH 密钥登录并禁用密码")
        self.assertContains(response, "PasswordAuthentication no")
        self.assertContains(response, "执行前请确保已开启至少一个保持连接的终端会话")
        self.assertContains(response, "如果重启 sshd 报错无法启动怎么办")
        self.assertContains(response, "OpenSSH 安全配置规范")
        self.assertContains(response, "自动化核查脚本")

        # 验证相关阅读输出
        self.assertContains(response, "密码复杂度策略指南")

    def test_menu_tag_integration_without_hardcoded_url(self) -> None:
        """测试主导航 generate_menu_items 标签自动将公开的 GuideIndexPage 纳入菜单。"""
        template_code = """
        {% load blog_tags %}
        {% generate_menu_items home current_page %}
        """
        template = Template(template_code)
        rendered = template.render(Context({"home": self.home, "current_page": self.home}))

        # 验证生成的菜单 HTML 中包含 GuideIndexPage 且使用了其原生 pageurl / slug
        self.assertIn(self.index_page.title, rendered)
        self.assertIn(self.index_page.url, rendered)
    def test_guide_index_page_zero_mongo_interaction(self) -> None:
        """验证前台访问 GuideIndexPage 时纯走 MySQL，全链路零 MongoDB 读写及客户端获取交互。"""
        # 创建已发布的指南
        guide = GuidePage(
            title="MySQL 专用测试指南",
            slug="mysql-only-test-guide",
            summary="测试索引页零 Mongo 读取",
            topic=self.topic,
            difficulty="beginner",
            body=[{"type": "step", "value": {"title": "S1", "description": "<p>D</p>", "image": None}}],
        )
        self.index_page.add_child(instance=guide)
        guide._test_content_store = self.store
        rev = guide.save_revision(user=self.user)
        guide.publish(rev, user=self.user)

        # 在最高层 Store 入口与底层 PyMongo 入口实施全面监控
        with mock.patch("external_content.store.get_mongo_client") as mock_get_client,              mock.patch("external_content.store.get_collection") as mock_get_coll,              mock.patch("external_content.store.ContentVersionStore.get_version") as mock_get_ver,              mock.patch("pymongo.collection.Collection.find") as mock_find,              mock.patch("pymongo.collection.Collection.find_one") as mock_find_one,              mock.patch("pymongo.collection.Collection.insert_one") as mock_insert,              mock.patch("pymongo.collection.Collection.update_one") as mock_update,              mock.patch("pymongo.collection.Collection.delete_one") as mock_delete:
            response = self.client.get(self.index_page.url)
            self.assertEqual(response.status_code, 200)
            mock_get_client.assert_not_called()
            mock_get_coll.assert_not_called()
            mock_get_ver.assert_not_called()
            mock_find.assert_not_called()
            mock_find_one.assert_not_called()
            mock_insert.assert_not_called()
            mock_update.assert_not_called()
            mock_delete.assert_not_called()

    def test_guide_detail_step_anchor_and_toc_alignment(self) -> None:
        """验证 TOC 中的步骤 ID 与正文中的步骤锚点容器 100% 精确对应且存在。"""
        guide = GuidePage(
            title="锚点定位测试指南",
            slug="anchor-alignment-guide",
            summary="验证 TOC 与正文锚点闭环",
            topic=self.topic,
            difficulty="intermediate",
            body=[
                {
                    "type": "lead",
                    "value": "<p>导读内容</p>",
                },
                {
                    "type": "step",
                    "value": {
                        "title": "第一步：部署集群",
                        "description": "<p>初始化控制节点</p>",
                        "image": None,
                    },
                },
                {
                    "type": "code",
                    "value": {"language": "bash", "code": "kubeadm init", "caption": ""},
                },
                {
                    "type": "step",
                    "value": {
                        "title": "第二步：加入工作节点",
                        "description": "<p>运行 kubeadm join</p>",
                        "image": None,
                    },
                },
            ],
        )
        self.index_page.add_child(instance=guide)
        guide._test_content_store = self.store
        rev = guide.save_revision(user=self.user)
        guide.publish(rev, user=self.user)

        response = self.client.get(guide.url)
        self.assertEqual(response.status_code, 200)

        toc = response.context["toc"]
        self.assertEqual(len(toc), 2)
        html_str = response.content.decode("utf-8")

        for item in toc:
            target_id = item["id"]
            # 1. 验证 TOC 中存在对应的跳转链接 href="#<id>"
            self.assertIn(f'href="#{target_id}"', html_str)
            # 2. 验证正文中存在对应的锚点容器 id="<id>"
            self.assertIn(f'id="{target_id}"', html_str)

    def test_visible_related_pages_privacy_and_site_locale_filtering(self) -> None:
        """验证相关阅读独立过滤未发布、祖先限制、跨站点与跨 Locale 页面。"""
        from wagtail.models import Locale, PageViewRestriction

        # 1. 合法正常页面 (同一站点、同一语言、公开)
        page_normal = GuidePage(
            title="正常同站公开指南",
            slug="normal-public-guide",
            summary="合法推荐项",
            topic=self.topic,
            difficulty="beginner",
            body=[{"type": "step", "value": {"title": "S", "description": "<p>D</p>", "image": None}}],
        )
        self.index_page.add_child(instance=page_normal)
        page_normal._test_content_store = self.store
        rev_normal = page_normal.save_revision(user=self.user)
        page_normal.publish(rev_normal, user=self.user)

        # 2. 未发布页面 (live=False)
        page_unpub = GuidePage(
            title="未发布草稿指南",
            slug="unpub-draft-guide",
            summary="草稿项",
            topic=self.topic,
            difficulty="beginner",
            body=[{"type": "step", "value": {"title": "S", "description": "<p>D</p>", "image": None}}],
            live=False,
            has_unpublished_changes=True,
        )
        self.index_page.add_child(instance=page_unpub)

        # 3. 祖先存在隐私限制的页面
        restricted_parent = Page(title="受限父页面", slug="restricted-parent", locale=self.locale)
        self.home.add_child(instance=restricted_parent)
        PageViewRestriction.objects.create(page=restricted_parent, restriction_type=PageViewRestriction.LOGIN)

        page_restricted_descendant = GuidePage(
            title="受限祖先下的指南",
            slug="restricted-descendant-guide",
            summary="隐私项",
            topic=self.topic,
            difficulty="beginner",
            body=[{"type": "step", "value": {"title": "S", "description": "<p>D</p>", "image": None}}],
        )
        restricted_parent.add_child(instance=page_restricted_descendant)
        page_restricted_descendant._test_content_store = self.store
        rev_res = page_restricted_descendant.save_revision(user=self.user)
        page_restricted_descendant.publish(rev_res, user=self.user)

        # 4. 独立跨 Site 页面（语言相同为 zh-hans，但属于第二个独立 Site 树）
        other_site_root = Page(title="独立站点根页", slug="other-site-root", locale=self.locale)
        self.root_page.add_child(instance=other_site_root)
        Site.objects.create(
            hostname="independent-test-site.local",
            port=80,
            site_name="Independent Test Site",
            root_page=other_site_root,
            is_default_site=False,
        )
        page_other_site = GuidePage(
            title="独立站点技术指南",
            slug="other-site-guide",
            summary="同语言但属于独立站点树的页面",
            topic=self.topic,
            difficulty="beginner",
            body=[{"type": "step", "value": {"title": "S", "description": "<p>D</p>", "image": None}}],
            locale=self.locale,
        )
        other_site_root.add_child(instance=page_other_site)
        page_other_site._test_content_store = self.store
        rev_other_site = page_other_site.save_revision(user=self.user)
        page_other_site.publish(rev_other_site, user=self.user)

        # 5. 独立跨 Locale 页面（属于当前主站点树 self.home 之下，但语言为 en）
        en_locale, _ = Locale.objects.get_or_create(language_code="en")
        page_diff_locale = GuidePage(
            title="English Guide In Current Site",
            slug="en-guide-in-current-site",
            summary="Diff Locale in same site tree",
            topic=self.topic,
            difficulty="beginner",
            body=[{"type": "step", "value": {"title": "S", "description": "<p>D</p>", "image": None}}],
            locale=en_locale,
        )
        self.index_page.add_child(instance=page_diff_locale)
        page_diff_locale._test_content_store = self.store
        rev_en = page_diff_locale.save_revision(user=self.user)
        page_diff_locale.publish(rev_en, user=self.user)

        # 6. 主测试指南，关联上述全部 5 个测试候选页面
        main_guide = GuidePage(
            title="关联推荐综合测试指南",
            slug="related-filter-test-guide",
            summary="测试全面可见性过滤",
            topic=self.topic,
            difficulty="beginner",
            body=[{"type": "step", "value": {"title": "S", "description": "<p>D</p>", "image": None}}],
        )
        self.index_page.add_child(instance=main_guide)
        GuideRelatedPage.objects.create(page=main_guide, related_page=page_normal, sort_order=0)
        GuideRelatedPage.objects.create(page=main_guide, related_page=page_unpub, sort_order=1)
        GuideRelatedPage.objects.create(page=main_guide, related_page=page_restricted_descendant, sort_order=2)
        GuideRelatedPage.objects.create(page=main_guide, related_page=page_other_site, sort_order=3)
        GuideRelatedPage.objects.create(page=main_guide, related_page=page_diff_locale, sort_order=4)

        main_guide._test_content_store = self.store
        rev_main = main_guide.save_revision(user=self.user)
        main_guide.publish(rev_main, user=self.user)

        response = self.client.get(main_guide.url)
        self.assertEqual(response.status_code, 200)

        visible_pages = response.context["visible_related_pages"]
        # 严格验证：只有合法公开、同站、同语言的 page_normal 被保留，其余 4 个全部被过滤排除
        self.assertEqual(len(visible_pages), 1)
        self.assertEqual(visible_pages[0].pk, page_normal.pk)

        # 验证 HTML 中不含受限、草稿、独立站点或异种语言标题
        self.assertContains(response, page_normal.title)
        self.assertNotContains(response, page_unpub.title)
        self.assertNotContains(response, page_restricted_descendant.title)
        self.assertNotContains(response, page_other_site.title)
        self.assertNotContains(response, page_diff_locale.title)

    def test_guide_index_page_locale_isolation(self) -> None:
        """
        验证 GuideIndexPage.get_context() 严格按当前页面的 Locale 过滤子页面，
        异种语言的指南即使在数据库中也不会泄漏在当前语言列表页中。
        """
        from wagtail.models import Locale

        # 1. 创建同语言 (zh-hans) 指南
        guide_zh = GuidePage(
            title="中文版云原生实战指南",
            slug="cloud-native-zh",
            summary="中文指南摘要",
            topic=self.topic,
            difficulty="beginner",
            body=[{"type": "step", "value": {"title": "第一步", "description": "<p>中文描述</p>", "image": None}}],
            locale=self.locale,
        )
        self.index_page.add_child(instance=guide_zh)
        guide_zh._test_content_store = self.store
        rev_zh = guide_zh.save_revision(user=self.user)
        guide_zh.publish(rev_zh, user=self.user)

        # 2. 创建不同语言 (en) 的 GuidePage
        locale_en, _ = Locale.objects.get_or_create(language_code="en")
        guide_en = GuidePage(
            title="English Cloud Native Guide",
            slug="cloud-native-en",
            summary="English guide summary",
            topic=self.topic,
            difficulty="beginner",
            body=[{"type": "step", "value": {"title": "Step One", "description": "<p>English desc</p>", "image": None}}],
            locale=locale_en,
        )
        self.index_page.add_child(instance=guide_en)
        guide_en._test_content_store = self.store
        rev_en = guide_en.save_revision(user=self.user)
        guide_en.publish(rev_en, user=self.user)

        # 3. 请求中文索引页
        response = self.client.get(self.index_page.url)
        self.assertEqual(response.status_code, 200)

        # 验证 context 中的指南列表严格隔离当前语言
        context_guides = list(response.context["guides"])
        guide_ids = [g.id for g in context_guides]
        self.assertIn(guide_zh.id, guide_ids)
        self.assertNotIn(guide_en.id, guide_ids)

        # 验证渲染的 HTML 中严格不包含英文版指南标题与链接
        self.assertContains(response, guide_zh.title)
        self.assertNotContains(response, guide_en.title)

    def test_guide_accessibility_and_responsive_structure(self) -> None:
        """
        验证 GuidePage 详情页的无障碍（ARIA、语义化、键盘焦点）与响应式断点资产完备性。
        """
        # 1. 创建包含 step 与 code 的指南详情页
        guide = GuidePage(
            title="键盘访问与响应式测试指南",
            slug="a11y-responsive-guide",
            summary="验证目录键盘跳转与代码块复制",
            topic=self.topic,
            difficulty="intermediate",
            body=[
                {
                    "type": "step",
                    "value": {"title": "环境初始化", "description": "<p>准备容器与网络</p>", "image": None},
                },
                {
                    "type": "code",
                    "value": {"language": "bash", "code": "docker run -d nginx:latest", "caption": ""},
                },
            ],
            locale=self.locale,
        )
        self.index_page.add_child(instance=guide)
        guide._test_content_store = self.store
        rev = guide.save_revision(user=self.user)
        guide.publish(rev, user=self.user)

        response = self.client.get(guide.url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")

        # 2. 验证 TOC 目录导航的语义化与 ARIA 标记
        self.assertIn('<nav class="guide-toc"', html)
        self.assertIn('aria-label="指南目录导航"', html)

        # 3. 验证步骤锚点容器 ID 与 TOC 精确闭环对应
        import re
        toc_match = re.search(r'class="guide-toc-link"\s*href="([^"]+)"', html) or re.search(
            r'href="([^"]+)"\s*class="guide-toc-link"', html
        )
        self.assertIsNotNone(toc_match)
        anchor_ref = toc_match.group(1)
        self.assertTrue(anchor_ref.startswith("#"))
        target_id = anchor_ref.lstrip("#")
        self.assertIn(f'id="{target_id}"', html)
        self.assertIn('class="guide-step-anchor-wrapper"', html)

        # 4. 验证代码复制按钮支持键盘操作且具备可访问标签
        self.assertIn('class="guide-code-copy-btn"', html)
        self.assertIn('aria-label="复制代码"', html)
        self.assertIn('<button type="button"', html)

        # 5. 验证 CSS 响应式断点与键盘焦点样式
        from pathlib import Path
        css_path = Path("wagtailblog3/static/guides/css/guides.css")
        self.assertTrue(css_path.exists())
        css_content = css_path.read_text(encoding="utf-8")

        # 断言响应式媒体查询
        self.assertIn("@media (max-width: 991px)", css_content)
        self.assertIn("@media (max-width: 767px)", css_content)

        # 断言键盘无障碍焦点可见性规则
        self.assertIn(":focus-visible", css_content)
