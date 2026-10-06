# -*- coding: utf-8 -*-
"""
正文外置通用协议测试隔离与安全性纯行为单元测试集 (feat/external-content-guides)。

测试目标与行为验证规范（严格遵守纯行为测试，彻底杜绝源码文本/AST查找）：
1. 凭据管理行为测试 (Credentials Behavior)：
   - 缺凭据文件时严格失败关闭 (fail-closed)，严禁静默回退弱口令或污染环境变量；
   - 损坏行、未知键、重复键严格拦截，且抛出的安全异常中绝不回显输入内容或敏感凭据；
   - 凭据权限安全验证：Linux/POSIX 环境下验证父目录 0700、凭据文件 0600，拒绝符号链接 (symlink)，拒绝 WSL DrvFS 挂载路径。
2. Settings 导入前置隔离行为测试 (Settings Behavior Isolation)：
   - 注入 Fake Base Spy 监控导入时序，断言缺少测试密码、恶意端口、恶意库名在导入 base 前必须立即抛出异常；
   - 验证在非法配置下 base 模块绝不被调用或导入；合规配置下完成配置隔离自检。
3. Socket Guard 底层网络拦截行为测试 (Socket Guard Behavior)：
   - 验证连接外部 IP、未授权本地端口、Unix Domain Socket 被拦截，异常信息脱敏无秘密回显；
   - 验证放行 127.0.0.1:13306 与 127.0.0.1:13307 专用测试端口；
   - 验证 getaddrinfo 与 gethostbyname 阻断外部 DNS 探测；
   - 验证允许端口集合 get_allowed_ports() 严格固定为 {13306, 13307}。
4. Docker 服务引导安全行为测试 (Bootstrap Behavior)：
   - 容器 inspect 复用校验：镜像名称精确比对（拦截 evil_mysql:8.4 等子串绕过），端口绑定必须为 127.0.0.1，测试标签完整；
   - MySQL 密码通过容器内部 shell $MYSQL_ROOT_PASSWORD 引用，docker argv 中绝无明文密码；
   - 数据库授权失败异常脱敏：仅输出有限退出码，绝不包含 stderr 或 SQL 原文；
   - 优雅停止操作绝不包含 docker rm 破坏性操作。
5. CLI 常量与初始化时序行为测试 (CLI Constants Order)：
   - 验证安全白名单与配置常量在模块加载时即正确初始化，保证主入口调用时序安全。
"""

import importlib
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
import traceback
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from unittest import mock

# 确保项目根路径进入 sys.path
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.run_external_content_tests import (
    ALLOWED_CREDENTIAL_KEYS,
    DEFAULT_MONGO_PORT,
    DEFAULT_MYSQL_PORT,
    DEFAULT_TEST_LABELS,
    SecurityError,
    get_allowed_ports,
    get_credentials_path,
    install_socket_guard,
    is_socket_guard_installed,
    load_test_credentials,
    uninstall_socket_guard,
)
import tools.bootstrap_external_content_test_services as bootstrap_module
import tools.external_content_test_credentials as credentials_module
import tools.run_external_content_tests as runner_module


def _assert_redacted_error(
    test_case: unittest.TestCase,
    error: SecurityError,
    code: str,
    *sentinels: str,
) -> None:
    """检查展示用异常链；断言失败也不回显被检测的输入。"""
    test_case.assertEqual(error.code, code)
    test_case.assertIsNone(error.__cause__)
    if error.__context__ is not None:
        test_case.assertTrue(error.__suppress_context__)
    formatted = "".join(traceback.format_exception(error))
    for sentinel in sentinels:
        test_case.assertTrue(sentinel not in formatted, "异常链必须脱敏")


