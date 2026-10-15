# Codex 子代理配置与预检

## 配置契约

- 2026-10-08：在已有 Astra 模型目录项基础上，增加 `astra`、`luna` 具名角色及项目 `gpt-6-luna` 模型条目；默认子代理仍为 Sol，原五个角色不变，全局配置未改。Astra/Luna 均通过 `custom` 请求 `high`；这些别名和能力字段是本地配置，不是官网型号或上游能力声明。角色加载后可按 `agent_type` 选择，也可显式指定已加载的完整 `model` 名称；不要将工具说明中的模型展示列表当成完整拒绝名单。

### Astra / Luna 配置入口

项目 `.codex/config.toml` 注册角色并引用相对于该文件的角色配置：

```toml
[agents.luna]
description = "按明确授权承担独立任务的 Luna 子代理"
config_file = "agents/luna.toml"
```

对应 `.codex/agents/luna.toml` 使用角色配置字段，而不是工具调用参数名：

```toml
name = "luna"
description = "按明确授权承担独立任务的 Luna 子代理"
model_provider = "custom"
model = "gpt-6-luna"
model_reasoning_effort = "high"
developer_instructions = "仅执行明确分派的任务，遵守项目安全和验证边界。"
```

Astra 使用相同结构，角色名为 `astra`，模型为 `gpt-6-astra`。`model_reasoning_effort` 是 TOML 配置字段；V1 `spawn_agent` 的显式覆盖参数才是 `reasoning_effort`。模型另须进入本项目 `model-catalog.json`；仅加入全局桌面菜单不足以证明本项目子代理已加载。

2026-10-08 尝试核对官方 Subagents、Configuration reference / schema 页面时，HTTP 客户端与隔离浏览器均返回 403，未取得官方正文；不能将此配置记录说成已逐条通过官网核对。本次以已安装 CLI 的解析、项目预检和运行时证据为验证依据。修改后应在重新加载项目配置的会话中验证具名角色，旧聊天成功按 `model` 调用不等于新 `agent_type` 已热加载。

回退仅移除本次新增的两个角色注册、两个角色文件、Luna 项目目录项及 Luna 白名单项，并恢复对应说明；保留原 Astra 模型条目、Sol 默认设置及所有他人修改，不覆盖整个配置文件。

### 既有配置约束

- Sol 与项目默认子代理模型统一为 `gpt-6.1-sol`；Gemini、Grok、Claude 保持各自角色文件中的真实模型。主会话模型由宿主配置决定，不能从本文件反推。
- 本项目执行 **high-only**：项目默认、已注册角色的 `model_reasoning_effort` 和目录默认等级均为 `high`。这是本地一致性策略，不代表提供商只支持一种等级，也不能限制外部 CLI 覆盖；预检只检查当前工作区实际存在的有效角色文件。
- `.codex/config.toml` 的 `model_catalog_json` 加载同目录 `model-catalog.json`，先注册自定义模型，再执行原生子代理路由。
- 提供方 `custom` 继承宿主 `~/.codex/config.toml`；凭据继续由宿主认证配置管理，本仓库不存储 API Key、Cookie 或 Token。
- 目录按本机 Codex CLI `0.157.1` 验证。升级后必须重新检查 schema；旧目录可能因缺少 `support_verbosity` 等字段无法读取。每个模型还须提供非空基础指令或指令模板，本目录使用项目自有、中性的 Codex 协作指令。
- 最近一次目录检查确认五个目录别名已载入且仅声明 `high`；这只是历史/配置证据，不能代替新会话的真实推理或工具调用验证。
- 目录中的 128 Ki token 上限与 100000 token 压缩阈值是本项目的保守运行预算，不代表厂商公布的模型上限。文本输入、普通 Responses 与 V1 多代理为当前兼容基线，不启用尚未验证的图片、Responses Lite 或厂商内置搜索接口。

## 原生调用规则

