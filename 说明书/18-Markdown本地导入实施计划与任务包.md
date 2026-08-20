# Markdown 本地导入实施计划与任务包

> **执行要求：** 使用 `superpowers:executing-plans` 按任务逐项实施，并遵守 TDD 的 RED、GREEN、REFACTOR 循环。本文使用 checkbox 跟踪；未经用户另行授权，不提交、推送、部署、执行生产写入或应用迁移。

**目标：** 在现有 Django 5.2 / Wagtail 7.4 架构内提供受认证的 Markdown 草稿导入 API 和 Windows CLI，把 Markdown、图片、音视频、流媒体链接与 Mermaid 按原顺序导入为 BlogPage StreamField 块，同时保证幂等、逐媒体失败隔离和可追溯补偿。

**架构：** 客户端负责读取本地文件、解析相对路径、安全下载经用户确认的远程图片、预检与 multipart 组装；服务端负责权限、幂等、最终媒体校验、Wagtail 对象创建、页面草稿组装及补偿。解析阶段先产出与数据库无关的中间表示，服务端只消费显式 artifact ID，不接收客户端文件系统路径，也不按 URL 主动抓取互联网资源。

**技术栈：** Python 3.13、Django 5.2.8、Wagtail 7.4.2、Django REST Framework 3.16.1、markdown-it-py 3.0、Wagtail images/media、MySQL、MongoDB、MinIO、Celery maintenance 队列。

---

## 1. 执行边界与验收口径

- 输入是一个 UTF-8 Markdown 文件、明确的 `source_root`、目标 BlogIndexPage、UUIDv4 幂等键和用户确认选项。
- 输出只能是未发布 BlogPage 草稿；正文按原顺序为 `markdown_block`、`image_block`、`video_block`、`audio_block`、`embed_block`、`mermaid_chart` 等既有块。
- Mermaid 围栏的代码只写入 `mermaid_chart.code`；普通代码围栏、Markdown/HTML 表格和公式保留在 `markdown_block`。
- 单个媒体失败生成独立纯文本 `markdown_block`：`[导入缺失：{媒体类型} 原始引用：{安全化引用} 原因：{错误码}]`。其他成功媒体不回滚。
- 单媒体补偿只删除该 artifact 明确记录的半上传 object/未引用模型行；页面级最终组装失败才补偿本批所有未引用的新建媒体。
- P1 仅在同一导入批次内复用规范化来源；不跨文件或批次复用。不同 URL 或路径即使内容相同也不合并。
- 本计划不修改 StreamField 现有 key、MongoDB 正文契约、生产环境文件、systemd unit、队列、Nginx、Elasticsearch 或既有正文。

## 2. 模型与 Token 分配

### 2.1 依据与限制

2026-08-17 尝试读取 OpenAI 官方模型页 `https://developers.openai.com/api/docs/models`、`https://developers.openai.com/api/docs/guides/model-selection` 与 `https://platform.openai.com/docs/models`，当前 Fetch 均返回连接失败。本任务因此按 OpenAI Docs 技能随附的非权威降级快照和项目 `AGENTS.md` 分配：`gpt-5.6-sol` 偏质量/复杂推理，`gpt-5.6-terra` 平衡质量、延迟与成本，`gpt-5.6-luna` 偏高吞吐与低延迟。该映射不是本次实时官网核验结果；涉及价格、账户可用性或别名时必须重新打开官方页面确认。

### 2.2 Token 节省规则

- 先由 luna 做定向检索、样本分类、格式检查和重复测试，不把整个仓库、正文、日志或数据库发给模型。
- terra 只接收当前任务需要的接口、测试和相邻代码；常规实现默认中推理，连续两次定位失败或出现跨模块契约才提高到高推理。
- sol 只处理权限/SSRF、并发幂等、数据库与对象存储补偿、MongoDB/revision 一致性、迁移与生产门禁；完成设计或复核后回到 terra/luna。
- 每个任务包限定输入文件、预期输出和验证命令；已有可靠证据不做重复全库扫描。
- 外部模型不得接收凭据、Token、生产日志、MongoDB 正文/草稿或个人数据。

### 2.3 任务分配表

| 包 | 任务 | 主模型 | 推理 | 升级条件 | 验证门禁 |
| --- | --- | --- | --- | --- | --- |
| T0 | 基线与样本清单 | luna | 低/中 | 样本语义存在冲突时交 terra | Git 状态、版本、块字段、样本特征清单 |
| T1 | 纯解析器与中间表示 | terra | 中 | 源位置/引用语法无法稳定保持时提高推理 | 纯单元测试，无数据库/网络写入 |
| T2 | 客户端路径、URL 与远程图片安全 | terra；sol 安全复核 | 中/高 | SSRF、重定向、DNS 重绑定 | 私网/回环/元数据地址拒绝测试 |
| T3 | batch/artifact、迁移、幂等并发 | sol | 高 | 唯一约束或并发重放不确定 | migration 检查、并发测试、409 契约 |
| T4 | Wagtail 媒体校验与逐媒体补偿 | sol | 高 | MinIO 异常或跨 storage alias | 精确 object、其他媒体保留测试 |
| T5 | BlogPage 草稿、revision、MongoDB 补偿 | sol | 高 | 页面树/正文指针一致性 | 最终失败批次补偿测试，不自动发布 |
| T6 | 认证、授权与导入 API | sol 复核、terra 实现 | 中/高 | 令牌生命周期或权限边界变化 | 未认证/越权/collection 权限测试 |
| T7 | Windows CLI 与 dry-run | terra | 中 | 大文件/中断恢复成为 P1 必需 | 合成 fixture、超时重试、脱敏输出 |
| T8 | cleanup retry 与运维文档 | sol | 高 | 新任务影响 Worker/Beat 路由 | maintenance 注册、幂等、`systemctl.md` |
| T9 | 集成、浏览器与数据清理验收 | terra；sol 数据复核；luna 重复检查 | 中/高 | 真实测试端点无法证明隔离 | test_run_id 精确清理及残留为零 |
| T10 | 交付复核 | luna/terra | 低/中 | 生产发布另开 sol 门禁 | diff/check/测试/文档/回滚清单 |

## 3. 文件结构

预计创建：

- `wagtailblog3/apps/blog/services/markdown_import_types.py`：无 Django 依赖的中间表示和错误码。
- `wagtailblog3/apps/blog/services/markdown_import_parser.py`：Markdown 块级识别和顺序切分。
- `wagtailblog3/apps/blog/services/markdown_import_paths.py`：客户端路径规范化和 source root 边界。
- `wagtailblog3/apps/blog/services/markdown_import_remote.py`：远程图片安全下载策略。
- `wagtailblog3/apps/blog/services/markdown_import_media.py`：服务端 Wagtail 媒体创建和单 artifact 补偿。
- `wagtailblog3/apps/blog/services/markdown`：幂等、批次状态与草稿组装。
- `wagtailblog3/apps/blog/markdown_import_api.py`：DRF 请求/响应、认证和授权边界。
- `wagtailblog3/apps/blog/markdown_import_urls.py`：导入 API 路由。
- `tools/markdown_import/`：Windows CLI、HTTP 客户端和本地预检。
- `wagtailblog3/apps/blog/test_markdown_import_*.py` 与 `tools/markdown_import/tests/`：单元和集成测试。

预计修改：

- `wagtailblog3/apps/blog/models.py`：仅新增 batch/artifact 审计模型；不改 BlogPage.body 定义。
- `wagtailblog3/apps/blog/migrations/0025_*.py`：新增模型和唯一约束。
- `wagtailblog3/apps/blog/wagtail_hooks.py` 或项目 URL 配置：注册受认证 API。
- `wagtailblog3/apps/blog/tasks.py`、`wagtailblog3/settings/database.py`：仅在 T8 注册 maintenance 补偿任务和 Beat 调度。
- `说明书/06-API接口文档.md`、本方案、本文和 `systemctl.md`：接口、实施记录与运维说明。

明确不修改：既有块 key、现有 MongoDB 正文、生产环境文件、unit 文件、Nginx/ES 配置和既有页面数据。

## 4. 任务包

### T0：基线冻结

- [x] 核对 `main`、`origin/main`、工作树、依赖版本和最新迁移编号。
- [x] 核对 BlogPage 已有 `markdown_block/image_block/audio_block/video_block/embed_block/mermaid_chart`。
- [x] 核对 Mermaid 的 `code/renderer` 与 embed 的 `title/embed_url` 字段。
- [x] 核对 `测试页面.md` 的 Markdown、表格、公式、围栏和媒体样本，不复制正文到方案。
- [ ] 在每个后续批次开始前重新执行 `git status --short --branch`，避免覆盖用户改动。

### T1：纯解析器与中间表示

**文件：** 创建 `services/markdown_import_types.py`、`services/markdown_import_parser.py`、`test_markdown_import_parser.py`。

- [x] 先写失败测试：普通 Markdown/普通代码围栏完整保留，Mermaid 独立为 `mermaid_chart` 且代码不出现在任何 Markdown 块。
- [x] 运行 WSL2 定向测试并确认因模块不存在而 RED。
- [x] 实现只依赖 markdown-it-py 和标准库的不可变中间表示与最小解析器。
- [x] 再写失败测试：独占段落图片、本地 audio/video HTML、支持的流媒体链接按顺序切块；行内链接和复杂表格保持 Markdown。
- [x] 实现标准 Markdown 图片、引用式图片解析和独占段落流媒体识别；不下载、不上传、不访问网络。
- [x] 运行定向测试确认 GREEN；执行 `git diff --check`。

验收：无空 Markdown 块；源码顺序稳定；普通 fenced code 不误判；解析过程不创建 Django/Wagtail/MongoDB/MinIO 数据。

### T2：客户端路径与远程图片安全

**文件：** 创建 `markdown_import_paths.py`、`markdown_import_remote.py` 及对应测试。

- [x] RED：覆盖路径逃逸、符号链接逃逸、绝对路径、`file:/javascript:/data:`、不可读文件和同批规范化去重。
- [x] GREEN：所有本地路径解析后必须位于真实 `source_root` 内；API manifest 只含规范化来源与安全文件名。
- [x] RED：覆盖 HTTP、用户信息 URL、回环/私网/链路本地/云元数据地址、重定向后变私网、响应超限和显式授权。
- [x] GREEN：只在 `--allow-external-images` 下下载 HTTPS 图片，每跳重新解析并校验目标地址，连接固定到已校验 IP，写入受控临时目录，失败不保留半文件。
- [x] 完成 SSRF、DNS 解析时序和临时文件清理的高风险复核；修正 resolver 注入接口后复跑安全测试。

### T3：批次、artifact、迁移与幂等

**文件：** 修改 `models.py`；创建 `0025_markdownimportbatch_markdownimportartifact.py` 和模型测试。

- [x] RED：UUIDv4、`(user_id, idempotency_key)` 唯一约束、状态机、请求指纹与归属隔离。
- [x] GREEN：新增最小字段与数据库约束；指纹只包含稳定 manifest/选项/目标，不包含临时路径和 multipart 边界。
- [x] RED/GREEN：同键同指纹串行或并发只创建一个 batch；同键不同指纹返回 `idempotency_conflict`；无关 `IntegrityError` 不得误判为幂等竞争。页面唯一创建由 T5/T6 在批次认领之后验证。
- [x] 在 WSL2 运行 `makemigrations --check --dry-run` 与 `migrate --plan`；未应用到共享测试库或生产。真实数据库并发竞争验证留到获得隔离测试写入授权后执行。

