# -*- coding: utf-8 -*-
"""
Wagtailblog3 正文外置通用协议专用测试隔离配置 (feat/external-content-guides)。

本配置文件专为正文外置通用协议 (external_content) 的真实契约测试打造，严格执行以下隔离约束：
1. 从 .base 继承（绝不继承 .dev，坚决防止 .local 覆盖及开发调试逻辑污染）；
2. 在导入 .base 之前锁定环境变量为本地回环与隔离测试专用参数，彻底杜绝 .env 秘密回退到生产 IP (192.168.20.2)；
3. MySQL 采用真实 8.4 协议（独立库 g4_external_content 与 test_g4_external_content，端口 13306），拒绝 SQLite 冒充验证锁；
4. MongoDB 采用独立库 g4_external_content_tests（端口 13307，单节点 Write Concern w=1）；
5. 彻底屏蔽外部依赖：CACHES 走 LocMemCache，STORAGES 走 InMemoryStorage，ES 搜索后端置空，Celery 走内存同步模式；
6. 模块末尾设置强力安全断言，发现任何配置偏离隔离边界立即终止启动。
"""

import os
import sys
from typing import Any, Dict

class SecurityError(RuntimeError):
    """测试隔离安全配置异常，拦截非法端口、未授权数据库或缺失密码。"""
    pass

# 必须在 import .base 之前完成测试密码、端口与库名的硬隔离校验
_raw_pwd = os.environ.get("EXTERNAL_CONTENT_TEST_MYSQL_PASSWORD")
if not _raw_pwd:
    raise SecurityError("安全隔离校验失败: 缺少必须的测试密码 EXTERNAL_CONTENT_TEST_MYSQL_PASSWORD")

if "EXTERNAL_CONTENT_TEST_MYSQL_PORT" in os.environ and os.environ["EXTERNAL_CONTENT_TEST_MYSQL_PORT"] != "13306":
    raise SecurityError("安全拦截: 拒绝覆盖 MySQL 测试端口，必须固定为 13306")
if "EXTERNAL_CONTENT_TEST_MONGO_PORT" in os.environ and os.environ["EXTERNAL_CONTENT_TEST_MONGO_PORT"] != "13307":
    raise SecurityError("安全拦截: 拒绝覆盖 MongoDB 测试端口，必须固定为 13307")
if "EXTERNAL_CONTENT_TEST_MYSQL_NAME" in os.environ and os.environ["EXTERNAL_CONTENT_TEST_MYSQL_NAME"] != "g4_external_content":
    raise SecurityError("安全拦截: 拒绝覆盖 MySQL 测试数据库名，必须固定为 g4_external_content")
if "EXTERNAL_CONTENT_TEST_MONGO_NAME" in os.environ and os.environ["EXTERNAL_CONTENT_TEST_MONGO_NAME"] != "g4_external_content_tests":
    raise SecurityError("安全拦截: 拒绝覆盖 MongoDB 测试数据库名，必须固定为 g4_external_content_tests")

os.environ["MYSQL_PASSWORD"] = _raw_pwd

# ==========================================================
# 0. 导入 base 前的环境变量强制锁死（阻断 .env / .env.test 远程覆盖）
# ==========================================================
# base.py 使用 load_dotenv(..., override=False)；
# 只要在此处预先注入回环与测试专用变量，任何生产或远程主机配置都绝不可能生效。
os.environ["WAGTAILBLOG_ENV"] = "test"
os.environ["SERVICE_HOST"] = "127.0.0.1"

# MySQL 测试环境变量（优先读取外部凭据注入，默认回退安全参数）
os.environ["MYSQL_HOST"] = "127.0.0.1"
os.environ["MYSQL_PORT"] = os.environ.get("EXTERNAL_CONTENT_TEST_MYSQL_PORT", "13306")
os.environ["MYSQL_NAME"] = os.environ.get("EXTERNAL_CONTENT_TEST_MYSQL_NAME", "g4_external_content")
os.environ["MYSQL_USER"] = os.environ.get("EXTERNAL_CONTENT_TEST_MYSQL_USER", "g4_test_user")
os.environ["MYSQL_PASSWORD"] = os.environ.get("EXTERNAL_CONTENT_TEST_MYSQL_PASSWORD", "")

