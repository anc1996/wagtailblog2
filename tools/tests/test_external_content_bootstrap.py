"""用完整Docker输出替身验证隔离身份与脱敏，不执行任何容器命令。"""

from __future__ import annotations

from copy import deepcopy
import json
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from tools import bootstrap_external_content_test_services as bootstrap


class ContainerBoundaryTests(unittest.TestCase):
    """只允许精确标识的测试容器，坏输出不得进入复用或停止流程。"""

    def setUp(self) -> None:
        """构造合法inspect基线与完全替代的Docker调用入口。"""
        self.info = {
            "Config": {
                "Image": "mysql:8.4",
                "Labels": {
                    "external_content.tests": "true",
                    "guides.tests": "true",
                    "wagtailblog.test.isolation": "g4",
                },
            },
            "HostConfig": {"NetworkMode": "default", "Privileged": False},
            "NetworkSettings": {
                "Ports": {"3306/tcp": [{"HostIp": "127.0.0.1", "HostPort": "13306"}]}
            },
        }
        self.command_patch = patch.object(bootstrap, "run_docker_command")
        self.command = self.command_patch.start()
        self.addCleanup(self.command_patch.stop)
        self.set_response([self.info])

    def set_response(self, details: object, *, returncode: int = 0) -> None:
        """替身返回完整结构，原始输出永不来自真实Docker。"""
        self.command.return_value = subprocess.CompletedProcess(
            ["docker", "inspect"], returncode, json.dumps(details), "private-error-sentinel"
        )

    def verify(self) -> None:
        """沿真实容器核查入口调用，而非自行重复核查逻辑。"""
        bootstrap.verify_existing_container("wblog-g4-test-mysql", "mysql:8.4", 13306, 3306)

    def test_valid_container_is_accepted_with_exact_inspect_target(self) -> None:
        """合法对照确实通过，排除永远拒绝的假安全。"""
        self.verify()
        self.command.assert_called_once_with(["inspect", "wblog-g4-test-mysql"], check=False)

    def test_image_substring_does_not_authorize_container(self) -> None:
        """镜像含合法子串也不能冒充官方版本。"""
        self.info["Config"]["Image"] = "private-image-sentinel/mysql:8.4"
        self.set_response([self.info])
        with self.assertRaises(bootstrap.SecurityError) as caught:
            self.verify()
        self.assertNotIn("private-image-sentinel", str(caught.exception))

    def test_malformed_inspect_output_is_classified_without_raw_json(self) -> None:
        """JSON损坏不得回显inspect里的敏感内容。"""
        self.command.return_value = subprocess.CompletedProcess(
            [], 0, "private-json-sentinel", ""
        )
        with self.assertRaises(bootstrap.SecurityError) as caught:
            self.verify()
        self.assertNotIn("private-json-sentinel", str(caught.exception))

    def test_failed_inspect_does_not_echo_stderr(self) -> None:
        """Docker失败只返回有限码，不回显stderr。"""
        self.set_response([], returncode=1)
        with self.assertRaises(bootstrap.SecurityError) as caught:
            self.verify()
        self.assertNotIn("private-error-sentinel", str(caught.exception))

    def test_extra_externally_bound_port_is_rejected(self) -> None:
        """专用端口正确不能弥补另一个端口公开监听。"""
        self.info["NetworkSettings"]["Ports"]["9999/tcp"] = [
            {"HostIp": "0.0.0.0", "HostPort": "9999"}
        ]
        self.set_response([self.info])
        with self.assertRaises(bootstrap.SecurityError):
            self.verify()

    def test_host_network_mode_is_rejected(self) -> None:
        """不得借host网络绕过独立端口隔离。"""
        self.info["HostConfig"]["NetworkMode"] = "host"
        self.set_response([self.info])
        with self.assertRaises(bootstrap.SecurityError):
            self.verify()

    def test_each_required_label_is_individually_checked(self) -> None:
        """单变量移除每个标签均被拒绝，不借其他坏条件假绿。"""
        for label in ("external_content.tests", "guides.tests", "wagtailblog.test.isolation"):
            with self.subTest(label=label):
                modified = deepcopy(self.info)
                del modified["Config"]["Labels"][label]
                self.set_response([modified])
                with self.assertRaises(bootstrap.SecurityError):
                    self.verify()

    def test_wrong_container_spec_is_rejected_before_docker(self) -> None:
        """公开工具入口不能核查或停止任意其他容器。"""
        with self.assertRaises(bootstrap.SecurityError):
            bootstrap.verify_existing_container("private-container", "mysql:8.4", 13306, 3306)
        self.command.assert_not_called()

    def test_malformed_metadata_shape_is_rejected(self) -> None:
        """标量、数组、空结构都应有限分类而非AttributeError。"""
        for details in ([None], [{}], {"Config": {}}, [self.info, self.info]):
            with self.subTest(details_type=type(details).__name__):
                self.set_response(details)
                with self.assertRaises(bootstrap.SecurityError):
                    self.verify()