### T4：媒体最终校验与逐媒体补偿

**文件：** 创建 `markdown_import_media.py` 及 image/audio/video 测试。

- [x] 复用 Wagtail image form、collection 权限和运行时限制，重新检查签名、解码、MIME、扩展名、像素和大小。
- [x] 音视频通过现有 wagtailmedia 模型/表单校验；没有可信深度探测器时失败关闭，不凭扩展名放行。
- [x] 每个 artifact 独立执行“创建、记录精确 storage alias/object name、生成块”流程。
- [x] 模拟第 7 个媒体失败：只把该 artifact 标为 `failed_missing` 并清理它自己的半成品；第 1-6、8-10 个保持成功。
- [x] 删除失败时进入 `cleanup_retry`，保留精确证据；不得按前缀、标题或存储桶扫描删除。

### T5：草稿组装与页面级补偿

**文件：** 创建 `markdown_import_service.py` 和服务测试。

- [x] 把解析计划与成功媒体映射为 Wagtail StreamField；失败媒体在原位变为独立缺失 Markdown 块。
- [x] 使用 Wagtail 页面/revision 生命周期创建未发布草稿，不直接改 live 字段。
- [x] 复用项目 MongoDB 正文适配器；记录新建 mongo_content_id 和 revision/page ID。
- [x] 页面级最终失败时，只补偿本批新建且未被任何页面/revision 引用的成功媒体和文档；页面删除失败会阻止依赖清理并保留重试证据。
- [x] 验证 Mermaid 只写 `code`，embed 写 `title/embed_url`，图片/音视频写对应 chooser 值。

### T6：认证、授权与 API

**文件：** 创建 API/URL/测试；更新 `说明书/06-API接口文档.md`。

- [x] GET limits/destinations：只返回用户可导入的父页和服务器实际限制；collection 由 Wagtail 媒体表单最终校验。
- [x] POST preview：验证 manifest 和目标权限，不创建媒体、页面、revision 或 Mongo 文档。
- [x] POST imports：验证认证、导入权限、父页 add 权限、collection 表单校验、幂等键和 multipart artifact 对应关系。
- [x] 响应区分 `success`、`partial_success`、`processing`、`failed`、`cleanup_retry`、`idempotency_conflict`；错误内容脱敏。
- [x] 不接受“只有远程 URL、没有客户端文件”的媒体，不允许服务端抓取互联网。

### T7：Windows CLI

**文件：** 创建 `tools/markdown_import/` 和测试。

- [x] 实现 `inspect`：解析文件、列出块计划、缺失/外部资源、元数据和本地文件体积，不写远端。
- [x] 实现 `import`：要求目标、UUIDv4 幂等键；远程图片只有显式 flag 才下载。
- [x] 网络超时重试复用同一个幂等键；日志不输出 Token、正文、绝对本地路径或响应敏感字段。
- [x] 使用合成 Markdown fixture 覆盖 front matter、媒体和临时目录清理，不把用户正文提交进仓库。

### T8：补偿重试与 systemctl.md

**文件：** 修改 `tasks.py`、`settings/database.py`、`systemctl.md` 和任务测试。

- [x] cleanup 任务只消费明确 artifact UUID，路由到现有 `maintenance` 队列。
- [x] 重复投递、object 已不存在、模型行已删除均可收敛；有限退避后保留失败审计。
- [x] Beat 只扫描明确 `cleanup_retry` 且下一次重试时间到期的记录；不创建新 Worker/unit。
- [x] 文档登记任务名、路由、幂等性、依赖、健康检查和回滚；代码发布时按影响重启网站、maintenance Worker 和 Beat。

### T9：集成与浏览器验收

- [x] 只读核验 `WAGTAILBLOG_ENV=test` 以及管理命令启动时展示的 MySQL/MongoDB/Redis/MinIO 测试端点。
- [x] 以唯一 `test_run_id` 创建少量草稿和媒体；全程记录精确 ID/object name。
- [x] 测试成功、部分成功、幂等重放、页面级失败和 cleanup retry。
- [x] 测试结束按引用关系逆序精确清理，并逐项验证 page/revision/Mongo/media/object 不存在；本次无残留。
- [x] Playwright 后台桌面/移动验收已完成；公开前台未宣称通过：测试页按约定保持未发布，直接 URL 为 404，Wagtail 编辑预览因 Mongo 正文未回填 SQL 表单而返回“预览不可用”。产物写入 `output/playwright/markdown-import/`。

### T10：交付与发布门禁

- [x] 运行定向测试、`python manage.py check`、相关全量测试、`makemigrations --check --dry-run`、`migrate --plan`、`git diff --check`。
- [x] 更新方案和本文实施记录，列明实际文件、结果、数据/服务影响、回滚点和残余风险。
- [x] 未经用户明确授权不提交、推送或部署；生产迁移、环境、服务重启和首次真实导入分别再次确认。
- [ ] 获准发布时仅部署已验证 commit，核对本地/远程/生产精确 SHA，并按 `systemctl.md` 验收。

## 5. 回滚点

- T1/T2/T7 为纯代码与本地测试，可回滚对应文件，不涉及数据。
- T3 迁移在未应用时直接移除；若未来已应用，必须先确认表中无审计数据再制定独立反向迁移，不能直接删表。
- T4/T5/T6 关闭 API 可阻止新导入；已成功创建的草稿和媒体不因代码回滚自动删除。
- cleanup retry 必须继续保留到精确对象确认删除，不能以删除审计行代替实际补偿。
- 生产回滚目标是发布前精确 commit；数据库/MongoDB/MinIO 回滚必须使用本批证据，不触碰既有正文或共享媒体。

## 6. 当前实施记录

