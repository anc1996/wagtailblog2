"""请求防重接入测试；SQL、缓存边界使用显式替身，禁止真实 IO。"""

import hashlib
import json
from contextlib import nullcontext
from unittest.mock import Mock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import HttpResponse, JsonResponse
from django.http import QueryDict
from django.test import RequestFactory, SimpleTestCase


class TestRequestGuardRed(SimpleTestCase):
    def test_controlled_post_without_token_never_enters_native_action(self) -> None:
        """缺少防重载体不能绕过门禁执行原生写入。"""
        calls = []

        def native(view, request, *args, **kwargs):
            calls.append("business-write")
            return HttpResponse(status=201)

        from external_content.request_bridge import guard_dispatch
        dispatch = guard_dispatch(native, "edit")
        request = RequestFactory().post("/admin/pages/7/edit/", {"title": "a"})
        request.user = type("User", (), {"pk": 3, "is_authenticated": True})()
        with patch("external_content.request_bridge.resolve_scope", return_value={
            "kind": "edit", "page": 7,
        }):
            response = dispatch(object(), request, page_id=7)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(calls, [])


class FakeService:
    def __init__(self, *, claim=None):
        self.claim_result = claim
        self.calls = []
        self.next_ticket = "next-ticket"

    def reserve(self, user_id, scope):
        self.calls.append(("reserve", user_id, scope))
        return "reservation-ticket"

    def prepare(self, ticket, user_id, scope, digest, *, authorization=None):
        self.calls.append(("prepare", ticket, user_id, scope, digest, authorization))
        return "execution-token"

    def claim(self, token, user_id, scope, digest):
        self.calls.append(("claim", token, user_id, scope, digest))
        return self.claim_result

    def complete(self, claim, result):
        self.calls.append(("complete", claim, result))
        return True

    def fail(self, claim, *, rollback_proven=False):
        self.calls.append(("fail", claim, rollback_proven))
        return True


def authenticated_request(request):
    request.user = type("User", (), {"pk": 3, "is_authenticated": True})()
    request._dont_enforce_csrf_checks = True
    return request


class TestRequestDigest(SimpleTestCase):
    def test_digest_preserves_repeated_values_and_excludes_carriers(self):
        from external_content.request_bridge import request_digest

        request = authenticated_request(RequestFactory().post(
            "/admin/pages/7/edit/",
            QueryDict("tag=first&tag=second&_ec_token=secret-carrier&csrfmiddlewaretoken=csrf"),
        ))
        original = request.POST.getlist("tag")
        digest = request_digest(request)
        request.POST = request.POST.copy()
        request.POST["_ec_token"] = "different-carrier"
        request.POST["csrfmiddlewaretoken"] = "different-csrf"
        self.assertEqual(request_digest(request), digest)
        self.assertEqual(request.POST.getlist("tag"), original)

    def test_digest_changes_when_repeated_value_order_changes(self):
        from external_content.request_bridge import request_digest

        first = authenticated_request(RequestFactory().post(
            "/admin/pages/7/edit/", QueryDict("tag=first&tag=second")
        ))
        second = authenticated_request(RequestFactory().post(
            "/admin/pages/7/edit/", QueryDict("tag=second&tag=first")
        ))
        self.assertNotEqual(request_digest(first), request_digest(second))

    def test_digest_restores_uploaded_file_position_and_hashes_content(self):
        from external_content.request_bridge import request_digest

        upload = SimpleUploadedFile("body.txt", b"stable body", content_type="text/plain")
        upload.seek(3)
        request = authenticated_request(RequestFactory().post(
            "/admin/pages/7/edit/", {"title": "a"},
            format="multipart",
        ))
        request.FILES.setlist("file", [upload])
        position = upload.tell()
        digest = request_digest(request)
        self.assertEqual(upload.tell(), position)
        self.assertEqual(len(digest), hashlib.sha256().digest_size * 2)

    def test_digest_for_json_body_includes_raw_body_hash(self):
        from external_content.request_bridge import request_digest

        first = authenticated_request(RequestFactory().generic(
            "POST", "/admin/pages/7/edit/", b'{"title":"a"}',
            content_type="application/json",
        ))
        second = authenticated_request(RequestFactory().generic(
            "POST", "/admin/pages/7/edit/", b'{"title":"b"}',
            content_type="application/json",
        ))
        self.assertNotEqual(request_digest(first), request_digest(second))


