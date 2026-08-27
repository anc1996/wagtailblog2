# Wagtail 8.0 升级可行性方案

## 1. 方案状态

- 方案日期：2026-08-26
- 当前阶段：仅调研与方案，未修改业务代码、依赖、数据库、服务或生产环境
- 目标分支：`main`
- 当前工作区：`main` 与 `origin/main` 已核对为同一分支；本方案不创建提交
- 当前基线（WSL2 `wagtailblog-test` 实测）：Python 3.13.2、Django 5.2.8、Wagtail 7.4.3
- 生产范围：本方案不构成生产安装、迁移、索引写入、服务重启或发布授权

## 2. 背景与现状证据

### 2.1 项目现状

`requirements.txt` 当前锁定 `wagtail==7.4.3`、`Django==5.2.8`、`djangorestframework==3.16.1`、
`draftjs_exporter==5.1.0`、`modelsearch==1.3.1`、`django-tasks==0.9.0`，并使用
`wagtailmedia==0.18.1`、`wagtailcodeblock==1.30.0.0`、`wagtail-modeladmin==2.3.0`、
`wagtail-video==1.0.1`。WSL2 实际环境通过 `pip check`，但没有安装 `django-ninja`、`wagtail-ai`。
项目测试也明确要求不注册 `wagtail_ai`，因此本次不应顺便加入 `wagtail-ai`。

项目使用 Wagtail 管理后台、页面树、多语言、图片/文档、StreamField、表单、SnippetViewSet、
自定义图片上传、Wagtail 搜索和自建 Elasticsearch 内容搜索；正文、StreamField、MongoDB、
revision、媒体对象和搜索 outbox 均属于受保护数据。

### 2.2 Wagtail 8.0 官方事实

依据 Wagtail 官方 8.0 发布说明（2026-08-25）及 8.0 的 `pyproject.toml`：

- 支持 Python >=3.10、Django >=5.2；项目现有 Python 3.13 / Django 5.2.8 满足核心版本范围。
- 新增 REST API v3（预览版），依赖 `django-ninja>=1.6.3`，但项目当前没有挂载该 API，不能把预览 API 当作本次升级目标。
- 新增 `wagtailcore.0098_apitoken`，首次启用 Wagtail 8 时预计会创建 API token 表；需把它视为 schema 迁移，而不是“零迁移”升级。
- 修复页面管理 API、文档 SHA1 探测、私有 collection 后代泄露、Snippet 复制权限和页面翻译权限等安全问题。
- 删除 Wagtail 6.4–7.3 已弃用功能，包括旧 telepath 路径、旧 `INDEX` 搜索配置、旧按钮类、旧用户栏接口等。
- AVIF/WebP 在未指定输出格式时不再默认转换为 PNG。
- 自定义 `SnippetViewSet` / `ModelViewSet` / `ChooserViewSet` 权限策略需要单独注册；继续依赖自动注册会产生弃用警告。
- `AbstractFormField.field_type` 设置为 `required_on_save=True`，需检查表单字段草稿保存行为。

#### REST API v3 是什么、这次为何不启用

Wagtail 8.0 的 REST API v3 是面向外部客户端的内容读取/管理接口预览版，基于 Django Ninja 提供类型化的 OpenAPI 接口和 token 认证能力。典型用途包括：移动端或独立前端按页面、站点和语言读取已发布内容；为搜索、内容分发或静态生成器提供结构化数据；在明确授权后由受控的集成程序执行页面/媒体等管理操作。它与项目现有的 Markdown 导入 API、Wagtail 后台请求和自建内容搜索 API 不是同一条路由，也不会自动替代这些接口。

启用 v3 API 需要额外决定公开 URL、字段序列化格式、草稿/权限边界、速率限制、token 生命周期、审计和客户端兼容策略。当前项目没有该 API 的消费方、路由或安全评审，也没有启用 `django-ninja` 的业务需求；Wagtail 8 安装它只是依赖解析结果。因此本次升级只安装其运行依赖，不注册 v3 URL、不发放 API token、不改变公开 API 契约。未来若要启用，应另立方案，先完成威胁建模、最小字段白名单、只读权限和版本化契约，再单独取得发布授权。

官方来源：

- <https://github.com/wagtail/wagtail/releases/tag/v8.0>
- <https://raw.githubusercontent.com/wagtail/wagtail/v8.0/docs/releases/8.0.md>
- <https://raw.githubusercontent.com/wagtail/wagtail/v8.0/pyproject.toml>

### 2.3 依赖解析实测

