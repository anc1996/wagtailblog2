"""在测试环境演练独立内容索引的 read alias 切换和回滚。"""

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
from search.services.elasticsearch import ContentSearchElasticsearchError, verify_content_search_index
from search.services.rebuild import get_content_search_build_gate


class Command(BaseCommand):
    help = "在测试环境原子切换独立内容索引 read alias，默认只输出预演结果"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--target", required=True, help="精确 ContentSearchTarget.target_id")
        parser.add_argument("--alias", help="稳定 read alias，默认使用 CONTENT_SEARCH_READ_ALIAS")
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="确认执行测试环境 ES alias 写入和 Target 状态更新",
        )

    def handle(self, *args: object, **options: object) -> None:
        environment = os.environ.get("WAGTAILBLOG_ENV", "unset")
        target_id = options["target"]
        alias_changed = False
        try:
            alias = validate_content_search_alias(options.get("alias"))
        except ContentSearchElasticsearchError as error:
            raise CommandError(error.code) from error
        target = (
            ContentSearchTarget.objects.filter(
                target_id=target_id,
                connection_name=settings.CONTENT_SEARCH_CONNECTION_NAME,
            )
            .first()
        )
        if target is None:
            raise CommandError("content_target_not_found")
        try:
            validate_content_index_name(target.index_name, settings.CONTENT_SEARCH_INDEX_PREFIX)
        except ValueError as error:
            raise CommandError("content_target_index_invalid") from error
        try:
            current_indices = get_content_search_read_alias_indices(target, alias)
        except ContentSearchElasticsearchError as error:
            raise CommandError(error.code) from error
        build = SearchIndexBuild.objects.filter(target=target).order_by("-pk").first()
        report = {
            "environment": environment,
            "dry_run": not options["confirm"],
            "alias": alias,
            "target_id": target.target_id,
            "index_name": target.index_name,
            "current_indices": list(current_indices),
            "target_enabled": target.enabled,
            "target_role": target.role,
            "build_status": build.status if build else "missing",
            "alias_changed": False,
        }
        if not options["confirm"]:
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return
        if environment != "test":
            report["refused"] = "test_environment_required"
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise CommandError("WP4C alias 切换仅允许 WAGTAILBLOG_ENV=test")
        if "test" not in settings.CONTENT_SEARCH_INDEX_PREFIX.split("-"):
            report["refused"] = "test_index_prefix_required"
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise CommandError("测试环境内容索引前缀必须包含 test")
        if not target.enabled:
            raise CommandError("content_target_must_be_enabled")
        if target.role not in (ContentSearchTargetRole.BUILDING, ContentSearchTargetRole.SERVING):
            raise CommandError("content_target_not_switchable")
        if build is None or build.status not in (
            SearchIndexBuildStatus.READY,
            SearchIndexBuildStatus.SERVING,
        ):
            raise CommandError("content_build_not_ready")

        try:
            with content_search_alias_switch_lock(settings.CONTENT_SEARCH_CONNECTION_NAME):
                # 锁内重读避免等待其他切换命令时使用过期的 alias 或 Target 状态。
                target.refresh_from_db()
                build = SearchIndexBuild.objects.filter(target=target).order_by("-pk").first()
                if not target.enabled or target.role not in (
                    ContentSearchTargetRole.BUILDING,
                    ContentSearchTargetRole.SERVING,
                ):
                    raise CommandError("content_target_not_switchable")
                if build is None or build.status not in (
                    SearchIndexBuildStatus.READY,
                    SearchIndexBuildStatus.SERVING,
                ):
                    raise CommandError("content_build_not_ready")
                current_indices = get_content_search_read_alias_indices(target, alias)
                # READY 之后仍可能产生新 Delivery；在锁内重新验证，避免切换遗漏增量。
                gate = get_content_search_build_gate(target.target_id, mutate=False)
                report["gate"] = gate
                if not gate.get("clean"):
                    raise CommandError("content_build_gate_not_clean")
                verify_content_search_index(target)
                result = switch_content_search_read_alias(
                    target,
                    target.index_name,
                    alias=alias,
                    expected_indices=current_indices,
                )
                alias_changed = True
                with transaction.atomic():
                    locked_target = ContentSearchTarget.objects.select_for_update().get(pk=target.pk)
                    # read alias 唯一指向的索引必须是 required serving，旧 serving 目标不得继续接收增量事件。
                    ContentSearchTarget.objects.select_for_update().filter(
                        connection_name=settings.CONTENT_SEARCH_CONNECTION_NAME,
                        enabled=True,
                        role=ContentSearchTargetRole.SERVING,
                    ).exclude(pk=locked_target.pk).update(
                        enabled=False,
                        required=False,
                        role=ContentSearchTargetRole.RETIRED,
                    )
                    locked_target.role = ContentSearchTargetRole.SERVING
                    locked_target.required = True
                    locked_target.enabled = True
                    locked_target.save(update_fields=("role", "required", "enabled", "updated_at"))
                    build.status = SearchIndexBuildStatus.SERVING
                    build.save(update_fields=("status", "updated_at"))
        except ContentSearchAliasSwitchInProgress as error:
            raise CommandError("content_alias_switch_in_progress") from error
        except CommandError:
            raise
        except ContentSearchElasticsearchError as error:
            report["error"] = {"code": error.code, "retryable": error.retryable}
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise CommandError("独立内容索引 read alias 切换失败") from error
        except Exception as error:
            if not alias_changed:
                raise
            report["warning"] = "alias_switched_target_state_update_failed"
            report["alias_changed"] = True
            report["new_indices"] = [result.new_index]
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise CommandError("alias 已切换但 Target 状态未更新，请保留现场并人工核对") from error

        report["alias_changed"] = True
        report["new_indices"] = [result.new_index]
        self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
