# -*- coding: utf-8 -*-
"""Codex 子代理配置与模型目录离线预检工具。

核心职责：
1. 离线只读检查 .codex/config.toml 中 default_subagent_model 与各个 registered agents 的合法性；
2. 校验 model_catalog_json 所引用的 JSON 模型目录，包括关键 schema 字段、必须包含的基础模型及 duplicate slug 拦截；
3. 校验各角色 model 与推理等级（model_reasoning_effort）是否在对应 supported_reasoning_levels 内；
4. 校验所有角色使用的 model_provider（如 custom）是否在仓库或全局 ~/.codex/config.toml 的 model_providers 中登记；
5. 拦截禁止使用的 gpt-6-astra；
6. 当开启 --runtime 时，通过调用 `codex debug models` 验证运行时实际加载情况，严格保障零凭据与敏感信息泄露。
"""

import argparse
import json
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# 必需包含的最小基础模型列表
REQUIRED_MINIMAL_MODELS: Tuple[str, ...] = (
    "gpt-6.1-sol",
    "gemini-3.8-flash",
    "grok-4.7",
    "claude-sonnet-5-5",
)

# 模型目录必需的关键 schema 字段
REQUIRED_SCHEMA_FIELDS: Tuple[str, ...] = (
    "support_verbosity",
    "supports_reasoning_summary_parameter",
    "supports_parallel_tool_calls",
    "supported_reasoning_levels",
)

# 严禁在默认或各角色中选用的高消耗/弃用模型
FORBIDDEN_MODELS: Set[str] = {
    "gpt-6-astra",
}


@dataclass
class CheckResult:
    """预检结果数据结构。

    Attributes:
        success: 是否全部通过预检（无致命错误）
        errors: 阻断性错误列表（安全文案）
        warnings: 警告信息列表
        summary: 汇总摘要信息
    """

    success: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    summary: str = ""


def _safe_str(path: Any) -> str:
    """返回适合打印的安全路径字符串（脱敏或标准化展示）。"""
    return str(path) if path is not None else ""


def _extract_reasoning_levels(raw_levels: Any) -> Set[str]:
    """仅从原生对象列表提取推理等级，避免非原生结构被误报为合法。"""
    levels: Set[str] = set()
    if isinstance(raw_levels, list):
        for item in raw_levels:
            if isinstance(item, dict):
                effort = item.get("effort")
                if isinstance(effort, str) and effort.strip():
                    levels.add(effort)
    return levels


