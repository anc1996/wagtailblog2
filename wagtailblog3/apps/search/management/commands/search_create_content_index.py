"""在测试环境创建独立精简内容索引原型。"""

from __future__ import annotations

import json
import os
import re

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


class Command(BaseCommand):
    """原型创建只允许测试环境，且不会启用投递或切换任何读别名。"""

    help = "创建测试环境的独立精简内容索引原型，默认仅输出预演计划"

    def add_arguments(self, parser):
        parser.add_argument("--target", required=True, help="精确的 ContentSearchTarget.target_id。")
        parser.add_argument("--index-name", help="物理索引名；省略时按当前测试前缀生成。")
        parser.add_argument(
            "--mapping-version",
            default=CONTENT_INDEX_MAPPING_VERSION,
            help="版本化 mapping 标识，例如 v001。",
        )
        parser.add_argument(
            "--analyzer-profile",
            choices=sorted(CONTENT_INDEX_ANALYZER_PROFILES),
            default="balanced",
            help="mapping 的 analyzer 配置；默认标题/简介 max-word、正文 smart。",
        )
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="确认创建测试 ES 模板、物理索引及禁用状态的 MySQL Target/Build 记录。",
        )

    def handle(self, *args, **options):
        environment = os.environ.get("WAGTAILBLOG_ENV", "unset")
        target_id = options["target"]
        index_prefix = settings.CONTENT_SEARCH_INDEX_PREFIX
        if not _TARGET_ID_PATTERN.fullmatch(target_id):
            raise CommandError("目标 ID 必须为精确的 slug，不能包含通配符或空白")
        index_name = options["index_name"] or default_content_index_name(
            index_prefix,
            options["mapping_version"],
        )
        try:
            index_name = validate_content_index_name(index_name, index_prefix)
            template_definition = build_content_index_template(
                index_name,
                analyzer_profile=options["analyzer_profile"],
                version=options["mapping_version"],
                shards=settings.CONTENT_SEARCH_INDEX_SHARDS,
                replicas=settings.CONTENT_SEARCH_INDEX_REPLICAS,
                refresh_interval=settings.CONTENT_SEARCH_INDEX_REFRESH_INTERVAL,
            )
            mapping_version = content_index_mapping_version(
                options["mapping_version"],
                options["analyzer_profile"],
            )
        except ValueError as error:
            raise CommandError(str(error)) from error

        template_name = content_index_template_name(index_name)
        report = {
            "environment": environment,
            "dry_run": not options["confirm"],
            "target_id": target_id,
            "connection_name": settings.CONTENT_SEARCH_CONNECTION_NAME,
            "index_name": index_name,
            "template_name": template_name,
            "mapping_version": mapping_version,
            "analyzer_profile": options["analyzer_profile"],
            "target_enabled": False,
            "read_alias_changed": False,
        }
        if not options["confirm"]:
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return
        if environment != "test":
            report["refused"] = "test_environment_required"
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise CommandError("WP4A 仅允许在 WAGTAILBLOG_ENV=test 创建索引原型")
        if "test" not in index_prefix.split("-"):
            report["refused"] = "test_index_prefix_required"
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise CommandError("测试环境内容索引前缀必须包含独立 test 标识")
        if ContentSearchTarget.objects.filter(target_id=target_id).exists():
            raise CommandError("指定目标 ID 已存在；禁止覆盖已有目标")

        try:
            index_result = create_content_search_index(
                connection_name=settings.CONTENT_SEARCH_CONNECTION_NAME,
                index_name=index_name,
                template_name=template_name,
                template_definition=template_definition,
            )
        except ContentSearchElasticsearchError as error:
            report["error"] = {"code": error.code, "retryable": error.retryable}
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise CommandError("独立内容索引原型创建失败") from error

        try:
            with transaction.atomic():
                target = ContentSearchTarget.objects.create(
                    target_id=target_id,
                    connection_name=settings.CONTENT_SEARCH_CONNECTION_NAME,
                    index_name=index_name,
                    role=ContentSearchTargetRole.BUILDING,
                    required=False,
                    enabled=False,
                )
                build = SearchIndexBuild.objects.create(
                    target=target,
                    mapping_version=mapping_version,
                    status=SearchIndexBuildStatus.CREATED,
                )
        except Exception as error:
            # ES 原型保留以便审计，不能在异常路径自动删除证据或误删同名索引。
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
