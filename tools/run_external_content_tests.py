"""正文外置专用测试入口：私有凭据、固定双库拓扑和初始化前网络围栏。"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import socket
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.external_content_test_credentials import (
    ALLOWED_CREDENTIAL_KEYS,
    FIXED_CREDENTIALS,
    PASSWORD_KEY,
    SecurityError,
    get_credentials_path,
    read_private_credentials,
)


ALLOWED_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
DEFAULT_MYSQL_PORT = 13306
DEFAULT_MONGO_PORT = 13307
DEFAULT_TEST_LABELS = ["external_content.tests", "guides.tests"]
_GUARD_INSTALLED = False
_ORIGINAL_CONNECT = socket.socket.connect
_ORIGINAL_CONNECT_EX = socket.socket.connect_ex
_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_ORIGINAL_GETHOSTBYNAME = socket.gethostbyname
_ORIGINAL_GETHOSTBYNAME_EX = socket.gethostbyname_ex


def load_test_credentials(custom_path: Path | None = None) -> dict[str, str]:
    """所有校验完成才注入环境；已有冲突不能通过覆盖被悄悄掩盖。"""
    loaded = read_private_credentials(
        custom_path if custom_path is not None else get_credentials_path()
    )
    if any(
        key in os.environ and os.environ[key] != value
        for key, value in loaded.items()
    ):
        raise SecurityError("ERR_CREDENTIALS_ENV_CONFLICT")
    os.environ.update(loaded)
    return loaded


def get_allowed_ports() -> set[int]:
    """只允许专用端口；异常链不保留非法输入。"""
    for service, expected in (
        ("MYSQL", DEFAULT_MYSQL_PORT),
        ("MONGO", DEFAULT_MONGO_PORT),
    ):
        raw = os.environ.get(f"EXTERNAL_CONTENT_TEST_{service}_PORT", str(expected))
        try:
            int(raw)
        except (TypeError, ValueError):
            raise SecurityError(f"ERR_ENV_{service}_PORT_INVALID") from None
        if raw != str(expected):
            raise SecurityError(f"ERR_ENV_{service}_PORT_UNAUTHORIZED")
    return {DEFAULT_MYSQL_PORT, DEFAULT_MONGO_PORT}


def _check_host(host: Any, code: str) -> None:
    """不调用未知对象的字符串转换，拒绝所有非回环主机。"""
    if isinstance(host, bytes):
        try:
            host = host.decode("ascii")
        except UnicodeError:
            raise SecurityError(code) from None
    if type(host) is not str or host.lower() not in ALLOWED_LOOPBACK_HOSTS:
        raise SecurityError(code)


def _check_port(port: Any, prefix: str) -> None:
    """整数解析与授权分开，两个失败分支均不回显输入或异常链。"""
    if type(port) not in (str, int, bytes):
        raise SecurityError(f"ERR_{prefix}_INVALID_PORT")
    try:
        numeric = int(port)
    except (ValueError, TypeError):
        raise SecurityError(f"ERR_{prefix}_INVALID_PORT") from None
    if numeric not in get_allowed_ports():
        raise SecurityError(f"ERR_{prefix}_DISALLOWED_PORT")


def _check_socket_target(address: Any) -> None:
    """Unix socket和未知地址类型不属于隔离双库授权范围。"""
    if isinstance(address, (str, bytes)):
        raise SecurityError("ERR_SOCKET_UNIX")
    if not isinstance(address, tuple) or len(address) not in (2, 4):
        raise SecurityError("ERR_SOCKET_TARGET")
    _check_host(address[0], "ERR_SOCKET_DISALLOWED_HOST")
    _check_port(address[1], "SOCKET")


def guarded_connect(connection: socket.socket, address: Any) -> None:
    """连接前固定校验回环地址与专用端口。"""
    _check_socket_target(address)
    return _ORIGINAL_CONNECT(connection, address)


def guarded_connect_ex(connection: socket.socket, address: Any) -> int:
    """connect_ex同样抛安全异常，不能把阻断伪装为普通网络错误。"""
    _check_socket_target(address)
    return _ORIGINAL_CONNECT_EX(connection, address)


def guarded_getaddrinfo(
    host: Any, port: Any, *args: Any, **kwargs: Any
) -> list[tuple[Any, ...]]:
    """只允许回环DNS解析；零端口解析不授予连接权限。"""
    _check_host(host, "ERR_DNS_DISALLOWED_HOST")
    if port is not None and port not in ("", 0, "0"):
        _check_port(port, "DNS")
    return _ORIGINAL_GETADDRINFO(host, port, *args, **kwargs)


def guarded_gethostbyname(hostname: str) -> str:
    """禁止外部DNS，错误只包含固定安全码。"""
    _check_host(hostname, "ERR_DNS_DISALLOWED_HOST")
    return _ORIGINAL_GETHOSTBYNAME(hostname)


def guarded_gethostbyname_ex(hostname: str) -> tuple[str, list[str], list[str]]:
    """多地址DNS入口沿用相同回环限制。"""
    _check_host(hostname, "ERR_DNS_DISALLOWED_HOST")
    return _ORIGINAL_GETHOSTBYNAME_EX(hostname)


def install_socket_guard() -> None:
    """幂等安装，必须先于Django及其App初始化。"""
    global _GUARD_INSTALLED
    if not _GUARD_INSTALLED:
        socket.socket.connect = guarded_connect
        socket.socket.connect_ex = guarded_connect_ex
        socket.getaddrinfo = guarded_getaddrinfo
        socket.gethostbyname = guarded_gethostbyname
        socket.gethostbyname_ex = guarded_gethostbyname_ex
        _GUARD_INSTALLED = True


def uninstall_socket_guard() -> None:
    """还原原生方法，供纯行为测试精确收尾。"""
    global _GUARD_INSTALLED
    if _GUARD_INSTALLED:
        socket.socket.connect = _ORIGINAL_CONNECT
        socket.socket.connect_ex = _ORIGINAL_CONNECT_EX
        socket.getaddrinfo = _ORIGINAL_GETADDRINFO
        socket.gethostbyname = _ORIGINAL_GETHOSTBYNAME
        socket.gethostbyname_ex = _ORIGINAL_GETHOSTBYNAME_EX
        _GUARD_INSTALLED = False


def is_socket_guard_installed() -> bool:
    """返回守卫状态，不进行网络探测。"""
    return _GUARD_INSTALLED


def verify_isolation_settings() -> None:
    """配置漂移立即终止；不回显目标、密码或底层异常。"""
    from django.conf import settings

    if set(settings.DATABASES) != {"default"}:
        raise SecurityError("ERR_SETTINGS_DATABASES")
    database = settings.DATABASES["default"]
    expected_database = {
        "ENGINE": "django.db.backends.mysql",
        "HOST": FIXED_CREDENTIALS["EXTERNAL_CONTENT_TEST_MYSQL_HOST"],
        "PORT": FIXED_CREDENTIALS["EXTERNAL_CONTENT_TEST_MYSQL_PORT"],
        "NAME": FIXED_CREDENTIALS["EXTERNAL_CONTENT_TEST_MYSQL_NAME"],
        "USER": FIXED_CREDENTIALS["EXTERNAL_CONTENT_TEST_MYSQL_USER"],
        "PASSWORD": os.environ.get(PASSWORD_KEY),
    }
    if not expected_database["PASSWORD"] or any(
        database.get(key) != value for key, value in expected_database.items()
    ):
        raise SecurityError("ERR_SETTINGS_MYSQL")
    if database.get("TEST", {}).get("NAME") != "test_g4_external_content":
        raise SecurityError("ERR_SETTINGS_MYSQL_TEST")
    mongo = getattr(settings, "MONGO_DB", {})
    if (
        mongo.get("HOST") != "127.0.0.1"
        or type(mongo.get("PORT")) is not int
        or mongo.get("PORT") != DEFAULT_MONGO_PORT
        or mongo.get("NAME") != "g4_external_content_tests"
    ):
        raise SecurityError("ERR_SETTINGS_MONGO")
    if getattr(settings, "WAGTAILSEARCH_BACKENDS", {}):
        raise SecurityError("ERR_SETTINGS_SEARCH")
    if not settings.CACHES or any(
        cache.get("BACKEND") != "django.core.cache.backends.locmem.LocMemCache"
        for cache in settings.CACHES.values()
    ):
        raise SecurityError("ERR_SETTINGS_CACHE")
    if (
        getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False) is not True
        or getattr(settings, "CELERY_BROKER_URL", "") != "memory://"
        or getattr(settings, "CELERY_RESULT_BACKEND", "") != "cache+locmem://"
    ):
        raise SecurityError("ERR_SETTINGS_CELERY")


def main() -> None:
    """严格初始化顺序，所有双库测试只使用单个DiscoverRunner进程。"""
    parser = argparse.ArgumentParser(description="正文外置隔离双库测试入口")
    parser.add_argument("test_labels", nargs="*", default=DEFAULT_TEST_LABELS)
    parser.add_argument("--keepdb", action="store_true")
    parser.add_argument("--failfast", action="store_true")
    parser.add_argument("-v", "--verbosity", type=int, choices=range(4), default=1)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_test_credentials()
    os.environ["DJANGO_SETTINGS_MODULE"] = "wagtailblog3.settings.external_content_test"
    install_socket_guard()
    get_allowed_ports()
    print("[Runner] 私有凭据校验通过，固定双库网络守卫已安装。")

    import django

    django.setup()
    verify_isolation_settings()
    if args.dry_run:
        print("[Runner] 初始化与隔离配置预检通过；未执行测试。")
        return

    from django.test.runner import DiscoverRunner

    runner = DiscoverRunner(
        verbosity=args.verbosity,
        interactive=False,
        keepdb=args.keepdb,
        failfast=args.failfast,
        parallel=0,
    )
    failures = runner.run_tests(args.test_labels)
    sys.exit(bool(failures))


if __name__ == "__main__":
    try:
        main()
    except SecurityError as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
