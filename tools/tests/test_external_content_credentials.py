"""验证测试凭据真实文件边界；任何失败不得污染环境或泄露输入。"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import bootstrap_external_content_test_services as bootstrap


class PrivateCredentialTests(unittest.TestCase):
    """通过私有临时文件挑战凭据读取，绝不连接服务。"""

    def setUp(self) -> None:
        """建立完整合法基线，每个负例仅改变一个安全条件。"""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.directory.chmod(0o700)
        self.path = self.directory / "credentials.env"
        self.values: dict[str, str] = {
            "EXTERNAL_CONTENT_TEST_MYSQL_HOST": "127.0.0.1",
            "EXTERNAL_CONTENT_TEST_MYSQL_PORT": "13306",
            "EXTERNAL_CONTENT_TEST_MYSQL_NAME": "g4_external_content",
            "EXTERNAL_CONTENT_TEST_MYSQL_USER": "g4_test_user",
            "EXTERNAL_CONTENT_TEST_MYSQL_PASSWORD": "only-unit-test-private-sentinel",
            "EXTERNAL_CONTENT_TEST_MONGO_HOST": "127.0.0.1",
            "EXTERNAL_CONTENT_TEST_MONGO_PORT": "13307",
            "EXTERNAL_CONTENT_TEST_MONGO_NAME": "g4_external_content_tests",
        }
        self.write_values(self.values)
        self.path_patch = patch.object(
            bootstrap, "get_credentials_path", return_value=self.path
        )
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def write_values(self, values: dict[str, str]) -> None:
        """仅写测试私有夹具，不输出密码。"""
        self.path.write_text(
            "".join(f"{key}={value}\n" for key, value in values.items()),
            encoding="utf-8",
        )
        self.path.chmod(0o600)

    def assert_rejected(self) -> None:
        """校验确实失败关闭且异常不包含敏感输入。"""
        before = dict(os.environ)
        with self.assertRaises(bootstrap.SecurityError) as caught:
            bootstrap.read_existing_credentials()
        self.assertNotIn(self.values["EXTERNAL_CONTENT_TEST_MYSQL_PASSWORD"], str(caught.exception))
        self.assertNotIn(str(self.path), str(caught.exception))
        self.assertEqual(dict(os.environ), before)

    def test_valid_complete_credentials_are_read_without_environment_changes(self) -> None:
        """合法基线可读，防止一律拒绝造成假绿。"""
        before = dict(os.environ)
        self.assertEqual(bootstrap.read_existing_credentials(), self.values)
        self.assertEqual(dict(os.environ), before)

    def test_missing_file_is_allowed_only_for_bootstrap_creation(self) -> None:
        """首次引导无文件可返回空值，不把损坏文件当首次启动。"""
        self.path.unlink()
        self.assertEqual(bootstrap.read_existing_credentials(), {})

    def test_duplicate_key_is_rejected(self) -> None:
        """重复键不能后值覆盖前值。"""
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("EXTERNAL_CONTENT_TEST_MYSQL_PORT=13306\n")
        self.assert_rejected()

    def test_incomplete_credentials_are_rejected(self) -> None:
        """缺项不能回退弱密码或其他数据库。"""
        values = dict(self.values)
        del values["EXTERNAL_CONTENT_TEST_MYSQL_PASSWORD"]
        self.write_values(values)
        self.assert_rejected()

    def test_malformed_line_is_rejected(self) -> None:
        """损坏行不能静默忽略。"""
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("private-malformed-sentinel\n")
        self.assert_rejected()

    def test_unsafe_target_is_rejected(self) -> None:
        """凭据中的目标必须是精确隔离拓扑。"""
        values = dict(self.values, EXTERNAL_CONTENT_TEST_MYSQL_HOST="private-host-sentinel")
        self.write_values(values)
        self.assert_rejected()

    def test_world_readable_file_is_rejected(self) -> None:
        """即使内容合法，文件权限不安全也不得读取。"""
        self.path.chmod(0o644)
        self.assert_rejected()

    def test_non_private_parent_is_rejected(self) -> None:
        """文件0600不能弥补父目录可被他人修改。"""
        self.directory.chmod(0o755)
        self.assert_rejected()

    def test_symlink_file_is_rejected(self) -> None:
        """通过叶节点链接不能绕开权限校验。"""
        original = self.directory / "original"
        self.path.rename(original)
        self.path.symlink_to(original)
        self.assert_rejected()

    def test_symlink_parent_is_rejected(self) -> None:
        """父目录链接也必须拒绝，不能先resolve隐藏链接。"""
        actual = self.directory / "actual"
        actual.mkdir(mode=0o700)
        self.path.rename(actual / "credentials.env")
        linked = self.directory / "linked"
        linked.symlink_to(actual, target_is_directory=True)
        self.path = linked / "credentials.env"
        self.path_patch.stop()
        self.path_patch = patch.object(
            bootstrap, "get_credentials_path", return_value=self.path
        )
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.assert_rejected()

    def test_oversized_file_is_rejected(self) -> None:
        """读取必须有界，避免损坏凭据无限占用内存。"""
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("#" + "x" * 9000)
        self.assert_rejected()

    def test_atomic_replace_failure_preserves_old_credentials(self) -> None:
        """替换失败不能截断旧凭据，错误不得泄露路径和密码。"""
        original = self.path.read_bytes()
        with patch.object(bootstrap.os, "replace", side_effect=OSError("private-error-sentinel")):
            with self.assertRaises(bootstrap.SecurityError) as caught:
                bootstrap.save_credentials("replacement-private-sentinel")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertNotIn("private-error-sentinel", str(caught.exception))
        self.assertNotIn("replacement-private-sentinel", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