在 WSL2 测试环境执行 `python -m pip install --dry-run --upgrade Wagtail==8.0`（未写入环境）后，
解析器给出的必需变化为：

| 依赖 | 当前 | Wagtail 8.0 要求/解析结果 | 判断 |
|---|---:|---:|---|
| Wagtail | 7.4.3 | 8.0 | 必须升级 |
| Django | 5.2.8 | >=5.2 | 可保持，不与本次合并升级 Django 6 |
| djangorestframework | 3.16.1 | >=3.18.0，解析到 3.18.0 | 必须升级并回归 Markdown API |
| django-ninja | 未安装 | >=1.6.3，解析到 1.6.3 | 作为 Wagtail 依赖安装，但不启用 v3 API |
| draftjs_exporter | 5.1.0 | >=7.1.0，解析到 7.1.0 | 必须升级，回归富文本/StreamField |
| modelsearch | 1.3.1 | >=1.3.2，解析到 1.3.2 | 必须升级，回归搜索 |
| swapper | 未安装 | >=1.4，解析到 1.4.0 | 作为 Wagtail 依赖安装 |
| django-modelcluster | 6.5 | >=6.5,<7 | 可保持 |
| django-tasks | 0.9.0 | >=0.9,<0.13 | 可保持，检查任务时序 |

因此仅把 `wagtail==7.4.3` 改成 `8.0` 会造成依赖锁定不完整；必须将上述解析结果完整记录并重新生成可复现环境。

## 3. 升级收益

1. 获得 Wagtail 8.0 的五项权限/信息泄露安全修复，尤其覆盖项目启用的页面后台、图片/文档、Snippet、翻译和多语言能力。
2. 进入当前主版本的依赖支持范围，减少继续停留在 7.4.3 所产生的安全补丁和第三方兼容性滞后。
3. 获得后续使用 REST API v3、全局权限策略注册和新版后台组件的能力；这些能力可以延后启用，不改变现有公开 API。
4. 修复图片验证、表单自动保存、工作流、多语言移动页面和权限相关缺陷。

## 4. 成本与风险

1. 依赖升级不是单包变更：DRF 3.18、`draftjs_exporter` 7.1、`modelsearch` 1.3.2、`django-ninja` 和 `swapper` 会进入运行环境，需重新锁定并验证。
2. `wagtailcore.0098_apitoken` 可能产生数据库 DDL；当前 7.4.3 环境还存在 `0098_merge_20260603_0945` 迁移记录，目标版本使用同号的 `0098_apitoken`，必须在隔离库实际验证迁移图和 SQL。生产必须先备份 MySQL，先在隔离测试库执行 `migrate --plan` 和迁移，再决定窗口。
3. Wagtail 7.2 已要求在升级到 8.0 前运行 `update_index`，把 Elasticsearch 文档字段 `content_type` 更新为 `_django_content_type`；项目仍保留 Wagtail `default` Page 索引，因此不能跳过。自建内容索引不能未经核对直接重建或删除。
4. `content_ai` 的 `BlogMetadataPromptTemplateViewSet` 和 `blog` 的 `PageViewSnippetViewSet` 自定义 `permission_policy` 需要显式注册；否则虽可能暂时工作，但会产生弃用警告并留下未来版本风险。
5. 项目自定义 Markdown widget、图片 chooser、文档 chooser、Wagtail ModelAdmin、表单和 StreamField 渲染均依赖后台内部接口，必须做真实后台浏览器回归。
6. AVIF/WebP 默认转换策略变化可能影响新生成的 rendition；项目已有显式 `WAGTAILIMAGES_FORMAT_CONVERSIONS` 和 JPEG 输出测试，仍需验证原图、缩略图及富文本图片。
7. Wagtail 8.0 的 REST API v3 是预览版，接口可能在后续版本不兼容；本次不启用、不挂载 URL、不发放 API token。
8. `wagtail-modeladmin`、`wagtailmedia`、`wagtailcodeblock`、`wagtail-video` 虽未声明 Wagtail 8 上限，但实际兼容性不能由元数据替代，需在测试环境逐项验证；必要时再单独升级扩展包。

## 5. 可行性结论与建议

技术上可行，建议升级，但采用“先测试、后生产”的分阶段方式：