class TestPrepareAndTarget(SimpleTestCase):
    def test_prepare_keeps_publish_permission_requirement_without_request_snapshot(self):
        from external_content import request_bridge

        request = authenticated_request(RequestFactory().post(
            "/admin/external-content/request/prepare/",
            {"_ec_ticket": "reservation", "_ec_target": "/admin/pages/7/edit/",
             "action-publish": "Publish", "body": "不应缓存的正文"},
        ))
        service = FakeService()
        with patch.object(request_bridge, "resolve_scope",
                          return_value={"kind": "edit", "page": 7}), \
             patch.object(request_bridge, "service", return_value=service):
            response = request_bridge.prepare_view(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(service.calls[0][-1], {"publish": True})

    def test_status_rechecks_original_publish_requirement(self):
        from django.core.exceptions import PermissionDenied
        from external_content import request_bridge

        request = authenticated_request(RequestFactory().post(
            "/admin/external-content/request/status/", {"_ec_token": "execution"}))
        service = FakeService()
        service.status = lambda *args: {
            "state": "succeeded", "scope": {"kind": "edit", "page": 7},
            "result": {"page": 7}, "authorization": {"publish": True},
        }
        def permission(req, kind, kwargs):
            if req.POST.get("action-publish"):
                raise PermissionDenied
            return {"kind": kind, "page": 7}
        with patch.object(request_bridge, "service", return_value=service), \
             patch.object(request_bridge, "resolve_scope", side_effect=permission), \
             patch.object(request_bridge, "visible_result"):
            response = request_bridge.status_view(request)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("action-publish", request.POST)

    def test_prepare_uses_original_target_query_and_returns_token(self):
        from external_content import request_bridge

        request = authenticated_request(RequestFactory().post(
            "/admin/external-content/request/prepare/",
            {"_ec_ticket": "reservation", "_ec_target": "/admin/pages/7/edit/?revision_id=2",
             "title": "a"},
        ))
        service = FakeService()
        with (
            patch.object(request_bridge, "target_route",
                         return_value=("edit", {"page_id": 7})),
            patch.object(request_bridge, "resolve_scope",
                         return_value={"kind": "edit", "page": 7}) as resolve_scope,
            patch.object(request_bridge, "service", return_value=service),
            patch.object(request_bridge, "request_digest", return_value="digest"),
        ):
            response = request_bridge.prepare_view(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), {"token": "execution-token"})
        self.assertEqual(resolve_scope.call_args.args[1:], ("edit", {"page_id": 7}))
        self.assertEqual(request.GET.get("revision_id"), None)
        self.assertEqual(service.calls[0][0], "prepare")

    def test_prepare_rejects_external_target_before_scope_resolution(self):
        from external_content import request_bridge

        request = authenticated_request(RequestFactory().post(
            "/admin/external-content/request/prepare/",
            {"_ec_ticket": "reservation", "_ec_target": "https://evil.invalid/admin/pages/7/edit/",
             "title": "a"},
        ))
        with patch.object(request_bridge, "resolve_scope") as resolve_scope:
            response = request_bridge.prepare_view(request)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.content)["error"], "external_content_target")
        resolve_scope.assert_not_called()

    def test_prepare_rejects_workflow_action_without_signing(self):
        from external_content import request_bridge

        request = authenticated_request(RequestFactory().post(
            "/admin/external-content/request/prepare/",
            {"_ec_ticket": "reservation", "_ec_target": "/admin/pages/7/edit/",
             "action-submit": "Submit"},
        ))
        with patch.object(request_bridge, "target_route",
                          return_value=("edit", {"page_id": 7})), \
             patch.object(request_bridge, "service") as service:
            response = request_bridge.prepare_view(request)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.content)["error"], "external_content_unsupported")
        service.assert_not_called()


