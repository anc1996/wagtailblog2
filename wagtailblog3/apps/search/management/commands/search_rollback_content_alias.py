"""在测试环境清除独立内容 alias，回退到旧搜索实现。"""

from __future__ import annotations

import json
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from search.models import (
	ContentSearchTarget,
	ContentSearchTargetRole,
	SearchIndexBuild,
	SearchIndexBuildStatus,
)
from search.services.alias import (
	clear_content_search_read_alias,
	get_content_search_read_alias_indices,
	validate_content_search_alias,
)
from search.services.content_index import validate_content_index_name
from search.services.elasticsearch import ContentSearchElasticsearchError


class Command(BaseCommand):
	help = "在测试环境清除独立内容 read alias，默认只输出预演结果"

	def add_arguments(self, parser):
		parser.add_argument("--target", required=True, help="精确 ContentSearchTarget.target_id")
		parser.add_argument("--alias", help="稳定 read alias，默认使用 CONTENT_SEARCH_READ_ALIAS")
		parser.add_argument("--confirm", action="store_true", help="确认执行测试环境 alias 回滚")

	def handle(self, *args, **options):
		environment = os.environ.get("WAGTAILBLOG_ENV", "unset")
		try:
			alias = validate_content_search_alias(options.get("alias"))
		except ContentSearchElasticsearchError as error:
			raise CommandError(error.code) from error
		target = ContentSearchTarget.objects.filter(
			target_id=options["target"],
			connection_name=settings.CONTENT_SEARCH_CONNECTION_NAME,
		).first()
		if target is None:
			raise CommandError("content_target_not_found")
		try:
			validate_content_index_name(target.index_name, settings.CONTENT_SEARCH_INDEX_PREFIX)
			current_indices = get_content_search_read_alias_indices(target, alias)
		except (ValueError, ContentSearchElasticsearchError) as error:
			code = getattr(error, "code", "content_target_index_invalid")
			raise CommandError(code) from error
		build = SearchIndexBuild.objects.filter(target=target).order_by("-pk").first()
		report = {
			"environment": environment,
			"dry_run": not options["confirm"],
			"alias": alias,
			"target_id": target.target_id,
			"index_name": target.index_name,
			"current_indices": list(current_indices),
			"alias_changed": False,
			"target_role": target.role,
			"build_status": build.status if build else "missing",
		}
		if not options["confirm"]:
			self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
			return
		if environment != "test":
			report["refused"] = "test_environment_required"
			self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
			raise CommandError("WP4C alias 回滚仅允许 WAGTAILBLOG_ENV=test")
		if "test" not in settings.CONTENT_SEARCH_INDEX_PREFIX.split("-"):
			raise CommandError("test_index_prefix_required")
		if target.role != ContentSearchTargetRole.SERVING:
			raise CommandError("content_target_not_serving")
		if build is None or build.status != SearchIndexBuildStatus.SERVING:
			raise CommandError("content_build_not_serving")
		try:
			removed_indices = clear_content_search_read_alias(
				target,
				alias=alias,
				expected_indices=current_indices,
			)
		except ContentSearchElasticsearchError as error:
			report["error"] = {"code": error.code, "retryable": error.retryable}
			self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
			raise CommandError("独立内容索引 read alias 回滚失败") from error
		with transaction.atomic():
			target.role = ContentSearchTargetRole.BUILDING
			target.save(update_fields=("role", "updated_at"))
			build.status = SearchIndexBuildStatus.READY
			build.save(update_fields=("status", "updated_at"))
		report["alias_changed"] = bool(removed_indices)
		report["removed_indices"] = list(removed_indices)
		report["target_role"] = target.role
		report["build_status"] = build.status
		self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
