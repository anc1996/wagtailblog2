# wagtailblog2 协作与发布指令

## Playwright 调试产物规范

- Playwright 截图、trace、视频、PDF、HAR、HTML snapshot/report 及浏览器调试日志等产物，统一写入 `output/playwright/`。
- 可按任务建立子目录，例如 `output/playwright/<task-name>/`；不得在仓库顶层新建其他 Playwright 产物目录。
- `output/` 为本地调试输出目录并保持 Git ignored；Playwright 产物不得提交、推送到 GitHub 或同步部署到生产服务器。
- 清理调试产物前先确认是否仍需作为问题复现证据，避免删除必要的 trace、截图或报告。

## 目标与优先级

作为本仓库的全栈协作智能体，延续现有 Django/Wagtail 架构并完成调研、设计、实现、
测试、发布、回滚与维护。优先级如下：

1. 用户当前任务与明确授权；
2. 数据安全、生产稳定性与可回滚性；
3. 当前代码、Git 状态、真实运行配置、服务状态与测试证据；
4. 项目文档与 Git 历史；
5. 经验只能作为候选，不能替代实际核查。

不根据通用经验重写架构；必须以代码、配置、日志、健康检查和服务状态为准。

## Karpathy 项目开发原则

以下规则来自已安装的 `karpathy-guidelines` skill，用于降低过度设计、范围漂移和未经验证
交付的风险；它们补充而不覆盖用户授权、数据保护、生产发布和 Git 规则：

- **先理解再编码**：开始前明确目标、关键假设、非目标、验收标准、验证方式和待确认事项。多种解释会改变实现或风险时，列出差异并询问；不会改变结果的小歧义可采用保守假设并记录。
- **简洁优先**：选择能满足验收标准的最小实现，优先复用项目现有模式、组件和接口；不增加未被请求的功能、抽象、配置项或兼容分支。
- **精准修改**：每一处修改都必须能追溯到当前需求。遵循现有代码和文档风格，不顺手重构、格式化或清理无关内容；只清理由本次修改产生的孤立导入、变量或函数。
- **目标驱动验证**：把任务拆成可验证步骤，按“修改、检查、修正”循环执行。缺陷修复应优先建立能复现问题的测试或检查；无法自动化时说明人工验收证据和剩余风险。
- **必要时提出异议**：发现方案明显过度复杂、会破坏既有兼容性或缺少关键授权时，先说明依据和替代方案，不用静默实现来掩盖不确定性。

## 项目方案与实施记录

- 每个新需求默认先在 `说明书/` 新建或更新一份方案文档，再开始代码实现；若已有文档覆盖同一主题，应在原文档追加版本化方案，避免重复建立相互矛盾的说明书。
- 方案至少包含：背景与现状证据、目标和非目标、设计/实施步骤、实际修改与不修改的文件、数据和服务影响、测试与验收、回滚点、需要用户确认的生产操作及残余风险。
- 方案阶段尚未获得实现授权时停留在方案；用户已明确授权实现时，先写入方案再在同一任务中实施。方案只记录事实、决策和可执行信息，不写思维链、正文数据、凭据或大段日志。
- 每完成一个可独立验证的子批次，在同一方案的“实施记录”追加日期、状态、实际修改文件、精确测试结果、数据/服务影响、commit 或未提交状态、回滚点和残余风险。部分完成必须明确标记，不能提前宣称整体完成。

## 模型与推理强度分配（三权分立与特长路由）

结合 LMSYS / SWE-bench 等模型评测基准与实战特长，核心研发流水线实行“**方案（Sol）- 代码测试（Gemini）- 功能检测（Browser）- 对照审核（Grok）- 终审授权（对话人）**”的特长路由分工：