- **测试环境：建议现在开始**。依赖解析、弃用 API 扫描、迁移计划、搜索索引兼容和后台回归均可在隔离测试库完成。
- **生产环境：有条件升级**。只有在测试环境通过、生产 MySQL/Elasticsearch 备份完成、`update_index` 范围和停机窗口明确，并获得单独生产授权后才可发布。
- **不建议同时升级 Django 6.x 或重构搜索架构**。保持 Django 5.2.8 可以缩小变量；Wagtail v3 API、内容索引切换、MongoDB 正文修复、媒体迁移均不属于本次目标。
- 若当前业务更重视最低变更风险，可暂缓生产升级并继续使用 7.4.3；但需接受无法获得 Wagtail 8.0 安全修复的残余风险。建议将安全修复收益作为升级优先级依据，而不是为了新 API 立即上线。

## 6. 计划实施步骤（获得实现授权后）

### 阶段 A：依赖与代码兼容性

1. 在方案实施记录中锁定实际基线 SHA、测试环境包清单和生产包清单；不复制任何环境凭据。
2. 更新 `requirements.txt` 中 Wagtail 及其必需依赖，保持 Django 5.2.8；扩展包先保持当前版本，若测试证明不兼容再单独升级。
3. 在 `content_ai/wagtail_hooks.py` 与博客 Snippet 注册路径显式调用 `register_permission_policy`，保持现有“仅超级用户”及只读策略语义不变。
4. 扫描并清除/替换 8.0 删除的旧 telepath、旧按钮类、旧 `INDEX` 配置、旧 userbar 和旧模板接口；当前代码已使用 `wagtail.admin.telepath`、`wagtail.admin.telepath.widgets` 和 `INDEX_PREFIX`，初步未命中这些删除项。
5. 核对 `AbstractEmailForm`/`AbstractFormField` 的草稿保存、发布和表单提交行为；不改变表单数据模型和邮件副作用。

### 阶段 B：隔离测试与迁移

1. 在 WSL2 `wagtailblog-test` 安装完整锁定依赖，运行 `pip check`。
2. 运行 `python manage.py check`、`makemigrations --check --dry-run`、`migrate --plan`；审阅 Wagtail 8 的 `wagtailcore.0098_apitoken` 及任何扩展包迁移 SQL。
3. 仅在隔离测试数据库执行迁移；核验 BlogPage、StreamField、Markdown `markdown_block` key、revision pointer、MongoDB 正文和媒体引用均未被修改。
4. 在 Wagtail 7.4.3 测试环境先以 `update_index --backend default` 更新 Wagtail Page 索引，再切换到 8.0 验证搜索结果；自建 `content_*` 索引只按现有 outbox/alias 方案处理，不与 Wagtail 迁移混做。
5. 运行现有 `blog`、`base`、`search`、`comments`、`content_ai`、`observability` 测试，并增加权限策略、表单草稿、图片/文档 chooser、Markdown 导入和搜索结果的针对性断言。

### 阶段 C：浏览器与服务验收

1. 用既有测试栈启动 8080，Playwright 产物全部写入 `output/playwright/wagtail8-upgrade/`。
2. 桌面与移动视口检查首页、博客正文、搜索、Wagtail 登录、页面编辑、StreamField、图片/文档 chooser、Snippet 列表、评论 ModelAdmin、表单页面和多语言切换。
3. 检查控制台错误、静态资源 404、权限越权、媒体 URL、草稿保存/发布和 Markdown API 的 Bearer 认证；不调用 Wagtail v3 写入 API。
4. 仅当代码/依赖实际改变时，按 `systemctl.md` 的顺序重启测试或生产对应服务；不新增 Worker，不修改 Elasticsearch 容器和 unit。

### 阶段 D：生产候选与发布门禁

1. 生产操作前独立备份 MySQL、MongoDB 正文/草稿/revision、媒体和 Elasticsearch snapshot，并记录可恢复路径。
2. 明确维护窗口和 `update_index --backend default` 的影响范围，确认 Wagtail Page 索引与自建内容索引的 alias 不交叉。
3. 用户单独确认生产依赖安装、数据库迁移、索引更新、代码同步和服务重启；未确认前不得 SSH 写生产。
4. 按 `main` 的已验证 commit 发布，先执行生产 `pip check`、`manage.py check`、迁移计划和健康检查，再依次重启 `wagtailblog3.service`、maintenance Worker、Beat；Filebeat/Nginx 只有受实际变更影响时才处理。
5. 发布后验证四个服务 active/enabled、失败 unit 为 0、uWSGI socket、首页/后台、Wagtail Page 搜索、内容搜索 alias、队列和日志；任何失败都回到上一个已验证 commit。

### 阶段 E：收尾与交付

