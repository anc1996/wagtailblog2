from django.core.management.base import BaseCommand, CommandError

from observability.elasticsearch_logs import (
    LogSearchUnavailable,
    bulk_index_records,
    prepare_log_index,
    record_domain,
    record_document,
)
from observability.reader import read_logs


class Command(BaseCommand):
    help = "Import registered local log files into the Elasticsearch read model."

    def add_arguments(self, parser):
        parser.add_argument("--confirm", action="store_true")
        parser.add_argument("--domain", default="")
        parser.add_argument("--kind", choices=("", "activity", "error"), default="")
        parser.add_argument("--include-rotated", action="store_true")
        parser.add_argument("--max-records", type=int, default=10000)
        parser.add_argument("--page-size", type=int, default=200)

    def handle(self, *args, **options):
        if not options["confirm"]:
            raise CommandError("This command writes Elasticsearch; rerun with --confirm.")
        max_records = max(1, min(options["max_records"], 100000))
        page_size = max(1, min(options["page_size"], 200))
        try:
            index = prepare_log_index()
            # The command uses the same configured client as the query path,
            # ensuring the write alias and credentials are identical.
            del index
            cursor = ""
            indexed = 0
            while indexed < max_records:
                result = read_logs(
                    domain=options["domain"],
                    kind=options["kind"],
                    include_rotated=options["include_rotated"],
                    page_size=min(page_size, max_records - indexed),
                    cursor=cursor,
                )
                payload = [
                    record_document(
                        record,
                        domain=options["domain"]
                        or record_domain(record),
                    )
                    for record in result.records
                ]
                if payload:
                    bulk_index_records(payload)
                    indexed += len(payload)
                if not result.has_more or not result.next_cursor:
                    break
                cursor = result.next_cursor
            self.stdout.write(self.style.SUCCESS(f"Indexed {indexed} log records."))
        except LogSearchUnavailable as exc:
            raise CommandError(str(exc)) from exc
