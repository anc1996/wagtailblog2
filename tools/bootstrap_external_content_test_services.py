# -*- coding: utf-8 -*-
"""
Wagtailblog3 正文外置协议专用 Docker 测试环境服务引导管理工具。

功能说明：
1. 管理独立本地测试容器：
   - MySQL 8.4: 容器名 `wblog-g4-test-mysql`，绑定 `127.0.0.1:13306`
   - Mongo 7.0: 容器名 `wblog-g4-test-mongo`，绑定 `127.0.0.1:13307`
2. 隔离与安全约束：
   - 严格白名单机制，绝不访问或操作非测试命名空间的外部容器；
   - 绝不与已有生产或宿主端口 (3306/27017) 冲突；
   - 绝不删除或清理旧容器（仅做 start / stop / readiness 检查，不执行 rm）；
   - 随机密码通过临时文件注入 Docker，并在创建失败或完成时在 finally 中清理；
   - 日志与命令行中严禁输出凭据，inspect 与 grant 异常脱敏不泄露密码与 stderr；
   - 单次探活与授权调用设置显式超时，防止进程永久阻塞；
   - 测试凭据通过统一安全契约落盘至私有安全目录，只有服务全部就绪后才保存；
   - 供主控在 WSL 测试环境中安全审查与启动。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Dict, List, Optional, Tuple

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.external_content_test_credentials import (
    FIXED_CREDENTIALS,
    PASSWORD_KEY,
    SecurityError,
    get_credentials_path,
    read_private_credentials,
    write_private_credentials,
)

# 容器固定标识与网络拓扑
MYSQL_CONTAINER_NAME: str = "wblog-g4-test-mysql"
MYSQL_IMAGE: str = "mysql:8.4"
MYSQL_HOST: str = "127.0.0.1"
MYSQL_PORT: int = 13306
MYSQL_DB_NAME: str = "g4_external_content"
MYSQL_TEST_DB_NAME: str = "test_g4_external_content"
MYSQL_USER: str = "g4_test_user"

MONGO_CONTAINER_NAME: str = "wblog-g4-test-mongo"
MONGO_IMAGE: str = "mongo:7"
MONGO_HOST: str = "127.0.0.1"
MONGO_PORT: int = 13307
MONGO_DB_NAME: str = "g4_external_content_tests"

# 容器操作白名单（严格限制只允许操作本隔离体系容器）
ALLOWED_CONTAINER_NAMES: Tuple[str, ...] = (MYSQL_CONTAINER_NAME, MONGO_CONTAINER_NAME)

# 测试隔离标准标签常量
ISOLATION_LABEL_KEY: str = "wagtailblog.test.isolation"
ISOLATION_LABEL_VAL: str = "g4"
LABEL_EXTERNAL_CONTENT: str = "external_content.tests"
LABEL_GUIDES: str = "guides.tests"

CREDENTIALS_DIR_REL: str = os.path.join("output", "external-content-g4")
CREDENTIALS_FILE_NAME: str = "credentials.env"


# ==============================================================================
# 专用异常层次体系（具备有限错误码脱敏设计）
# ==============================================================================

class BootstrapError(RuntimeError):
    """测试环境引导基础异常类，包含业务错误码。"""

    def __init__(self, message: str, code: str = "ERR_BOOTSTRAP") -> None:
        super().__init__(message)
        self.code: str = code


class ContainerInspectError(BootstrapError):
    """容器状态或元数据 inspect 查询异常，不泄露敏感 stderr。"""

    def __init__(self, message: str, code: str = "ERR_CONTAINER_INSPECT") -> None:
        super().__init__(message, code=code)


class ContainerStartError(BootstrapError):
    """容器启动或运行失败异常。"""

    def __init__(self, message: str, code: str = "ERR_CONTAINER_START") -> None:
        super().__init__(message, code=code)


class ServiceReadinessError(BootstrapError):
    """服务就绪探活超时或探测失败异常。"""

    def __init__(self, message: str, code: str = "ERR_SERVICE_READINESS") -> None:
        super().__init__(message, code=code)


class ServiceGrantError(BootstrapError):
    """服务初始化与权限授予失败异常，不泄露 SQL、密码或原始错误信息。"""

    def __init__(self, message: str, code: str = "ERR_SERVICE_GRANT") -> None:
        super().__init__(message, code=code)


# ==============================================================================
# 凭据读取与保存封装
# ==============================================================================

def get_project_root() -> Path:
    """获取项目根目录路径。"""
    return Path(__file__).resolve().parent.parent


def read_existing_credentials() -> Dict[str, str]:
    """仅首次缺失允许生成；任何损坏或不安全文件均失败关闭。"""
    try:
        return read_private_credentials(get_credentials_path())
    except SecurityError as security_exception:
        if security_exception.code == "ERR_CREDENTIALS_MISSING":
            return {}
        raise


def save_credentials(user_password: str) -> Path:
    """复用统一凭据协议原子落盘，不修改既有父目录权限。"""
    credentials_target_path = get_credentials_path()
    write_private_credentials(
        credentials_target_path,
        dict(FIXED_CREDENTIALS, **{PASSWORD_KEY: user_password}),
    )
    return credentials_target_path


# ==============================================================================
# Docker CLI 交互与状态检查
# ==============================================================================

def run_docker_command(
    command_args: List[str],
    check: bool = True,
    timeout: int = 60,
) -> subprocess.CompletedProcess:
    """执行 Docker CLI 命令并捕获输出，杜绝敏感凭据回显。"""
    if shutil.which("docker") is None:
        raise RuntimeError("未检测到 docker 命令行工具，请确保 Docker 已安装并在系统 PATH 中。")
    return subprocess.run(
        ["docker"] + command_args,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def get_container_status(container_name: str) -> Optional[str]:
    """
    查询指定容器的运行状态。

    返回: 'running', 'exited', 'created' 等；若容器明确不存在返回 None。
    当 inspect 失败时抛出分类异常，绝不把 inspect 错误误判为容器不存在。
    """
    if container_name not in ALLOWED_CONTAINER_NAMES:
        raise SecurityError("ERR_CONTAINER_TARGET")

    try:
        process_result = run_docker_command(
            ["inspect", "--format", "{{.State.Status}}", container_name],
            check=False,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        raise ContainerInspectError(
            f"检查容器 {container_name} 状态超时（超过 10 秒）",
            code="ERR_DOCKER_TIMEOUT",
        ) from None
    except (subprocess.SubprocessError, OSError, RuntimeError) as execution_error:
        raise ContainerInspectError(
            f"执行容器状态检查命令失败: {type(execution_error).__name__}",
            code="ERR_CONTAINER_INSPECT",
        ) from None

    if process_result.returncode == 0:
        return process_result.stdout.strip()

    error_output_lower = (process_result.stderr or "").lower()
    if (
        "is the docker daemon running" in error_output_lower
        or "cannot connect to the docker daemon" in error_output_lower
    ):
        raise ContainerInspectError(
            "Docker 守护进程未启动或无法连接",
            code="ERR_DOCKER_DAEMON",
        )

    if "no such container" in error_output_lower or "no such object" in error_output_lower:
        return None

    raise ContainerInspectError(
        f"检查容器 {container_name} 状态失败（退出码: {process_result.returncode}）",
        code="ERR_CONTAINER_INSPECT",
    )


def normalize_image_name(image_string: str) -> str:
    """标准化 Docker 镜像名称，移除常见 registry 前缀便于精确比对。"""
    clean_image_string = image_string.strip()
    registry_prefixes = ["docker.io/library/", "docker.io/", "library/"]
    for prefix_entry in registry_prefixes:
        if clean_image_string.startswith(prefix_entry):
            return clean_image_string[len(prefix_entry):]
    return clean_image_string


def verify_existing_container(
    container_name: str,
    expected_image: str,
    expected_port: int,
    expected_container_port: int,
) -> None:
    """
    复用或停止容器前严格 inspect 校验：
    1. 镜像名称与预期的隔离镜像一致；
    2. 包含隔离标签 (external_content.tests=true, guides.tests=true, wagtailblog.test.isolation=g4)；
    3. 端口绑定必须严格为 127.0.0.1 上的专属隔离端口 (13306/13307)，拒绝未授权绑定。
    """
    allowed_specs = (
        (MYSQL_CONTAINER_NAME, MYSQL_IMAGE, MYSQL_PORT, 3306),
        (MONGO_CONTAINER_NAME, MONGO_IMAGE, MONGO_PORT, 27017),
    )
    if (
        type(expected_port) is not int
        or type(expected_container_port) is not int
        or (container_name, expected_image, expected_port, expected_container_port)
        not in allowed_specs
    ):
        raise SecurityError("ERR_CONTAINER_TARGET")
    try:
        process_result = run_docker_command(["inspect", container_name], check=False)
    except (OSError, subprocess.SubprocessError, RuntimeError):
        raise SecurityError("ERR_CONTAINER_INSPECT") from None
    if process_result.returncode != 0:
        raise SecurityError("ERR_CONTAINER_INSPECT")

    try:
        details_list = json.loads(process_result.stdout)
        if type(details_list) is not list or len(details_list) != 1:
            raise SecurityError("ERR_CONTAINER_METADATA")
        container_info = details_list[0]
    except (json.JSONDecodeError, TypeError):
        raise SecurityError("ERR_CONTAINER_METADATA") from None

    if type(container_info) is not dict:
        raise SecurityError("ERR_CONTAINER_METADATA")
    config_section = container_info.get("Config")
    host_config_section = container_info.get("HostConfig")
    network_section = container_info.get("NetworkSettings")
    if any(type(section_item) is not dict for section_item in (config_section, host_config_section, network_section)):
        raise SecurityError("ERR_CONTAINER_METADATA")

    # 1. 校验镜像
    config_image = config_section.get("Image")
    if type(config_image) is not str or normalize_image_name(config_image) != expected_image:
        raise SecurityError("ERR_CONTAINER_IMAGE")

    # host/container网络可绕过端口围栏；特权容器亦不属于专用测试拓扑。
    if host_config_section.get("NetworkMode") not in ("default", "bridge"):
        raise SecurityError("ERR_CONTAINER_NETWORK")
    if host_config_section.get("Privileged") is not False:
        raise SecurityError("ERR_CONTAINER_PRIVILEGED")

    # 2. 校验隔离标签
    labels_dict = config_section.get("Labels")
    if type(labels_dict) is not dict:
        raise SecurityError("ERR_CONTAINER_LABELS")
    if (
        labels_dict.get(LABEL_EXTERNAL_CONTENT) != "true"
        or labels_dict.get(LABEL_GUIDES) != "true"
        or labels_dict.get(ISOLATION_LABEL_KEY) != ISOLATION_LABEL_VAL
    ):
        raise SecurityError("ERR_CONTAINER_LABELS")

    # 3. 校验端口绑定为 127.0.0.1 专用端口
    port_bindings = network_section.get("Ports")
    if type(port_bindings) is not dict:
        raise SecurityError("ERR_CONTAINER_PORTS")
    target_key = f"{expected_container_port}/tcp"
    bindings_list = port_bindings.get(target_key)
    if type(bindings_list) is not list or len(bindings_list) != 1:
        raise SecurityError("ERR_CONTAINER_PORTS")
    # 官方镜像可声明未映射端口；任何额外实际绑定都必须拒绝。
    if any(value is not None for key, value in port_bindings.items() if key != target_key):
        raise SecurityError("ERR_CONTAINER_PORTS")

    for binding in bindings_list:
        if (
            type(binding) is not dict
            or binding.get("HostIp") != "127.0.0.1"
            or binding.get("HostPort") != str(expected_port)
        ):
            raise SecurityError("ERR_CONTAINER_PORTS")


def ensure_image_exists(image_name: str) -> None:
    """确认本地已有镜像；若没有则尝试拉取。"""
    check_process = run_docker_command(["image", "inspect", image_name], check=False)
    if check_process.returncode != 0:
        print(f"[Docker] 本地缺少镜像 {image_name}，正在拉取...")
        run_docker_command(["pull", image_name], check=True, timeout=600)
        print(f"[Docker] 镜像 {image_name} 拉取成功。")


# ==============================================================================
# 服务引导与就绪检查实现
# ==============================================================================

def start_mysql_service(root_password: str, user_password: str) -> None:
    """启动或恢复本地 MySQL 8.4 测试容器，并执行鉴权配置。"""
    ensure_image_exists(MYSQL_IMAGE)
    container_status = get_container_status(MYSQL_CONTAINER_NAME)

    if container_status is not None:
        # 无论 running 或 exited，在操作前必须通过 verify_existing_container 审查元数据
        verify_existing_container(
            MYSQL_CONTAINER_NAME,
            expected_image=MYSQL_IMAGE,
            expected_port=MYSQL_PORT,
            expected_container_port=3306,
        )

    if container_status == "running":
        print(f"[MySQL] 容器 {MYSQL_CONTAINER_NAME} 已在运行中。")
    elif container_status is not None:
        print(f"[MySQL] 容器 {MYSQL_CONTAINER_NAME} 处于 {container_status} 状态，正在启动...")
        try:
            run_docker_command(["start", MYSQL_CONTAINER_NAME], check=True)
        except Exception as start_error:
            if isinstance(start_error, ContainerStartError):
                raise
            raise ContainerStartError(
                f"启动 MySQL 容器失败: {type(start_error).__name__}",
                code="ERR_CONTAINER_START",
            ) from None
    else:
        print(f"[MySQL] 创建并启动新容器 {MYSQL_CONTAINER_NAME} ({MYSQL_HOST}:{MYSQL_PORT})...")
        # 使用临时文件传递密码，严防通过命令行参数或进程列表泄露
        file_descriptor, tmp_env_path = tempfile.mkstemp(prefix="g4_mysql_", suffix=".env")
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as env_file:
                env_file.write(f"MYSQL_ROOT_PASSWORD={root_password}\n")
                env_file.write(f"MYSQL_DATABASE={MYSQL_DB_NAME}\n")
                env_file.write(f"MYSQL_USER={MYSQL_USER}\n")
                env_file.write(f"MYSQL_PASSWORD={user_password}\n")
            try:
                run_docker_command(
                    [
                        "run",
                        "-d",
                        "--name",
                        MYSQL_CONTAINER_NAME,
                        "--label",
                        f"{LABEL_EXTERNAL_CONTENT}=true",
                        "--label",
                        f"{LABEL_GUIDES}=true",
                        "--label",
                        f"{ISOLATION_LABEL_KEY}={ISOLATION_LABEL_VAL}",
                        "-p",
                        f"{MYSQL_HOST}:{MYSQL_PORT}:3306",
                        "--env-file",
                        tmp_env_path,
                        MYSQL_IMAGE,
                    ],
                    check=True,
                )
            except Exception as run_error:
                if isinstance(run_error, ContainerStartError):
                    raise
                raise ContainerStartError(
                    f"创建并启动 MySQL 容器失败: {type(run_error).__name__}",
                    code="ERR_CONTAINER_START",
                ) from None
        finally:
            # 无论创建成功与否，在 finally 中立即删除临时 env 文件，不残留明文
            if os.path.exists(tmp_env_path):
                os.remove(tmp_env_path)

    # 轮询就绪状态：严禁在 docker exec argv 中包含密码，改用容器内部环境变量 $MYSQL_ROOT_PASSWORD
    print("[MySQL] 正在等待 MySQL 就绪...")
    is_ready = False
    for attempt_index in range(45):
        try:
            ping_process = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    MYSQL_CONTAINER_NAME,
                    "sh",
                    "-c",
                    'exec mysqladmin ping -h 127.0.0.1 -u root -p"$MYSQL_ROOT_PASSWORD" --silent',
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if ping_process.returncode == 0:
                is_ready = True
                break
        except subprocess.TimeoutExpired:
            pass
        time.sleep(1)

    if not is_ready:
        raise ServiceReadinessError(
            "[MySQL] 启动超时或就绪检查失败，请检查 Docker 日志。",
            code="ERR_SERVICE_READINESS",
        )

    print("[MySQL] 就绪检查通过，正在初始化测试库与权限...")
    # 安全授权语句（通过标准输入注入，命令行中无任何明文）
    init_sql = (
        f"CREATE DATABASE IF NOT EXISTS `{MYSQL_DB_NAME}` CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;\n"
        f"CREATE DATABASE IF NOT EXISTS `{MYSQL_TEST_DB_NAME}` CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;\n"
        f"CREATE USER IF NOT EXISTS '{MYSQL_USER}'@'%' IDENTIFIED BY '{user_password}';\n"
        f"ALTER USER '{MYSQL_USER}'@'%' IDENTIFIED BY '{user_password}';\n"
        f"GRANT ALL PRIVILEGES ON `{MYSQL_DB_NAME}`.* TO '{MYSQL_USER}'@'%';\n"
        f"GRANT ALL PRIVILEGES ON `{MYSQL_TEST_DB_NAME}`.* TO '{MYSQL_USER}'@'%';\n"
        "FLUSH PRIVILEGES;\n"
    )
    try:
        grant_process = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                MYSQL_CONTAINER_NAME,
                "sh",
                "-c",
                'exec mysql -u root -p"$MYSQL_ROOT_PASSWORD"',
            ],
            input=init_sql,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        raise ServiceGrantError(
            "[MySQL] 初始化数据库与权限执行超时（超过 15 秒）",
            code="ERR_SERVICE_GRANT",
        ) from None
    except Exception as grant_execution_error:
        if isinstance(grant_execution_error, ServiceGrantError):
            raise
        raise ServiceGrantError(
            f"[MySQL] 执行权限初始化失败: {type(grant_execution_error).__name__}",
            code="ERR_SERVICE_GRANT",
        ) from None

    if grant_process.returncode != 0:
        # 权限初始化失败异常仅输出有限退出码，严禁输出可能包含密码的 stderr 或 SQL
        raise ServiceGrantError(
            f"[MySQL] 初始化数据库与权限失败 (退出码: {grant_process.returncode})",
            code="ERR_SERVICE_GRANT",
        )
    print(f"[MySQL] 测试库 ({MYSQL_DB_NAME}, {MYSQL_TEST_DB_NAME}) 与账户权限配置完成。")


def start_mongo_service() -> None:
    """启动或恢复本地 Mongo 7.0 测试容器。"""
    ensure_image_exists(MONGO_IMAGE)
    container_status = get_container_status(MONGO_CONTAINER_NAME)

    if container_status is not None:
        verify_existing_container(
            MONGO_CONTAINER_NAME,
            expected_image=MONGO_IMAGE,
            expected_port=MONGO_PORT,
            expected_container_port=27017,
        )

    if container_status == "running":
        print(f"[Mongo] 容器 {MONGO_CONTAINER_NAME} 已在运行中。")
    elif container_status is not None:
        print(f"[Mongo] 容器 {MONGO_CONTAINER_NAME} 处于 {container_status} 状态，正在启动...")
        try:
            run_docker_command(["start", MONGO_CONTAINER_NAME], check=True)
        except Exception as start_error:
            if isinstance(start_error, ContainerStartError):
                raise
            raise ContainerStartError(
                f"启动 Mongo 容器失败: {type(start_error).__name__}",
                code="ERR_CONTAINER_START",
            ) from None
    else:
        print(f"[Mongo] 创建并启动新容器 {MONGO_CONTAINER_NAME} ({MONGO_HOST}:{MONGO_PORT})...")
        try:
            run_docker_command(
                [
                    "run",
                    "-d",
                    "--name",
                    MONGO_CONTAINER_NAME,
                    "--label",
                    f"{LABEL_EXTERNAL_CONTENT}=true",
                    "--label",
                    f"{LABEL_GUIDES}=true",
                    "--label",
                    f"{ISOLATION_LABEL_KEY}={ISOLATION_LABEL_VAL}",
                    "-p",
                    f"{MONGO_HOST}:{MONGO_PORT}:27017",
                    MONGO_IMAGE,
                ],
                check=True,
            )
        except Exception as run_error:
            if isinstance(run_error, ContainerStartError):
                raise
            raise ContainerStartError(
                f"创建并启动 Mongo 容器失败: {type(run_error).__name__}",
                code="ERR_CONTAINER_START",
            ) from None

    print("[Mongo] 正在等待 MongoDB 就绪...")
    is_ready = False
    for attempt_index in range(30):
        try:
            ping_process = subprocess.run(
                [
                    "docker",
                    "exec",
                    MONGO_CONTAINER_NAME,
                    "mongosh",
                    "--quiet",
                    "--eval",
                    "db.runCommand({ ping: 1 }).ok",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if ping_process.returncode == 0 and "1" in ping_process.stdout:
                is_ready = True
                break
        except subprocess.TimeoutExpired:
            pass
        time.sleep(1)

    if not is_ready:
        raise ServiceReadinessError(
            "[Mongo] 启动超时或就绪检查失败，请检查 Docker 日志。",
            code="ERR_SERVICE_READINESS",
        )
    print(f"[Mongo] 就绪检查通过，监听端口 {MONGO_PORT}。")


def start_services() -> Path:
    """
    按顺序引导 MySQL 与 Mongo 测试容器，并在两项服务完全就绪后安全持久化凭据。

    1. 读取既有凭据，若已存在密码则优先复用，否则生成安全随机密码；
    2. 启动并配置 MySQL 测试服务（密码在失败时不泄露）；
    3. 启动并探活 Mongo 测试服务；
    4. 只有所有服务完全成功启动且探活通过后，才将凭据安全落盘，返回凭据文件路径；
    5. 若任一环节失败，不写入凭据文件，保持可恢复一致性。
    """
    existing_credentials = read_existing_credentials()
    user_password = (
        existing_credentials.get(PASSWORD_KEY)
        or secrets.token_urlsafe(24)
    )
    root_password = secrets.token_urlsafe(24)

    start_mysql_service(root_password=root_password, user_password=user_password)
    start_mongo_service()

    saved_credentials_path = save_credentials(user_password=user_password)
    return saved_credentials_path


def stop_services() -> None:
    """优雅停止测试容器（严格保留容器与卷，绝不执行删除操作）。"""
    for container_name in ALLOWED_CONTAINER_NAMES:
        container_status = get_container_status(container_name)
        if container_status is not None:
            # 停止前先行校验容器元数据与标签，防止误停外部容器
            expected_image = MYSQL_IMAGE if container_name == MYSQL_CONTAINER_NAME else MONGO_IMAGE
            expected_port = MYSQL_PORT if container_name == MYSQL_CONTAINER_NAME else MONGO_PORT
            expected_container_port = 3306 if container_name == MYSQL_CONTAINER_NAME else 27017
            verify_existing_container(
                container_name,
                expected_image,
                expected_port,
                expected_container_port,
            )

        if container_status == "running":
            print(f"[Docker] 正在停止容器 {container_name}...")
            run_docker_command(["stop", container_name], check=True)
            print(f"[Docker] 容器 {container_name} 已停止。")
        elif container_status is not None:
            print(f"[Docker] 容器 {container_name} 当前状态为 {container_status}，无需停止。")
        else:
            print(f"[Docker] 容器 {container_name} 不存在。")


def check_status() -> None:
    """核查当前测试容器状态与端口映射。"""
    print("=== Wagtailblog3 正文外置协议测试容器状态 ===")
    for service_name, target_port in (
        (MYSQL_CONTAINER_NAME, MYSQL_PORT),
        (MONGO_CONTAINER_NAME, MONGO_PORT),
    ):
        try:
            status_text = get_container_status(service_name)
            status_display = status_text if status_text else "不存在 (Not Found)"
        except BootstrapError as inspect_error:
            status_display = f"检查异常 ({inspect_error.code})"
        except SecurityError as security_error:
            status_display = f"安全告警 ({security_error.code})"
        print(f"服务: {service_name:25} 目标端口: {target_port}  状态: {status_display}")

    credentials_file_path = get_credentials_path()
    if credentials_file_path.is_file():
        print(f"凭据文件: 已就绪 ({credentials_file_path})")
    else:
        print(f"凭据文件: 尚未生成 ({credentials_file_path})")


def main() -> None:
    """CLI 入口，支持 start / stop / status / check 指令。"""
    command_parser = argparse.ArgumentParser(description="正文外置协议本地 Docker 测试服务引导管理工具")
    command_parser.add_argument(
        "action",
        choices=["start", "stop", "status", "check"],
        help="执行动作: start (启动并就绪), stop (安全停止), status (状态查看), check (检查就绪)",
    )
    parsed_arguments = command_parser.parse_args()

    if parsed_arguments.action in ("status", "check"):
        check_status()
        return

    if parsed_arguments.action == "stop":
        stop_services()
        return

    if parsed_arguments.action == "start":
        print("[Bootstrap] 正在初始化正文外置协议测试服务隔离环境...")
        saved_credentials_path = start_services()
        print(f"[Bootstrap] 测试凭据已安全落盘至: {saved_credentials_path} (文件权限 0600)")
        print("[Bootstrap] 本地隔离服务已全量就绪，可供安全运行测试套件。")


if __name__ == "__main__":
    main()