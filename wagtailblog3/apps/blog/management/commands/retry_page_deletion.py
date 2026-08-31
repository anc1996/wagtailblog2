"""人工解锁并重试处于 blocked/dead 状态的页面删除意图。"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from blog.models import PageDeletionIntent, PageDeletionIntentStatus


class Command(BaseCommand):
	"""以显式 ``--apply`` 作为写入门禁，避免误触发删除任务。"""

	help = "查看或人工解锁 PageDeletionIntent；默认只读，写入必须指定 --apply。"

	def add_arguments(self, parser):
		parser.add_argument("intent_id", nargs="+", help="删除意图 UUID")
		parser.add_argument("--apply", action="store_true", help="实际重置状态并投递任务")

	def handle(self, *args, **options):
		ids = options["intent_id"]
		intents = list(PageDeletionIntent.objects.filter(intent_id__in=ids))
		found = {str(intent.intent_id) for intent in intents}
		missing = [value for value in ids if value not in found]
		if missing:
			raise CommandError(f"未找到删除意图: {', '.join(missing)}")
		eligible = {
			PageDeletionIntentStatus.BLOCKED_REFERENCE,
			PageDeletionIntentStatus.DEAD,
		}
		for intent in intents:
			if intent.status not in eligible:
				self.stdout.write(f"{intent.intent_id}: 跳过 status={intent.status}")
				continue
			self.stdout.write(
				f"{intent.intent_id}: page_id={intent.page_id} status={intent.status} "
				f"step={intent.step} attempts={intent.attempts}"
			)
			if not options["apply"]:
				continue
			from blog.tasks import _page_deletion_queue, process_page_deletion
			with transaction.atomic():
				locked = PageDeletionIntent.objects.select_for_update().get(pk=intent.pk)
				if locked.status not in eligible:
					continue
				locked.status = PageDeletionIntentStatus.PARTIAL_FAILED
				locked.attempts = 0
				locked.available_at = timezone.now()
				locked.lease_owner = ""
				locked.lease_expires_at = None
				locked.last_error_code = "manual_retry"
				locked.save(
					update_fields=(
						"status", "attempts", "available_at", "lease_owner",
						"lease_expires_at", "last_error_code", "updated_at",
					)
				)
				transaction.on_commit(
					lambda intent_id=str(locked.intent_id): process_page_deletion.apply_async(
						args=(intent_id,), queue=_page_deletion_queue()
					)
				)
			self.stdout.write(self.style.SUCCESS(f"已解锁并投递: {locked.intent_id}"))