正常调用只选择当前工具暴露的 `agent_type`，并设置 `fork_context=false`；实际模型、推理等级、角色职责由角色 TOML 和当前运行时加载结果共同决定。默认模型必须已进入当前会话目录，角色文件尚未加载时也不能绕过前置校验。

必须显式传 `model` 时，使用目录中的完整模型名称。错误提示通常只列前五个可见名称，不代表整个目录只有五个模型。旧会话不会自动保证加载新目录，修改后应从本仓库开启新会话再验证。

紧凑派发独立任务，明确文件归属并保留他人改动；同一问题优先复用实例。只在真正阻塞时等待，结束后关闭代理。按照现有门禁由 Gemini 实现与测试、Grok 审查，Sol 负责规划；Claude 仅用于已授权的疑难升级。

## 验证步骤

若启动报 `string values must be quoted`，先检查 `.codex/config.toml` 顶层目录路径是否使用 ASCII 双引号：

```toml
model_catalog_json = "model-catalog.json"
```

这是本地 TOML 解析阶段失败，尚未进入模型请求阶段；不应先排查网络或重新授权。修正后重新启动 Codex 会话，并执行下方预检。若仍显示同样错误，对照新日志中的实际文件路径与当前第 4 行，排除未保存文件或启动了另一个工作区。

离线配置检查（不联网，不读取认证文件，不产生推理费用）：

```powershell
python tools/check_codex_agents.py
```

检查当前 CLI 实际加载的完整目录：

```powershell
python tools/check_codex_agents.py --runtime --codex "C:\Users\haryk\.codemoss\dependencies\codex-sdk\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe"
```

路径不同可通过 `--codex` 指定实际二进制；不要将密码或 API Key 放入命令。Windows 编辑与 WSL2 测试共享仓库，但提供方及认证配置仍分别来自各自用户目录。

真实冒烟验证应在新会话按需执行：对需要使用的代理分别安排无副作用的只读任务，必要时再安排公开页面 Fetch 请求，并从对应 rollout 的 `turn_context.model` 与 `effort` 核对实际模型及 `high`。若某个提供商当前不可用，应记录为路由失败并降级到已验证角色，不伪造成功；API 返回 200、代理自报身份或成功创建 agent ID 均不能替代此验证。

Windows 只读沙箱未必继承宿主 Python 的 PATH。连通性验收优先使用 PowerShell 内置 `Write-Output READONLY_TOOL_OK`；项目 Python 测试按专属技能走 WSL2/Conda 的明确环境，不把可执行程序缺失误判为模型路由故障。

本次修复仅涉及本地 Codex 配置与工具，不需要博客数据库迁移、生产服务重启或重新授权 CLI Proxy API。

## Fetch MCP 独立依赖

本机原安装目录报告 `@yawlabs/fetch-mcp 0.3.6`，实际源码将外部 Undici 8 的 `Agent` 与 Node 24 内置 Undici 7 的全局 `fetch` 混用。真实 MCP 公网 GET 返回 `fetch failed`；底层 A/B 测试明确出现 `UND_ERR_INVALID_ARG / invalid onRequestStart method`。换用同一依赖的 `undici.fetch` 后成功，与模型能力、响应头配置无关。

项目改用上游已修复的 `@yawlabs/fetch-mcp 0.8.2`，通过 `tools/fetch-mcp/package.json` 和 `package-lock.json` 固定整个依赖树；不手改宿主 `node_modules`，不增加全局 fetch 补丁，不关闭 TLS 或 SSRF。保留原 MCP adapter，仅更换项目 Fetch 的包入口。宿主全局 Fetch 配置未改，其他项目仍需单独接入；本项目优先使用项目配置。

安装或重建（需要 Node >=22.19.0，本机验收为 24.17.0；路径不同使用实际 Node/npm）：

```powershell
& "D:\softwarepro\nodejs\npm.cmd" ci nt文件的-prefix tools/fetch-mcp --ignore-scripts --no-audit --no-fund
& "D:\softwarepro\nodejs\node.exe" --test tools/fetch-mcp/test-fetch.mjs
```

