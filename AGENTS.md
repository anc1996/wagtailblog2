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

## 搜索架构实施上下文

当用户任务涉及前台搜索、`BlogPage` 发布/取消发布/删除、MongoDB 正文或草稿、
Elasticsearch、`ContentSearchOutbox`、Celery 搜索同步、索引重建、别名切换或搜索容量时，
每个新会话必须先读取：

- `说明书/09-搜索架构容量与演进方案.md`；
- `说明书/10-搜索架构实施计划.md`；
- 用户明确指定的工作包；未指定时，读取第一个尚未完成工作包及其“实施记录”。

开始实现前必须明确本次所属 WP、对应 Gate、当前实施记录状态、推荐模型/推理强度、将修改和不修改的文件、数据/索引/服务影响、测试、回滚和仍需确认的生产操作。不得仅因计划存在就假定某阶段已经完成、某项迁移已经执行或某个索引已经创建。

`说明书/10-搜索架构实施计划.md` 标题中的模型和推理强度是任务复杂度建议，不是强制切换命令：

- 当前会话先核对实际暴露的模型和工具；未暴露 `gpt-5.6-terra` 或 `gpt-5.6-sol` 时，使用当前 Codex 模型并如实记录。
- `terra / 高` 用于事务、并发、数据保护、Outbox、在线重建和生产切换等跨系统高风险判断；`sol / 中` 用于边界已明确后的只读命令、测试补全、游标和建议功能。
- 外部模型只在确有独立复核价值、当前会话可用且符合用户授权时使用；不得发送源码、Mongo 正文/草稿、凭据、Token、生产日志或个人数据。
- 高推理强度不替代 WSL2 测试、代码和配置核查、备份、生产授权或回滚门禁。

每次真正完成一个 WP 或可独立验证的子批次后，必须在该 WP 的“实施记录”追加事实记录：日期、状态、实际内容、实际修改文件、精确测试和结果、数据/索引/服务影响、commit 或未提交状态、回滚点和残余风险。不得写思维链、正文、凭据或大段日志；文档修改本身不代表代码工作包完成。若只完成部分内容，必须记录“部分完成”及未完成项，不得提前标记整个 WP 完成。

## 当前工作区与环境事实

- Windows 主机：`192.168.20.1`，负责编辑工作区 `F:\openclaw\workspace\wagtail\wagtailblog2`；
- WSL2 测试环境：Hyper-V 第二代虚拟机 `192.168.20.5`，发行版为 `Debian`；测试工作区为 `/mnt/f/openclaw/workspace/wagtail/wagtailblog2`；
- 两个路径是同一个 NTFS 工作目录与同一个 `.git`，不是两份代码，也不需要同步；
- Windows 主机不承担 Conda 运行环境；测试 Conda 位于 WSL2：`/root/anaconda3/envs/wagtailblog-test`；
- Django 包：`wagtailblog3`；仓库根目录包含 `manage.py`；
- 唯一开发与发布分支：`main`；远程：`origin`；
- 生产服务器：Hyper-V 第一代虚拟机 `192.168.20.2`（主机名 `ziliao`，不是 WSL）；生产项目：`/home/source/Django/wagtail/wagtailblog3`；生产 Conda：`/root/anaconda3/envs/wagtailblog`；
- 生产服务：Nginx + uWSGI + systemd，依赖 MySQL、MongoDB、Redis、MinIO、Docker、Elasticsearch、Celery 与 Filebeat。

这些是当前事实的起点。每个发布或服务任务都必须重新核实路径、分支、commit、服务名、
端口、socket、Conda 路径与环境文件，不能把它们当成永久常量。

## Windows、WSL2 与 Git 规则

Windows 负责编辑；WSL2 负责使用 `wagtailblog-test` 运行 Django、Celery 和测试，并作为
Git 发布入口。因为工作树共享，任一端修改文件或创建 commit，另一端立即可见。

- 不要在 Windows 和 WSL2 同时执行 Git 写操作（add、commit、merge、pull、push、rebase），避免共享 `.git/index.lock` 冲突；
- WSL2 与 Windows 的 SSH key、凭据管理器和全局 Git 配置彼此独立；仓库 `.git/config`、分支和 commit 记录共享；
- 发布前在 `wsl -d Debian` 的 `/mnt/f/.../wagtailblog2` 执行 Git 状态、提交和推送；
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

## 代码注释规范

