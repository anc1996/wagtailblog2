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
    ContentSearchAliasSwitchInProgress,
    content_search_alias_switch_lock,
    get_content_search_read_alias_indices,
	switch_content_search_read_alias,
	validate_content_search_alias,
)
from search.services.content_index import validate_content_index_name
from search.services.elasticsearch import ContentSearchElasticsearchError
from search.services.rebuild import get_content_search_build_gate


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
		previous_target = ContentSearchTarget.objects.filter(
			connection_name=settings.CONTENT_SEARCH_CONNECTION_NAME,
			index_name=previous_index,
		).first()
		if previous_target is None:
			raise CommandError("previous_target_not_found")
		if previous_target.pk == target.pk:
			raise CommandError("previous_target_must_differ")
		previous_build = SearchIndexBuild.objects.filter(target=previous_target).order_by("-pk").first()
		if previous_build is None or previous_build.status not in (
			SearchIndexBuildStatus.READY,
		):
			raise CommandError("previous_build_not_ready")
		# 已退役目标不会再接收增量事件，不能把它直接重新公开；必须先作为 building
		# 目标完成新的回填和追平，避免旧索引重新暴露已更新或已取消发布的文档。
		if not previous_target.enabled or previous_target.role != ContentSearchTargetRole.BUILDING:
			raise CommandError("previous_target_requires_rebuild")
		if tuple(current_indices) != (target.index_name,):
			raise CommandError("content_alias_not_on_current_target")
		if target.role != ContentSearchTargetRole.SERVING:
			raise CommandError("content_target_not_serving")
		if build is None or build.status != SearchIndexBuildStatus.SERVING:
			raise CommandError("content_build_not_serving")
		alias_changed = False
		try:
			with content_search_alias_switch_lock(settings.CONTENT_SEARCH_CONNECTION_NAME):
				# 锁内复查，保证回滚不会覆盖同时开始的发布切换。
				target.refresh_from_db()
				previous_target.refresh_from_db()
				build = SearchIndexBuild.objects.filter(target=target).order_by("-pk").first()
				previous_build = SearchIndexBuild.objects.filter(target=previous_target).order_by("-pk").first()
				current_indices = get_content_search_read_alias_indices(target, alias)
				if tuple(current_indices) != (target.index_name,):
					raise CommandError("content_alias_not_on_current_target")
				if target.role != ContentSearchTargetRole.SERVING or not target.required:
					raise CommandError("content_target_not_serving")
				if build is None or build.status != SearchIndexBuildStatus.SERVING:
					raise CommandError("content_build_not_serving")
				if previous_build is None or previous_build.status not in (
					SearchIndexBuildStatus.READY,
				):
					raise CommandError("previous_build_not_ready")
				if not previous_target.enabled or previous_target.role != ContentSearchTargetRole.BUILDING:
					raise CommandError("previous_target_requires_rebuild")
				previous_gate = get_content_search_build_gate(previous_target.target_id, mutate=False)
				report["previous_gate"] = previous_gate
				if not previous_gate.get("clean"):
					raise CommandError("previous_build_gate_not_clean")
				result = switch_content_search_read_alias(
					target,
					previous_index,
					alias=alias,
					expected_indices=current_indices,
				)
				alias_changed = True
				with transaction.atomic():
					locked_target = ContentSearchTarget.objects.select_for_update().get(pk=target.pk)
					locked_previous = ContentSearchTarget.objects.select_for_update().get(pk=previous_target.pk)
					# 回滚后旧索引对应 Target 必须恢复为唯一 required serving，避免查询端无可用目标。
					ContentSearchTarget.objects.select_for_update().filter(
						connection_name=settings.CONTENT_SEARCH_CONNECTION_NAME,
						enabled=True,
						role=ContentSearchTargetRole.SERVING,
					).exclude(pk=locked_previous.pk).update(
						enabled=False,
						required=False,
						role=ContentSearchTargetRole.RETIRED,
					)
					locked_target.enabled = False
					locked_target.required = False
					locked_target.role = ContentSearchTargetRole.RETIRED
					locked_target.save(update_fields=("enabled", "required", "role", "updated_at"))
					locked_previous.enabled = True
					locked_previous.required = True
					locked_previous.role = ContentSearchTargetRole.SERVING
					locked_previous.save(update_fields=("enabled", "required", "role", "updated_at"))
					build.status = SearchIndexBuildStatus.READY
					build.save(update_fields=("status", "updated_at"))
					previous_build.status = SearchIndexBuildStatus.SERVING
					previous_build.save(update_fields=("status", "updated_at"))
		except ContentSearchAliasSwitchInProgress as error:
			raise CommandError("content_alias_switch_in_progress") from error
		except CommandError:
			raise
		except ContentSearchElasticsearchError as error:
			report["error"] = {"code": error.code, "retryable": error.retryable}
			self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
			raise CommandError("独立内容索引 read alias 回滚失败") from error
		except Exception as error:
			if not alias_changed:
				raise
			report.update({"alias_changed": True, "new_indices": [previous_index]})
			self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
			raise CommandError("alias 已回滚但 MySQL 状态更新失败；请保留现场并人工核对") from error
		report["alias_changed"] = True
		report["new_indices"] = [result.new_index]
		report["target_role"] = ContentSearchTargetRole.RETIRED
		report["build_status"] = build.status
		self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
