from pathlib import Path

from django.test import SimpleTestCase


class HomeCarouselContractTests(SimpleTestCase):
    def test_home_carousel_uses_post_theme_styles_and_responsive_guards(self):
        app_root = Path(__file__).resolve().parents[3]
        template = (app_root / "templates/home/home_page.html").read_text()
        stylesheet = (app_root / "static/css/home-carousel.css").read_text()

        self.assertIn("extra_css_after_site_theme", template)
        self.assertIn("home-carousel.css", template)
        self.assertIn("?v=20260825-1", template)
        self.assertIn("aspect-ratio: 4 / 3", stylesheet)
        self.assertIn("-webkit-line-clamp: 2", stylesheet)
        self.assertIn("overflow-wrap: anywhere", stylesheet)
        self.assertIn("prefers-reduced-motion: reduce", stylesheet)