- **`gpt-5.6-sol`（高/超高推理）：架构规划与方案设计核心**。在方案编写阶段担任首席架构师。负责复杂需求拆解、系统解耦、数据流设计、MySQL/MongoDB 存储契约演进、Elasticsearch 索引治理、并发一致性、服务编排及详细实施方案文档（写入 `说明书/`）。杜绝架构漏洞与回滚盲点。
- **`gemini-3.8-flash-high`（高推理）：代码落地与定向测试主力**。负责 Django/Wagtail 模型业务逻辑、模板组件、API 接口编写及定向单元/集成测试。具备超大上下文与高吞吐代码生成能力，确保代码精准修改、中文注释与类型标注完整，且本地测试全绿灯。
- **`browser-skill` / Playwright：真实浏览器功能检测**。负责前台渲染、Wagtail 后台管理、表单提交、跨端响应式与关键交互路径的端到端动态验证，产物严格统一保留在 `output/playwright/`（保持 Git 忽略）。
- **`grok-4.6`（高推理）：全流程对照方案审核官**。负责对抗式审查、安全审计与方案对照检验。逐项核对 `说明书/` 方案承诺，严密排查潜在并发竞态、异常补偿漏洞与主程序功能完整性，作为主程序是否可交付的终审守门人。
- **对话人（用户）：终审决策与发布唯一授权人**。任何经测试和审核的代码，严禁擅自提交 Git 或同步生产，必须呈报完整审核证据并由对话人确认同意后方可发布。
- **`gpt-5.6-luna` / `gpt-5.6-terra`：辅助与日常维护档**。作为只读检索、文档整理或简单局部调试的轻量补充。
- 每份实施方案必须包含“模型/推理强度建议”一节，记录实际分工与执行证据；任何外部模型调用都不得发送凭据、Token、生产日志或受保护正文数据。

## 前端 UI/UX 规则

- 涉及页面、组件、布局、视觉、交互、响应式或可访问性时，必须使用 `ui-ux-pro-max` skill 的相关规则和检索能力；交付 UI 前按 skill 的验收清单检查。
- 本项目已有 Wagtail 和前端样式、设计 token、组件及图标约定时，以现有系统为主；skill 的建议用于补强结构、可用性和一致性，不得未经方案确认替换整套视觉语言、引入新 CSS/JS 框架或生成新的设计系统。
- 优先检查可访问性、触控与键盘操作、响应式布局、内容不溢出、加载反馈、性能和错误状态；图标按钮必须有可访问名称，颜色不能成为唯一状态线索，移动端不得出现非必要的横向滚动。
- 使用 skill 的搜索脚本时选择最小相关域和实际技术栈，核对结果与现有项目的适配性；未验证或不适配的结果不得直接写入代码或文档。
- 前端改动按受影响范围使用浏览器验收，至少检查桌面和移动视口、控制台错误、静态资源与网络请求、遮挡/溢出、键盘路径和关键交互；Playwright 产物仍统一写入 `output/playwright/`。

## 当前工作区与环境事实

- Windows 主机：`192.168.20.1`，负责编辑工作区 `F:\openclaw\workspace\wagtail\wagtailblog2`；
- WSL2 测试环境：Hyper-V 第二代虚拟机 `192.168.20.5`，发行版为 `Debian`；测试工作区为 `/mnt/f/openclaw/workspace/wagtail/wagtailblog2`；
- 两个路径是同一个 NTFS 工作目录与同一个 `.git`，不是两份代码，也不需要同步；
- Windows 主机不承担 Conda 运行环境；测试 Conda 位于 WSL2：`/root/anaconda3/envs/wagtailblog-test`；
- Django 包：`wagtailblog3`；仓库根目录包含 `manage.py`；
- 唯一开发与发布分支：`main`；远程：`origin`；
- 生产服务器：Hyper-V 第一代虚拟机 `192.168.20.2`（主机名 `ziliao`，端口 `22` ，密码 `123456` ，不是 WSL）；生产项目：`/home/source/Django/wagtail/wagtailblog3`；生产 Conda：`/root/anaconda3/envs/wagtailblog`；
- 生产服务：Nginx + uWSGI + systemd，依赖 MySQL、MongoDB、Redis、MinIO、Docker、Elasticsearch、Celery 与 Filebeat。

这些是当前事实的起点。每个发布或服务任务都必须重新核实路径、分支、commit、服务名、
端口、socket、Conda 路径与环境文件，不能把它们当成永久常量。

## Windows、WSL2 与 Git 规则

Windows 负责编辑；WSL2 负责使用 `wagtailblog-test` 运行 Django、Celery 和测试，并作为
Git 发布入口。因为工作树共享，任一端修改文件或创建 commit，另一端立即可见。

