"""BlogPage 发布状态只读对账命令。"""

import json

from django.core.management.base import BaseCommand, CommandError

from blog.services.publication_consistency import check_blog_publication_consistency


class Command(BaseCommand):
	"""输出发布状态异常分类，不执行任何自动修复。"""

	help = "只读核对 BlogPage、发布状态、Revision、Mongo 版本和搜索事件"

	def add_arguments(self, parser):
		parser.add_argument("--after-page-id", type=int, default=0)
		parser.add_argument("--limit", type=int, default=1000)
		parser.add_argument("--sample-limit", type=int, default=20)
		parser.add_argument("--skip-mongo", action="store_true")

	def handle(self, *args, **options):
		try:
			report = check_blog_publication_consistency(
				after_page_id=options["after_page_id"],
				limit=options["limit"],
				sample_limit=options["sample_limit"],
				check_mongo=not options["skip_mongo"],
			)
		except (TypeError, ValueError) as exc:
			raise CommandError("对账参数无效") from exc
		report["read_only"] = True
		report["mongo_checked"] = not options["skip_mongo"]
		self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