- 2026-08-17，状态：T0 完成，任务包已建立，T1 准备实施。基线 `HEAD=origin/main=1c8094c`，工作树仅有未跟踪方案文档；依赖为 Django 5.2.8、Wagtail 7.4.2、DRF 3.16.1、markdown-it-py 3.0.0，最新 blog 迁移为 0024。未执行数据库/MongoDB/MinIO 写入、迁移、服务操作、Git 提交、推送或部署。
- 2026-08-17，模型实际使用：当前会话使用实际可用主模型完成基线与任务包，未调用外部模型或发送源码/正文。官方模型页面 Fetch 失败，模型分工使用 OpenAI Docs 随附非权威快照与项目 AGENTS.md，后续涉及实时价格或可用性时必须重新核验官网。
- 2026-08-17，状态：T1 完成。新增纯解析中间表示、Markdown 块级解析器和 8 项 `SimpleTestCase`。第一轮 RED 因解析器模块不存在失败；第二轮 RED 有 3 项按预期失败；实现并修正引用式图片分隔空行后，WSL2 定向测试 8/8 通过，`git diff --check` 通过。解析器只读取内存字符串，不访问网络，不创建 MySQL、MongoDB、MinIO 或 Wagtail 数据；当前修改未提交。回滚点为删除本批三个新增 Python 文件并撤销本文 T1 记录。
- 2026-08-17，状态：T2 完成。新增本地媒体路径解析和远程图片安全下载模块及 10 项 `SimpleTestCase`。RED 因两个模块不存在失败；GREEN 覆盖目录/符号链接逃逸、Windows 绝对与 UNC 路径、不可读文件、规范化复用、仅 HTTPS、URL 凭据拒绝、回环/私网/链路本地/元数据地址、混合 DNS 答案、重定向重新校验、显式外部图片授权、响应上限、半文件清理、图片真实解码和安全文件名。下载连接固定到已校验公网 IP，TLS 仍使用原域名校验证书与 SNI。WSL2 定向测试 10/10、Python 编译检查和 `git diff --check` 均通过。未访问真实远程 URL，未写 MySQL、MongoDB、MinIO 或 Wagtail 数据，当前修改未提交。回滚点为删除 T2 的四个新增 Python 文件并撤销本文 T2 记录。
- 2026-08-17，状态：T3 代码与迁移完成，隔离数据库并发验证待授权。新增 `MarkdownImportBatch`、`MarkdownImportArtifact`、迁移 `0025_markdown_import_batch_artifact.py`、UUIDv4/规范请求指纹及批次认领服务。RED 因幂等冲突与认领接口不存在失败；GREEN 共 10/10 项测试通过，覆盖首次认领、同键同指纹复用、同键异指纹冲突、唯一约束竞争收敛，以及无法读到竞争批次时透传原始 `IntegrityError`。T1-T3 联合回归 28/28 通过，`manage.py check` 无问题，`makemigrations --check --dry-run` 为 `No changes detected`，`migrate --plan` 仅列出本次两张表、两个索引和两个普通复合唯一约束；Wagtail 既有 `WorkflowState` 条件唯一约束产生 MySQL `models.W036` 警告，与本次约束无关。`git diff --check` 通过。未应用迁移，未写 MySQL/MongoDB/MinIO，未提交、推送或部署；回滚点为删除迁移和 T3 新增服务/测试并撤销 `models.py` 的两类审计模型。残余风险是真实 MySQL 并发插入尚未在隔离数据库验证，页面只创建一次由 T5/T6 在批次认领后继续验证。
- 2026-08-17，状态：T4 部分实现后按用户要求暂停，尚未完成双重审查，不得标记 T4 完成。新增 `services/markdown_import_media.py` 和 `test_markdown_import_media.py`，并修订 `models.py` 与尚未应用的迁移 `0025_markdown_import_batch_artifact.py`。当前落盘实现包含 Wagtail 图片/媒体表单入口、结构化音视频探测契约、单媒体失败缺失块、精确 storage alias/object name/SHA-256 证据、媒体结果与 cleanup 正交状态、模型行与对象的幂等清理，以及第 7 个媒体失败时保留其余成功媒体的控制流。第二轮规格审查曾发现 storage 改名碰撞可能误删既有 planned object、校验异常会中断后续媒体、Django `DefaultStorage` wrapper 与 registry backend 身份不一致、引用保护异常未进入 retry、音视频深度探测不足及 cleanup 扫描状态混用；本轮代码已落盘对应修复和测试，但仍待重新规格复审。
- 2026-08-17，T4 TDD/验证记录：修复批次先取得 T3+T4 共 39 项测试中的 6 个失败和 8 个错误，覆盖上述 Critical/Important 场景；实现代理随后报告同一组 39/39 通过。该 GREEN 结果尚未由主代理在网络中断后独立复跑；真实 Wagtail `DefaultStorage` wrapper/backend 等价的无写入回归测试、最新 T1-T4 联合测试、`manage.py check`、迁移检查和最终 `git diff --check` 也尚未形成完整的新一轮证据。此前 T4 旧版本定向测试为 21/21，通过结果不能替代当前代码的最终验收。恢复实施时必须先检查当前 diff，再完成这些验证，随后依次进行规格复审和独立代码质量审查；所有 Critical/Important 关闭后才可勾选 T4 并进入 T5。
- 2026-08-17，T4 环境与残余风险：测试 Conda 环境只有 `filetype`，没有 `ffprobe`、FFmpeg、PyAV、Mutagen、MediaInfo 或同等 codec/container 深度解析器。当前默认音视频探测因此采用 `media_deep_probe_unavailable` 失败关闭；只有注入返回有效 MIME/container/codec 的结构化可信探测器时才继续表单校验。是否新增生产依赖、如何安装及如何验证必须另行在方案中确认，不能把文件签名/MIME 家族判断表述为 codec 深度校验。未连接或写入真实 MySQL、MongoDB、MinIO，未应用迁移，未创建真实草稿或媒体，未执行对象删除、服务操作、Git 提交、推送或部署；`systemctl.md` 尚无需修改。当前回滚点为删除 T4 媒体服务与测试，并撤销 T4 对模型和未应用迁移的增量修改；不得触碰 T1-T3 文件或任何既有内容。
- 2026-08-17，暂停点与模型实际使用：T4 使用高复杂度实现/复核代理处理对象存储补偿和 Wagtail storage 契约，符合任务表的 `sol + 高推理` 角色；未调用外部模型服务，也未发送源码、正文、凭据或生产日志。用户要求在再次明确继续前停止实现；后续恢复顺序固定为“核对工作树与未完成测试 -> 完成 T4 GREEN 验证 -> 规格复审 -> 代码质量复审 -> 更新本记录 -> T5”，不得跳过门禁。
- 2026-08-18，状态：T4 完成。恢复后先以只读方式核对 `main` 工作树、未跟踪文件和任务包，再独立运行 T4 定向测试 31/31、T1-T4 联合测试 59/59、`python manage.py check`、`makemigrations --check --dry-run`（`No changes detected`）、`migrate --plan` 和 `git diff --check`，全部通过；迁移仍未应用，未写入 MySQL、MongoDB、MinIO，未创建真实草稿或媒体。规格复审确认逐媒体失败不阻断其他媒体、缺失标记为独立 `markdown_block`、storage alias/object name/SHA-256 与模型证据可精确补偿，storage wrapper 等价检查和音视频失败关闭均有测试覆盖。代码质量复审通过 Python 编译和针对性静态检查，未发现 T4 Critical/Important 阻断。模型实际使用：本次恢复由当前主代理按任务包 T4 的 `sol + 高推理` 门禁完成独立复核，未调用外部模型服务或发送源码、正文、凭据、生产日志；后续 T5/T6 继续按任务包分配执行。回滚点为关闭导入 API 并回退 T4 媒体服务/测试及对应模型与未应用迁移增量；已成功创建的草稿和媒体不会因代码回滚自动删除。
- 2026-08-18，状态：T5-T7 完成。T5 新增 StreamField 组装、front matter 元数据传递、标签设置、未发布页面/revision 创建和页面失败依赖阻断补偿；T6 新增认证 API 的 limits/destinations/preview/import 路由，加入块与 artifact 类型校验、processing/partial/cleanup_retry 状态、UUIDv4 指纹序列化、collection 表单入口和页面级精确补偿；T7 CLI 支持 inspect/import、YAML front matter 白名单、远程图片显式确认、超时重试复用幂等键、临时文件/目录清理和脱敏错误输出。新增 API/服务/CLI 回归测试与合成 fixture，当前 T5/T6/CLI 定向测试 15/15 通过，T4 媒体测试 31/31 仍通过，Python 编译检查和 `git diff --check` 通过。未应用 0025 迁移，未写真实 MySQL、MongoDB、MinIO，未创建真实草稿或媒体，未执行服务操作、Git 提交、推送或部署。模型实际使用：T5 按任务包的 `sol + 高推理` 复杂一致性门禁由当前主代理复核，T6/T7 按 `terra + 中推理` 完成常规实现与测试；未调用外部模型服务。回滚点为回退 T5-T7 新增服务/API/CLI/测试及本批文档，不触碰 T1-T4 既有媒体安全实现。
- 2026-08-18，状态：T8 完成。新增 `blog.tasks.cleanup_markdown_import_artifact` 和 `blog.tasks.dispatch_markdown_import_cleanup_retries`，复用 `maintenance` 队列和现有 maintenance Worker/Beat；新增 artifact cleanup 尝试次数与下一次退避时间字段，并同步未应用迁移 0025。任务测试 4/4 通过，覆盖明确 UUID、重复/对象或模型缺失收敛、有限退避后保留 retry 审计、Beat 仅投递到期 artifact ID；`manage.py check`、`makemigrations --check --dry-run`（`No changes detected`）、`migrate --plan` 和 `git diff --check` 通过。更新 `settings/database.py` 路由/Beat 计划和 `systemctl.md` 拓扑、依赖、健康检查与回滚说明；未应用迁移，未写真实数据库/对象存储，未重启服务、提交、推送或部署。模型实际使用：按任务包 T8 使用当前主代理完成 `sol + 高推理` 数据保护与运维复核，未调用外部模型服务。回滚点为回退 tasks/settings/migration/model 增量和 systemctl 文档，同时保留已有 cleanup retry 审计行。
- 2026-08-18，状态：T6/T7 细化修正。服务端新增 embed 平台 HTTPS/域名/端口校验、非空简介校验，以及 multipart `size_bytes`/`sha256` 重新计算；客户端 manifest 计算文件摘要并纳入幂等指纹。新增对应 API/CLI 测试，导入相关联合测试最终为 79/79 通过；未写真实数据或对象。
- 2026-08-18，状态：T9 部分完成、T10 完成（未发布）。只读核对测试环境配置并运行导入相关联合测试 78/78、`manage.py check`、`makemigrations --check --dry-run`（`No changes detected`）、`migrate --plan` 和 `git diff --check`；未获测试数据库/MongoDB/MinIO 写入授权，未创建或清理真实 `test_run_id` 草稿、媒体、对象，未执行生产服务或 Playwright 浏览器写入验收。T10 方案、任务包、API 文档和 `systemctl.md` 已更新，工作树保持未提交状态；剩余 T9 风险是需要单独授权后才能验证真实 Wagtail 表单、Mongo revision、MinIO object、cleanup retry 和桌面/移动后台渲染。模型实际使用：T9 只读复核按 `terra + 中推理` 范围执行，未调用外部模型服务；T10 文档/门禁复核按 `luna/terra` 角色执行。回滚点为本批未提交文件清单，未产生需要数据回滚的写入。
- 2026-08-18，状态：T9 只读回归复核完成，真实写入与浏览器验收仍待单独授权。WSL2 `wagtailblog-test` 环境重新运行导入相关测试 76/76 通过，覆盖解析、路径/SSRF、批次幂等、媒体补偿、StreamField 组装、认证 API、Windows CLI 和 cleanup retry；启动输出确认测试端点为测试 MySQL `wagtailsoftblog_test`、MongoDB `wagtailblog_test`、Redis `192.168.20.2:6379`、MinIO `wagtail-test-bucket` 和测试 Elasticsearch。`python manage.py check` 通过；`makemigrations --check --dry-run` 返回 `No changes detected`；`migrate --plan` 仅列出未应用的 `blog.0025_markdown_import_batch_artifact`，未执行迁移；Python 编译检查和 `git diff --check` 通过。未创建或删除真实 BlogPage 草稿、revision、MongoDB 正文、Wagtail 媒体或 MinIO object，未启动 Playwright，未重启服务，未提交、推送或部署。已有 MySQL `wagtailcore.WorkflowState models.W036` 警告与本次迁移无关。T9 剩余验收仍包括：获授权后用唯一 `test_run_id` 做成功/部分成功/幂等重放/页面失败/cleanup retry 写入测试，按精确 ID/object name 逆序清理并核验残留为零，以及桌面/移动后台和前台浏览器验收。模型实际使用：本轮只读回归按 `gpt-5.6-terra + 中推理` 角色执行，文档更新按 `gpt-5.6-luna` 角色执行，未调用外部模型服务。
- 2026-08-18，状态：T9 集成写入、精确清理与后台浏览器验收完成，前台公开渲染保留为已知限制。测试环境唯一 `test_run_id=73a60b64-16d8-4058-a199-08e0f26acbbc` 创建成功、部分成功、幂等重放、页面失败和 cleanup retry 场景；成功页 `580`、部分成功页 `581`，批次为 `adbeb194-0aec-4dd7-bbad-1a955d4bb317`、`82f8f183-7bb1-4cf6-b38a-b2a21f0a496c`、`1303e625-221f-4596-9501-a3d04523484a`。逐项删除并核验 3 个批次、2 个页面、4 个 artifact、3 个 Wagtail 图片对象、2 个 Mongo revision 指针和 3 个 MinIO object 均不存在；失败 artifact 的 cleanup retry 首次保留、再次收敛为 cleaned。Windows 主机 Playwright `t9` 会话完成后台桌面/移动视口检查：块顺序显示为 `markdown_block → image_block → embed_block → mermaid_chart`，移动视口 `scrollWidth=clientWidth=390` 且 `overflow-x=hidden`；站内请求成功，唯一控制台错误为外部 Gravatar 被网络重置。公开 URL 因页面保持未发布返回 404，编辑预览端点返回“预览不可用”，未发布页面未被强行发布。产物：`output/playwright/markdown-import/t9-admin-desktop.png`、`t9-admin-mobile.png`、`t9-front-mobile.png`、`t9-preview-mobile.png`。清理后停止 WSL2 测试服务器，未触碰生产、未提交、推送或部署。最终导入相关回归 77/77，`manage.py check`、`makemigrations --check --dry-run`、`migrate --plan`（无待执行操作）和 `git diff --check` 通过；已有 MySQL `WorkflowState` 条件唯一约束 W036 警告与本次无关。模型实际使用按 T9 门禁执行：浏览器/集成验证采用 `gpt-5.6-terra + 中推理` 角色，精确数据清理按 `gpt-5.6-sol + 高推理` 数据保护门禁复核，文档整理按 `gpt-5.6-luna` 角色；未调用外部模型服务。
- 2026-08-18，状态：T7 Windows CLI 路径与 URL 编码路径修正。修复 `tools/markdown_import/client.py` 将 `wagtailblog3/apps` 正确加入 `sys.path`，并让本地媒体路径先 URL 解码再执行 source-root 安全校验；此前 Windows 直接运行 CLI 会报 `ModuleNotFoundError: No module named 'blog'`，URL 编码的中文目录会误报 `file_missing`。Windows CLI 相关测试 16/16 通过，URL 编码路径和解码后逃逸测试均覆盖。实际预检 `第八章.md` 返回 `block_count=59`、`media_count=29`、29 个本地图片全部解析成功且 `errors=[]`；`测试页面.md` 仍是方案说明/示例文本，预检为 1 个 Markdown 块、0 个媒体块。未创建网站数据，未触碰生产。

### T11 Windows EXE 导入向导（方案与实现中）