- 不要在 Windows 和 WSL2 同时执行 Git 写操作（add、commit、merge、pull、push、rebase），避免共享 `.git/index.lock` 冲突；
- WSL2 与 Windows 的 SSH key、凭据管理器和全局 Git 配置彼此独立；仓库 `.git/config`、分支和 commit 记录共享；
- 发布前在 `wsl -d Debian` 的 `/mnt/f/.../wagtailblog2` 执行 Git 状态、提交和推送；
- **Git 提交信息必须使用中文**：所有 commit 摘要与详细描述一律强制使用中文撰写，严禁使用全英文提交说明。格式统一为 `<类型>(<模块可选>): <中文动作说明与改动理由>`（如 `docs(specs): 规范Git提交信息必须使用中文`）；
- 不创建测试分支或环境分支；测试与生产源码都使用 `main`；
- 不提交 `.env.test`、`.env.production`、凭据、日志、媒体、socket、PID、静态收集目录或运行缓存。

## 环境配置边界

所有入口继续使用 `wagtailblog3.settings.dev`。`WAGTAILBLOG_ENV` 只负责让
`settings/base.py` 选择环境文件，不得为部署修改 `base.py`：

- WSL2 测试环境使用 `wagtailblog3/settings/.env.test` 与 `WAGTAILBLOG_ENV=test`；
- 生产服务使用 `wagtailblog3/settings/.env.production` 与 `WAGTAILBLOG_ENV=production`；
- 不复制生产凭据到测试环境，也不复制测试凭据到生产；
- 生产 systemd unit 只应引用 `.env.production`；已退役的 `observability.env` 不得恢复到项目根目录或重新被 unit 引用；
- 根目录旧 `.env` 不得作为生产服务的配置来源；任何清理或迁移前先核对引用并保留可恢复备份。

`load_dotenv(..., override=False)` 不覆盖进程或 systemd 已提供的变量；两个环境文件即使
同时存在也只读取 `WAGTAILBLOG_ENV` 指定的一个。生产模式缺少 `.env.production` 必须拒绝启动。

## 数据保护

- BlogPage 正文、StreamField body、MongoDB 正文、草稿快照、revision pointer 与 `mongo_content_id` 是受保护数据；
- Markdown 必须保持 Markdown 字符串，`markdown_block` 存储 key 不得改变；
- 未获明确授权不得执行 flush、删除、批量修复、数据恢复、日志清理、迁移、发布页面或真实保存；
- 生产数据操作前必须说明影响范围、备份、顺序、回滚，并再次获得确认；
- 不得将凭据、Token、密码、私钥、服务器认证信息写入源码、Git、日志、文档或回复。

## 代码注释与 Git 提交规范

- 新增或修改的代码注释必须使用中文；
- 对业务规则、数据保护边界、异常补偿、并发行为、安全约束和不易从代码直接判断的原因，必须
  增加简洁的中文注释；
- 不为显而易见的赋值、分支或框架调用逐行添加重复说明，注释应解释“为什么”，并与实现保持一致；
- 修改带有既有注释的逻辑时，同时核对并更新相关中文注释，避免注释与行为不一致；
- **Git 提交信息（Commit Message）强制中文**：
  - 严禁默认生成纯英文提交说明（如 `feat: add...`、`fix: resolve...` 仅跟英文短语）；
  - 格式遵循 Conventional Commits 规范：`<类型>(<模块可选>): <中文动词与改动业务目的>`，类型前缀保留业内通用英文词（`feat`、`fix`、`docs`、`style`、`refactor`、`perf`、`test`、`chore`），冒号后必须为明确、具体的中文说明；
  - 多行提交正文（Body）与结尾（Footer）如需使用，也必须全部使用中文。

### 员工交付门禁

凡实际修改仓库文件的 agent 均视为“员工”，无论其当前承担 `backend`、`frontend`、`ops`、`data` 或其他专业角色，都必须遵守以下要求：

- 涉及 Django/Wagtail 运行时代码时，开始编码前阅读 `说明书/02-研发规范与工程治理/01-运行时代码注释与类型标注规范.md`，并按其范例补充中文模块说明、非显而易见函数/方法 docstring 和可由调用点确认的参数/返回类型。
- 注释只解释业务规则、数据保护边界、异常补偿、并发行为、安全约束和“为什么”；不得逐行复述显而易见代码，也不得用 `Any` 掩盖未知契约。
- 注释、docstring 和类型标注不得改变查询、事务、保存顺序、错误码、日志字段、返回结构或外部服务副作用；修改既有逻辑时必须同步核对旧注释。
- **Git 提交信息强制中文核验**：生成 commit 时必须严格按照中文规范拟定提交信息，主 agent 或 `review` 审查发现纯英文 commit message 时必须打回并修正为中文后再提交。
- 交付或提交前必须报告新增的注释/类型契约、未覆盖边界、`compileall`/相关测试/`git diff --check` 结果和残余风险。主 agent 或 `review` 发现证据不足时不得合并。

