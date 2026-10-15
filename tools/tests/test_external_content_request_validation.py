"""原生校验返回页的防重回归；不创建测试库或访问外部存储。"""

import copy
import json
import re
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from django import forms
from django.contrib.messages.storage.cookie import CookieStorage
from django.http import HttpResponse
from django.template import engines
from django.template.response import TemplateResponse
from django.test import RequestFactory, SimpleTestCase
from wagtail.admin.views.pages.create import CreateView
from wagtail.admin.views.pages.edit import EditView

from external_content import request_bridge
from external_content.idempotency import GuardError, IdempotencyService
from external_content.idempotency_store import StoreUnavailable


class MemoryRecords:
    """仅替换 Redis IO，仍运行真实签名、参数绑定和 CAS 状态机。"""

    def __init__(self):
        self.records = {}
        self.reject_finish = False
        self.reject_reserve = False

    def create(self, identity, record, now):
        if self.reject_reserve:
            raise StoreUnavailable()
        if identity in self.records:
            return False
        self.records[identity] = copy.deepcopy(record)
        return True

    def read(self, identity):
        return copy.deepcopy(self.records.get(identity))

    def cas(self, identity, expected, replacement, now):
        if self.reject_finish and expected["state"] == "processing":
            return False
        if self.records.get(identity) != expected:
            return False
        self.records[identity] = copy.deepcopy(replacement)
        return True


class RequiredStepForm(forms.Form):
    """轻量输入夹具；真实运行 Wagtail post/form_invalid，不替代桥接行为。"""

    title = forms.CharField()
    step = forms.CharField()

    def __init__(self, *args, **kwargs):
        for name in ("instance", "subscription", "parent_page", "for_user"):
            kwargs.pop(name, None)
        super().__init__(*args, **kwargs)
        self.formsets = {}

    def defer_required_fields(self):
        pass

    def restore_required_fields(self):
        pass


class RenderFormMixin:
    form_class = RequiredStepForm

    def get_context_data(self):
        return {"form": self.form}

    def render_to_response(self, context):
        template = engines["django"].from_string("<html><body>{{ form.as_p }}</body></html>")
        return TemplateResponse(self.request, template, context)

    def publish_action(self):
        # 动作夹具只记录入口次数，不进行 Page/Revision/Mongo 写入。
        self.saved.append(dict(self.form.cleaned_data))
        self.request._ec_completed_page_id = 4321
        return HttpResponse(status=302, headers={"Location": "/saved/"})


class CreateFormView(RenderFormMixin, CreateView):
    pass


class EditFormView(RenderFormMixin, EditView):
    pass