1. 在同一方案的实施记录中逐批记录实际 SHA、修改文件、依赖版本、测试结果、数据和服务影响、未提交状态或 commit、回滚点与残余风险。
2. 仅在 WSL2 工作树完成 Git add、commit 与 push；确认本地 `HEAD`、`origin/main` 和 GitHub `main` 为同一 SHA 后，才能形成生产候选。
3. 将实施期间确认的真实服务操作更新到 `systemctl.md`。当前其中的“WP8 code-only 发布服务规则”是此前业务功能发布的说明，不能作为本次 Wagtail 8 依赖、迁移与索引升级的操作依据；实施前必须新增经复核的 Wagtail 8 runbook，且不修改 systemd unit 时不得执行 `daemon-reload`。

## 6.1 工作包、角色与模型调度

以下工作包在用户以 `/implement` 明确授权后启用。子 agent 只执行所分配的测试或代码任务；数据库迁移、索引写入、生产 SSH 写操作、服务重启和回滚均由主 agent 在取得单独生产授权后编排。

| 工作包 | 目标与主要输出 | 负责人/协作角色 | 模型与推理 | 可并行性与文件边界 |
|---|---|---|---|---|
| WP0 基线与依赖冻结 | 锁定 SHA、`pip freeze`、解析后的精确依赖和第三方兼容性清单 | pm（主 agent）、qa | luna 中；依赖冲突升级 terra 高 | 可与 WP1 并行；只读，不写运行时代码 |
| WP1 代码兼容修复 | 更新 `requirements.txt`；修复已证实的弃用 API 与权限策略显式注册；补充定向测试 | backend | terra 中；权限语义或迁移图不一致升级 sol 高 | 独占 `requirements.txt`、`content_ai/wagtail_hooks.py`、最终选定的 blog 注册文件及其测试；不得同时由另一角色编辑 |
| WP2 迁移与数据不变量 | 审阅迁移图和 SQL；在克隆测试库执行；比较受保护数据的元数据/摘要 | data、review | sol 高 | 可与 WP1 的代码审阅并行，但实际迁移必须在依赖安装和代码冻结后串行；不输出正文，不修改生产数据 |
| WP3 Wagtail Page 搜索 | 在 7.4.3 隔离环境先升级 `default` Page 索引，再在 8.0 比较权限、多语言和结果 | data、qa | sol 高 | 与 WP4 可并行；只操作隔离测试 ES；不得触碰 `content_*` serving alias 或物理索引 |
| WP4 单元、集成与浏览器回归 | 执行定向测试、全量测试及桌面/移动后台和前台回归，保存最小必要证据 | qa | terra 高 | 可与 WP3 并行，但独占自己的测试数据库和浏览器用户会话；产物只写 `output/playwright/wagtail8-upgrade/` |
| WP5 独立安全复核 | 验证低权限后台、私有 collection、Snippet/Prompt 策略、Markdown Bearer 认证与草稿边界 | review | terra 高；发现越权或数据完整性风险升级 sol 高 | 可在 WP1 后并行 WP3/WP4；只读和测试账户操作，不保存真实内容 |
| WP6 生产候选与 runbook | 备份清单、维护顺序、精确命令审阅、`systemctl.md` 更新、回滚演练与授权单 | ops、review、主 agent | sol 高 | 必须在 WP1-WP5 全绿后进行；独占 `systemctl.md`，禁止与其他角色同时编辑 |
| WP7 生产发布与验收 | 按已验证 commit 部署、执行备份/索引/迁移/重启、健康检查与交付记录 | 主 agent、ops、qa、review | sol 高，发布前后独立复核 | 全程串行；每个写入动作都需要用户单独确认 |

角色可按需要新建，但不得以“更多 agent”为目的扩张范围。建议新增的 `data` 角色仅拥有测试环境最小范围的只读/隔离迁移核验职责，`review` 角色独立于实现者复核权限、数据不变量和回滚证据。

### 工作包依赖图

```text
WP0 基线/依赖 ──┬──> WP1 代码兼容 ──> WP2 隔离迁移与数据不变量 ──┐
                │                                                ├──> WP6 生产候选/runbook ──> WP7 生产发布
                └──> WP3 Wagtail Page 搜索 ──┐                   │
                                              ├──> WP4 回归测试 ─┤
WP1 完成 ────────────────────────────────────> WP5 安全复核 ────┘
```

WP2、WP3、WP4 与 WP5 的结论均为 WP6 的阻塞输入。任意一个工作包触发停止条件时，停止向下一阶段推进，保持 Wagtail 7.4.3 和现有生产 serving 索引不变。

## 6.2 详细执行计划

### 预备批次：冻结事实与隔离环境

