from django.core.management.base import BaseCommand, CommandError

from observability.elasticsearch_logs import LogSearchUnavailable, prepare_log_index


class Command(BaseCommand):
    help = "Create the isolated Elasticsearch log index and aliases."

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Confirm that this command may create the configured index.",
        )

    def handle(self, *args, **options):
        if not options["confirm"]:
            raise CommandError(
                "This command can change Elasticsearch metadata; rerun with --confirm."
            )
        try:
            index = prepare_log_index()
        except LogSearchUnavailable as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Log index ready: {index}"))
