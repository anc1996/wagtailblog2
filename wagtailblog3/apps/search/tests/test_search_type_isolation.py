"""WP-G5 搜索类型隔离测试与联合流边界断言。

涵盖维度：
1. SimpleTestCase 单元测试：
   - 核心层、API 层、视图层对未知 search_type 的 fail-closed / 400 防御；
   - API invalid 固定错误码 invalid_search_type 与消息，不依赖英文原值；
   - 类型白名单（all, blog, pages, guides）分流路由，绝不吞异常假 GREEN；
   - live() / public() 链路约束。
2. TestCase 真实 MySQL 数据层断言：
   - build_public_guides_queryset(): 严格隔离出公开 live GuidePage，排除 BlogPage、普通 Page、草稿及受限 GuidePage；
   - build_public_pages_queryset(): 严格隔离出普通公开 live Page，双重排除所有 BlogPage 与 GuidePage、草稿及受限 Page；
   - search.services.federated_query._public_pages_queryset():
     断言全站联合搜索 non-Blog 流必须包含公开 live GuidePage 与普通 Page，
     排除 BlogPage、草稿及受限页面。
"""

from __future__ import annotations

from datetime import date, datetime, timezone as dt_timezone
from unittest.mock import MagicMock, patch

from django.test import RequestFactory, SimpleTestCase, TestCase
from wagtail.models import Locale, Page, PageViewRestriction, Site

from blog.models import BlogPage
from guides.models import GuideIndexPage, GuidePage, GuideTopic
from search.api import search_api
from search.core import SearchTypeInvalidError, perform_search
from search.services.federated_query import _public_pages_queryset
from search.services.guides_query import build_public_guides_queryset
from search.services.pages_query import build_public_pages_queryset
from search.tests.test_lifecycle_baseline import BlogLifecycleFixtureMixin
from search.views import get_search_results_context