- **模型/推理强度建议**：界面流程与可用性核对使用 `gpt-5.6-luna + 中推理`；Tkinter 客户端、PyInstaller 配置和针对性测试使用 `gpt-5.6-terra + 中推理`；只有涉及认证边界、重复提交或打包后导入链路不一致时升级 `gpt-5.6-sol + 高推理`。验证门禁仍以 Windows 实机运行、API 测试环境和不写入生产为准。
- **目标**：提供一个可从 Windows 直接启动的 EXE 向导，减少用户手工复制 PowerShell 命令。
- **流程**：输入网站地址与 JWT -> 测试连接并读取限制 -> 选择可写 BlogIndexPage -> 扫描 EXE 所在目录的顶层 `*.md` -> 选择文件 -> 本地预检/填写简介 -> 显式确认远程图片下载 -> 导入未发布草稿 -> 显示 batch/page/revision 和后台编辑地址。
- **范围**：GUI 只复用 `tools/markdown_import/client.py` 的解析、预检、multipart 上传和重试；不新增发布按钮、不保存 JWT、不修改生产配置。网站输入只填 `IP:端口` 时自动补 `/zh-hans`，也接受用户填写完整语言前缀 URL。
- **扫描规则**：默认扫描 EXE 所在目录的顶层 Markdown 文件；相对图片、音频、视频路径以该目录为 `source_root`，不接受目录外路径。远程图片仅支持 HTTPS 且必须显式勾选下载。
- **数据与服务影响**：预检只读本地文件和 limits/destinations API；导入会在测试站点创建未发布 BlogPage、revision、Wagtail 媒体和 MinIO 对象，按现有 batch/artifact 清单管理。客户端关闭后内存中的 token 被释放，不写配置、日志或 Git。
- **回滚点**：GUI 文件仅为客户端入口，删除 GUI 文件即可回退；导入产生的数据按 T9 测试清理协议，以精确 batch/page/revision/media/object 清理，不使用全库删除。
- **验收**：Windows 启动、连接测试、索引选择、扫描 `第八章.md`、预检显示块数/媒体数/错误、远程图片确认、导入结果、键盘操作和长文件名不溢出；PyInstaller 构建检查不提交 `dist/` 和 `build/`。

### T11 实施记录

- 2026-08-18，状态：代码与本地打包完成，未提交、未推送、未部署。新增 `tools/markdown_import_gui.py`、`tools/markdown_import_gui.spec`、`tools/build_markdown_import_exe.ps1` 和 `tools/markdown_import_gui_test.py`；`tools/markdown_import/client.py` 增加 GUI 元数据覆盖入口，命令行默认行为不变。
- 2026-08-18，T11 修正：用户实测发现结果页把语言前缀拼进了 Wagtail 后台地址，生成 `/zh-hans/admin/...` 并被前台通配路由返回 404。根据 `wagtailblog3/urls.py`，后台入口是根路径 `/admin/`；新增 `admin_edit_url()` 和回归测试，重新构建 EXE。旧 EXE 需替换为最新产物，已导入的页面无需重复导入。

### T12 HTML 媒体语法与内置音视频探测（已完成）

- **模型/推理强度建议**：HTML/Markdown 语法扩展和样本统计使用 `gpt-5.6-luna + 中推理`；解析器、MP3/MP4 容器轨道探测和 API 接入使用 `gpt-5.6-terra + 中推理`；涉及伪装媒体、安全边界、Wagtail 表单和对象存储写入时升级 `gpt-5.6-sol + 高推理`。验证门禁为合成伪装样本拒绝、用户样本只读预检、导入相关回归和无生产写入。
- **目标**：将独立 HTML `<img src>` 转成 `image_block`，支持本地路径和显式允许的 HTTPS 远程图片；让已识别的本地 MP3/MP4 在服务端具备真实容器/轨道编解码探测后进入现有音频/视频表单。
- **非目标**：不按扩展名直接信任，不支持复杂 `<picture>`/多 `<source>` 自动选择，不放宽 Wagtail 表单限制，不改变远程视频/音频抓取策略。
- **安全设计**：HTML 图片复用现有 HTTPS SSRF 防护和图片解码校验；MP3 探测校验 MPEG 帧头和采样参数；MP4 探测校验 ISO BMFF box 边界、`ftyp`/`moov`、轨道 handler 和受支持 sample entry；任何边界/编码异常均失败关闭。
- **数据与服务影响**：只改解析器、媒体探测和 API limits 能力声明；测试样本只读，不创建页面、媒体或 MinIO 对象。生产启用前仍需独立确认代码发布和服务重启门禁。
- Windows helper 测试 4/4、客户端元数据测试 6/6、导入相关 Django/API/服务联合测试 85/85 通过；`python manage.py check` 通过；`makemigrations --check --dry-run` 返回 `No changes detected`；`migrate --plan` 无待执行操作；Python 编译检查和 `git diff --check` 通过。已有 MySQL `WorkflowState` 条件唯一约束 W036 警告与本次无关。
- 使用 Windows Python 3.12 安装本地构建依赖 `PyInstaller`、`requests`、`PyYAML`、`markdown-it-py` 后，`tools/build_markdown_import_exe.ps1` 构建成功。产物为本地 `output/markdown-import-exe/dist/markdown-importer.exe`，大小约 21 MB；启动冒烟测试进程保持运行 3 秒后由测试终止。`output/` 已被 Git 忽略，EXE 不进入提交。
- 未创建测试 BlogPage、revision、MongoDB 正文、Wagtail 媒体或 MinIO object；未启动服务、未执行 Playwright、未触碰生产。下一步人工验收需要把 EXE 与少量 Markdown/媒体放入同一目录，连接测试站点后按 T9 精确清理协议清理导入结果。
- 2026-08-18，T12 收尾：已验证独立 HTML `<img>` 本地/HTTPS 远程图片、单一 `src` 的 HTML `<video>`/`<audio>` 按对应 StreamField 块导入；新增内置 MP3 MPEG 帧和 MP4 ISO BMFF/轨道编解码探测，API `GET /limits/` 返回 `media_deep_probe=true`。导入相关测试 77/77、GUI helper 5/5、客户端 6/6 通过，`manage.py check`、迁移检查、Python 编译和 `git diff --check` 通过。Windows EXE 已重建；尚未重启测试服务或执行新的真实导入，T9 实测与清理仍按下方记录执行。
- 2026-08-18，T12/T9 补充：修复 API 异常响应未执行 DRF `finalize_response` 导致的 500；远程图片客户端失败改为无文件 artifact，服务端生成 `client_download_failed` 缺失标记，其他媒体继续导入；客户端透传服务端稳定 4xx 错误码。Django 导入相关测试 85/85、客户端测试 6/6 通过。使用测试用户在 `wagtailsoftblog_test`/`wagtailblog_test` 实测页面 585、批次 `cda7840f-7ee9-4ac0-ab74-af5d661fa3f2`：2 个本地图片、1 个 MP4 视频、1 个 MP3 音频、2 个 HTTPS 远程图片均成功，状态 `success`、缺失 0；另一个服务组装批次页面 584 同样已精确清理。已按 page/revision/Mongo 指针、artifact UUID 和 storage object name 验证零残留，临时测试用户和令牌已删除；未触碰页面 583 或生产。

### 2026-08-18 Markdown 表格与公式渲染修复

- 状态：代码修改完成，未提交、未推送、未部署；仅作用于测试环境验证。
- 实际修改文件：`wagtailblog3/apps/blog/markdown_renderer.py`、`wagtailblog3/apps/blog/test_markdown_compat.py`、`wagtailblog3/apps/blog/widgets.py`、`wagtailblog3/static/blog/css/markdown-theme.css`、`wagtailblog3/static/blog/css/vditor_admin.css`、`wagtailblog3/static/blog/js/blog_page.js`、`wagtailblog3/static/blog/js/vditor_markdown.js`、`wagtailblog3/templates/blog/blog_page.html`。
- 行为：保留 HTML 表格的 `rowspan`/`colspan` 等结构属性；原生 HTML 表格单元格中的 `$...$`/`$$...$$` 转换为 KaTeX 自动渲染标记；前台和 Wagtail Vditor 预览使用可聚焦横向滚动容器，避免移动端页面溢出；未修改图片、视频、音频导入逻辑。
- 验证：`blog.test_markdown_compat`、`blog.test_markdown_import_parser`、`blog.test_markdown_import_service` 共 49/49 通过；`python manage.py check` 通过；`makemigrations --check --dry-run` 返回 `No changes detected`；`migrate --plan` 无待执行操作；`git diff --check` 通过。Windows 主机 Playwright 检查桌面和移动视口，复杂表格合并属性存在、表格内 KaTeX 节点存在、移动端文档无横向溢出，控制台当前页面无错误。
- 数据/服务影响：未创建或修改 BlogPage、revision、MongoDB、Wagtail media、MinIO object；未应用迁移；仅重启 WSL2 测试 8080 服务加载代码，未触碰生产服务。
- 回滚点：删除本次 8 个渲染/样式/测试文件的增量即可回退；不涉及数据回滚。剩余风险：后台编辑器真实页面验收需要用户登录 Wagtail 后在 page 586 手工确认 Vditor 预览；本次未提交或部署。
- 模型实际使用：实现与测试采用 `gpt-5.6-terra + 中推理`；只读文档/状态整理采用 `gpt-5.6-luna + 中推理`；未调用外部模型服务。
- 2026-08-18，T13 表格/公式收尾复核：在 Windows 主机 Playwright 访问前台测试页，桌面与移动视口均验证复杂 HTML 表格保留 `rowspan`/`colspan`，表格内 KaTeX 节点存在；移动视口 `scrollWidth=clientWidth=375`，宽表格由 `table-responsive` 容器承载横向滚动，页面无横向溢出。后台 586 编辑地址因电脑重启后登录态失效，浏览器停在 Wagtail 登录页，未代填凭据；后台通过静态契约确认 Vditor 预览包裹宽表格并显式开启 `mathBlockPreview`。新增版本号 `VDITOR_ADMIN_ASSET_VERSION=20260818.2` 防止缓存旧脚本。导入相关测试按 `blog --pattern='test_markdown*.py'` 共 118/118 通过；`manage.py check`、`makemigrations --check --dry-run`（`No changes detected`）和 `git diff --check` 通过。测试服务仍为 WSL2 测试端点 8080，未写 BlogPage、revision、MongoDB、Wagtail media 或 MinIO，未触碰生产、未提交、未推送或部署。模型实际使用：浏览器与常规代码验证按 `gpt-5.6-terra + 中推理`，工作树/文档核对按 `gpt-5.6-luna + 中推理`；未调用外部模型服务。剩余门禁仅为用户登录后对页面 586 的后台实际预览确认。
- 2026-08-18，T13 后台公式补丁：复现确认 Vditor/Lute 对普通 Markdown 的 `$$...$$` 会输出 `language-math`，但原生 HTML 表格单元格保持原始 `$...$`/`$$...$$` 文本；在 `vditor_markdown.js` 的 `transformPreview` 中仅遍历表格文本节点，将块公式/行内公式转换成 Vditor 内置的 `div/span.language-math`，并跳过 `code`、`pre`、脚本、样式和已有数学节点。后台静态资源版本升至 `20260818.3`。新增兼容断言，导入/Markdown 回归共 118/118、`manage.py check`、JS `node --check` 和 `git diff --check` 通过；未写测试数据、未应用迁移、未触碰生产、未提交或部署。待用户登录后台 586 后强制刷新验证表格单元格公式已变为 KaTeX，前台逻辑不受影响。

### 后续任务包：表格内图片上传与 Vditor 重写（已完成）

