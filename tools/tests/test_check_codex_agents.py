# -*- coding: utf-8 -*-
"""Codex 子代理配置与模型目录预检工具的单元测试集。

覆盖场景：
1. 完整合规配置下所有检查项全部通过（PASS）
2. 模型目录缺失必选基础模型或存在重复 slug
3. default_subagent_model 非法或配置了未登记的 gpt-6-astra
4. Agent 角色缺少配置文件、未配置模型、配置未登记模型或未注册 provider
5. Agent 角色指定了不支持的推理等级（reasoning level）
6. 旧模型目录缺少关键 schema 字段
7. --runtime 运行时 subprocess 调用成功解析与异常（超时、非零退出、输出缺少模型）
8. CLI main 入口参数解析与退出码（0 与 1）验证
"""

import json
import subprocess
import sys
import tempfile
import unittest
import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools.check_codex_agents import (
    CheckResult,
    REQUIRED_MINIMAL_MODELS,
    REQUIRED_SCHEMA_FIELDS,
    check_codex_agents,
    main,
)


class TestCheckCodexAgents(unittest.TestCase):
    """Codex 子代理预检测试类。"""

    def setUp(self) -> None:
        """创建临时测试目录并准备标准测试夹具。"""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)

        self.repo_dir = self.base_path / "repo"
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        self.codex_dir = self.repo_dir / ".codex"
        self.codex_dir.mkdir(parents=True, exist_ok=True)
        self.agents_dir = self.codex_dir / "agents"
        self.agents_dir.mkdir(parents=True, exist_ok=True)

        self.global_dir = self.base_path / "global_codex"
        self.global_dir.mkdir(parents=True, exist_ok=True)
        self.global_config_path = self.global_dir / "config.toml"

        self._create_valid_fixture()

    def tearDown(self) -> None:
        """清理临时测试目录。"""
        self.temp_dir.cleanup()

    def _create_valid_fixture(self) -> None:
        """生成完全合规的测试配置与模型目录。"""
        global_config_content = """
[model_providers.custom]
name = "studytop"
wire_api = "responses"
"""
        self.global_config_path.write_text(global_config_content, encoding="utf-8")

        catalog_data = {
            "models": [
                {
                    "slug": "gpt-6.1-sol",
                    "default_reasoning_level": "high",
                    "support_verbosity": True,
                    "supports_reasoning_summary_parameter": True,
                    "supports_parallel_tool_calls": True,
                    "supported_reasoning_levels": [{"effort": "high", "description": "复杂任务"}],
                    "base_instructions": "You are Sol, chief architect.",
                },
                {
                    "slug": "gemini-3.8-flash",
                    "default_reasoning_level": "high",
                    "support_verbosity": True,
                    "supports_reasoning_summary_parameter": True,
                    "supports_parallel_tool_calls": True,
                    "supported_reasoning_levels": [{"effort": "high", "description": "复杂任务"}],
                    "base_instructions": "You are Gemini, coding assistant.",
                },
                {
                    "slug": "grok-4.7",
                    "default_reasoning_level": "high",
                    "support_verbosity": True,
                    "supports_reasoning_summary_parameter": False,
                    "supports_parallel_tool_calls": True,
                    "supported_reasoning_levels": [{"effort": "high", "description": "复杂任务"}],
                    "model_messages": {
                        "instructions_template": "You are Grok auditor."
                    },
                },
                {
                    "slug": "claude-opus-5-5",
                    "default_reasoning_level": "high",
                    "support_verbosity": True,
                    "supports_reasoning_summary_parameter": False,
                    "supports_parallel_tool_calls": True,
                    "supported_reasoning_levels": [{"effort": "high", "description": "复杂任务"}],
                    "base_instructions": "You are Claude, complex dev.",
                },
            ]
        }
        self.catalog_path = self.codex_dir / "model-catalog.json"
        self.catalog_path.write_text(json.dumps(catalog_data, ensure_ascii=False, indent=2), encoding="utf-8")

        repo_config_content = """
model_catalog_json = "model-catalog.json"

[agents]
default_subagent_model = "gpt-6.1-sol"
default_subagent_reasoning_effort = "high"

[agents.sol]
description = "Sol 架构师"
config_file = "agents/sol.toml"

[agents.gemini]
description = "Gemini 工程师"
config_file = "agents/gemini.toml"
"""
        self.repo_config_path = self.codex_dir / "config.toml"
        self.repo_config_path.write_text(repo_config_content, encoding="utf-8")

        sol_agent_content = """
name = "sol"
model_provider = "custom"
model = "gpt-6.1-sol"
model_reasoning_effort = "high"
"""
        (self.agents_dir / "sol.toml").write_text(sol_agent_content, encoding="utf-8")

        gemini_agent_content = """
name = "gemini"
model_provider = "custom"
model = "gemini-3.8-flash"
model_reasoning_effort = "high"
"""
        (self.agents_dir / "gemini.toml").write_text(gemini_agent_content, encoding="utf-8")

    def test_all_pass_valid_configuration(self) -> None:
        """验证标准合规配置下所有离线检查全部通过。"""
        result: CheckResult = check_codex_agents(
            repo_dir=self.repo_dir,
            global_config_path=self.global_config_path,
            runtime=False,
        )
        self.assertTrue(result.success, f"预检预期通过但失败: {result.errors}")
        self.assertEqual(len(result.errors), 0)
        self.assertIn("全部通过", result.summary)

    def test_unquoted_catalog_path_returns_safe_failure(self) -> None:
        """拦截目录路径缺少引号，避免启动失败被预检漏报。"""
        content = self.repo_config_path.read_text(encoding="utf-8")
        content = content.replace('"model-catalog.json"', "model-catalog.json")
        self.repo_config_path.write_text(content, encoding="utf-8")
        result = check_codex_agents(self.repo_dir, self.global_config_path)
        self.assertFalse(result.success)
        self.assertTrue(any("TOML" in error for error in result.errors))

    def test_catalog_path_rejects_non_string_values(self) -> None:
        """畸形路径必须返回安全失败，不得抛出未捕获的类型异常。"""
        content = self.repo_config_path.read_text(encoding="utf-8")
        for invalid_value in ['["model-catalog.json"]', "42", "true", '"   "']:
            with self.subTest(invalid_value=invalid_value):
                self.repo_config_path.write_text(
                    content.replace('"model-catalog.json"', invalid_value),
                    encoding="utf-8",
                )
                result = check_codex_agents(self.repo_dir, self.global_config_path)
                self.assertFalse(result.success)
                self.assertTrue(any("model_catalog_json" in error for error in result.errors))

    def test_role_path_does_not_fall_back_to_repo_root(self) -> None:
        """同名仓库根目录文件不能掩盖实际角色路径缺失。"""
        root_agents = self.repo_dir / "agents"
        root_agents.mkdir()
        source_path = self.agents_dir / "sol.toml"
        (root_agents / "sol.toml").write_text(
            source_path.read_text(encoding="utf-8"), encoding="utf-8"
        )
        source_path.unlink()
        result = check_codex_agents(self.repo_dir, self.global_config_path)
        self.assertFalse(result.success)
        self.assertTrue(any("不存在" in error and "sol.toml" in error for error in result.errors))

    def test_catalog_schema_boolean_fields_reject_invalid_types(self) -> None:
        """布尔能力字段不能接受字符串、数字或空值。"""
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        for field_name in [
            "support_verbosity",
            "supports_reasoning_summary_parameter",
            "supports_parallel_tool_calls",
        ]:
            for invalid_value in ["false", 1, None]:
                with self.subTest(field_name=field_name, invalid_value=invalid_value):
                    catalog["models"][0][field_name] = invalid_value
                    self.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
                    result = check_codex_agents(self.repo_dir, self.global_config_path)
                    self.assertFalse(result.success)
                    self.assertTrue(any(field_name in error for error in result.errors))
            catalog["models"][0][field_name] = True

    def test_catalog_reasoning_schema_rejects_malformed_entries(self) -> None:
        """推理等级须使用原生对象列表，不能用兼容别名掩盖加载失败。"""
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        for invalid_levels in [
            "high", ["high"], [{"effort": "high"}],
            [{"effort": 42, "description": "复杂任务"}],
            [{"effort": "unknown", "description": "复杂任务"}],
            [{"effort": "high", "description": None}],
        ]:
            with self.subTest(invalid_levels=invalid_levels):
                catalog["models"][0]["supported_reasoning_levels"] = invalid_levels
                self.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
                result = check_codex_agents(self.repo_dir, self.global_config_path)
                self.assertFalse(result.success)
                self.assertTrue(any("supported_reasoning_levels" in error for error in result.errors))

    def test_catalog_missing_required_minimal_model(self) -> None:
        """验证目录模型缺失基础模型时拦截（如缺少 claude-opus-5-5）。"""
        catalog_data = {
            "models": [
                {
                    "slug": "gpt-6.1-sol",
                    "default_reasoning_level": "high",
                    "support_verbosity": True,
                    "supports_reasoning_summary_parameter": True,
                    "supports_parallel_tool_calls": True,
                    "supported_reasoning_levels": [{"effort": "high", "description": "复杂任务"}],
                    "base_instructions": "You are Sol.",
                },
                {
                    "slug": "gemini-3.8-flash",
                    "default_reasoning_level": "high",
                    "support_verbosity": True,
                    "supports_reasoning_summary_parameter": True,
                    "supports_parallel_tool_calls": True,
                    "supported_reasoning_levels": [{"effort": "high", "description": "复杂任务"}],
                    "base_instructions": "You are Gemini.",
                },
                {
                    "slug": "grok-4.7",
                    "default_reasoning_level": "high",
                    "support_verbosity": True,
                    "supports_reasoning_summary_parameter": True,
                    "supports_parallel_tool_calls": True,
                    "supported_reasoning_levels": [{"effort": "high", "description": "复杂任务"}],
                    "base_instructions": "You are Grok.",
                },
            ]
        }
        self.catalog_path.write_text(json.dumps(catalog_data), encoding="utf-8")

        result = check_codex_agents(
            repo_dir=self.repo_dir,
            global_config_path=self.global_config_path,
        )
        self.assertFalse(result.success)
        self.assertTrue(any("缺少" in err and "claude-opus-5-5" in err for err in result.errors))

    def test_catalog_duplicate_slug_detection(self) -> None:
        """验证目录模型包含重复 slug 时拦截。"""
        catalog_data = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        duplicate = next(model for model in catalog_data["models"] if model["slug"] == "gemini-3.8-flash")
        catalog_data["models"].append(duplicate)
        self.catalog_path.write_text(json.dumps(catalog_data), encoding="utf-8")

        result = check_codex_agents(self.repo_dir, self.global_config_path)
        self.assertFalse(result.success)
        self.assertTrue(any("重复" in err and "gemini-3.8-flash" in err for err in result.errors))

    def test_default_subagent_model_invalid_or_astra(self) -> None:
        """验证未登记的默认模型被拦截，包括尚未登记的 Astra。"""
        repo_config_astra = """
model_catalog_json = "model-catalog.json"

[agents]
default_subagent_model = "gpt-6-astra"
default_subagent_reasoning_effort = "high"

[agents.sol]
config_file = "agents/sol.toml"
"""
        self.repo_config_path.write_text(repo_config_astra, encoding="utf-8")

        result = check_codex_agents(
            repo_dir=self.repo_dir,
            global_config_path=self.global_config_path,
        )
        self.assertFalse(result.success)
        self.assertTrue(any("未在模型目录" in err and "gpt-6-astra" in err for err in result.errors))

        repo_config_unknown = """
model_catalog_json = "model-catalog.json"

[agents]
default_subagent_model = "non-existent-model"
default_subagent_reasoning_effort = "high"

[agents.sol]
config_file = "agents/sol.toml"
"""
        self.repo_config_path.write_text(repo_config_unknown, encoding="utf-8")
        result_unknown = check_codex_agents(
            repo_dir=self.repo_dir,
            global_config_path=self.global_config_path,
        )
        self.assertFalse(result_unknown.success)
        self.assertTrue(any("未在模型目录" in err for err in result_unknown.errors))

    def test_role_missing_file_or_invalid_model_or_astra(self) -> None:
        """验证角色缺配置文件或使用未登记模型时被拦截。"""
        repo_config_missing_file = (
            'model_catalog_json = "model-catalog.json"\n'
            "[agents]\n"
            'default_subagent_model = "gpt-6.1-sol"\n'
            'default_subagent_reasoning_effort = "high"\n'
            "[agents.missing]\n"
            'config_file = "agents/missing.toml"\n'
        )
        self.repo_config_path.write_text(repo_config_missing_file, encoding="utf-8")
        result = check_codex_agents(self.repo_dir, self.global_config_path)
        self.assertFalse(result.success)
        self.assertTrue(any("不存在" in err and "missing.toml" in err for err in result.errors))

        astra_agent_content = (
            'name = "bad_role"\n'
            'model_provider = "custom"\n'
            'model = "gpt-6-astra"\n'
            'model_reasoning_effort = "high"\n'
        )
        (self.agents_dir / "bad_role.toml").write_text(astra_agent_content, encoding="utf-8")
        repo_config_bad_role = (
            'model_catalog_json = "model-catalog.json"\n'
            "[agents]\n"
            'default_subagent_model = "gpt-6.1-sol"\n'
            'default_subagent_reasoning_effort = "high"\n'
            "[agents.bad]\n"
            'config_file = "agents/bad_role.toml"\n'
        )
        self.repo_config_path.write_text(repo_config_bad_role, encoding="utf-8")
        result_bad = check_codex_agents(self.repo_dir, self.global_config_path)
        self.assertFalse(result_bad.success)
        self.assertTrue(any("未在模型目录" in err and "gpt-6-astra" in err for err in result_bad.errors))

    def test_registered_astra_allowed_for_default_and_roles(self) -> None:
        """Astra 登记后允许默认及角色使用，仍校验推理等级。"""
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        existing = {entry["slug"] for entry in catalog["models"]}
        for slug in (*REQUIRED_MINIMAL_MODELS, "gpt-6-astra"):
            if slug not in existing:
                catalog["models"].append(dict(catalog["models"][0], slug=slug))
        self.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        config = self.repo_config_path.read_text(encoding="utf-8")
        self.repo_config_path.write_text(
            config.replace('default_subagent_model = "gpt-6.1-sol"',
                           'default_subagent_model = "gpt-6-astra"'), encoding="utf-8"
        )
        role_config = (
            'model_provider = "custom"\n'
            'model = "gpt-6-astra"\n'
            'model_reasoning_effort = "high"\n'
        )
        for filename in ("sol.toml", "astra-unregistered.toml"):
            (self.agents_dir / filename).write_text(role_config, encoding="utf-8")
        result = check_codex_agents(self.repo_dir, self.global_config_path)
        self.assertTrue(result.success, result.errors)
        (self.agents_dir / "sol.toml").write_text(
            role_config.replace('"high"', '"low"'), encoding="utf-8"
        )
        rejected = check_codex_agents(self.repo_dir, self.global_config_path)
        self.assertFalse(rejected.success)
        self.assertTrue(any("high" in error for error in rejected.errors))

    def test_role_provider_not_registered(self) -> None:
        """验证角色指定的 provider 未在全局或仓库 model_providers 中时拦截。"""
        unregistered_provider_agent = """
name = "sol"
model_provider = "non_existent_provider"
model = "gpt-6.1-sol"
model_reasoning_effort = "high"
"""
        (self.agents_dir / "sol.toml").write_text(unregistered_provider_agent, encoding="utf-8")
        result = check_codex_agents(
            repo_dir=self.repo_dir,
            global_config_path=self.global_config_path,
        )
        self.assertFalse(result.success)
        self.assertTrue(any("provider" in err and "non_existent_provider" in err for err in result.errors))

    def test_role_unsupported_reasoning_level(self) -> None:
        """验证角色配置的推理等级不在模型 supported_reasoning_levels 内时拦截。"""
        bad_reasoning_agent = """
name = "sol"
model_provider = "custom"
model = "gpt-6.1-sol"
model_reasoning_effort = "extreme_ultra"
"""
        (self.agents_dir / "sol.toml").write_text(bad_reasoning_agent, encoding="utf-8")
        result = check_codex_agents(
            repo_dir=self.repo_dir,
            global_config_path=self.global_config_path,
        )
        self.assertFalse(result.success)
        self.assertTrue(any("推理等级" in err and "extreme_ultra" in err for err in result.errors))

    def test_catalog_missing_schema_fields(self) -> None:
        """验证旧模型目录缺少必需的 schema 关键字段时拦截。"""
        catalog_missing_fields = {
            "models": [
                {
                    "slug": "gpt-6.1-sol",
                    "support_verbosity": True,
                },
                {
                    "slug": "gemini-3.8-flash",
                    "supports_parallel_tool_calls": True,
                },
                {
                    "slug": "grok-4.7",
                    "supported_reasoning_levels": ["high"],
                },
                {
                    "slug": "claude-opus-5-5",
                    "supported_reasoning_levels": ["high"],
                },
            ]
        }
        self.catalog_path.write_text(json.dumps(catalog_missing_fields), encoding="utf-8")
        result = check_codex_agents(
            repo_dir=self.repo_dir,
            global_config_path=self.global_config_path,
        )
        self.assertFalse(result.success)
        self.assertTrue(any("schema" in err for err in result.errors))

    def test_catalog_missing_instructions_fails(self) -> None:
        """验证模型目录中缺少非空 base_instructions 和 instructions_template 时拦截。"""
        catalog_missing_instructions = {
           "models": [
               {
                   "slug": "gpt-6.1-sol",
                    "default_reasoning_level": "high",
                    "support_verbosity": True,
                    "supports_reasoning_summary_parameter": True,
                    "supports_parallel_tool_calls": True,
                    "supported_reasoning_levels": [{"effort": "high", "description": "复杂任务"}],
                },
                {
                    "slug": "gemini-3.8-flash",
                    "default_reasoning_level": "high",
                    "support_verbosity": True,
                    "supports_reasoning_summary_parameter": True,
                    "supports_parallel_tool_calls": True,
                    "supported_reasoning_levels": [{"effort": "high", "description": "复杂任务"}],
                    "base_instructions": "   ",
                },
                {
                    "slug": "grok-4.7",
                    "default_reasoning_level": "high",
                    "support_verbosity": True,
                    "supports_reasoning_summary_parameter": True,
                    "supports_parallel_tool_calls": True,
                    "supported_reasoning_levels": [{"effort": "high", "description": "复杂任务"}],
                    "model_messages": {},
                },
                {
                    "slug": "claude-opus-5-5",
                    "default_reasoning_level": "high",
                    "support_verbosity": True,
                    "supports_reasoning_summary_parameter": True,
                    "supports_parallel_tool_calls": True,
                    "supported_reasoning_levels": [{"effort": "high", "description": "复杂任务"}],
                    "model_messages": {"instructions_template": ""},
                },
            ]
        }
        self.catalog_path.write_text(json.dumps(catalog_missing_instructions), encoding="utf-8")
        result = check_codex_agents(
            repo_dir=self.repo_dir,
            global_config_path=self.global_config_path,
        )
        self.assertFalse(result.success)
        self.assertTrue(any("base_instructions" in err or "instructions_template" in err for err in result.errors))

    def test_supported_reasoning_levels_object_list(self) -> None:
        """验证 supported_reasoning_levels 为对象列表 [{"effort": "high"}] 时正常解析。"""
        catalog_data = {
            "models": [
                {
                    "slug": "gpt-6.1-sol",
                    "default_reasoning_level": "high",
                    "support_verbosity": True,
                    "supports_reasoning_summary_parameter": True,
                    "supports_parallel_tool_calls": True,
                    "supported_reasoning_levels": [
                        {"effort": "high", "description": "复杂任务"},
                    ],
                    "base_instructions": "You are Sol.",
                },
                {
                    "slug": "gemini-3.8-flash",
                    "default_reasoning_level": "high",
                    "support_verbosity": True,
                    "supports_reasoning_summary_parameter": True,
                    "supports_parallel_tool_calls": True,
                    "supported_reasoning_levels": [
                        {"effort": "high", "description": "复杂任务"},
                    ],
                    "base_instructions": "You are Gemini.",
                },
                {
                    "slug": "grok-4.7",
                    "default_reasoning_level": "high",
                    "support_verbosity": True,
                    "supports_reasoning_summary_parameter": False,
                    "supports_parallel_tool_calls": True,
                    "supported_reasoning_levels": [
                        {"effort": "high", "description": "复杂任务"},
                    ],
                    "base_instructions": "You are Grok.",
                },
                {
                    "slug": "claude-opus-5-5",
                    "default_reasoning_level": "high",
                    "support_verbosity": True,
                    "supports_reasoning_summary_parameter": False,
                    "supports_parallel_tool_calls": True,
                    "supported_reasoning_levels": [
                        {"effort": "high", "description": "复杂任务"},
                    ],
                    "base_instructions": "You are Claude.",
                },
            ]
        }
        self.catalog_path.write_text(json.dumps(catalog_data), encoding="utf-8")
        result = check_codex_agents(
            repo_dir=self.repo_dir,
            global_config_path=self.global_config_path,
        )
        self.assertTrue(result.success, f"对象列表 reasoning levels 解析失败: {result.errors}")

    def test_catalog_rejects_top_level_list(self) -> None:
        """验证模型目录顶层为数组而非包含 models 的对象时被拒绝。"""
        catalog_list = [
            {
                "slug": "gpt-6.1-sol",
                "support_verbosity": True,
                "supports_reasoning_summary_parameter": True,
                "supports_parallel_tool_calls": True,
                "supported_reasoning_levels": ["high"],
                "base_instructions": "You are Sol.",
            }
        ]
        self.catalog_path.write_text(json.dumps(catalog_list), encoding="utf-8")
        result = check_codex_agents(
            repo_dir=self.repo_dir,
            global_config_path=self.global_config_path,
        )
        self.assertFalse(result.success)
        self.assertTrue(any("models" in err for err in result.errors))

    def test_config_rejects_model_catalog_json_in_agents_section(self) -> None:
        """验证 model_catalog_json 仅放在 [agents] 下时被拒绝（必须在顶层配置）。"""
        repo_config_agents_only = """
[agents]
model_catalog_json = "model-catalog.json"
default_subagent_model = "gpt-6.1-sol"
default_subagent_reasoning_effort = "high"

[agents.sol]
config_file = "agents/sol.toml"
"""
        self.repo_config_path.write_text(repo_config_agents_only, encoding="utf-8")
        result = check_codex_agents(
            repo_dir=self.repo_dir,
            global_config_path=self.global_config_path,
        )
        self.assertFalse(result.success)
        self.assertTrue(any("缺少 model_catalog_json" in err for err in result.errors))

    @patch("subprocess.run")
    def test_runtime_rejects_malformed_or_duplicate_entries(self, mock_run: MagicMock) -> None:
        """验证有效模型混入畸形或重复条目时不能误报运行时通过。"""
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        for extra_entry in [None, {"slug": ""}, {"slug": 42}, catalog["models"][0]]:
            with self.subTest(extra_entry=extra_entry):
                mock_run.return_value = subprocess.CompletedProcess(
                    args=["codex", "debug", "models"],
                    returncode=0,
                    stdout=json.dumps({"models": catalog["models"] + [extra_entry]}),
                    stderr="",
                )
                result = check_codex_agents(
                    repo_dir=self.repo_dir,
                    global_config_path=self.global_config_path,
                    runtime=True,
                )
                self.assertFalse(result.success)

    @patch("subprocess.run")
    def test_runtime_subprocess_success(self, mock_run: MagicMock) -> None:
        """验证 --runtime 模式下通过 subprocess 检查模型加载成功。"""
        mock_output = json.dumps({
            "models": [
                {"slug": "gpt-6.1-sol"},
                {"slug": "gemini-3.8-flash"},
                {"slug": "grok-4.7"},
                {"slug": "claude-opus-5-5"},
            ]
        })
        mock_run.return_value = subprocess.CompletedProcess(
            args=["codex", "debug", "models"],
            returncode=0,
            stdout=mock_output,
            stderr="",
        )

        result = check_codex_agents(
            repo_dir=self.repo_dir,
            global_config_path=self.global_config_path,
            runtime=True,
            codex_bin="codex",
        )
        self.assertTrue(result.success, f"预期 runtime 成功但失败: {result.errors}")
        mock_run.assert_called_once_with(
            ["codex", "debug", "models"],
            cwd=self.repo_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )

    @patch("subprocess.run")
    def test_runtime_subprocess_failure_and_timeout(self, mock_run: MagicMock) -> None:
        """验证 --runtime 模式下子进程非零退出与超时处理，且不输出原始 stderr。"""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["codex", "debug", "models"],
            returncode=1,
            stdout="",
            stderr="sensitive_token_or_secret_xyz",
        )
        result_fail = check_codex_agents(
            repo_dir=self.repo_dir,
            global_config_path=self.global_config_path,
            runtime=True,
        )
        self.assertFalse(result_fail.success)
        self.assertTrue(any("非零" in err or "失败" in err for err in result_fail.errors))
        for err in result_fail.errors:
            self.assertNotIn("sensitive_token_or_secret_xyz", err)

        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["codex", "debug", "models"], timeout=30)
        result_timeout = check_codex_agents(
            repo_dir=self.repo_dir,
            global_config_path=self.global_config_path,
            runtime=True,
        )
        self.assertFalse(result_timeout.success)
        self.assertTrue(any("超时" in err for err in result_timeout.errors))

    @patch("subprocess.run")
    def test_runtime_rejects_non_json_or_text_output(self, mock_run: MagicMock) -> None:
        """验证 runtime 模式严格拒绝非 JSON 或纯文本列表输出，防止错误文本误报通过。"""
        # 场景 A: 模拟进程返回 0，但 stdout 仅是纯文本列出的四个模型名称
        text_output = (
            "Available models:\n"
            "- gpt-6.1-sol\n"
            "- gemini-3.8-flash\n"
            "- grok-4.7\n"
            "- claude-opus-5-5\n"
        )
        mock_run.return_value = subprocess.CompletedProcess(
            args=["codex", "debug", "models"],
            returncode=0,
            stdout=text_output,
            stderr="",
        )
        result_text = check_codex_agents(
            repo_dir=self.repo_dir,
            global_config_path=self.global_config_path,
            runtime=True,
        )
        self.assertFalse(result_text.success, "纯文本输出不应被误判为有效 JSON models 对象")
        self.assertTrue(any("JSON" in err or "结构" in err or "未能确认" in err for err in result_text.errors))

        # 场景 B: 模拟包含模型名称的错误信息
        error_text = "ERROR: Failed initializing gpt-6.1-sol, gemini-3.8-flash, grok-4.7, claude-opus-5-5"
        mock_run.return_value = subprocess.CompletedProcess(
            args=["codex", "debug", "models"],
            returncode=0,
            stdout=error_text,
            stderr="",
        )
        result_err = check_codex_agents(
            repo_dir=self.repo_dir,
            global_config_path=self.global_config_path,
            runtime=True,
        )
        self.assertFalse(result_err.success)

    def test_cli_main_exit_codes(self) -> None:
        """验证 CLI main 入口函数在成功与失败时的退出码。"""
        buf_out = io.StringIO()
        buf_err = io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            with self.assertRaises(SystemExit) as cm_pass:
                main([
                    "--repo", str(self.repo_dir),
                    "--global-config", str(self.global_config_path),
                ])
        self.assertEqual(cm_pass.exception.code, 0)

        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            with self.assertRaises(SystemExit) as cm_fail:
                main([
                    "--repo", str(self.base_path / "non_existent_repo"),
                ])
        self.assertEqual(cm_fail.exception.code, 1)


    def test_default_subagent_reasoning_effort_must_be_high(self) -> None:
        """验证 default_subagent_reasoning_effort 缺失或非 high 时拦截。"""
        # 场景 A: 缺失配置
        repo_config_missing_effort = """
model_catalog_json = "model-catalog.json"

[agents]
default_subagent_model = "gpt-6.1-sol"

[agents.sol]
config_file = "agents/sol.toml"
"""
        self.repo_config_path.write_text(repo_config_missing_effort, encoding="utf-8")
        result_missing = check_codex_agents(self.repo_dir, self.global_config_path)
        self.assertFalse(result_missing.success)
        self.assertTrue(any("default_subagent_reasoning_effort" in err and "high" in err for err in result_missing.errors))

        # 场景 B: 配置为非 high（如 medium）
        repo_config_medium = """
model_catalog_json = "model-catalog.json"

[agents]
default_subagent_model = "gpt-6.1-sol"
default_subagent_reasoning_effort = "medium"

[agents.sol]
config_file = "agents/sol.toml"
"""
        self.repo_config_path.write_text(repo_config_medium, encoding="utf-8")
        result_medium = check_codex_agents(self.repo_dir, self.global_config_path)
        self.assertFalse(result_medium.success)
        self.assertTrue(any("default_subagent_reasoning_effort" in err and "high" in err for err in result_medium.errors))

    def test_global_default_subagent_effort_must_be_high(self) -> None:
        """验证全局子代理默认等级缺失或非 high 时被拦截。"""
        for global_agents in [
            '[agents]\ndefault_subagent_model = "gpt-6.1-sol"\n',
            '[agents]\ndefault_subagent_model = "gpt-6.1-sol"\ndefault_subagent_reasoning_effort = "medium"\n',
        ]:
            with self.subTest(global_agents=global_agents):
                self.global_config_path.write_text(
                    '[model_providers.custom]\nname = "test"\nwire_api = "responses"\n' + global_agents,
                    encoding="utf-8",
                )
                result = check_codex_agents(self.repo_dir, self.global_config_path)
                self.assertFalse(result.success)
                self.assertTrue(any("全局" in error and "high" in error for error in result.errors))

    def test_registered_agent_model_reasoning_effort_must_be_high(self) -> None:
        """验证已注册角色缺失或非 high 的 model_reasoning_effort 时拦截。"""
        # 场景 A: 缺失 model_reasoning_effort
        missing_effort_agent = """
name = "sol"
model_provider = "custom"
model = "gpt-6.1-sol"
"""
        (self.agents_dir / "sol.toml").write_text(missing_effort_agent, encoding="utf-8")
        result_missing = check_codex_agents(self.repo_dir, self.global_config_path)
        self.assertFalse(result_missing.success)
        self.assertTrue(any("model_reasoning_effort" in err and "sol" in err for err in result_missing.errors))

        # 场景 B: 设置为非 high（如 low）
        low_effort_agent = """
name = "sol"
model_provider = "custom"
model = "gpt-6.1-sol"
model_reasoning_effort = "low"
"""
        (self.agents_dir / "sol.toml").write_text(low_effort_agent, encoding="utf-8")
        result_low = check_codex_agents(self.repo_dir, self.global_config_path)
        self.assertFalse(result_low.success)
        self.assertTrue(any("model_reasoning_effort" in err and "sol" in err and "high" in err for err in result_low.errors))

    def test_registered_agent_rejects_legacy_fields_when_model_reasoning_effort_missing(self) -> None:
        """验证角色缺少 model_reasoning_effort 时禁止以旧字段 (reasoning_effort) 掩盖缺失。"""
        legacy_field_agent = """
name = "sol"
model_provider = "custom"
model = "gpt-6.1-sol"
reasoning_effort = "high"
default_reasoning_level = "high"
"""
        (self.agents_dir / "sol.toml").write_text(legacy_field_agent, encoding="utf-8")
        result = check_codex_agents(self.repo_dir, self.global_config_path)
        self.assertFalse(result.success)
        self.assertTrue(any("model_reasoning_effort" in err and "sol" in err for err in result.errors))

    def test_catalog_default_reasoning_level_must_be_high(self) -> None:
        """验证模型目录中 default_reasoning_level 缺失或非 high 时拦截。"""
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        # 场景 A: 缺失
        del catalog["models"][0]["default_reasoning_level"]
        self.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        result_missing = check_codex_agents(self.repo_dir, self.global_config_path)
        self.assertFalse(result_missing.success)
        self.assertTrue(any("default_reasoning_level" in err for err in result_missing.errors))

        # 场景 B: 非 high（如 medium）
        catalog["models"][0]["default_reasoning_level"] = "medium"
        self.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        result_medium = check_codex_agents(self.repo_dir, self.global_config_path)
        self.assertFalse(result_medium.success)
        self.assertTrue(any("default_reasoning_level" in err and "high" in err for err in result_medium.errors))

    def test_catalog_supported_reasoning_levels_high_only(self) -> None:
        """验证模型目录 supported_reasoning_levels 必须仅包含 high，拒绝缺失 high 或包含非 high 选项。"""
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        # 场景 A: 缺少 high
        catalog["models"][0]["supported_reasoning_levels"] = [
            {"effort": "medium", "description": "标准"}
        ]
        self.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        result_no_high = check_codex_agents(self.repo_dir, self.global_config_path)
        self.assertFalse(result_no_high.success)
        self.assertTrue(any("supported_reasoning_levels" in err and "high" in err for err in result_no_high.errors))

        # 场景 B: 包含非 high 选项（如 low + high）
        catalog["models"][0]["supported_reasoning_levels"] = [
            {"effort": "high", "description": "复杂任务"},
            {"effort": "low", "description": "轻量任务"},
        ]
        self.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        result_with_non_high = check_codex_agents(self.repo_dir, self.global_config_path)
        self.assertFalse(result_with_non_high.success)
        self.assertTrue(any("supported_reasoning_levels" in err and ("非 high" in err or "high" in err) for err in result_with_non_high.errors))

    def test_unregistered_agent_files_must_be_high_only(self) -> None:
        """验证 agents 目录下有效未注册角色同样强制执行 high-only 防回归。"""
        unregistered_path = self.agents_dir / "unregistered-reviewer.toml"
        # 场景 A: 未注册角色 model_reasoning_effort 为 medium
        unregistered_path.write_text("""
name = "unregistered-reviewer"
model_provider = "custom"
model = "grok-4.7"
model_reasoning_effort = "medium"
""", encoding="utf-8")
        result_fail = check_codex_agents(self.repo_dir, self.global_config_path)
        self.assertFalse(result_fail.success)
        self.assertTrue(any("model_reasoning_effort" in err and "unregistered-reviewer" in err for err in result_fail.errors))

        # 场景 B: 修复为 high
        unregistered_path.write_text("""
name = "unregistered-reviewer"
model_provider = "custom"
model = "grok-4.7"
model_reasoning_effort = "high"
""", encoding="utf-8")
        result_pass = check_codex_agents(self.repo_dir, self.global_config_path)
        self.assertTrue(result_pass.success, f"未注册角色配置 high 预期通过但失败: {result_pass.errors}")

    def test_unregistered_agent_empty_or_comment_only_ignored(self) -> None:
        """验证 agents 目录下纯注释或空文件（如 grok-complex.toml）被安全忽略。"""
        (self.agents_dir / "grok-complex.toml").write_text("# 纯注释已废弃\n# 无有效配置\n", encoding="utf-8")
        (self.agents_dir / "empty-role.toml").write_text("", encoding="utf-8")
        result = check_codex_agents(self.repo_dir, self.global_config_path)
        self.assertTrue(result.success, f"纯注释与空配置文件应被忽略: {result.errors}")

if __name__ == "__main__":
    unittest.main()
