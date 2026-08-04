"""Browser-compatible Wagtail image renditions used by rich text and Vditor."""

from wagtail.images.formats import Format, register_image_format


register_image_format(
    Format(
        "fullwidth_web",
        "全宽（网页兼容）",
        "richtext-image full-width",
        "width-800|format-jpeg",
    )
)