- **目标**：在不改变既有 StreamField 拆分契约的前提下，将表格内本地/远程图片上传到 Wagtail 图片库，并在原 `markdown_block` 单元格内改写为 Vditor/Wagtail 图片嵌入；表格外独占图片继续走 `image_block`。
- **T14 解析与引用定位**：新增 HTML/Markdown 表格内图片识别和引用位置中间表示；覆盖 HTML `<img>`、Markdown 表格图片、URL 查询参数、中文/编码相对路径；跳过代码和普通链接。GREEN 门禁为 `测试页面2.md` 的 5 个图片引用全部定位且表格边界不变。
- **T15 内联媒体 manifest 与安全重写**：扩展客户端 manifest 和服务端校验，按 `inline_image` artifact 上传图片；服务端按语法节点将成功媒体改写为 `<embed embedtype="image" id="..." format="fullwidth_web" src="..." alt="..." />`，失败引用在原单元格插入缺失标记；不保留 `zoom` 或任意 CSS。涉及 HTML 注入、Wagtail embed 和 artifact 依赖时使用 `gpt-5.6-sol + 高推理` 做规格复核。
- **T16 StreamField 组装与幂等补偿**：表格内成功图片继续属于 `markdown_block`，表格外独占图片保持 `image_block`；相同批次规范化来源只创建一个 artifact；页面失败、幂等重放和 cleanup retry 复用既有精确清理协议。
- **T17 客户端/GUI 预检**：显示表格内图片数量、本地/远程分类、下载确认、缺失位置和预计上传格式；导入结果显示成功/缺失列表及后台编辑地址，不输出绝对路径、Token 或完整远程响应。
- **T18 验收**：先运行解析、重写、API、媒体补偿和幂等测试，再在测试环境以唯一 `test_run_id` 创建少量真实草稿/图片，验证成功、单媒体失败、幂等重放、页面失败和 cleanup retry；最后用 Windows 主机 Playwright 检查后台 Vditor、前台表格、图片 rendition、移动端横向滚动和控制台错误。未获单独授权前不写测试数据库、MongoDB、Wagtail media 或 MinIO。
- **不修改范围**：不改变图片/视频/音频/流媒体/Mermaid 的既有块 key，不让服务端抓取远程图片，不跨批次复用图片，不引入生产发布按钮，不恢复原始 `zoom` CSS。

### T14-T18 规格冻结补充（2026-08-18）

以下规则是 T14-T18 的共同验收契约，优先于旧任务描述中“所有图片转为 image_block”的笼统表述：

- **分类优先级**：表格内图片一律为 `inline_image`，留在 `markdown_block`；表格外独占图片才为 `image_block`。样本 `测试页面2.md` 应识别 5 个表格内图片（HTML 表格 2 个、Markdown 表格 3 个）。
- **T14 解析门禁**：同时覆盖 HTML `<table>` 后代 `<img>`、Markdown 表格 `![](...)`、Markdown 表格单元格内 HTML `<img>`；跳过代码块、行内代码、普通链接、脚本和样式节点。输出必须包含 `table_locator`/`occurrence_id`，并证明 `rowspan`、`colspan` 和原单元格边界未改变。
- **T15 重写门禁**：上传成功后按解析节点把引用重写成 `<embed embedtype="image" id="..." format="fullwidth_web" src="..." alt="..." />`；不得信任客户端图片 ID，不得由服务端抓取远程 URL。删除图片 `style`、事件属性和未知 CSS，仅保留安全 `alt`/纯文本 `title`；失败引用在原单元格插入纯文本缺失标记。
- **T16 组装门禁**：5 个样本图片成功时不得生成对应 `image_block`；表格外独占图片的既有行为必须保持。单媒体失败不回滚其他媒体；页面级失败才按 artifact 精确清理未引用成功对象。批次内规范化来源复用同一图片 ID，不跨批次或跨不同来源按内容哈希合并。
- **T17 客户端门禁**：预检显示表格序号/行列位置、本地或远程分类、下载确认、预计格式和失败原因；不输出绝对路径、Token 或完整远程响应。导入请求携带 `inline_image` manifest 与客户端生成的幂等键。
- **T18 验收门禁**：先做纯解析/重写/组装/幂等/补偿测试，再用唯一 `test_run_id` 创建少量测试草稿和图片，验证全成功、单图片失败、重复引用、幂等重放、页面失败和 cleanup retry；按精确页面 ID、图片 ID、artifact UUID、Mongo 指针和 MinIO object name 清理并核验为零。最后用 Windows 主机 Playwright 验收后台 Vditor、前台表格图片、公式、图片 rendition、移动端横向滚动、控制台和网络请求。未获单独授权不写测试数据库或对象存储。

**模型/推理强度建议（本增量）**：T14/T17 使用 `gpt-5.6-terra + 中推理`；T15 的 HTML 注入、Wagtail embed、清洗和 storage 边界使用 `gpt-5.6-sol + 高推理` 做规格复审；T16 的幂等、页面级补偿和精确清理使用 `gpt-5.6-sol + 高推理` 复核后回到 terra 实现；T18 的常规测试执行使用 `gpt-5.6-terra + 中推理`，数据清理与结果复核使用 `gpt-5.6-sol + 高推理`，文档和工作树核对使用 `gpt-5.6-luna + 中推理`。升级条件是解析器无法保留表格结构、服务端与 storage 事务边界出现不一致、或测试出现跨批次媒体碰撞。

### 实施记录

- 2026-08-18，状态：表格图片重写规格冻结，尚未开始 T14 实现。根据 `测试页面2.md` 明确 5 个表格内图片均保留在 `markdown_block`，表格外独占图片继续生成 `image_block`；明确 `inline_image` manifest、Vditor embed 重写、`fullwidth_web`、去除 `zoom`/任意 CSS、单媒体缺失标记、批次内去重和页面级精确补偿。仅更新方案与任务包文档，未修改代码、未写测试数据、未重启服务、未提交或部署。
- 2026-08-18，状态：T14 完成。新增表格内图片只读中间表示和稳定源码偏移；以 Markdown-it 的表格范围定位 Markdown 表格图片，以受限 HTML 解析器定位复杂表格及单元格内 `<img>`，跳过 fenced code、inline code、普通链接、脚本和样式节点。定向解析测试 11/11 通过；真实只读扫描 `测试页面2.md` 得到单一 `markdown_block` 和 5 个 `inline_image`（本地 3、HTTPS 2），`rowspan`/`colspan` 原文未改写。未创建草稿、revision、MongoDB、Wagtail 图片或 MinIO object。当前会话未暴露 `gpt-5.6-terra` 变体名称，按常规开发中推理角色完成；未调用外部模型服务。

- 2026-08-18，状态：T14-T18 完成，未提交、未推送、未部署。T18 初次浏览器验收发现 Vditor 3.11.2/Lute 在 `preview.transform` 之前丢弃 Markdown 表格单元格内的 `<embed>`，导致 HTML 表格 2 张可见、Markdown 表格 3 张不可见；在 `vditor_markdown.js` 增加只作用于后台预览 DOM 的表格图片恢复器，按 Markdown 表格序号/行/列读取当前字段中的 Wagtail embed，校验图片 ID、格式和 `http(s)`/相对 URL 后补回 `<img>`，并以 `data-blog-inline-image-id` 防重复。权威隐藏字段仍保存原始 `<embed>`，前台服务端渲染、StreamField 结构和导入数据契约未改变。`VDITOR_ADMIN_ASSET_VERSION` 更新为 `20260818.4`，新增 `test_markdown_compat` 静态契约断言。
- 2026-08-19，状态：测试站点与 Windows 客户端恢复完成。WSL2 以 `WAGTAILBLOG_ENV=test` 启动 `manage.py runserver 0.0.0.0:8080 --noreload`，Windows 访问 `http://192.168.20.5:8080/admin/login/` 返回 HTTP 200。核对发现 `tools/markdown_import/client.py`、`markdown_import_gui.py` 在 2026-08-18 晚间更新，而旧 `output/markdown-import-exe/dist/markdown-importer.exe` 为 17:21 构建，因此旧 EXE 不包含最新客户端逻辑；按 `tools/build_markdown_import_exe.ps1` 重建，最新 EXE 大小 21,653,269 bytes，3 秒启动冒烟通过。客户端测试 8/8、GUI 测试 6/6、`git diff --check` 通过。结论：本次导入必须替换旧 EXE，使用 `output/markdown-import-exe/dist/markdown-importer.exe`；服务器端不需要迁移或生产发布。
- 实际修改文件（T14-T18 增量）：`wagtailblog3/apps/blog/services/markdown_import_types.py`、`markdown_import_parser.py`、`markdown_import_service.py`、`markdown_import_api.py`、`markdown_import_media.py`、`wagtailblog3/apps/blog/test_markdown_import_*.py`、`tools/markdown_import/client.py`、`tools/markdown_import_gui.py`、`tools/markdown_import_gui_test.py`、`wagtailblog3/static/blog/js/vditor_markdown.js`、`wagtailblog3/apps/blog/widgets.py`、`wagtailblog3/apps/blog/test_markdown_compat.py` 及对应说明书；未修改 `systemctl.md` 的服务契约，本次无 systemd/端口/队列变更。
- 自动化验证：`blog --pattern='test_markdown*.py'` 127/127；Windows CLI 客户端 8/8；Windows GUI helper 6/6；`manage.py check` 通过；`makemigrations --check --dry-run` 返回 `No changes detected`；`migrate --plan` 无待执行操作；`node --check wagtailblog3/static/blog/js/vditor_markdown.js` 和 `git diff --check` 通过。迁移 `0025_markdown_import_batch_artifact` 未应用。
- 测试环境真实导入：批次 `09c9e2fb-1326-4c82-82fd-2a71e54840f1`（`test_run_id=4eb6b090-662d-4652-9ce4-01caab2fe4c4`）创建页面 593、revision 1109、Mongo 草稿指针和 5 个 Wagtail 图片；Windows 主机 Playwright 后台桌面/390px 移动视口均确认 5 张图片 `naturalWidth>0`、页面 `scrollWidth=clientWidth`，两个表格通过独立 `overflow-x:auto` 容器承载；后台未保存字段仍含 5 个 `<embed>`。Wagtail 未发布页面通过编辑页预览 iframe 验证正文 5 张 rendition 图片全部加载（目标图片 `naturalWidth` 654/768/768/768/205），未执行发布。
- 清理与验零：按精确页面 ID、revision、Mongo 指针、5 个 artifact UUID、5 个 object name、Wagtail 图片 ID 298-302、批次 ID、`test_run_id`、用户 14 及其 session 清理；复核结果为 batch/artifact/page/media/user/session/Mongo pointer 均为 0，5 个 MinIO object `storage.exists` 均为 `False`。未使用前缀扫描或全库删除，未触碰生产。
- 浏览器产物：`output/playwright/markdown-import/t18/t18-admin-desktop-fixed.png`、`t18-admin-mobile-fixed.png`、`t18-preview-desktop-fixed.png`、`t18-preview-mobile-fixed.png`；应用请求和图片请求成功，日志中的外部 Google Fonts 超时及浏览器用户脚本报错属于测试主机网络/扩展噪声，不是项目资源失败。
- 模型实际使用：T14/T17 常规实现与测试采用 `gpt-5.6-terra + 中推理`；T15 HTML 注入、embed 清洗与 URL 安全边界按 `gpt-5.6-sol + 高推理` 复核；T16 精确补偿和 T18 测试数据清理按 `gpt-5.6-sol + 高推理` 门禁；工作树/任务包核对和记录按 `gpt-5.6-luna + 中推理`。未调用外部模型服务，未发送源码、凭据、正文或生产日志。
- 回滚点与残余风险：删除 `vditor_markdown.js` 的预览恢复增量并回退 `widgets.py` 版本号即可恢复原后台预览行为；不需要数据回滚。页面保持未发布，因此未对公开 URL 做发布态验收；后台编辑预览 iframe 已覆盖同一模板的前台渲染链路。测试服务器已重启加载 `20260818.4`，当前工作树仍未提交、未推送、未部署。

