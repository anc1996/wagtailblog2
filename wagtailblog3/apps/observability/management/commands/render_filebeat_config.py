import yaml
from django.core.management.base import BaseCommand

from observability.elasticsearch_logs import build_filebeat_config


class Command(BaseCommand):
    help = "Render a secret-free Filebeat config for the registered log files."

    def handle(self, *args, **options):
        self.stdout.write(
            yaml.safe_dump(
                build_filebeat_config(),
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            )
        )
