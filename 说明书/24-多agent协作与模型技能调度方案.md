# 多 agent 协作与模型技能调度方案

## 1. 文档定位

本方案定义新对话中的主 agent、专业子 agent、快捷指令、模型推理强度、Skill 与 MCP 路由。
它只约束协作方式，不改变 Django/Wagtail 业务架构、数据结构、服务配置或生产授权边界。

## 2. 背景与现状证据

- 项目已有 `AGENTS.md`，规定了 Wagtail、WSL2、Git、数据保护、测试、发布和回滚门禁。
- 当前可用能力以每次会话实际暴露的 Skill/MCP 为准；项目记录了 Django/Wagtail、Playwright、UI/UX、Karpathy、文档、GitHub 和 SSH 运维等能力。
- 子 agent 需要按职责隔离，避免多个 agent 同时修改同一文件、重复调用工具或绕过生产授权。

## 3. 目标与非目标

### 3.1 目标

1. 用户只需描述业务目标，主 agent 自动识别任务类型和风险并分派专业 agent。
2. 每个 agent 有明确职责、模型档位、Skill 和工具边界。
3. 主 agent 统一方案、集成、最终测试、授权门禁和交付报告。
4. 新对话可以通过本方案恢复相同的协作规则。

### 3.2 非目标

- 不把 `/` 指令伪装成项目运行时或操作系统命令；它们是主 agent 识别的对话协议。
- 不为每个小任务强行启动全部 agent。
- 不因 agent 拥有工具能力就自动获得生产数据、迁移、发布、删除或重启授权。

## 4. 角色与职责

| 角色 | 别名 | 主要职责 | 主要输出 |
|---|---|---|---|
| 产品经理/需求分析师 | `pm` | 目标、场景、验收标准、非目标和待确认事项 | 需求摘要、验收清单 |
| 架构规划师 | `arch` | 现状核查、数据流、接口、兼容性、异常和回滚设计 | 技术方案、影响清单 |
| 后端开发工程师 | `backend` | Django/Wagtail、模型、API、任务和服务端测试 | 代码、迁移检查、测试 |
| 前端开发工程师 | `frontend` | 模板、CSS、JavaScript、响应式和可访问性 | UI 代码、浏览器验收证据 |
| 测试与质量工程师 | `qa` | 单元、集成、回归、Playwright 和配置检查 | 测试结果、缺陷清单 |
| 代码审查与安全工程师 | `review` | diff、权限、数据保护、并发、敏感信息和回滚风险 | 按严重性排序的审查发现 |
| DevOps/发布运维工程师 | `ops` | WSL2、Git、CI、systemd、uWSGI、Nginx、Celery、Filebeat 和健康检查 | 发布步骤、服务证据 |
| 数据与集成工程师 | `data` | MongoDB、MySQL、Redis、Elasticsearch、MinIO、队列和跨服务一致性 | 数据影响、索引/补偿方案 |

主 agent 是技术负责人和交付编排者，负责拆解任务、分派 agent、处理冲突、集成结果、执行最终门禁和向用户交付；子 agent 不单独决定生产操作。

## 5. 对话快捷指令

快捷指令是约定格式，等价的自然语言也有效。

| 指令 | 行为 |
|---|---|
| `/do <任务>` | 自动调研、分派、实现、测试、审查和汇报 |
| `/plan <任务>` | 只调研和写方案，不修改业务代码 |
| `/implement` | 执行已确认的方案 |
| `/assign <别名> <子任务>` | 指定一个角色执行子任务，由主 agent 集成 |
| `/review <范围>` | 进入代码审查模式，优先报告问题 |
| `/test <范围>` | 执行测试和验收，不以修改测试掩盖缺陷 |
| `/status` | 汇报任务、agent、测试和阻塞状态 |
| `/prepare-release` | 准备提交、推送和发布步骤，不执行生产发布 |
| `/confirm-production <已说明操作>` | 明确授权已说明的生产操作 |
| `/stop` | 停止当前未完成的协作任务 |

未指定角色时，主 agent 根据风险自动分派。`/assign` 不能绕过方案、测试、审查或生产门禁。

## 6. 风险与模型调度

模型与推理强度独立选择，实际可用模型不足时使用最接近档位并记录差异。

| 风险 | 典型任务 | 默认模型 |
|---|---|---|
| R0 | 只读检索、文档整理、格式检查 | `gpt-5.6-luna` 低/中 |
| R1 | 单文件小修复、简单测试、常规浏览器采集 | `gpt-5.6-luna` 中；必要时 `terra` 中 |
| R2 | 跨文件功能、Django/Wagtail 实现、集成测试 | `gpt-5.6-terra` 中/高 |
| R3 | 迁移、搜索链路、并发、安全、生产发布或回滚 | `gpt-5.6-sol` 高/xhigh，并安排独立复核 |

角色默认档位如下：