1. 在 WSL2 读取 `git status --short --branch`、`git rev-parse HEAD`、`git ls-remote origin refs/heads/main`，记录精确 SHA；若工作树已有无关改动，保留并明确隔离，不将其纳入升级 commit。
2. 读取测试与生产的 Python/Wagtail/Django 包版本和 `pip freeze` 摘要，核对 `WAGTAILBLOG_ENV` 只选择各自环境文件；不读取或记录环境文件中的秘密值。
3. 创建可丢弃的测试 Python 环境或经确认的隔离克隆环境，确保原 `wagtailblog-test` 可恢复。禁止在生产 Conda 环境中半升级依赖。
4. 针对测试库建立仅含标识和哈希的升级前快照：迁移记录、表计数、Page ID/slug/live 状态、`mongo_content_id`、revision 指针、Markdown 块类型与 key、正文 SHA-256、媒体 ID/rendition 引用、权限与 Snippet 元数据。禁止输出 MongoDB 正文、草稿或凭据。

### 实现批次：依赖与兼容性

1. 依据 Wagtail 8.0 resolver 结果更新精确依赖：Wagtail 8.0、DRF 3.18.0、`draftjs_exporter` 7.1.0、`modelsearch` 1.3.2、`django-ninja` 1.6.3、`swapper` 1.4.0；Django 保持 5.2.8，除非解析器和完整测试证明需要更小范围的调整。
2. 不挂载 Wagtail REST API v3 URL、不创建或发放 API token、不改变现有 Markdown API 契约。`django-ninja` 是框架依赖而非启用 v3 API 的授权。
3. 修复 Wagtail 8 明确要求的自定义 `permission_policy` 显式注册，先为当前“仅超级用户”和只读 Snippet 策略写回归断言，再实施最小修改，避免策略语义扩大。
4. 扫描已删除接口，只有命中且测试失败时才替换；不顺便重构 StreamField、搜索架构、表单模型或前端样式。
5. 如修改 Django/Wagtail 运行时代码，遵守运行时代码注释与类型标注方案：中文模块说明、非显而易见方法的中文 docstring、可从调用点确认的参数/返回类型；不以 `Any` 掩盖契约。

### 验证批次：迁移、索引与回归

按以下层级在 WSL2 执行，后一层以前一层通过为前提；具体测试 label 以仓库实际发现结果为准。

| 层级 | 必做检查 | 通过条件 |
|---|---|---|
| L0 依赖与静态 | `pip check`、版本导入检查、`python -m compileall -q wagtailblog3 tools`、`git diff --check` | 版本准确、无依赖冲突、无语法或空白错误 |
| L1 配置与迁移图 | `manage.py check`、`makemigrations --check --dry-run`、`showmigrations wagtailcore`、`migrate --plan` | 计划已审阅；无项目意外迁移；明确 `0098_apitoken` 与现有 `0098_merge_*` 的兼容关系 |
| L2 定向单测 | Markdown 兼容/导入/认证/媒体、管理后台上传与页面视图、内容 AI、搜索正确性/同步/rebuild/content index | 无 Markdown key、媒体、权限、搜索或导入回归 |
| L3 隔离迁移 | 克隆测试库执行已审阅的迁移，复核升级前后不变量快照 | 不丢 Page/Revision/媒体；Mongo 正文哈希、`markdown_block` key 和 revision 关系不变 |
| L4 搜索 | 先在 7.4.3 对 Wagtail `default` 运行 `update_index --backend default`，升级后比较查询 | Page 索引字段兼容；标题/正文、权限、多语言、分页和排序结果符合基线；自建 alias 未被改变 |
| L5 浏览器 | 前台、后台、管理员与低权限编辑者，1440x900 和 390x844 视口 | 无控制台错误、静态资源 404、横向溢出、越权、草稿/媒体/Chooser/Telepath 回归 |
| L6 全量与性能 | `manage.py test -v 2`，在接近生产规模的隔离数据测 p50/p95 与错误率 | 全套通过；p95 相比基线劣化不超过 20%，错误率不高于 1% |

浏览器验收至少覆盖：首页、博客索引和搜索、正文的富文本/Markdown/代码/图片/文档、评论和多语言；后台的页面树、编辑/草稿/预览/发布/历史恢复、StreamField、图片与文档 chooser、Vditor chooser/upload、Snippet 与 ModelAdmin；低权限用户仅能访问已授权页面和 collection。每条关键路径检查网络、控制台、键盘路径和可访问名称。测试环境不得通过真实发布或覆盖正文来“验证”。

### 生产发布批次：仅在单独授权后