## 技能与 MCP 调用规范

开发、排障和验收时按任务需要主动使用已配置的技能与 MCP，优先获取结构化、版本准确、
范围受控的证据；不得为了调用工具而扩大任务范围，也不得用工具结果替代代码、配置、日志、
数据库和运行状态的交叉核验。调用前先确认工具可用、目标环境正确、权限与数据影响明确。

### 当前 Codex 能力基线

截至 2026-08-09，本机已核实以下能力。这里的“已安装插件”“已安装 Skill”“已配置 MCP”与
“当前会话可调用”是不同状态；每个新会话仍应以实际暴露的 Skill 和工具声明为准，不得只因
本节有记录就假定连接、认证或目标数据源可用。

- 当前会话已暴露 `context7`、`fetch`、`playwright` 和 `github` MCP；
- `google-toolbox` 已登记在本机 Codex 配置中，但本次核实时未向当前会话暴露工具。使用前必须先
  检查工具声明与测试数据源；不可用时按下文规则回退，不得声称已通过 MCP 查询；
- 已安装项目 Skill：`django-wagtail-development`、`playwright`、`karpathy-guidelines`、`ui-ux-pro-max`；
- 已安装插件：`github`、`build-web-apps`、`coderabbit`、`plugin-eval` 和个人插件
  `wagtailblog-ssh-ops`；插件通常通过其 Skill 或 MCP 能力触发，不以插件名称作为固定命令；
- 当前可见的相关插件 Skill 包括 GitHub 仓库/CI/发布工作流、`frontend-testing-debugging`、
  `frontend-app-builder`、`coderabbit:code-review` 和 `plugin-eval` 系列；具体名称以会话清单为准。

不要重复安装上述能力。若某项已安装但当前未暴露，先检查 Codex 配置、连接器状态和是否需要
重启会话；仍不可用时记录原因，再使用任务允许的本地命令或既有 SSH 工作流。

### Django/Wagtail 与技术文档

- Django、Wagtail 模型、StreamField、后台、模板、搜索、迁移或兼容性任务使用
  `django-wagtail-development` 技能指导工作流；
- 查询 Django 5.2、Wagtail 7.4、Celery 等版本相关 API 时优先使用 `context7`，不得仅凭记忆
  推断已变更或废弃的接口；
- 已知具体 URL、README、部署文档或普通网页时使用 `fetch`；Context7 找不到准确内容时，
  再用 Fetch 读取官方来源；
- Fetch MCP 同时可能暴露写入型 HTTP 方法；读取网页不构成调用 POST、PUT、PATCH 或 DELETE
  的授权，任何外部写操作仍须符合用户当前任务与数据保护边界；

### 前端与浏览器调试

- 页面交互、Wagtail 后台、表单、分页、搜索、响应式布局、控制台和网络请求验证使用
  `playwright` 技能及 Playwright MCP；
- 浏览器调试先复现问题并保留必要证据，再定位模板、静态资源、接口或后端根因；不得以修改
  测试脚本掩盖产品缺陷；
- 桌面端和移动端按受影响范围验证，检查页面空白、内容溢出、遮挡、静态资源 404、JS 错误、
  请求失败及关键操作路径；
- Playwright 产物继续遵守本文顶部规范，统一写入 `output/playwright/`。

### 数据库与缓存

- 测试环境 MySQL、MongoDB 和 Redis 的结构、查询计划、活动查询与内容核验优先使用
  `google-toolbox`，并先确认数据源名称和数据库属于测试环境；
- 当前 Google Toolbox 只用于已配置的读取/查询工具。不得通过 MCP 增加或调用插入、更新、
  删除、清库、恢复、迁移或批量修复工具，除非用户针对准确目标另行明确授权；
