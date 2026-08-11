"""只读检查 Elasticsearch 连接、集群健康和索引容量摘要。"""

from __future__ import annotations

import json
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from search.services.elasticsearch import get_content_search_client_for_connection


def _response_body(response):
    return getattr(response, "body", response)


def _node_capacity_summary(nodes_response):
    nodes = nodes_response.get("nodes", {}) if isinstance(nodes_response, dict) else {}
    summaries = []
    for node in nodes.values():
        jvm_mem = (node.get("jvm") or {}).get("mem") or {}
        fs_total = (node.get("fs") or {}).get("total") or {}
        summaries.append(
            {
                "heap_used_percent": jvm_mem.get("heap_used_percent"),
                "heap_max_in_bytes": jvm_mem.get("heap_max_in_bytes"),
                "fs_available_in_bytes": fs_total.get("available_in_bytes"),
            }
        )
    heap_values = [
        item["heap_used_percent"]
        for item in summaries
        if isinstance(item["heap_used_percent"], (int, float))
    ]
    disk_values = [
        item["fs_available_in_bytes"]
        for item in summaries
        if isinstance(item["fs_available_in_bytes"], (int, float))
    ]
    return {
        "number_of_nodes": len(summaries),
        "max_heap_used_percent": max(heap_values) if heap_values else None,
        "min_fs_available_in_bytes": min(disk_values) if disk_values else None,
        "nodes": summaries,
    }


class Command(BaseCommand):
    help = "输出 Elasticsearch 集群只读预检报告，不创建索引、不写文档、不切换 alias"

    def add_arguments(self, parser):
        parser.add_argument(
            "--connection",
            default=None,
            help="settings 中的 Wagtail 搜索连接名，默认使用 CONTENT_SEARCH_CONNECTION_NAME。",
        )
        parser.add_argument("--index", default="", help="可选的精确物理索引名")
        parser.add_argument(
            "--snapshot-repository",
            default="",
            help="可选的 snapshot repository 名称，只读取校验",
        )
        parser.add_argument("--strict", action="store_true", help="集群状态非 green 时返回失败")

    def handle(self, *args, **options):
        connection_name = options["connection"] or settings.CONTENT_SEARCH_CONNECTION_NAME
        report = {
            "report": "search-cluster-preflight-v1",
            "environment": os.environ.get("WAGTAILBLOG_ENV", "unset"),
            "read_only": True,
            "connection": connection_name,
            "cluster": None,
            "health": None,
            "index": None,
            "capacity": None,
            "snapshot": None,
        }
        try:
            if connection_name not in settings.WAGTAILSEARCH_BACKENDS:
                raise CommandError("搜索连接未在 settings 中注册")
            client = get_content_search_client_for_connection(connection_name)
            info = client.info()
            health = client.cluster.health(
                index=options["index"] or None,
                level="cluster",
            )
            nodes = _response_body(client.nodes.stats(metric="jvm,fs"))
            report["cluster"] = {
                "name": info.get("cluster_name", "unknown"),
                "version": info.get("version", {}).get("number", "unknown"),
            }
            report["health"] = {
                "status": health.get("status", "unknown"),
                "number_of_nodes": health.get("number_of_nodes"),
                "active_primary_shards": health.get("active_primary_shards"),
                "active_shards": health.get("active_shards"),
                "unassigned_shards": health.get("unassigned_shards"),
            }
            report["capacity"] = _node_capacity_summary(nodes)
            if options["index"]:
                index_name = options["index"]
                stats = client.indices.stats(index=index_name, metric="docs,store")
                index_stats = stats.get("indices", {}).get(index_name, {})
                report["index"] = {
                    "name": index_name,
                    "docs_count": index_stats.get("primaries", {}).get("docs", {}).get("count", 0),
                    "store_size_in_bytes": index_stats.get("primaries", {})
                    .get("store", {})
                    .get("size_in_bytes", 0),
                }
            if options["snapshot_repository"]:
                snapshot = client.snapshot.get_repository(
                    name=options["snapshot_repository"],
                )
                snapshot_body = _response_body(snapshot)
                report["snapshot"] = {
                    "repository": options["snapshot_repository"],
                    "exists": bool(snapshot_body),
                    "repositories": sorted(snapshot_body)
                    if isinstance(snapshot_body, dict)
                    else [],
                }
        except CommandError:
            raise
        except Exception as error:
            report["error"] = {"type": type(error).__name__}
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise CommandError("Elasticsearch 只读预检失败") from error
        self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
        if options["strict"] and report["health"]["status"] != "green":
            raise CommandError("Elasticsearch 集群健康状态不是 green")
