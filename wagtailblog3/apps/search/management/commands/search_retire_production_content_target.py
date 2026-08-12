"""受控退役不再提供前台服务的生产内容搜索目标。"""

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
from search.services.alias import get_content_search_read_alias_indices
from search.services.content_index import validate_content_index_name
from search.services.elasticsearch import ContentSearchElasticsearchError


_BACKUP_REFERENCE_PATTERN = re.compile(r"wagtailblog3-pre-search-\d{8}-\d{6}\Z")
_UNFINISHED_STATUSES = (
    ContentSearchStatus.PENDING,
    ContentSearchStatus.PROCESSING,
    ContentSearchStatus.RETRY,
    ContentSearchStatus.DEAD,
)
_RETIRABLE_BUILD_STATUSES = (
    SearchIndexBuildStatus.READY,
    SearchIndexBuildStatus.SERVING,
    SearchIndexBuildStatus.RETIRED,
)


class Command(BaseCommand):
    """保留 Target、Build 和 Delivery 审计，只关闭旧目标的后续投递。"""

    help = "受控退役生产内容搜索目标；默认 dry-run，不删除 Elasticsearch 索引"

    def add_arguments(self, parser):
        parser.add_argument("--target", required=True, help="精确 ContentSearchTarget.target_id")
        parser.add_argument(
            "--backup-reference",
            required=True,
            help="已校验备份目录名，例如 wagtailblog3-pre-search-YYYYMMDD-HHMMSS",
        )
        parser.add_argument("--confirm", action="store_true", help="确认更新生产 MySQL 目标状态")
        parser.add_argument(
            "--confirm-production-target-retire",
            action="store_true",
            help="生产二次确认；必须与 --confirm 同时提供",
        )

    def handle(self, *args, **options):
        environment = os.environ.get("WAGTAILBLOG_ENV", "unset")
        production_prefix = getattr(settings, "CONTENT_SEARCH_PRODUCTION_INDEX_PREFIX", "")
        production_connection = getattr(settings, "CONTENT_SEARCH_PRODUCTION_CONNECTION_NAME", "")
        production_alias = f"{production_prefix}-read" if production_prefix else ""
        target = ContentSearchTarget.objects.filter(target_id=options["target"]).first()
        report = {
            "environment": environment,
            "dry_run": not options["confirm"],
            "target_id": options["target"],
            "target_retired": False,
            "index_deleted": False,
        }
        refusals = []
        if environment != "production":
            refusals.append("production_environment_required")
        if not getattr(settings, "CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_ENABLED", False):
            refusals.append("production_existing_cluster_required")
        if not _BACKUP_REFERENCE_PATTERN.fullmatch(options["backup_reference"]):
            refusals.append("recognized_backup_reference_required")
        backup_root = getattr(settings, "CONTENT_SEARCH_PRODUCTION_BACKUP_ROOT", "")
        if not backup_root or not (Path(backup_root) / options["backup_reference"] / "checksums.sha256").is_file():
            refusals.append("verified_backup_manifest_required")
        if not production_prefix or "prod" not in production_prefix.split("-"):
            refusals.append("explicit_production_index_prefix_required")
        if settings.CONTENT_SEARCH_INDEX_PREFIX != production_prefix:
            refusals.append("runtime_index_prefix_must_match_production_prefix")
        if settings.CONTENT_SEARCH_READ_ALIAS != production_alias:
            refusals.append("runtime_read_alias_must_match_production_prefix")
        if settings.CONTENT_SEARCH_CONNECTION_NAME != production_connection:
            refusals.append("runtime_connection_must_match_production_connection")

        current_indices = ()
        build = None
        if target is None:
            refusals.append("content_target_not_found")
        else:
            report.update(
                {
                    "connection_name": target.connection_name,
                    "index_name": target.index_name,
                    "target_enabled": target.enabled,
                    "target_role": target.role,
                }
            )
            if target.connection_name != production_connection:
                refusals.append("production_connection_target_mismatch")
            try:
                validate_content_index_name(target.index_name, production_prefix)
            except ValueError:
                refusals.append("content_target_index_invalid")
            build = SearchIndexBuild.objects.filter(target=target).order_by("-pk").first()
            report["build_status"] = build.status if build else "missing"
            if build is None or build.status not in _RETIRABLE_BUILD_STATUSES:
                refusals.append("content_build_not_retirable")
            unfinished = ContentSearchDelivery.objects.filter(
                target=target, status__in=_UNFINISHED_STATUSES
            ).count()
            report["unfinished_delivery_count"] = unfinished
            if unfinished:
                refusals.append("content_deliveries_not_caught_up")
            try:
                current_indices = tuple(
                    get_content_search_read_alias_indices(
                        target,
                        production_alias,
                        index_prefix=production_prefix,
                    )
                )
            except ContentSearchElasticsearchError as error:
                refusals.append(error.code)
            report["current_indices"] = list(current_indices)
            if len(current_indices) != 1:
                refusals.append("single_serving_alias_target_required")
            elif target.index_name in current_indices:
                refusals.append("target_still_serving_alias")
            elif not ContentSearchTarget.objects.filter(
                connection_name=production_connection,
                index_name=current_indices[0],
                enabled=True,
                role=ContentSearchTargetRole.SERVING,
            ).exclude(pk=target.pk).exists():
                refusals.append("replacement_serving_target_required")

        if not options["confirm"]:
            report["ready_for_confirm"] = not refusals
            if refusals:
                report["refused"] = refusals
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return
        if not options["confirm_production_target_retire"]:
            refusals.append("second_production_confirmation_required")
        if refusals:
            report["refused"] = refusals
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise CommandError("生产内容搜索目标退役门禁未满足")

        # ES alias 在事务外二次核对，避免把刚重新接管流量的目标误退役。
        confirmed_indices = tuple(
            get_content_search_read_alias_indices(
                target,
                production_alias,
                index_prefix=production_prefix,
            )
        )
        if confirmed_indices != current_indices:
            report["refused"] = ["content_read_alias_changed"]
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise CommandError("生产内容 read alias 已变化，拒绝退役")

        with transaction.atomic():
            locked_target = ContentSearchTarget.objects.select_for_update().get(pk=target.pk)
            unfinished = ContentSearchDelivery.objects.filter(
                target=locked_target, status__in=_UNFINISHED_STATUSES
            ).count()
            if unfinished:
                report["refused"] = ["content_deliveries_changed"]
                self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
                raise CommandError("生产内容投递状态已变化，拒绝退役")
            replacement_exists = ContentSearchTarget.objects.select_for_update().filter(
                connection_name=production_connection,
                index_name=current_indices[0],
                enabled=True,
                role=ContentSearchTargetRole.SERVING,
            ).exclude(pk=locked_target.pk).exists()
            if not replacement_exists:
                report["refused"] = ["replacement_serving_target_changed"]
                self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
                raise CommandError("生产替代服务目标已变化，拒绝退役")
            locked_target.enabled = False
            locked_target.required = False
            locked_target.role = ContentSearchTargetRole.RETIRED
            locked_target.save(update_fields=("enabled", "required", "role", "updated_at"))
            locked_build = SearchIndexBuild.objects.select_for_update().filter(
                target=locked_target
            ).order_by("-pk").first()
            if locked_build is not None:
                locked_build.status = SearchIndexBuildStatus.RETIRED
                locked_build.save(update_fields=("status", "updated_at"))

        report.update(
            {
                "target_enabled": False,
                "target_role": ContentSearchTargetRole.RETIRED,
                "build_status": SearchIndexBuildStatus.RETIRED,
                "target_retired": True,
            }
        )
        self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
