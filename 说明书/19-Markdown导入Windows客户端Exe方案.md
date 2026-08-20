# Markdown 导入 Windows 客户端 EXE 方案

## 1. 背景与目标

现有导入 API 和命令行客户端已经支持 Markdown 解析、媒体拆分、远程图片显式下载、幂等和未发布草稿，但用户需要复制多条 PowerShell 命令。新增一个 Windows EXE 向导作为薄客户端入口，降低操作成本，不改变服务端导入契约。

目标流程：输入站点地址和 JWT，读取可写博客索引页，扫描 EXE 所在目录的 Markdown，预检并显示问题，用户确认后导入未发布 BlogPage 草稿。

非目标：不在客户端发布页面、不保存 JWT、不修改生产配置、不让服务端抓取互联网、不替换现有 StreamField 或 MongoDB 存储边界。

## 2. 用户流程

1. 启动 EXE，填写网站地址（`IP:端口` 或完整 `http(s)` 地址）和 JWT。
2. 点击“测试连接”。只填 `IP:端口` 时自动补 `/zh-hans`；客户端调用 `limits` 和 `destinations`，显示媒体限制和音视频深度探测状态。
3. 选择有权限的 BlogIndexPage。界面展示标题和 ID，不允许手工输入未验证的父页。
4. 客户端扫描 EXE 所在目录顶层的 `*.md`。相对媒体路径以 EXE 目录为 `source_root`，不允许目录逃逸。
5. 选择 Markdown，执行本地预检。显示标题、简介、日期、标签、块数、媒体数、本地文件错误和远程图片数量。
6. 如果简介为空，用户在预检页填写简介；标题、日期和标签可从 YAML front matter 读取，必要时允许在导入前修正。
7. 远程 HTTPS 图片默认不下载，必须勾选“允许下载远程图片”后才会在本地下载、解码校验并上传。远程视频/音频仍按 embed 处理或按项目既有媒体规则处理，不由客户端抓取。
8. 点击“导入未发布草稿”并确认。客户端使用 UUIDv4 幂等键、有限超时重试和同一请求内的 manifest/artifact；服务端返回部分成功时保留成功媒体，并在失败媒体位置显示缺失标记。
9. 结果页显示状态、batch/page/revision、缺失媒体和后台编辑链接。页面保持未发布，用户自行在 Wagtail 后台复核。

## 3. 技术设计

- UI：Python 标准库 Tkinter/ttk，多步骤向导；网络和文件导入在后台线程运行，主线程只更新控件，避免大 Markdown 或媒体上传时窗口无响应。
- 复用：调用 `tools/markdown_import/client.py` 的 `inspect_markdown`、`import_markdown` 和 manifest 逻辑，不复制解析器。
- 打包：PyInstaller `--onefile --noconsole`，spec 显式加入 `wagtailblog3/apps` 的包路径和 Markdown 导入服务 hidden imports。构建产物只放本地 `output/`，不提交 `dist/`、`build/` 或 token。
- 目录：冻结运行时使用 `Path(sys.executable).parent`；源码运行使用 GUI 文件所在项目工具目录。首期只扫描顶层 `*.md`，避免递归扫描把无关笔记批量导入。
- 地址：输入无 scheme 时先补 `http://`；无 path 时追加 `/zh-hans`。禁止 `file://`、用户名密码、空主机和明显无效端口。
- 认证：JWT 只存在进程内变量，输入框默认掩码；不写日志、配置、崩溃报告或剪贴板。错误只显示脱敏的 HTTP 状态和服务端错误码。
- 幂等：每次选定文件的导入会话生成一个 UUIDv4 幂等键；请求内重试复用 manifest 和该键。成功后按钮禁用，防止误点重复创建草稿；用户重新选择文件时生成新键。
- 媒体：客户端先做路径、大小和图片解码预检，服务端再次以 Wagtail 表单做最终校验。单媒体失败不阻断其他媒体，失败位置由服务端组装独立 `markdown_block` 缺失标记。

## 4. 文件与修改范围

新增：

- `tools/markdown_import_gui.py`：Tkinter 向导入口和界面状态。
- `tools/markdown_import_gui_test.py`：地址规范化、扫描规则和 GUI 辅助逻辑测试。
- `tools/markdown_import_gui.spec`：PyInstaller 打包描述。
- `tools/build_markdown_import_exe.ps1`：Windows 本地构建脚本。

小范围修改：

- `tools/markdown_import/client.py`：允许 GUI 将预检页编辑后的 `title/intro/date/tags` 覆盖到一次 manifest；保留命令行兼容行为。
- `说明书/18-Markdown本地导入实施计划与任务包.md`：登记 T11 设计、模型角色、实施记录和验收证据。