1. 主 agent 向用户逐项列出并取得确认：生产依赖安装、MySQL migration、Wagtail Page `update_index`、代码 fast-forward、静态文件处理、具体服务重启和必要的回滚动作。任何一项未确认即停止在生产外。
2. 预检：生产目录、`main`、远程地址、工作树、目标 SHA、服务/队列/ES/alias/磁盘空间及维护窗口均正确；确认 Wagtail Page 索引与 `wagtailblog-prod-content-v002` 等自建内容/日志索引不交叉。
3. 备份并校验可恢复性：MySQL（含迁移状态）、受保护 MongoDB 数据、MinIO 媒体、最小相关 Elasticsearch snapshot（`include_global_state=false`）。保存旧代码 SHA 与旧环境依赖清单，但绝不把凭据写入仓库或方案。
4. 先在旧 7.4.3 代码下执行已授权的 Wagtail Page `update_index --backend default` 并记录耗时、文档计数、权限和多语言样例；若结果异常，停止升级，不触碰自建内容索引。
5. 宣布维护并冻结应用写入：排空相关队列，停止 uWSGI、maintenance Worker 与 Beat，避免旧代码与迁移并发写入。基础设施保持可用。
6. 确认生产工作树干净后，在 WSL2 推送已验证 SHA；生产仅执行 `fetch`、差异检查和 `merge --ff-only origin/main`。用准备好的新环境安装完整依赖，执行已审阅迁移、必要的 `collectstatic`、`pip check`、`manage.py check` 与 `showmigrations`。
7. 启动与验收顺序：基础设施 -> Django/uWSGI -> maintenance Worker -> Beat -> Filebeat（仅日志格式受影响） -> Nginx（仅实际受影响）。核对四服务 active/enabled、`systemctl --failed` 为零、socket/HTTP、后台、Celery ping/registered、Beat 调度、ES health/alias/outbox/Delivery 和日志恢复。

## 6.3 停止条件与决策

下列任一事实出现即停止升级推进，保留旧代码/旧环境/当前 serving alias，并把证据写入实施记录：

- `0098` 迁移图冲突、隔离迁移失败、耗时无法满足维护窗口，或未能验证其 SQL/可恢复性。
- BlogPage、StreamField、`markdown_block` key、Mongo 正文摘要、草稿/live/revision 指针、媒体对象或 rendition 引用发生非预期变化。
- 第三方扩展导入失败，或后台 Telepath/Chooser/ModelAdmin 出现 JavaScript 错误、静态资源 404、草稿丢失或表单保存问题。
- 任何私有 collection、Snippet、Prompt、页面翻译、Markdown API 或低权限账户出现越权读写。
- Wagtail Page 索引字段/结果异常、ES alias 错指、内容 outbox/Delivery 存在未处理状态，或有人提出重建/删除/切换自建内容索引但尚未获得独立授权。
- 性能 p95 比升级基线恶化超过 20%，或错误率超过 1%。
- 生产备份、恢复校验、维护窗口、精确 SHA、用户生产授权或回滚负责人任一缺失。

发生停止条件后的默认回滚决策是：先停止本批次涉及服务，切回上一个已验证 commit 与旧依赖环境，保留新建 API token 表和所有受保护数据。schema 回退、MySQL/ES/Mongo/MinIO 恢复均不是默认动作，须有已验证恢复步骤和新的明确授权。

## 7. 文件范围

### 预计需要修改（实现阶段）

- `requirements.txt`：Wagtail 8.0 及其必需依赖的精确锁定。
- `wagtailblog3/apps/content_ai/wagtail_hooks.py`：显式注册自定义权限策略（若测试确认需要）。
- `wagtailblog3/apps/blog/wagtail_hooks.py` / `wagtailblog3/apps/blog/admin.py`：显式注册只读 Snippet 权限策略（采用最终注册位置）。
- 与本次实际兼容性问题直接相关的测试文件。
- 本方案文档的“实施记录”。

### 本方案明确不修改

- BlogPage、StreamField block 定义、`markdown_block` 存储 key、MongoDB 正文/草稿/revision、媒体对象和页面发布数据。
- Wagtail v3 API 路由、API token 发放、公开 API 契约、前端视觉语言和搜索架构。
- `.env.test`、`.env.production`、凭据、systemd unit、Nginx、uWSGI、Celery 队列定义和 Elasticsearch 容器配置；若服务操作流程发生变化，只更新 `systemctl.md` 的事实记录，不把秘密写入文档。

## 8. 测试与验收门禁

必须同时满足：

