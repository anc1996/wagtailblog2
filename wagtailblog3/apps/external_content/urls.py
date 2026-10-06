from django.urls import path

from .admin_views import cancel_deletion_intent_view

app_name = "external_content"

urlpatterns = [
    path("cancel-deletion-intent/", cancel_deletion_intent_view, name="cancel_deletion_intent"),
]