# MongoDB 测试环境变量
os.environ["MONGO_HOST"] = "127.0.0.1"
os.environ["MONGO_PORT"] = os.environ.get("EXTERNAL_CONTENT_TEST_MONGO_PORT", "13307")
os.environ["MONGO_NAME"] = os.environ.get("EXTERNAL_CONTENT_TEST_MONGO_NAME", "g4_external_content_tests")

# Redis 环境变量隔离（阻断向 6379 或生产 Redis 的任何潜在流量）
os.environ["REDIS_HOST"] = "127.0.0.1"
os.environ["REDIS_PORT"] = "16379"
os.environ["REDIS_PASSWORD"] = ""

# Elasticsearch 环境变量隔离
os.environ["ELASTICSEARCH_HOST"] = "127.0.0.1"
os.environ["ELASTICSEARCH_PORT"] = "19200"

# MinIO 环境变量隔离
os.environ["MINIO_ENDPOINT"] = "http://127.0.0.1:19000"
os.environ["MINIO_ACCESS_KEY"] = "test_minio_key"
os.environ["MINIO_SECRET_KEY"] = "test_minio_secret"

# 搜索与异步特性开关隔离（静默所有外部生产者/消费者）
os.environ["CONTENT_SEARCH_PRODUCER_ENABLED"] = "0"
os.environ["CONTENT_SEARCH_CONSUMER_ENABLED"] = "0"
os.environ["CONTENT_SEARCH_CURSOR_ENABLED"] = "0"
os.environ["CONTENT_SEARCH_PIT_ENABLED"] = "0"
os.environ["CONTENT_SEARCH_RECONCILE_ENABLED"] = "0"
os.environ["CONTENT_SEARCH_SECONDARY_CONNECTION_ENABLED"] = "0"
os.environ["CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_ENABLED"] = "0"
os.environ["CONTENT_SEARCH_INDEX_PREFIX"] = "test_g4"
os.environ.setdefault(
    "DJANGO_SECRET_KEY", "insecure-test-key-for-g4-external-content-testing-only"
)

# ==========================================================
# 1. 导入基础配置（base.py）
# ==========================================================
from .base import *  # noqa: F401, F403

# ==========================================================
# 2. 基础运行参数覆盖
# ==========================================================
DEBUG: bool = False
ALLOWED_HOSTS: list[str] = ["127.0.0.1", "localhost"]

# 密码哈希优化（加速测试执行）
PASSWORD_HASHERS: list[str] = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

# ==========================================================
# 3. MySQL 真实协议数据库配置（支持行级排他锁并发验证）
# ==========================================================
TEST_MYSQL_HOST: str = "127.0.0.1"
TEST_MYSQL_PORT: int = 13306
TEST_MYSQL_NAME: str = "g4_external_content"
TEST_MYSQL_USER: str = os.environ.get("EXTERNAL_CONTENT_TEST_MYSQL_USER", "g4_test_user")
TEST_MYSQL_PASSWORD: str = os.environ.get("EXTERNAL_CONTENT_TEST_MYSQL_PASSWORD", "")

DATABASES: Dict[str, Dict[str, Any]] = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": TEST_MYSQL_NAME,
        "USER": TEST_MYSQL_USER,
        "PASSWORD": TEST_MYSQL_PASSWORD,
        "HOST": TEST_MYSQL_HOST,
        "PORT": str(TEST_MYSQL_PORT),
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": False,
        "OPTIONS": {
            "charset": "utf8mb4",
            "collation": "utf8mb4_general_ci",
        },
        "TEST": {
            "NAME": f"test_{TEST_MYSQL_NAME}",
            "CHARSET": "utf8mb4",
            "COLLATION": "utf8mb4_general_ci",
        },
    }
}

