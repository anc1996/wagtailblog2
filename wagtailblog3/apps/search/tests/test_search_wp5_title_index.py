from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from search.services.title_index import (
    build_title_suggestion_index_template,
    create_title_suggestion_index,
    default_title_suggestion_index_name,
)


class TitleSuggestionIndexTests(SimpleTestCase):
    def test_mapping_is_strict_and_small(self):
        index_name = default_title_suggestion_index_name("wagtailblog-test-content")
        definition = build_title_suggestion_index_template(index_name)
        properties = definition["template"]["mappings"]["properties"]
        self.assertEqual(definition["template"]["mappings"]["dynamic"], "strict")
        self.assertEqual(set(properties), {"page_id", "title", "locale_id", "searchable", "popularity"})
        self.assertNotIn("body_text", properties)

    @override_settings(CONTENT_SEARCH_INDEX_PREFIX="wagtailblog-test-content")
    def test_creation_validates_the_resulting_mapping(self):
        client = Mock()
        client.indices.exists.return_value = False
        client.indices.get_mapping.return_value = {
            "wagtailblog-test-content-title-v001": {
                "mappings": {
                    "dynamic": "strict",
                    "properties": {
                        "page_id": {}, "title": {}, "locale_id": {},
                        "searchable": {}, "popularity": {},
                    },
                }
            }
        }
        with patch(
            "search.services.title_index.get_content_search_client_for_connection",
            return_value=client,
        ):
            result = create_title_suggestion_index(
                "default", "wagtailblog-test-content-title-v001"
            )
        self.assertTrue(result.index_created)
        client.indices.create.assert_called_once()

    @override_settings(
        CONTENT_SEARCH_INDEX_PREFIX="wagtailblog-test-content",
        CONTENT_SEARCH_TITLE_SUGGESTIONS_READ_ALIAS="wagtailblog-test-content-title-read",
    )
    def test_backfill_command_is_dry_run_by_default(self):
        from io import StringIO
        from django.core.management import call_command

        with patch("search.management.commands.search_backfill_title_suggestions.BlogPage.objects.live") as live:
            live.return_value.public.return_value.order_by.return_value.count.return_value = 117
            output = StringIO()
            call_command("search_backfill_title_suggestions", stdout=output)

        self.assertIn('"dry_run": true', output.getvalue())
        self.assertIn('"written": 0', output.getvalue())
