# Vditor Markdown 后台控件
from django import forms
from django.conf import settings
from django.urls import reverse
from wagtail.admin.staticfiles import versioned_static


VDITOR_ADMIN_ASSET_VERSION = "20260819.1"


def vditor_admin_static(path):
    # 用独立版本参数缓存静态资源；只要 JS/CSS 改动，后台页面即可绕过旧缓存。
    url = versioned_static(path)
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}blog_vditor={VDITOR_ADMIN_ASSET_VERSION}"


class VditorMarkdownWidget(forms.Textarea):
    """使用隐藏 Markdown 文本域作为唯一提交来源的后台控件。"""

    template_name = "blog/widgets/vditor_markdown.html"

    def build_attrs(self, base_attrs=None, extra_attrs=None):
        # Vditor 只负责可视化编辑，所有动态块操作最终都写回这个文本域。
        attrs = super().build_attrs(base_attrs, extra_attrs)
        attrs["data-vditor-markdown"] = "true"
        attrs["data-vditor-cdn"] = (
            f"{settings.STATIC_URL.rstrip('/')}/vendor/vditor"
        )
        attrs["data-vditor-mode"] = "sv"
        attrs["data-vditor-locale"] = "zh_CN"
        attrs["data-vditor-page-chooser-url"] = reverse(
            "wagtailadmin_choose_page"
        )
        attrs["data-vditor-image-chooser-url"] = (
            f'{reverse("wagtailimages_chooser:choose")}?select_format=true'
        )
        attrs["data-vditor-image-upload-url"] = reverse(
            "blog_vditor_image_upload"
        )
        attrs["data-vditor-max-image-size"] = str(
            getattr(settings, "WAGTAILIMAGES_MAX_UPLOAD_SIZE", 10 * 1024 * 1024)
        )
        attrs["class"] = " ".join(
            value
            for value in (attrs.get("class", ""), "blog-vditor-source")
            if value
        )
        return attrs

    @property
    def media(self):
        # 资源路径统一经过 Wagtail 版本化处理，并附加项目自己的缓存版本号。
        return forms.Media(
            css={
                "all": (
                    versioned_static("vendor/vditor/dist/index.css"),
                    vditor_admin_static("blog/css/vditor_admin.css"),
                )
            },
            js=(
                versioned_static("wagtailadmin/js/page-chooser-modal.js"),
                versioned_static("wagtailimages/js/image-chooser-modal.js"),
                versioned_static("vendor/vditor/dist/index.min.js"),
                vditor_admin_static("blog/js/vditor_markdown.js"),
            ),
        )
