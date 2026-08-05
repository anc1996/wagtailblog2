from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from observability.models import LogIndexSyncJob
from observability.tasks import _update_audit, sync_log_index


class Command(BaseCommand):
    help = "Retry one dead-letter or failed Elasticsearch log cleanup job."

    def add_arguments(self, parser):
        parser.add_argument("audit_id", type=int)
        parser.add_argument("--confirm", action="store_true")

    def handle(self, *args, **options):
        if not options["confirm"]:
            raise CommandError(
                "This command can delete Elasticsearch documents; rerun with --confirm."
            )
        try:
            job = LogIndexSyncJob.objects.select_related("audit").get(
                audit_id=options["audit_id"]
            )
        except LogIndexSyncJob.DoesNotExist as exc:
            raise CommandError("No Elasticsearch synchronization job for this audit.") from exc
        if job.state == "completed":
            self.stdout.write(self.style.WARNING("The job is already completed."))
            return
        job.state = "pending"
        job.next_retry_at = timezone.now()
        job.dead_letter_at = None
        job.last_error = ""
        job.es_task_id = ""
        selector = dict(job.selector or {})
        selector.pop("failure_attempts", None)
        job.selector = selector
        job.save(
            update_fields=(
                "state",
                "next_retry_at",
                "dead_letter_at",
                "last_error",
                "es_task_id",
                "selector",
                "updated_at",
            )
        )
        _update_audit(job)
        sync_log_index.apply_async(args=(job.pk,), queue="maintenance")
        self.stdout.write(self.style.SUCCESS(f"Queued audit {job.audit_id}."))
