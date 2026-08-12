"""受控创建生产独立内容搜索空索引。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from search.models import (
    ContentSearchTarget,
    ContentSearchTargetRole,
    SearchIndexBuild,
    SearchIndexBuildStatus,
)
from search.services.content_index import (
    CONTENT_INDEX_ANALYZER_PROFILES,
    CONTENT_INDEX_MAPPING_VERSION,
    build_content_index_template,
    content_index_mapping_version,
    content_index_template_name,
    default_content_index_name,
    validate_content_index_name,
)
from search.services.elasticsearch import (
    ContentSearchElasticsearchError,
    create_content_search_index,
)


_TARGET_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
_BACKUP_REFERENCE_PATTERN = re.compile(r"wagtailblog3-pre-search-\d{8}-\d{6}\Z")
_SEARCH_FLAGS = (
    "CONTENT_SEARCH_PRODUCER_ENABLED",
    "CONTENT_SEARCH_CONSUMER_ENABLED",
    "CONTENT_SEARCH_SHADOW_READ_ENABLED",
    "CONTENT_SEARCH_QUERY_ENABLED",
    "CONTENT_SEARCH_CURSOR_ENABLED",
    "CONTENT_SEARCH_PIT_ENABLED",
    "SEARCH_SUGGESTIONS_V2_ENABLED",
    "SEARCH_POPULAR_SUGGESTIONS_ENABLED",
    "SEARCH_TITLE_SUGGESTIONS_ENABLED",
    "CONTENT_SEARCH_RECONCILE_ENABLED",
)


class Command(BaseCommand):
    """生产写入仅限已备份、独立连接和所有搜索功能关闭时的空索引创建。"""

    help = "受控创建生产独立内容搜索空索引；默认 dry-run，不创建 alias、不回填或启用搜索"

    def add_arguments(self, parser):
        parser.add_argument("--target", required=True, help="精确的 ContentSearchTarget.target_id")
        parser.add_argument("--index-name", required=True, help="精确的生产物理索引名")
        parser.add_argument(
            "--backup-reference",
            required=True,
            help="已校验备份目录名，例如 wagtailblog3-pre-search-YYYYMMDD-HHMMSS",
        )
        parser.add_argument(
            "--mapping-version",
            default=CONTENT_INDEX_MAPPING_VERSION,
            help="版本化 mapping 标识，例如 v001",
        )
        parser.add_argument(
            "--analyzer-profile",
            choices=sorted(CONTENT_INDEX_ANALYZER_PROFILES),
            default="balanced",
            help="mapping 的 analyzer 配置",
        )
        parser.add_argument("--confirm", action="store_true", help="确认执行生产 ES/MySQL 空索引创建")
        parser.add_argument(
            "--confirm-production-index-create",
            action="store_true",
            help="生产二次确认；必须与 --confirm 同时提供",
        )

    def _build_report(self, options, environment, index_prefix, connection_name):
        return {
            "environment": environment,
            "dry_run": not options["confirm"],
            "target_id": options["target"],
            "connection_name": connection_name,
            "index_name": options["index_name"],
            "index_prefix": index_prefix,
            "backup_reference": options["backup_reference"],
            "target_enabled": False,
            "read_alias_changed": False,
            "backfill_started": False,
        }

    def _preconditions(self, options, environment, index_prefix, connection_name):
        refusals = []
        if environment != "production":
            refusals.append("production_environment_required")
        if not _TARGET_ID_PATTERN.fullmatch(options["target"]):
            refusals.append("exact_target_id_required")
        if not _BACKUP_REFERENCE_PATTERN.fullmatch(options["backup_reference"]):
            refusals.append("recognized_backup_reference_required")
        if not index_prefix or "prod" not in index_prefix.split("-"):
            refusals.append("explicit_production_index_prefix_required")
        if connection_name in {"", "default"}:
            refusals.append("non_default_production_connection_required")
        elif connection_name not in settings.WAGTAILSEARCH_BACKENDS:
            refusals.append("configured_production_connection_required")
        elif not (
            getattr(settings, "CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_ENABLED", False)
            or getattr(settings, "CONTENT_SEARCH_SECONDARY_CONNECTION_ENABLED", False)
        ):
            refusals.append("explicit_production_cluster_mode_required")
        try:
            validate_content_index_name(options["index_name"], index_prefix)
        except ValueError:
            refusals.append("exact_index_name_within_production_prefix_required")
        try:
            expected_index_name = default_content_index_name(index_prefix, options["mapping_version"])
        except ValueError:
            expected_index_name = None
        if expected_index_name and options["index_name"] != expected_index_name:
            refusals.append("index_name_must_match_mapping_version")

        backup_root = getattr(settings, "CONTENT_SEARCH_PRODUCTION_BACKUP_ROOT", "")
        if not backup_root:
            refusals.append("production_backup_root_required")
        elif not (Path(backup_root) / options["backup_reference"] / "checksums.sha256").is_file():
            refusals.append("verified_backup_manifest_required")
        return refusals

    def _write_preconditions(self, options):
        refusals = []
        if not options["confirm_production_index_create"]:
            refusals.append("second_production_confirmation_required")
        if not getattr(settings, "CONTENT_SEARCH_PRODUCTION_INDEX_CREATE_ENABLED", False):
            refusals.append("production_index_create_flag_required")
        if any(getattr(settings, name, False) for name in _SEARCH_FLAGS):
            refusals.append("search_feature_flags_must_remain_disabled")
        return refusals

    def handle(self, *args, **options):
        environment = os.environ.get("WAGTAILBLOG_ENV", "unset")
        index_prefix = getattr(settings, "CONTENT_SEARCH_PRODUCTION_INDEX_PREFIX", "")
        connection_name = getattr(settings, "CONTENT_SEARCH_PRODUCTION_CONNECTION_NAME", "")
        report = self._build_report(options, environment, index_prefix, connection_name)
        refusals = self._preconditions(options, environment, index_prefix, connection_name)
        if environment != "production":
            report["refused"] = refusals
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise CommandError("生产空索引命令仅允许 WAGTAILBLOG_ENV=production")

        try:
            template_definition = build_content_index_template(
                options["index_name"],
                analyzer_profile=options["analyzer_profile"],
                version=options["mapping_version"],
                shards=settings.CONTENT_SEARCH_INDEX_SHARDS,
                replicas=settings.CONTENT_SEARCH_INDEX_REPLICAS,
                refresh_interval=settings.CONTENT_SEARCH_INDEX_REFRESH_INTERVAL,
            )
            report["template_name"] = content_index_template_name(options["index_name"])
            report["mapping_version"] = content_index_mapping_version(
                options["mapping_version"], options["analyzer_profile"]
            )
        except ValueError as error:
            report["refused"] = ["invalid_content_index_mapping"]
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise CommandError(str(error)) from error

        if not options["confirm"]:
            report["ready_for_confirm"] = not refusals
            if refusals:
                report["refused"] = refusals
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return

        refusals.extend(self._write_preconditions(options))
        if refusals:
            report["refused"] = refusals
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise CommandError("生产空索引创建门禁未满足")
        if ContentSearchTarget.objects.filter(target_id=options["target"]).exists():
            raise CommandError("指定目标 ID 已存在；禁止覆盖已有生产目标")

        try:
            index_result = create_content_search_index(
                connection_name=connection_name,
                index_name=options["index_name"],
                template_name=report["template_name"],
                template_definition=template_definition,
            )
        except ContentSearchElasticsearchError as error:
            report["error"] = {"code": error.code, "retryable": error.retryable}
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise CommandError("生产独立内容搜索空索引创建失败") from error

        try:
            with transaction.atomic():
                target = ContentSearchTarget.objects.create(
                    target_id=options["target"],
                    connection_name=connection_name,
                    index_name=options["index_name"],
                    role=ContentSearchTargetRole.BUILDING,
                    required=False,
                    enabled=False,
                )
                build = SearchIndexBuild.objects.create(
                    target=target,
                    mapping_version=report["mapping_version"],
                    status=SearchIndexBuildStatus.CREATED,
                )
        except Exception as error:
            # 索引已创建时保留现场，避免自动删除同名资源破坏审计和后续人工核验。
            report["error"] = {"code": "content_target_registration_failed", "retryable": False}
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise CommandError("ES 索引已创建，但 MySQL 目标登记失败；请保留现场后人工核对") from error

        report.update(
            {
                "template_created": index_result.template_created,
                "index_created": index_result.index_created,
                "target_pk": target.pk,
                "build_id": str(build.build_id),
            }
        )
        self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