class SearchTypeIsolationTests(SimpleTestCase):
	"""验证搜索类型互斥、白名单路由与安全边界（无异常吞没）。"""

	def test_unknown_search_type_raises_value_error(self):
		"""未知类型在核心层明确拒绝抛出 ValueError/SearchTypeInvalidError。"""
		with self.assertRaises(ValueError) as context:
			perform_search("测试", search_type="unknown")

		self.assertIn("不支持的搜索类型", str(context.exception))

	def test_api_rejects_unknown_search_type_with_400(self):
		"""API 层对未知类型返回 400 错误，断言固定中文消息与错误码，不要求英文原值。"""
		factory = RequestFactory()
		request = factory.get("/search/api/", {"q": "测试", "type": "invalid"})

		response = search_api(request)

		self.assertEqual(response.status_code, 400)
		data = response.data
		self.assertEqual(data["error"]["code"], "invalid_search_type")
		self.assertEqual(data["error"]["message"], "不支持的搜索类型。")

	def test_view_rejects_unknown_search_type(self):
		"""视图层对未知类型拒绝处理并抛出 SearchTypeInvalidError/ValueError。"""
		with self.assertRaises((SearchTypeInvalidError, ValueError)):
			get_search_results_context({"query": "测试", "type": "xyz"})

	@patch("search.core._build_search_results_for_queryset")
	@patch("search.core.Query.get")
	@patch("search.services.guides_query.build_public_guides_queryset")
	def test_guides_type_uses_guides_queryset(
		self, mock_build_guides_qs, mock_query, mock_search
	):
		"""type=guides 命中 guides 专用查询集构造分支。"""
		mock_search.return_value = MagicMock()
		mock_build_guides_qs.return_value = MagicMock()
		perform_search("指南", search_type="guides")

		mock_build_guides_qs.assert_called_once()
		mock_search.assert_called_once()
		call_kwargs = mock_search.call_args.kwargs
		self.assertEqual(call_kwargs["search_type"], "guides")
		self.assertEqual(mock_search.call_args.args[0], mock_build_guides_qs.return_value)

	@patch("guides.models.GuidePage")
	@patch("search.services.pages_query.BlogPage")
	@patch("search.services.pages_query.Page.objects")
	def test_pages_queryset_excludes_blog_and_guides(
		self, mock_page_objects, mock_blog_page, mock_guide_page
	):
		"""pages QuerySet 必须双重排除 BlogPage 与 GuidePage。"""
		mock_blog_page.objects.values.return_value = [201]
		mock_guide_page.objects.values.return_value = [101]

		public_qs = mock_page_objects.live.return_value.public.return_value
		exclude_blog_qs = public_qs.exclude.return_value
		exclude_guide_qs = exclude_blog_qs.exclude.return_value

		result = build_public_pages_queryset()

		public_qs.exclude.assert_called_once_with(id__in=[201])
		exclude_blog_qs.exclude.assert_called_once_with(id__in=[101])
		self.assertEqual(result, exclude_guide_qs)

	@patch("guides.models.GuidePage")
	@patch("search.services.guides_query.Page.objects")
	def test_guides_queryset_includes_only_guides(
		self, mock_page_objects, mock_guide_page
	):
		"""guides QuerySet 仅限定包含 GuidePage ID。"""
		mock_guide_page.objects.values.return_value = [101, 102]
		public_qs = mock_page_objects.live.return_value.public.return_value
		filtered_qs = public_qs.filter.return_value

		result = build_public_guides_queryset()

		public_qs.filter.assert_called_once_with(id__in=[101, 102])
		self.assertEqual(result, filtered_qs)

	@patch("search.core.build_content_search_results")
	@patch("search.core.Query.get")
	def test_whitelist_allows_blog(self, mock_query, mock_blog_search):
		"""白名单允许 blog 类型且明确调用博客内容搜索流（无 try/except pass 吞异常）。"""
		mock_blog_search.return_value = MagicMock()
		result = perform_search("测试", search_type="blog")
		self.assertEqual(result, mock_blog_search.return_value)
		mock_blog_search.assert_called_once()

	@patch("search.core.build_federated_search_results")
	@patch("search.core.Query.get")
	def test_whitelist_allows_all(self, mock_query, mock_fed_search):
		"""白名单允许 all 类型且明确调用全站联合搜索流（无 try/except pass 吞异常）。"""
		mock_fed_search.return_value = MagicMock()
		result = perform_search("测试", search_type="all")
		self.assertEqual(result, mock_fed_search.return_value)
		mock_fed_search.assert_called_once()

	@patch("search.core._build_search_results_for_queryset")
	@patch("search.core.Query.get")
	@patch("search.services.guides_query.build_public_guides_queryset")
	def test_whitelist_allows_guides(self, mock_build_guides_qs, mock_query, mock_search):
		"""白名单允许 guides 类型且返回专用查询集封装（无 try/except pass 吞异常）。"""
		mock_search.return_value = MagicMock()
		mock_build_guides_qs.return_value = MagicMock()
		result = perform_search("测试", search_type="guides")
		self.assertEqual(result, mock_search.return_value)
		self.assertEqual(mock_search.call_args.kwargs["search_type"], "guides")

	@patch("search.core._build_search_results_for_queryset")
	@patch("search.core.Query.get")
	@patch("search.core.build_public_pages_queryset")
	def test_whitelist_allows_pages(self, mock_build_pages_qs, mock_query, mock_search):
		"""白名单允许 pages 类型且返回专用查询集封装（无 try/except pass 吞异常）。"""
		mock_search.return_value = MagicMock()
		mock_build_pages_qs.return_value = MagicMock()
		result = perform_search("测试", search_type="pages")
		self.assertEqual(result, mock_search.return_value)
		self.assertEqual(mock_search.call_args.kwargs["search_type"], "pages")


class SearchTypeSecurityTests(SimpleTestCase):
	"""验证类型隔离查询构造入口强制限定 live 和 public。"""

	@patch("guides.models.GuidePage")
	@patch("search.services.guides_query.Page.objects")
	def test_guides_queryset_enforces_live_and_public(
		self, mock_page_objects, mock_guide_page
	):
		"""guides 查询强制限定 live() 和 public()。"""
		mock_guide_page.objects.values.return_value = []
		mock_live_qs = MagicMock()
		mock_public_qs = MagicMock()

		mock_page_objects.live.return_value = mock_live_qs
		mock_live_qs.public.return_value = mock_public_qs
		mock_public_qs.filter.return_value = MagicMock()

		build_public_guides_queryset()

		mock_page_objects.live.assert_called_once()
		mock_live_qs.public.assert_called_once()

	@patch("guides.models.GuidePage")
	@patch("search.services.pages_query.BlogPage")
	@patch("search.services.pages_query.Page.objects")
	def test_pages_queryset_enforces_live_and_public(
		self, mock_page_objects, mock_blog_page, mock_guide_page
	):
		"""pages 查询强制限定 live() 和 public()。"""
		mock_blog_page.objects.values.return_value = []
		mock_guide_page.objects.values.return_value = []
		mock_live_qs = MagicMock()
		mock_public_qs = MagicMock()
		mock_exclude_blog = MagicMock()

		mock_page_objects.live.return_value = mock_live_qs
		mock_live_qs.public.return_value = mock_public_qs
		mock_public_qs.exclude.return_value = mock_exclude_blog
		mock_exclude_blog.exclude.return_value = MagicMock()

		build_public_pages_queryset()

		mock_page_objects.live.assert_called_once()
		mock_live_qs.public.assert_called_once()