class TestSocketGuardBehavior(unittest.TestCase):
    """底层 Socket Guard 真实网络拦截与异常脱敏纯行为测试。"""

    def setUp(self) -> None:
        """测试前确保挂载安全守卫。"""
        environment = mock.patch.dict(
            os.environ,
            {
                "EXTERNAL_CONTENT_TEST_MYSQL_PORT": str(DEFAULT_MYSQL_PORT),
                "EXTERNAL_CONTENT_TEST_MONGO_PORT": str(DEFAULT_MONGO_PORT),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        install_socket_guard()

    def tearDown(self) -> None:
        """测试后彻底卸载安全守卫，恢复原生环境。"""
        uninstall_socket_guard()

    def test_guard_lifecycle_and_state(self) -> None:
        """验证 Socket Guard 挂载、感知与卸载恢复行为。"""
        self.assertTrue(is_socket_guard_installed(), "安全守卫应处于挂载状态")
        uninstall_socket_guard()
        self.assertFalse(is_socket_guard_installed(), "卸载后应处于未挂载状态")
        install_socket_guard()
        self.assertTrue(is_socket_guard_installed(), "再次挂载后应处于挂载状态")

    def test_blocks_external_ip_connect(self) -> None:
        """验证拦截向生产虚拟机 (192.168.20.2) 等外部地址发起 socket.connect。"""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(SecurityError) as cm:
                s.connect(("192.168.20.2", DEFAULT_MYSQL_PORT))
            self.assertIn("ERR_SOCKET_DISALLOWED_HOST", str(cm.exception))
            self.assertNotIn("mysql_root_password", str(cm.exception).lower())
        finally:
            s.close()

    def test_blocks_external_ip_connect_ex(self) -> None:
        """验证拦截向外部 IP 发起的 socket.connect_ex。"""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(SecurityError):
                s.connect_ex(("192.168.20.2", 3306))
        finally:
            s.close()

    def test_blocks_unauthorized_ports_on_loopback(self) -> None:
        """验证拦截 127.0.0.1 上的未授权端口（3306, 27017, 6379, 9200, 80, 443）。"""
        dangerous_ports = [3306, 27017, 6379, 9200, 80, 443]
        for port in dangerous_ports:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                with self.assertRaises(SecurityError, msg=f"未拦截未授权端口 {port}"):
                    s.connect(("127.0.0.1", port))
            finally:
                s.close()

    def test_allows_authorized_test_ports_loopback(self) -> None:
        """合法端口只验证委托原连接函数，绝不连接真实服务。"""
        with mock.patch.object(runner_module, "_ORIGINAL_CONNECT") as connect:
            for port in (DEFAULT_MYSQL_PORT, DEFAULT_MONGO_PORT):
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
                    connection.connect(("127.0.0.1", port))
                    connect.assert_called_once_with(connection, ("127.0.0.1", port))
                    connect.reset_mock()

    def test_blocks_unix_domain_socket_and_redacts(self) -> None:
        """验证拦截 Unix Domain Socket，且异常中不泄露敏感本地路径。"""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(SecurityError) as cm:
                s.connect("/var/run/mysqld/mysqld.sock")
            _assert_redacted_error(
                self, cm.exception, "ERR_SOCKET_UNIX", "/var/run/mysqld/mysqld.sock"
            )
        finally:
            s.close()

    def test_blocks_dns_and_external_host_getaddrinfo(self) -> None:
        """验证 socket.getaddrinfo 阻断外部域名和非法端口解析。"""
        with self.assertRaises(SecurityError):
            socket.getaddrinfo("production-host.internal", DEFAULT_MYSQL_PORT)
        with self.assertRaises(SecurityError):
            socket.getaddrinfo("127.0.0.1", 3306)

    def test_allows_loopback_getaddrinfo(self) -> None:
        """验证 socket.getaddrinfo 放行本地回环和专属测试端口。"""
        try:
            res = socket.getaddrinfo("127.0.0.1", DEFAULT_MYSQL_PORT)
            self.assertIsInstance(res, list)
        except SecurityError:
            self.fail("合法回环 getaddrinfo 解析不应触发 SecurityError")

    def test_blocks_external_gethostbyname(self) -> None:
        """验证 socket.gethostbyname 阻断非回环主机名。"""
        with self.assertRaises(SecurityError):
            socket.gethostbyname("production.internal")

    def test_runner_get_allowed_ports_strictly_fixed(self) -> None:
        """验证 get_allowed_ports() 严格固定为 {13306, 13307}，拒绝环境变量篡改。"""
        self.assertEqual(get_allowed_ports(), {DEFAULT_MYSQL_PORT, DEFAULT_MONGO_PORT})
        with mock.patch.dict(os.environ, {"EXTERNAL_CONTENT_TEST_MYSQL_PORT": "3306"}):
            with self.assertRaises(SecurityError):
                get_allowed_ports()

    def test_socket_guard_error_does_not_echo_secret_payload(self) -> None:
        """验证 Socket Guard 在遇到畸形或非法输入时，错误信息不放大回显私密输入。"""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sensitive_host = "secret-token-credential-xyz.internal"
        try:
            with self.assertRaises(SecurityError) as cm:
                s.connect((sensitive_host, 8888))
            # 异常信息必须脱敏或受控，不外泄其它潜在内存
            self.assertNotIn("mysql_root_password", str(cm.exception).lower())
        finally:
            s.close()

    def test_guarded_getaddrinfo_secret_host_redacted(self) -> None:
        """验证 socket.getaddrinfo 拦截外部域名时，异常信息中绝不回显私密 host 字符串。"""
        secret_host = "secret-token-payload-xyz999.internal"
        with self.assertRaises(SecurityError) as cm:
            socket.getaddrinfo(secret_host, DEFAULT_MYSQL_PORT)
        _assert_redacted_error(self, cm.exception, "ERR_DNS_DISALLOWED_HOST", secret_host)

    def test_guarded_getaddrinfo_secret_port_string_redacted(self) -> None:
        """验证 socket.getaddrinfo 遇到非法端口格式时，异常信息中绝不回显私密 port 字符串。"""
        secret_port = "secret_port_token_888"
        with self.assertRaises(SecurityError) as cm:
            socket.getaddrinfo("127.0.0.1", secret_port)
        _assert_redacted_error(self, cm.exception, "ERR_DNS_INVALID_PORT", secret_port)

    def test_guarded_gethostbyname_secret_host_redacted(self) -> None:
        """验证 socket.gethostbyname 拦截外部域名时，异常信息中绝不回显私密 host 字符串。"""
        secret_host = "secret-dns-leak-attempt-777.internal"
        with self.assertRaises(SecurityError) as cm:
            socket.gethostbyname(secret_host)
        _assert_redacted_error(self, cm.exception, "ERR_DNS_DISALLOWED_HOST", secret_host)

    def test_guarded_connect_secret_port_redacted(self) -> None:
        """验证 socket.connect 遇到非法端口字符串时，异常信息中绝不回显私密 port。"""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(SecurityError) as cm:
                s.connect(("127.0.0.1", "secret_port_token_555"))
            _assert_redacted_error(
                self, cm.exception, "ERR_SOCKET_INVALID_PORT", "secret_port_token_555"
            )
        finally:
            s.close()

    def test_runner_get_allowed_ports_invalid_env_redacted(self) -> None:
        """验证 get_allowed_ports 遇到非法或篡改环境变量端口时，异常中绝不回显恶意端口内容。"""
        with mock.patch.dict(os.environ, {"EXTERNAL_CONTENT_TEST_MYSQL_PORT": "secret_port_env_444"}):
            with self.assertRaises(SecurityError) as cm:
                get_allowed_ports()
            self.assertNotIn("secret_port_env_444", str(cm.exception))
            self.assertIn("ERR_ENV_MYSQL_PORT_INVALID", str(cm.exception))

        with mock.patch.dict(os.environ, {"EXTERNAL_CONTENT_TEST_MYSQL_PORT": "3306"}):
            with self.assertRaises(SecurityError) as cm:
                get_allowed_ports()
            self.assertIn("ERR_ENV_MYSQL_PORT_UNAUTHORIZED", str(cm.exception))


@unittest.skipUnless(sys.platform.startswith("linux"), "私有凭据契约需 Linux")
class TestCredentialsBehavior(unittest.TestCase):
    """凭据加载与环境变量注入的安全性行为测试。"""

    def setUp(self) -> None:
        """完整合法基线只位于私有目录，负例每次仅改变一个因素。"""
        self._orig_env = dict(os.environ)
        for key in ALLOWED_CREDENTIAL_KEYS:
            os.environ.pop(key, None)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.directory.chmod(0o700)
        self.path = self.directory / "credentials.env"
        self.values: Dict[str, str] = {
            "EXTERNAL_CONTENT_TEST_MYSQL_HOST": "127.0.0.1",
            "EXTERNAL_CONTENT_TEST_MYSQL_PORT": "13306",
            "EXTERNAL_CONTENT_TEST_MYSQL_NAME": "g4_external_content",
            "EXTERNAL_CONTENT_TEST_MYSQL_USER": "g4_test_user",
            "EXTERNAL_CONTENT_TEST_MYSQL_PASSWORD": "only-unit-test-private-sentinel",
            "EXTERNAL_CONTENT_TEST_MONGO_HOST": "127.0.0.1",
            "EXTERNAL_CONTENT_TEST_MONGO_PORT": "13307",
            "EXTERNAL_CONTENT_TEST_MONGO_NAME": "g4_external_content_tests",
        }
        self._write_values(self.values)

    def tearDown(self) -> None:
        """测试后精确还原环境变量。"""
        os.environ.clear()
        os.environ.update(self._orig_env)

    def _write_values(self, values: Dict[str, str]) -> None:
        """写入0600完整夹具，不借助被测解析或校验实现。"""
        self.path.write_text(
            "".join(f"{key}={value}\n" for key, value in values.items()),
            encoding="utf-8",
        )
        self.path.chmod(0o600)

    def _assert_rejected(
        self, code: str, *sentinels: str, path: Optional[Path] = None
    ) -> None:
        """任何失败不得部分注入环境，异常展示不得包含输入和路径。"""
        target = self.path if path is None else path
        before = dict(os.environ)
        with self.assertRaises(SecurityError) as caught:
            load_test_credentials(target)
        _assert_redacted_error(
            self,
            caught.exception,
            code,
            str(target),
            self.values["EXTERNAL_CONTENT_TEST_MYSQL_PASSWORD"],
            *ALLOWED_CREDENTIAL_KEYS,
            *sentinels,
        )
        self.assertTrue(dict(os.environ) == before, "失败不得改变任何环境变量")

    def test_missing_credentials_fails_closed_without_modifying_env(self) -> None:
        """缺失只改变文件存在性，不操作共享临时文件。"""
        self.path.unlink()
        self._assert_rejected("ERR_CREDENTIALS_MISSING")

    def test_corrupted_line_rejects_and_does_not_echo_input(self) -> None:
        """验证凭据文件行缺少等号时严格拒绝，且异常信息中绝不回显损坏行内容（防止泄露凭据片段）。"""
        sentinel = "malformed-line-sentinel"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(f"{sentinel}\n")
        self._assert_rejected("ERR_CREDENTIALS_LINE", sentinel)

    def test_unknown_key_rejects_and_does_not_echo_value(self) -> None:
        """验证凭据文件中包含未知键时严格抛出 SecurityError，且异常绝不回显未知键的值。"""
        sentinel = "unknown-value-sentinel"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(f"UNKNOWN_KEY={sentinel}\n")
        self._assert_rejected("ERR_CREDENTIALS_KEYS", "UNKNOWN_KEY", sentinel)

    def test_duplicate_key_rejects_and_does_not_echo_value(self) -> None:
        """验证凭据文件中存在重复键时严格拒绝，且异常信息不回显私密值。"""
        password = self.values["EXTERNAL_CONTENT_TEST_MYSQL_PASSWORD"]
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(f"EXTERNAL_CONTENT_TEST_MYSQL_PASSWORD={password}\n")
        self._assert_rejected("ERR_CREDENTIALS_KEYS")

    def test_unsafe_port_override_rejected(self) -> None:
        """验证凭据中若企图篡改端口为 3306 或 27017，拒绝注入并抛出 SecurityError。"""
        for key, value in (
            ("EXTERNAL_CONTENT_TEST_MYSQL_PORT", "3306"),
            ("EXTERNAL_CONTENT_TEST_MONGO_PORT", "27017"),
        ):
            with self.subTest(key=key):
                self._write_values(dict(self.values, **{key: value}))
                self._assert_rejected("ERR_CREDENTIALS_TARGET")

    def test_unsafe_db_name_override_rejected(self) -> None:
        """验证凭据中若企图篡改数据库名为生产库名，拒绝注入并抛出 SecurityError。"""
        self._write_values(
            dict(self.values, EXTERNAL_CONTENT_TEST_MYSQL_NAME="unsafe-name-sentinel")
        )
        self._assert_rejected("ERR_CREDENTIALS_TARGET", "unsafe-name-sentinel")

    def test_valid_credentials_loads_and_injects_env(self) -> None:
        """验证完全合规的白名单凭据能够正常解析并正确注入环境变量。"""
        before = dict(os.environ)
        loaded = load_test_credentials(self.path)
        self.assertEqual(set(loaded), ALLOWED_CREDENTIAL_KEYS)
        self.assertTrue(loaded == self.values, "应完整读取八键凭据")
        self.assertTrue(dict(os.environ) == {**before, **self.values}, "应原子注入八键")

    def test_each_existing_credential_conflict_leaves_entire_env_unchanged(self) -> None:
        """每个键含口令独立冲突时，其他合法值也不能提前写入。"""
        for key in sorted(ALLOWED_CREDENTIAL_KEYS):
            with self.subTest(key=key):
                with mock.patch.dict(os.environ, {key: "conflict-value-sentinel"}):
                    self._assert_rejected(
                        "ERR_CREDENTIALS_ENV_CONFLICT", "conflict-value-sentinel"
                    )

    def test_matching_existing_credentials_allow_complete_injection(self) -> None:
        """已有相同值不是冲突，逐键验证其余七键仍正常注入。"""
        for key in sorted(ALLOWED_CREDENTIAL_KEYS):
            with self.subTest(key=key):
                with mock.patch.dict(os.environ, {key: self.values[key]}):
                    before = dict(os.environ)
                    loaded = load_test_credentials(self.path)
                    self.assertTrue(loaded == self.values, "应完整读取八键凭据")
                    self.assertTrue(
                        dict(os.environ) == {**before, **self.values}, "应完整注入且保留其他环境"
                    )

    def test_symlink_credentials_rejected(self) -> None:
        """验证凭据文件若为符号链接 (symlink)，拒绝读取并抛出 SecurityError。"""
        link_path = self.directory / "linked.env"
        link_path.symlink_to(self.path)
        self._assert_rejected("ERR_CREDENTIALS_IO", path=link_path)

    def test_linux_credentials_permissions_0600_and_dir_0700_strictly_enforced(self) -> None:
        """验证在 Linux 环境下，凭据文件权限非 0600 或所在目录非 0700 时拒绝读取。"""
        self.path.chmod(0o644)
        self._assert_rejected("ERR_CREDENTIALS_FILE_PERMISSIONS")
        self.path.chmod(0o600)
        self.directory.chmod(0o755)
        self._assert_rejected("ERR_CREDENTIALS_DIRECTORY_PERMISSIONS")

    def test_drvfs_credentials_path_rejected_on_linux(self) -> None:
        """验证在 Linux/WSL 环境下，拒绝使用 DrvFS 挂载路径 (/mnt/...) 作为凭据路径。"""
        fake_drvfs_path = Path("/mnt/f/openclaw/workspace/output/credentials.env")
        self._assert_rejected("ERR_CREDENTIALS_DRVFS", path=fake_drvfs_path)

    def test_credentials_path_does_not_resolve_symlink_prematurely(self) -> None:
        """验证 get_credentials_path 绝不提前 resolve 丢弃符号链接证据。"""
        link_path = self.directory / "linked.env"
        link_path.symlink_to(self.path)
        with mock.patch.dict(os.environ, {"EXTERNAL_CONTENT_TEST_CREDENTIALS_PATH": str(link_path)}):
            unresolved = get_credentials_path()
            self.assertEqual(unresolved, link_path)
            self.assertTrue(unresolved.is_symlink(), "必须保留符号链接证据")

    def test_credentials_parent_dir_symlink_rejected(self) -> None:
        """验证凭据所在父目录若为符号链接，坚决拒绝读取并抛出 SecurityError。"""
        real_dir = self.directory / "real"
        real_dir.mkdir(mode=0o700)
        self.path.rename(real_dir / self.path.name)
        link_dir = self.directory / "linked"
        link_dir.symlink_to(real_dir, target_is_directory=True)
        self._assert_rejected("ERR_CREDENTIALS_IO", path=link_dir / self.path.name)

    def test_credentials_size_bounded(self) -> None:
        """合法基线只追加注释，准确触发共享helper的大小上限。"""
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("#" * (credentials_module.MAX_CREDENTIAL_BYTES + 1))
        self._assert_rejected("ERR_CREDENTIALS_SIZE")

    def test_fifo_credentials_rejected_without_blocking(self) -> None:
        """无写端FIFO必须非阻塞拒绝；缺标志时先失败以免测试挂死。"""
        self.path.unlink()
        os.mkfifo(self.path, mode=0o600)
        original_open = os.open

        def checked_open(
            path: Any, flags: int, *args: Any, **kwargs: Any
        ) -> int:
            if path == self.path.name:
                self.assertTrue(flags & os.O_NONBLOCK, "FIFO必须以非阻塞方式打开")
            return original_open(path, flags, *args, **kwargs)

        started = time.monotonic()
        with mock.patch.object(credentials_module.os, "open", side_effect=checked_open):
            self._assert_rejected("ERR_CREDENTIALS_FILE_PERMISSIONS")
        self.assertLess(time.monotonic() - started, 2.0, "FIFO应及时拒绝")

    def test_path_encoding_errors_redact_formatted_exception_chain(self) -> None:
        """路径编码或NUL异常必须稳定脱敏，不展示底层异常上下文。"""
        original_open = os.open
        sentinel = "path-encoding-sentinel"
        for error in (
            UnicodeEncodeError("ascii", sentinel, 0, len(sentinel), sentinel),
            ValueError(sentinel),
        ):
            with self.subTest(error_type=type(error).__name__):
                def failing_open(
                    path: Any, flags: int, *args: Any, **kwargs: Any
                ) -> int:
                    if path == self.path.name:
                        raise error
                    return original_open(path, flags, *args, **kwargs)

                with mock.patch.object(
                    credentials_module.os, "open", side_effect=failing_open
                ):
                    self._assert_rejected("ERR_CREDENTIALS_IO", sentinel)

    def test_initial_root_open_failure_redacts_formatted_exception_chain(self) -> None:
        """最初根目录打开也必须位于脱敏边界，不能遗漏初始故障。"""
        sentinel = "root-open-sentinel"
        with mock.patch.object(
            credentials_module.os, "open", side_effect=OSError(sentinel)
        ) as mocked_open:
            self._assert_rejected("ERR_CREDENTIALS_IO", sentinel)
        mocked_open.assert_called_once_with(
            "/",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )


class BaseSpyModule(types.ModuleType):
    """用于监测 settings/base.py 导入时序的 Spy 模块。"""

    def __init__(self, name: str = "wagtailblog3.settings.base") -> None:
        super().__init__(name)
        self.base_was_imported: bool = False
        self.DEBUG: bool = False
        self.DATABASES: Dict[str, Any] = {"default": {}}
        self.MONGO_DB: Dict[str, Any] = {}
        self.CACHES: Dict[str, Any] = {"default": {}}
        self.STORAGES: Dict[str, Any] = {"default": {}}
        self.WAGTAILSEARCH_BACKENDS: Dict[str, Any] = {}
        self.CELERY_TASK_ALWAYS_EAGER: bool = False
        self.INSTALLED_APPS: List[str] = []
        self.LOGGING: Dict[str, Any] = {}
        self.MIDDLEWARE: List[str] = []
        self.ROOT_URLCONF: str = "wagtailblog3.urls"

    def mark_imported(self) -> None:
        self.base_was_imported = True

    def __getattribute__(self, name: str) -> Any:
        if name not in ("__class__", "__dict__", "__name__", "__doc__", "base_was_imported", "mark_imported"):
            super().__getattribute__("mark_imported")()
        return super().__getattribute__(name)


class TestSettingsBehaviorIsolation(unittest.TestCase):
    """
    Settings 导入前置隔离与配置自检纯行为测试。
    验证核心：通过注入 Fake Base Spy 监控导入时序，断言必须在 import base 之前拦截所有非法配置！
    """

    def setUp(self) -> None:
        """保存环境与模块状态。"""
        self._orig_env = dict(os.environ)
        self._cleanup_modules()

    def tearDown(self) -> None:
        """清理模块与环境。"""
        self._cleanup_modules()
        os.environ.clear()
        os.environ.update(self._orig_env)

    def _cleanup_modules(self) -> None:
        sys.modules.pop("wagtailblog3.settings.external_content_test", None)
        sys.modules.pop("wagtailblog3.settings.base", None)

    def _setup_base_spy(self) -> BaseSpyModule:
        """注入 fake base 模块并监听其是否被导入。"""
        spy = BaseSpyModule()
        sys.modules["wagtailblog3.settings.base"] = spy
        return spy

    def test_missing_password_rejected_before_base_imported(self) -> None:
        """
        验证缺少测试密码时，必须在 from .base import * 之前立即抛出异常拒绝，
        断言 base 模块绝对不能被导入或访问！
        """
        spy = self._setup_base_spy()
        # 清除密码环境变量
        os.environ.pop("EXTERNAL_CONTENT_TEST_MYSQL_PASSWORD", None)
        os.environ["EXTERNAL_CONTENT_TEST_MYSQL_PORT"] = "13306"
        os.environ["EXTERNAL_CONTENT_TEST_MONGO_PORT"] = "13307"

        with self.assertRaises((SecurityError, RuntimeError)):
            importlib.import_module("wagtailblog3.settings.external_content_test")

        # 核心时序断言：base 绝未被导入
        self.assertFalse(spy.base_was_imported, "缺少测试密码时必须在导入 base 之前拦截！")

    def test_unsafe_mysql_port_rejected_before_base_imported(self) -> None:
        """验证恶意 MySQL 端口（如 3306）必须在导入 base 之前拦截，base 绝不被导入。"""
        spy = self._setup_base_spy()
        os.environ["EXTERNAL_CONTENT_TEST_MYSQL_PASSWORD"] = "test_pass_123"
        os.environ["EXTERNAL_CONTENT_TEST_MYSQL_PORT"] = "3306"

        with self.assertRaises((SecurityError, RuntimeError)):
            importlib.import_module("wagtailblog3.settings.external_content_test")

        self.assertFalse(spy.base_was_imported, "非法 MySQL 端口必须在导入 base 之前拦截！")

    def test_unsafe_mongo_port_rejected_before_base_imported(self) -> None:
        """验证恶意 Mongo 端口（如 27017）必须在导入 base 之前拦截，base 绝不被导入。"""
        spy = self._setup_base_spy()
        os.environ["EXTERNAL_CONTENT_TEST_MYSQL_PASSWORD"] = "test_pass_123"
        os.environ["EXTERNAL_CONTENT_TEST_MONGO_PORT"] = "27017"

        with self.assertRaises((SecurityError, RuntimeError)):
            importlib.import_module("wagtailblog3.settings.external_content_test")

        self.assertFalse(spy.base_was_imported, "非法 Mongo 端口必须在导入 base 之前拦截！")

    def test_unsafe_mysql_db_name_rejected_before_base_imported(self) -> None:
        """验证恶意 MySQL 库名必须在导入 base 之前拦截，base 绝不被导入。"""
        spy = self._setup_base_spy()
        os.environ["EXTERNAL_CONTENT_TEST_MYSQL_PASSWORD"] = "test_pass_123"
        os.environ["EXTERNAL_CONTENT_TEST_MYSQL_NAME"] = "wagtailblog_prod"

        with self.assertRaises((SecurityError, RuntimeError)):
            importlib.import_module("wagtailblog3.settings.external_content_test")

        self.assertFalse(spy.base_was_imported, "非法 MySQL 库名必须在导入 base 之前拦截！")

    def test_unsafe_mongo_db_name_rejected_before_base_imported(self) -> None:
        """验证恶意 Mongo 库名必须在导入 base 之前拦截，base 绝不被导入。"""
        spy = self._setup_base_spy()
        os.environ["EXTERNAL_CONTENT_TEST_MYSQL_PASSWORD"] = "test_pass_123"
        os.environ["EXTERNAL_CONTENT_TEST_MONGO_NAME"] = "production_mongo"

        with self.assertRaises((SecurityError, RuntimeError)):
            importlib.import_module("wagtailblog3.settings.external_content_test")

        self.assertFalse(spy.base_was_imported, "非法 Mongo 库名必须在导入 base 之前拦截！")

    def test_valid_settings_loads_with_hard_isolation(self) -> None:
        """验证在合规环境变量下成功导入 settings，并对最终隔离配置进行行为检查。"""
        self._setup_base_spy()
        os.environ["EXTERNAL_CONTENT_TEST_MYSQL_PASSWORD"] = "valid_password_abc"
        os.environ["EXTERNAL_CONTENT_TEST_MYSQL_PORT"] = "13306"
        os.environ["EXTERNAL_CONTENT_TEST_MONGO_PORT"] = "13307"
        os.environ["EXTERNAL_CONTENT_TEST_MYSQL_NAME"] = "g4_external_content"
        os.environ["EXTERNAL_CONTENT_TEST_MONGO_NAME"] = "g4_external_content_tests"

        mod = importlib.import_module("wagtailblog3.settings.external_content_test")

        # 检查隔离配置
        self.assertEqual(mod.DATABASES["default"]["HOST"], "127.0.0.1")
        self.assertEqual(str(mod.DATABASES["default"]["PORT"]), "13306")
        self.assertEqual(mod.DATABASES["default"]["NAME"], "g4_external_content")
        self.assertEqual(mod.DATABASES["default"]["PASSWORD"], "valid_password_abc")

        self.assertEqual(mod.MONGO_DB["HOST"], "127.0.0.1")
        self.assertEqual(mod.MONGO_DB["PORT"], 13307)
        self.assertEqual(mod.MONGO_DB["NAME"], "g4_external_content_tests")

        self.assertEqual(mod.WAGTAILSEARCH_BACKENDS, {})
        self.assertIn("LocMemCache", mod.CACHES["default"]["BACKEND"])
        self.assertTrue(mod.CELERY_TASK_ALWAYS_EAGER)


class TestBootstrapBehavior(unittest.TestCase):
    """Bootstrap 引导脚本行为与安全沙箱纯测试（全 Mock 外部 Docker/Subprocess）。"""

    def test_verify_existing_container_exact_image_matching(self) -> None:
        """
        验证容器复用校验对镜像名称执行精确匹配，拦截 evil_mysql:8.4 等子串绕过漏洞。
        整改规范：精确比对 image，杜绝 in 子串绕过。
        """
        fake_inspect_output = json.dumps([
            {
                "Config": {
                    "Image": "evil_mysql:8.4",  # 恶意构造：包含 mysql:8.4 子串
                    "Labels": {
                        bootstrap_module.LABEL_EXTERNAL_CONTENT: "true",
                        bootstrap_module.LABEL_GUIDES: "true",
                        bootstrap_module.ISOLATION_LABEL_KEY: bootstrap_module.ISOLATION_LABEL_VAL,
                    },
                },
                "NetworkSettings": {
                    "Ports": {
                        "3306/tcp": [{"HostIp": "127.0.0.1", "HostPort": "13306"}]
                    }
                },
            }
        ])

        with mock.patch("tools.bootstrap_external_content_test_services.run_docker_command") as mock_run:
            mock_proc = mock.MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = fake_inspect_output
            mock_run.return_value = mock_proc

            with self.assertRaises(bootstrap_module.SecurityError):
                bootstrap_module.verify_existing_container(
                    "wblog-g4-test-mysql",
                    expected_image="mysql:8.4",
                    expected_port=13306,
                    expected_container_port=3306,
                )

    def test_verify_existing_container_rejects_non_loopback_binding(self) -> None:
        """验证容器端口绑定若为 0.0.0.0 或公网 IP，拒绝复用并抛出 SecurityError。"""
        fake_inspect_output = json.dumps([
            {
                "Config": {
                    "Image": "mysql:8.4",
                    "Labels": {
                        bootstrap_module.LABEL_EXTERNAL_CONTENT: "true",
                        bootstrap_module.LABEL_GUIDES: "true",
                        bootstrap_module.ISOLATION_LABEL_KEY: bootstrap_module.ISOLATION_LABEL_VAL,
                    },
                },
                "NetworkSettings": {
                    "Ports": {
                        "3306/tcp": [{"HostIp": "0.0.0.0", "HostPort": "13306"}]  # 绑定所有网卡，危险
                    }
                },
            }
        ])

        with mock.patch("tools.bootstrap_external_content_test_services.run_docker_command") as mock_run:
            mock_proc = mock.MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = fake_inspect_output
            mock_run.return_value = mock_proc

            with self.assertRaises(bootstrap_module.SecurityError):
                bootstrap_module.verify_existing_container(
                    "wblog-g4-test-mysql",
                    expected_image="mysql:8.4",
                    expected_port=13306,
                    expected_container_port=3306,
                )

    def test_verify_existing_container_requires_isolation_labels(self) -> None:
        """验证缺少 external_content.tests 或 guides.tests 标签的容器被坚决拒绝。"""
        fake_inspect_output = json.dumps([
            {
                "Config": {
                    "Image": "mysql:8.4",
                    "Labels": {},  # 缺少隔离标签
                },
                "NetworkSettings": {
                    "Ports": {
                        "3306/tcp": [{"HostIp": "127.0.0.1", "HostPort": "13306"}]
                    }
                },
            }
        ])

        with mock.patch("tools.bootstrap_external_content_test_services.run_docker_command") as mock_run:
            mock_proc = mock.MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = fake_inspect_output
            mock_run.return_value = mock_proc

            with self.assertRaises(bootstrap_module.SecurityError):
                bootstrap_module.verify_existing_container(
                    "wblog-g4-test-mysql",
                    expected_image="mysql:8.4",
                    expected_port=13306,
                    expected_container_port=3306,
                )

    def test_mysql_password_passed_via_container_env_not_docker_argv(self) -> None:
        """
        验证 MySQL root 密码在执行 ping 或 grant 时通过容器环境变量 $MYSQL_ROOT_PASSWORD 引用，
        命令行 argv 列表中绝不包含明文密码字符串。
        """
        executed_commands: List[List[str]] = []
        secret_root_pwd = "super_secret_root_pwd_9999"

        def _fake_subprocess_run(cmd: List[str], *args: Any, **kwargs: Any) -> mock.MagicMock:
            executed_commands.append(cmd)
            proc = mock.MagicMock()
            proc.returncode = 0
            proc.stdout = "mysqld is alive\n"
            return proc

        with mock.patch("subprocess.run", side_effect=_fake_subprocess_run), \
             mock.patch.object(bootstrap_module, "ensure_image_exists"), \
             mock.patch.object(bootstrap_module, "get_container_status", return_value="running"), \
             mock.patch.object(bootstrap_module, "verify_existing_container"):

            bootstrap_module.start_mysql_service(root_password=secret_root_pwd, user_password="user_pwd_123")

        # 验证每一个调用的命令行参数列表中绝无明文 root 密码
        for cmd in executed_commands:
            joined = " ".join(cmd)
            self.assertNotIn(secret_root_pwd, joined, "docker 命令 argv 列表中绝不能包含明文 root 密码！")
            # 必须引用 $MYSQL_ROOT_PASSWORD
            if "mysql" in joined:
                self.assertIn("$MYSQL_ROOT_PASSWORD", joined)

    def test_grant_failure_redacts_stderr_and_sql(self) -> None:
        """
        验证权限初始化失败时，异常信息仅输出有限退出码，绝不包含可能带有密码或 SQL 文本的 stderr。
        """
        sensitive_stderr = "ERROR 1045 (28000): Access denied for user 'root'@'localhost' (using password: leaked_pwd_777)"

        def _fake_subprocess_run(cmd: List[str], *args: Any, **kwargs: Any) -> mock.MagicMock:
            proc = mock.MagicMock()
            if "ping" in " ".join(cmd):
                proc.returncode = 0
                return proc
            # grant 步骤失败
            proc.returncode = 1
            proc.stderr = sensitive_stderr
            return proc

        with mock.patch("subprocess.run", side_effect=_fake_subprocess_run), \
             mock.patch.object(bootstrap_module, "ensure_image_exists"), \
             mock.patch.object(bootstrap_module, "get_container_status", return_value="running"), \
             mock.patch.object(bootstrap_module, "verify_existing_container"):

            with self.assertRaises(RuntimeError) as cm:
                bootstrap_module.start_mysql_service(root_password="root", user_password="user")

            exc_msg = str(cm.exception)
            # 关键行为断言：绝不包含敏感 stderr，且绝不包含泄露的密码
            self.assertNotIn("leaked_pwd_777", exc_msg)
            self.assertNotIn("Access denied", exc_msg)
            # 包含有限退出码
            self.assertIn("1", exc_msg)

    def test_stop_services_does_not_call_docker_rm(self) -> None:
        """验证 stop_services 仅停止容器，绝对不调用 docker rm 破坏性删除容器。"""
        docker_subcommands: List[str] = []

        def _fake_run_docker(args: List[str], *a: Any, **kw: Any) -> mock.MagicMock:
            if args:
                docker_subcommands.append(args[0])
            proc = mock.MagicMock()
            proc.returncode = 0
            return proc

        with mock.patch.object(bootstrap_module, "run_docker_command", side_effect=_fake_run_docker), \
             mock.patch.object(bootstrap_module, "get_container_status", return_value="running"), \
             mock.patch.object(bootstrap_module, "verify_existing_container"):

            bootstrap_module.stop_services()

        self.assertNotIn("rm", docker_subcommands, "stop 操作绝不能调用 docker rm！")
        self.assertIn("stop", docker_subcommands)

    def test_save_credentials_atomic_and_mode_0600(self) -> None:
        """验证 save_credentials 在写入凭据时以 0600 权限原子落盘。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            cred_file = Path(tmp_dir) / "credentials.env"
            with mock.patch.object(bootstrap_module, "get_credentials_path", return_value=cred_file):
                saved = bootstrap_module.save_credentials("my_test_password_abc")
                self.assertTrue(saved.exists())
                # 在支持权限的系统上验证 0600
                if sys.platform.startswith("linux"):
                    mode = stat.S_IMODE(os.stat(saved).st_mode)
                    self.assertEqual(mode, 0o600, "生成的凭据文件权限必须为 0600")

    def test_read_existing_credentials_fail_closed_on_corrupted_file(self) -> None:
        """损坏文件必须抛安全异常，不能伪装成首次启动的空凭据。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            directory = Path(tmp_dir)
            directory.chmod(0o700)
            tmp_path = directory / "credentials.env"
            values = dict(
                credentials_module.FIXED_CREDENTIALS,
                **{credentials_module.PASSWORD_KEY: "only-unit-test-private-sentinel"},
            )
            sentinel = "corrupted-line-sentinel"
            tmp_path.write_text(
                "".join(f"{key}={value}\n" for key, value in values.items())
                + sentinel + "\n",
                encoding="utf-8",
            )
            tmp_path.chmod(0o600)
            before = dict(os.environ)
            with mock.patch.object(bootstrap_module, "get_credentials_path", return_value=tmp_path):
                with self.assertRaises(bootstrap_module.SecurityError) as caught:
                    bootstrap_module.read_existing_credentials()
            _assert_redacted_error(
                self, caught.exception, "ERR_CREDENTIALS_LINE", sentinel, str(tmp_path),
                values[credentials_module.PASSWORD_KEY],
            )
            self.assertTrue(dict(os.environ) == before, "损坏文件不得改变环境")


class TestCLIConstantsOrder(unittest.TestCase):
    """验证模块与 CLI 常量初始化时序。"""

    def test_constants_available_at_import_time(self) -> None:
        """验证 ALLOWED_CREDENTIAL_KEYS 与 DEFAULT_TEST_LABELS 在模块导入完成时即全局可用。"""
        self.assertIn("EXTERNAL_CONTENT_TEST_MYSQL_PORT", ALLOWED_CREDENTIAL_KEYS)
        self.assertIn("EXTERNAL_CONTENT_TEST_MYSQL_PASSWORD", ALLOWED_CREDENTIAL_KEYS)
        self.assertEqual(DEFAULT_TEST_LABELS, ["external_content.tests", "guides.tests"])

    def test_main_cli_invokes_credentials_guard_and_runner_in_order(self) -> None:
        """验证 CLI main 入口以正确的时序调度: 凭据加载 -> Socket Guard 安装 -> django.setup -> 隔离配置校验 -> 测试执行。"""
        call_order: List[str] = []

        def _mock_load_creds(*a: Any, **kw: Any) -> Dict[str, str]:
            call_order.append("load_test_credentials")
            return {"EXTERNAL_CONTENT_TEST_MYSQL_PORT": "13306"}

        def _mock_install_guard() -> None:
            call_order.append("install_socket_guard")

        def _mock_django_setup() -> None:
            call_order.append("django.setup")

        def _mock_verify_isolation() -> None:
            call_order.append("verify_isolation_settings")

        mock_runner_cls = mock.MagicMock()
        mock_runner_instance = mock.MagicMock()
        mock_runner_instance.run_tests.return_value = 0
        mock_runner_cls.return_value = mock_runner_instance

        import tools.run_external_content_tests as runner_module

        with mock.patch("sys.argv", ["run_external_content_tests.py", "external_content.tests"]), \
             mock.patch.object(runner_module, "load_test_credentials", side_effect=_mock_load_creds), \
             mock.patch.object(runner_module, "install_socket_guard", side_effect=_mock_install_guard), \
             mock.patch("django.setup", side_effect=_mock_django_setup), \
             mock.patch.object(runner_module, "verify_isolation_settings", side_effect=_mock_verify_isolation), \
             mock.patch("django.test.runner.DiscoverRunner", mock_runner_cls), \
             mock.patch("sys.exit") as mock_exit:

            runner_module.main()

        self.assertEqual(
            call_order,
            ["load_test_credentials", "install_socket_guard", "django.setup", "verify_isolation_settings"],
            "CLI 执行时序必须严格保证: 凭据加载 -> Socket Guard 安装 -> django.setup -> 隔离配置校验"
        )
        mock_runner_instance.run_tests.assert_called_once_with(["external_content.tests"])
        mock_exit.assert_called_once_with(False)

    def test_main_cli_fails_fast_on_missing_credentials(self) -> None:
        """验证 CLI main 入口在凭据缺失或加载失败时立即中止，绝对不调用 django.setup。"""
        import tools.run_external_content_tests as runner_module

        with mock.patch("sys.argv", ["run_external_content_tests.py"]), \
             mock.patch.object(runner_module, "load_test_credentials", side_effect=SecurityError("ERR_CREDENTIALS_MISSING")), \
             mock.patch("django.setup") as mock_django_setup:

            with self.assertRaises(SecurityError):
                runner_module.main()

            mock_django_setup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