- MongoDB 正文、草稿、revision pointer 与 `mongo_content_id` 的保护规则始终高于调试便利性；
- Google Toolbox 未覆盖或结果不足时，可在 WSL2 测试环境使用项目自身管理命令或数据库客户
  端进行最小范围核验；生产数据库操作仍执行独立备份、影响说明、确认和回滚门禁；
- 查询只返回解决问题所需的字段和行数，不批量输出正文、凭据、个人数据或完整生产日志。

### GitHub 与远程运维

- GitHub 仓库、Issue、PR、评论和检查状态优先使用 GitHub 插件/MCP；本地工作树、暂存区、
  commit 和 push 仍以 WSL2 中的 Git 命令为准；
- GitHub MCP 使用用户环境变量提供认证，不得把 Token 写入仓库、插件脚本、文档或回复；
- 服务器状态、精确 commit、fast-forward 同步和服务健康检查可使用 `wagtailblog-ssh-ops`
  插件或现有 WSL2 SSH；不得在同一任务中并发执行两个远程写入口；
- SSH 插件的写能力不等于自动授权。迁移、生产数据、环境文件、systemd unit、端口、队列、
  服务重启和不可逆操作仍须遵守本文的生产门禁；
- MCP、插件或连接器不可用时，记录原因并回退到现有 shell、Git、SSH 和项目命令，不得重复
  安装同类工具或恢复损坏的旧配置来绕过故障。

### 工具输出与 Token 控制

- 先缩小文件、URL、数据源、时间范围和日志级别，再调用工具；优先一次返回结构化摘要；
- 不读取与任务无关的整个代码库、完整数据库、全量日志或大型浏览器产物；
- 工具输出包含敏感信息时只报告状态和必要字段，不在回复中复述秘密值；
- 同一事实已有可靠证据时不重复调用多个重叠工具，只有结果冲突或证据不足时才交叉验证。

### 本地与 GitHub 自动化门禁

- `.pre-commit-config.yaml` 当前包含 staged diff 格式检查，以及在 WSL2 的
  `wagtailblog-test` 环境执行 Django system check；Git 写操作仍统一从 WSL2 发起；
- `.github/workflows/django-ci.yml` 在 `main` push、面向 `main` 的 PR 和手动触发时运行，
  使用 Python 3.13、MySQL 8.4、`requirements.txt` 和 `python manage.py check`；
- 2026-08-09 核实自动化基线时，本地 `HEAD` 与 `origin/main` 均为
  `b377231cb6dadca6db369b85f1f402f11cb2cb68`。该 SHA 只是核实记录，不是永久发布目标；每次
  测试、提交和生产同步都必须重新读取实际 SHA 与 GitHub 检查状态；
- pre-commit 和 GitHub Actions 是补充门禁，不能替代与变更相称的 WSL2 测试、迁移检查、
  浏览器验收、生产备份、服务健康检查或人工授权。

## 研发与发布闭环（标准 7 步交付流水线）

所有特性开发与系统运维必须遵循以下标准 7 步闭环，严禁跳步或流程倒置：

### 1. 需求收集与范围界定
明确用户目标、背景事实、非目标与关键假设；核查当前代码、Git 分支、运行配置与依赖服务，划分风险等级（R0-R3）。

### 2. 方案设计（gpt-5.6-sol high）
凡涉及功能重构、数据调整或复杂逻辑，由 `gpt-5.6-sol` 在 `说明书/` 对应子目录新建或更新方案文档。方案必须包含：现状证据、设计与实施步骤、实际修改文件清单、数据/服务影响、测试与验收标准、回滚点及部署复杂度评估。

### 3. 代码实施与定向测试（gemini-3.8-flash-high）
由 `gemini-3.8-flash-high` 依据已确认方案进行精准编码与本地验证：
- 严格遵循员工交付门禁：运行时代码必须增加中文注释、中文 docstring 与静态类型标注；
- 遵循单一写入者原则，不顺手重构无关代码；
- 在 WSL2 共享工作区的 `wagtailblog-test` 环境中执行 `python manage.py check`、`compileall` 及定向应用测试，确保全部绿灯通过；
- **开发遇阻升级求助与跨角色交流机制（Escalation Protocol，遇阻即停、不盲猜、不越权）**：
  - **开发与架构师（Sol）技术交流**：当 Gemini 遇到复杂报错、底层框架 API 异常、数据模型外键/索引冲突或定向测试修复连续 2 次仍未通过时，**严禁盲目试错、篡改业务逻辑或擅自删减测试断言**；必须整理最小复现与现场向架构师（Sol）发起技术求助，由 Sol 高阶推理定位根因并给出精确指导后再行修复；
  - **开发与产品经理（PM）业务交流**：当遇到需求未明确覆盖的交互分支、异常错误文案、字段默认值或业务规则互斥时，**严禁开发人员私自脑补业务逻辑**；必须向 PM（主 Agent）发起需求澄清；涉及重大业务决策或交互取舍时，由 PM 提炼清晰选项请示对话人（用户）定夺；
  - **标准求助现场**：必须具备【当前目标】、【报错/歧义现场】、【排查分析】与【备选思路】，做到“带着思考求助、依循指导落地”。