- `pm`：`luna` 中；需求歧义或生产影响升级为 `terra` 高。
- `arch`：`terra` 高；迁移、搜索、并发、跨服务契约或不可逆操作使用 `sol` 高/xhigh。
- `backend`：`terra` 中；数据一致性、权限、迁移和队列补偿使用 `sol` 高。
- `frontend`：小改动 `luna` 中，普通功能 `terra` 中；跨页面交互、性能或可访问性风险 `terra` 高。
- `qa`：定向测试 `luna` 中；集成、浏览器回归和发布验收 `terra` 高。
- `review`：`terra` 高；安全、敏感数据、并发和生产回滚风险 `sol` 高/xhigh。
- `ops`：只读状态采集 `luna` 低/中；常规发布 `terra` 高；生产变更和回滚 `sol` 高。
- `data`：`terra` 高；数据库、索引切换、正文一致性和生产数据使用 `sol` xhigh。

推荐流水线是 `luna` 收集事实 → `terra` 实现 → 独立 `terra` 审查；R3 任务使用 `sol` 设计和复核、`terra` 执行。

## 7. Skill 与 MCP 路由

只调用当前会话实际暴露且与任务相关的能力。工具不可用时记录原因并回退到允许的本地命令；不得声称未实际调用的 MCP 结果。

| 角色 | Skill | MCP/工具路由 |
|---|---|---|
| `pm` | `karpathy-guidelines`；需要检索项目文档时使用 `mineru-document-explorer` | 必要时使用只读 `fetch`；不发送凭据、正文或个人数据 |
| `arch` | `karpathy-guidelines`、`django-wagtail-development` | Django/Wagtail/Celery 版本 API 优先 `context7`；官方 URL 使用 `fetch` |
| `backend` | `django-wagtail-development`、`karpathy-guidelines` | `context7` 核对版本 API；数据库只读核验按项目允许的工具执行 |
| `frontend` | `ui-ux-pro-max`、`playwright`、`karpathy-guidelines` | 页面交互使用 Playwright；登录浏览器自动化仅在 `browser-skill` 可用且获授权时使用；位图素材需求才使用 `imagegen` |
| `qa` | `playwright`、`django-wagtail-development`、`karpathy-guidelines` | Playwright MCP/CLI；产物统一写入 `output/playwright/` |
| `review` | `karpathy-guidelines`、`django-wagtail-development`；涉及 UI 时使用 `ui-ux-pro-max` | GitHub 仓库、PR 和 CI 状态优先 GitHub MCP；不把审查工具当作生产授权 |
| `ops` | 适用的发布/SSH 运维插件 Skill（以当前会话暴露为准） | Git 写操作统一 WSL2；生产状态可用 SSH 运维工具或既有 SSH，禁止并行两个远程写入口 |
| `data` | `django-wagtail-development`、`karpathy-guidelines`；需要读文档时使用 `mineru-document-explorer` | 测试数据库只读查询优先 `google-toolbox`（当前会话未暴露时不得声称已调用）；版本 API 使用 `context7` |

`openai-docs` 只用于 Codex/OpenAI 产品和 API 问题，`pdf` 只用于 PDF 读取、生成或版式验收，`skill-creator`、`skill-installer` 和 `plugin-creator` 只在用户明确要求创建、安装或维护 Skill/插件时启用。

## 8. 统一协作协议

1. 主 agent 先读取本文件、`AGENTS.md`、Git 状态和相关代码，判断 R0-R3。
2. R2/R3 或用户要求实现时，先在 `说明书/` 建立或更新方案，再分派实现 agent。
3. 子 agent 输出必须包含：检查过的文件/工具、事实依据、修改建议或实际修改、测试结果、未解决风险。
4. 同一文件不得由多个 agent 同时写入；主 agent 负责合并和冲突处理。
5. 每个可独立验证的子批次都在方案“实施记录”中记录状态、文件、测试、影响和回滚点。
6. 生产、敏感数据、迁移、发布、服务重启和不可逆操作必须由主 agent 说明影响并获得用户明确授权。
7. 子 agent 的结论不能替代本地测试、配置核查、备份、服务健康检查和人工授权。

## 9. 模型/推理强度建议

- 模型角色：主 agent 使用 `terra` 中推理进行日常编排；R3 最终判断升级 `sol` 高/xhigh。
- 选择理由：事实收集和重复工作使用 `luna` 控制成本；局部开发使用 `terra`；高风险设计、复核和发布使用 `sol`。
- 升级条件：生产数据、不可逆迁移、跨服务契约、并发一致性、安全边界、回滚失败风险或证据冲突。
- 验证门禁：模型选择不能替代 WSL2 测试、Git diff 检查、浏览器验收、备份、生产授权和回滚方案。

## 10. 实施记录

### 2026-08-23：已完成协作协议草案

- 状态：文档规则已写入本文件和 `AGENTS.md`，未修改业务代码、数据库、服务或生产环境。
- 实际修改文件：`说明书/24-多agent协作与模型技能调度方案.md`、`AGENTS.md`。
- 测试：执行文档 diff 检查；未运行 Django 或生产服务测试，因为本批次不涉及代码和服务。
- 回滚点：删除本方案文件并回退 `AGENTS.md` 追加章节即可恢复原协作规则。
- 残余风险：不同会话暴露的 Skill/MCP 可能不同，主 agent 必须以当前会话实际工具声明为准。
- 实际模型使用：当前会话由主 agent 完成文档调研与编写；未调用外部模型或生产工具。
