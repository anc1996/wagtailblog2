from types import SimpleNamespace
from unittest.mock import Mock

from django.test import SimpleTestCase

from external_content.admin_views import _can_cancel


class CancelDeletionIntentPermissionTests(SimpleTestCase):
    def test_superuser_still_requires_page_edit_permission(self) -> None:
        request = SimpleNamespace(user=SimpleNamespace(is_superuser=True, has_perm=Mock(return_value=False)))
        page = Mock()
        page.permissions_for_user.return_value.can_edit.return_value = False

        self.assertFalse(_can_cancel(request, page))

    def test_operator_requires_page_edit_and_special_permission(self) -> None:
        request = SimpleNamespace(user=SimpleNamespace(is_superuser=False, has_perm=Mock(return_value=True)))
        page = Mock()
        page.permissions_for_user.return_value.can_edit.return_value = True

        self.assertTrue(_can_cancel(request, page))
        request.user.has_perm.assert_called_once_with(
            "external_content.change_externalcontentpublicationstate"
        )

    def test_operator_without_special_permission_is_denied(self) -> None:
        request = SimpleNamespace(user=SimpleNamespace(is_superuser=False, has_perm=Mock(return_value=False)))
        page = Mock()
        page.permissions_for_user.return_value.can_edit.return_value = True

        self.assertFalse(_can_cancel(request, page))