def _load_toml_file(file_path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """安全读取并解析 TOML 配置文件。

    Args:
        file_path: TOML 文件绝对或相对路径

    Returns:
        (解析出的字典对象, 错误信息字符串)
    """
    if not file_path.exists():
        return None, f"文件不存在: {_safe_str(file_path)}"
    try:
        content = file_path.read_text(encoding="utf-8-sig")
        data = tomllib.loads(content)
        return data, None
    except Exception as exc:
        return None, f"解析 TOML 失败 ({file_path.name}): {type(exc).__name__}"


def _load_catalog_json(file_path: Path) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """安全读取并解析模型目录 JSON 文件。

    Args:
        file_path: JSON 文件路径

    Returns:
        (模型列表, 错误信息字符串)
    """
    if not file_path.exists():
        return None, f"模型目录 JSON 文件不存在: {_safe_str(file_path)}"
    try:
        content = file_path.read_text(encoding="utf-8-sig")
        data = json.loads(content)
        if isinstance(data, dict):
            models = data.get("models")
            if isinstance(models, list):
                return models, None
            return None, "模型目录 JSON 缺少顶层 'models' 列表对象"
        return None, "模型目录 JSON 根节点必须是包含 'models' 列表的对象"
    except Exception as exc:
        return None, f"解析模型目录 JSON 失败 ({file_path.name}): {type(exc).__name__}"


def _collect_providers(
    repo_config: Dict[str, Any],
    global_config_path: Optional[Path],
) -> Set[str]:
    """汇总仓库与全局配置中登记的所有合法 model_providers 名称。

    Args:
        repo_config: 仓库配置字典
        global_config_path: 全局 Codex 配置文件路径

    Returns:
        所有已登记的 provider 集合
    """
    providers: Set[str] = set()

    # 从仓库配置读取
    repo_providers = repo_config.get("model_providers", {})
    if isinstance(repo_providers, dict):
        providers.update(repo_providers.keys())

    # 从全局配置读取
    target_global = global_config_path
    if target_global is None:
        target_global = Path.home() / ".codex" / "config.toml"

    if target_global and target_global.exists():
        global_data, _ = _load_toml_file(target_global)
        if global_data and isinstance(global_data, dict):
            global_providers = global_data.get("model_providers", {})
            if isinstance(global_providers, dict):
                providers.update(global_providers.keys())

    return providers


def check_codex_agents(
    repo_dir: Path | str = ".",
    global_config_path: Optional[Path | str] = None,
    runtime: bool = False,
    codex_bin: str = "codex",
) -> CheckResult:
    """执行 Codex 子代理配置与模型目录的核心检查逻辑。

    Args:
        repo_dir: 仓库根目录
        global_config_path: 全局 ~/.codex/config.toml 路径，缺省时使用用户主目录路径
        runtime: 是否通过子进程调用 codex debug models 验证实际加载
        codex_bin: codex 可执行文件路径或命令名

    Returns:
        CheckResult 对象，包含 success 状态和脱敏安全错误列表
    """
    repo_path = Path(repo_dir).resolve()
    errors: List[str] = []
    warnings: List[str] = []

    repo_config_path = repo_path / ".codex" / "config.toml"
    repo_config, err = _load_toml_file(repo_config_path)
    if err:
        errors.append(f"仓库配置文件错误: {err}")
        return CheckResult(
            success=False,
            errors=errors,
            warnings=warnings,
            summary="仓库配置文件未找到或解析失败",
        )

    assert repo_config is not None

    # 1. 检查并解析 model_catalog_json
    catalog_rel_str = repo_config.get("model_catalog_json")
    if not catalog_rel_str:
        errors.append("仓库配置中缺少 model_catalog_json 路径配置")
        catalog_models: Dict[str, Dict[str, Any]] = {}
    elif not isinstance(catalog_rel_str, str) or not catalog_rel_str.strip():
        errors.append("model_catalog_json 路径必须是非空字符串")
        catalog_models = {}
    else:
        catalog_path = Path(catalog_rel_str)
        if not catalog_path.is_absolute():
            catalog_path = (repo_config_path.parent / catalog_path).resolve()

        raw_models, cat_err = _load_catalog_json(catalog_path)
        if cat_err:
            errors.append(f"模型目录加载失败: {cat_err}")
            catalog_models = {}
        else:
            assert raw_models is not None
            catalog_models = {}
            seen_slugs: Set[str] = set()

            for item in raw_models:
                if not isinstance(item, dict):
                    errors.append("模型目录包含非字典类型的模型条目")
                    continue
                slug = item.get("slug")
                if not slug or not isinstance(slug, str):
                    errors.append("模型条目缺少有效的 'slug' 字段")
                    continue

                if slug in seen_slugs:
                    errors.append(f"模型目录中检测到重复的 slug: '{slug}'")
                seen_slugs.add(slug)

                # 校验关键 schema 字段
                missing_schema_fields = [
                    field_name for field_name in REQUIRED_SCHEMA_FIELDS if field_name not in item
                ]
                if missing_schema_fields:
                    errors.append(
                        f"模型 '{slug}' 缺少必需的 schema 关键字段: {missing_schema_fields}"
                    )

                # JSON 布尔值不能用字符串或数字代替，否则原生目录反序列化会失败。
                for field_name in REQUIRED_SCHEMA_FIELDS[:3]:
                    if field_name in item and not isinstance(item[field_name], bool):
                        errors.append(f"模型 '{slug}' 的 schema 字段 '{field_name}' 必须是布尔值")

                raw_levels = item.get("supported_reasoning_levels")
                if not isinstance(raw_levels, list) or any(
                    not isinstance(level, dict)
                    or not isinstance(level.get("effort"), str)
                    or not level["effort"].strip()
                    or level["effort"] not in {"none", "minimal", "low", "medium", "high", "xhigh"}
                    or not isinstance(level.get("description"), str)
                    for level in raw_levels
                ):
                    errors.append(
                        f"模型 '{slug}' 的 supported_reasoning_levels 必须是含有效 effort 和 description 的对象列表"
                    )
                elif len(raw_levels) != 1 or raw_levels[0].get("effort") != "high":
                    errors.append(
                        f"模型 '{slug}' 的 supported_reasoning_levels 必须仅包含 high"
                    )

                if item.get("default_reasoning_level") != "high":
                    errors.append(
                        f"模型 '{slug}' 的 default_reasoning_level 必须是 high"
                    )

                # 校验 CLI 0.157.1 约束: 至少提供非空 base_instructions 或 model_messages.instructions_template
                base_inst = item.get("base_instructions")
                has_base_inst = isinstance(base_inst, str) and bool(base_inst.strip())
                model_msgs = item.get("model_messages")
                has_template = (
                    isinstance(model_msgs, dict)
                    and isinstance(model_msgs.get("instructions_template"), str)
                    and bool(model_msgs.get("instructions_template", "").strip())
                )
                if not (has_base_inst or has_template):
                    errors.append(
                        f"模型 '{slug}' 缺少非空的 base_instructions 或 model_messages.instructions_template"
                    )

                catalog_models[slug] = item

            # 校验是否包含必须的最小基础模型
            missing_minimal = [
                model_name for model_name in REQUIRED_MINIMAL_MODELS if model_name not in catalog_models
            ]
            if missing_minimal:
                errors.append(
                    f"模型目录缺少必须包含的基础模型: {missing_minimal}"
                )

    # 2. 汇总可用的 model_providers
    resolved_global_path = Path(global_config_path).resolve() if global_config_path else None
    available_providers = _collect_providers(repo_config, resolved_global_path)

    # 仅在全局配置指定了子代理默认项时校验，避免把仓库默认误当作全局覆盖。
    effective_global_path = resolved_global_path or (Path.home() / ".codex" / "config.toml")
    if effective_global_path.exists():
        global_config, global_err = _load_toml_file(effective_global_path)
        if global_err:
            errors.append(f"全局 Codex 配置文件错误: {global_err}")
        elif global_config is not None:
            global_agents = global_config.get("agents", {})
            if isinstance(global_agents, dict) and (
                "default_subagent_model" in global_agents
                or "default_subagent_reasoning_effort" in global_agents
            ):
                if global_agents.get("default_subagent_reasoning_effort") != "high":
                    errors.append(
                        "全局 default_subagent_reasoning_effort 必须显式设置为 high"
                    )

    # 3. 检查 default_subagent_model
    agents_table = repo_config.get("agents", {})
    default_subagent_model: Optional[str] = None
    if isinstance(agents_table, dict):
        default_subagent_model = agents_table.get("default_subagent_model")
    if not default_subagent_model:
        default_subagent_model = repo_config.get("default_subagent_model")

    if not default_subagent_model:
        errors.append("未在 [agents] 或根级别配置 default_subagent_model")
    else:
        if default_subagent_model in FORBIDDEN_MODELS:
            errors.append(
                f"禁止将 '{default_subagent_model}' 配置为 default_subagent_model"
            )
        elif catalog_models and default_subagent_model not in catalog_models:
            errors.append(
                f"default_subagent_model '{default_subagent_model}' 未在模型目录中定义"
            )
        elif catalog_models and default_subagent_model in catalog_models:
            default_effort = (
                agents_table["default_subagent_reasoning_effort"]
                if isinstance(agents_table, dict)
                and "default_subagent_reasoning_effort" in agents_table
                else repo_config.get("default_subagent_reasoning_effort")
            )
            if default_effort != "high":
                errors.append(
                    "default_subagent_reasoning_effort 缺失或非 high；本地策略要求显式 high"
                )
            else:
                model_spec = catalog_models[default_subagent_model]
                supported_levels = _extract_reasoning_levels(
                    model_spec.get("supported_reasoning_levels")
                )
                if default_effort not in supported_levels:
                    errors.append(
                        f"default_subagent_reasoning_effort '{default_effort}' "
                        f"不在模型 '{default_subagent_model}' 的 supported_reasoning_levels 范围内: {sorted(supported_levels)}"
                    )

    # 4. 检查各 registered agents 及其 config_file
    registered_role_models: Set[str] = set()
    registered_role_paths: Set[Path] = set()

    if isinstance(agents_table, dict):
        for role_name, role_val in agents_table.items():
            # 排除非角色子表的顶层属性
            if not isinstance(role_val, dict):
                continue

            config_file_rel = role_val.get("config_file")
            if not config_file_rel:
                errors.append(f"角色 '{role_name}' 未指定 config_file 路径")
                continue

            agent_config_path = (repo_config_path.parent / config_file_rel).resolve()
            registered_role_paths.add(agent_config_path)
            if not agent_config_path.exists():
                errors.append(
                    f"角色 '{role_name}' 的配置文件不存在: {config_file_rel}"
                )
                continue

            role_cfg, role_err = _load_toml_file(agent_config_path)
            if role_err:
                errors.append(f"角色 '{role_name}' 配置文件加载失败: {role_err}")
                continue

            assert role_cfg is not None

            # 校验角色 model
            role_model = role_cfg.get("model") or role_val.get("model")
            if not role_model:
                errors.append(f"角色 '{role_name}' 未配置 model")
            else:
                registered_role_models.add(role_model)
                if role_model in FORBIDDEN_MODELS:
                    errors.append(f"角色 '{role_name}' 禁止使用 '{role_model}'")
                elif catalog_models and role_model not in catalog_models:
                    errors.append(
                        f"角色 '{role_name}' 的模型 '{role_model}' 未在模型目录中定义"
                    )
            # 原生角色只接受 model_reasoning_effort，旧字段不能掩盖缺失或降级。
            reasoning_effort = role_cfg.get("model_reasoning_effort")
            if reasoning_effort != "high":
                errors.append(
                    f"角色 '{role_name}' 的推理等级 model_reasoning_effort '{reasoning_effort}' 缺失或非 high"
                )
            elif role_model in catalog_models:
                supported_levels = _extract_reasoning_levels(
                    catalog_models[role_model].get("supported_reasoning_levels")
                )
                if reasoning_effort not in supported_levels:
                    errors.append(
                        f"角色 '{role_name}' 的 model_reasoning_effort 不受模型 '{role_model}' 支持"
                    )

            # 校验角色 provider
            role_provider = role_cfg.get("model_provider") or role_val.get("model_provider")
            if role_provider:
                if role_provider not in available_providers:
                    errors.append(
                        f"角色 '{role_name}' 指定的 provider '{role_provider}' "
                        f"未在全局或仓库 model_providers 中登记"
                    )

    # 未注册但可调用的角色也必须满足 high-only 策略。
    agents_dir = repo_config_path.parent / "agents"
    if agents_dir.is_dir():
        for agent_config_path in sorted(agents_dir.glob("*.toml")):
            if agent_config_path.resolve() in registered_role_paths:
                continue
            role_cfg, role_err = _load_toml_file(agent_config_path)
            if role_err:
                errors.append(f"未注册角色配置文件加载失败: {role_err}")
                continue
            if not role_cfg:
                continue

            role_name = role_cfg.get("name") or agent_config_path.stem
            role_model = role_cfg.get("model")
            if not role_model:
                errors.append(f"未注册角色 '{role_name}' 未配置 model")
            else:
                registered_role_models.add(role_model)
                if role_model in FORBIDDEN_MODELS:
                    errors.append(f"未注册角色 '{role_name}' 禁止使用 '{role_model}'")
                elif catalog_models and role_model not in catalog_models:
                    errors.append(
                        f"未注册角色 '{role_name}' 的模型 '{role_model}' 未在模型目录中定义"
                    )

            if role_cfg.get("model_reasoning_effort") != "high":
                errors.append(
                    f"未注册角色 '{role_name}' 的 model_reasoning_effort '{role_cfg.get('model_reasoning_effort')}' 缺失或非 high"
                )

            role_provider = role_cfg.get("model_provider")
            if role_provider and role_provider not in available_providers:
                errors.append(f"未注册角色 '{role_name}' 指定的 provider 未登记")

    # 5. 若指定 --runtime，调用子进程校验实际加载模型
    if runtime:
        runtime_err = _check_runtime_models(
            codex_bin=codex_bin,
            repo_path=repo_path,
            required_models=registered_role_models | ({default_subagent_model} if default_subagent_model else set()),
        )
        if runtime_err:
            errors.append(runtime_err)

    success = len(errors) == 0
    if success:
        summary = (
            f"预检全部通过: 模型目录包含 {len(catalog_models)} 个有效模型，"
            f"已校验角色与默认模型合规性。"
        )
    else:
        summary = f"预检发现 {len(errors)} 项阻断性错误，请修复后继续。"

    return CheckResult(
        success=success,
        errors=errors,
        warnings=warnings,
        summary=summary,
    )


def _check_runtime_models(
    codex_bin: str,
    repo_path: Path,
    required_models: Set[str],
) -> Optional[str]:
    """通过子进程调用 codex debug models 验证运行时模型加载。

    严禁泄露输出中的敏感凭据或原始错误流，仅提取模型 slug 并对比。

    Args:
        codex_bin: codex 可执行文件路径
        repo_path: 仓库工作目录
        required_models: 需要验证实际加载的模型 slug 集合

    Returns:
        若有错误返回安全错误描述文本，成功则返回 None
    """
    cmd = [codex_bin, "debug", "models"]
    try:
        proc = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "Runtime 检查执行超时（超过 30 秒）"
    except FileNotFoundError:
        return f"未找到指定可执行文件: {codex_bin}"
    except Exception as exc:
        return f"Runtime 检查启动异常: {type(exc).__name__}"

    if proc.returncode != 0:
        return f"Runtime 检查子进程非零退出 (退出码 {proc.returncode})"

    # 安全解析 stdout，不打印敏感文本
    output_text = proc.stdout or ""
    try:
        data = json.loads(output_text)
    except Exception:
        return "Runtime 检查失败: codex debug models 输出非标准 JSON"

    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        return "Runtime 检查失败: codex debug models 输出缺少顶层 'models' 列表对象"

    loaded_models: Set[str] = set()
    # 损坏条目和重复名称必须阻断，不能只收集合法名称后误报整个目录通过。
    for item in data["models"]:
        if not isinstance(item, dict):
            return "Runtime 检查失败: 模型目录包含非对象条目"
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            return "Runtime 检查失败: 模型目录包含无效 slug"
        if slug in loaded_models:
            return "Runtime 检查失败: 模型目录包含重复 slug"
        loaded_models.add(slug)

    missing_runtime = [
        model_name
        for model_name in sorted(required_models)
        if model_name not in loaded_models
    ]
    if missing_runtime:
        return f"Runtime 检查未能确认加载以下模型: {missing_runtime}"

    return None


def main(argv: Optional[List[str]] = None) -> None:
    """CLI 入口函数，解析参数并输出安全汇总信息。"""
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    parser = argparse.ArgumentParser(
        description="Codex 子代理配置与模型目录离线预检工具",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--repo",
        dest="repo",
        default=".",
        help="仓库根目录路径",
    )
    parser.add_argument(
        "--global-config",
        dest="global_config",
        default=None,
        help="全局 Codex 配置文件路径 (默认 ~/.codex/config.toml)",
    )
    parser.add_argument(
        "--runtime",
        action="store_true",
        help="通过子进程调用 `codex debug models` 验证实际加载情况",
    )
    parser.add_argument(
        "--codex",
        dest="codex_bin",
        default="codex",
        help="指定 codex 二进制程序路径",
    )

    args = parser.parse_args(argv)

    result = check_codex_agents(
        repo_dir=args.repo,
        global_config_path=args.global_config,
        runtime=args.runtime,
        codex_bin=args.codex_bin,
    )

    if result.success:
        print(f"[PASS] {result.summary}")
        if result.warnings:
            for warning in result.warnings:
                print(f"  [WARN] {warning}")
        sys.exit(0)
    else:
        print(f"[FAIL] {result.summary}")
        for err in result.errors:
            print(f"  - {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