class ExceptionHierarchyTests(unittest.TestCase):
    """验证 Bootstrap 专用异常层次结构与错误码规范。"""

    def test_exception_classes_inherit_from_runtime_error_and_have_code(self) -> None:
        """所有专用异常均继承自 RuntimeError/BootstrapError 且具备 .code 属性。"""
        self.assertTrue(issubclass(bootstrap.BootstrapError, RuntimeError))
        self.assertTrue(issubclass(bootstrap.ContainerInspectError, bootstrap.BootstrapError))
        self.assertTrue(issubclass(bootstrap.ContainerStartError, bootstrap.BootstrapError))
        self.assertTrue(issubclass(bootstrap.ServiceReadinessError, bootstrap.BootstrapError))
        self.assertTrue(issubclass(bootstrap.ServiceGrantError, bootstrap.BootstrapError))

        for exc_cls, default_code in (
            (bootstrap.BootstrapError, "ERR_BOOTSTRAP"),
            (bootstrap.ContainerInspectError, "ERR_CONTAINER_INSPECT"),
            (bootstrap.ContainerStartError, "ERR_CONTAINER_START"),
            (bootstrap.ServiceReadinessError, "ERR_SERVICE_READINESS"),
            (bootstrap.ServiceGrantError, "ERR_SERVICE_GRANT"),
        ):
            instance = exc_cls("测试异常")
            self.assertEqual(instance.code, default_code)