# ==========================================================
# 4. MongoDB 独立测试库配置（单节点测试环境适配）
# ==========================================================
TEST_MONGO_HOST: str = "127.0.0.1"
TEST_MONGO_PORT: int = 13307
TEST_MONGO_NAME: str = "g4_external_content_tests"

MONGO_DB: Dict[str, Any] = {
    "HOST": TEST_MONGO_HOST,
    "PORT": TEST_MONGO_PORT,
    "NAME": TEST_MONGO_NAME,
    "WRITE_CONCERN_W": 1,
    "WRITE_CONCERN_J": True,
    "READ_CONCERN_LEVEL": "local",
    "CONNECT_TIMEOUT_MS": 3000,
    "SERVER_SELECTION_TIMEOUT_MS": 3000,
}
MONGO_DEBUG: bool = False

# ==========================================================
# 5. 内存缓存隔离（彻底消除 Redis 依赖与网络外泄）
# ==========================================================
CACHES: Dict[str, Dict[str, Any]] = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "g4-test-locmem-default",
        "KEY_PREFIX": "g4_test",
    },
    "comment_rate_limit_cache": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "g4-test-locmem-rate",
        "KEY_PREFIX": "g4_test_rate",
    },
}

# ==========================================================
# 6. Wagtail 搜索与全文索引强阻断
# ==========================================================
WAGTAILSEARCH_BACKENDS: Dict[str, Any] = {}
CONTENT_SEARCH_PRODUCER_ENABLED: bool = False
CONTENT_SEARCH_CONSUMER_ENABLED: bool = False
CONTENT_SEARCH_CURSOR_ENABLED: bool = False
CONTENT_SEARCH_PIT_ENABLED: bool = False
CONTENT_SEARCH_RECONCILE_ENABLED: bool = False
CONTENT_SEARCH_SECONDARY_CONNECTION_ENABLED: bool = False
CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_ENABLED: bool = False
CONTENT_SEARCH_INDEX_PREFIX: str = "test_g4"
CONTENT_SEARCH_READ_ALIAS: str = "test_g4-read"
CONTENT_SEARCH_TITLE_SUGGESTIONS_READ_ALIAS: str = "test_g4-title-read"

