"""测试隔离工具共用的凭据契约；通过目录描述符消除路径检查竞态。"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import secrets
import stat
import sys
from typing import Iterator


MAX_CREDENTIAL_BYTES = 8192
FIXED_CREDENTIALS: dict[str, str] = {
    "EXTERNAL_CONTENT_TEST_MYSQL_HOST": "127.0.0.1",
    "EXTERNAL_CONTENT_TEST_MYSQL_PORT": "13306",
    "EXTERNAL_CONTENT_TEST_MYSQL_NAME": "g4_external_content",
    "EXTERNAL_CONTENT_TEST_MYSQL_USER": "g4_test_user",
    "EXTERNAL_CONTENT_TEST_MONGO_HOST": "127.0.0.1",
    "EXTERNAL_CONTENT_TEST_MONGO_PORT": "13307",
    "EXTERNAL_CONTENT_TEST_MONGO_NAME": "g4_external_content_tests",
}
PASSWORD_KEY = "EXTERNAL_CONTENT_TEST_MYSQL_PASSWORD"
ALLOWED_CREDENTIAL_KEYS = frozenset((*FIXED_CREDENTIALS, PASSWORD_KEY))


class SecurityError(PermissionError):
    """仅暴露有限错误码，绝不把文件内容、路径或系统异常带入日志。"""

    def __init__(self, code: str) -> None:
        """保留安全错误码供调用方区分缺失与损坏，不传递原始输入。"""
        self.code = code
        super().__init__(f"[{code}] 测试凭据安全校验未通过")


def get_credentials_path() -> Path:
    """返回未解析链接的Linux原生凭据路径，避免resolve隐藏父链接。"""
    custom = os.environ.get("EXTERNAL_CONTENT_TEST_CREDENTIALS_PATH")
    if custom:
        return Path(custom)
    return Path.home() / ".wagtailblog" / "external-content-g4" / "credentials.env"


def validate_credentials(values: dict[str, str]) -> dict[str, str]:
    """完整校验固定隔离拓扑与可安全用于env/SQL的测试密码。"""
    if type(values) is not dict or set(values) != ALLOWED_CREDENTIAL_KEYS:
        raise SecurityError("ERR_CREDENTIALS_KEYS")
    if any(type(value) is not str for value in values.values()):
        raise SecurityError("ERR_CREDENTIALS_VALUE")
    if any(values[key] != expected for key, expected in FIXED_CREDENTIALS.items()):
        raise SecurityError("ERR_CREDENTIALS_TARGET")
    password = values[PASSWORD_KEY]
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", password):
        raise SecurityError("ERR_CREDENTIALS_PASSWORD")
    return dict(values)


def parse_credentials(content: str) -> dict[str, str]:
    """拒绝未知键、重复键和损坏行，全部校验成功后才返回值。"""
    values: dict[str, str] = {}
    for line in content.splitlines():
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SecurityError("ERR_CREDENTIALS_LINE")
        key, value = line.split("=", 1)
        if key not in ALLOWED_CREDENTIAL_KEYS or key in values:
            raise SecurityError("ERR_CREDENTIALS_KEYS")
        values[key] = value
    return validate_credentials(values)


def _validate_directory(info: os.stat_result, *, private: bool) -> None:
    """私有目录须0700；祖先仅允许可信所有者且不可被其他用户替换。"""
    owner = os.geteuid()
    mode = stat.S_IMODE(info.st_mode)
    if not stat.S_ISDIR(info.st_mode):
        raise SecurityError("ERR_CREDENTIALS_DIRECTORY")
    if private:
        if info.st_uid != owner or mode != 0o700:
            raise SecurityError("ERR_CREDENTIALS_DIRECTORY_PERMISSIONS")
    elif info.st_uid not in (0, owner) or (
        mode & 0o022 and not (info.st_uid == 0 and mode == 0o1777)
    ):
        raise SecurityError("ERR_CREDENTIALS_ANCESTOR")


@contextmanager
def _private_parent(path: Path, *, create: bool = False) -> Iterator[int]:
    """逐层no-follow并固定父目录句柄，拒绝DrvFS及任一层符号链接。"""
    if not sys.platform.startswith("linux") or not path.is_absolute():
        raise SecurityError("ERR_CREDENTIALS_PATH")
    parts = path.parts
    if ".." in parts or len(parts) < 3:
        raise SecurityError("ERR_CREDENTIALS_PATH")
    if parts[1] == "mnt":
        raise SecurityError("ERR_CREDENTIALS_DRVFS")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = None
    try:
        descriptor = os.open("/", flags)
        _validate_directory(os.fstat(descriptor), private=False)
        for index, component in enumerate(parts[1:-1], start=1):
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
            _validate_directory(
                os.fstat(descriptor), private=index == len(parts) - 2
            )
        yield descriptor
    except SecurityError:
        raise
    except FileNotFoundError:
        raise SecurityError("ERR_CREDENTIALS_MISSING") from None
    except (OSError, UnicodeError, ValueError):
        raise SecurityError("ERR_CREDENTIALS_IO") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validate_file(info: os.stat_result) -> None:
    """拒绝非普通文件、硬链接、非本人所有及非0600的凭据。"""
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise SecurityError("ERR_CREDENTIALS_FILE_PERMISSIONS")
    if info.st_size > MAX_CREDENTIAL_BYTES:
        raise SecurityError("ERR_CREDENTIALS_SIZE")


def read_private_credentials(path: Path) -> dict[str, str]:
    """非阻塞打开并先核验普通文件，避免FIFO挂起；有界读取并复核变化。"""
    with _private_parent(path) as parent:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=parent,
        )
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            _validate_file(before)
            data = handle.read(MAX_CREDENTIAL_BYTES + 1)
            after = os.fstat(handle.fileno())
            if (
                len(data) > MAX_CREDENTIAL_BYTES
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
            ):
                raise SecurityError("ERR_CREDENTIALS_CHANGED")
        try:
            return parse_credentials(data.decode("utf-8"))
        except UnicodeError:
            raise SecurityError("ERR_CREDENTIALS_ENCODING") from None


def write_private_credentials(path: Path, values: dict[str, str]) -> None:
    """私有目录内独占创建、fsync并原子替换；失败保留旧文件并脱敏。"""
    valid = validate_credentials(values)
    data = "".join(f"{key}={value}\n" for key, value in valid.items()).encode("utf-8")
    with _private_parent(path, create=True) as parent:
        try:
            old = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            old = None
        if old is not None:
            _validate_file(old)
        temporary = f".credentials-{secrets.token_hex(16)}"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent,
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                _validate_file(os.fstat(handle.fileno()))
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