验证包含真实 stdio MCP 初始化、DNS 固定公网 GET 的状态码/响应头/正文、HEAD 无正文、HTML 提取及环回地址阻断。测试仅访问公开的 `example.com`，不读取任何登录 Cookie。覆盖完整宿主 adapter 链：

```powershell
$env:FETCH_MCP_TEST_ADAPTER = "C:\Users\haryk\.codemoss\dependencies\mcp-adapter.cjs"
try {
    & "D:\softwarepro\nodejs\node.exe" --test tools/fetch-mcp/test-fetch.mjs
} finally {
    Remove-Item Env:FETCH_MCP_TEST_ADAPTER -ErrorAction SilentlyContinue
}
```

`FETCH_MCP_TEST_ENTRY` 可临时指定旧包入口复现红灯；正常运行应清除此变量。依赖目录受 `.gitignore` 保护，锁文件必须保留。项目 TOML 当前为 Windows 本机绝对路径；迁移到另一主机需同步修改 Node、adapter 与 Fetch 包入口。

**生效与回滚**：修改配置后从本仓库启动新 Codex 会话，旧会话的 MCP 进程不会热更新。回滚只需将项目 `[mcp_servers.fetch].args` 的末项恢复为原宿主包入口；不用删除依赖或重启博客服务。旧包仍存在已知 Fetch 故障，回滚是配置回退而非故障修复。

## 本机默认与代理核验边界

当前审计记录显示，宿主默认子代理已针对性改为 `gpt-6.1-sol/high` 并引用本仓库同一模型目录，主会话保持 `gemini-3.8-flash/high`；这些是本机状态，不是所有会话或提供商的保证。仓库外会话也可能依赖该目录的绝对路径，移动仓库须重新验证；回退时只恢复明确改动项，不覆盖整个全局配置。

`fork_context=false` 不会把主会话历史自动复制给子代理；代理之间也没有可假定的共享长期记忆或缓存。共享事实必须通过紧凑提示、工作区文件、工具结果或显式消息传递。固定提示前缀可能改善缓存命中，但不是跨模型知识共享机制。

Codex 的 `reasoning.effort=high` 是请求体配置，不通过自定义 HTTP 响应头开启推理；`default_reasoning_summary="none"` 仅关闭摘要展示，不等于关闭推理。终端与 MCP 工具是宿主能力，模型经 Responses 工具调用协议使用这些能力，不是模型直接获得主机权限。

rollout 的模型与 `effort=high` 只能证明 Codex 侧选择；要证明 CLI Proxy API 没有改写/降级，还需其实际部署版本、别名目标、能力配置及脱敏的转发请求证据。本次无密码参数的 SSH 只读连接未通过认证，未修改远程服务。不要将此误报为代理版本已核实或 high 已完整透传；后续仅采集版本、别名及 `reasoning.effort`/`thinkingLevel` 白名单字段，禁止导出认证头、Cookie、Token 或完整请求正文。

### 2026-09-30 实测

- 新 Codex 会话中 Sol 与原生 Gemini 子代理均记录正确模型/`high`，并完成公网 Fetch 200、`text/html` 响应头、`Example Domain` 正文及 PowerShell 标记回传。
- Grok 两次实调失败：`model_not_found / unknown provider for model grok-4.7`，rollout 仍正确记录 `grok-4.7/high`，说明失败发生在代理模型路由层，尚未执行工具。
- 以 Codex User-Agent 调用当前 `custom` 提供方 `/v1/models` 返回 200：存在 Sol/Gemini 别名，但没有 `grok-4.7`，也没有任何 Grok 模型。初次 Python 默认 User-Agent 探测为 403，不能据此判定模型请求授权失效。
- Grok 角色保留原模型与 high；不擅自换成其他模型、不伪造别名。恢复需代理管理员先接入可用的 Grok 上游，建立明确的别名映射并核验 Responses 工具调用和高推理透传，再重做原生工具冒烟。在此之前不能宣称三模型全部通过，独立审查临时由可用的 Sol/high 完成。