- 依赖解析成功，`pip check` 通过；Python 3.13、Django 5.2.8、Wagtail 8.0 版本可读且无意外升级。
- `python manage.py check` 通过；`makemigrations --check --dry-run` 无项目模型新迁移；`migrate --plan` 中的 Wagtail 迁移经审阅并在隔离库成功。
- Wagtail default Page 索引已在 7.4.3 预先 `update_index`，升级后搜索结果、权限过滤和多语言结果一致；自建内容搜索 outbox/alias 无 pending 或越权写入。
- 现有受影响测试和新增回归测试通过；真实后台浏览器检查无 JS 错误、静态资源 404、媒体/权限/表单/Markdown 回归缺陷。
- `compileall` 与 `git diff --check` 通过；若修改 Django/Wagtail 运行时代码，新增中文模块说明、必要 docstring 和可由调用点确认的类型标注，并在实施记录报告未覆盖边界。
- 生产发布前已取得备份、迁移、索引更新、服务重启和回滚的独立确认；本方案本身不替代该确认。

## 9. 回滚点与残余风险

- **代码/依赖回滚**：恢复到升级前已验证 commit 和原依赖锁定，重建测试/生产 Python 环境；不删除数据库表、不删除 MongoDB 正文、不回退媒体对象。
- **迁移回滚**：优先恢复代码并保留新增 API token 表；只有确认 Django 迁移可逆且得到授权时才回退 schema，不能把 `migrate` 回滚当作默认动作。
- **搜索回滚**：恢复 Wagtail 旧代码后继续使用已验证的 Page 索引；自建内容搜索通过既有 alias/target 回滚，不删除 serving 索引。
- **服务回滚**：按 `systemctl.md` 停止本批次涉及服务、恢复旧版本并依序启动，重新执行 Django、HTTP、socket、队列和日志检查。
- **残余风险**：Wagtail 8.0 刚发布，第三方扩展的真实兼容性、未来 8.x 预览 API 稳定性、生产数据量下的索引更新时间和自定义后台权限边界仍需测试证据；未完成前不能宣称可生产发布。

## 10. 模型/推理强度建议

- 事实收集与依赖清单：`gpt-5.6-luna` 中推理，理由是版本、文件和元数据核对属于范围明确的只读工作。
- 兼容性实现与测试：`gpt-5.6-terra` 中/高推理，理由是涉及 Django/Wagtail 运行时代码、第三方依赖和后台回归。
- 生产迁移、索引更新、备份、服务重启和回滚：`gpt-5.6-sol` 高推理并安排独立复核；触发条件是数据库 schema、搜索索引、跨服务一致性或回滚失败风险。
- 所有档位均不得替代 WSL2 测试、依赖解析、备份证据、生产授权和服务健康检查。
- 本方案实际使用：当前会话以可用 Codex 模型完成本地事实核对，并使用 `django-wagtail-development` 技能；未调用外部模型，未发送源码、凭据或生产数据。

## 11. 实施记录

### 2026-08-26：方案阶段完成

- 状态：已完成 Wagtail 8.0 官方发布说明、项目依赖、Wagtail 使用面和 WSL2 测试环境版本核对；未进入实现。
- 协作复核：架构/迁移与生产运维风险由 `gpt-5.6-sol` 高推理复核；测试、浏览器回归和验收矩阵由 `gpt-5.6-terra` 高推理复核。主 agent 已将结论收敛为 WP0-WP7 工作包、串并行边界和停止条件；未向外部服务发送源码、凭据、正文或生产日志。
- 实际修改文件：仅新增本方案文档；未修改业务代码、`requirements.txt`、数据库、索引、服务或环境文件。
- 核对结果：当前 WSL2 环境 `pip check` 通过；Wagtail 8.0 dry-run 解析出 DRF 3.18.0、django-ninja 1.6.3、draftjs_exporter 7.1.0、modelsearch 1.3.2、swapper 1.4.0；项目当前未安装 `django-ninja` 与 `wagtail-ai`。
- 数据/服务影响：无；未执行迁移、`update_index`、真实保存、生产 SSH、服务重启或索引写入。`systemctl.md` 未在方案阶段修改；因其现有“WP8 code-only”段与本次 Wagtail 8 真实升级范围不同，实现前需按 WP6 新增经复核的升级 runbook。
- 回滚点：删除本方案文档即可恢复方案阶段前的工作树；无业务数据回滚动作。
- 未解决风险：第三方扩展的 Wagtail 8 实机兼容性、Wagtail 8 迁移在项目测试库的实际计划、生产 Page 索引更新时间及低权限后台浏览器回归仍待实现阶段验证。

