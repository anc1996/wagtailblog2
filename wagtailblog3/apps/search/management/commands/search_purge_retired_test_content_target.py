"""受控清理测试环境已退役的独立内容搜索目标。"""

from __future__ import annotations

import json
import os

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
from search.services.content_index import content_index_template_name, validate_content_index_name
from search.services.elasticsearch import ContentSearchElasticsearchError, get_content_search_client


_UNFINISHED_STATUSES = (
    ContentSearchStatus.PENDING,
    ContentSearchStatus.PROCESSING,
    ContentSearchStatus.RETRY,
    ContentSearchStatus.DEAD,
)


class Command(BaseCommand):
    """只允许清理测试命名空间内、未被 read alias 使用的历史目标。"""

    help = "受控退役或清理测试独立内容搜索目标；默认 dry-run，不修改 MySQL 或 Elasticsearch"

    def add_arguments(self, parser):
        parser.add_argument("--target", required=True, help="精确 ContentSearchTarget.target_id")
        parser.add_argument("--retire-only", action="store_true", help="只退役目标，不删除 ES 索引或审计记录")
        parser.add_argument("--confirm", action="store_true", help="确认执行测试 MySQL/ES 写操作")

    def handle(self, *args, **options):
        environment = os.environ.get("WAGTAILBLOG_ENV", "unset").strip().lower()
        target = ContentSearchTarget.objects.filter(target_id=options["target"]).first()
        index_prefix = settings.CONTENT_SEARCH_INDEX_PREFIX
        alias = settings.CONTENT_SEARCH_READ_ALIAS
        report = {
            "environment": environment,
            "dry_run": not options["confirm"],
            "target_id": options["target"],
            "retire_only": options["retire_only"],
            "target_retired": False,
            "index_deleted": False,
            "template_deleted": False,
            "mysql_records_deleted": False,
        }
        refusals = []
        if environment != "test":
            refusals.append("test_environment_required")
        if target is None:
            refusals.append("content_target_not_found")
        elif target.connection_name != settings.CONTENT_SEARCH_CONNECTION_NAME:
            refusals.append("runtime_connection_target_mismatch")
        else:
            report.update(
                {
                    "connection_name": target.connection_name,
                    "index_name": target.index_name,
                    "target_enabled": target.enabled,
                    "target_role": target.role,
                }
            )
            try:
                validate_content_index_name(target.index_name, index_prefix)
            except ValueError:
                refusals.append("test_content_index_prefix_required")

            unfinished = ContentSearchDelivery.objects.filter(
                target=target, status__in=_UNFINISHED_STATUSES
            ).count()
            report["unfinished_delivery_count"] = unfinished
            if unfinished:
                refusals.append("content_deliveries_not_caught_up")

            try:
                current_indices = tuple(
                    get_content_search_read_alias_indices(target, alias, index_prefix=index_prefix)
                )
            except ContentSearchElasticsearchError as error:
                current_indices = ()
                refusals.append(error.code)
            report["current_indices"] = list(current_indices)
            if target.index_name in current_indices:
                refusals.append("target_still_serving_alias")

            if options["retire_only"]:
                if target.role == ContentSearchTargetRole.RETIRED and not target.enabled:
                    refusals.append("target_already_retired")
            elif target.enabled or target.role != ContentSearchTargetRole.RETIRED:
                refusals.append("target_must_be_retired_before_purge")

        if not options["retire_only"] and target is not None and not refusals:
            client = get_content_search_client(target)
            template_name = content_index_template_name(target.index_name)
            try:
                report["index_exists"] = bool(client.indices.exists(index=target.index_name))
                report["template_exists"] = bool(client.indices.exists_index_template(name=template_name))
            except Exception as error:
                refusals.append("es_test_target_inspection_failed")

        if not options["confirm"]:
            report["ready_for_confirm"] = not refusals
            if refusals:
                report["refused"] = refusals
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return
        if refusals:
            report["refused"] = refusals
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise CommandError("测试内容搜索目标清理门禁未满足")

        # ES 删除前再次读取 alias，避免并发切换后误删刚接管流量的索引。
        confirmed_indices = tuple(
            get_content_search_read_alias_indices(target, alias, index_prefix=index_prefix)
        )
        if target.index_name in confirmed_indices:
            report["refused"] = ["content_read_alias_changed"]
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise CommandError("测试内容 read alias 已变化，拒绝清理")

        if options["retire_only"]:
            with transaction.atomic():
                locked_target = ContentSearchTarget.objects.select_for_update().get(pk=target.pk)
                if ContentSearchDelivery.objects.filter(
                    target=locked_target, status__in=_UNFINISHED_STATUSES
                ).exists():
                    raise CommandError("测试内容投递状态已变化，拒绝退役")
                locked_target.enabled = False
                locked_target.required = False
                locked_target.role = ContentSearchTargetRole.RETIRED
                locked_target.save(update_fields=("enabled", "required", "role", "updated_at"))
                SearchIndexBuild.objects.filter(target=locked_target).update(
                    status=SearchIndexBuildStatus.RETIRED
                )
            report.update({"target_retired": True, "target_enabled": False, "target_role": "retired"})
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return

        client = get_content_search_client(target)
        template_name = content_index_template_name(target.index_name)
        try:
            if client.indices.exists(index=target.index_name):
                client.indices.delete(index=target.index_name)
                report["index_deleted"] = True
            if client.indices.exists_index_template(name=template_name):
                client.indices.delete_index_template(name=template_name)
                report["template_deleted"] = True
        except Exception as error:
            report["refused"] = ["es_test_target_purge_failed"]
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise CommandError("测试 Elasticsearch 历史索引清理失败；MySQL 审计记录未删除") from error

        with transaction.atomic():
            locked_target = ContentSearchTarget.objects.select_for_update().get(pk=target.pk)
            if locked_target.enabled or locked_target.role != ContentSearchTargetRole.RETIRED:
                raise CommandError("测试 Target 状态已变化，拒绝删除审计记录")
            ContentSearchDelivery.objects.filter(target=locked_target).delete()
            SearchIndexBuild.objects.filter(target=locked_target).delete()
            locked_target.delete()
        report["mysql_records_deleted"] = True
        self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
