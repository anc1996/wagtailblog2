"""在测试环境把独立内容 alias 原子回切到明确的旧物理索引。"""

from __future__ import annotations

import json
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from search.models import (
	ContentSearchTarget,
	ContentSearchTargetRole,
	SearchIndexBuild,
	SearchIndexBuildStatus,
)
from search.services.alias import (
	get_content_search_read_alias_indices,
	switch_content_search_read_alias,
	validate_content_search_alias,
)
from search.services.content_index import validate_content_index_name
from search.services.elasticsearch import ContentSearchElasticsearchError


class Command(BaseCommand):
	help = "在测试环境原子回切独立内容 read alias，默认只输出预演结果"

	def add_arguments(self, parser: CommandParser) -> None:
		parser.add_argument("--target", required=True, help="精确 ContentSearchTarget.target_id")
		parser.add_argument("--alias", help="稳定 read alias，默认使用 CONTENT_SEARCH_READ_ALIAS")
		parser.add_argument("--previous-index", help="必须恢复的旧物理索引精确名称")
		parser.add_argument("--confirm", action="store_true", help="确认执行测试环境 alias 回滚")

	def handle(self, *args: object, **options: object) -> None:
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
			"previous_index": options.get("previous_index"),
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
		previous_index = options.get("previous_index")
		if not isinstance(previous_index, str) or not previous_index:
			raise CommandError("previous_index_required")
		try:
			validate_content_index_name(previous_index, settings.CONTENT_SEARCH_INDEX_PREFIX)
		except ValueError as error:
			raise CommandError("previous_index_invalid") from error
		if tuple(current_indices) != (target.index_name,):
			raise CommandError("content_alias_not_on_current_target")
		if target.role != ContentSearchTargetRole.SERVING:
			raise CommandError("content_target_not_serving")
		if build is None or build.status != SearchIndexBuildStatus.SERVING:
			raise CommandError("content_build_not_serving")
		try:
			result = switch_content_search_read_alias(
				target,
				previous_index,
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
		report["alias_changed"] = True
		report["new_indices"] = [result.new_index]
		report["target_role"] = target.role
		report["build_status"] = build.status
		self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