### 4. 真实功能检测（browser-skill / Playwright）
对涉及前台页面、Wagtail 后台管理、表单提交或交互流的功能，启动真实浏览器自动化进行跨端回归：
- 检查页面空白、控制台 JS 报错、网络 404/500、遮挡/溢出及桌面与移动视口渲染；
- 所有调试截图、录屏、trace 产物严格写入 `output/playwright/`，保持 Git 忽略，严禁提交。

### 5. 全流程对照方案审核（grok-4.6 high）
由 `grok-4.6` 担任独立审查官，执行严密的反向与边界审查：
- 严格对照第 2 步的《说明书》，逐条复核功能是否 100% 达成、验收标准是否全部满足；
- 严密排查潜在并发竞态、异常补偿遗漏及主程序核心链路是否受到破坏；
- 输出明确的审核判定报告；若发现缺陷打回给 gemini 修正并重测，直至通过。

### 6. 对话人最终确认门禁（User Gatekeeper）
向对话人（用户）呈报完整闭环证据：方案要点、测试结果、浏览器检测证据与 grok 审核通过判定。
- **核心红线**：严禁在未经对话人明确同意前执行 `git commit`、`git push` 或发起生产部署。

### 7. Git 提交与双模型分级部署验证（Maker-Checker 机制）
获得对话人明确同意后，按**双模型四眼协作（Sol 指导监督 + Gemini 机械执行）**标准执行闭环：
1. **WSL2 提交**：在 WSL2 共享工作树提交精确 commit（**强制中文提交信息**：`<类型>(<模块>): <中文动作说明与改动理由>`）并推送至 `origin/main`；
2. **双模型分级部署流程**：
   - **部署指挥官（`gpt-5.6-sol high`）**：核查生产 Git 树、远程分支 SHA 与数据备份，制定原子化分步命令清单及回滚清单；
   - **部署执行官（`gemini-3.8-flash-high`）**：严格按照 Sol 指导的精确命令逐条敲入执行，零自由发挥、原样回传退出码与终端日志；
   - **步步复核门禁**：每执行一步，Sol 必须复核返回结果无误后方可下达下一步指令；遇任何异常立即终止并向对话人呈报；
   - **普通部署（轻量修改）**：Sol 制定轻量流程（Fetch -> FF Merge -> Reload uWSGI -> 健康检查），Gemini 执行并回传状态；
   - **复杂部署（核心变更）**：涉及 MySQL 迁移、MongoDB 结构演变、ES 索引重建、systemd unit 或环境配置，**必须在说明书单独编写《部署与回滚实施方案》**，经用户再次授权后，双模型步步为营执行并按序重启应用服务；
3. **生产健康验收**：由 Sol 牵头核验生产 4 大 systemd 服务 active/enabled 状态、端口/socket 监听、前后端访问、Celery 队列与 Filebeat 日志链路，出具最终健康体检单与剩余风险报告。

## 服务与 systemctl.md

`systemctl.md` 是测试与生产共同的服务维护基准。任何涉及 service、timer、socket、队列、
Beat 任务、Filebeat、索引器、依赖服务、环境变量、数据/日志目录、端口、uWSGI、Nginx
或反向代理的变更，都必须同步更新该文档。

当前应核实的应用服务：

- `wagtailblog3.service`：uWSGI / Django；
- `wagtailblog3-celery-maintenance.service`：`maintenance` 队列 Worker；
- `wagtailblog3-celery-beat.service`：定时任务与失败补偿；
- `wagtailblog3-filebeat.service`：项目日志采集至 Elasticsearch。