### 2026-08-26：实现批次 WP0-WP5（测试环境）

- 状态：部分完成。已在共享工作树实施 Wagtail 8 依赖和代码兼容修复；生产升级、生产迁移、Page 索引写入、服务重启和 Git 提交/推送均未执行。
- 实际修改文件：`requirements.txt`；`wagtailblog3/apps/blog/admin.py`、`admin_image_upload.py`、`markdown_renderer.py` 及 `test_page_view_admin.py`；`wagtailblog3/apps/content_ai/wagtail_hooks.py`；7 个项目迁移文件将旧 Wagtail 7 节点依赖改为 Wagtail 8 实际提供的 `0096_referenceindex_referenceindex_source_object_and_more`；博客 3 个迁移文件将已移除的 `0097_merge_20250811_1740` 依赖改为 Wagtail 8 的 `0097_baselogentry_uuid_action_timestamp_indexes`；`systemctl.md` 新增 Wagtail 8 待授权 runbook；本方案文档更新实施记录。
- 依赖结果：测试运行时为 Python 3.13.2、Django 5.2.8、Wagtail 8.0、DRF 3.18.0、`draftjs_exporter` 7.1.0、`modelsearch` 1.3.2、`django-ninja` 1.6.3、`swapper` 1.4.0；`pip check` 通过。
- 迁移结果：清理测试 Conda 中不属于 Wagtail 8 RECORD 的旧 `0095_query_searchpromotion_querydailyhits.py`、`0096_remove_searchpromotion_query_and_more.py`、`0097_merge_20250811_1740.py`、`0098_merge_20260603_0945.py` 及已发现的缓存文件（可恢复备份位于 `/tmp/wagtail8-stale-migrations-20260826/`，不在仓库）；当前迁移目录已与 Wagtail 8 RECORD 一致。干净 Wagtail 8 运行时 `migrate --plan` 仅显示 `wagtailcore.0098_apitoken`，`makemigrations --check --dry-run` 无变化，`sqlmigrate` 仅创建 `wagtailcore_apitoken` 表；新建临时测试库可完整建库并完成关键回归。未对现有测试数据库执行迁移。
- 测试结果：`manage.py check`、`compileall -q wagtailblog3 tools`、`git diff --check` 通过；Markdown 兼容 36/36、PageView 管理隔离库 5/5、Markdown 导入解析 11/11、图片格式 3/3、搜索正确性 16/16 通过。临时数据库全量 589 tests 中有 3 个既有错误（测试标签导入导致 app_label 初始化失败）和 2 个既有断言失败（模板静态版本串、日志 traceback 规范），需另立缺陷处理，不归因于 Wagtail 8。
- 数据/服务影响：仅创建并销毁临时测试数据库；未修改现有 `wagtailsoftblog_test`、MongoDB、MinIO、Elasticsearch、Redis 或生产服务。未执行 `update_index`、真实正文保存、发布或 API token 写入。
- 回滚点：代码可回退至升级前 commit；测试环境孤儿迁移文件可从临时备份恢复。迁移依赖改写只影响迁移图，不自动执行 DDL；生产仍需先核对 `0096_referenceindex`/`0097_baselogentry` 记录和备份。
- 未解决风险：第三方扩展真实后台兼容性和 Playwright 回归尚未完成；Wagtail default Page 索引需在 7.4.3 隔离环境先 `update_index` 再比较；全量测试既有失败需确认是否为基线；生产依赖安装、迁移、索引、停机窗口和服务重启均等待用户单独授权。

### 2026-08-27：生产预检与发布候选

- 状态：生产只读预检完成。生产仓库在 `main`、工作树干净，服务、基础设施和内容 read alias 均可用；当前未同步、未备份、未迁移、未写入索引、未停止或重启生产服务。
- 生产迁移影响：生产 Wagtail 7.4.3 的迁移记录缺少 Wagtail 8 所需的 `0096_referenceindex_referenceindex_source_object_and_more` 与 `0097_baselogentry_uuid_action_timestamp_indexes`。升级后须审阅这两个索引迁移及 `0098_apitoken` 的完整 SQL，不能假定只创建 API token 表。
- 发布门禁：提交并推送后，仍需先执行旧 Wagtail 7.4.3 的 `update_index --backend default`、取得并验证 MySQL/MongoDB/媒体/Elasticsearch 备份、冻结应用写入，再安装生产依赖和执行已审阅迁移。该批操作由用户当前部署授权覆盖，但任何预检或备份失败均停止。