class ContainerStatusTests(unittest.TestCase):
    """验证容器状态查询的白名单限制、异常分类及绝不将 inspect 异常误判为 None。"""

    def test_status_rejects_unauthorized_container_name(self) -> None:
        """非白名单容器名必须在执行 docker 命令前抛出 SecurityError(ERR_CONTAINER_TARGET)。"""
        with patch.object(bootstrap, "run_docker_command") as mock_cmd:
            with self.assertRaises(bootstrap.SecurityError) as caught:
                bootstrap.get_container_status("unauthorized_container_name")
            self.assertEqual(caught.exception.code, "ERR_CONTAINER_TARGET")
            mock_cmd.assert_not_called()

    def test_status_returns_running_when_inspect_succeeds(self) -> None:
        """inspect 成功退出且输出状态时，准确返回状态字符串。"""
        with patch.object(bootstrap, "run_docker_command") as mock_cmd:
            mock_cmd.return_value = subprocess.CompletedProcess(
                ["docker", "inspect"], 0, "running\n", ""
            )
            status = bootstrap.get_container_status("wblog-g4-test-mysql")
            self.assertEqual(status, "running")
            mock_cmd.assert_called_once_with(
                ["inspect", "--format", "{{.State.Status}}", "wblog-g4-test-mysql"],
                check=False,
                timeout=10,
            )

    def test_status_returns_none_only_when_no_such_container(self) -> None:
        """仅在 Docker CLI 明确报错 'No such container' 或 'no such object' 时返回 None。"""
        with patch.object(bootstrap, "run_docker_command") as mock_cmd:
            for error_msg in (
                "Error: No such container: wblog-g4-test-mysql",
                "Error: No such object: wblog-g4-test-mysql",
            ):
                with self.subTest(msg=error_msg):
                    mock_cmd.return_value = subprocess.CompletedProcess(
                        ["docker", "inspect"], 1, "", error_msg
                    )
                    status = bootstrap.get_container_status("wblog-g4-test-mysql")
                    self.assertIsNone(status)

    def test_status_raises_daemon_error_when_daemon_is_not_running(self) -> None:
        """当 Docker 守护进程未启动或无法连接时，抛出 ContainerInspectError(ERR_DOCKER_DAEMON)，绝不误判为 None。"""
        daemon_error = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?"
        with patch.object(bootstrap, "run_docker_command") as mock_cmd:
            mock_cmd.return_value = subprocess.CompletedProcess(
                ["docker", "inspect"], 1, "", daemon_error
            )
            with self.assertRaises(bootstrap.ContainerInspectError) as caught:
                bootstrap.get_container_status("wblog-g4-test-mysql")
            self.assertEqual(caught.exception.code, "ERR_DOCKER_DAEMON")

    def test_status_raises_inspect_error_without_echoing_sensitive_stderr(self) -> None:
        """其他 inspect 异常抛出 ContainerInspectError(ERR_CONTAINER_INSPECT)，且错误信息脱敏不回显 stderr。"""
        secret_stderr = "docker daemon internal error with private-host-token-xyz"
        with patch.object(bootstrap, "run_docker_command") as mock_cmd:
            mock_cmd.return_value = subprocess.CompletedProcess(
                ["docker", "inspect"], 2, "", secret_stderr
            )
            with self.assertRaises(bootstrap.ContainerInspectError) as caught:
                bootstrap.get_container_status("wblog-g4-test-mongo")
            self.assertEqual(caught.exception.code, "ERR_CONTAINER_INSPECT")
            self.assertNotIn("private-host-token-xyz", str(caught.exception))

    def test_status_raises_timeout_error_when_inspect_command_times_out(self) -> None:
        """inspect 超时时抛出 ContainerInspectError(ERR_DOCKER_TIMEOUT)，绝不吞掉超时误判为 None。"""
        with patch.object(bootstrap, "run_docker_command") as mock_cmd:
            mock_cmd.side_effect = subprocess.TimeoutExpired(
                cmd=["docker", "inspect"], timeout=10
            )
            with self.assertRaises(bootstrap.ContainerInspectError) as caught:
                bootstrap.get_container_status("wblog-g4-test-mysql")
            self.assertEqual(caught.exception.code, "ERR_DOCKER_TIMEOUT")