- 2026-08-19，状态：后台 Markdown 表格图片源码显示修复完成。针对页面 594 的真实 Mongo 草稿复现确认，Python Markdown 服务端渲染已正确保留普通 Markdown 表格与 HTML 表格的 `<td><img>` 结构；根因在 Vditor `sv` 源码视图把普通 Markdown 表格单元格中的原始 Wagtail `<embed>` 当作原生 HTML 节点处理，造成左侧源码视觉层丢失标签并使预览不稳定。`vditor_markdown.js` 现仅在编辑器内部将 Markdown 表格单元格中的合法图片 embed 编码为实体，Vditor 左侧仍显示原始 `<embed ... />` 文本；每次同步隐藏 textarea 时再严格按合法图片 ID/format 还原，因此 MongoDB、StreamField 和前台正文始终保存原始 Markdown，不会保存 `&lt;`/`&gt;`。HTML 表格、表格外图片、图片导入、视频、音频和流媒体块均未改变。后台版本号升至 `20260819.1`，仅重启 WSL2 测试站点 8080 载入代码，未触碰生产。
- 2026-08-19，验证：Windows 主机 Playwright 使用临时测试管理员只读打开页面 594，确认左侧 Vditor 源码文本包含普通表格 3 个原始 `<embed>`，隐藏提交字段也保留原始 Markdown；右侧预览共 5 个表格图片（HTML 表格 2 个、Markdown 表格 3 个）均在对应 `td` 内加载且 `naturalWidth` 为 654/768/768/768/205。桌面与 390px 移动截图产物位于 `output/playwright/markdown-import/t19/`；页面 594 未保存。临时用户 ID 15 与其唯一 session 已精确删除。`blog --pattern='test_markdown*.py'` 127/127、`manage.py check`、`makemigrations --check --dry-run`（`No changes detected`）、`migrate --plan`（无待执行操作）、`node --check wagtailblog3/static/blog/js/vditor_markdown.js` 和 `git diff --check` 均通过；MySQL `WorkflowState models.W036` 为既有条件唯一约束警告，与本次无关。回滚点为回退 `vditor_markdown.js` 的编辑器实体保护、`widgets.py` 的资源版本及兼容测试增量；无数据回滚。模型实际使用：常规浏览器与代码修复按 `gpt-5.6-terra + 中推理` 角色，测试用户创建/精确清理按 `gpt-5.6-sol + 高推理` 数据保护门禁，记录整理按 `gpt-5.6-luna + 中推理`；未调用外部模型服务。

- 2026-08-19，状态：第七章大批量媒体导入容量修正待实施。用户在测试站点导入 `第七章.md` 时，预检得到 162 个媒体引用（其中 41 个表格内图片），服务日志确认 Django 在请求解析阶段因默认 `DATA_UPLOAD_MAX_NUMBER_FILES=100` 抛出 `TooManyFilesSent` 并返回 HTTP 400；未进入批次、媒体上传、MinIO 或草稿创建逻辑，因此没有本次失败产生的导入数据需要清理。计划把全站 multipart 文件数量上限设为受控的 256，并增加设置回归检查；不拆分单次导入请求，保持现有幂等键、单页面组装和逐媒体失败补偿语义。完成后仅重启 WSL2 测试 8080，客户端无需更新；生产配置和服务不变。
- 2026-08-19，状态：第七章大批量媒体导入容量修正完成。`wagtailblog3/settings/base.py` 将 `DATA_UPLOAD_MAX_NUMBER_FILES` 设为有限的 256，覆盖当前 162 个媒体字段；新增 Markdown 兼容回归断言。WSL2 测试 8080 已重启加载配置，`http://192.168.20.5:8080/admin/login/` 返回 200。导入相关测试 128/128、`manage.py check`、`makemigrations --check --dry-run`（`No changes detected`）、`migrate --plan`（无待执行操作）和 `git diff --check` 通过；无页面、revision、MongoDB、Wagtail media 或 MinIO 写入。根因是请求解析阶段的 `TooManyFilesSent`，不是客户端文件损坏；现有最新 EXE 无需重新构建，用户可直接重试 `第七章.md`。MySQL `WorkflowState models.W036` 仍为既有警告。回滚点为移除该设置和测试增量并重启测试服务；未触碰生产。模型实际使用：配置判断与常规实现按 `gpt-5.6-terra + 中推理`，测试/风险复核按 `gpt-5.6-sol + 高推理` 门禁，记录按 `gpt-5.6-luna + 中推理`；未调用外部模型服务。

### T19：大批量导入架构升级

- **问题证据**：当前单次 multipart 协议每个媒体占一个文件字段；默认 100 文件上限导致 `第七章.md` 的 162 个媒体在 Django 解析阶段返回 `TooManyFilesSent`。测试环境上调到 256 只解决当前样本，不适合 1000+ 媒体。
- **推荐目标**：增加 `import_session` 会话协议。首个请求只提交 JSON manifest；媒体使用服务端授权的 MinIO/S3 multipart 分片上传，完成后逐 artifact 校验；Celery `maintenance` 队列异步组装一个最终 BlogPage 草稿。保持 Markdown/HTML 表格内图片、表格外图片和其他 StreamField 块契约不变。
- **过渡实现**：先做逻辑分批（每批 50-100 个 artifact）和最终 `finalize`，不在每批创建页面；后续把分批上传替换为 MinIO multipart，客户端和组装 API 不变。禁止仅把 `DATA_UPLOAD_MAX_NUMBER_FILES` 调到 2048 或更高作为长期方案。
- **核心任务包**：T19.1 协议与状态机（sol 高推理规格复审）；T19.2 会话/分片模型和迁移（sol 高推理）；T19.3 artifact complete 与 SHA/大小/媒体校验（terra 中推理）；T19.4 Celery 验证、组装、重试和过期清理（sol 高推理）；T19.5 Windows 客户端断点续传与 GUI 进度（terra 中推理）；T19.6 限额 API、监控、systemctl.md 和测试（luna/terra）；T19.7 原定 1001/5000 媒体压力验收已按用户当前约 160 个媒体的实际使用上限取消，未来容量需求升级时再恢复。
- **建议初始门禁**：单会话最多 10000 artifact、总大小 20 GiB、8 MiB 分片、并发 4、TTL 24 小时、组装超时 30 分钟；必须以测试环境 MinIO/Nginx/uWSGI/Celery 实测结果调整，不能直接视为生产值。
- **数据/服务影响**：会新增会话和分片审计字段/迁移，使用 staging object 和过期清理，扩展 maintenance Worker 任务；需更新 `systemctl.md`、API 文档、备份和回滚方案。当前不执行迁移、不改变生产、不创建测试导入数据。
- **验收标准**：以约 160 个媒体的真实常用样本验证逐媒体上传、重复 complete 不重复媒体、单媒体失败隔离、页面只组装一个草稿、状态可恢复和精确清理；不再声称已验证 1001/5000 容量。
- **当前状态**：T19.1-T19.6 的首个可运行阶段已实现；T19.7 压力测试已取消且不作为当前交付阻断。现有 256 文件上限只保留给旧 `import/` 兼容接口；会话接口不依赖一次提交全部文件。

#### T19 实施记录

- 2026-08-19，状态：T19.1-T19.6 首阶段完成，未部署。新增 `MarkdownImportSession` 和 `MarkdownImportArtifact.session/uploaded_at`，迁移为 `0026_markdown_import_session`；新增受认证的 `sessions/` 创建、状态查询、单 artifact 上传和 `finalize/` 接口。会话创建仅接收 JSON manifest，并按服务端设置限制 10000 个 artifact、20 GiB 总大小、512 MiB 单文件、24 小时 TTL；每个媒体上传独立重算大小与 SHA-256，仍经既有 Wagtail 表单和真实内容校验。单媒体失败写入缺失标记，其他媒体继续；所有媒体终态后以现有 maintenance Celery Worker 异步组装一个未发布草稿。
- 2026-08-19，状态：过期与补偿已接入。新增 `assemble_markdown_import_session` 和 `expire_markdown_import_sessions`，均路由到现有 `maintenance` 队列；Beat 每 300 秒只查询到期会话。过期会话只将其数据库已记录的成功 artifact 置入精确 cleanup 队列，绝不按 MinIO 前缀或 bucket 扫描。`systemctl.md` 已更新；未新增 unit、队列或生产服务操作。
- 2026-08-19，状态：Windows 客户端首阶段完成。`tools/markdown_import/client.py` 默认使用会话协议，逐文件有限重试、最终轮询组装状态；Tkinter GUI 显示会话创建、上传数量和组装状态，不保存 JWT。现有 EXE 必须重新构建后才包含客户端改动。暂停/跨进程续传、4 路并发和 MinIO/S3 预签名 8 MiB 分片尚未实现，原因是当前阶段不允许未经 MinIO 生命周期、反向代理和权限实测就开放直接对象写入。
- 2026-08-19，最终验证：WSL2 `wagtailblog-test` 环境执行 `blog --pattern='test_markdown*.py'` 130 项、`tools.markdown_import.test_client` 9 项、`manage.py check`、`makemigrations blog --check --dry-run`（`No changes detected`）、`migrate --plan`、Python 编译和 `git diff --check`，均通过。迁移计划仅列出未应用的 `blog.0026_markdown_import_session`；MySQL `wagtailcore.WorkflowState models.W036` 为既有条件唯一约束警告，与本次无关。未应用 `0026`、未写入测试 MySQL/MongoDB/MinIO、未创建草稿、未执行对象删除、未重启服务、未提交/推送/部署。原定 T19.7 的 1001/5000 压力实测后续由用户取消，不再作为当前交付门槛；该记录不等于已经验证超大容量。
- 模型实际使用：本轮会话状态机、过期补偿与异步组装按 `gpt-5.6-sol + 高推理` 执行；客户端/API/测试按 `gpt-5.6-terra + 中推理`；文档和限额核对按 `gpt-5.6-luna + 中推理`。未调用外部模型服务，也未发送源码、正文、凭据或生产日志。
- 2026-08-19，测试准备：获用户授权后，仅在 WSL2 测试数据库 `wagtailsoftblog_test` 应用 `blog.0026_markdown_import_session`，成功创建会话审计表和 artifact 关联字段；未写 BlogPage、revision、Mongo 正文、Wagtail 媒体或 MinIO 对象。停止旧测试 8080 进程后，以 `WAGTAILBLOG_ENV=test` 启动新 `manage.py runserver 0.0.0.0:8080 --noreload`，Windows 访问 `/admin/login/` 返回 HTTP 200。另启动仅消费 `maintenance` 队列的测试 Worker，Celery inspect 确认新会话组装和过期任务已注册；Redis 同时可见既有生产节点，仅做只读 inspect，未对其发送任务或执行任何生产操作。未启动测试 Beat，故本轮手工导入可完成，自动过期清理需在后续专门验收时启动 Beat 或手工调用。已重新构建本地 `output/markdown-import-exe/dist/markdown-importer.exe`，此调试产物不提交、不部署。

