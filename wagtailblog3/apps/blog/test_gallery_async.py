import json
from unittest.mock import Mock, patch

from django.test import RequestFactory, SimpleTestCase

from blog.views import gallery_items_api


class GalleryItemsApiTests(SimpleTestCase):
    """验证公开画廊接口的分页、方法和公开页面边界。"""

    def setUp(self):
        self.factory = RequestFactory()
        self.page = Mock(pk=38)

    def test_rejects_non_get_requests(self):
        response = gallery_items_api(self.factory.post('/gallery/38/items/'), 38)

        self.assertEqual(response.status_code, 405)
        self.assertEqual(response['Allow'], 'GET')

    @patch('blog.views.BlogPage.objects')
    def test_returns_404_for_non_public_page(self, page_manager):
        page_manager.live.return_value.public.return_value.filter.return_value.first.return_value = None

        response = gallery_items_api(self.factory.get('/gallery/999/items/?page=2'), 999)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(json.loads(response.content)['error']['code'], 'blog_page_not_found')

    @patch('blog.views.render_to_string', return_value='<figure class="gallery-item"></figure>' * 8)
    @patch('blog.views.BlogPageGalleryImage.objects')
    @patch('blog.views.BlogPage.objects')
    def test_returns_at_most_eight_sorted_items_per_page(
        self,
        page_manager,
        gallery_manager,
        render_fragment,
    ):
        page_manager.live.return_value.public.return_value.filter.return_value.first.return_value = self.page
        gallery_manager.filter.return_value.select_related.return_value.order_by.return_value = list(range(17))

        response = gallery_items_api(self.factory.get('/gallery/38/items/?page=2'), 38)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['data']['page'], 2)
        self.assertEqual(payload['data']['loaded_count'], 16)
        self.assertEqual(payload['data']['total'], 17)
        self.assertTrue(payload['data']['has_next'])
        render_fragment.assert_called_once()
        self.assertEqual(render_fragment.call_args.kwargs['request'].GET['page'], '2')
        self.assertEqual(len(render_fragment.call_args.args[1]['gallery_images']), 8)
        self.assertEqual(render_fragment.call_args.args[1]['gallery_offset'], 8)