# ==========================================================
# 7. 存储后端隔离（内存模拟，杜绝 MinIO 侧效应）
# ==========================================================
STORAGES: Dict[str, Dict[str, Any]] = {
    "default": {
        "BACKEND": "django.core.files.storage.InMemoryStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
    "images": {
        "BACKEND": "django.core.files.storage.InMemoryStorage",
    },
    "original_images": {
        "BACKEND": "django.core.files.storage.InMemoryStorage",
    },
    "documents": {
        "BACKEND": "django.core.files.storage.InMemoryStorage",
    },
    "wagtailmedia": {
        "BACKEND": "django.core.files.storage.InMemoryStorage",
    },
}

# ==========================================================
# 8. Celery 异步队列隔离（内存同步模式）
# ==========================================================
CELERY_TASK_ALWAYS_EAGER: bool = True
CELERY_TASK_EAGER_PROPAGATES: bool = True
CELERY_BROKER_URL: str = "memory://"
CELERY_RESULT_BACKEND: str = "cache+locmem://"

# ==========================================================
# 9. 邮件系统隔离（内存投递）
# ==========================================================
EMAIL_BACKEND: str = "django.core.mail.backends.locmem.EmailBackend"

# ==========================================================
# 10. 强力安全断言（严格自检，防止任何生产污染与泄漏）
# ==========================================================
_default_db = DATABASES.get("default", {})
_db_host = str(_default_db.get("HOST", ""))
_db_port = str(_default_db.get("PORT", ""))
_db_name = str(_default_db.get("NAME", ""))
_db_test_name = str(_default_db.get("TEST", {}).get("NAME", ""))

if _db_host not in ("127.0.0.1", "localhost"):
    raise RuntimeError(f"安全隔离断言失败: MySQL HOST 必须为回环地址，当前为: {_db_host}")
if _db_port != str(TEST_MYSQL_PORT):
    raise RuntimeError(f"安全隔离断言失败: MySQL PORT 必须为隔离端口 {TEST_MYSQL_PORT}，当前为: {_db_port}")
if "production" in _db_name.lower() or "production" in _db_test_name.lower():
    raise RuntimeError(f"安全隔离断言失败: 检测到生产数据库命名: {_db_name}")
if _db_name != TEST_MYSQL_NAME:
    raise RuntimeError(f"安全隔离断言失败: MySQL NAME 必须为 {TEST_MYSQL_NAME}，当前为: {_db_name}")
if _db_test_name != f"test_{TEST_MYSQL_NAME}":
    raise RuntimeError(f"安全隔离断言失败: MySQL TEST.NAME 必须为 test_{TEST_MYSQL_NAME}，当前为: {_db_test_name}")

_mongo_host = str(MONGO_DB.get("HOST", ""))
_mongo_port = int(MONGO_DB.get("PORT", 0))
_mongo_name = str(MONGO_DB.get("NAME", ""))

if _mongo_host not in ("127.0.0.1", "localhost"):
    raise RuntimeError(f"安全隔离断言失败: MongoDB HOST 必须为回环地址，当前为: {_mongo_host}")
if _mongo_port != TEST_MONGO_PORT:
    raise RuntimeError(f"安全隔离断言失败: MongoDB PORT 必须为隔离端口 {TEST_MONGO_PORT}，当前为: {_mongo_port}")
if _mongo_name != TEST_MONGO_NAME:
    raise RuntimeError(f"安全隔离断言失败: MongoDB NAME 必须为 {TEST_MONGO_NAME}，当前为: {_mongo_name}")

if WAGTAILSEARCH_BACKENDS:
    raise RuntimeError("安全隔离断言失败: WAGTAILSEARCH_BACKENDS 必须为空字典以阻断 Elasticsearch 连接")

_cache_backend = CACHES.get("default", {}).get("BACKEND", "")
if "locmem" not in _cache_backend.lower():
    raise RuntimeError(f"安全隔离断言失败: 默认缓存必须为 LocMemCache，当前为: {_cache_backend}")

if not CELERY_TASK_ALWAYS_EAGER:
    raise RuntimeError("安全隔离断言失败: CELERY_TASK_ALWAYS_EAGER 必须为 True")

# 环境变量严格校验：端口与数据库名严格锁定，拒绝任意环境变量覆盖
if "EXTERNAL_CONTENT_TEST_MYSQL_PORT" in os.environ and os.environ["EXTERNAL_CONTENT_TEST_MYSQL_PORT"] != "13306":
    raise RuntimeError("安全拦截: 拒绝覆盖 MySQL 测试端口，必须固定为 13306")
if "EXTERNAL_CONTENT_TEST_MONGO_PORT" in os.environ and os.environ["EXTERNAL_CONTENT_TEST_MONGO_PORT"] != "13307":
    raise RuntimeError("安全拦截: 拒绝覆盖 MongoDB 测试端口，必须固定为 13307")
if "EXTERNAL_CONTENT_TEST_MYSQL_NAME" in os.environ and os.environ["EXTERNAL_CONTENT_TEST_MYSQL_NAME"] != "g4_external_content":
    raise RuntimeError("安全拦截: 拒绝覆盖 MySQL 测试数据库名，必须固定为 g4_external_content")
if "EXTERNAL_CONTENT_TEST_MONGO_NAME" in os.environ and os.environ["EXTERNAL_CONTENT_TEST_MONGO_NAME"] != "g4_external_content_tests":
    raise RuntimeError("安全拦截: 拒绝覆盖 MongoDB 测试数据库名，必须固定为 g4_external_content_tests")