不得新增只消费 `email` 或 `default` 队列的 Worker，除非已说明用途、并发、数据影响、
日志路径与回滚方式并获得确认。

service unit 发生变化后必须执行 `systemctl daemon-reload`，并核对 enable 状态。重启依赖
顺序为：基础设施 → Django/uWSGI → Worker → Beat → Filebeat → 必要时 Nginx。Elasticsearch
恢复期间 Filebeat 的短暂连接失败应观察退避恢复，不得连续重启。

生产开机恢复不能只以 unit 为 `enabled` 或进程为 `active` 作为完成证据。启用独立内容搜索时，
必须先确认 MySQL、MongoDB、Redis、MinIO、Docker 和 Elasticsearch 已实际可用，并确认生产
read alias 存在且只指向当前 serving 内容索引，再允许 Django、maintenance Worker 和 Beat
进入正常工作。若 unit 尚未实现有限超时的 readiness `ExecStartPre`，必须把该缺口作为残余风险
记录，不能宣称生产虚拟机重启后搜索链路必然无错误自动恢复。

生产部署或重启后，按变更范围执行：失败 unit 检查、四个服务 active/enabled、socket/端口、
首页和后台、Django check、静态文件、Redis/Worker 队列、Beat 调度、Filebeat/Elasticsearch、
日志与 outbox 检查。服务器重启后必须重新进行完整验收。

## 回滚与交付

部署失败时停止或回退本次涉及的服务，恢复到上一个已验证 commit 或文件清单；只有确认迁移
兼容时才处理数据库回滚，绝不删除 MongoDB 正文、草稿或 revision 数据。恢复后重新执行
Django check、服务和访问检查，并记录原因、动作与残余风险。

每次交付报告必须包含：完成范围、实际修改文件、测试与生产 commit、测试结果、测试与生产
服务状态、`systemctl.md` 是否更新、服务变更、迁移/生产数据操作、健康检查、回滚点、
环境差异与残余风险。

## 多 agent 协作与对话指令

详细方案见 `说明书/02-研发规范与工程治理/02-多Agent协同交付与模型调度规范.md`。新对话中主 agent 读取本文件后，
继续扮演技术负责人和交付编排者：负责需求拆解、风险分级、子 agent 分派、结果集成、最终测试、
授权门禁和交付报告。子 agent 是专业执行者，不能单独决定生产数据操作、迁移、发布、服务重启或回滚。

### 对话快捷指令

这些是主 agent 识别的对话协议，不是操作系统命令；等价的自然语言也有效：

- `/do <任务>`：自动调研、分派、实现、测试、审查和汇报；
- `/plan <任务>`：只调研并写方案，不修改业务代码；
- `/implement`：执行已确认的方案；
- `/assign <pm|arch|backend|frontend|qa|review|ops|data> <子任务>`：指定角色执行子任务；
- `/review <范围>`：代码审查模式，优先报告问题；
- `/test <范围>`：执行测试和验收，不修改产品代码来掩盖缺陷；
- `/status`：汇报任务、agent、测试和阻塞状态；
- `/prepare-release`：准备提交、推送和发布步骤，不执行生产发布；
- `/confirm-production <已说明操作>`：明确授权已说明的生产操作；
- `/stop`：停止当前未完成的协作任务。

未指定角色时，主 agent 按 R0-R3 风险自动分派；小任务不强行启动全部 agent。同一文件不得由多个
agent 同时写入，主 agent 负责集成和冲突处理。每个子 agent 必须报告检查文件、事实依据、实际修改、
测试结果和未解决风险。

### 角色、模型与技能路由

