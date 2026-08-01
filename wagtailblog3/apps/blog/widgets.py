from django import forms
from django.conf import settings
from wagtail.admin.staticfiles import versioned_static


VDITOR_ADMIN_ASSET_VERSION = "20260801.4"


def vditor_admin_static(path):
    url = versioned_static(path)
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}blog_vditor={VDITOR_ADMIN_ASSET_VERSION}"


class VditorMarkdownWidget(forms.Textarea):
    """Admin widget that keeps a plain Markdown textarea as the source of truth."""

    template_name = "blog/widgets/vditor_markdown.html"

    def build_attrs(self, base_attrs=None, extra_attrs=None):
        attrs = super().build_attrs(base_attrs, extra_attrs)
        attrs["data-vditor-markdown"] = "true"
        attrs["data-vditor-cdn"] = (
            f"{settings.STATIC_URL.rstrip('/')}/vendor/vditor"
        )
        attrs["data-vditor-mode"] = "sv"
        attrs["data-vditor-locale"] = "zh_CN"
        attrs["class"] = " ".join(
            value
            for value in (attrs.get("class", ""), "blog-vditor-source")
            if value
        )
        return attrs

    @property
    def media(self):
        return forms.Media(
            css={
                "all": (
                    versioned_static("vendor/vditor/dist/index.css"),
                    vditor_admin_static("blog/css/vditor_admin.css"),
                )
            },
            js=(
                versioned_static("vendor/vditor/dist/index.min.js"),
                vditor_admin_static("blog/js/vditor_markdown.js"),
            ),
        )
