"""Compatibility tests for source image formats enabled project-wide."""

from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from PIL import Image as PillowImage
from wagtail.images.fields import WagtailImageField
from wagtail.images.models import Filter

from blog.models import BlogImage


class ExtendedImageFormatTests(SimpleTestCase):
    formats = (
        ("bmp", "BMP", "image/bmp", "png"),
        ("tiff", "TIFF", "image/tiff", "jpeg"),
        ("heic", "HEIF", "image/heic", "jpeg"),
    )

    @staticmethod
    def make_image_file(extension, pillow_format, content_type):
        output = BytesIO()
        PillowImage.new("RGB", (4, 3), (30, 60, 90)).save(
            output, format=pillow_format
        )
        return SimpleUploadedFile(
            f"format-test.{extension}",
            output.getvalue(),
            content_type=content_type,
        )

    def test_wagtail_upload_field_accepts_extended_formats(self):
        field = WagtailImageField()

        for extension, pillow_format, content_type, expected_output in self.formats:
            with self.subTest(extension=extension):
                uploaded_file = self.make_image_file(
                    extension, pillow_format, content_type
                )
                cleaned_file = field.clean(uploaded_file)

                self.assertEqual(cleaned_file.image.format_name, extension)

    def test_standard_renditions_use_browser_compatible_formats(self):
        for extension, pillow_format, content_type, expected_output in self.formats:
            with self.subTest(extension=extension):
                uploaded_file = self.make_image_file(
                    extension, pillow_format, content_type
                )
                image = BlogImage(
                    title="format-test",
                    file=uploaded_file,
                    width=4,
                    height=3,
                    collection_id=1,
                )

                rendition_file = Filter("width-2").run(image, BytesIO())

                self.assertEqual(rendition_file.format_name, expected_output)

    def test_web_fullwidth_rendition_is_always_jpeg(self):
        for extension, pillow_format, content_type, expected_output in self.formats:
            with self.subTest(extension=extension):
                uploaded_file = self.make_image_file(
                    extension, pillow_format, content_type
                )
                image = BlogImage(
                    title="format-test",
                    file=uploaded_file,
                    width=4,
                    height=3,
                    collection_id=1,
                )

                rendition_file = Filter(
                    "width-2|format-jpeg"
                ).run(image, BytesIO())

                self.assertEqual(rendition_file.format_name, "jpeg")