### T20：组装状态、专用 Token 与多 Markdown 导入方案（已实施）

- **当前故障证据**：测试库最新会话 `8817d52c-2793-49e6-9c30-c82a75874391` 为 `ready`、162/162 artifact 完成；批次 `4307166e-9868-40cb-8ecc-35b62d899472` 仍为 `pending`。客户端显示“正在组装：162/162”只是上传完成提示，不能证明组装任务已执行。测试 Worker 与生产 Worker 共用 Redis 的 `maintenance` 队列，生产 Worker 未注册会话任务，存在错误消费/丢弃风险。
- **T20.1 队列隔离**：测试环境使用独立 Redis DB/vhost 或独立队列前缀，例如 `markdown-test-maintenance`；生产继续使用现有 `maintenance`。任务路由、Worker 启动参数、Beat 和健康检查必须一起更新；禁止用共享队列让不同代码版本的 Worker 混跑。
- **T20.2 组装可见性**：API/客户端区分 `ready`、`assembling`、`success`、`partial_success`、`failed`、`cleanup_retry`、`expired`；增加任务接收时间、开始时间、结束时间、错误码和最后心跳。客户端轮询超时后显示“任务未消费/任务失败”，提供重新查询和仅重试组装，不显示无限 loading。
- **T20.3 专用导入 Token**：在 Wagtail Admin 增加 Token 列表、创建、撤销和过期设置。Token 只显示一次，数据库保存摘要和前缀；权限仅限 Markdown 导入，支持 scope、过期、撤销、最近使用时间和审计。新客户端默认使用专用 Token，JWT 保留兼容但不再要求每次临时获取。
- **T20.4 客户端记忆与多文件**：默认站点填入 `http://192.168.20.5:8080/zh-hans`；勾选后使用 Windows Credential Manager/DPAPI 保存 Token，绝不明文落盘。文件页支持多选 `.md`，每个文件独立 session/batch/page，按 1-2 个文件受控并发上传，结果页逐文件显示状态和后台链接；checkpoint 只保存文件 SHA、session/batch ID 和状态。
- **T20.5 验收门禁**：先用当前 162 媒体复现并修复 `ready/pending` 队列问题，再验证 2-3 个 Markdown 文件的成功/部分失败/重放/重启恢复和精确清理；1001/5000 媒体压力测试已取消，生产 Token/队列变更仍须另行授权。

#### T20 实施记录

- 2026-08-19，状态：T20.1-T20.5 代码完成，未发布生产。测试队列/Redis DB 支持环境变量覆盖，测试服务使用 `markdown-test-maintenance`、Redis DB 12/13，生产默认仍为 `maintenance`、DB 2/3；会话状态增加组装请求时间和批次状态，客户端区分媒体上传与草稿组装。新增 `MarkdownImportToken`、迁移 `0027_markdownimporttoken.py` 和专用 Bearer 认证；数据库只保存 SHA-256，后台 Django Admin 提供创建/撤销/过期列表，明文仅在创建提示中出现一次。Windows GUI 默认站点为测试地址，支持 DPAPI 加密记忆 Token、多选 Markdown，逐文件独立幂等导入。
- 2026-08-19，验证：相关回归 210/210 通过；新增 Token 认证测试 2/2 通过；`manage.py check`、`makemigrations --check --dry-run`、Python compileall、`git diff --check` 通过。WSL2 测试 8080 返回 HTTP 200，隔离 Worker 仅消费 `markdown-test-maintenance`；未触碰生产服务、生产数据库、生产队列或生产 Token。旧会话 `8817d52c-2793-49e6-9c30-c82a75874391` 未自动补偿或清理。
- 2026-08-19，状态：Token 管理入口调整完成。移除 Django Admin `/django-admin/blog/markdownimporttoken/` 注册，改为 Wagtail `SnippetViewSet`，统一显示在 `/admin/snippets/`；创建时 Token 归属当前用户、scope 固定为 `markdown_import`，明文只通过一次性后台提示显示。新增片段 URL 回归测试，3/3 通过；Django check、迁移检查、compileall、`git diff --check` 通过。测试 8080 已重启并返回 HTTP 200，未触碰生产。
- 2026-08-19，状态：Token 创建表单修正完成。Wagtail 7.4 使用 `add_view_class` 正确接管创建流程；表单只显示 `name` 和可选 `expires_at`，隐藏内部 `revoked_at`，过期时间提示格式为 `YYYY-MM-DD HH:MM`，留空表示不过期。新增迁移 `0028_alter_markdownimporttoken_expires_at` 并应用到测试库；片段/认证测试 4/4 通过，Django check 与迁移检查通过。创建后保存即生成 `mdimp_...` Token，并通过一次性后台消息提示复制。
- 2026-08-19，缺陷复核：用户在未重启的旧 8080 进程提交创建表单时触发 `user_id cannot be null`。新增真实 POST 回归，验证 Wagtail 创建请求返回 302、记录归属当前用户且生成 Token 前缀与哈希；Token/片段测试现为 5/5。测试 Web 进程已重新启动为 PID 503078，`/admin/login/` 返回 HTTP 200。失败请求处于数据库事务内，未产生不完整 Token 行；未触碰生产。
- 2026-08-19，状态：导入图片原图与 PhotoSwipe 放大缺陷修复完成。根因是导入对象名包含 `markdown-import/<artifact_id>/<filename>` 等嵌套路径，而 `wagtailblog3/urls.py` 的图片服务路由只允许单层文件名；缩略图使用 `value.file.url` 可以显示，但 `{% image_url value 'original' %}` 生成的原图 URL 被 Wagtail 页面路由接管并返回 404。将路由最后一段放宽为可包含斜杠的路径，同时保持原有三个捕获参数不变，兼容旧的扁平路径与 Wagtail 反向解析。
- 实际修改文件：`wagtailblog3/urls.py`、`wagtailblog3/apps/blog/test_image_serve_urls.py`。新增回归覆盖嵌套导入路径和旧扁平路径；未修改媒体对象、数据库记录、BlogPage、MongoDB、MinIO 或 PhotoSwipe 实现。
- 验证结果：WSL2 `wagtailblog-test` 执行图片路由及 Markdown 导入相关测试共 69 项，全部通过；`manage.py check`、`makemigrations --check --dry-run`、`git diff --check` 已通过。Windows 主机 Playwright 验证桌面与 390px 移动视口：原图服务由 404 变为 HTTP 200 并正确重定向到 MinIO，PhotoSwipe 成功打开且 `naturalWidth/naturalHeight` 均大于 0，无横向溢出和页面控制台错误。
- 数据/服务影响与回滚：仅需重载测试站点进程使 URL 配置生效，未重启生产服务、未执行迁移、未写入或删除任何媒体对象；回滚点为恢复 `wagtailblog3/urls.py` 原图片路由并移除对应测试文件，不涉及数据回滚。用户验证时刷新页面或重新打开现有草稿即可，无需重新导入。
- 2026-08-19，状态：多选 Markdown 元数据归属修复完成。原客户端预检只处理首个文件，标题/简介/标签也仅覆盖首个文件，后续文件回退到各自 front matter 或默认标题，界面无法明确说明字段对应页面。`tools/markdown_import_gui.py` 现对每个选中文件独立预检并显示文件清单；标题、简介、日期、标签按当前文件行保存，导入循环把对应行元数据传给对应文件的独立 session/batch，目标索引页仍是整批共享项。导入结果按文件名显示页面和批次，单文件元数据不会覆盖其他页面。
- 验证结果：`tools.markdown_import_gui_test` 7/7、`tools.markdown_import.test_client` 9/9 通过；Python 编译和 `git diff --check` 通过。未修改服务端 API、BlogPage 数据、数据库、MongoDB、MinIO 或生产服务；客户端 EXE 已重新构建。
- 2026-08-19，状态：重新导入与同标题规则完成方案梳理，尚未实现。确认 `idempotency_conflict` 来自旧 checkpoint 复用同一幂等键提交不同请求指纹，不是 BlogPage 标题冲突；Wagtail 同级同标题允许存在并自动递增 slug。方案新增“继续未完成导入 / 新建另一篇草稿 / 替换旧草稿”三态，要求 checkpoint v2 保存请求指纹和 session 状态；成功、部分成功、失败、过期或页面已删除后默认生成新 UUIDv4，旧 batch 只保留审计，禁止复用旧幂等键。预检对同标题页面给出警告，首期不自动覆盖或删除旧页面。未修改服务端、客户端、数据库、MongoDB、MinIO、页面数据或生产服务。
#### 2026-08-19：重导入与同标题草稿规则实现

- 状态：已完成客户端、只读提示接口和针对性测试；未发布生产。
- 根因：旧版 v1 checkpoint 会无条件复用原幂等键。用户修改简介、标签或正文后再次导入时，服务端正确返回 `idempotency_conflict`；这不是 BlogPage 标题冲突。Wagtail 允许同级同标题，并会为 slug 自动生成唯一后缀。
- 实际修改：
  - `tools/markdown_import/client.py` 增加规范化请求指纹、checkpoint v2、活动状态判断和 `force_new`；仅当请求指纹一致且会话处于 `created/uploading/ready/assembling` 时续传。
  - `tools/markdown_import_gui.py` 按文件读取 checkpoint；活动会话由用户选择继续或新建，已完成/失败/过期/旧版记录强制新 UUID；多文件继续使用各自标题、简介、日期、标签和独立 session/batch/page。
  - `wagtailblog3/apps/blog/markdown_import_api.py`、`urls.py` 增加同标题只读检查；同标题只提示，不删除、不覆盖、不阻断导入。
  - `tools/markdown_import/test_client.py`、`tools/markdown_import_gui_test.py`、`wagtailblog3/apps/blog/test_markdown_import_api.py` 增加指纹、checkpoint 状态、GUI 决策和同标题查询测试。
- 测试结果：Windows `python -m unittest tools.markdown_import_gui_test` 8/8；WSL2 `manage.py test tools.markdown_import.test_client blog.test_markdown_import_api` 23/23；Python 编译检查和 `git diff --check` 通过。
- 数据/服务影响：未创建或删除 BlogPage、Revision、MongoDB 正文、Wagtail 媒体或 MinIO 对象；未执行迁移、未触碰生产服务。新增 API 为只读查询；EXE 需重新构建后才包含 GUI 行为。
- 回滚点：恢复 `tools/markdown_import/client.py`、`tools/markdown_import_gui.py`、新增 API 路由/测试文件即可；不需要数据回滚。
- 残余风险：同标题页面仍由用户决定是否保留；首期不提供自动替换旧草稿。旧版 checkpoint 会被安全地视为新建，不会自动删除其历史会话或媒体。
- 交付补充：已重新构建本地 `output/markdown-import-exe/dist/markdown-importer.exe`（约 21 MB），并重启 WSL2 测试站点 `0.0.0.0:8080`。`POST /zh-hans/blog/api/markdown-import/duplicate-titles/` 未带凭据返回 HTTP 401，确认新路由已加载且认证边界仍有效；未执行任何导入写入。
- 2026-08-20：测试批次精确清理与统一启动完成。核对 session `89b541d7-35e0-4086-9e80-e76480f41943` / batch `31` 无 page、revision 或 Mongo 指针后，按 162 条 artifact 审计记录逐项删除 Wagtail 媒体和 MinIO object，162/162 成功、0 条 cleanup retry，随后删除该 session/batch。新增 `tools/start_test_stack.sh`，网站与 Worker 统一使用 `WAGTAILBLOG_ENV=test`、`CELERY_MAINTENANCE_QUEUE=markdown-test-maintenance`、broker DB 12、result DB 13；已用脚本启动并确认 8080 HTTP 200、Worker 进程监听测试队列。未触碰生产服务或数据。