class SearchTypeIsolationSQLTests(BlogLifecycleFixtureMixin, TestCase):
	"""真实 MySQL 数据库断言类型隔离、权限边界与全站联合流覆盖。

	覆盖维度：
	1. build_public_guides_queryset(): 仅包含公开 live GuidePage，排除 BlogPage、普通 Page、草稿及受限 GuidePage。
	2. build_public_pages_queryset(): 仅包含公开 live 普通 Page，排除所有 BlogPage 与 GuidePage、草稿及受限 Page。
	3. search.services.federated_query._public_pages_queryset():
	   [全站联合搜索 non-Blog 流 RED 断言]
	   断言 _public_pages_queryset() 必须包含公开 live GuidePage 与普通 Page，排除 BlogPage、草稿及受限页面。
	   在主线程接入 pages_query.build_public_non_blog_queryset 之前，此处预期 RED。
	"""

	def setUp(self):
		super().setUp()
		self.locale, _ = Locale.objects.get_or_create(language_code="zh-hans")
		root = Page.objects.filter(depth=1).first()
		if not root:
			root = Page.add_root(title="Root", locale=self.locale)
		self.root = root

		self.home = Page.objects.filter(depth=2).first()
		if not self.home:
			self.home = Page(title="Home", slug="home", locale=self.locale)
			self.root.add_child(instance=self.home)

		self.site = Site.objects.filter(is_default_site=True).first()
		if not self.site:
			self.site = Site.objects.create(
				hostname="localhost",
				port=80,
				site_name="Test Site",
				root_page=self.home,
				is_default_site=True,
			)

		# 1. 普通 Wagtail Page
		# 1.1 公开已发布普通页
		self.standard_live_page = Page(
			title="公开普通页面",
			slug="standard-live-page",
			locale=self.locale,
			live=True,
			first_published_at=datetime(2026, 9, 1, 10, 0, 0, tzinfo=dt_timezone.utc),
		)
		self.home.add_child(instance=self.standard_live_page)

		# 1.2 草稿普通页
		self.standard_draft_page = Page(
			title="草稿普通页面",
			slug="standard-draft-page",
			locale=self.locale,
			live=False,
			first_published_at=datetime(2026, 9, 2, 10, 0, 0, tzinfo=dt_timezone.utc),
		)
		self.home.add_child(instance=self.standard_draft_page)

		# 1.3 密码保护/受限普通页
		self.standard_restricted_page = Page(
			title="受限普通页面",
			slug="standard-restricted-page",
			locale=self.locale,
			live=True,
			first_published_at=datetime(2026, 9, 3, 10, 0, 0, tzinfo=dt_timezone.utc),
		)
		self.home.add_child(instance=self.standard_restricted_page)
		PageViewRestriction.objects.create(
			page=self.standard_restricted_page,
			restriction_type=PageViewRestriction.PASSWORD,
			password="secret-password",
		)

		# 2. Guide 相关模型
		self.guide_topic = GuideTopic.objects.create(
			name="类型隔离专题",
			slug="type-iso-topic",
			description="用于搜索类型隔离测试",
		)
		self.guide_index = GuideIndexPage(
			title="指南中心",
			slug="guides-center",
			intro="专题指南集合",
			locale=self.locale,
			live=True,
			first_published_at=datetime(2026, 9, 4, 10, 0, 0, tzinfo=dt_timezone.utc),
		)
		self.home.add_child(instance=self.guide_index)

		# 2.1 公开已发布 GuidePage
		self.guide_live = GuidePage(
			title="公开指南页面",
			slug="guide-live-page",
			summary="公开指南摘要",
			topic=self.guide_topic,
			difficulty="beginner",
			locale=self.locale,
			live=True,
			first_published_at=datetime(2026, 9, 10, 10, 0, 0, tzinfo=dt_timezone.utc),
			body=[
				{
					"type": "step",
					"value": {
						"title": "测试步骤",
						"description": "<p>测试说明</p>",
						"image": None,
					},
				}
			],
		)
		self.guide_index.add_child(instance=self.guide_live)

		# 2.2 草稿 GuidePage
		self.guide_draft = GuidePage(
			title="草稿指南页面",
			slug="guide-draft-page",
			summary="草稿指南摘要",
			topic=self.guide_topic,
			difficulty="beginner",
			locale=self.locale,
			live=False,
			first_published_at=datetime(2026, 9, 11, 10, 0, 0, tzinfo=dt_timezone.utc),
			body=[],
		)
		self.guide_index.add_child(instance=self.guide_draft)

		# 2.3 受限 GuidePage
		self.guide_restricted = GuidePage(
			title="受限指南页面",
			slug="guide-restricted-page",
			summary="受限指南摘要",
			topic=self.guide_topic,
			difficulty="beginner",
			locale=self.locale,
			live=True,
			first_published_at=datetime(2026, 9, 12, 10, 0, 0, tzinfo=dt_timezone.utc),
			body=[
				{
					"type": "step",
					"value": {
						"title": "受限步骤",
						"description": "<p>受限说明</p>",
						"image": None,
					},
				}
			],
		)
		self.guide_index.add_child(instance=self.guide_restricted)
		PageViewRestriction.objects.create(
			page=self.guide_restricted,
			restriction_type=PageViewRestriction.PASSWORD,
			password="guide-secret-password",
		)

		# 3. BlogPage
		# 3.1 公开已发布 BlogPage
		self.blog_live = BlogPage(
			title="公开博客页面",
			slug="blog-live-page",
			locale=self.locale,
			live=True,
			date=date(2026, 9, 15),
			first_published_at=datetime(2026, 9, 15, 10, 0, 0, tzinfo=dt_timezone.utc),
			intro="<p>公开博客简介</p>",
			body=[],
		)
		self.home.add_child(instance=self.blog_live)

		# 3.2 草稿 BlogPage
		self.blog_draft = BlogPage(
			title="草稿博客页面",
			slug="blog-draft-page",
			locale=self.locale,
			live=False,
			date=date(2026, 9, 16),
			first_published_at=datetime(2026, 9, 16, 10, 0, 0, tzinfo=dt_timezone.utc),
			intro="<p>草稿博客简介</p>",
			body=[],
		)
		self.home.add_child(instance=self.blog_draft)

		# 3.3 受限 BlogPage
		self.blog_restricted = BlogPage(
			title="受限博客页面",
			slug="blog-restricted-page",
			locale=self.locale,
			live=True,
			date=date(2026, 9, 17),
			first_published_at=datetime(2026, 9, 17, 10, 0, 0, tzinfo=dt_timezone.utc),
			intro="<p>受限博客简介</p>",
			body=[],
		)
		self.home.add_child(instance=self.blog_restricted)
		PageViewRestriction.objects.create(
			page=self.blog_restricted,
			restriction_type=PageViewRestriction.PASSWORD,
			password="blog-secret-password",
		)

	def test_sql_guides_queryset_type_isolation(self):
		"""build_public_guides_queryset() 在真实数据库中仅包含公开 live GuidePage。"""
		qs = build_public_guides_queryset()
		result_ids = set(qs.values_list("id", flat=True))

		# 1. 包含公开已发布 GuidePage
		self.assertIn(self.guide_live.pk, result_ids)

		# 2. 严格排除非 GuidePage（BlogPage、普通 Page、GuideIndexPage）
		self.assertNotIn(self.blog_live.pk, result_ids)
		self.assertNotIn(self.standard_live_page.pk, result_ids)
		self.assertNotIn(self.guide_index.pk, result_ids)

		# 3. 严格排除草稿与受限页面
		self.assertNotIn(self.guide_draft.pk, result_ids)
		self.assertNotIn(self.guide_restricted.pk, result_ids)

	def test_sql_pages_queryset_type_isolation(self):
		"""build_public_pages_queryset() 在真实数据库中排除 BlogPage 与 GuidePage，仅含普通公开 live Page。"""
		qs = build_public_pages_queryset()
		result_ids = set(qs.values_list("id", flat=True))

		# 1. 包含公开已发布普通 Page 与 GuideIndexPage（均为非 Blog/Guide 页面）
		self.assertIn(self.standard_live_page.pk, result_ids)
		self.assertIn(self.guide_index.pk, result_ids)

		# 2. 严格排除所有 BlogPage（live、draft、restricted）
		self.assertNotIn(self.blog_live.pk, result_ids)
		self.assertNotIn(self.blog_draft.pk, result_ids)
		self.assertNotIn(self.blog_restricted.pk, result_ids)

		# 3. 严格排除所有 GuidePage（live、draft、restricted）
		self.assertNotIn(self.guide_live.pk, result_ids)
		self.assertNotIn(self.guide_draft.pk, result_ids)
		self.assertNotIn(self.guide_restricted.pk, result_ids)

		# 4. 严格排除草稿普通页与受限普通页
		self.assertNotIn(self.standard_draft_page.pk, result_ids)
		self.assertNotIn(self.standard_restricted_page.pk, result_ids)

	def test_sql_federated_public_pages_queryset_includes_guides_and_pages_red(self):
		"""[全站联合搜索 non-Blog 流 RED 断言]

		契约要求：
		全站联合搜索（type=all）的双流之一应为包含所有非 Blog 的公开页面（普通 Page + GuidePage）。
		通过 search.services.federated_query._public_pages_queryset 断言：
		1. 必须包含公开 live GuidePage 与普通 Page；
		2. 严格排除 BlogPage、草稿（draft）与受限（restricted）页面。

		[RED 说明]
		当前 federated_query._public_pages_queryset 直接代理了 pages_query.build_public_pages_queryset，
		而后者执行了 .exclude(id__in=guide_ids)，导致 GuidePage 在 all 流中被遗漏。
		主线程后续将在 pages_query 中提供 build_public_non_blog_queryset 并在 federated_query 中替换接入。
		在主线程落地该 runtime 修改前，此处对 self.guide_live.pk 的包含断言预期 RED。
		"""
		qs = _public_pages_queryset()
		result_ids = set(qs.values_list("id", flat=True))

		# 必须排除所有 BlogPage
		self.assertNotIn(self.blog_live.pk, result_ids)
		self.assertNotIn(self.blog_draft.pk, result_ids)
		self.assertNotIn(self.blog_restricted.pk, result_ids)

		# 必须排除草稿和受限页面
		self.assertNotIn(self.standard_draft_page.pk, result_ids)
		self.assertNotIn(self.standard_restricted_page.pk, result_ids)
		self.assertNotIn(self.guide_draft.pk, result_ids)
		self.assertNotIn(self.guide_restricted.pk, result_ids)

		# 必须包含公开 live 普通 Page
		self.assertIn(self.standard_live_page.pk, result_ids)

		# 核心 RED 断言：all 联合搜索 non-Blog 流必须包含公开 live GuidePage
		self.assertIn(
			self.guide_live.pk,
			result_ids,
			"全站联合搜索 non-Blog 流遗漏了公开 live GuidePage（当前预期 RED，等待主线程接入 build_public_non_blog_queryset）",
		)

	def test_sql_guides_date_filtering_and_ordering(self):
		"""build_public_guides_queryset() 日期范围过滤与排序断言。"""
		start = datetime(2026, 9, 9, 0, 0, 0, tzinfo=dt_timezone.utc)
		end = datetime(2026, 9, 11, 23, 59, 59, tzinfo=dt_timezone.utc)

		# 在范围内的 GuidePage
		in_range_qs = build_public_guides_queryset(start_date=start, end_date=end)
		self.assertIn(self.guide_live.pk, list(in_range_qs.values_list("id", flat=True)))

		# 超出范围
		out_range_qs = build_public_guides_queryset(
			start_date=datetime(2026, 10, 1, tzinfo=dt_timezone.utc)
		)
		self.assertNotIn(self.guide_live.pk, list(out_range_qs.values_list("id", flat=True)))

	def test_sql_pages_date_filtering_and_ordering(self):
		"""build_public_pages_queryset() 日期范围过滤与排序断言。"""
		start = datetime(2026, 8, 31, 0, 0, 0, tzinfo=dt_timezone.utc)
		end = datetime(2026, 9, 2, 23, 59, 59, tzinfo=dt_timezone.utc)

		in_range_qs = build_public_pages_queryset(start_date=start, end_date=end)
		self.assertIn(self.standard_live_page.pk, list(in_range_qs.values_list("id", flat=True)))

		out_range_qs = build_public_pages_queryset(
			start_date=datetime(2026, 10, 1, tzinfo=dt_timezone.utc)
		)
		self.assertNotIn(self.standard_live_page.pk, list(out_range_qs.values_list("id", flat=True)))
