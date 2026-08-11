"""输出搜索架构 WP0 的只读基线报告。"""

import json
import os

import django
import elasticsearch
import pymongo
import wagtail
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from wagtail.models import Page
from wagtail.search.backends import get_search_backend

from blog.models import BlogPage


class Command(BaseCommand):
    """采集版本、索引和集合摘要，不读取正文也不执行写操作。"""

    help = "输出 WP0 搜索基线的只读 JSON 报告"

    def add_arguments(self, parser):
        parser.add_argument(
            "--strict",
            action="store_true",
            help="任一外部依赖无法读取时返回失败状态。",
        )

    def handle(self, *args, **options):
        report = {
            "report": "search-baseline-v1",
            "environment": os.environ.get("WAGTAILBLOG_ENV", "test"),
            "read_only": True,
            "runtime": {
                "django": django.get_version(),
                "wagtail": wagtail.__version__,
                "elasticsearch_client": getattr(elasticsearch, "__versionstr__", "unknown"),
                "pymongo": getattr(pymongo, "version", "unknown"),
            },
            "mysql": None,
            "mongodb": None,
            "elasticsearch": None,
            "errors": [],
        }

        for name, collector in (
            ("mysql", self._mysql_summary),
            ("mongodb", self._mongo_summary),
            ("elasticsearch", self._elasticsearch_summary),
        ):
            try:
                report[name] = collector()
            except Exception as error:
                report["errors"].append(
                    {
                        "component": name,
                        "error_type": type(error).__name__,
                    }
                )

        self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))

        if options["strict"] and report["errors"]:
            raise CommandError("搜索基线报告未能读取所有外部依赖")

    def _mysql_summary(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT VERSION()")
            version = cursor.fetchone()[0]
        return {
            "engine": connection.vendor,
            "version": version,
            "blog_page_count": BlogPage.objects.count(),
            "live_blog_page_count": BlogPage.objects.live().count(),
        }

    def _mongo_summary(self):
        mongo_settings = settings.MONGO_DB
        client = pymongo.MongoClient(
            host=mongo_settings["HOST"],
            port=mongo_settings["PORT"],
            serverSelectionTimeoutMS=5000,
        )
        try:
            database = client[mongo_settings["NAME"]]
            build_info = client.admin.command("buildInfo")
            collections = {}
            for collection_name in ("blog_content", "blog_page_revision_bodies"):
                collection = database[collection_name]
                collections[collection_name] = {
                    "document_count": collection.count_documents({}),
                    "indexes": [
                        {
                            "name": item.get("name", ""),
                            "key": list(item.get("key", {}).items()),
                        }
                        for item in collection.list_indexes()
                    ],
                }
            return {
                "version": build_info.get("version", "unknown"),
                "collections": collections,
            }
        finally:
            client.close()

    def _elasticsearch_summary(self):
        backend = get_search_backend("default")
        client = getattr(backend, "es", None) or getattr(backend, "client", None)
        if client is None:
            raise RuntimeError("Wagtail 搜索后端未提供 Elasticsearch 客户端")

        index_reference = backend.get_index_for_model(Page).name
        index_names = sorted(
            client.indices.get_alias(index=index_reference).keys()
        )
        indices = {}
        for index_name in index_names:
            settings = client.indices.get_settings(index=index_name).get(index_name, {})
            mapping = client.indices.get_mapping(index=index_name).get(index_name, {})
            statistics = client.indices.stats(index=index_name, metric="store").get(index_name, {})
            index_settings = settings.get("settings", {}).get("index", {})
            properties = (
                mapping.get("mappings", {}).get("properties", {})
            )
            indices[index_name] = {
                "document_count": client.count(index=index_name).get("count", 0),
                "primary_shards": index_settings.get("number_of_shards"),
                "replicas": index_settings.get("number_of_replicas"),
                "primary_store_size_bytes": statistics.get("primaries", {})
                .get("store", {})
                .get("size_in_bytes", 0),
                "mapping_fields": sorted(properties.keys()),
            }

        info = client.info()
        health = client.cluster.health(index=",".join(index_names)) if index_names else {}
        return {
            "version": info.get("version", {}).get("number", "unknown"),
            "index_reference": index_reference,
            "indices": indices,
            "cluster_status": health.get("status", "unknown"),
        }
