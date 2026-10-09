from django.urls import path

from .admin_views import cancel_deletion_intent_view
from .request_bridge import prepare_view, status_view

app_name = "external_content"

urlpatterns = [
    path("cancel-deletion-intent/", cancel_deletion_intent_view, name="cancel_deletion_intent"),
    path("request/prepare/", prepare_view, name="prepare"),
    path("request/status/", status_view, name="status"),
]