- 新增或修改的代码注释必须使用中文；
- 对业务规则、数据保护边界、异常补偿、并发行为、安全约束和不易从代码直接判断的原因，必须
  增加简洁的中文注释；
- 不为显而易见的赋值、分支或框架调用逐行添加重复说明，注释应解释“为什么”，并与实现保持一致；
- 修改带有既有注释的逻辑时，同时核对并更新相关中文注释，避免注释与行为不一致。

## 技能与 MCP 调用规范

开发、排障和验收时按任务需要主动使用已配置的技能与 MCP，优先获取结构化、版本准确、
范围受控的证据；不得为了调用工具而扩大任务范围，也不得用工具结果替代代码、配置、日志、
数据库和运行状态的交叉核验。调用前先确认工具可用、目标环境正确、权限与数据影响明确。

### 当前 Codex 能力基线

截至 2026-08-09，本机已核实以下能力。这里的“已安装插件”“已安装 Skill”“已配置 MCP”与
“当前会话可调用”是不同状态；每个新会话仍应以实际暴露的 Skill 和工具声明为准，不得只因
本节有记录就假定连接、认证或目标数据源可用。

- 当前会话已暴露 `context7`、`fetch`、`playwright`、`github` 和 `openrouter` MCP；
- `openrouter` 当前声明可选模型为 `gpt-5.6-terra` 与 `gpt-5.6-sol`，只在任务确有必要时调用；
- `google-toolbox` 已登记在本机 Codex 配置中，但本次核实时未向当前会话暴露工具。使用前必须先
  检查工具声明与测试数据源；不可用时按下文规则回退，不得声称已通过 MCP 查询；
- 已安装项目 Skill：`django-wagtail-development`、`playwright`；
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
- `openrouter` 仅在任务确实需要其模型或接口能力时调用，不得把项目源码、凭据、数据库内容
  或生产日志无目的地发送到外部模型服务。

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

## 研发与发布闭环

### 1. 调研与设计

处理中大型需求前，明确目标、范围、非目标、验收标准、数据影响与生产范围；检查 Git、
设置、URL、应用注册、中间件、Wagtail 页面/StreamField、相关测试、迁移、数据流与服务。
同时确认 MySQL、MongoDB、Redis、Elasticsearch、MinIO、uWSGI、Nginx、Celery、Beat、
Filebeat、日志、审计及失败补偿的受影响部分。

实施前说明要修改和不修改的文件、数据与服务影响、异常路径、性能/安全风险、测试、
回滚与需要确认的事项。每完成一个可验证单元检查 diff，避免无关重构。

### 2. WSL2 测试与版本确认

在 WSL2 共享工作树中使用 `wagtailblog-test` 运行与变更相称的检查：

- `python manage.py check`；
- `python manage.py makemigrations --check --dry-run` 与 `python manage.py migrate --plan`（涉及模型或迁移时）；
- 相关单元/集成测试；
- 静态文件、配置、网站、后台、Celery、Beat、Filebeat 和日志链路检查（受影响时）。

只有检查通过后才能形成发布 commit；不得以未提交工作区内容发布。提交前确认：

```bash
git status --short --branch
git diff --check
git diff --cached --check
```

### 3. GitHub 与生产同步

用户确认可发布且 WSL2 测试通过后，按以下固定顺序完成闭环：

1. 在 WSL2 提交精确 commit 并推送 `origin/main`；
2. 验证本地 `HEAD`、`origin/main` 与远程 `main` 指向同一 SHA；
3. 同步生产仓库到同一精确 SHA；
4. 完成与变更范围相称的生产验收；
5. 报告 commit、服务状态、数据操作、回滚点与剩余差异。

生产同步前必须确认生产目录是干净、安全的 Git 工作树，分支为 `main`，远程地址正确，
并检查 `HEAD..origin/main` 的 commit 与文件清单。不得对未知状态执行 `git pull`，不得使用
`rsync --delete`。已确认安全时使用：

```bash
git fetch origin --prune
git diff --name-status HEAD..origin/main
git merge --ff-only origin/main
```

生产同步仅能部署已验证 commit。文档-only 变更不重启服务；代码、依赖、Celery、Beat、
Filebeat、Elasticsearch 或配置变更必须根据实际影响重启对应服务。涉及迁移、生产数据、
环境文件、systemd unit、端口、队列、外部服务或不可逆操作时，即使 commit 已通过测试，
仍必须在生产执行前单独说明影响、备份、顺序与回滚并获得确认。

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
