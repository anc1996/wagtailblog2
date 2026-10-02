# Codex 子代理配置与预检

## 配置契约

- Sol 与项目默认子代理模型统一为 `gpt-6.1-sol`；Gemini、Grok、Claude 保持各自角色文件中的真实模型。
- 本项目执行 **high-only**：项目默认、已注册/未注册角色的 `model_reasoning_effort`、目录默认等级均必须是 `high`，目录仅公布 `high` 选项。预检覆盖有效角色文件并忽略纯注释废弃文件。Codex 官方 Subagents 文档规定，自定义角色文件中的等级优先于显式 spawn 值与 `[agents]` 默认；调用方仍应显式传 `high`。这属于本地配置与预检策略，不代表厂商只支持一种等级，也不能限制任意外部 CLI 覆盖。
- `.codex/config.toml` 的 `model_catalog_json` 加载同目录 `model-catalog.json`，先注册自定义模型，再执行原生子代理路由。
- 提供方 `custom` 继承宿主 `~/.codex/config.toml`；凭据继续由宿主认证配置管理，本仓库不存储 API Key、Cookie 或 Token。
- 目录按本机 Codex CLI `0.157.1` 验证。升级后必须重新检查 schema；旧目录可能因缺少 `support_verbosity` 等字段无法读取。每个模型还须提供非空基础指令或指令模板，本目录使用项目自有、中性的 Codex 协作指令。
- 当前 `codex debug models` 确认四个目录别名已载入且仅声明 `high`；输出未包含 `supports_parallel_tool_calls`，因此目录检查不作为该能力已被运行时采用的证据。
- 目录中的 128 Ki token 上限与 100000 token 压缩阈值是本项目的保守运行预算，不代表厂商公布的模型上限。文本输入、普通 Responses 与 V1 多代理为当前兼容基线，不启用尚未验证的图片、Responses Lite 或厂商内置搜索接口。

## 原生调用规则

正常调用只选择当前工具暴露的 `agent_type`，并设置 `fork_context=false`；实际模型、推理等级、角色职责由角色 TOML 加载。默认模型必须已进入当前会话目录，角色文件尚未加载时也不能绕过前置校验。

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

真实冒烟验证必须在新会话执行：创建 Gemini 与 Grok 子代理，要求一次无副作用的只读任务和一次公开页面的 Fetch 请求，确认代理完成，并从对应 rollout 的 `turn_context.model` 与 `effort` 核对实际模型及 `high`。API 返回 200、代理自报身份或成功创建 agent ID 均不能替代此验证。

Windows 只读沙箱未必继承宿主 Python 的 PATH。连通性验收优先使用 PowerShell 内置 `Write-Output READONLY_TOOL_OK`；项目 Python 测试按专属技能走 WSL2/Conda 的明确环境，不把可执行程序缺失误判为模型路由故障。

本次修复仅涉及本地 Codex 配置与工具，不需要博客数据库迁移、生产服务重启或重新授权 CLI Proxy API。

## Fetch MCP 独立依赖

本机原安装目录报告 `@yawlabs/fetch-mcp 0.3.6`，实际源码将外部 Undici 8 的 `Agent` 与 Node 24 内置 Undici 7 的全局 `fetch` 混用。真实 MCP 公网 GET 返回 `fetch failed`；底层 A/B 测试明确出现 `UND_ERR_INVALID_ARG / invalid onRequestStart method`。换用同一依赖的 `undici.fetch` 后成功，与模型能力、响应头配置无关。

项目改用上游已修复的 `@yawlabs/fetch-mcp 0.8.2`，通过 `tools/fetch-mcp/package.json` 和 `package-lock.json` 固定整个依赖树；不手改宿主 `node_modules`，不增加全局 fetch 补丁，不关闭 TLS 或 SSRF。保留原 MCP adapter，仅更换项目 Fetch 的包入口。宿主全局 Fetch 配置未改，其他项目仍需单独接入；本项目优先使用项目配置。

安装或重建（需要 Node >=22.19.0，本机验收为 24.17.0；路径不同使用实际 Node/npm）：

```powershell
& "D:\softwarepro\nodejs\npm.cmd" ci --prefix tools/fetch-mcp --ignore-scripts --no-audit --no-fund
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

本机 `~/.codex/config.toml` 原默认子代理是 `gpt-6-astra/medium`，已针对性改为 `gpt-6.1-sol/high` 并引用本仓库同一模型目录；主会话保持 `gemini-3.8-flash/high`，提供方及认证未改。仓库外会话也依赖该目录的绝对路径，移动仓库须同步调整；需要回退时仅恢复上述三项，不覆盖整个全局配置。

Codex 的 `reasoning.effort=high` 是请求体配置，不通过自定义 HTTP 响应头开启推理；`default_reasoning_summary="none"` 仅关闭摘要展示，不等于关闭推理。终端与 MCP 工具是宿主能力，模型经 Responses 工具调用协议使用这些能力，不是模型直接获得主机权限。

rollout 的模型与 `effort=high` 只能证明 Codex 侧选择；要证明 CLI Proxy API 没有改写/降级，还需其实际部署版本、别名目标、能力配置及脱敏的转发请求证据。本次无密码参数的 SSH 只读连接未通过认证，未修改远程服务。不要将此误报为代理版本已核实或 high 已完整透传；后续仅采集版本、别名及 `reasoning.effort`/`thinkingLevel` 白名单字段，禁止导出认证头、Cookie、Token 或完整请求正文。

### 2026-09-30 实测

- 新 Codex 会话中 Sol 与原生 Gemini 子代理均记录正确模型/`high`，并完成公网 Fetch 200、`text/html` 响应头、`Example Domain` 正文及 PowerShell 标记回传。
- Grok 两次实调失败：`model_not_found / unknown provider for model grok-4.7`，rollout 仍正确记录 `grok-4.7/high`，说明失败发生在代理模型路由层，尚未执行工具。
- 以 Codex User-Agent 调用当前 `custom` 提供方 `/v1/models` 返回 200：存在 Sol/Gemini 别名，但没有 `grok-4.7`，也没有任何 Grok 模型。初次 Python 默认 User-Agent 探测为 403，不能据此判定模型请求授权失效。
- Grok 角色保留原模型与 high；不擅自换成其他模型、不伪造别名。恢复需代理管理员先接入可用的 Grok 上游，建立明确的别名映射并核验 Responses 工具调用和高推理透传，再重做原生工具冒烟。在此之前不能宣称三模型全部通过，独立审查临时由可用的 Sol/high 完成。
