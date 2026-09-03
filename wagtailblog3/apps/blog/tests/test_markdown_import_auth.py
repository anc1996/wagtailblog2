from datetime import timedelta

from django.contrib.auth import get_user_model
from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from blog.models import MarkdownImportToken
from blog.services.markdown_import_auth import MarkdownImportTokenAuthentication
from blog.services.markdown_import_crypto import encrypt_token, decrypt_token
from blog.admin import MarkdownImportTokenCreateView, MarkdownImportTokenSnippetViewSet


class MarkdownImportCryptoTests(TestCase):
    """AES-256-GCM 对称认证加解密单元测试。"""

    def test_encrypt_decrypt_round_trip(self):
        secret = "mdimp_abcdef123456_test_token"
        ciphertext = encrypt_token(secret)
        self.assertIsInstance(ciphertext, str)
        self.assertNotEqual(ciphertext, secret)
        decrypted = decrypt_token(ciphertext)
        self.assertEqual(decrypted, secret)

    def test_tampered_payload_fails_verification(self):
        secret = "mdimp_secure_token_123"
        ciphertext = encrypt_token(secret)
        # 篡改密文 Base64 尾部字符
        tampered = ciphertext[:-2] + ("A" if ciphertext[-2] != "A" else "B") + ciphertext[-1]
        with self.assertRaises(ValueError):
            decrypt_token(tampered)

    def test_invalid_payload_fails(self):
        with self.assertRaises(ValueError):
            decrypt_token("not-a-valid-token-payload")
        with self.assertRaises(ValueError):
            encrypt_token("")


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

    def test_create_generates_token_for_current_user_with_encryption(self):
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
        self.assertTrue(token.token_encrypted)
        self.assertIsNotNone(token.get_plaintext())
        self.assertTrue(token.get_plaintext().startswith(token.token_prefix))

    def test_copy_token_view_returns_plaintext(self):
        user = get_user_model().objects.create_superuser(
            username="token-copy-admin",
            password="test-password",
            email="token-copy@example.com",
        )
        token = MarkdownImportToken(
            name="copy-test",
            user=user,
            scopes=["markdown_import"],
        )
        plaintext = token.issue_plaintext()
        token.save()

        self.client.force_login(user)
        copy_url = reverse("wagtailsnippets_blog_markdownimporttoken:copy_token", args=[token.pk])

        # GET 请求被拒绝
        get_res = self.client.get(copy_url)
        self.assertEqual(get_res.status_code, 405)

        # POST 请求成功解密并返回明文
        post_res = self.client.post(copy_url)
        self.assertEqual(post_res.status_code, 200)
        data = post_res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["token"], plaintext)

    def test_copy_token_view_handles_legacy_unencrypted_token(self):
        user = get_user_model().objects.create_superuser(
            username="token-legacy-admin",
            password="test-password",
            email="token-legacy@example.com",
        )
        token = MarkdownImportToken(
            name="legacy-test",
            user=user,
            scopes=["markdown_import"],
            token_prefix="mdimp_legacy1234",
            token_hash="dummyhash1234567890",
            token_encrypted=None,  # 模拟旧版本记录
        )
        token.save()

        self.client.force_login(user)
        copy_url = reverse("wagtailsnippets_blog_markdownimporttoken:copy_token", args=[token.pk])
        res = self.client.post(copy_url)
        self.assertEqual(res.status_code, 400)
        data = res.json()
        self.assertFalse(data["success"])
        self.assertTrue(data["can_rotate"])

    def test_rotate_token_view_updates_token(self):
        user = get_user_model().objects.create_superuser(
            username="token-rotate-admin",
            password="test-password",
            email="token-rotate@example.com",
        )
        token = MarkdownImportToken(
            name="rotate-test",
            user=user,
            scopes=["markdown_import"],
        )
        old_plaintext = token.issue_plaintext()
        old_hash = token.token_hash
        token.save()

        self.client.force_login(user)
        rotate_url = reverse("wagtailsnippets_blog_markdownimporttoken:rotate_token", args=[token.pk])
        res = self.client.post(rotate_url)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertNotEqual(data["token"], old_plaintext)

        token.refresh_from_db()
        self.assertNotEqual(token.token_hash, old_hash)
        self.assertEqual(token.get_plaintext(), data["token"])

    def test_snippet_list_includes_actions_and_js(self):
        user = get_user_model().objects.create_superuser(
            username="token-list-admin",
            password="test-password",
            email="token-list@example.com",
        )
        token = MarkdownImportToken(
            name="list-test-token",
            user=user,
            scopes=["markdown_import"],
        )
        token.issue_plaintext()
        token.save()

        self.client.force_login(user)
        res = self.client.get(reverse("wagtailsnippets_blog_markdownimporttoken:list"))
        self.assertEqual(res.status_code, 200)
        content = res.content.decode("utf-8")
        self.assertIn("markdown_import_token_admin.js", content)
        self.assertIn("copy-markdown-import-token", content)
        self.assertIn("rotate-markdown-import-token", content)