不修改：API 路由、BlogPage 模型、StreamField key、MongoDB 契约、systemd unit、生产环境文件、服务端媒体补偿逻辑。

## 5. 验收与回滚

- 本地静态验收：Python 编译、GUI 辅助测试、现有导入回归、`git diff --check`。
- Windows 验收：双击 EXE，输入测试站点和 token，选择索引页，扫描 `第八章.md`，预检显示 59 块/29 图片且无路径错误；检查键盘 Tab 顺序、Token 掩码、错误提示和长文件名。
- 测试导入：仅使用测试环境和唯一 `test_run_id`，创建少量未发布草稿后按精确 page/revision/Mongo/media/object 清理并核验零残留。远程图片测试使用受控站点，不访问生产或未知第三方 URL。
- 回滚：删除 GUI 入口和打包脚本即可停止客户端；已有导入数据不能通过代码回滚删除，必须按现有 batch/artifact 补偿协议处理。

## 6. 残余风险

- Windows 机器必须安装与项目兼容的 Python/PyInstaller 才能重新构建 EXE；交付的 EXE 本身不需要用户安装 Python。
- 测试环境 limits 当前报告 `media_deep_probe=False`，本地 MP4/MP3 的可用性仍以服务端现有媒体表单和探测能力为准，GUI 只提前提示，不绕过校验。
- 公开前台页面和 Wagtail 预览仍受未发布页面与 Mongo 正文边界限制，客户端不强行发布或伪造前台预览。

## 7. 版本化方案：组装可见性、专用 Token 与多文件导入

### 7.1 当前问题证据

- `162/162` 只表示客户端已经完成 162 个 artifact 的上传，不表示 BlogPage 已组装完成。当前测试库最新会话为 `status=ready`、`completed_artifacts=162`，对应批次仍为 `pending`，说明 finalize 已完成状态切换，但组装任务没有被 Worker 消费。
- 测试 Worker `markdown-test-maintenance@ming` 与生产 Worker `maintenance@ziliao` 共用 Redis 的 `maintenance` 队列。生产 Worker 未注册新的会话组装任务，可能抢到测试任务并丢弃未注册任务。这是当前卡在组装阶段的主要根因，不能通过延长客户端轮询时间解决。
- 当前客户端把 finalize 后的所有状态都显示为“正在组装草稿：162/162”，没有区分“上传完成、任务排队、组装中、组装失败、组装超时”，因此用户无法判断真实状态。

### 7.2 目标

- 测试和生产的导入任务必须使用不同的 Celery broker/vhost 或至少不同队列名；测试任务不能被生产 Worker 消费。
- 客户端明确显示 `上传完成 -> 等待组装 -> 组装中 -> 成功/部分成功/失败`，服务端任务未消费或失败时返回可诊断状态，不无限卡住。
- 在 Wagtail Admin 中提供“Markdown 导入 Token”管理：管理员创建、查看元数据、撤销和设置过期时间；完整 Token 只在创建时显示一次，数据库只保存摘要，日志不记录 Token。
- 客户端默认填入 `http://192.168.20.5:8080/zh-hans`，可勾选“记住站点和 Token”；Token 使用 Windows Credential Manager/DPAPI 加密保存，不写明文配置、日志或 Markdown 目录。
- 客户端支持一次选择多个 `.md` 文件，并在同一索引页下创建多个独立未发布 BlogPage；每个文件拥有独立 session、batch 和幂等键，单文件失败不阻断其他文件。

### 7.3 认证设计

不再要求用户每次手工获取短时 JWT。新增专用 opaque import token，建议格式 `mdimp_<random>`：

- 模型字段：`user`、`name`、`token_prefix`、`token_hash`、`scopes`、`expires_at`、`revoked_at`、`last_used_at`、创建时间和最后使用 IP 的脱敏摘要。
- Token 为高熵随机值，只在后台创建成功响应中显示一次；列表只显示名称、前缀、范围、创建/过期/撤销/最近使用时间。
- 认证类只允许访问 Markdown 导入 API，默认 scope 为 `markdown_import`；不能用于 Django Admin 登录、普通 REST API、发布页面或生产运维。
- 导入 API 同时保留现有 JWT 兼容入口，但新客户端默认使用专用 Token。撤销、过期、用户禁用和权限变化必须在每次请求校验。
- 后台页面使用 Wagtail ModelAdmin/管理视图，创建和撤销均要求管理员权限；撤销是可审计的软状态变更，不删除历史 Token 记录。

### 7.4 客户端交互

