from datetime import timedelta

from django.contrib.auth import get_user_model
from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from blog.models import MarkdownImportToken
from blog.services.markdown_import_auth import MarkdownImportTokenAuthentication
from blog.admin import MarkdownImportTokenCreateView, MarkdownImportTokenSnippetViewSet


class MarkdownImportTokenAuthenticationTests(TestCase):
    def test_issued_token_authenticates_active_user(self):
        user = get_user_model()(username="token-user", is_active=True)
        user.set_unusable_password()
        user.save()
        token = MarkdownImportToken(
            name="test",
            user=user,
            scopes=["markdown_import"],
            expires_at=timezone.now() + timedelta(hours=1),
        )
        plaintext = token.issue_plaintext()
        token.save()
        request = APIRequestFactory().get(
            "/blog/api/markdown-import/limits/",
            HTTP_AUTHORIZATION=f"Bearer {plaintext}",
        )
        result = MarkdownImportTokenAuthentication().authenticate(request)
        self.assertIsNotNone(result)
        self.assertEqual(result[0].pk, user.pk)

    def test_revoked_token_is_rejected(self):
        user = get_user_model()(username="revoked-user", is_active=True)
        user.save()
        token = MarkdownImportToken(
            name="test",
            user=user,
            scopes=["markdown_import"],
            revoked_at=timezone.now(),
        )
        plaintext = token.issue_plaintext()
        token.save()
        request = APIRequestFactory().get(
            "/blog/api/markdown-import/limits/",
            HTTP_AUTHORIZATION=f"Bearer {plaintext}",
        )
        self.assertIsNone(MarkdownImportTokenAuthentication().authenticate(request))


@override_settings(
    STORAGES={
        **settings.STORAGES,
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }
)
class MarkdownImportTokenSnippetTests(TestCase):
    def test_token_uses_wagtail_snippet_urls(self):
        user = get_user_model().objects.create_superuser(
            username="token-snippet-admin",
            password="test-password",
            email="token@example.com",
        )
        self.client.force_login(user)
        response = self.client.get(reverse("wagtailsnippets_blog_markdownimporttoken:list"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("Markdown 导入 Token", response.content.decode())

    def test_create_view_is_wired_and_internal_fields_are_hidden(self):
        self.assertIs(MarkdownImportTokenSnippetViewSet.add_view_class, MarkdownImportTokenCreateView)
        self.assertFalse(MarkdownImportTokenSnippetViewSet.copy_view_enabled)
        self.assertEqual(
            [panel.field_name for panel in MarkdownImportTokenSnippetViewSet.panels],
            ["name", "expires_at"],
        )

    def test_create_generates_token_for_current_user(self):
        user = get_user_model().objects.create_superuser(
            username="token-create-admin",
            password="test-password",
            email="token-create@example.com",
        )
        self.client.force_login(user)
        response = self.client.post(
            reverse("wagtailsnippets_blog_markdownimporttoken:add"),
            {"name": "client token", "expires_at": ""},
        )
        self.assertEqual(response.status_code, 302)
        token = MarkdownImportToken.objects.get(name="client token")
        self.assertEqual(token.user_id, user.pk)
        self.assertTrue(token.token_hash)
        self.assertTrue(token.token_prefix.startswith("mdimp_"))