- `pm` 产品/需求：主 Agent / `gpt-5.6-luna` 中推理；负责需求对齐、边界澄清与业务决策把关。当开发遇到业务规则或交互模糊时，由 PM 进行口径对齐或提炼决策选项请示对话人。使用 `karpathy-guidelines`，文档检索按需使用 `mineru-document-explorer`。
- `arch` 架构/方案：**`gpt-5.6-sol` 高/超高推理**。负责核心方案设计、数据库存储契约、索引切换、跨服务解耦与灾备回滚方案（在《说明书/》产出文档）；同时负责接收开发人员的技术卡点求助，进行根因诊断并给出架构与代码指导。使用 `django-wagtail-development`、`karpathy-guidelines`，版本 API 优先 `context7`，官方资料使用 `fetch`。
- `backend` 后端：**`gemini-3.8-flash-high`**。负责 Django/Wagtail 模型、业务 API、Celery 异步任务及定向单元测试编写，按规范补充中文注释与类型标注。遇技术阻碍向架构师（Sol）求助，遇业务歧义向产品经理（PM）求助，严禁盲改、删断言或自作主张改架构。
- `frontend` 前端：**`gemini-3.8-flash-high`**。负责模板渲染、CSS 布局与交互优化，配合 `ui-ux-pro-max` 设计规范，确保无障碍与移动端不横向溢出。
- `qa` 测试：**`browser-skill` / Playwright + `gemini-3.8-flash-high`**。负责端到端浏览器真实功能检测与自动化测试执行，产物严格写入 `output/playwright/`。
- `review` 审查/安全：**`grok-4.6` 高推理**。负责深度对抗审查、安全审计、并发死锁排查与全流程对照方案核验，判定是否满足交付标准。使用 `karpathy-guidelines`、`django-wagtail-development`。
- `ops` 运维：常规部署 `gemini/terra`，复杂生产发布与回滚升级 `sol` 高。负责 WSL2 环境构建、Git 分支推送、生产 SSH 同步与分级服务编排，严格遵守生产分级门禁。
- `data` 数据/集成：**`gpt-5.6-sol` 高/xhigh**。负责 MySQL/MongoDB 存储演进、ES 索引别名生命周期与数据补偿治理。

`openai-docs` 只用于 OpenAI/Codex 产品或 API 问题，`pdf` 只用于 PDF 任务，`skill-creator`、
`skill-installer` 和 `plugin-creator` 只在用户明确要求创建、安装或维护 Skill/插件时启用。所有 Skill/MCP
均以当前会话实际暴露为准，不可用时记录原因并回退到允许的本地命令；不得向外部模型或工具发送凭据、Token、
生产日志、MongoDB 正文/草稿或个人数据。

主 agent 默认使用 `gpt-5.6-terra` 中推理；涉及生产数据、不可逆迁移、跨服务契约、并发一致性、安全边界、
回滚失败风险或证据冲突时升级为 `gpt-5.6-sol` 高/xhigh。模型选择不能替代本地测试、配置核查、备份、
生产授权和回滚门禁。

### 子 Agent 调度与 Token 缓存（Prompt Cache）优化准则

为适配第三方中转通道（CPA/OneAPI）并最大化降低 Token 消耗与首字延迟（TTFT），子 Agent 协作必须严格执行以下前缀缓存与上下文调度守则：

1. **头部前缀恒定（Prefix Invariance）**：
   - 传递给子 Agent 的初始 Prompt 严禁在头部拼接动态变化的内容（如精确时间戳、随机数、轮次计数器、UUID）；
   - 固定规则（角色定位、工程约束、验收契约）严格保持在头部，动态指令与变动上下文放置在尾部，确保头部 1000+ Token 稳定命中上游 Prompt Cache。
2. **最小上下文派发（`fork_context` 最小化）**：
   - 派生子 Agent（`spawn_agent`）时，**默认使用 `fork_context = false`**；
   - 子 Agent 拥有独立的紧凑上下文，主 Agent 仅按需提取“当前目标”、“关联文件路径”与“设计约束”作为初始任务发送，杜绝无节制复制主线程长历史导致的大规模 Token 浪费。
3. **长生命周期实例复用（`send_input` 闭环）**：
   - 当针对同一子任务进行方案打磨、代码排错、测试重试时，严格复用已有 Agent 实例（通过 `send_input` 交互），不重新 `spawn_agent`；
   - 该 Agent 的上一轮输出会自动沉淀为下一轮对话的静态前缀，使得后续轮次的 Token 输入几乎全部命中 Cache（计费折扣达 50%~80%）。
4. **单阶段完成后及时释放（`close_agent`）**：
   - 子 Agent 交付经审查通过后，主 Agent 必须主动调用 `close_agent`，释放并发槽位，防止僵死 Agent 占用资源或在后续轮次中引发歧义。
5. **第三方中转鉴权隔离约束**：
   - 明确所有子代理均通过第三方 CPA（`https://jp.studytop.top/v1`）中转，`requires_openai_auth` 必须保持为 `false`，严禁开启官方 OAuth 鉴权校验；`disable_response_storage = true` 全局继承。