- 连接页默认站点为 `http://192.168.20.5:8080/zh-hans`，Token 输入框保持掩码；“记住登录信息”复选框默认关闭，开启后只写入 Windows Credential Manager。
- 连接成功后显示 Token 有效期和导入权限，不显示完整密钥；Token 失效时提示“已过期/已撤销/无导入权限”，不要求用户复制 JWT 命令。
- 文件页改为多选 Treeview：支持全选、取消全选、按名称过滤和显示选中文件数/总大小。单击“预检”后按文件显示标题、媒体数、错误数和状态。
- 导入确认页显示“将创建 N 个未发布页面，目标索引页为 X”；上传进度按文件和媒体双层展示，并保留失败文件的“仅重试”入口。
- 结果页显示每个 Markdown 文件的 session/batch/page/revision、缺失媒体和后台链接；一个文件失败不隐藏其他成功页面。

### 7.5 多文件服务端边界

首期不把多个 Markdown 文件塞进一个页面或一个数据库事务。客户端对每个文件调用现有 session 协议：

1. 为每个文件建立稳定的文件 SHA-256、独立幂等键和独立 session。
2. 按受控并发 1-2 个文件顺序上传，避免同时创建大量 Wagtail 页面和媒体；单文件内部仍按 artifact 独立上传。
3. 每个文件 finalize 后单独轮询和记录结果；服务端只创建该文件对应的一个未发布 BlogPage。
4. 客户端 checkpoint 保存文件摘要、session_id、batch_id 和状态，不保存 Token；重启后跳过已成功文件，只重试未完成文件。

后续若需要 5000 个 Markdown 文件的任务级监控，再新增 `MarkdownImportJob` 父模型汇总多个 session；首期不引入跨页面事务或批量发布。

### 7.6 实施与验收

- T20.1：隔离测试/生产 Celery broker 或队列，并增加任务注册和未消费超时监控；先复现当前 `ready/pending` 状态，再验证任务进入 `assembling` 和终态。
- T20.2：专用 Token 模型、迁移、Wagtail Admin 创建/撤销/列表、scope/过期校验和审计测试。
- T20.3：客户端安全保存站点/Token、默认地址、组装阶段状态、轮询超时和失败重试。
- T20.4：多文件选择、逐文件 session 编排、checkpoint 恢复和结果汇总。
- T20.5：测试环境用 2-3 个 Markdown 文件验证成功、部分失败、重启恢复、重复重放和精确清理；再单独授权 1001/5000 媒体压力测试。
- 采用 UI/UX 验收：阶段状态必须可见，按钮提交后必须有 loading/成功/失败反馈，Token 字段有明确标签和掩码，失败信息靠近对应文件，不用颜色作为唯一状态线索。

### 7.7 模型/推理强度建议

- 队列隔离、Token 权限边界、撤销语义和多页面幂等：`gpt-5.6-sol + 高推理`，涉及生产 Redis/vhost、权限升级或回滚失败时必须复审。
- Django 模型/API、Wagtail Admin 页面、Tkinter 多选和客户端编排：`gpt-5.6-terra + 中推理`。
- 文档、界面文案、配置默认值和常规测试整理：`gpt-5.6-luna + 中推理`。
- 在用户确认方案并授权实现前，保持当前代码不变；本节 7.8 已获得实现授权并完成首版修复。

### 7.8 多选文件的元数据归属规则（2026-08-19）

多选导入时，不能使用一组标题、简介和标签隐式覆盖多篇文章。字段归属固定如下：

| 字段 | 归属 | 规则 |
| --- | --- | --- |
| 目标索引页 | 整批选择 | 所有选中的 Markdown 都在该索引页下各创建一个 BlogPage。 |
| 标题、简介、日期、标签 | 单个 Markdown 文件 | 文件清单每一行对应一篇文章；编辑哪一行，就只修改哪一个文件的元数据。 |
| 文章正文和媒体 | 单个 Markdown 文件 | 每个文件独立 session、batch、幂等键和页面，不与其他文件合并。 |
| 远程图片确认、服务端地址、Token | 当前导入会话 | 这些是连接/安全选项，不属于任何文章。 |

元数据来源按以下优先级处理：客户端文件行编辑值 > 该文件 front matter > 标题使用文件名（去掉扩展名），简介仍必须逐文件确认。预检页显示文件名、标题、标签和状态；导入时将该行的元数据传给对应文件的 API 请求，结果页按文件显示页面和批次。这样选择两个文件并填写两个标签时，两个标签只会落到当前正在编辑的那一行，不会产生“标签对应哪个页面”的歧义。

本次已在 `tools/markdown_import_gui.py` 实现逐文件预检、元数据清单和逐文件传参；索引页选择与现有多 session 导入协议不变。

### 7.9 重新导入、删除后重导与同标题规则（2026-08-19，方案增量）

#### 问题边界