class TestGuardDispatch(SimpleTestCase):
    scope = {"kind": "edit", "page": 7}

    def setUp(self):
        self.user = type("User", (), {"pk": 3, "is_authenticated": True})()
        # 在事务替身安装前加载 URLconf，避免替身影响上游模块的 atomic 装饰器。
        from django.urls import resolve
        resolve("/admin/pages/7/edit/")

    def request(self, data=None, *, method="POST"):
        factory = RequestFactory()
        if method == "GET":
            request = factory.get("/admin/pages/7/edit/")
        else:
            request = factory.post("/admin/pages/7/edit/", data or {"title": "a"})
        request.user = self.user
        request._dont_enforce_csrf_checks = True
        return request

    def test_controlled_workflow_fields_never_bypass_guard(self):
        """无效或混合工作流字段可能退化为保存，受控入口必须拒绝而非裸透传。"""
        from external_content.request_bridge import guard_dispatch

        calls = []
        native = lambda view, request, **kwargs: (calls.append("native") or HttpResponse(status=202))
        for data in (
            {"action-submit": "Submit"},
            {"action-publish": "Publish", "action-submit": "Submit"},
            {"action-workflow-action": "invalid"},
            {"action-cancel-workflow": ""},
        ):
            with self.subTest(data=data), \
                 patch("external_content.request_bridge.resolve_scope", return_value=self.scope), \
                 patch("external_content.request_bridge.service") as service:
                response = guard_dispatch(native, "edit")(object(), self.request(data), page_id=7)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(json.loads(response.content)["error"],
                                 "external_content_unsupported")
                self.assertEqual(calls, [])
                service.assert_not_called()

    def test_ordinary_page_workflow_remains_transparent(self):
        from external_content.request_bridge import guard_dispatch

        calls = []
        native = lambda view, request, **kwargs: (calls.append("native") or HttpResponse(status=202))
        with patch("external_content.request_bridge.resolve_scope", return_value=None):
            response = guard_dispatch(native, "edit")(
                object(), self.request({"action-submit": "Submit"}), page_id=7)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(calls, ["native"])

    def test_json_replay_only_returns_authorized_result_identity_without_native_get(self):
        """重放真实适配器，原生 GET 可写 subscription/session，不能当作只读重建。"""
        from external_content import request_bridge
        from wagtail.admin.views.pages.edit import EditView

        writes = []
        def forbidden_dispatch(*args, **kwargs):
            writes.append("native-get-side-effects")
            return HttpResponse(status=200)
        result = {"page": 7, "revision": 11, "json": True,
                  "created": False, "field_updates": {}}
        revision = type("Revision", (), {"pk": 11})()
        page = type("Page", (), {"pk": 7, "get_latest_revision": lambda self: revision})()
        request = self.request({"_ec_token": "execution"})
        request.META["HTTP_ACCEPT"] = "application/json"
        with patch.object(request_bridge, "visible_result", return_value=page), \
             patch.object(EditView, "dispatch", forbidden_dispatch), \
             patch.object(request_bridge, "service") as service:
            response = request_bridge.replay_response(request, result)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(writes, [])
        data = json.loads(response.content)
        self.assertFalse(data["success"])
        self.assertEqual(data.get("state"), "succeeded")
        self.assertEqual(data.get("result"), {"page": 7, "revision": 11})
        self.assertEqual(data["error"], "external_content_replay_unavailable")
        self.assertNotIn("_ec_ticket", data)
        self.assertEqual(writes, [])
        service.assert_not_called()

    def test_non_web_direct_dispatch_is_transparent(self):
        """内部调用不属于白名单 Web 入口，不能额外查库或领取身份。"""
        from external_content.request_bridge import guard_dispatch

        native = Mock(return_value=HttpResponse(status=202))
        request = RequestFactory().post("/L0/")
        with patch("external_content.request_bridge.resolve_scope") as scope:
            response = guard_dispatch(native, "delete")(object(), request, page_id=1)
        self.assertEqual(response.status_code, 202)
        native.assert_called_once()
        scope.assert_not_called()

    def test_native_route_does_not_bypass_guard_without_resolver_match(self):
        """直接构造的真实路由请求也必须防重，不能以缺少 middleware 标记绕过。"""
        from external_content.request_bridge import guard_dispatch

        native = Mock(return_value=HttpResponse(status=202))
        request = self.request()
        with patch("external_content.request_bridge.resolve_scope", return_value=self.scope):
            response = guard_dispatch(native, "edit")(object(), request, page_id=7)
        self.assertEqual(response.status_code, 409)
        native.assert_not_called()

    def test_json_success_is_completed_on_commit_and_preserves_native_fields(self):
        from external_content import request_bridge
        from external_content.idempotency import Claim

        claim = Claim("a" * 32, {"state": "processing"}, replay=False)
        service = FakeService(claim=claim)
        native_response = JsonResponse({
            "success": True, "pk": 7, "revision_id": 11,
            "revision_created_at": "2026-10-09T00:00:00Z",
            "field_updates": {"items-INITIAL_FORMS": "1"},
            "comments": {"threads": []}, "html": "<div>kept</div>",
        })
        def native(view, request, **kwargs):
            from external_content.wagtail_hooks import capture_edit_result
            capture_edit_result(request, type("Page", (), {"pk": 7})())
            return native_response
        request = self.request({"title": "a", "_ec_token": "execution"})
        request.META["HTTP_ACCEPT"] = "application/json"
        with patch.object(request_bridge, "resolve_scope", return_value=self.scope), \
             patch.object(request_bridge, "service", return_value=service), \
             patch.object(request_bridge, "request_digest", return_value="digest"), \
             patch.object(request_bridge.transaction, "atomic", return_value=nullcontext()), \
             patch.object(request_bridge.transaction, "on_commit",
                          side_effect=lambda callback: callback()):
            response = request_bridge.guard_dispatch(native, "edit")(object(), request, page_id=7)
        body = json.loads(response.content)
        self.assertTrue(body["success"])
        self.assertEqual(body["comments"], {"threads": []})
        self.assertEqual(body["html"], "<div>kept</div>")
        self.assertEqual(body["field_updates"], {"items-INITIAL_FORMS": "1"})
        self.assertEqual(body["_ec_ticket"], "reservation-ticket")
        self.assertEqual([call[0] for call in service.calls], ["claim", "complete", "reserve"])

    def test_redirect_without_after_hook_is_unknown_not_success(self):
        """before-hook 的重定向不是写入完成证据，复制也不能回退为源页面。"""
        from external_content.request_bridge import result_summary

        view = type("View", (), {"page": type("Page", (), {"pk": 7})()})()
        for kind in ("edit", "copy", "delete", "unpublish", "convert_alias", "create"):
            with self.subTest(kind=kind):
                self.assertIsNone(result_summary(view, self.request(), HttpResponse(status=302),
                                                 {"kind": kind, "page": 7}))

    def test_matching_after_hooks_capture_actual_result_and_delete_identity(self):
        from external_content import request_bridge, wagtail_hooks

        cases = [
            ("edit", wagtail_hooks.capture_edit_result, 7),
            ("create", wagtail_hooks.capture_create_result, 8),
            ("publish", wagtail_hooks.capture_publish_result, 7),
            ("unpublish", wagtail_hooks.capture_unpublish_result, 7),
            ("delete", wagtail_hooks.capture_delete_result, None),
            ("convert_alias", wagtail_hooks.capture_convert_alias_result, 7),
        ]
        for effect, hook, page_id in cases:
            with self.subTest(effect=effect):
                request = self.request({"action-publish": "Publish"} if effect == "publish"
                                       else {"title": "a"})
                request._ec_expected_effect = effect
                request._ec_source_page_id = 7
                hook(request, type("Page", (), {"pk": page_id})())
                result = request_bridge.result_summary(object(), request,
                                                       HttpResponse(status=302), self.scope)
                self.assertEqual(result, {"page": page_id or 7})
        request = self.request()
        request._ec_expected_effect = "copy"
        wagtail_hooks.capture_copy_result(request, type("Source", (), {"pk": 7})(),
                                          type("NewPage", (), {"pk": 9})())
        self.assertEqual(request_bridge.result_summary(object(), request, HttpResponse(status=302),
                                                       {"kind": "copy", "page": 7}), {"page": 9})

    def test_publish_not_completed_by_only_after_edit(self):
        from external_content import request_bridge, wagtail_hooks

        request = self.request({"action-publish": "Publish"})
        request._ec_expected_effect = "publish"
        wagtail_hooks.capture_edit_result(request, type("Page", (), {"pk": 7})())
        self.assertIsNone(request_bridge.result_summary(object(), request,
                                                       HttpResponse(status=302), self.scope))

    def test_failed_registration_or_deferred_commit_never_issues_next_ticket(self):
        """业务响应保留；未最终登记时不得让客户端把旧尝试自动替换为新身份。"""
        from external_content import request_bridge, wagtail_hooks
        from external_content.idempotency import Claim

        for deferred in (False, True):
            with self.subTest(deferred=deferred):
                service = FakeService(claim=Claim("a" * 32, {"state": "processing"}))
                service.complete = lambda *args: False
                request = self.request({"title": "a", "_ec_token": "execution"})
                request.META["HTTP_ACCEPT"] = "application/json"
                def native(view, request, **kwargs):
                    wagtail_hooks.capture_edit_result(request, type("Page", (), {"pk": 7})())
                    return JsonResponse({"success": True, "pk": 7, "revision_id": 11,
                                         "field_updates": {}, "html": "<div>kept</div>",
                                         "comments": {}})
                with patch.object(request_bridge, "resolve_scope", return_value=self.scope), \
                     patch.object(request_bridge, "service", return_value=service), \
                     patch.object(request_bridge.transaction, "atomic", return_value=nullcontext()), \
                     patch.object(request_bridge.transaction, "on_commit",
                                  side_effect=(lambda callback: None) if deferred
                                  else (lambda callback: callback())):
                    response = request_bridge.guard_dispatch(native, "edit")(object(), request,
                                                                             page_id=7)
                data = json.loads(response.content)
                self.assertTrue(data["success"])
                self.assertTrue(data.get("_ec_guard_unavailable"))
                self.assertNotIn("_ec_ticket", data)
                self.assertNotIn("reserve", [call[0] for call in service.calls])

    def test_native_exception_marks_unknown_and_is_re_raised(self):
        from external_content import request_bridge
        from external_content.idempotency import Claim

        claim = Claim("a" * 32, {"state": "processing"}, replay=False)
        service = FakeService(claim=claim)
        native = Mock(side_effect=RuntimeError("native failed"))
        request = self.request({"title": "a", "_ec_token": "execution"})
        with patch.object(request_bridge, "resolve_scope", return_value=self.scope), \
             patch.object(request_bridge, "service", return_value=service), \
             patch.object(request_bridge, "request_digest", return_value="digest"), \
             patch.object(request_bridge.transaction, "atomic", return_value=nullcontext()), \
             self.assertRaises(RuntimeError):
            request_bridge.guard_dispatch(native, "edit")(object(), request, page_id=7)
        self.assertEqual([call[0] for call in service.calls], ["claim", "fail"])

    def test_replay_does_not_enter_native_post(self):
        from external_content import request_bridge
        from external_content.idempotency import Claim

        claim = Claim("a" * 32, {"state": "succeeded", "result": {"page": 7}},
                      replay=True)
        service = FakeService(claim=claim)
        native = Mock(return_value=HttpResponse(status=201))
        request = self.request({"title": "a", "_ec_token": "execution"})
        with patch.object(request_bridge, "resolve_scope", return_value=self.scope), \
             patch.object(request_bridge, "service", return_value=service), \
             patch.object(request_bridge, "request_digest", return_value="digest"), \
             patch.object(request_bridge, "replay_response",
                          return_value=HttpResponse(status=200)) as replay:
            response = request_bridge.guard_dispatch(native, "edit")(object(), request, page_id=7)
        self.assertEqual(response.status_code, 200)
        native.assert_not_called()
        replay.assert_called_once()

    def test_get_success_attaches_reservation_marker(self):
        from external_content import request_bridge

        request = self.request(method="GET")
        native = lambda view, request, **kwargs: HttpResponse(
            "<html><body>edit</body></html>", status=200
        )
        service = FakeService()
        with patch.object(request_bridge, "resolve_scope", return_value=self.scope), \
             patch.object(request_bridge, "service", return_value=service):
            response = request_bridge.guard_dispatch(native, "edit")(object(), request, page_id=7)
        self.assertIn("data-external-content-guard", response.content.decode())
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertIn("data-external-content-guard", response.content.decode())
        self.assertEqual(service.calls[0][0], "reserve")

    def test_unknown_template_response_never_reserves_another_attempt(self) -> None:
        """200 模板可能来自写入后的 before hook，不能自动换身份重试。"""
        from django.template import engines
        from django.template.response import TemplateResponse
        from external_content import request_bridge, wagtail_hooks
        from external_content.idempotency import IdempotencyService
        from external_content.tests.test_request_idempotency_unit import MemoryStore

        cases = (
            ("edit", False, False),
            ("edit", True, False),
            ("create", True, False),
            ("create", True, True),
        )
        for kind, already_written, deferred in cases:
            with self.subTest(kind=kind, already_written=already_written, deferred=deferred):
                path = ("/admin/pages/add/guides/guidepage/42/" if kind == "create"
                        else "/admin/pages/7/edit/")
                kwargs = ({"content_type_app_name": "guides",
                           "content_type_model_name": "guidepage", "parent_page_id": 42}
                          if kind == "create" else {"page_id": 7})
                scope = ({"kind": "create", "model": "guides.guidepage", "parent": 42}
                         if kind == "create" else self.scope)
                request = authenticated_request(RequestFactory().post(
                    path, {"title": "a", "action-publish": "Publish"}))
                store = MemoryStore()
                service = IdempotencyService(store, clock=lambda: 10000.0)
                ticket = service.reserve(3, scope)
                token = service.prepare(ticket, 3, scope, request_bridge.request_digest(request))
                request.POST = request.POST.copy()
                request.POST["_ec_token"] = token
                writes = []

                def native(view, req, **native_kwargs):
                    if already_written:
                        # 模拟已保存 Page/Revision，但 publish 尚无匹配 after hook。
                        writes.extend(["page", "revision"])
                        wagtail_hooks.capture_edit_result(req, type("Page", (), {"pk": 7})())
                    return TemplateResponse(
                        req, engines["django"].from_string("<html><body>核对结果</body></html>"))

                with patch.object(request_bridge, "resolve_scope", return_value=scope), \
                     patch.object(request_bridge, "service", return_value=service), \
                     patch.object(request_bridge.transaction, "atomic", return_value=nullcontext()), \
                     patch.object(request_bridge.transaction, "on_commit",
                                  side_effect=(lambda callback: None) if deferred
                                  else (lambda callback: callback())):
                    response = request_bridge.guard_dispatch(native, kind)(
                        object(), request, **kwargs)
                    response.render()
                self.assertEqual(response.status_code, 200)
                self.assertEqual(writes, ["page", "revision"] if already_written else [])
                self.assertEqual(len(store.records), 1, "未知旧尝试不得自动创建新预留")
                self.assertEqual(next(iter(store.records.values()))["state"],
                                 "processing" if deferred else "unknown")
                self.assertEqual(response["X-External-Content-Outcome"], "unknown")
                self.assertIn('"unavailable":true', response.content.decode())
                self.assertNotIn('"ticket":', response.content.decode())
                with self.assertRaises(request_bridge.GuardError) as caught:
                    service.claim(token, 3, scope, request_bridge.request_digest(request))
                self.assertEqual(caught.exception.code, "processing" if deferred else "unknown")