class ServiceReadinessAndGrantTests(unittest.TestCase):
    """验证服务就绪探活单次超时、数据库初始化脱敏与异常分类。"""

    @patch("time.sleep", return_value=None)
    @patch.object(bootstrap, "ensure_image_exists")
    @patch.object(bootstrap, "get_container_status", return_value="running")
    @patch.object(bootstrap, "verify_existing_container")
    def test_mysql_readiness_failure_raises_service_readiness_error_with_timeout(
        self,
        mock_verify: unittest.mock.MagicMock,
        mock_status: unittest.mock.MagicMock,
        mock_ensure: unittest.mock.MagicMock,
        mock_sleep: unittest.mock.MagicMock,
    ) -> None:
        """MySQL 就绪探活失败或超时抛出 ServiceReadinessError，单次探测有 timeout 保护。"""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], returncode=1, stdout="", stderr="")
            with self.assertRaises(bootstrap.ServiceReadinessError) as caught:
                bootstrap.start_mysql_service(root_password="root_password", user_password="user_password")
            self.assertEqual(caught.exception.code, "ERR_SERVICE_READINESS")
            for call_item in mock_run.call_args_list:
                self.assertIn("timeout", call_item.kwargs)
                self.assertEqual(call_item.kwargs["timeout"], 5)

    @patch.object(bootstrap, "ensure_image_exists")
    @patch.object(bootstrap, "get_container_status", return_value="running")
    @patch.object(bootstrap, "verify_existing_container")
    def test_mysql_grant_failure_raises_service_grant_error_without_leaking_credentials(
        self,
        mock_verify: unittest.mock.MagicMock,
        mock_status: unittest.mock.MagicMock,
        mock_ensure: unittest.mock.MagicMock,
    ) -> None:
        """MySQL 授权失败抛出 ServiceGrantError，绝不回显密码、SQL 与原始 stderr。"""
        private_password = "super-secret-grant-password-42"
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess([], returncode=0, stdout="alive", stderr=""),
                subprocess.CompletedProcess([], returncode=1, stdout="", stderr="ERROR 1064: syntax error private-leak"),
            ]
            with self.assertRaises(bootstrap.ServiceGrantError) as caught:
                bootstrap.start_mysql_service(root_password="root_pwd", user_password=private_password)
            self.assertEqual(caught.exception.code, "ERR_SERVICE_GRANT")
            self.assertNotIn(private_password, str(caught.exception))
            self.assertNotIn("CREATE DATABASE", str(caught.exception))
            self.assertNotIn("private-leak", str(caught.exception))

    @patch.object(bootstrap, "ensure_image_exists")
    @patch.object(bootstrap, "get_container_status", return_value="running")
    @patch.object(bootstrap, "verify_existing_container")
    def test_mysql_grant_timeout_raises_service_grant_error(
        self,
        mock_verify: unittest.mock.MagicMock,
        mock_status: unittest.mock.MagicMock,
        mock_ensure: unittest.mock.MagicMock,
    ) -> None:
        """MySQL 授权执行超时抛出 ServiceGrantError(ERR_SERVICE_GRANT)，单次 timeout 保护生效。"""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess([], returncode=0, stdout="alive", stderr=""),
                subprocess.TimeoutExpired(cmd=["docker", "exec"], timeout=15),
            ]
            with self.assertRaises(bootstrap.ServiceGrantError) as caught:
                bootstrap.start_mysql_service(root_password="root_pwd", user_password="user_pwd")
            self.assertEqual(caught.exception.code, "ERR_SERVICE_GRANT")

    @patch("time.sleep", return_value=None)
    @patch.object(bootstrap, "ensure_image_exists")
    @patch.object(bootstrap, "get_container_status", return_value="running")
    @patch.object(bootstrap, "verify_existing_container")
    def test_mongo_readiness_failure_raises_service_readiness_error_with_timeout(
        self,
        mock_verify: unittest.mock.MagicMock,
        mock_status: unittest.mock.MagicMock,
        mock_ensure: unittest.mock.MagicMock,
        mock_sleep: unittest.mock.MagicMock,
    ) -> None:
        """Mongo 就绪探活失败抛出 ServiceReadinessError，单次探测有 timeout 保护。"""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], returncode=1, stdout="", stderr="")
            with self.assertRaises(bootstrap.ServiceReadinessError) as caught:
                bootstrap.start_mongo_service()
            self.assertEqual(caught.exception.code, "ERR_SERVICE_READINESS")
            for call_item in mock_run.call_args_list:
                self.assertIn("timeout", call_item.kwargs)
                self.assertEqual(call_item.kwargs["timeout"], 5)


