"""受控切换生产独立内容搜索的 read alias。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from search.models import (
    ContentSearchDelivery,
    ContentSearchStatus,
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
from search.services.elasticsearch import ContentSearchElasticsearchError, verify_content_search_index


_BACKUP_REFERENCE_PATTERN = re.compile(r"wagtailblog3-pre-search-\d{8}-\d{6}\Z")
_UNFINISHED_STATUSES = (
    ContentSearchStatus.PENDING,
    ContentSearchStatus.PROCESSING,
    ContentSearchStatus.RETRY,
    ContentSearchStatus.DEAD,
)


class Command(BaseCommand):
    """仅允许把已追平的生产目标提升为 serving，不能覆盖任意索引。"""

    help = "受控切换生产内容搜索 read alias；默认 dry-run，不启用前台 query flag"

    def add_arguments(self, parser):
        parser.add_argument("--target", required=True, help="精确 ContentSearchTarget.target_id")
        parser.add_argument(
            "--backup-reference",
            required=True,
            help="已校验备份目录名，例如 wagtailblog3-pre-search-YYYYMMDD-HHMMSS",
        )
        parser.add_argument("--confirm", action="store_true", help="确认执行生产 ES alias 和 MySQL 状态更新")
        parser.add_argument(
            "--confirm-production-query-switch",
            action="store_true",
            help="生产二次确认；必须与 --confirm 同时提供",
        )

    def handle(self, *args, **options):
        environment = os.environ.get("WAGTAILBLOG_ENV", "unset")
        target = ContentSearchTarget.objects.filter(target_id=options["target"]).first()
        report = {
            "environment": environment,
            "dry_run": not options["confirm"],
            "target_id": options["target"],
            "alias_changed": False,
        }
        refusals = []
        if environment != "production":
            refusals.append("production_environment_required")
        if not _BACKUP_REFERENCE_PATTERN.fullmatch(options["backup_reference"]):
            refusals.append("recognized_backup_reference_required")
        backup_root = getattr(settings, "CONTENT_SEARCH_PRODUCTION_BACKUP_ROOT", "")
        if not backup_root or not (Path(backup_root) / options["backup_reference"] / "checksums.sha256").is_file():
            refusals.append("verified_backup_manifest_required")
        if not getattr(settings, "CONTENT_SEARCH_PRODUCTION_QUERY_SWITCH_ENABLED", False):
            refusals.append("production_query_switch_flag_required")
        if getattr(settings, "CONTENT_SEARCH_QUERY_ENABLED", False):
            refusals.append("frontend_query_must_remain_disabled_until_alias_ready")
        if target is None:
            refusals.append("content_target_not_found")
        else:
            report.update({"index_name": target.index_name, "target_enabled": target.enabled, "target_role": target.role})
            if target.connection_name != getattr(settings, "CONTENT_SEARCH_PRODUCTION_CONNECTION_NAME", ""):
                refusals.append("production_connection_target_mismatch")
            if not target.enabled or target.role not in (ContentSearchTargetRole.BUILDING, ContentSearchTargetRole.SERVING):
                refusals.append("content_target_not_switchable")
            try:
                validate_content_index_name(target.index_name, settings.CONTENT_SEARCH_PRODUCTION_INDEX_PREFIX)
            except ValueError:
                refusals.append("content_target_index_invalid")
            build = SearchIndexBuild.objects.filter(target=target).order_by("-pk").first()
            report["build_status"] = build.status if build else "missing"
            if build is None or build.status not in (SearchIndexBuildStatus.READY, SearchIndexBuildStatus.SERVING):
                refusals.append("content_build_not_ready")
            unfinished = ContentSearchDelivery.objects.filter(target=target, status__in=_UNFINISHED_STATUSES).count()
            report["unfinished_delivery_count"] = unfinished
            if unfinished:
                refusals.append("content_deliveries_not_caught_up")
        try:
            alias = validate_content_search_alias()
            report["alias"] = alias
            if target is not None:
                report["current_indices"] = list(get_content_search_read_alias_indices(target, alias))
        except ContentSearchElasticsearchError as error:
            refusals.append(error.code)

        if not options["confirm"]:
            report["ready_for_confirm"] = not refusals
            if refusals:
                report["refused"] = refusals
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return
        if not options["confirm_production_query_switch"]:
            refusals.append("second_production_confirmation_required")
        if refusals:
            report["refused"] = refusals
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise CommandError("生产前台搜索切换门禁未满足")

        try:
            verify_content_search_index(target)
            result = switch_content_search_read_alias(
                target, target.index_name, alias=report["alias"], expected_indices=tuple(report["current_indices"])
            )
        except ContentSearchElasticsearchError as error:
            report["error"] = {"code": error.code, "retryable": error.retryable}
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise CommandError("生产内容 read alias 切换失败") from error
        try:
            with transaction.atomic():
                target.role = ContentSearchTargetRole.SERVING
                target.save(update_fields=("role", "updated_at"))
                build.status = SearchIndexBuildStatus.SERVING
                build.save(update_fields=("status", "updated_at"))
        except Exception as error:
            report.update({"alias_changed": True, "new_indices": [result.new_index]})
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise CommandError("alias 已切换但 MySQL 状态更新失败；请保留现场并人工核对") from error
        report.update({"alias_changed": True, "new_indices": [result.new_index]})
        self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