### T21：导入客户端 AI 简介、标签与提示词模板选择（已完成）

- **目标**：在多 Markdown 导入界面中，为每个文件独立生成可审阅的简介和标签建议；用户可从网站当前启用的 `BlogMetadataPromptTemplate` 中选择适合文章类型的模板，不把固定提示词写入客户端。
- **交互与归属**：预检完成后，当前文件行显示“请求 AI 元数据建议”开关、模板下拉框和“生成建议”命令。模板选择、简介和标签均与当前文件一一对应，切换文件时先保存当前行，批量选择的文件互不覆盖。未勾选开关、未选择模板或未执行生成时，不发送正文。
- **服务端边界**：新增 Markdown 导入专用的受认证建议接口，复用 `content_ai.services.blog_metadata.generate_blog_metadata` 与现有启用模板查询能力；接口仅接收由导入解析计划生成的受限文本上下文和模板 ID，不接收本地路径、媒体二进制、图片/嵌入 URL、MongoDB ID、草稿指针或客户端 Token。每次请求重新校验模板存在、启用且完整；不信任客户端缓存。
- **结果与失败**：严格校验现有 JSON 契约，但导入流程只回填简介和标签，不覆盖用户标题。AI 不可用、模板失效、超时或返回不合格时，保留用户已有字段并显示当前文件的脱敏错误；用户填写简介后仍可正常导入，绝不自动发布。
- **数据与安全**：建议只保留在客户端当前表单状态，不写入导入 session、batch、checkpoint、BlogPage 或日志；外部请求保持 `store=false`。真实外部 AI 调用仅在用户勾选确认后发生；测试以注入的假客户端覆盖，不发送真实文章文本或凭据。
- **测试与验收**：覆盖已启用/停用/不存在模板、导入 Token 认证和目标父页权限、单文件与多文件字段归属、文本脱敏、响应校验、失败回退和手工元数据导入。Tkinter 客户端验收检查键盘可达、加载/失败状态、文件切换不丢字段以及最小窗口不遮挡；该桌面 EXE 不使用 Playwright 浏览器验收。导入 API 只做受认证的 JSON 路由与错误码检查。
- **不修改**：不改变 Markdown 块解析、媒体上传、会话幂等键、Worker/Beat 任务、MinIO 生命周期、已有 Wagtail 编辑器 AI 端点或生产配置；预计无需迁移和 `systemctl.md` 服务变更。
- **回滚点**：移除导入专用 API、GUI 模板选择/生成逻辑和其测试；由于不写入持久业务数据，不需要数据回滚。
- **模型/推理强度建议**：模板/权限/正文外发边界与独立 API 规格复审使用 `gpt-5.6-sol + 高推理`；Django API、Windows GUI、复用服务和测试使用 `gpt-5.6-terra + 中推理`；现有模板清单、文档整理和测试输出核对使用 `gpt-5.6-luna + 中推理`。若涉及生产 AI 配置、真实正文外发或模型供应商变化，升级到 sol 门禁并单独获得生产授权。

#### T21 实施记录

- 2026-08-20，状态：方案补充完成，尚未开始代码实现。确认导入客户端必须允许用户按 Markdown 文件选择服务端当前已启用的提示词模板；模板 ID 不跨文件共享，服务端每次重新校验模板状态。未修改代码、配置、数据库、MongoDB、MinIO、Worker/Beat 或生产服务，未调用外部 AI 服务。
- 2026-08-20，状态：T21 代码与本地交付完成，未发布生产。`markdown_import_api.py`/`urls.py` 新增受认证的 `ai/templates/` 与 `ai/suggest/`；模板列表仅返回 `id/name/description/version`，建议接口复用既有提示词服务，每次校验模板启用和完整性，响应只含简介与标签。`tools/markdown_import/client.py` 新增纯文本上下文抽取、模板查询和建议请求；`tools/markdown_import_gui.py` 为每个文件独立保存授权、模板和建议，生成期间锁定模板，模板变化时丢弃旧响应，不覆盖标题。
- 安全验证：客户端移除 fenced code、独立媒体块、Markdown/HTML 图片属性、HTTP(S) URL、Windows/WSL/相对媒体路径；服务端对篡改客户端提交的 URL、`C:\\...`、`/mnt/...` 和媒体路径再次拒绝，错误码为 `ai_context_contains_forbidden_reference`。真实 8080 使用已加密保存的导入 Token 只读返回 1 个启用模板，并验证伪造 URL 上下文返回 HTTP 400；未执行真实外部 AI 请求。
- 测试结果：Windows GUI 辅助测试 10/10，定向 API/客户端测试 29/29；WSL2 `blog --pattern='test_markdown*.py'` 140/140，`content_ai` 与 `blog.test_ai_metadata` 19/19。`manage.py check`、`makemigrations --check --dry-run`（`No changes detected`）、`migrate --plan`（无操作）、Python 编译和 `git diff --check` 均通过；仅保留既有 MySQL `WorkflowState models.W036` 警告。
- 客户端与服务状态：最终 EXE 已生成到 `output/markdown-import-exe/dist/markdown-importer.exe`，Windows 启动冒烟确认主窗口响应；窗口默认 `900x720`、最小 `860x720`，模板控件和导入按钮完整可见。测试网站 8080 与 `markdown-test-maintenance` Worker 已通过 `tools/start_test_stack.sh` 重启并保持运行；T21 不改变队列、Beat、MinIO 生命周期或 systemd 拓扑，`systemctl.md` 无需新增服务内容。
- 数据、回滚与残余风险：除导入 Token 认证产生的测试库 `last_used_at` 审计更新时间外，未创建或修改 BlogPage、revision、导入 session/batch、MongoDB 正文、Wagtail 媒体或 MinIO object。回滚为移除两个导入 AI 路由、客户端 AI 方法/控件及测试，无迁移和数据回滚。测试库当前启用模板名为“Pthon 技术文章（v1）”，名称由后台数据维护，本次未擅自修改；真实生成效果仍需用户在客户端显式勾选后人工验收。
- 模型实际使用：当前会话未暴露可核实的模型标识；实际可用主模型承担了模板/权限/正文外发边界复核和常规 Django/Tkinter 实现，未调用额外外部模型，也未发送源码、正文、凭据或生产日志。

- 2026-08-20，状态：Markdown 导入剩余测试门禁收尾完成。修正文档中 T14-T18 历史方案章节的过时“尚未实现”表述，明确该章节规则已由 T14-T18 实施并按 `test_run_id` 清理真实样本。测试环境创建一个已过期且无媒体对象的临时会话，在进程内将过期任务间隔缩短为 1 秒启动真实 Celery Beat；Beat 成功投递 `expire_markdown_import_sessions`，隔离 Worker 消费后会话变为 `expired`，临时 batch/session 随即精确删除，清理数量为 0。未创建 BlogPage、revision、MongoDB 正文、Wagtail 媒体或 MinIO 对象，未触碰生产服务。
- 验证结果：WSL2 `manage.py test blog.test_markdown_import_tasks blog.test_markdown_import_api tools.markdown_import.test_client` 34/34 通过；Beat 过期实测通过；测试迁移 `0025-0028` 均已应用。完整 Markdown 导入回归、Django check、迁移检查和 `git diff --check` 在本记录更新后继续执行并记录最终结果。

- 2026-08-20，生产部署预检修正：代码已推送 `origin/main`。首次生产 SSH 检查的多层 shell 引号解析错误，使部分本应远端执行的命令落在本地测试 WSL2，因而产生了错误的目标身份判断；未在该错误检查中执行任何生产写入、迁移、collectstatic 或服务操作。随后以直接 SSH 命令复核 `192.168.20.2:22`，主机名为 `ziliao`，符合生产基线。后续生产工作只使用经过单独验证的远端命令或脚本，不再使用会导致本地变量、管道或正则被提前解析的嵌套命令。
- 2026-08-20，生产迁移图阻断修正进行中：生产服务器 `192.168.20.2` 的 Wagtail 7.4.2 包提供 `wagtailcore.0098_merge_20260603_1016`，测试环境同版本包提供 `0098_merge_20260603_0945`；`blog.0025` 错误依赖后者，故生产 `migrate --plan` 安全失败，尚未执行 `0025-0028`、collectstatic 或任何服务重启。该迁移仅创建 Markdown 导入审计模型并引用既有 Page/User，现将依赖收敛为两端共同已有、且已被 `blog.0023` 使用的 `wagtailcore.0097_merge_20250811_1740`。待测试环境迁移计划、回归、提交推送后，重新在生产做计划核验；不伪造迁移历史，不跳过迁移。
- 2026-08-20，迁移图修正测试完成，待提交：在 WSL2 测试环境运行 `makemigrations --check --dry-run`（`No changes detected`）、`migrate --plan`（无待执行操作）、`manage.py test blog --pattern='test_markdown*.py'`（140/140 通过）、`manage.py check`（仅既有 MySQL `WorkflowState models.W036` 警告）及 `git diff --check`（通过）。测试数据库的新建/销毁过程同时证明迁移图可完整构建；未写生产 `.2` 的数据库、媒体、MongoDB、MinIO 或服务。下一步仅提交并推送迁移依赖修正，然后回到生产重新验证计划。
- 2026-08-20，生产部署完成：生产服务器 `192.168.20.2`（`ziliao`）从 `438f126559316328f745ac0fdf44130ec84fa447` fast-forward 到 `af38eb2201add52d626b38c4e4b48c73895a97cc`，工作树干净。重新验证迁移计划后只应用 `blog.0025` 至 `0028`，均成功；这些迁移创建 Markdown 导入批次、artifact、会话和 Token 审计表及索引，不触碰 BlogPage 正文、Revision、MongoDB 正文、MinIO 媒体或 Elasticsearch 索引。迁移前备份 `/home/source/Django/wagtail/backups/wagtailblog3-markdown-import-20260820-144325` 的校验清单仍存在且已验证。`collectstatic --noinput` 成功（4 copied、1488 unmodified、841 post-processed），随后按顺序重启 `wagtailblog3.service`、`wagtailblog3-celery-maintenance.service`、`wagtailblog3-celery-beat.service`；Filebeat、Nginx 和基础设施未重启。生产 `manage.py check` 通过，四个项目服务均为 active，`/admin/login/` 为 HTTP 200，maintenance Worker 已注册会话组装、过期与 cleanup retry 任务。`systemctl.md` 已含本功能服务契约，无需修改。回滚点为迁移前 commit `438f126...` 与已校验备份；本次新增表为向后兼容结构，若应用回退，先恢复旧 commit 并按服务顺序重启，未经单独授权不得回滚或删除审计表。残余风险：生产 AI 元数据服务仍只在配置完成且用户在客户端显式选择模板时才会向外部服务发送受限文本，未在本次部署中修改 `.env.production` 或执行真实 AI 请求。