class ServiceBootstrapConsistencyTests(unittest.TestCase):
    """验证服务启动可恢复一致性与凭据写入原子契约。"""

    @patch.object(bootstrap, "save_credentials")
    @patch.object(bootstrap, "start_mongo_service")
    @patch.object(bootstrap, "start_mysql_service")
    @patch.object(bootstrap, "read_existing_credentials")
    def test_start_services_reuses_existing_password_when_available(
        self,
        mock_read: unittest.mock.MagicMock,
        mock_start_mysql: unittest.mock.MagicMock,
        mock_start_mongo: unittest.mock.MagicMock,
        mock_save: unittest.mock.MagicMock,
    ) -> None:
        """启动服务时优先复用既有凭据中的密码，保证服务配置一致。"""
        from pathlib import Path
        existing_pwd = "reused-existing-test-password-123456"
        mock_read.return_value = {"EXTERNAL_CONTENT_TEST_MYSQL_PASSWORD": existing_pwd}
        mock_save.return_value = Path("/tmp/mock/credentials.env")

        saved_path = bootstrap.start_services()

        self.assertEqual(saved_path, Path("/tmp/mock/credentials.env"))
        mock_start_mysql.assert_called_once()
        self.assertEqual(mock_start_mysql.call_args.kwargs["user_password"], existing_pwd)
        mock_start_mongo.assert_called_once()
        mock_save.assert_called_once_with(user_password=existing_pwd)

    @patch.object(bootstrap, "save_credentials")
    @patch.object(bootstrap, "start_mongo_service")
    @patch.object(bootstrap, "start_mysql_service")
    @patch.object(bootstrap, "read_existing_credentials")
    def test_start_services_does_not_save_credentials_if_mysql_fails(
        self,
        mock_read: unittest.mock.MagicMock,
        mock_start_mysql: unittest.mock.MagicMock,
        mock_start_mongo: unittest.mock.MagicMock,
        mock_save: unittest.mock.MagicMock,
    ) -> None:
        """若 MySQL 启动失败，严禁写入凭据文件，保持可恢复一致性。"""
        mock_read.return_value = {}
        mock_start_mysql.side_effect = bootstrap.ServiceReadinessError("mysql ready failed")

        with self.assertRaises(bootstrap.ServiceReadinessError):
            bootstrap.start_services()

        mock_start_mongo.assert_not_called()
        mock_save.assert_not_called()

    @patch.object(bootstrap, "save_credentials")
    @patch.object(bootstrap, "start_mongo_service")
    @patch.object(bootstrap, "start_mysql_service")
    @patch.object(bootstrap, "read_existing_credentials")
    def test_start_services_does_not_save_credentials_if_mongo_fails(
        self,
        mock_read: unittest.mock.MagicMock,
        mock_start_mysql: unittest.mock.MagicMock,
        mock_start_mongo: unittest.mock.MagicMock,
        mock_save: MagicMock,
    ) -> None:
        """若 Mongo 启动失败，严禁写入凭据文件，保持可恢复一致性。"""
        mock_read.return_value = {}
        mock_start_mysql.return_value = None
        mock_start_mongo.side_effect = bootstrap.ServiceReadinessError("mongo ready failed")

        with self.assertRaises(bootstrap.ServiceReadinessError):
            bootstrap.start_services()

        mock_save.assert_not_called()

    @patch.object(bootstrap, "ensure_image_exists")
    @patch.object(bootstrap, "get_container_status", return_value=None)
    def test_start_mysql_cleans_up_temp_env_file_on_run_failure(
        self,
        mock_status: unittest.mock.MagicMock,
        mock_ensure: unittest.mock.MagicMock,
    ) -> None:
        """创建容器时若 docker run 失败，必须在 finally 中清理临时 env 文件，不残留明文文件。"""
        import os
        created_temp_files: list[str] = []

        def fake_run(args: list[str], check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess:
            if "run" in args:
                env_file_index = args.index("--env-file") + 1
                env_path = args[env_file_index]
                created_temp_files.append(env_path)
                self.assertTrue(os.path.exists(env_path), "docker run 执行时临时 env 文件应存在")
                raise bootstrap.ContainerStartError("docker run failed", code="ERR_CONTAINER_START")
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch.object(bootstrap, "run_docker_command", side_effect=fake_run):
            with self.assertRaises(bootstrap.ContainerStartError):
                bootstrap.start_mysql_service(root_password="root_password", user_password="user_password")

        self.assertEqual(len(created_temp_files), 1)
        self.assertFalse(os.path.exists(created_temp_files[0]), "失败后临时 env 文件必须已被删除清理")


if __name__ == "__main__":
    unittest.main()
