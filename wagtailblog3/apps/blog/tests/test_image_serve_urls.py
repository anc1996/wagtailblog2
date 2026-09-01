from django.test import SimpleTestCase
from django.urls import resolve


class ImageServeUrlTests(SimpleTestCase):
    def test_imported_image_url_accepts_nested_storage_path(self):
        match = resolve(
            "/zh-hans/images/signature/548/original/"
            "8de9e8a9f715454c841e581a7ac3475e/photo.jpg"
        )

        self.assertEqual(match.url_name, "wagtailimages_serve")
        self.assertEqual(match.args, ("signature", "548", "original"))

    def test_legacy_flat_image_url_remains_supported(self):
        match = resolve(
            "/zh-hans/images/signature/240/original/image.png"
        )

        self.assertEqual(match.url_name, "wagtailimages_serve")
        self.assertEqual(match.args, ("signature", "240", "original"))