class TestCopyAuthorization(SimpleTestCase):
    """运行真实 scope/status/重放门禁，只替换数据库对象与缓存边界。"""

    def setUp(self) -> None:
        from external_content.mixins import ExternalContentPageMixin

        self.source_permissions = Mock()
        self.source_permissions.can_copy.return_value = True
        self.source_permissions.can_copy_to.return_value = True
        self.source_permissions.can_publish.return_value = False
        self.target_permissions = Mock()
        self.target_permissions.can_add_subpage.return_value = True
        self.target_permissions.can_publish_subpage.return_value = True
        self.target = Mock(pk=42)
        self.target.specific = self.target
        self.target.permissions_for_user.return_value = self.target_permissions
        self.source_parent = Mock(pk=43)
        self.source_parent.permissions_for_user.return_value = self.target_permissions
        self.source = Mock(spec=ExternalContentPageMixin)
        self.source.pk = 7
        self.source.specific = self.source
        self.source.permissions_for_user = Mock(return_value=self.source_permissions)
        self.source.get_parent = Mock(return_value=self.source_parent)
        self.lookups = []
        from django.urls import resolve
        resolve("/admin/pages/7/copy/")

    def lookup(self, model: object, *, pk: object) -> object:
        self.lookups.append(int(pk))
        return {7: self.source, 42: self.target}[int(pk)]

    def request(self, data: dict) -> object:
        return authenticated_request(RequestFactory().post("/admin/pages/7/copy/", data))

    def issued_copy(self, field: str) -> tuple:
        from external_content import request_bridge
        from external_content.idempotency import IdempotencyService
        from external_content.tests.test_request_idempotency_unit import MemoryStore

        request = self.request({"new_parent_page": "42", field: "on"})
        store = MemoryStore()
        service = IdempotencyService(store, clock=lambda: 10000.0)
        scope = {"kind": "copy", "page": 7}
        digest = request_bridge.request_digest(request)
        ticket = service.reserve(3, scope)
        token = service.prepare(ticket, 3, scope, digest,
                                authorization={"publish": True, "copy_parent": 42})
        claim = service.claim(token, 3, scope, digest)
        service.complete(claim, {"page": 9})
        request.POST = request.POST.copy()
        request.POST["_ec_token"] = token
        return service, request, token

    def test_prepare_records_native_copy_publish_fields_and_resolved_parent(self) -> None:
        from external_content import request_bridge

        for fields, publish in (({"publish_copies": "on"}, True), ({"alias": "on"}, True),
                                ({}, False), ({"publish_copies": "false", "alias": "False"}, False),
                                ({"alias": "0"}, True),
                                ({"action-publish": "Publish"}, False)):
            with self.subTest(fields=fields):
                self.target_permissions.can_publish_subpage.return_value = publish
                request = authenticated_request(RequestFactory().post(
                    "/admin/external-content/request/prepare/",
                    {"_ec_ticket": "reservation", "_ec_target": "/admin/pages/7/copy/",
                     "new_parent_page": "42", **fields}))
                service = FakeService()
                with patch.object(request_bridge, "get_object_or_404", side_effect=self.lookup), \
                     patch.object(request_bridge, "service", return_value=service):
                    response = request_bridge.prepare_view(request)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(service.calls[0][-1], {"publish": publish, "copy_parent": 42})

    def test_copy_requires_destination_publish_permission(self) -> None:
        from django.core.exceptions import PermissionDenied
        from external_content import request_bridge

        self.target_permissions.can_publish_subpage.return_value = False
        for field in ("publish_copies", "alias"):
            with self.subTest(field=field), \
                 patch.object(request_bridge, "get_object_or_404", side_effect=self.lookup):
                with self.assertRaises(PermissionDenied):
                    request_bridge.resolve_scope(
                        self.request({"new_parent_page": "42", field: "on"}),
                        "copy", {"page_id": 7})

    def test_copy_requires_source_copy_to_destination_even_without_publish(self) -> None:
        from django.core.exceptions import PermissionDenied
        from external_content import request_bridge

        self.source_permissions.can_copy_to.return_value = False
        with patch.object(request_bridge, "get_object_or_404", side_effect=self.lookup):
            with self.assertRaises(PermissionDenied):
                request_bridge.resolve_scope(self.request({"new_parent_page": "42"}),
                                             "copy", {"page_id": 7})

    def test_succeeded_copy_status_rechecks_current_destination_permissions(self) -> None:
        from external_content import request_bridge

        self.source_permissions.can_publish.return_value = True
        for field in ("publish_copies", "alias"):
            for revoked in ("publish", "copy_to"):
                with self.subTest(field=field, revoked=revoked):
                    service, _, token = self.issued_copy(field)
                    self.target_permissions.can_publish_subpage.return_value = revoked != "publish"
                    self.source_permissions.can_copy_to.return_value = revoked != "copy_to"
                    request = authenticated_request(RequestFactory().post(
                        "/admin/external-content/request/status/", {"_ec_token": token}))
                    with patch.object(request_bridge, "service", return_value=service), \
                         patch.object(request_bridge, "get_object_or_404", side_effect=self.lookup), \
                         patch.object(request_bridge, "visible_result") as visible:
                        response = request_bridge.status_view(request)
                    self.assertEqual(response.status_code, 403)
                    visible.assert_not_called()
                    self.assertNotIn("new_parent_page", request.POST)

    def test_succeeded_copy_replay_rechecks_permissions_before_returning_result(self) -> None:
        from django.core.exceptions import PermissionDenied
        from external_content import request_bridge

        for field in ("publish_copies", "alias"):
            for revoked in ("publish", "copy_to"):
                with self.subTest(field=field, revoked=revoked):
                    service, request, _ = self.issued_copy(field)
                    self.target_permissions.can_publish_subpage.return_value = revoked != "publish"
                    self.source_permissions.can_copy_to.return_value = revoked != "copy_to"
                    native = Mock(return_value=HttpResponse(status=302))
                    with patch.object(request_bridge, "service", return_value=service), \
                         patch.object(request_bridge, "get_object_or_404", side_effect=self.lookup), \
                         patch.object(request_bridge, "replay_response") as replay:
                        with self.assertRaises(PermissionDenied):
                            request_bridge.guard_dispatch(native, "copy")(
                                object(), request, page_id=7)
                    native.assert_not_called()
                    replay.assert_not_called()

    def test_copy_status_uses_recorded_parent_not_current_source_parent(self) -> None:
        from external_content import request_bridge

        service, _, token = self.issued_copy("alias")
        request = authenticated_request(RequestFactory().post(
            "/admin/external-content/request/status/", {"_ec_token": token}))
        with patch.object(request_bridge, "service", return_value=service), \
             patch.object(request_bridge, "get_object_or_404", side_effect=self.lookup), \
             patch.object(request_bridge, "visible_result"):
            response = request_bridge.status_view(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.lookups, [7, 42])
        self.source.get_parent.assert_not_called()
        self.source_permissions.can_copy_to.assert_called_once_with(self.target)

    def test_copy_status_missing_parent_is_unknown_before_any_page_lookup(self) -> None:
        from external_content import request_bridge

        service, _, token = self.issued_copy("alias")
        record = next(iter(service.store.records.values()))
        record["authorization"] = {"publish": False}
        request = authenticated_request(RequestFactory().post(
            "/admin/external-content/request/status/", {"_ec_token": token}))
        with patch.object(request_bridge, "service", return_value=service), \
             patch.object(request_bridge, "get_object_or_404", side_effect=self.lookup), \
             patch.object(request_bridge, "visible_result"):
            response = request_bridge.status_view(request)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.content)["error"], "external_content_unknown")
        self.assertEqual(self.lookups, [])


class TestBridgeInstallation(SimpleTestCase):
    def test_all_six_native_page_views_are_guarded(self):
        from wagtail.admin.views.pages.convert_alias import ConvertAliasView
        from wagtail.admin.views.pages.copy import CopyView
        from wagtail.admin.views.pages.create import CreateView
        from wagtail.admin.views.pages.delete import DeleteView
        from wagtail.admin.views.pages.edit import EditView
        from wagtail.admin.views.pages.unpublish import UnpublishView

        for view_class in (CreateView, EditView, CopyView, DeleteView,
                           UnpublishView, ConvertAliasView):
            with self.subTest(view=view_class.__name__):
                self.assertTrue(
                    getattr(view_class.dispatch, "_external_content_request_guard", False)
                )
