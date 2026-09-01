"""Tests for the Vditor Wagtail-admin image upload contract."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import reverse
from wagtail.admin.rich_text.editors.draftail import DraftailRichTextArea

from blog.admin_image_upload import _safe_upload_title, _upload_vditor_image
from blog.wagtail_hooks import editor_js


class VditorImageUploadTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = SimpleNamespace(
            is_authenticated=True,
            is_active=True,
            is_anonymous=False,
            has_perms=lambda permissions: True,
        )

    def make_request(self, **data):
        upload = SimpleUploadedFile(
            "clipboard.png",
            b"not-read-by-mocked-form",
            content_type="image/png",
        )
        payload = {"file": upload, "format": "fullwidth_web", **data}
        request = self.factory.post("/admin/blog/vditor/images/upload/", payload)
        request.user = self.user
        return request

    def call_view_logic(self, request):
        return _upload_vditor_image(request)

    def test_admin_url_is_registered(self):
        self.assertEqual(
            reverse("blog_vditor_image_upload"),
            "/admin/blog/vditor/images/upload/",
        )

    def test_anonymous_request_is_rejected_by_admin_access(self):
        response = self.client.post(reverse("blog_vditor_image_upload"))

        self.assertEqual(response.status_code, 302)

    @override_settings(WAGTAILIMAGES_MAX_UPLOAD_SIZE=12345)
    def test_editor_js_configures_rich_text_clipboard_upload(self):
        with patch(
            "blog.wagtail_hooks.static",
            side_effect=lambda path: "/static/" + path,
        ):
            markup = str(editor_js())

        self.assertIn("blog/js/rich_text_image_paste.js", markup)
        self.assertIn(
            'data-upload-url="/admin/blog/vditor/images/upload/"',
            markup,
        )
        self.assertIn('data-max-image-size="12345"', markup)

    def test_draftail_image_entity_serializes_as_left_decorative_embed(self):
        raw_content_state = json.dumps(
            {
                "blocks": [
                    {
                        "key": "image1",
                        "text": " ",
                        "type": "atomic",
                        "depth": 0,
                        "inlineStyleRanges": [],
                        "entityRanges": [{"offset": 0, "length": 1, "key": 0}],
                        "data": {},
                    }
                ],
                "entityMap": {
                    "0": {
                        "type": "IMAGE",
                        "mutability": "IMMUTABLE",
                        "data": {
                            "id": 43,
                            "src": "/media/renditions/left.jpg",
                            "alt": "",
                            "format": "left",
                        },
                    }
                },
            }
        )
        widget = DraftailRichTextArea(features=["image"])

        value = widget.value_from_datadict(
            {"rich_text": raw_content_state},
            {},
            "rich_text",
        )

        self.assertIn('embedtype="image"', value)
        self.assertIn('format="left"', value)
        self.assertIn('alt=""', value)

    def test_add_permission_is_required(self):
        request = self.make_request()
        with patch(
            "blog.admin_image_upload.permission_policy.user_has_permission",
            return_value=False,
        ):
            response = self.call_view_logic(request)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(json.loads(response.content)["error"]["code"], "image_add_forbidden")

    @override_settings(BLOG_VDITOR_IMAGE_UPLOAD_COLLECTION_ID=None)
    def test_multiple_collections_require_explicit_configuration(self):
        request = self.make_request()
        collections = MagicMock()
        collections.count.return_value = 2
        with (
            patch(
                "blog.admin_image_upload.permission_policy.user_has_permission",
                return_value=True,
            ),
            patch(
                "blog.admin_image_upload.permission_policy.collections_user_has_permission_for",
                return_value=collections,
            ),
            patch("blog.admin_image_upload.get_image_format"),
        ):
            response = self.call_view_logic(request)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.content)["error"]["code"], "collection_required")

    @override_settings(BLOG_VDITOR_IMAGE_UPLOAD_COLLECTION_ID=7)
    def test_success_uses_wagtail_form_and_returns_structured_image_data(self):
        request = self.make_request(alt="diagram")
        collection = SimpleNamespace(pk=7)
        collections = MagicMock()
        collections.filter.return_value.first.return_value = collection
        image_format = SimpleNamespace(
            name="fullwidth_web", filter_spec="width-800|format-jpeg"
        )
        rendition = SimpleNamespace(
            url="/media/renditions/current.jpg", width=800, height=450
        )
        saved_image = SimpleNamespace(
            pk=42,
            title="clipboard",
            get_rendition=MagicMock(return_value=rendition),
        )
        form = MagicMock()
        form.is_valid.return_value = True
        form.save.return_value = saved_image
        form_class = MagicMock(return_value=form)
        image_instance = SimpleNamespace()
        image_model = MagicMock(return_value=image_instance)
        image_model.objects.filter.return_value.exists.return_value = False

        with (
            patch(
                "blog.admin_image_upload.permission_policy.user_has_permission",
                return_value=True,
            ),
            patch(
                "blog.admin_image_upload.permission_policy.collections_user_has_permission_for",
                return_value=collections,
            ),
            patch(
                "blog.admin_image_upload.get_image_format",
                return_value=image_format,
            ),
            patch(
                "blog.admin_image_upload.get_image_model",
                return_value=image_model,
            ),
            patch(
                "blog.admin_image_upload.get_image_form",
                return_value=form_class,
            ),
        ):
            response = self.call_view_logic(request)

        self.assertEqual(response.status_code, 201)
        payload = json.loads(response.content)
        self.assertEqual(payload["image"]["id"], 42)
        self.assertEqual(payload["image"]["alt"], "diagram")
        self.assertEqual(payload["preview"]["url"], "/media/renditions/current.jpg")
        image_model.assert_called_once_with(uploaded_by_user=self.user)
        form_kwargs = form_class.call_args.kwargs
        self.assertIs(form_kwargs["instance"], image_instance)
        self.assertIs(form_kwargs["user"], self.user)
        self.assertEqual(form_kwargs["data"]["collection"], "7")
        self.assertEqual(form_kwargs["data"]["title"], "clipboard")
        saved_image.get_rendition.assert_called_once_with(
            "width-800|format-jpeg"
        )

    @override_settings(BLOG_VDITOR_IMAGE_UPLOAD_COLLECTION_ID=7)
    def test_rich_text_upload_uses_left_format_and_decorative_alt(self):
        request = self.make_request(format="left", alt="")
        collection = SimpleNamespace(pk=7)
        collections = MagicMock()
        collections.filter.return_value.first.return_value = collection
        image_format = SimpleNamespace(name="left", filter_spec="width-500")
        rendition = SimpleNamespace(
            url="/media/renditions/left.jpg", width=500, height=281
        )
        saved_image = SimpleNamespace(
            pk=43,
            title="clipboard",
            get_rendition=MagicMock(return_value=rendition),
        )
        form = MagicMock()
        form.is_valid.return_value = True
        form.save.return_value = saved_image
        form_class = MagicMock(return_value=form)
        image_model = MagicMock(return_value=SimpleNamespace())
        image_model.objects.filter.return_value.exists.return_value = False

        with (
            patch(
                "blog.admin_image_upload.permission_policy.user_has_permission",
                return_value=True,
            ),
            patch(
                "blog.admin_image_upload.permission_policy.collections_user_has_permission_for",
                return_value=collections,
            ),
            patch(
                "blog.admin_image_upload.get_image_format",
                return_value=image_format,
            ) as get_image_format_mock,
            patch(
                "blog.admin_image_upload.get_image_model",
                return_value=image_model,
            ),
            patch(
                "blog.admin_image_upload.get_image_form",
                return_value=form_class,
            ),
        ):
            response = self.call_view_logic(request)

        self.assertEqual(response.status_code, 201)
        payload = json.loads(response.content)
        self.assertEqual(payload["image"]["alt"], "")
        self.assertEqual(payload["image"]["format"], "left")
        get_image_format_mock.assert_called_once_with("left")
        saved_image.get_rendition.assert_called_once_with("width-500")

    def test_generated_title_uses_filename_without_extension(self):
        image_model = MagicMock()
        image_model.objects.filter.return_value.exists.return_value = False
        upload = SimpleNamespace(name="architecture-diagram.tiff")

        self.assertEqual(
            _safe_upload_title(upload, image_model),
            "architecture-diagram",
        )

    @patch("blog.admin_image_upload._random_title_suffix", return_value="123456789")
    def test_generated_title_uses_random_number_without_filename(self, random_suffix):
        image_model = MagicMock()
        image_model.objects.filter.return_value.exists.return_value = False
        upload = SimpleNamespace(name="")

        self.assertEqual(
            _safe_upload_title(upload, image_model),
            "pasted-image-123456789",
        )
        random_suffix.assert_called_once_with()

    @patch("blog.admin_image_upload._random_title_suffix", return_value="987654321")
    def test_generated_title_adds_random_number_when_title_exists(self, random_suffix):
        image_model = MagicMock()
        image_model.objects.filter.return_value.exists.side_effect = [True, False]
        upload = SimpleNamespace(name="architecture-diagram.png")

        self.assertEqual(
            _safe_upload_title(upload, image_model),
            "architecture-diagram-987654321",
        )
        random_suffix.assert_called_once_with()
        self.assertEqual(
            [call.kwargs["title"] for call in image_model.objects.filter.call_args_list],
            ["architecture-diagram", "architecture-diagram-987654321"],
        )

    @override_settings(BLOG_VDITOR_IMAGE_UPLOAD_COLLECTION_ID=7)
    def test_configured_collection_must_be_writable(self):
        request = self.make_request()
        collections = MagicMock()
        collections.filter.return_value.first.return_value = None
        with (
            patch(
                "blog.admin_image_upload.permission_policy.user_has_permission",
                return_value=True,
            ),
            patch(
                "blog.admin_image_upload.permission_policy.collections_user_has_permission_for",
                return_value=collections,
            ),
            patch("blog.admin_image_upload.get_image_format"),
        ):
            response = self.call_view_logic(request)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(json.loads(response.content)["error"]["code"], "collection_forbidden")