`idempotency_conflict` 的含义不是“标题重复”。服务端对同一用户的 `(idempotency_key)` 保存一次请求指纹；同一个幂等键再次提交时，标题、简介、日期、标签、正文、媒体清单或导入选项任一字段变化，就必须返回 `idempotency_conflict`，防止网络重试误创建第二个不同页面。当前客户端 checkpoint 只按文件 SHA 和目标父页恢复，未把文章元数据纳入判断，且可能把已经成功或已经删除页面的旧 checkpoint 当成“继续上传”，这是本次报错的直接原因。

#### 三种用户意图

| 意图 | 判断条件 | 客户端动作 | 服务端语义 |
| --- | --- | --- | --- |
| 继续未完成导入 | checkpoint 对应 session 仍处于 `created/uploading/ready/assembling`，且文件 SHA、目标索引页、标题、简介、日期、标签和导入选项完全一致 | 复用原 `idempotency_key` 和 session，继续上传/轮询 | 幂等重放，返回同一批次，不创建第二个页面 |
| 新建另一篇草稿 | 用户主动选择“新建草稿”，或 checkpoint 对应 session 已经 `success/partial_success/failed/expired` | 生成全新的 UUIDv4；旧 checkpoint 标记为历史，不再自动复用 | 创建新的 batch、session 和 BlogPage，即使标题和正文相同也不复用旧页面 |
| 替换旧草稿 | 用户明确选择某个旧页面并确认替换 | 首期不自动删除或覆盖；打开旧页面供用户手工处理，随后再以“新建草稿”导入 | 不引入跨页面删除、Mongo 指针转移或媒体共享事务 |

删除旧 BlogPage 后，原 batch 审计记录仍保留，`result_page` 会因 Wagtail `SET_NULL` 变为空；原幂等键永远不能用于新页面。新导入必须生成新幂等键，即使文件、标题和标签完全相同。这样既能保留审计，又不会把新草稿错误地映射到已删除页面。

#### checkpoint v2 规则

checkpoint 增加 `request_fingerprint` 和 `session_status`，指纹至少覆盖：文档 SHA-256、目标父页 ID、标题、简介、日期、标签、远程图片确认和其他导入选项。客户端读取 checkpoint 后先查询 session 状态：

- 活跃状态且指纹相同：显示“继续上次导入”；
- 活跃状态但指纹不同：显示“文章信息已变化，将创建新草稿”，自动生成新 UUIDv4；
- 终态或 session/page 已不存在：默认进入“新建草稿”，不复用旧 key；
- 旧版 v1 checkpoint 缺少指纹：只能作为“待确认的未完成导入”显示，不能静默复用。

导入成功、部分成功、失败或过期后，checkpoint 不再参与默认续传；可以保留脱敏历史摘要供“查看上次结果”，但不得作为新请求的幂等键来源。客户端界面必须在确认框中显示“继续未完成导入”或“创建新的未发布页面”，不使用无说明的自动选择。

#### 同标题规则

Wagtail 在同一父页下按 slug 保证唯一，同标题页面可以存在，后续页面会得到自动递增 slug（例如 `title-2`）。因此同标题不是导入硬错误。预检阶段应返回同一索引页下匹配标题的现有页面摘要（页面 ID、发布/草稿状态和编辑地址），客户端显示警告：

`发现同标题页面：将创建新草稿，URL slug 可能自动追加后缀。`

首期默认允许用户继续创建；若产品后续需要“禁止同标题”，应增加明确的 `duplicate_title_policy=warn|block` 选项，不能复用幂等冲突码表达标题策略。删除旧页面后重新导入仍按新幂等键创建，不依赖标题是否释放。

#### 验收标准

1. 第一次导入成功后，修改简介或标签再次导入，客户端不再复用旧 key，创建新 batch/page。
2. 第一次导入成功后删除页面，再次导入相同文件和标题，创建新 batch/page，旧 batch 仅保留审计，不产生 `idempotency_conflict`。
3. 活跃上传中网络中断，文件和元数据不变时可继续；元数据改变时必须明确提示并转为新草稿。
4. 同索引页同标题预检显示警告但不误报幂等错误；Wagtail 生成的 slug 唯一。
5. 多文件导入中每个文件仍使用自己的元数据、session、batch 和页面；一个文件的重导不会改变其他文件。

#### 模型/推理强度建议

幂等生命周期、删除后审计、页面替换边界和跨 MySQL/MongoDB/MinIO 补偿使用 `gpt-5.6-sol + 高推理` 做规格复审；客户端 checkpoint、GUI 三态确认和测试使用 `gpt-5.6-terra + 中推理`；文档与重复检查使用 `gpt-5.6-luna + 中推理`。涉及实际删除页面、迁移或生产数据时，必须单独获得授权并升级 sol 门禁。本节当前仅完成方案，尚未修改代码或测试数据。