class RequestValidationTests(SimpleTestCase):
    """缺失校验返回身份将使补填无法准备；错误放行则会破坏未知结果保护。"""

    def setUp(self):
        self.factory = RequestFactory()
        self.store = MemoryRecords()
        self.service = IdempotencyService(self.store)
        self.saved = []
        self.scope = {"kind": "create", "model": "guides.guidepage", "parent": 1292}
        self.path = "/admin/pages/add/guides/guidepage/1292/"
        self.kwargs = {
            "content_type_app_name": "guides",
            "content_type_model_name": "guidepage",
            "parent_page_id": 1292,
        }
        self.enterContext(patch.object(request_bridge, "service", return_value=self.service))
        self.enterContext(patch.object(request_bridge, "resolve_scope", side_effect=lambda *a: self.scope))
        # SimpleTestCase 禁止 SQL；只替换事务调度，保留真实视图校验和防重逻辑。
        self.enterContext(patch.object(request_bridge.transaction, "atomic", side_effect=lambda: nullcontext()))
        self.enterContext(patch.object(request_bridge.transaction, "on_commit", side_effect=lambda fn: fn()))

    def use_edit(self):
        self.scope = {"kind": "edit", "page": 1293}
        self.path = "/admin/pages/1293/edit/"
        self.kwargs = {"page_id": 1293}

    def request(self, data, *, json_response=False):
        request = self.factory.post(
            self.path, data, HTTP_ACCEPT="application/json" if json_response else "text/html"
        )
        request.user = SimpleNamespace(pk=7)
        request._messages = CookieStorage(request)
        return request

    def prepare(self, data, ticket=None, *, json_response=False):
        request = self.request(data, json_response=json_response)
        ticket = ticket or self.service.reserve(7, self.scope)
        token = self.service.prepare(ticket, 7, self.scope, request_bridge.request_digest(request))
        return self.request(dict(data, _ec_token=token), json_response=json_response), token

    def view(self, request):
        view_class = CreateFormView if self.scope["kind"] == "create" else EditFormView
        view = view_class()
        view.request = request
        view.page = SimpleNamespace(alias_of_id=None)
        view.parent_page = view.parent = view.subscription = None
        view.workflow_state = None
        view.locked_for_user = False
        view.parent_page_perms = SimpleNamespace(can_publish_subpage=lambda: True)
        view.page_perms = SimpleNamespace(can_publish=lambda: True)
        view.saved = self.saved
        return view

    def dispatch(self, request, native=None):
        native = native or (lambda view, request, **kwargs: view.post(request))
        return request_bridge.guard_dispatch(native, self.scope["kind"])(
            self.view(request), request, **self.kwargs
        )

    def config(self, response):
        response.render()
        marker = re.search(
            r'<script[^>]*data-external-content-guard[^>]*>(.*?)</script>',
            response.content.decode(),
        )
        self.assertIsNotNone(marker)
        return json.loads(marker.group(1))

    def assert_validation_can_resubmit(self):
        data = {"title": "保留用户输入", "step": "", "action-publish": "1"}
        request, old_token = self.prepare(data)
        response = self.dispatch(request)
        config = self.config(response)
        self.assertNotIn("X-External-Content-Outcome", response)
        self.assertIn("ticket", config)
        self.assertContains(response, "保留用户输入")
        self.assertTrue(response.context_data["form"].errors["step"])
        self.assertEqual(self.saved, [])
        self.assertEqual(self.service.status(old_token, 7)["state"], "failed")

        corrected = dict(data, step="填写后的操作步骤")
        request, new_token = self.prepare(corrected, config["ticket"])
        response = self.dispatch(request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.saved, [{"title": "保留用户输入", "step": "填写后的操作步骤"}])
        self.assertEqual(self.service.status(new_token, 7)["state"], "succeeded")
        # 补填必须使用新身份；原身份不能被重新绑定到另一份正文。
        with self.assertRaises(GuardError):
            self.service.prepare(config["ticket"], 7, self.scope, "changed")

    def test_create_validation_error_preserves_input_and_allows_corrected_submit(self):
        self.assert_validation_can_resubmit()

    def test_edit_validation_error_preserves_input_and_allows_corrected_submit(self):
        self.use_edit()
        self.assert_validation_can_resubmit()

    def test_arbitrary_template_response_remains_unknown(self):
        request, token = self.prepare({"title": "标题", "action-publish": "1"})
        response = self.dispatch(
            request, lambda view, request, **kwargs: view.render_to_response({})
        )
        self.assertEqual(self.config(response), {"unavailable": True})
        self.assertEqual(self.service.status(token, 7)["state"], "unknown")

    def test_validation_registration_failure_does_not_issue_new_identity(self):
        request, token = self.prepare({"title": "标题", "action-publish": "1"})
        self.store.reject_finish = True
        response = self.dispatch(request)
        self.assertEqual(self.config(response), {"unavailable": True})
        self.assertEqual(self.service.status(token, 7)["state"], "processing")
        self.assertEqual(self.saved, [])

    def test_new_ticket_storage_failure_remains_blocked(self):
        request, _ = self.prepare({"title": "标题", "action-publish": "1"})
        self.store.reject_reserve = True
        self.assertEqual(self.config(self.dispatch(request)), {"unavailable": True})

    def test_locked_edit_is_not_reclassified_as_validation_rejection(self):
        self.use_edit()
        request, token = self.prepare({"title": "标题", "action-publish": "1"})

        def native(view, request, **kwargs):
            view.locked_for_user = True
            return view.post(request)

        self.assertEqual(self.config(self.dispatch(request, native)), {"unavailable": True})
        self.assertEqual(self.service.status(token, 7)["state"], "unknown")

    def test_form_invalid_after_valid_path_remains_unknown(self):
        request, token = self.prepare({"title": "标题", "step": "步骤", "action-publish": "1"})

        def native(view, request, **kwargs):
            view.post(request)
            view.form = RequiredStepForm({"title": "标题"})
            return view.form_invalid(view.form)

        response = self.dispatch(request, native)
        self.assertEqual(self.config(response), {"unavailable": True})
        self.assertEqual(self.service.status(token, 7)["state"], "unknown")
        self.assertEqual(len(self.saved), 1)

    def test_replaced_validation_response_remains_unknown(self):
        request, token = self.prepare({"title": "标题", "action-publish": "1"})

        def native(view, request, **kwargs):
            view.post(request)
            return view.render_to_response({})

        self.assertEqual(self.config(self.dispatch(request, native)), {"unavailable": True})
        self.assertEqual(self.service.status(token, 7)["state"], "unknown")

    def test_late_validation_registration_remains_unknown(self):
        request, token = self.prepare({"title": "标题", "action-publish": "1"})
        after_deadline = self.service.clock() + 301

        def native(view, request, **kwargs):
            response = view.post(request)
            self.service.clock = lambda: after_deadline
            return response

        self.assertEqual(self.config(self.dispatch(request, native)), {"unavailable": True})
        self.assertEqual(self.service.status(token, 7)["state"], "unknown")

    def test_uncommitted_validation_does_not_issue_new_identity(self):
        request, token = self.prepare({"title": "标题", "action-publish": "1"})
        callbacks = []
        with patch.object(request_bridge.transaction, "on_commit", side_effect=callbacks.append):
            response = self.dispatch(request)
        self.assertEqual(self.config(response), {"unavailable": True})
        self.assertEqual(self.service.status(token, 7)["state"], "processing")
        self.assertEqual(len(callbacks), 1)

    def test_repeated_install_does_not_stack_wrappers(self):
        methods = (CreateView.dispatch, CreateView.form_valid, CreateView.form_invalid,
                   EditView.dispatch, EditView.form_valid, EditView.form_invalid)
        request_bridge.install_request_bridge()
        self.assertEqual(methods, (
            CreateView.dispatch, CreateView.form_valid, CreateView.form_invalid,
            EditView.dispatch, EditView.form_valid, EditView.form_invalid,
        ))

    def test_nonexternal_native_response_is_unchanged(self):
        request = self.request({"title": "标题", "action-publish": "1"})
        expected = HttpResponse("原生响应")
        with patch.object(request_bridge, "resolve_scope", return_value=None):
            response = self.dispatch(request, lambda *args, **kwargs: expected)
        self.assertIs(response, expected)
        self.assertEqual(self.store.records, {})

    def test_exception_keeps_unknown_and_does_not_reexecute(self):
        request, token = self.prepare({"title": "标题", "step": "步骤"})

        def native(view, request, **kwargs):
            raise RuntimeError("isolated failure")

        with self.assertRaisesRegex(RuntimeError, "isolated failure"):
            self.dispatch(request, native)
        self.assertEqual(self.service.status(token, 7)["state"], "unknown")
        self.assertEqual(self.dispatch(request, native).status_code, 409)

    def test_json_validation_remains_conservative_without_new_ticket(self):
        request, token = self.prepare({"title": "标题", "action-publish": "1"}, json_response=True)
        response = self.dispatch(request)
        self.assertEqual(json.loads(response.content)["error_code"], "validation_error")
        self.assertNotIn("_ec_ticket", json.loads(response.content))
        self.assertEqual(self.service.status(token, 7)["state"], "unknown")
