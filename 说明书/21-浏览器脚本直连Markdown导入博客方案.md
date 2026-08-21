# 浏览器脚本直连 Markdown 导入博客方案

## 文档状态

- 版本：V0.4，登记 T23.0-T23.3 的实际实现、自动化验证和 AdGuard 分阶段实机结果。
- 日期：2026-08-20。
- 状态：T23.0-T23.3 已实现并通过定向自动化检查；AdGuard 已完成博客园注入、GM 能力及 `0.3.1`/`0.3.2` 文本响应诊断。`0.3.3` 已实机确认注入，但“连接并预检”表单触发存在兼容性故障，现按本记录修复；真实 Token、session/草稿写入、提交、推送和生产发布均未执行。
- 目标脚本：`wagtailblog3/static/vendor/Script/downlaod_markdown.js`。
- 关联文档：`06-API接口文档.md`、`17-Markdown本地导入博客方案.md`、`18-Markdown本地导入实施计划与任务包.md`、`19-Markdown导入Windows客户端Exe方案.md`。

## 24. 实施记录

### 2026-08-20 T23.0-T23.3 已完成（未提交）

- 实际修改：新增 userscript 预检接口、媒体引用计划服务和定向测试；扩展现有导入路由与 limits；实现脚本 GM 隔离配置、目标索引页选择、远程图片预检、标题/摘要/日期/标签、重复标题提示；新增 TEST 副本构建脚本。
- 实际文件：`wagtailblog3/apps/blog/markdown_import_api.py`、`wagtailblog3/apps/blog/services/markdown_import_prepare.py`、`wagtailblog3/apps/blog/urls.py`、`wagtailblog3/apps/blog/test_markdown_import_api.py`、`wagtailblog3/apps/blog/test_markdown_import_prepare.py`、`wagtailblog3/settings/base.py`、`wagtailblog3/static/vendor/Script/downlaod_markdown.js`、`tools/build_userscript_blog_import_test.ps1`。
- 验证：WSL2 `wagtailblog-test` 运行 31 个定向测试通过；`python manage.py check` 通过；`python manage.py makemigrations --check --dry-run` 报告无迁移变化；源脚本和 `output/userscript-blog-import/` 下的 TEST 副本均通过 `node --check`。
- 数据/服务影响：prepare 只解析请求内容并返回计划，不创建 session、batch、page、revision、MongoDB 正文或媒体对象；认证的既有 `last_used_at` 更新时间除外。未修改 service、systemd、Celery 或生产环境，`systemctl.md` 无需更新。
- 未执行：未安装 TEST 副本到 AdGuard，未输入真实 Token，未访问用户样本站点，未调用 `sessions/`、artifact 上传或 finalize；这些属于后续 T23.4-T23.6 的独立验收。
- 回滚：删除本批次未提交文件或恢复到 `ab88116b3cc4e68cf125e28e5552ba566ff35eeb`；删除 `output/userscript-blog-import/` 只会清除本地测试副本，不影响博客数据。
- 残余风险：AdGuard 对 GM API 与 `@connect` 的真实行为、第三方站点 DOM 变化和 Windows 浏览器实际注入尚未验证，不能据此宣称端到端导入可用。
- 实际模型/推理：用户指定的 `gpt-5.6-terra` 高推理；未外发源码、Token、正文或生产日志。

### 2026-08-20 AdGuard 实机兼容诊断与 `0.3.3` 修复（部分完成，未提交）

- 实机证据：用户将正式 `downlaod_markdown.js` 安装到 AdGuard；Edge Agent Window 打开博客园样本后只出现一个“导入博客预检”入口，标题自动识别为“DeepSeek Harness 命令大全”，摘要提取正常；GM 隔离存储/删除与 `GM_xmlhttpRequest` 能力检查通过。CSDN 样本被站点滑块安全验证拦截，未尝试绕过。
- 只读探针：本地探针仅记录请求 path、host、method 和 Authorization/Cookie 是否存在，不记录头值、请求正文或第三方正文。补齐本地 HTTP 的 CORS/PNA、`Content-Length` 和连接关闭响应头后，AdGuard 能收到 `destinations/` 响应；博客 API 请求携带哨兵 Authorization 且不携带 Cookie，跨 origin 302 未被跟随，Bearer 未到达重定向目标。
- 根因与修复：AdGuard 的文本 `GM_xmlhttpRequest` 对显式 `responseType: "text"` 的返回行为与常见 userscript 实现不一致。旧 `0.3.0` 仅读取 `response`，`0.3.1`/`0.3.2` 调整字段回退后实机仍没有取得正文。正式脚本升级为 `0.3.3`：JSON 文本请求完全省略 `responseType`，只为图片保留 `arraybuffer`；统一解析仍优先使用非空 `responseText`。根节点增加只读 `data-version`，用于确认 AdGuard 实际运行版本，不暴露配置或 Token。
- 自动化验证：Windows `node --check` 对正式脚本及 `0.3.3-test.1` TEST 副本均通过；Playwright 精确模拟“空 `response` + 有效 `responseText`”三段 JSON，并验证三个博客 API 均未传 `responseType`，图片请求显式使用 `arraybuffer`。四段预检结果为 1 个块、1 张图片、0 张失败；根节点版本为 `0.3.3`；Bearer 只出现在三个博客 API，请求图片时 Authorization/Cookie 均不存在；哨兵 Token 未进入 GM 持久配置、`localStorage` 或 `sessionStorage`。
- 后端回归：WSL2 `wagtailblog-test` 运行 `test_markdown_import_api`、`test_markdown_import_prepare`、`test_markdown_import_parser` 共 31 个测试通过；`python manage.py check` 通过；`python manage.py makemigrations --check --dry-run` 报告 `No changes detected`。
- 数据/服务影响：只调用本地只读探针和现有 prepare 级模拟；未调用真实博客 API、`sessions/`、artifact 上传或 finalize，未创建 page、revision、MongoDB 正文、媒体对象或生产数据；未修改 service/systemd，`systemctl.md` 无需更新。
- 当前门禁：`0.3.1` 和 `0.3.2` 实机均确认只收到 `destinations/`，错误为目标列表 `undefined`；`0.3.3` 尚待用户重新导入，下一轮先读取根节点 `data-version` 确认版本，再执行真实四段链路。G4 仍不得标记通过。
- 回滚：将脚本版本与响应读取表达式恢复为本批次前状态即可；本地探针和 Playwright 产物位于 Git ignored 的 `output/`，删除它们不会影响项目或博客数据。
- 残余风险：需重新导入 `0.3.3` 后验证真实 AdGuard 的 JSON、二进制和状态反馈；微信、知乎及被验证页拦截的 CSDN 尚未完成平台样本验收。真实 Token 和任何草稿写入仍需独立授权。
- 实际模型/推理：当前会话未暴露可核对的目标模型标签，使用实际可用模型完成诊断；未调用外部模型，未外发源码、Token、正文或生产日志。

### 2026-08-20 AdGuard 表单触发兼容性修复计划（进行中，未提交）

- 现状证据：在真实 Edge Agent Window 的博客园样本中，根节点 `data-version` 已确认是 `0.3.3`；原文标题和摘要正确显示。填写本地探针地址与固定哨兵 Token 后，点击“连接并预检”仍保留“博客地址不能为空”提示，探针请求记录为空，因此故障发生在 API 调用前。
- 目标：将预检按钮从依赖 `form method="dialog"` 的 submit 事件改为明确的普通按钮 click 事件；提取同一个预检处理函数，保留既有表单语义、键盘可达性、忙碌状态、错误反馈和只读 prepare 边界。
- 非目标：不变更 API、认证、GM 存储格式、图片下载、Markdown 转换、session/artifact/finalize、数据库、服务或生产配置。
- 实施文件：仅修改 `wagtailblog3/static/vendor/Script/downlaod_markdown.js` 与本方案实施记录；不修改 `systemctl.md`。
- 验证与门禁：`node --check`、现有 Playwright GM 模拟回归、真实 AdGuard 复测。实机仅使用 `localhost` 探针与哨兵值；成功标准为三个博客 API 和一个图片请求到达探针，前三者仅有 Authorization 存在标记且无 Cookie，图片无 Authorization/Cookie。不得调用 `sessions/` 或任何写入接口。
- 回滚：恢复脚本预检按钮的原 `type="submit"` 与 submit 监听即可；本次不触及博客数据或服务。
- 残余风险：第三方网站 DOM、CSDN 滑块及真实 Token/草稿写入仍未验收。
- 模型/推理强度建议：脚本/UI 局部兼容修复使用 `gpt-5.6-terra + 中/高`；只读证据收集和重复验收可使用 `gpt-5.6-luna + 中`；只有 Token 隔离、跨 origin 重定向或后续 T23.4 写入补偿出现新风险时升级 `gpt-5.6-sol + 高`。无论模型选择，必须通过上述自动化和实机只读门禁。
- 实际修改与验证：脚本升至 `0.3.4`，预检按钮改为 `type="button"` 并直接监听其 click，避免 AdGuard 中 `method="dialog"` 的 submit 路径未稳定触发。正式脚本和新建的 `0.3.4-test.1` TEST 副本均通过 `node --check`。以本机 Edge 无头实例运行现有 GM 模拟：状态为“预检完成：1 个块，1 张图片，0 张图片失败”；三个博客 API 仅出现 Authorization 标记且均无 Cookie，图片请求显式 `arraybuffer` 且没有 Authorization/Cookie；哨兵值未进入 GM 持久配置、`localStorage` 或 `sessionStorage`。
- 当前状态：等待用户在 AdGuard 手工重新导入正式 `0.3.4` 脚本（或独立 `0.3.4-test.1` 副本）后进行真实 Edge 只读复测。未输入真实 Token，未调用 `sessions/`、上传或 finalize；未创建页面、revision、MongoDB 正文或媒体；未修改服务，`systemctl.md` 无需更新。
- 实机复测结果：用户报告已更新 `0.3.4` 后，真实 Edge Agent Window 的根节点仍返回 `data-version = 0.3.3`，弹窗仍为旧 submit 版本；因此本轮未填写地址或 Token、未触发任何探针请求，已立即关闭会话和本地探针。G4 仍未通过，必须先在 AdGuard 界面确认已启用的扩展版本。
- 后续实机证据：禁用旧扩展后，真实 Edge Agent Window 已确认根节点 `data-version = 0.3.4`。通过原生 click 触发预检后，本地探针仅收到 `destinations/`，其中 Authorization 存在、Cookie 不存在；弹窗报 `Cannot read properties of undefined (reading 'destinations')`。这证明 0.3.4 的事件路径已恢复，但 AdGuard 返回正文仍被解析为空对象。
- 诊断调整：下一版只补充无敏感数据的响应形状诊断（状态码、`response`/`responseText` 的类型、字符串长度和对象键名），不记录或显示正文、Token、Authorization/Cookie 值、URL 查询参数或第三方内容。通过该诊断确定 AdGuard 正文所在字段后再做单点兼容修复；本轮继续禁止任何写入接口。
- 诊断版实施：正式脚本已升至 `0.3.5`，并生成 `0.3.5-test.1` TEST 副本。两份脚本均通过 `node --check`；Edge 无头 GM 模拟仍完整通过四段预检，根版本为 `0.3.5`，三个博客 API 只有 Authorization 标记且无 Cookie，图片请求保持 `arraybuffer` 且无 Authorization/Cookie，哨兵值未进入 GM 持久配置、`localStorage` 或 `sessionStorage`。等待 AdGuard 重导入后采集一次无敏感形状诊断。
- `0.3.5` 实机诊断结果：根节点确认 `data-version = 0.3.5`。原生 click 触发后，探针收到 `GET /blog/api/markdown-import/destinations/`，Authorization 存在且 Cookie 不存在；但 AdGuard 回调返回 `status=204; responseText=string(0); response=undefined`，与探针返回的 HTTP 200 JSON 不一致。会话与探针已立即关闭，未调用后续 API、图片或任何写入接口。该证据不足以安全推断正文应从哪个字段读取，G4 仍未通过。

## 1. 背景与现状证据

当前 userscript 已完成以下能力：

- 根据站点 host 选择正文 DOM 容器；
- 从 `document.title` 取得标题并按既有站点后缀规则清理；
- 使用内置 Turndown 与 GFM 规则把正文 HTML 转为 Markdown；
- 支持标题、段落、列表、引用、代码块、表格、删除线、任务列表和图片 Markdown；
- 在 Markdown 末尾追加原文 URL 来源声明；
- 下载 Markdown 和 Word 文件。

当前脚本使用 `@grant none`，并通过外部 `@require` 加载 Word 导出依赖。这个模式不适合保存博客导入 Token：页面脚本与外部依赖都不应获得 Token 访问能力。新增直连功能时必须使用 userscript 管理器的隔离存储和跨域请求能力，并移除不再需要的外部 Word 依赖。

博客服务端已经提供 Markdown 专用 Token 和完整导入协议：

- `GET /blog/api/markdown-import/limits/`：上传和会话限制；
- `GET /blog/api/markdown-import/destinations/`：当前 Token 用户可写入的 BlogIndexPage；
- `POST /blog/api/markdown-import/duplicate-titles/`：同标题只读检查；
- `POST /blog/api/markdown-import/preview/`：现有 block 预检；
- `POST /blog/api/markdown-import/sessions/`：创建幂等导入会话；
- `POST /blog/api/markdown-import/sessions/<session_id>/artifacts/<artifact_id>/upload/`：逐文件上传；
- `POST /blog/api/markdown-import/sessions/<session_id>/finalize/`：请求后台组装；
- `GET /blog/api/markdown-import/sessions/<session_id>/`：轮询终态。

Token 必须以 `mdimp_` 开头，具备 `markdown_import` scope，且关联用户仍处于启用状态。服务端每次仍会检查 `blog.add_blogpage` 权限和目标 BlogIndexPage 的 `can_add_subpage()`，因此脚本不能通过手工篡改 `target_parent_id` 越权写入其他索引页。

现有服务端 `parse_markdown_blocks()` 会按源码顺序保留普通 Markdown 字符串，并把独占图片、音视频、允许的 embed、Mermaid 和表格内图片引用接入现有媒体处理。BlogPage 正文、StreamField、MongoDB revision pointer、`mongo_content_id`、媒体补偿和未发布草稿创建均已有稳定实现，不应为 userscript 新建第二套写入服务。

### 当前工作区注意事项

方案核查时目标脚本处于 `AM` 状态：索引中是新增空文件，工作树中是 1,535 行脚本内容。这属于用户当前未提交工作，方案阶段不得重置、覆盖或重新暂存它。实施前必须重新读取 staged/worktree 差异，并只在工作树版本上做精准修改。

## 2. 方案结论

采用以下单一路线：

```text
第三方文章页面
  -> userscript 从当前已加载 DOM 提取标题和正文
  -> 复用现有 Turndown 生成 Markdown
  -> 博客 userscript prepare API 使用现有 Python 解析器生成 blocks/媒体引用
  -> userscript 按用户选择下载正文远程图片
  -> 复用现有 session/artifact/finalize API
  -> Wagtail 创建一个未发布 BlogPage 草稿
```

不再经过 Windows EXE，不启动本地端口，不把网页 URL 交给生产服务器抓取，也不依赖博客登录 Cookie。脚本仅向用户配置的博客地址发送 Bearer Token；第三方站点页面、图片 CDN 和其他网络请求绝不能携带该 Token。

“正文直接导入 Markdown block”的准确含义是：脚本生成的 Markdown 是唯一正文源，普通文本不会转换成 RichText 或另一种 HTML 数据结构；服务端仍通过现有解析器把连续 Markdown 原样保存为 `markdown_block`。为了让图片进入 Wagtail 媒体库，独占图片和表格内图片可以按现有导入契约拆成 `image_block` 或在 `markdown_block` 内改写为 Wagtail image embed。这不是二次改写文章，而是现有媒体导入的必要步骤。

## 3. 目标

1. 在当前“下载 Markdown”旁增加“导入到我的博客”入口。
2. 用户可以配置博客地址（域名或 IP:端口）与 Markdown 导入 Token。当前项目使用 `mdimp_` 前缀的不透明专用 Token，并非标准 JWT；界面和文档以真实实现命名。
3. 脚本连接博客后，只展示当前 Token 有权写入的 BlogIndexPage，并要求用户选择目标索引页。
4. 复用脚本现有 HTML→Markdown 结果，标题默认使用脚本现有标题候选，并允许导入前修改。
5. 用户可以选择是否把正文远程图片下载并上传到 Wagtail 媒体库。
6. 导入图片关闭时，不保留远程热链图片；图片转换成带 alt 的原图文字链接。
7. 导入图片开启时，按正文顺序上传，成功后由服务端替换为 Wagtail 图片引用；单图失败必须明确提示并由用户决定重试、移除或继续生成缺失标记。
8. 导入前显示标题、简介、日期、标签、目标索引页、Markdown 字符数和图片数量，并执行同标题检查。
9. 最终只创建未发布草稿，不自动发布、不覆盖同标题页面。
10. 导入过程支持幂等重试和会话恢复，不因重复点击创建重复页面。

## 4. 非目标

- 不实现 Windows 客户端中转、本地 HTTP 服务、自定义 URL scheme 或浏览器扩展原生消息。
- 不由 Django、Celery 或生产服务器抓取第三方网页 HTML。
- 不处理验证码、浏览器伪装、反爬绕过、登录墙或付费墙；脚本只读取用户当前浏览器已经正常显示的 DOM。
- 不依赖第三方网站 Cookie 完成博客认证；博客只使用 MarkdownImportToken Bearer 认证。
- 不自动生成 AI 简介和标签；首版使用页面描述/正文首段候选并允许人工编辑。
- 不批量导入多个页面、不定时同步、不监控原文更新、不自动覆盖已有文章。
- 不导入评论、导航、推荐、广告、附件或正文容器以外资源。
- 不自动发布页面，不自动删除任何既有 BlogPage、revision、MongoDB 正文或媒体。
- 不改变 `markdown_block` 存储 key、BlogPage 模型、StreamField 定义、MongoDB 正文协议或 Elasticsearch 索引契约。
- 不保留 Word 导出作为本功能依赖；原外部 `html-docx-js` 必须移除。若以后恢复 Word，需把依赖固定并独立复核供应链风险。

## 5. 用户流程

### 5.1 首次设置

1. 用户通过 userscript 菜单“博客导入设置”或导入弹窗中的“设置”打开配置。
2. 填写博客地址，例如 `https://blog.example.com/zh-hans` 或测试地址 `http://192.168.x.x:端口/zh-hans`。
3. 填写 `mdimp_...` Markdown 导入 Token；输入框默认密码样式，不回显完整值。
4. 点击“测试连接”。脚本依次请求 `limits/` 和 `destinations/`。
5. 成功时显示可用索引页数量和服务器限制；失败时区分地址、网络、Token、权限、超时和协议错误。
6. 用户明确选择“记住 Token”后才写入 userscript 隔离存储；可以随时点击“清除 Token”。

地址规范化沿用 Windows 客户端语义：缺少 scheme 时补 `http://`，只允许 `http`/`https`，拒绝用户名密码 URL 和非法端口；没有路径时补 `/zh-hans`。生产使用 HTTP 会明示“Token 将以明文链路传输”的风险，推荐 HTTPS。

### 5.2 单篇导入

1. 用户在受支持文章页点击“导入到我的博客”。
2. 脚本调用现有 `getData()` 确认站点适配器、正文元素和标题；正文不存在时停止，不打开空导入窗口。
3. 脚本克隆正文 DOM，不修改第三方页面原节点；在克隆中移除脚本、样式和导出 UI，并规范懒加载图片地址。
4. 根据图片选项生成 Markdown：
   - 开启导入：保留规范 HTTPS 图片 Markdown；
   - 关闭导入：在 DOM 转换前把图片替换为 `查看原图：alt` 链接，因此 Markdown 不再含图片语法。
5. 打开预检弹窗，加载有权限的目标索引页列表。
6. 用户检查并可修改：目标索引页、标题、简介、日期、标签、图片选项和来源声明。
7. 脚本调用同标题检查；发现重复只显示已有页面状态和 ID，不自动阻断或覆盖。
8. 用户点击“确认导入为未发布草稿”。按钮进入禁用/进度状态，避免重复提交。
9. 脚本调用 userscript prepare API，得到服务端解析后的 blocks、图片引用和内容指纹。
10. 若开启图片导入，脚本下载每个去重后的正文图片，校验响应、大小并计算 SHA-256。
11. 脚本创建现有导入 session，逐个上传 artifact，调用 finalize，并轮询直到 `success`、`partial_success`、`failed` 或 `expired`。
12. 成功页显示标题、page ID、revision ID、批次 ID、缺失图片和 Wagtail 后台编辑链接。

### 5.3 图片失败处理

图片下载发生在创建 session 之前，预检阶段先显示：成功、超限、类型不支持、网络失败或地址不安全。用户有三种明确选择：

- 重试失败图片；
- 从 Markdown 中移除失败图片并保留原图文字链接，再重新 prepare；
- 继续导入并为失败图片提交 `client_download_failed`，由现有服务端生成缺失标记。

默认推荐“移除失败图片并保留原图链接”，避免明知缺失仍创建大量占位。开始创建 session 后，关闭弹窗只能停止本地等待，不能伪装成服务端会话已取消。

## 6. userscript 界面设计

### 6.1 入口

现有悬浮菜单保留“下载 Markdown”，新增“导入到我的博客”。Word 入口及外部依赖移除。页面内必须始终提供“博客导入设置”入口；只有 AdGuard 兼容性探针确认 `GM_registerMenuCommand` 可用时，才额外注册 userscript 菜单命令，不能把它作为唯一设置入口。

### 6.2 导入弹窗

弹窗采用单列、分组表单，不把所有细节堆在悬浮菜单：

```text
导入到我的博客

连接：已连接到 example.com            [设置]
目标索引页：[技术文章（ID 12）       ▼]

标题：[可编辑标题                     ]
简介：[可编辑简介                     ]
日期：[2026-08-20]  标签：[Python, Django]

正文：12,540 字符 / 8 张图片
[✓] 下载正文图片并上传到博客媒体库
[✓] 保留原文来源声明

同标题检查：未发现 / 发现 1 篇已有文章

[取消] [确认导入为未发布草稿]
```

异步状态分为：连接博客、解析 Markdown、检查重复标题、下载图片、创建会话、上传 `n/总数`、后台组装、成功/部分成功/失败。状态区域预留稳定高度并使用 `role="status"`；错误使用 `role="alert"`，同时提供“重试”“返回修改”“打开后台”等恢复操作。

所有输入有持久可见 `label`，Token 可显示/隐藏但默认隐藏；长 URL、Token 错误和标题使用 `overflow-wrap:anywhere`，不造成横向滚动。按钮具备明确名称、禁用状态和键盘焦点；弹窗打开时聚焦标题或首个错误，关闭后焦点返回触发按钮。点击背景不应在上传中误关弹窗。

`ui-ux-pro-max` 本轮检索确认：设置提交必须显示加载、成功或错误反馈；字段错误靠近对应输入；长 Token/URL 必须可换行；多阶段导入应显示进度；成功需要明确确认，失败需要恢复路径。这些规则只补强现有视觉，不引入新 CSS/JS 框架。

### 6.3 样式隔离

新增元素、class 和 CSS 全部使用 `zuihuitao-` 前缀并限定在 `#zuihuitao` 或独立 dialog 根节点下，不新增影响第三方页面的全局选择器。不得依靠 hover 才能发现唯一导入入口；键盘和触控均可打开。

## 7. 配置与凭据设计

### 7.1 userscript metadata

把 `@grant none` 改为最小所需权限：

```text
@grant GM_getValue
@grant GM_setValue
@grant GM_deleteValue
@grant GM_xmlhttpRequest
@connect <测试博客主机/IP>
@connect <生产博客主机/IP>
@connect *  # 仅图片跨 CDN 下载确有需要时启用
```

AdGuard 官方兼容性文档已核实：AdGuard for Windows 支持 `@grant`、`@connect`、`@downloadURL`、`@updateURL` 等元数据，并明确支持 `GM_getValue`、`GM_setValue`、`GM_deleteValue` 和 `GM_xmlhttpRequest`。`GM_registerMenuCommand` 出现在官方示例授权块中，但没有进入同页的明确支持函数清单，因此首版只把它作为探针通过后的可选增强。实际开发仍要在本机 AdGuard `7.22.5282.0` 上验证存储持久化、跨域 JSON、跨域二进制、重定向和 `@connect` 行为，不能只凭文档断言运行时完全兼容。

官方来源：

- <https://adguard.com/kb/adguard-for-windows/features/extensions/>；
- <https://adguard.com/kb/general/extensions/>。

博客 host 应优先显式列出。由于受支持站点的图片可能来自多个 CDN，若首版必须通用下载图片，AdGuard userscript 可能需要 `@connect *`；这只是网络权限，不代表代码可以向任意主机发送 Token。实现必须在唯一请求封装中保证 `Authorization` 只在请求 origin 与已验证博客 origin 完全一致时添加，图片请求永不包含该头。若能力探针确认 `GM_registerMenuCommand` 可用，再单独增加对应 `@grant`。

脚本不得继续加载未经固定和审计的远程 `@require`。Word 导出依赖移除后，Token 相关代码和所有 GM 请求均来自仓库内可审查脚本。

### 7.2 存储键

建议使用版本化对象：

```json
{
  "version": 1,
  "site_url": "https://example.invalid/zh-hans",
  "token": "仅存于 GM 隔离存储",
  "remember_token": true,
  "last_destination_id": 12,
  "import_remote_images": true
}
```

不使用页面 `localStorage`、`sessionStorage`、DOM 属性、URL 参数或普通 Cookie 保存 Token。不在控制台、toast、异常文本、Markdown、请求体或文档中输出 Token。GM 存储不是加密保险箱，同一浏览器配置的本地用户或 userscript 管理器仍可能读取；界面必须说明并提供清除入口。

索引页只能记住 ID 作为下次默认值。每次打开导入窗口都重新请求 `destinations/`，确认该 ID 仍存在且有权限；失效时要求重新选择。

## 8. Markdown 与元数据契约

### 8.1 Markdown 唯一来源

新增 `buildMarkdownForBlog()` 复用当前 Turndown 实例、GFM 插件和正文选择器。下载 Markdown 与博客导入必须调用同一个纯函数，避免两条转换逻辑逐渐不一致。函数返回：

```text
title
markdown
source_url
source_declaration
image_candidates
```

Markdown 字符串保持 UTF-8 和现有 GFM 语义，不把它先渲染成 HTML 再提交。来源声明默认：

```markdown
原文来源：[页面标题](最终页面 URL)
```

### 8.2 标题、简介、日期与标签

- 标题：沿用现有 `getData()`/站点后缀清理结果，导入前可编辑；不能为空。
- 简介：依次使用 `meta[name=description]`、`og:description`、正文首个有效段落；清理空白后作为候选，用户可编辑且不能为空，服务端仍执行 5,000 字符上限。
- 日期：首版默认当前本地日期，允许用户修改为 ISO `YYYY-MM-DD`；不为每个站点新增发布日期适配器。
- 标签：可选，逗号分隔；最多 50 个，每个最多 50 字符，与服务端一致。
- 来源 URL：只写入 Markdown 来源声明，不新增 BlogPage 字段或数据库列。

### 8.3 服务端解析

userscript 不复制 Python 的 `parse_markdown_blocks()`、表格图片 occurrence ID、媒体 scope 或 artifact 分组算法。prepare API 统一返回这些信息。连续文本仍是原 Markdown 字符串的 `markdown_block`；独占图片等只按现有解析器规则拆分。

## 9. 新增 userscript prepare API

### 9.1 路由

```text
POST /blog/api/markdown-import/userscript/prepare/
Authorization: Bearer mdimp_...
Content-Type: application/json
```

这是只读预处理接口：除现有认证更新时间外，不创建 batch、session、页面、revision、MongoDB 正文或媒体对象。

### 9.2 请求

```json
{
  "target_parent_id": 12,
  "title": "文章标题",
  "intro": "文章简介",
  "date": "2026-08-20",
  "tags": ["Django", "Wagtail"],
  "markdown": "脚本生成的 Markdown 原文",
  "options": {
    "import_remote_images": true
  }
}
```

接口验证 Token、目标索引页权限、元数据、Markdown 类型/大小和 options。`markdown` 只允许字符串且不能为空；大小上限在 `limits/` 中返回，脚本在发送前也执行相同预检。具体默认值实施前结合 Django 请求上限确定，不在方案中假设无限正文。

### 9.3 响应

```json
{
  "status": "preview",
  "content_fingerprint": "sha256...",
  "blocks": [
    {
      "block_type": "markdown_block",
      "value": "原始 Markdown 文本",
      "source_start_line": 1,
      "source_end_line": 120
    }
  ],
  "required_artifacts": [
    {
      "position": 0,
      "media_type": "image",
      "source_kind": "remote_https",
      "normalized_source": "https://cdn.example.invalid/a.png",
      "reference_sources": ["https://cdn.example.invalid/a.png"],
      "reference_scope": "block_media",
      "occurrence_ids": [],
      "safe_filename": "a.png"
    }
  ],
  "summary": {
    "block_count": 4,
    "image_count": 3,
    "markdown_chars": 12540
  }
}
```

服务端以现有解析器结果构建 `required_artifacts`，同一来源 URL 去重但保留全部引用。返回的 blocks 与引用随后原样用于现有 session 创建；userscript 只能补充每个 artifact 的 UUID、上传字段、实际字节数、SHA-256 和下载失败代码，不自行修改 occurrence ID 或 scope。

### 9.4 错误码

复用现有认证/权限/元数据错误，并增加稳定错误：

- `userscript_markdown_required`；
- `userscript_markdown_too_large`；
- `userscript_options_invalid`；
- `userscript_remote_image_scheme_invalid`；
- `userscript_prepare_failed`。

响应和日志不得包含完整 Markdown、Token 或图片二进制。服务器日志只记录用户/Token ID、目标页 ID、字符/块/图片数量、耗时和错误码。

## 10. 现有 session/artifact/finalize 复用

prepare 成功后，userscript 构造现有 session manifest：

```json
{
  "target_parent_id": 12,
  "idempotency_key": "UUIDv4",
  "title": "文章标题",
  "intro": "文章简介",
  "date": "2026-08-20",
  "tags": ["Django", "Wagtail"],
  "blocks": ["prepare 返回的 blocks"],
  "artifacts": ["prepare 引用 + 下载结果"],
  "options": {
    "allow_external_images": true
  }
}
```

每个成功下载的 artifact 使用 `crypto.randomUUID()` 生成 UUIDv4，以 `crypto.subtle.digest('SHA-256', bytes)` 计算摘要，记录真实字节数和安全文件名。随后：

1. `POST sessions/` 创建或恢复幂等会话；
2. 使用服务端返回的 artifact ID 对应关系逐个 multipart 上传；
3. 所有 artifact 到达成功或失败缺失终态后调用 `finalize/`；
4. 轮询 `GET sessions/<id>/`；
5. 终态显示 `page_id`、`revision_id`、`batch_id`、缺失详情和错误码。

成功页面的后台地址按站点 origin 构造 `/admin/pages/<page_id>/edit/`，不带语言前缀。脚本只打开链接，不携带 Markdown Token；后台仍使用用户正常 Wagtail 登录认证。

## 11. 图片处理设计

### 11.1 地址规范化

在克隆 DOM 中按站点现有加载结果依次读取 `data-original`、`data-src`、`data-lazy-src`、`src` 和可靠 `srcset` 候选，并用当前页面 URL 转成绝对地址。只接受 `https://` 正文图片；拒绝 `data:`、`blob:`、`file:`、`javascript:`、协议相对 URL、带用户名密码 URL、localhost、回环/私网字面量和异常端口。

博客地址可以是用户明确配置的 IP:端口；这个例外仅适用于博客 API，不能用于第三方图片地址。

### 11.2 下载与验证

- 使用 `GM_xmlhttpRequest` 二进制响应下载，设置有限连接/总超时；
- 遵守 `limits.max_image_size`，超过上限立即终止；
- 响应必须非空且 Content-Type 为允许的图片候选；
- 文件名只取 URL path 尾部并清理控制字符、斜杠和超长值；
- 最终格式、伪装媒体和深度探测仍以服务端校验为准；客户端 MIME 只用于早期反馈；
- 同一规范 URL 只下载一次，多处引用复用同一个 artifact；
- 下载图片请求绝不携带博客 Bearer Token。

部分图片 CDN 依赖防盗链、签名、浏览器 Cookie 或页面 Referer，GM 下载可能失败。首版不承诺绕过；按失败处理流程由用户决定。

### 11.3 关闭图片导入

关闭图片导入时，在 Turndown 前把克隆 DOM 中每个 `<img>` 替换为普通 `<a>`：

```text
查看原图：<alt 或“未命名图片”>
```

这样生成的 Markdown 不包含图片语法，prepare 返回零图片 artifact，也不会让最终博客长期依赖第三方图片热链。

## 12. 幂等、恢复与并发

- 用户第一次确认导入时生成 UUIDv4 幂等键；同一内容、目标和元数据的重试复用该键。
- GM 隔离存储只保存最小 checkpoint：版本、页面 URL 摘要、内容指纹、目标 ID、幂等键、session ID、artifact 状态和过期时间；不保存 Token 副本、完整 Markdown 或图片二进制。
- 页面重新加载后重新从 DOM 生成 Markdown并调用 prepare；只有 content fingerprint、目标和元数据一致才恢复旧 session。
- 用户修改标题、正文、来源声明、标签、日期、图片模式或目标索引页后生成新的幂等键，不能复用旧 session。
- session 终态或过期后清除 checkpoint；服务端现有 24 小时 TTL 和补偿流程保持不变。
- 导入运行时禁用第二次提交；多个浏览器标签页仍由服务端“用户 + 幂等键”唯一约束兜底。
- 轮询使用有限总时长和退避；超时只提示“后台仍可能处理中”，不得自动创建新 session。

## 13. 安全与隐私边界

1. Token 只存 GM 隔离存储，只发送到已测试并锁定的博客 origin。
2. 博客请求封装每次比较 URL origin；任何重定向离开配置 origin 都不得继续携带 Authorization。
3. 页面可控制标题、Markdown、图片 URL 和可见 DOM，因此全部按不可信输入处理；不允许页面注入请求头、目标 API 或 Token。
4. 移除远程 `@require`，避免第三方依赖与 Token 共享 userscript 权限。
5. 不把博客响应 JSON 直接作为 `innerHTML`；所有标题、错误和索引页名称使用 `textContent`。
6. 来源 HTML 不发送到博客；只发送脚本生成并由用户预览的 Markdown。
7. 不调用 AI 接口，不向外部模型发送正文。
8. 不记录或回显 Token、完整正文、完整服务器错误页、图片二进制或个人 Cookie。
9. Token 可在 Wagtail 后台随时撤销；权限变化后 `destinations/` 立即收敛。
10. 来源声明与确认不代表版权授权；用户仍需自行确认保存和发布权限。

`@connect *` 若为跨 CDN 图片下载所必需，会扩大 userscript 网络能力。实现审查必须证明唯一 Bearer 注入点只接受博客 origin，并加入“恶意页面把图片 URL 指向其他主机时仍不泄漏 Token”的自动测试。

## 14. 异常与错误恢复

| 阶段 | 典型错误 | 用户可执行恢复 |
| --- | --- | --- |
| 设置 | 地址无效、Token 失效、权限不足 | 修改配置、清除 Token、重新测试 |
| 目标 | 无可写索引页、上次目标失效 | 刷新列表、改用其他 Token/账号 |
| 正文 | 不支持站点、选择器失效、Markdown 为空 | 停止导入、仅下载 Markdown、更新站点适配器 |
| prepare | 正文超限、图片 scheme 不安全、解析失败 | 修改/移除内容后重新预检 |
| 图片 | 超限、防盗链、超时、类型不支持 | 重试、移除并保留链接、继续缺失标记 |
| session | 幂等冲突、过期、服务器限制 | 核对内容指纹、恢复原 session 或明确新建 |
| 上传 | 大小/摘要不一致、单图失败 | 重新下载并上传；不得跳过为成功 |
| finalize | artifact 未完成、队列不可用 | 等待/重试 finalize，不新建页面 |
| 组装 | failed/partial_success | 展示 page/batch/missing，去后台人工检查 |

错误文案映射稳定错误码，不直接显示服务器 HTML、Traceback 或网络库对象。

## 15. 预计修改与不修改文件

### 15.1 预计修改

- `wagtailblog3/static/vendor/Script/downlaod_markdown.js`：GM metadata、配置、博客导入 UI、Markdown 共享函数、图片处理、API 客户端、幂等恢复；实施时保留用户当前工作树内容。
- `wagtailblog3/apps/blog/markdown_import_api.py`：新增只读 prepare view，并把媒体引用清单构建提取为可复用纯函数。
- `wagtailblog3/apps/blog/urls.py`：注册 prepare 路由。
- `wagtailblog3/apps/blog/test_markdown_import_userscript.py`：认证、权限、解析、图片引用、错误与无写入测试。
- userscript 纯函数测试文件：具体位置在实施前按项目现有 JS 测试方式确定，不为单个脚本引入完整前端框架。
- `说明书/06-API接口文档.md`：新增 prepare 接口、请求/响应和错误码。
- 本方案与 `18-Markdown本地导入实施计划与任务包.md`：实施授权后登记精确任务和实施记录。

### 15.2 原则上不修改

- BlogPage 模型和 StreamField block 定义；
- MongoDB 正文、revision pointer 与 `mongo_content_id`；
- MarkdownImportBatch/Session/Artifact/Token 模型和迁移；
- media importer、MinIO 生命周期和补偿契约；
- Celery 队列、Beat 计划、Elasticsearch 和 Filebeat；
- Windows Markdown 导入客户端和 PyInstaller EXE；
- `.env.test`、`.env.production` 与生产凭据。

如果实施发现必须修改上述“不修改”范围，立即停止并先更新方案、数据/服务影响和回滚，再获取用户确认。

## 16. 数据与服务影响

- prepare 是只读业务接口，不创建正文、草稿或媒体；认证仍会更新 Token 的 `last_used_at`。
- session 开始后沿用现有 MySQL 批次/会话/artifact、临时对象存储、Celery 组装和补偿。
- 成功时创建一个未发布 BlogPage revision，并按现有流程写 MongoDB 草稿正文；用户在 Wagtail 后台审阅后自行发布。
- 开启图片导入时创建 Wagtail 图片及 MinIO 对象；失败/过期按现有精确补偿处理。
- 不新增迁移、端口、队列或 systemd unit。
- prepare API 与静态 userscript 上线后，Django/uWSGI 需要按实际代码重启并执行 collectstatic；若共享 parser/service 未改，maintenance Worker 原则上无需因 prepare 路由重启，发布前以最终 diff 重新判断。
- `systemctl.md` 仅在服务职责、配置、队列、环境变量或重启步骤实际变化时更新；纯 API/静态脚本增量不预设修改。

## 17. 测试与验收

### 17.1 服务端自动化

- 无 Token、无 scope、过期/撤销 Token、非活动用户拒绝；
- 有 `blog.add_blogpage` 但目标无子页权限仍拒绝；
- destinations 只返回有权限的 BlogIndexPage；
- Markdown 为空、超限、非法元数据/options 拒绝；
- 普通 Markdown 保持字节语义并形成 `markdown_block`；
- 独占图片、重复图片、表格图片、代码块内伪图片和不安全 URL 的引用结果正确；
- prepare 不创建 batch/session/page/revision/MongoDB/media；
- prepare 返回 blocks 可直接通过现有 session `_blocks`/artifact 校验；
- Token、正文和图片 URL 不进入错误日志。

### 17.2 userscript 自动化

- 博客地址规范化、origin 锁定和 Bearer 头注入；
- GM 存储读写/清除，页面 `localStorage` 不出现 Token；
- 第三方/图片请求永不携带 Authorization；
- 标题、简介、日期、标签和来源声明生成；
- 懒加载图片地址归一化、去重、关闭图片时转文字链接；
- SHA-256、UUIDv4、artifact 清单和单图失败；
- destinations 刷新、上次目标失效、重复标题提示；
- 幂等键、checkpoint、终态清理、刷新恢复和轮询超时；
- 网络错误、非 JSON、401/403/409/410/413/5xx 映射；
- HTML 注入字符串只以文本显示。

### 17.3 WSL2 门禁

- 受影响 Django 测试；
- 现有 Markdown 导入、远程图片、session、补偿、Token 和解析器回归；
- `python manage.py check`；
- `python manage.py makemigrations --check --dry-run`；
- `python manage.py migrate --plan`；
- JS 语法检查与纯函数测试；
- `git diff --check` 和敏感信息扫描。

### 17.4 浏览器验收

使用 Playwright 可对本地合成文章页注入脚本并模拟 GM API，验证桌面和移动视口、键盘路径、浮层遮挡/溢出、长 URL、状态反馈、重复点击和控制台错误。Playwright 截图、trace、日志及 HTML 产物统一写入 `output/playwright/userscript-blog-import/`，不得提交。

真实 AdGuard 权限、`@connect`、GM 存储和跨域二进制请求需要 Windows 浏览器人工验收；只读阶段不得调用 session 创建。真实草稿和图片写入测试必须另行获得用户授权，并按精确 page/revision/session/batch/artifact/media/object ID 记录；清理同样需要独立授权并在执行后验零。

### 17.5 完成标准

1. 脚本能保存并验证博客地址/Token，页面无法读取 Token。
2. 索引页下拉只包含当前 Token 有权写入的页面，失效选择不会继续导入。
3. 当前脚本转换结果经服务端 prepare 后，正文文本保持 Markdown 并进入 `markdown_block`。
4. 图片关闭时无远程图片 embed/artifact；图片开启时成功图片进入 Wagtail media 并替换正文引用。
5. 同一确认操作重试不产生重复页面。
6. 最终只创建未发布草稿，成功/部分成功/失败均可诊断。
7. 既有 Windows Markdown 客户端和 API 无回归。

## 18. 实施任务与模型/推理强度建议

| 任务 | 内容 | 建议模型/强度 | 升级条件 | 验证门禁 |
| --- | --- | --- | --- | --- |
| T23.0 契约与 AdGuard 探针 | staged/worktree 基线、API、错误码、fixtures、独立 TEST 脚本、GM/`@connect` 能力 | `gpt-5.6-luna + 中`，探针实现用 `terra + 中` | AdGuard 文档与实际行为冲突、Token 隔离不成立时交 `sol + 高` | 原脚本未覆盖、探针无真实 Token、GM 存储/请求/二进制能力有实机证据 |
| T23.1 prepare API | 解析、引用清单、权限、限制、无写入测试 | `gpt-5.6-terra + 高`，`sol + 高` 安全复核 | 必须改模型/迁移/正文协议 | API 测试与现有 parser/session 回归通过 |
| T23.2 userscript 安全底座 | GM metadata、配置、请求封装、Token 隔离、连接/索引页 | `gpt-5.6-terra + 高` | Bearer 隔离或重定向边界不清时用 sol | 恶意页面/第三方请求不泄漏 Token |
| T23.3 Markdown 与图片 | 共享转换函数、图片模式、下载、摘要、artifact | `gpt-5.6-terra + 高`，`sol + 高` 复核媒体边界 | 需浏览器 Cookie 破解或服务端抓图 | 图片开/关、失败和去重测试通过 |
| T23.4 session 集成 | 幂等、上传、finalize、轮询、checkpoint | `gpt-5.6-sol + 高` | 补偿或重复页面风险 | 重试/恢复/过期/部分成功回归通过 |
| T23.5 UI 与双通道浏览器验收 | 弹窗、设置、进度、键盘/移动/错误恢复；Playwright 模拟 GM；AdGuard 管理的真实 Edge | `gpt-5.6-terra + 中/高`，清单用 luna | 第三方站点 CSS/事件严重冲突或实机 GM 行为不一致 | UI 清单、Playwright、真实 AdGuard userscript 验收均通过 |
| T23.6 测试写入与发布 | 测试草稿精确清理、提交、生产发布 | `gpt-5.6-sol + 高` | 任何生产数据/服务异常 | 独立授权、备份/回滚、健康检查齐全 |

模型建议是角色分配，不是强制切换命令。当前会话未调用外部模型，也未向外发送源码、正文、Token 或日志。升级到 `sol` 只用于 Token 边界、幂等、媒体补偿、测试数据清理和生产发布；常规 JavaScript/UI 实现优先使用 `terra`，文档和重复检查优先使用 `luna`。任何模型都不能替代本地测试、真实 GM 权限验证、生产授权和回滚门禁。

## 19. 发布、生产确认与回滚

### 19.1 发布门禁

代码实施和本地测试完成不等于授权提交、推送或生产发布。发布前必须重新确认：

- 目标脚本 staged/worktree 差异已正确收敛，未覆盖用户内容；
- userscript 不包含真实站点 Token、Cookie、个人数据或测试正文；
- `@connect` 与远程依赖清单经过安全复核；
- WSL2 测试和浏览器验收通过；
- 本地 `HEAD`、`origin/main`、GitHub 检查和生产目标 commit 精确一致；
- 生产工作树、分支、远程、依赖、服务和迁移计划重新核实。

生产发布预计包含静态脚本 collectstatic、Django API 代码同步和受影响服务重启。若最终无迁移，不执行迁移写操作；是否重启 maintenance Worker 以最终共享代码影响为准。发布后检查 Django、Worker/Beat/Filebeat、后台登录页、prepare 认证/权限、静态脚本、队列和日志。

### 19.2 回滚

- 客户端回滚：恢复上一版 userscript/静态文件并重新 collectstatic；已安装 userscript 的浏览器需要重新安装或更新旧版本。
- API 回滚：回退新增 prepare 路由/view/测试和共享纯函数；现有 Markdown 客户端 API 保持兼容，无需数据迁移。
- 本地凭据：用户可通过 userscript 设置清除 GM Token；代码回滚不能远程删除浏览器 GM 存储。
- 已创建草稿/媒体不会随代码回滚自动删除。只有在用户明确授权、精确记录 ID 并确认属于本次测试时，才按页面→revision/Mongo→media/object→session/batch 的既有规则清理。
- 不删除 MongoDB 正文、revision pointer 或生产媒体来“回滚脚本”。

回滚点以实施前 commit 为准；当前方案基线 HEAD 为 `ab88116b3cc4e68cf125e28e5552ba566ff35eeb`，但实施和发布时必须重新读取实际 SHA，不能把它当作永久目标。

## 20. 残余风险

- 第三方站点改版会导致正文选择器或标题清理失效；必须通过最小结构 fixtures 和人工预览维护。
- Turndown 无法无损表达所有复杂 HTML、公式、交互组件和 CSS 布局；最终仍需后台人工审阅。
- `@connect *` 可能是通用图片 CDN 下载的必要权限，但会扩大 userscript 网络能力；Token origin 锁定测试必须长期保留。
- GM 存储不等于系统级加密凭据库；本机同用户、恶意扩展或 userscript 管理器失陷仍可能读取 Token。
- HTTP 博客地址会明文传输 Token；生产应使用 HTTPS。
- 图片 CDN 防盗链、签名过期、Cookie/Referer 要求、格式伪装和超大文件可能失败。
- 关闭图片导入会改变原文视觉内容为文字链接，这是用户明确选择的降级，不应描述为无损导入。
- 浏览器页面可能构造恶意标题、Markdown 或图片 URL；服务端权限、解析、媒体探测与输出转义仍是最终边界。
- 来源声明和未发布草稿不能替代版权许可；发布责任仍由用户承担。

## 21. 需要用户确认的实施前决策

方案默认采用以下推荐值；进入代码实现前只需用户确认是否接受：

1. Word 导出与远程 `html-docx-js` 一并移除，只保留 Markdown 下载和博客导入。
2. 图片导入默认开启；关闭时图片转换为文字原图链接，不保留热链图片。
3. Token 默认不保存，用户勾选“记住 Token”后才写 GM 隔离存储。
4. 不启用 AI 简介/标签；简介使用页面 description/正文首段候选并要求人工确认。
5. 同标题只告警，不覆盖、不阻断，最终仍创建新的未发布草稿。
6. 首版一次只导入当前一篇文章。
7. 图片下载若需要通用 CDN，接受经过严格 Token origin 锁定测试的 `@connect *`；若不接受，则必须维护逐 CDN `@connect` 清单并承受站点改版维护成本。

## 22. 方案记录

- 2026-08-20：用户选择方案二，即 userscript 直接调用博客，并确认导入时必须选择有权限的 BlogIndexPage；正文以脚本现有 Markdown 为唯一来源，以项目现有 `markdown_block` 和媒体导入协议为准。
- 2026-08-20：本轮只读核对 userscript、MarkdownImportToken、destinations、preview、session/artifact/finalize、解析器、媒体组装、幂等和补偿代码，并使用 `ui-ux-pro-max` 检索设置验证、长 Token/URL、进度、确认和错误恢复规则。实际仅新增本方案文档，未修改目标脚本、API、模型、迁移、配置、`systemctl.md` 或现有用户暂存内容；未读取/写入真实 Token、未抓取或保存第三方正文、未调用 AI、未创建草稿/媒体、未执行测试数据、Git 写操作或生产操作。
- 2026-08-20：用户指定使用 Windows 桌面版 AdGuard 安装独立测试脚本并在其管理的浏览器中验收。本机只读核实 AdGuard for Windows `7.22.5282.0`、AdGuard BrowserExtensionHost 正在运行；`bsk` 已连接 Edge `151.0.0.0` 与 browser-skill 扩展 `0.1.6`，无版本偏差；`npx 11.13.0` 和 Playwright wrapper 可用。未读取 `adguard.db` 内容、未改 AdGuard 配置、未启动浏览器自动化会话、未安装脚本。

## 23. AdGuard for Windows 实施、联调与验收计划

### 23.1 测试渠道与边界

使用三个互相隔离的渠道，不能直接拿已安装的原始 `0.2.11` 做覆盖测试：

| 渠道 | 用途 | 是否真实 AdGuard | 是否允许写博客 |
| --- | --- | --- | --- |
| Node/WSL2 自动化 | JavaScript 纯函数、Django API、协议和无副作用断言 | 否 | 否 |
| Playwright 隔离浏览器 | 注入脚本并模拟 GM API，验证 UI、键盘、移动视口和错误恢复 | 否 | 否 |
| browser-skill Agent Window + AdGuard | 验证真实注入、GM 存储、`@connect`、跨域 JSON/图片和第三方站点兼容性 | 是 | 默认只读；另行授权后才写一篇测试草稿 |

Playwright 不能操作 AdGuard 原生 Windows 设置窗口，也不能替代 AdGuard userscript 运行时。`browser-skill` 可以控制已连接的真实 Edge Agent Window，但也不负责安装或配置原生 AdGuard；安装、启停和删除 TEST 扩展由用户在 AdGuard 界面完成一次人工确认。

### 23.2 独立 TEST 脚本构建规则

实施时从当前工作树中的 `downlaod_markdown.js` 机械生成测试副本：

```text
output/userscript-blog-import/downlaod_markdown.blog-import-test.user.js
output/userscript-blog-import/build-info.json
```

`output/` 已被 Git ignore。测试副本和构建信息不得提交、推送或部署。构建必须满足：

1. `@name` 明确包含 `[TEST]`，与已安装的原始脚本区分；
2. 使用项目自有、测试专用的 `@namespace`；
3. `@version` 使用仅含数字和点的递增测试版本，例如 `0.2.11.9001`；
4. 删除原 Greasy Fork 的 `@downloadURL`、`@updateURL`，防止测试副本被上游覆盖；
5. 不内置博客地址、Token、Cookie、测试正文或个人信息；
6. 按测试目标写入最小 `@grant`/`@connect`，不继承无关远程 `@require`；
7. `build-info.json` 只记录源 commit、工作树状态、生成时间和两个文件的 SHA-256，不记录 Token 或文章内容；
8. 生成前后都记录目标脚本的 SHA-256，确认没有覆盖用户当前 `AM` 状态中的 1,535 行工作树内容。

首选“本地文件导入”，因为 AdGuard 官方明确提供 **Extensions → Add extension → Import from file or URL**。首版不搭建临时 HTTP 服务，也不依赖自动更新。若后续频繁迭代确有必要，再单独验证只绑定 `127.0.0.1` 的 URL 安装和更新行为；该优化不是首版验收前置条件。

### 23.3 AdGuard 安装、升级和回滚步骤

每轮实机测试按固定顺序执行：

1. 代码侧完成语法检查、秘密扫描和 TEST 文件 SHA-256 核对；
2. 用户打开 AdGuard → Settings → Extensions → Add extension → Import from file or URL；
3. 选择上述 `.test.user.js`，确认显示 `[TEST]` 名称、测试 namespace 和预期版本；
4. 暂时禁用已安装的原始 `0.2.11` 脚本，避免两个脚本同时注入相同文章页；不得删除或覆盖原脚本；
5. 启用 TEST 脚本并完成当轮验收；
6. 新版本测试时先在脚本界面清除 TEST Token，再删除旧 TEST 扩展并导入新文件；不依赖 AdGuard 自动更新；
7. 验收结束先清除 TEST Token，再禁用/删除 TEST 扩展，最后重新启用原始 `0.2.11`；
8. 复查原脚本名称、版本和下载 Markdown 功能仍正常。

若测试中止，最小回滚就是“清除 TEST Token → 删除 TEST 扩展 → 重新启用原脚本”。这不会触碰仓库、博客数据或生产服务。若已经获得授权并创建测试草稿，浏览器回滚不等于数据清理，仍须按第 23.8 节单独处理。

### 23.4 T23.0 AdGuard 能力探针

正式实现博客导入前，先生成一个不含业务写入、只使用哨兵值的最小探针版本，验证：

- `GM_getValue`、`GM_setValue`、`GM_deleteValue` 在刷新和新标签页后的持久化行为；
- 哨兵值不出现在页面 `localStorage`、`sessionStorage`、Cookie、DOM、URL 和控制台；
- `GM_xmlhttpRequest` 能对显式 `@connect` 主机发送 JSON 请求；
- 能以 `arraybuffer` 或等价二进制模式下载一张小图片，并取得状态码、Content-Type 和字节数；
- 未列入 `@connect` 的目标被 AdGuard 拒绝或提示授权，行为可诊断；
- 301/302 跳转后，博客 Bearer 头不会被带到不同 origin；
- 图片请求永远没有 Authorization；博客请求只有在最终请求 origin 与已验证博客 origin 完全一致时才加 Bearer；
- `GM_registerMenuCommand` 是否实际可用；不可用时页面内设置入口仍完整工作；
- 脚本只在声明的文章 URL 上运行，iframe 中不重复注入。

Token 泄漏探针使用无权限哨兵字符串，不使用真实 Token。需要观察请求头时启用一次性本地探针服务，它只记录“Authorization 是否存在”的布尔值和请求 origin，不记录 header 值、正文、Cookie 或图片内容；探针输出写入 `output/userscript-blog-import/` 并在证据确认后按用户同意清理。

探针是硬门禁：任何 GM 能力缺失、跨 origin Bearer 泄漏或二进制下载不可用，都暂停 T23.2–T23.4，先更新方案。不得退回页面 `localStorage` 保存 Token，也不得改成向第三方图片请求附带浏览器 Cookie/博客 Token。

### 23.5 自动化与 Playwright 验收

#### A. JavaScript 与 Django

1. 对地址规范化、origin 比较、标题清理、Markdown 生成、图片 URL 归一化、去重、SHA-256、错误映射和状态机写纯函数测试；
2. `node --check` 检查最终 userscript 和 TEST 副本；
3. WSL2 在 `wagtailblog-test` 环境运行 prepare API、现有 session/media/parser 回归、`manage.py check` 和迁移检查；
4. prepare 的前后分别统计 batch/session/page/revision/media 等对象，证明只读调用没有创建内容；Token 的既有 `last_used_at` 更新时间除外；
5. 扫描源码、测试产物和 Git diff，确认无 `mdimp_` 真值、Cookie、Authorization 值或整篇第三方正文。

#### B. Playwright 模拟 GM

使用本地最小文章 fixtures 和可编程 GM mock，不访问真实文章、不调用真实写接口。至少覆盖桌面 `1440×900` 与移动 `390×844`：

- 下载 Markdown 与博客导入使用同一转换结果；
- 设置、目标索引页、标题、简介、日期、标签、图片选项和来源声明可操作；
- 全键盘打开、遍历、提交、取消和焦点返回；
- 长 URL/标题/Token 错误不溢出，无非必要横向滚动；
- 加载使用 `aria-busy`/`role=status`，错误使用 `role=alert` 并提供重试或返回修改；
- 上传中按钮禁用，快速双击只发起一次确认动作；
- 页面 CSS 不污染弹窗，弹窗 CSS 不污染第三方页面；
- 控制台无错误，静态资源和模拟网络无意外请求。

Playwright 截图、trace、HTML、日志全部写入 `output/playwright/userscript-blog-import/`。含真实 Token 的步骤不录 trace/HAR；测试截图中 Token 输入框必须为空或保持遮罩。

### 23.6 真实 Edge + AdGuard 调通流程

每个浏览器场景都使用独立的 `bsk` session，先 `bsk session start`，所有命令携带 session ID，达到单一成功条件后立即 `bsk session stop`。不借用用户普通标签页，优先在 Agent Window 新建页面。

开始前重新执行：

- `bsk status`：Edge、browser-skill 扩展连接且无版本偏差；
- AdGuard 进程、BrowserExtensionHost、TEST 扩展启用状态核对；
- 打开 AdGuard 官方测试页或等价只读检查，确认 Agent Window 的 Edge 确实受 AdGuard 过滤，而不是只证明浏览器扩展已连接；
- TEST 脚本名称、namespace、版本和 SHA-256 与本轮构建一致。

真实浏览器按以下顺序调通：

1. **注入烟测**：打开一篇支持站点文章，确认只出现一个 `[TEST]` 入口，原脚本没有同时注入；
2. **GM 探针**：完成第 23.4 节，不使用真实 Token；
3. **连接测试**：启动 WSL2 测试站点并绑定经核实的局域网地址/未占用端口，Windows Edge 能访问健康页；
4. **凭据人工输入**：若已有测试 Token，由用户通过 `bsk request-help` 在遮罩输入框中手动填写并完成保存；自动化不填写、不读取、不复制、不执行脚本提取；若无测试 Token，创建 Token 属于测试数据写入，需另行授权；
5. **只读 API**：依次验证 `limits/`、`destinations/`、`duplicate-titles/`、`prepare/`，不得调用 `sessions/`；
6. **页面预览**：核对标题、Markdown 字符数、图片数、目标 BlogIndexPage 和错误反馈；
7. **会话结束**：关闭设置后再截取必要证据，停止 `bsk` session，不留下借用标签页或 Agent Window。

不得用 `bsk evaluate`、开发者工具脚本或网络导出读取 GM 存储、Token、Cookie 或 Authorization。若遇到登录、验证码或人工确认，使用 `bsk request-help`，不尝试绕过。

### 23.7 真实站点样本矩阵

用户提供的 8 个 URL 用于只读提取和预览回归：CSDN 3 篇、微信公众号 2 篇、知乎专栏 1 篇、博客园 2 篇。首轮每个平台至少检查 1 篇，平台适配稳定后再跑完 8 篇：

| 样本 | URL |
| --- | --- |
| CSDN-1 | <https://blog.csdn.net/mrdeam/article/details/163469822?spm=1000.2115.3001.10525> |
| CSDN-2 | <https://blog.csdn.net/gedonshen/article/details/162231463?spm=1000.2115.3001.10525> |
| CSDN-3 | <https://blog.csdn.net/alex_goden/article/details/163305146?spm=1000.2115.3001.10525> |
| WeChat-1 | <https://mp.weixin.qq.com/s/Zqchrfa9AgYMcJ7I7l5MTw> |
| WeChat-2 | <https://mp.weixin.qq.com/s/CpKoUZg9HxFzEP6reEoIAw> |
| Zhihu-1 | <https://zhuanlan.zhihu.com/p/2072294521560360649> |
| CNBlogs-1 | <https://www.cnblogs.com/12lisu/p/22597163> |
| CNBlogs-2 | <https://www.cnblogs.com/zhangrunhao/p/22593934> |

上述 URL 只作为测试输入，不把抓取后的 HTML、Markdown 正文或图片二进制写入 Git。若测试时 URL 已失效或内容已变化，只记录状态并由用户决定是否更换样本，不静默替换成其他文章。

每篇只记录以下摘要证据，不保存全文：

- 最终 URL、平台、正文选择器命中数；
- 浏览器标题、清理后标题、是否需要人工修正；
- Markdown 字符数、代码块/表格/图片数量；
- 是否混入导航、评论、推荐、广告或脚本 UI；
- 懒加载图片是否解析为有效绝对 URL；
- prepare block 类型和媒体引用数量是否与预览一致；
- 控制台错误、请求失败和页面遮挡/溢出摘要。

判定规则：正文选择器必须唯一命中；标题不得为空且默认映射到 Blog `title`；正文必须至少产生一个非空 `markdown_block`；站点改版导致命中失败时该平台标记为未通过，不能靠扩大到整个 `body` 继续导入。

### 23.8 写入验收与精确清理门禁

前述步骤全部通过后，才向用户报告只读验收结果并申请一次独立写入授权。推荐最小写入是：

1. 仅在 WSL2 测试环境；
2. 仅选择一个明确的测试 BlogIndexPage；
3. 仅导入一篇包含普通文本、代码和至少一张图片的样本；
4. 图片导入开启，以同时覆盖 session、artifact、MinIO/Wagtail media、MongoDB 正文和未发布 revision；
5. 只创建未发布草稿，不发布、不覆盖同标题页面；
6. 图片关闭模式由自动化和 prepare 只读链路验收；若用户要求两个模式都做真实端到端，则需另行批准第二篇测试草稿。

写入前记录 MySQL/MongoDB/媒体对象的相关计数和目标父页；写入后记录 batch、session、artifact、page、revision、`mongo_content_id`、media 和对象键的精确 ID，只报告必要标识，不输出正文。若用户授权清理，只删除这批精确测试对象并逐层验零；未授权时保留草稿并报告残留，绝不能把代码回滚当成数据回滚。

### 23.9 分阶段通过标准

| 阶段 | 通过条件 | 失败后的动作 |
| --- | --- | --- |
| G0 基线 | 用户脚本工作树、原 AdGuard 扩展、SHA 和 Git 状态已记录 | 停止，先澄清文件归属 |
| G1 探针 | GM 存储/删除、跨域 JSON/二进制、`@connect`、Token origin 锁定全部符合预期 | 停止业务实现并更新方案 |
| G2 自动化 | JS、Django、回归、check、迁移检查通过；prepare 无内容写入 | 修复后重跑受影响层 |
| G3 Playwright | 桌面/移动、键盘、状态、错误恢复、无溢出、无控制台错误 | 修复 UI/脚本后重跑 |
| G4 AdGuard 只读 | TEST 独立安装，四类站点至少一篇通过，四个只读 API 通过，无 Token 泄漏 | 不得调用 sessions |
| G5 测试写入 | 获得授权；一篇未发布草稿和图片链路成功；重复点击不重复建页 | 停止并按精确 ID 评估补偿/清理 |
| G6 发布 | 再次获得提交、推送和生产授权；精确 commit、服务与健康检查通过 | 按第 19 节回滚 |

只有 G0–G4 完成时，可以宣称“只读联调通过”；只有 G5 完成时，可以宣称“测试环境端到端导入通过”；只有 G6 完成时，才能宣称“已发布”。各层状态不得相互替代。

### 23.10 本轮 WSL2 只读预检联调记录（2026-08-20）

本轮仅为定位 AdGuard `GM_xmlhttpRequest` 在本地探针中出现 `204` 空响应的原因，用户已授权将目标替换为真实的 WSL2 测试 Django HTTP 入口；不涉及 T23.8 的写入验收。

- 服务范围：仅在 WSL2 `wagtailblog-test` 环境以 `runserver --noreload` 临时启动，绑定 `192.168.20.5` 上经确认未占用的临时端口；不使用生产服务器、systemd、uWSGI 或现有服务重启。
- 测试身份：先只读核实是否已有活动超级用户及 `BlogIndexPage`。已有时仅创建一枚带固定本轮标签的短期 `MarkdownImportToken`；不创建用户、页面、revision、MongoDB 正文、媒体、对象存储内容或任何导入会话。
- 凭据处理：令牌明文仅通过浏览器设置弹窗由人工输入，不写入仓库、`output/`、终端日志、浏览器自动化产物或本方案；自动化不读取、复制或回显 Token、Cookie、Authorization 值。
- 验证边界：只验证 `limits/`、`destinations/`、`duplicate-titles/`、`prepare/` 和单张远程图片下载的请求形状及状态；明确禁止调用 `sessions/`、上传或 finalize。认证引起的 `last_used_at` 更新是预期的唯一数据库副作用。
- 清理顺序：停止浏览器会话和临时 `runserver`，撤销并删除该精确测试 Token，然后只读验零；同时复核 BlogPage、revision、导入 batch/session/artifact、MongoDB 正文和媒体的计数未因本轮改变。若任何只读接口意外产生内容对象，立即停止并报告，不自行删除。
- 成功判据：真实 Django 回调返回其实际 JSON 状态和可解析响应；若仍为 AdGuard 回调 `204` 空响应，则把问题定性为 AdGuard 运行时兼容性，不扩展到写入链路，先修订方案后再决定是否评估 `fetch`/CORS/PNA 替代方案。

#### 实施记录

- 2026-08-20（部分完成）：已在 WSL2 测试库只读确认存在 11 个 `BlogIndexPage` 和 1 个活动超级用户；无需新建用户或页面。创建后又删除了名称精确匹配的本轮临时 Token，删除后验零为 0；该 Token 未用于任何 API 调用。
- 2026-08-20（部分完成）：`WAGTAILBLOG_ENV=test` 的临时 `runserver --noreload 0.0.0.0:8012` 已启动并由 Windows 连通性检查确认可访问，之后已停止，端口 8012 已无监听。未使用 systemd、生产服务或生产环境。
- 2026-08-20（阻塞）：AdGuard 管理的 Edge Agent Window 打开 CSDN 样本时出现站点安全验证；已请求用户在浏览器中处理，用户要求先关闭再重新打开浏览器。为不保留临时凭据与服务，本轮已完成清理，尚未调用 `limits/`、`destinations/`、`duplicate-titles/`、`prepare/` 或任何写入接口。
- 数据与服务影响：仅临时 Token 的创建和删除；未创建 BlogPage、revision、batch、session、artifact、MongoDB 正文、媒体或对象存储内容。未提交、推送或发布；`systemctl.md` 未修改。回滚点为当前工作树；下一轮从干净的临时 Token 和临时服务重新开始。
- 2026-08-20（安全阻断）：重启 Edge 后以博客园样本联调。键盘 `Enter` 能稳定打开对话框，但请求在到达 Django 前由 AdGuard 的 `gm-xml-http-request` 桥接端点失败，WSL2 `runserver` 日志没有任何 API 请求。浏览器网络诊断还显示该桥接层会把认证请求头编码到可观察的桥接请求 URL；因此它不满足“Bearer 只到已验证博客 origin”的门禁。即使该桥接端点预期由本地 AdGuard 接管，也不能把这一未经隔离验证的行为作为安全实现依据。
- 2026-08-20（清理完成）：已停止 Agent Window 与临时 `runserver`，端口 8012 无监听；本轮精确临时 Token 已删除并验零。未调用 `limits/`、`destinations/`、`duplicate-titles/`、`prepare/`、`sessions/`、上传或 finalize；因此没有 BlogPage、revision、batch、session、artifact、MongoDB 正文、媒体或对象存储副作用。
- 后续决策：T23.5 的真实 AdGuard 只读验收未通过，T23.4/T23.6 及任何写入测试继续冻结。若要继续，必须先修订方案并获得确认：要么采用经可见 URL/重定向/代理链路审计后仍可证明不泄露认证头的 AdGuard 兼容机制，要么改为浏览器 `fetch` 并单独实现、测试 CORS/PNA 与 HTTPS 安全边界；不能以降低 Token 保护要求作为替代。

### 23.11 原生 fetch 传输替代方案（2026-08-20，已获实现授权）

#### 目标与边界

将 userscript 对博客 API 的 JSON 请求从 `GM_xmlhttpRequest` 改为浏览器原生 `fetch`，消除 AdGuard GM 桥接层对认证头的可观察 URL 序列化。服务器只对本脚本已声明的文章来源 HTTPS origin 提供跨域响应，不允许任意 origin、Cookie 凭据或跨源重定向携带 Bearer。

- 本批次仅覆盖 `limits/`、`destinations/`、`duplicate-titles/`、`userscript/prepare/` 的只读链路；不启用 `sessions/`、上传、finalize 或测试草稿写入。
- `fetch` 固定 `credentials: 'omit'`、`redirect: 'error'`、`mode: 'cors'`；Token 仅作为对经过 origin 校验的博客 API 的 `Authorization` 请求头发送，不写入 URL、页面存储、日志或响应文本。
- CORS 只匹配 userscript 支持站点的 HTTPS 来源及 `https://blog.csdn.net` 的子域，不回显 Cookie 凭据；`CORS_URLS_REGEX` 仅覆盖 `/blog/api/markdown-import/`。
- Private Network Access 仅在 Django 配置与已安装 `django-cors-headers` 实际支持时开启，并以 Chrome 预检响应为准；测试环境的 HTTP 局域网地址只用于联调，不作为生产安全结论。生产使用时要求 HTTPS 博客 origin 和 HTTPS API origin。

#### 实施与验证

1. 核实当前 `django-cors-headers` 4.7.0 的 PNA 配置键和中间件顺序；将 CORS 中间件置于会产生响应的中间件之前，并从全站默认范围收敛到 Markdown 导入 API。
2. 在 settings 中新增受限来源正则、允许方法/头、拒绝凭据和 PNA（若版本支持）的配置；保留其他 API 的既有同源行为，不增加通配 origin。
3. userscript 删除博客 JSON 请求的 `GM_xmlhttpRequest` 依赖，改用可解析 JSON 的 `fetch` 封装；异常只显示 HTTP 状态和稳定错误，不回显响应正文或认证材料。
4. 为预检 API 增加 CORS 允许、拒绝 origin、预检头和无 Cookie 凭据的测试；为 userscript 请求封装增加静态/纯函数检查。
5. 在 WSL2 测试站点执行 Django 检查、受影响测试、迁移检查和脚本语法检查；真实 Edge/AdGuard 只读验收时验证实际 API 到达 Django、响应可解析、无 `injections.adguard.org` GM 请求、无 Cookie，并在结束后删除测试 Token 和停止临时服务。

#### 文件、影响、回滚与模型建议

- 预计修改：`wagtailblog3/settings/base.py`、`wagtailblog3/static/vendor/Script/downlaod_markdown.js`、相关 Markdown 导入 API 测试、方案 21；仅在确认现有测试组织需要时增加小型 userscript 测试辅助文件。
- 明确不修改：BlogPage/StreamField/MongoDB/迁移、session/artifact/media/Celery/systemd、生产环境文件和 `systemctl.md`。
- 数据与服务：无迁移、无内容写入。未来生产发布时需要按实际配置重启 Django/uWSGI；本批次不发布、不重启生产服务。
- 回滚：恢复本批次 CORS 设置和 userscript `fetch` 封装；不涉及数据回滚。若浏览器预检失败或出现 origin 扩张，立即停在只读阶段。
- 模型/推理强度建议：CORS/PNA 与 Token 边界采用 `gpt-5.6-sol + 高推理` 复核；局部 Django/JavaScript 实现与测试采用 `gpt-5.6-terra + 中/高推理`；格式检查和浏览器证据整理采用 `gpt-5.6-luna + 中推理`。升级条件是跨服务 origin 契约、生产 HTTPS/PNA 差异、任何认证头可能泄露或 CORS 范围扩大；验证门禁是 CORS 拒绝用例、无 Cookie、重定向拒绝、真实浏览器请求路径和临时 Token 清理。实际使用以当前可用会话模型为准，不能替代本地测试和生产授权。

#### 实施记录

- 2026-08-20（部分完成）：userscript 升级为 `0.3.6`。博客 JSON API 改用原生 `fetch`，固定 `credentials: 'omit'`、`redirect: 'error'`、`mode: 'cors'`、`cache: 'no-store'` 与 15 秒 AbortController 超时；Bearer 仅在经 `blogApiUrl` 校验后的目标 API 请求头中出现。远程图片预检仍使用不带认证头的 GM 二进制请求，未扩大其权限。
- 2026-08-20（部分完成）：启用并前置 `corsheaders.middleware.CorsMiddleware`，CORS 范围收敛到 `/blog/api/markdown-import/`；允许来源为 userscript 支持站点的 HTTPS origin（含 CSDN 子域），拒绝 Cookie 凭据，并启用已核实由 `django-cors-headers 4.7.0` 支持的 PNA 预检响应。未修改模型、迁移、正文、会话、媒体、队列、服务或生产配置；`systemctl.md` 未修改。
- 2026-08-20（自动化通过）：新增 `blog.test_markdown_import_cors`，覆盖允许来源的 CORS/PNA、恶意或 HTTP 来源拒绝，以及 requestBlog 不再调用 GM。WSL2 `wagtailblog-test` 执行 `blog.test_markdown_import_cors blog.test_markdown_import_api blog.test_markdown_import_prepare` 共 23/23 通过；`manage.py check` 通过，`makemigrations --check --dry-run` 为 `No changes detected`，`migrate --plan` 无待执行迁移。`node --check` 已通过正式脚本和 TEST 副本；TEST 副本 `0.3.6-test.1` SHA-256 为 `196dde2320b0382ff3b92cd637d0731e26fcdbf12648a0bc9f2db51ddcd0d84b`。
- 2026-08-20（实机阻塞与清理完成）：用户确认已更新 TEST 脚本后，在 Edge Agent Window 的博客园样本刷新页面；无障碍树仍未出现“打开博客 Markdown 导入预检”入口，故未发生 CORS/PNA 请求，临时 `runserver` 日志也没有本接口访问记录。已停止浏览器会话和临时 `runserver`（PID `10252`），并删除名称精确匹配的临时 Token 后验零为 0。未调用 `destinations/`、`duplicate-titles/`、`prepare/`、`sessions/`、上传或 finalize，未产生 BlogPage、revision、导入对象、MongoDB 正文或媒体副作用。
- 2026-08-20（实机再次阻塞与清理完成）：用户确认已导入 TEST 副本后，重新创建浏览器 Agent Window，重新导航博客园样本并等待脚本加载；入口仍不存在，故请求未离开页面、临时 Django 服务没有收到 API 访问。已关闭该浏览器会话、停止临时服务并删除同名临时 Token 后验零为 0；仍未调用任何导入写入链路，未产生内容或媒体副作用。
- 残余验证：需先在 AdGuard 侧确认已启用名称为 `[TEST] Markdown Blog Import Preview`、版本为 `0.3.6-test.1` 的 TEST 脚本，并禁用同匹配范围的旧测试脚本；再在 WSL2 临时服务上完成只读 API 验收。写入链路继续冻结，直到 G4 重新通过。

### 23.12 Userscript 启动失败可见诊断（2026-08-20）

#### 目标、范围与验证

- 背景证据：TEST 脚本在 AdGuard 中已启用，但真实页面未出现入口；现有启动 catch 仅写入控制台，无法在不读取 AdGuard 内部日志的情况下获得实际异常。
- 目标：当 `runModernApp()` 初始化失败时，在页面右下角显示非交互、可访问的简短错误提示；错误文本只使用 `Error.message` 的截断结果并以 `textContent` 写入，不显示 Token、请求头或响应正文。
- 非目标：不改变 Markdown 转换、API、CORS、Token 存储、图片处理和任何导入写入接口；不会自动重试或发送网络请求。
- 修改文件：`downlaod_markdown.js`、其 TEST 构建副本、定向静态测试和本方案；不修改模型、迁移、服务、生产配置或 `systemctl.md`。
- 验收：Node 语法检查、静态测试断言、导入新版 TEST 后的页面可见错误或预检入口；实机仍只允许预检读取接口。回滚为移除该诊断函数和 catch 调用，不涉及数据。
- 模型/推理强度建议：局部脚本与测试用 `gpt-5.6-terra + 中`；浏览器证据整理用 `gpt-5.6-luna + 中`；只有异常文本涉及认证边界或生产跨域问题时升级 `gpt-5.6-sol + 高`。验证门禁仍是无 Token 回显、无写入和临时凭据清理。

#### 实施记录

- 2026-08-20（已完成，待实机诊断）：正式 userscript 升级为 `0.3.7`。`runModernApp()` 的启动异常现在同时写入控制台和右下角 `role="alert"` 提示；提示只通过 `textContent` 显示截断后的错误消息，不创建可操作控件，不读取或显示认证材料，也不发起请求。
- 2026-08-20（测试通过）：生成 TEST 副本 `0.3.7-test.1`，SHA-256 为 `a4fe36231d4093419fb95f0bef608723bf6a676e27eab9a8e4c97eecedc5eabd`。`node --check` 已通过正式与 TEST 脚本；WSL2 `WAGTAILBLOG_ENV=test` 运行 `python manage.py test blog.test_markdown_import_cors --verbosity 1`，4/4 通过。`git diff --check` 仍只报告 userscript 既有旧代码的 trailing whitespace，本批次未清理无关行。
- 残余验证：用户导入新版 TEST 副本后，在实际文章页记录右下角按钮或该诊断提示的准确文案；本项仍禁止调用任何写入接口。

### 23.13 延迟正文解析以保证入口可见（2026-08-20）

#### 目标、范围与验证

- 背景证据：普通文章页也未出现入口。现有 `runModernApp()` 在创建入口前调用 `articleData()`；正文选择器未就绪或站点结构变化时，整个初始化会结束，用户无法看到入口或继续诊断。
- 目标：入口创建只依赖页面 DOM；正文标题、摘要和 Markdown 在打开面板或点击下载/预检时才解析。解析失败显示已有的面板错误，不发送 API 请求。
- 非目标：不扩大站点选择器、不变更 Markdown、认证、图片、CORS、导入写入接口或数据模型。
- 修改：userscript、TEST 副本、定向静态测试和本方案；不修改服务、迁移、生产配置或 `systemctl.md`。
- 验收：静态检查保证根容器先于 `articleData()` 创建，Node 语法检查与定向测试通过；实机最低成功标准为受支持文章页显示入口。回滚为恢复初始化时的 `articleData()` 调用，无数据回滚。
- 模型/推理强度建议：局部脚本和测试使用 `gpt-5.6-terra + 中`；实机证据整理使用 `gpt-5.6-luna + 中`；仅在异常触及认证、跨域或生产兼容性时升级 `gpt-5.6-sol + 高`。门禁保持不写入和不回显 Token。

#### 实施记录

- 2026-08-20（已完成，待实机验收）：正式 userscript 升级为 `0.3.8`。入口根容器和按钮不再依赖 `articleData()`；首次打开面板才解析正文并预填标题与摘要，下载和预检继续在操作时重新解析正文。选择器失败时只显示面板错误，不会请求博客 API。
- 2026-08-20（测试通过）：生成 TEST 副本 `0.3.8-test.1`，SHA-256 为 `6ebeffb08bb3e3fbf257e179da7aa3dea70165a2c911076bacea72b83f8b4991`。正式与 TEST 脚本 `node --check` 通过；WSL2 `python manage.py test blog.test_markdown_import_cors --verbosity 1` 为 5/5 通过；`python manage.py check` 通过。`git diff --check` 仍仅报告 userscript 既有旧代码 trailing whitespace，本批次未清理无关行。
- 残余验证：重新导入 `0.3.8-test.1` 后，受支持普通文章页最低须显示“导入博客预检”入口；只读 API 验收仍冻结在该证据之后。

### 23.14 TEST 注入哨兵（2026-08-20）

#### 目标、范围与验证

- 背景证据：AdGuard 本地日志已确认 TEST 脚本完成重新安装，但页面仍没有入口，且宿主日志未记录匹配页面后的执行事件。
- 目标：仅在 TEST 构建副本的 metadata 之后插入无网络、无 GM API、无交互的固定“TEST 脚本已执行”哨兵；借此区分元数据匹配/注入失败与正式初始化失败。
- 非目标：不修改正式 userscript 行为、导入 API、Token、CORS、图片或写入链路。
- 修改：TEST 构建脚本、生成的忽略文件和本方案；不修改模型、服务、生产配置或 `systemctl.md`。
- 验收：TEST 语法检查通过；实机若哨兵不存在，问题归入 AdGuard 对页面的匹配/注入，若哨兵存在而正式入口不存在，再根据可见启动错误定位。哨兵本身不触发请求或数据副作用。
- 模型/推理强度建议：范围明确的构建脚本修改使用 `gpt-5.6-luna + 中`；仅当需要变更 AdGuard 注入策略或安全边界时升级 `gpt-5.6-terra + 高`。门禁保持不触发网络、不写入和不显示认证数据。

#### 实施记录

- 2026-08-20（已完成，待实机诊断）：TEST 构建副本在 metadata 后插入固定 `role="status"` 哨兵 `TEST userscript executed`；它仅创建页面文本，不使用 GM API、不发送网络请求，也不会进入正式脚本。正式 userscript 与业务代码未因本项改变。
- 2026-08-20（测试通过）：生成 TEST 副本 `0.3.8-test.2`，SHA-256 为 `9ef28153d9b599c14ffe656fa56575666686db9f2ea670773eaf4d5572e980f2`。TEST 脚本 `node --check` 通过；构建后静态断言确认哨兵存在于 TEST 副本且未泄漏到正式脚本。
- 2026-08-20（Agent Window 实机阻塞）：AdGuard 本地 agent 日志确认 `[TEST] Markdown Blog Import Preview` 已于 22:56 完成重新安装；在随后新建的 Edge Agent Window 导航博客园样本后，页面仍未出现 `TEST userscript executed`。本次未启动 Django、未创建 Token、未调用 API 或写入链路。该结果只能证明隔离 Agent Window 未获注入，普通用户窗口仍需以同一哨兵作独立观察。
- 残余验证：重新导入 `0.3.8-test.2` 后，普通文章页若未显示 `TEST userscript executed`，即可确认是 AdGuard 对该页面的匹配/注入问题；若显示哨兵而没有“导入博客预检”或出现启动错误，再按页面文字定位正式脚本。

### 23.15 本地化 API 路径与重定向拒绝修复（2026-08-20）

#### 目标、范围与验证

- 背景证据：普通 Edge 的博客园文章页已成功注入并能打开预检面板。以 `http://192.168.20.5:8080` 发起只读预检时，浏览器只显示“博客接口跨域请求失败”；无凭据 `OPTIONS`/`GET` 复核表明未带语言前缀的 `/blog/api/markdown-import/...` 被 Django `LocaleMiddleware` 重定向到 `/zh-hans/blog/api/markdown-import/...`。脚本固定 `redirect: 'error'`，因此必须修正首个请求地址，不能放宽重定向保护。
- 目标：userscript 仅对既有 Markdown 导入 API 生成带当前固定语言前缀的同源 URL；CORS 范围同时覆盖该受限的语言前缀 URL。继续拒绝跨 origin、带凭据、以及任何 HTTP 重定向。
- 非目标：不跟随或重写服务端重定向；不改变文章来源白名单、Token、Markdown 转换、远程图片、`sessions/`、上传、finalize、模型、迁移、正文、媒体、生产配置或服务。
- 实际修改：`wagtailblog3/static/vendor/Script/downlaod_markdown.js`、`wagtailblog3/settings/base.py`、`wagtailblog3/apps/blog/test_markdown_import_cors.py`、TEST 构建副本与本方案；不修改 `systemctl.md`。
- 验证：语言前缀 URL 的 Django `reverse` 与 CORS/PNA 预检均须通过；userscript 静态检查继续确认 `redirect: 'error'` 和 `credentials: 'omit'`；正式与 TEST 脚本执行 `node --check`。实机继续只调用 `destinations/`、`duplicate-titles/`、`userscript/prepare/`，不调用写入链路。测试 API 为 HTTP 时，HTTPS 文章来源仍可能受到浏览器混合内容保护；这不应通过降低 HTTPS 约束或放宽 CORS 处理。
- 数据/服务与回滚：无迁移、无内容写入；本轮唯一既有副作用是已创建的一枚测试 Token，须在本轮结束前精确删除。未重启生产服务。回滚为恢复本节修改的 API 路径和 CORS 正则；不涉及数据回滚。
- 模型/推理强度建议：跨 origin 与认证边界复核采用 `gpt-5.6-sol + 高`；局部 Django/JavaScript 与定向测试采用 `gpt-5.6-terra + 中/高`；构建和浏览器证据整理采用 `gpt-5.6-luna + 中`。若需要生产 HTTPS、证书、反向代理或放宽 CORS，必须升级安全审查并另获生产授权。验证门禁是实际首跳无重定向、无 Cookie、拒绝非白名单 origin、测试 Token 清理，以及零写入接口调用。

#### 实施记录

- 2026-08-20（进行中）：已确认本节根因和最小修复边界；尚未修改路径或 CORS 正则。浏览器仅尝试只读预检，尚未到达目标页列表、重名检查或 `prepare`，更未调用 `sessions/`、上传或 finalize。短时测试 Token 将在本节联调结束后精确删除。
- 2026-08-20（代码与自动化通过）：正式 userscript 升级为 `0.3.9`。`blogApiUrl()` 仅接受既有 Markdown 导入 API 路径，并直接生成 `/zh-hans/blog/api/markdown-import/...` 首跳地址；`redirect: 'error'`、`credentials: 'omit'` 与 origin 校验均未放宽。`CORS_URLS_REGEX` 仅增加同一受限 API 的可选 `zh-hans/` 前缀。新增测试通过 `reverse()` 锁定实际本地化 URL 和其 CORS/PNA 预检。WSL2 `wagtailblog-test` 执行 `blog.test_markdown_import_cors blog.test_markdown_import_api blog.test_markdown_import_prepare` 共 27/27 通过；`manage.py check` 通过，`makemigrations --check --dry-run` 为 `No changes detected`；正式与 TEST 副本的 `node --check` 通过。
- 2026-08-20（临时服务核验与清理完成）：未重启已存在的 `:8080` 进程，改以 WSL2 临时 `runserver --noreload` 绑定 `127.0.0.1:8012`。对本地化 `destinations/` 进行无凭据 `OPTIONS` 验证得到 `200`、精确 `Access-Control-Allow-Origin: https://www.cnblogs.com`、无 `Access-Control-Allow-Credentials` 与 `Access-Control-Allow-Private-Network: true`；临时服务随命令退出并确认 `8012` 无监听。浏览器已执行“清除 Token”、关闭面板并归还文章标签；测试库中名称精确匹配的短时 Token 已删除且验零。未调用 `destinations/` 成功响应、`duplicate-titles/`、`prepare`、`sessions/`、上传或 finalize，未产生 BlogPage、revision、batch、session、artifact、MongoDB 正文、媒体或对象存储副作用。
- 2026-08-20（待实机复测）：已生成 `output/userscript-blog-import/downlaod_markdown.blog-import-test.user.js`，版本 `0.3.9-test.1`，SHA-256 为 `bc53cb135d03329970c0b02a269aa72ca8c6df65ca387a248b4fa8d2b54377f2`。需由用户重新导入该 TEST 副本后，才能在普通 Edge 文章页验证新路径。测试 API 为 HTTP 时，HTTPS 文章来源还可能被浏览器混合内容保护；该问题须以测试/生产 API HTTPS 配置解决，不能通过放宽 CORS、跟随重定向或降低 Token 保护绕过。

### 23.16 Edge HTTPS 测试入口可行性（2026-08-20，待用户确认）

#### 背景、选择与建议

- 现状证据：`0.3.9-test.1` 已由用户导入。普通 Edge 文章标签的短时借用在本轮被浏览器侧取消，未进入页面、未创建 Token、未发起 API 请求。上一轮已证实 HTTPS 博客园文章来源对 `http://192.168.20.5:8080` 的原生 `fetch` 会报跨域失败；即使本地化 API 首跳已修复，HTTP API 仍可能被 Edge 的混合内容与 Private Network Access 策略阻断。
- 方案 A（推荐）：在测试环境为固定的私有开发域名提供受 Windows 信任的 HTTPS 反向代理（例如 Caddy/Nginx + 受信任开发证书），代理到 WSL2 的 Django 测试入口。证书 SAN、Windows hosts/DNS、`ALLOWED_HOSTS`、代理监听地址和端口都需先核实；浏览器脚本改填 HTTPS API 地址。CORS 仍只允许已列出的文章来源，继续无 Cookie、PNA 和重定向拒绝。复杂度为中等，需要一次 Windows/WSL2 网络与证书配置，但最接近生产安全边界，适合作为 G4 只读验收基线。
- 方案 B（仅短时诊断，不推荐常态使用）：在 Edge 中仅为特定的文章来源设置“允许不安全内容”，完成一次本地 HTTP 只读验证后立刻撤销。不得使用 `--disable-web-security`、全局禁用安全策略、任意来源 CORS 或常驻例外。该例外会降低浏览第三方文章时的页面保护，且不能替代 PNA/CORS/HTTPS 生产验收。
- 方案 C（不采用）：把测试服务通过公开隧道暴露到互联网以取得 HTTPS。它扩大测试 API 的网络暴露与 Token 攻击面，不符合本项目数据保护边界。
- 推荐决策：选择方案 A。先只实现和验证 HTTPS 测试入口；成功标准是普通 Edge 的受支持文章来源调用三个只读 API 均收到实际 JSON，且请求不带 Cookie、不经重定向、不调用写入链路。只有该门禁通过，才恢复后续写入任务评估。

#### 授权与模型建议

- 方案阶段不执行：安装/配置 Caddy 或 Nginx、生成或安装受信任根证书、修改 Windows hosts/DNS、防火墙、`ALLOWED_HOSTS`、端口监听、测试服务重启或任何生产操作。这些都需要用户针对准确目标另行确认。
- 模型/推理强度建议：证书信任、跨系统网络和 PNA 边界使用 `gpt-5.6-sol + 高` 复核；已确认范围内的反向代理与 Django 适配使用 `gpt-5.6-terra + 高`；浏览器证据整理使用 `gpt-5.6-luna + 中`。升级条件为证书信任范围超出本机、监听暴露到非私有网络、CORS 范围变更、生产复用或认证头处理变化。验证门禁为私有监听、精确主机名、HTTPS 证书受信任、CORS/PNA 拒绝用例、无 Cookie/重定向、临时 Token 清理与零写入。
- 2026-08-20（只读补充核实）：Windows 重启后 `127.0.0.1:8080` 当前无监听，不能据此判断 Edge 对 loopback 的行为；这表明原测试 Django 服务已停止，而非 CORS 或脚本失败。可把仅本机监听的 `http://127.0.0.1:<port>` 作为方案 A 之前的轻量诊断候选：它不暴露给局域网，且现代 Chromium 对 loopback 有特殊可信处理；但仍须先验证 Windows 到 WSL2 的 loopback 转发与实际 PNA/CORS 结果。若该转发不可用，需要用户单独授权配置精确的 Windows loopback 端口转发，或回到方案 A 的 HTTPS 入口。
- 2026-08-20（已获实施授权，进行中）：用户授权启动测试仓库 Django 服务，并尝试 Windows 到 WSL2 的 loopback 转发。实施范围限定为 WSL2 `wagtailblog-test` 的临时 `runserver --noreload`、`127.0.0.1` 可达性验证，以及随后仅三条只读 API 的浏览器预检；不修改生产、systemd、环境文件、模型、数据或写入接口。若自动 loopback 转发失效，才评估建立精确的临时 `127.0.0.1` 端口转发，完成后必须删除。
- 2026-08-20（只读实机验收通过）：WSL2 临时 Django 服务已以 `wagtailblog-test` 在 `0.0.0.0:8012 --noreload` 运行；Windows 自动将其转发到 `127.0.0.1:8012`，无需新增 Windows 端口代理规则。Windows 对本机地址的本地化 `destinations/` CORS/PNA 预检返回 `200`，来源精确回显为 `https://www.cnblogs.com`，无 Cookie 凭据许可。普通 Edge 博客园文章页确认显示 `TEST userscript executed` 与预检入口；以 `http://127.0.0.1:8012` 和短时 Token 执行预检，实际成功调用 `destinations/`、`duplicate-titles/` 与 `userscript/prepare/`，页面结果为“预检完成：1 个块，0 张图片，0 张图片失败”。
- 2026-08-20（清理完成，服务按用户要求保留）：已通过面板清除 Token、关闭面板、归还用户文章标签并停止 Browser Skill 会话；名称精确匹配的测试 Token 已删除且验零。未调用 `sessions/`、上传或 finalize，未创建 BlogPage、revision、batch、session、artifact、MongoDB 正文、媒体或对象存储内容。初始临时服务曾绑定 WSL2 `0.0.0.0:8012`；为避免其经 WSL2 网卡暴露，已停止并改为 `127.0.0.1:8012 --noreload`，Windows loopback 复核仍为可达。服务按用户“启动一下”的要求继续运行，访问地址为 `http://127.0.0.1:8012`，仅供本机本轮测试使用；停止该临时服务时应同时记录端口已释放。

### 23.17 TEST 副本固定 loopback 默认地址（2026-08-20）

#### 目标、范围与验证

- 背景：用户的测试仓库固定使用 `8080` 端口；本轮临时 `8012` 地址不应成为后续 TEST 脚本的默认或污染正式脚本配置。
- 目标：仅在 TEST 构建副本中使用独立 GM 配置 key，并预填 `http://127.0.0.1:8080`；正式脚本继续使用正式配置 key 和用户可输入的博客地址，不能写死测试环境。
- 非目标：不改变 API URL 规则、Token、CORS、表单标签/输入类型、Markdown、图片、写入链路、模型、迁移、服务或生产配置。
- 修改与验证：修改 `tools/build_userscript_blog_import_test.ps1`、正式 userscript 的版本元数据和本方案，重建 TEST 副本；检查生成内容包含测试 key 与 loopback 默认值而正式源码不包含测试 key，正式与 TEST 副本均通过 `node --check`。正式元数据仅同步已实现的 `0.3.9` 版本号，不改变正式站点地址或运行行为。不修改 `systemctl.md`。
- 数据/回滚：无服务和数据副作用。回滚为移除 TEST 构建时的两处文本替换；已写入旧 TEST key 的值与正式配置互相隔离，不会因本项删除。
- 模型/推理强度建议：范围明确的构建脚本修改使用 `gpt-5.6-luna + 中`；只有配置隔离触及 Token 持久化或正式脚本行为时升级 `gpt-5.6-terra + 高`。门禁为不回显 Token、不触发网络或写入、并保持正式/TEST 配置隔离。

#### 实施记录

- 2026-08-20（进行中）：已获用户实现授权，待修改 TEST 构建脚本与生成新副本。
- 2026-08-20（完成，未提交）：正式 userscript 元数据已同步为 `0.3.9`，但未写入固定测试地址或 TEST 配置 key。TEST 构建脚本为生成副本替换为独立 `zuihuitao.blogImport.test.v1` 存储 key，并设置默认 `siteUrl: 'http://127.0.0.1:8080'`。已生成 `0.3.9-test.2`，SHA-256 为 `3f205af369a17a15552d941a978d74ac92d031752ca30840040feac65c199ae0`；PowerShell 解析、正式/TEST `node --check` 和配置隔离静态断言均通过。无 Token、网络、数据或服务副作用；`systemctl.md` 未修改。
- 2026-08-20（测试服务端口对齐）：按用户确认的固定测试端口，已停止本轮临时 `127.0.0.1:8012` Django 服务并精确改为 WSL2 `127.0.0.1:8080 --noreload`。Windows `127.0.0.1:8080` 自动转发可达，本地化 `destinations/` 的 CORS/PNA 预检继续为 `200`。未新增 Windows 端口代理规则，也未触及生产服务。

### 23.18 预检后创建未发布草稿（2026-08-21）

#### 背景、目标与边界

- 背景证据：正式 userscript `0.3.9` 的“连接并预检”只调用 `duplicate-titles/` 与 `userscript/prepare/`，状态文字仍声明 session 写入尚未启用；截图中的用户无法在预检后选择创建草稿。
- 目标：预检成功后，显示独立的“创建未发布草稿”按钮。该按钮必须使用预检返回的块和媒体计划，走既有 `sessions/`、逐个媒体上传、`finalize/`、会话轮询链路，最终只在所选 `BlogIndexPage` 下创建未发布的 `BlogPage` revision。
- 非目标：不发布页面；不变更模型、迁移、Markdown 存储 key、MongoDB 草稿机制、媒体校验、认证、CORS、Celery 路由、服务、生产配置或 `systemctl.md`。不以直写 `/import/` 替代可恢复的 session 协议。
- 交互与安全：创建按钮在预检前或参数变更后禁用；点击先用原生确认框明确“不会发布”，防止预检被误认为写入。执行时禁用两个主操作并在已有 `role=status` 中报告会话、上传、组装和最终页面 ID；错误继续只显示稳定错误码，不显示 Token、响应正文或媒体内容。
- 实际修改文件：`wagtailblog3/static/vendor/Script/downlaod_markdown.js`、`tools/build_userscript_blog_import_test.ps1`、`wagtailblog3/apps/blog/test_markdown_import_cors.py`、本方案。不会修改 `systemctl.md`。
- 数据与服务影响：本地自动化不写入数据；用户确认的测试验收会创建一条未发布 `BlogPage`、revision、session/batch 审计记录，且仅当页面含远程图片时才创建测试媒体对象。不会启动或重启生产服务；测试环境需已有 maintenance worker，或仅本次临时服务明确启用 eager task 执行。
- 测试与验收：静态断言锁定 session/finalize/轮询及确认门槛；正式和 TEST 脚本通过 `node --check`；WSL2 运行受影响 Django 测试、`manage.py check` 和迁移检查。浏览器验收检查桌面与移动视口、键盘可达、禁用/忙碌反馈、请求无 Cookie/无重定向，并确认测试库中的草稿 `live=False`、存在 revision 且正文仍为 Markdown 字符串。
- 回滚：恢复本节 userscript、构建脚本和测试的最小 diff 即可停止新写入入口；若测试验收创建草稿失败，按既有 session 补偿记录处理，不能删除不属于本次测试的页面、MongoDB 草稿或媒体。已成功创建的测试草稿是否删除须由用户另行明确授权。

#### 模型/推理强度建议

- 建议：脚本与 Django 定向测试使用 `gpt-5.6-terra + 中`；浏览器证据整理使用 `gpt-5.6-luna + 中`；如会话补偿、媒体或测试数据删除失败，升级 `gpt-5.6-sol + 高`。
- 升级条件：媒体上传、MongoDB 草稿指针、幂等性、Celery 组装或跨域认证边界发生异常；生产、迁移、数据恢复或删除不在本节授权范围。
- 验证门禁：确认前零 session/上传/finalize 请求；确认后只写测试环境和未发布草稿；任何错误不得回显 Token；浏览器与 WSL2 测试均须通过。

#### 实施记录

- 2026-08-21（进行中）：已确认后端 session、上传、finalize 与异步组装接口已存在，当前阻塞仅为 userscript 未暴露写入动作。尚未创建测试 session、媒体、页面、revision 或 MongoDB 草稿，未触及生产或服务配置。
- 2026-08-21（代码与自动化通过，待浏览器写入验收）：正式 userscript 升级为 `0.3.10`。预检成功后才启用“创建未发布草稿”；任何地址、Token、目标页、图片选项或元数据改动都会使该按钮重新禁用。确认后按既有 session 协议创建会话、顺序上传仅保存在本次内存中的图片二进制、提交 finalize 并轮询结果；0 图片文章直接从会话创建进入组装。按钮使用原生确认明确不发布，忙碌状态、禁用状态和结果/错误文字均通过现有可访问状态区反馈。TEST 构建副本已生成，版本 `0.3.10-test.1`，SHA-256 `dc85ef6ec67ba703dd77a2627b3a755bb5190f56f211c7778cd14ccc50429996`。
- 2026-08-21（测试通过）：正式与 TEST userscript 均通过 `node --check`；WSL2 `WAGTAILBLOG_ENV=test CELERY_ALWAYS_EAGER=true` 运行全部 11 个现有 Markdown 导入测试模块，118/118 通过；此前定向的 CORS/API/prepare/task 测试 34/34 通过；`python manage.py check` 通过，`makemigrations --check --dry-run` 为 `No changes detected`。新增测试锁定 session 创建端点的受限 CORS，以及“预检 → 确认 → session/finalize/轮询”的脚本门槛。MySQL 测试数据库仅报告 Wagtail 已知的条件唯一约束警告。
- 2026-08-21（浏览器写入验收未执行）：Browser Skill 已连接到用户浏览器；借用指定博客园文章标签前被浏览器端取消，因此立即停止会话并未读取 Token、未调用写入接口、未创建测试 session、媒体、页面、revision 或 MongoDB 草稿。需要用户在当前浏览器重新导入本节 TEST 副本并在预检后的原生确认框授权，或允许重新借用该标签后，才能完成真实文章的最终写入验收。
- 实际模型/推理：当前会话使用实际可用模型完成脚本与测试，未向外部模型发送源码、Token、文章正文或日志；未发生生产操作。
- 2026-08-21（真实浏览器写入验收通过）：用户确认已更新至 `0.3.10` 后，在 Browser Skill 隔离 Agent Window 打开 `https://www.cnblogs.com/shanyou/p/22602322`，可见 TEST 注入哨兵和导入入口。使用已填的测试 API `http://127.0.0.1:8080`、目标索引页 `python（ID 36）` 执行预检，结果为 `5` 个块、`2` 张图片、`0` 张失败；点击“创建未发布草稿”并接受原生确认。浏览器最终显示“未发布草稿已创建：页面 ID 602，revision ID 1122”。
- 2026-08-21（数据与任务核验）：会话 `d580e960-e921-4604-ab6c-12161de20bb0` 与 batch 均为 `success`，两条媒体 artifact 均为 `succeeded`。只读核验页面 `602` 的父页为 `36`、`live=False`、`has_unpublished_changes=True`；revision `1122` 含有效 Mongo 草稿指针，草稿正文可读取为 `markdown_block → image_block → markdown_block → image_block → markdown_block`，其中 Markdown value 均为字符串。未发布页面，未操作生产数据。
- 2026-08-21（测试 Worker 清理）：首次 finalize 后测试环境未运行 maintenance Worker，session 停留 `ready`；临时启动的 WSL2 `maintenance` Worker 未消费该既有任务，遂直接调用相同的幂等任务函数完成该精确 session。随后已停止两条本次临时 Worker 进程并确认无残留；测试 `runserver 127.0.0.1:8080` 保持原有状态。
- 2026-08-21（后续会话卡住诊断与恢复）：只读核验发现测试 `runserver 127.0.0.1:8080` 正常，但无 maintenance Worker；两条旧会话均为 `ready/pending`，表示上传已完成、finalize 已提交而异步任务无人消费。经用户授权启动 WSL2 测试 Worker（仅 `maintenance` 队列、并发 1），并仅重投最新会话 `6d50a73f-80a9-4618-a136-1f0248afc656`；较早、不同标题的会话未改动，避免重复草稿。
- 2026-08-21（恢复验收通过）：重投会话由 Worker 消费并成为 `success`，创建未发布页面 `603`、revision `1123`；只读核验父索引页为 `36`、`live=False`、`has_unpublished_changes=True`。测试 Worker 保持运行（PID `2181` 主进程及其单一子进程），不涉及 systemd、生产服务或生产数据；`systemctl.md` 无需更新。停止它时应使用其精确 PID，避免影响其他测试任务。

### 23.19 微信公众号弹窗表单初始化兼容性（2026-08-21）

#### 背景、目标与边界

- 背景证据：用户在微信公众号文章页面打开弹窗后仅看到“导入到我的博客”标题，后续表单控件没有渲染；博客园页面相同操作正常。
- 根因：`runModernApp()` 已将 `siteInput` 加入表单后，又执行 `form.insertBefore(form.lastChild, siteInput)`，等价于把节点插到自身之前。部分页面环境会抛出 DOM 异常，阻断后续表单构建。
- 目标：以最小改动在创建时设置站点输入框 ID，并将其显式标签移动到输入框之前，避免自插入；微信公众号和既有站点均应完整显示表单。
- 非目标：不改变 API、Token、Markdown 解析、预检、会话、上传、草稿创建、数据模型、迁移、服务、生产配置或 `systemctl.md`；不创建、发布或删除草稿。
- 数据与服务影响：仅修改浏览器端 DOM 构建；无数据写入、无服务重启。测试 maintenance Worker 继续按既有用户授权运行。
- 测试与验收：新增静态回归断言，运行正式和 TEST 脚本语法检查、目标 Django 测试及 Django 检查；在微信公众号文章页验证桌面和移动视口的完整表单、关闭与键盘路径，以及控制台无初始化错误。若需要用户登录或人机验证，不绕过。
- 回滚：恢复本节 userscript、测试和方案文档的最小 diff；不会影响任何已存在草稿或媒体。

#### 模型/推理强度建议

- 建议：范围明确的脚本与静态回归测试使用 `gpt-5.6-terra + 中推理`；浏览器只读验收使用 `gpt-5.6-luna + 中推理`。
- 升级条件：若出现 Token、跨域认证、会话写入、媒体上传、生产数据或服务异常，升级到 `gpt-5.6-sol + 高推理` 并另行确认相应授权。
- 验证门禁：不得读取 Token、Cookie 或正文；浏览器验收只检查 UI 初始化，不触发预检或创建草稿。

#### 实施记录

- 2026-08-21（进行中）：已依据微信公众号截图定位自插入 DOM 异常，完成最小代码与回归断言修改；待构建 TEST 副本、运行 WSL2 检查和真实微信页面只读验收。
- 2026-08-21（代码与回归测试通过，待安装新版 TEST 副本）：正式 userscript 升级为 `0.3.11`，测试副本已生成为 `0.3.11-test.1`，SHA-256 为 `725ab9a3982cb586937f90417c66db4b30f4473ece50258c2070750428143062`。正式与 TEST 脚本均通过 `node --check`；WSL2 `wagtailblog-test` 运行 30 个定向测试全部通过，`manage.py check` 通过，`makemigrations --check --dry-run` 报告 `No changes detected`。真实微信公众号文章的已安装旧副本复现为仅显示弹窗标题，符合根因判断；Browser Skill 会话已停止。当前浏览器尚未安装新 TEST 副本，故完整表单的真实页面复验待用户在 AdGuard 中导入该文件后执行；未读取 Token、Cookie 或正文，未触发预检、会话、上传或草稿创建。`systemctl.md` 未修改，maintenance Worker 继续运行。
- 2026-08-21（CSDN 会话只读诊断）：最新会话 `796e652e-00ae-4dd4-8586-833d69daddcd` 为 `ready`、关联批次为 `pending`、媒体总数和完成数均为 `0`，已记录 `assembly_requested_at`，说明前端已提交 finalize 而没有进入组装。WSL2 maintenance Worker（PID 2181/2183）仍存活并报告监听 `maintenance`，但没有 active/reserved 任务；对测试 broker 的被动 `maintenance` 队列查询返回“queue not found”。未重投任务，因为该操作会触发实际未发布草稿创建，待用户确认后才可针对该精确会话重投。`systemctl.md` 未修改，未读取 Token、Cookie 或正文。

### 23.20 微信页面表单可见性与版本标识校正（2026-08-21）

#### 背景、目标与边界

- 背景证据：用户指出 TEST 文件元数据为 `0.3.11-test.1`，但脚本运行时 `blogImportVersion` 仍为 `0.3.10`。浏览器只读检查确认表单节点已被创建，而微信公众号截图仅显示标题，符合宿主页面广义 `form` 样式隐藏脚本表单的表现。
- 目标：运行时版本与 userscript 元数据同步，并在脚本根节点内强制表单为块级显示，避免微信公众号 CSS 隐藏该表单。
- 非目标：不推断或修改用户使用的脚本管理器；不变更 API、Token、预检、会话、上传、草稿、模型、迁移、Worker 或生产服务。
- 数据与服务影响：仅用户脚本元数据、运行时标识和 scoped CSS；无数据写入、无服务变更，`systemctl.md` 不更新。
- 测试与验收：静态测试锁定两个版本标识和 scoped CSS；正式与 TEST 脚本语法检查；Tampermonkey 更新后，微信页面须显示所有字段，键盘可关闭弹窗。浏览器验收不提交预检或创建草稿。
- 回滚：恢复本节的 userscript、测试和方案文档最小 diff，不影响现有草稿和媒体。

#### 模型/推理强度建议

- 建议：脚本和静态测试采用 `gpt-5.6-terra + 中推理`；浏览器只读验收采用 `gpt-5.6-luna + 中推理`。
- 升级条件与验证门禁：涉及认证、写入会话、媒体上传、生产或服务时升级审查；始终不读取 Token、Cookie 或正文。

#### 实施记录

- 2026-08-21（进行中）：已确认先前将运行时 `0.3.10` 标识误判为脚本管理器安装版本；待完成 `0.3.12` 构建、语法与定向测试，并由用户在 Tampermonkey 更新后复验微信页面。
- 2026-08-21（构建与测试通过，待 Tampermonkey 实测）：正式 userscript 已升为 `0.3.12`，内部 `blogImportVersion` 同步为 `0.3.12`，并加入 `#zuihuitao-blog-import form{display:block!important}`。TEST 副本为 `0.3.12-test.1`，SHA-256 `4329f673fd2808a0ccb65d64ce7b9817058c2be69a9d9b3cd246488f9d7edd99`；正式和 TEST 均通过 `node --check`，WSL2 定向 Django 测试 11/11 通过，`manage.py check` 通过，迁移检查为 `No changes detected`。未修改 `systemctl.md`，未读取凭据或触发写入接口。浏览器已确认旧版表单节点实际存在；新版尚未由 Tampermonkey 加载，需更新后复验可见性。

### 23.21 预检后切换目标索引页（2026-08-21）

#### 背景、目标与边界

- 背景证据：预检成功后，选择另一个已授权的目标索引页会触发通用 `clearPreparedImport()`，使“创建未发布草稿”被禁用；用户需要在预检后选择目标页并直接创建。
- 目标：保留正文块和图片预检结果；目标页变更时仅更新待创建 session 的 `target_parent_id` 并生成新的幂等键。创建前按当前目标页重新复查重复标题，再由既有 session 创建端点重新验证目标页权限。
- 非目标：不放宽后端权限校验、不复用跨目标页 session、不跳过重复标题提示、不变更 Markdown、媒体、模型、迁移、Worker、生产服务或 `systemctl.md`。
- 数据与服务影响：本次仅变更 userscript 客户端状态；用户点击创建后仍按现有确认框和 session 协议写入一条未发布草稿。测试与本次自动验证不执行创建。
- 测试与验收：静态测试覆盖“目标页不清除预检”“目标变更更新父页和幂等键”“创建前复查当前目标的重复标题”；正式和 TEST 语法检查、定向 Django 测试、Django check 与迁移检查。浏览器验收确认预检后切换到“考公（ID 559）”时按钮保持可点击，确认后才创建未发布草稿。
- 回滚：恢复本节 userscript、测试和方案文档最小 diff；已创建的任何草稿不受代码回滚影响。

#### 模型/推理强度建议

- 建议：userscript 状态与定向测试使用 `gpt-5.6-terra + 中推理`；浏览器验证使用 `gpt-5.6-luna + 中推理`。
- 升级条件与验证门禁：若后端权限、幂等、session 写入、媒体、生产数据或服务行为异常，升级高风险审查；确认前不允许 session、上传或 finalize 写入。

#### 实施记录

- 2026-08-21（进行中）：已确认根因是客户端将目标页与内容变更统一失效处理；后端 session 创建仍对目标页执行权限校验。待修改脚本状态逻辑、构建 TEST 副本和完成回归验证。
- 2026-08-21（代码与自动化通过，待 Tampermonkey UI 复验）：正式 userscript 已升为 `0.3.13`，目标页从通用失效监听中移除并单独调用 `updatePreparedDestination()`；只有父页实际变化才更新 `target_parent_id` 和幂等键。`createPreparedDraft()` 在 session 写入前对当前父页重查重复标题，目标权限继续由 session 创建端点验证。TEST 副本为 `0.3.13-test.1`，SHA-256 `1bcc87050c38b0fc3740d1c0c2f2e9f2b57f5060b426b4a0149c7df24efb1770`；正式和 TEST 通过 `node --check`，WSL2 32 个定向测试通过，`manage.py check` 通过，迁移检查为 `No changes detected`。未创建或发布草稿，未变更 Worker、服务或 `systemctl.md`。需要用户更新 Tampermonkey 脚本后进行浏览器 UI 验收。

### 23.22 求是网、人民网理论与共产党员网正文获取（2026-08-21）

#### 背景与现状证据

- 用户要求把求是网、人民网理论和共产党员网的三个指定文章页纳入现有 `InterfaceList` 选择器框架，不使用整页 `body` 兜底。
- Browser Skill 在隔离 Agent Window 中只读核验：求是网页面标题无需截断，唯一 `.highlight` 容器含 22 个正文段落、首尾均为文章内容；人民网理论页的 `#rm_txt_zw` 含 25 个正文段落并止于报纸版次，不含标题、责编和推荐区；共产党员网页面的 `#font_area` 含 25 个正文段落并止于作者说明，不含标题、发布时间和页脚。
- 人民网指定 URL 实际保持 `http://theory.people.com.cn`，当前 CORS 策略拒绝该来源；仅增加 userscript 匹配会导致面板可见但预检被浏览器拦截，因此需增加该精确主机的 HTTP/HTTPS 来源规则，其他 HTTP 文章来源继续拒绝。

#### 目标、非目标与实施范围

- 目标：为 `www.qstheory.cn`、`theory.people.com.cn`、`www.12371.cn` 增加 userscript `@match` 与正文选择器；按页面标题格式分别保留原题、在 ` --` 前截断人民网标题、在 `_` 前截断共产党员网标题；预检和后续现有流程继续只接收选定正文容器生成的 Markdown。
- 非目标：不增加通用正文推断或 `body` 回退，不修改 Markdown block key、图片上传、预检/session/finalize、草稿状态、Worker、模型、迁移、生产部署或服务配置；本次自动验收不调用 API，不创建、发布或删除草稿。
- 实际修改文件：`wagtailblog3/static/vendor/Script/downlaod_markdown.js`、`wagtailblog3/settings/base.py`、`wagtailblog3/apps/blog/test_markdown_import_cors.py`、本方案；重新生成 Git ignored 的 TEST userscript。`systemctl.md` 不修改。
- 不修改文件：Markdown 解析和组装服务、Wagtail 页面模型、MongoDB/媒体逻辑、URL、Celery 与 systemd 文档。

#### 数据、服务、安全与异常路径

- 数据与服务影响：浏览器只读提取和 CORS 来源匹配无数据库或媒体写入，不启动、停止或重启 Worker/服务，不触及生产；CORS 仍只覆盖 Markdown 导入 API，Bearer 请求保持无 Cookie、无凭据跨源。
- 安全边界：新增 HTTP 例外只匹配 `theory.people.com.cn` 的精确 origin，不允许其子域、任意人民网域或其他 HTTP 站点；正文容器缺失时继续明确报错，不退回整页抓取。
- 异常与残余风险：站点改版导致选择器变化时会停止提取；三个样例页均无正文图片，现有 `data-original`、`data-src`、`data-lazy-src` 图片归一化逻辑本批次仅做回归保护，不能证明这些站点未来所有图片模板均相同。

#### 测试、验收与回滚

- 自动测试：静态断言锁定三个 `@match`、host/selector/title 截断映射、正式与运行时版本一致；CORS 测试验证三个新 HTTPS 来源和人民网精确 HTTP 来源被允许，同时 `http://www.cnblogs.com` 与恶意来源仍被拒绝。
- 验收：正式与 TEST userscript 运行 `node --check`；WSL2 `wagtailblog-test` 运行定向 Django 测试、`manage.py check` 与 `makemigrations --check --dry-run`。Tampermonkey 更新后在三个样例页确认入口、标题和正文转换可用，桌面与移动视口无溢出或遮挡、键盘可操作、控制台无脚本错误；不点击“连接并预检”或“创建未发布草稿”。
- 回滚：恢复本节四个受版本控制文件的最小 diff并重新生成旧 TEST 副本即可；无数据、迁移或服务回滚步骤，既有草稿和媒体不受影响。

#### 模型/推理强度建议

- 建议：浏览器 DOM 证据收集使用 `gpt-5.6-luna + 中推理`；userscript、精确 CORS 规则与定向测试使用 `gpt-5.6-terra + 中推理`。
- 选择理由：正文选择器与来源规则边界清晰、修改局部；不需要架构变更或生产数据判断。
- 升级条件：若需通用正文推断、跨站模板兼容、认证边界放宽、生产发布、数据写入或服务操作，升级到 `gpt-5.6-sol + 高推理` 并重新确认授权。
- 验证门禁：选择器必须有真实 DOM 证据且不得为 `body`；HTTP 仅允许精确人民网理论来源；正式/TEST 语法、定向测试和 Django 检查全部通过后才交付。

#### 实施记录

- 2026-08-21（进行中）：已完成三个指定页面的 Browser Skill 只读核验并停止会话；确认正文选择器分别为 `.highlight`、`#rm_txt_zw`、`#font_area`，确认人民网页面保留 HTTP scheme。尚未修改运行代码、调用博客 API、创建 session/媒体/页面/revision 或变更服务。
- 2026-08-21（代码与自动测试通过）：正式 userscript 升级为 `0.3.14`，加入三个站点的 `@match`、正文选择器和标题截断规则；CORS 增加求是网、共产党员网 HTTPS origin 及人民网理论精确 HTTP/HTTPS origin，其他 HTTP 来源仍拒绝。TEST 副本为 `0.3.14-test.1`，SHA-256 `ce46c5d63f1989c125a5da141847a23fa3c1e96cc8c61b8b1adf898b53e7d223`。正式和 TEST 脚本均通过 `node --check`；WSL2 定向测试 14/14 通过，`manage.py check` 通过，`makemigrations --check --dry-run` 为 `No changes detected`。
- 2026-08-21（浏览器只读验收通过）：使用 Playwright 在网络写入桩下加载正式 `0.3.14`，三个页面均显示导入入口并完整打开弹窗；正文选择器各匹配 1 个元素，段落数为 22/25/25，标题字段分别与预期原题、人民网截断题、共产党员网截断题一致。1440×900 与 390×844 下弹窗均位于视口内且无横向溢出，创建按钮在预检前保持禁用。控制台仅有目标站既有统计/留言资源错误和 12371 旧页面的 `orientNotice` 错误，没有 `blog_import` 错误。未点击预检或创建按钮，未调用博客 API、创建数据、变更 Worker/服务或修改 `systemctl.md`；Playwright 会话已关闭。实际使用当前可用模型完成实现与验证，未调用外部模型或发送源码、凭据、正文和日志。

### 23.23 求是网预检 CORS 运行进程刷新（2026-08-21）

#### 背景、现状证据与目标

- 用户在 `https://www.qstheory.cn/20260818/7911cff28c634f90bd85416278b8df0d/c.html` 使用 `0.3.14` 连接 `http://127.0.0.1:8080` 时收到“博客接口跨域请求失败”；同一测试服务此前可供博客园使用。
- 只读核验确认当前求是网页面 origin 为已配置的 `https://www.qstheory.cn`；对 userscript prepare 端点发送相同 OPTIONS/PNA 请求返回 HTTP 200，但没有 `Access-Control-Allow-Origin`、methods、headers 或 private-network 响应头。
- 测试 `runserver` PID 1169 自 2026-08-20 23:56 启动，命令为 `python manage.py runserver 127.0.0.1:8080 --noreload`；它早于 2026-08-21 新增求是网 CORS 规则，且 `--noreload` 不会加载设置文件变化。进程工作目录、`wagtailblog-test` Conda 与 `WAGTAILBLOG_ENV=test` 均已核准。
- 目标：只重启这一条精确测试 runserver，使现有 CORS 配置生效；验证求是网 OPTIONS/PNA 响应头、测试服务监听与 Django 健康检查。

#### 非目标、影响、验收与回滚

- 非目标：不继续放宽来源、不修改 userscript/API/模型/迁移/Worker/systemd/生产服务，不读取 Token/Cookie，不调用预检 POST、session、上传、finalize 或创建草稿。
- 实际修改文件仅为本方案的实施记录；运行操作会停止 PID 1169，并用相同工作目录、测试 Conda、环境、地址、端口和 `--noreload` 参数启动替代进程。`systemctl.md` 不更新，因为测试 runserver 不是 systemd unit，服务定义没有变化。
- 数据与服务影响：测试 HTTP 服务会有一次短暂停顿；无数据库、MongoDB、媒体或生产数据写入。新进程日志写到 WSL2 `/tmp`，不进入 Git。
- 验收：新 PID 正常监听 `127.0.0.1:8080`；求是网 origin 的 OPTIONS 返回精确 allow-origin、POST/authorization/content-type 与 private-network 头；恶意来源仍无 allow-origin；`manage.py check` 通过。
- 回滚：停止替代进程并按相同已核准命令重新启动；若新进程无法健康启动，立即报告并保留 `/tmp` 启动日志，不修改数据或生产服务。

#### 模型/推理强度建议

- 建议：只读进程/CORS 诊断使用 `gpt-5.6-luna + 中推理`，精确测试进程替换与验证使用 `gpt-5.6-terra + 中推理`。
- 升级条件：若出现端口冲突、测试数据库写入、Worker/Redis 异常、生产路径或 systemd 影响，停止操作并升级 `gpt-5.6-sol + 高推理` 复核。
- 验证门禁：停止前核准精确 PID、命令、cwd、Conda 和环境；启动后必须同时验证监听、CORS 正向/反向案例和 Django check。

#### 实施记录

- 2026-08-21（待执行）：已完成根因确认与运行边界核准；Browser Skill 只读会话已停止。尚未停止或启动进程，未修改数据、Worker、生产服务或 `systemctl.md`。
- 2026-08-21（修复与验收通过）：精确停止旧测试 runserver PID 1169，并以相同共享工作目录、`wagtailblog-test` Python、`WAGTAILBLOG_ENV=test`、`127.0.0.1:8080 --noreload` 启动替代 PID 4767；新进程正常监听，后台入口返回 302。求是网 OPTIONS/PNA 返回 allow-origin `https://www.qstheory.cn`、允许 POST、Authorization/Content-Type 和 private-network，且不允许 credentials；恶意 origin 的相同请求仍无 allow-origin。`manage.py check` 无问题。未调用预检 POST、读取 Token/Cookie、创建 session/媒体/页面/revision，未改动 Worker、生产服务或 `systemctl.md`。回滚点为替代进程 PID 4767 和 `/tmp/wagtailblog-test-runserver-8080.log`；本次仅更新本方案，实际使用当前可用模型完成诊断和测试进程替换，未调用外部模型。

### 23.24 人民网频道及政务媒体站点扩展（2026-08-21）

#### 背景与现状证据

- 用户要求扩展 5 个人民网频道、旗帜网、先锋文汇、学习强国、人民论坛网、半月谈和党建网，并要求摸清站点规律后再实施。
- Browser Skill 只读核验结果：`opinion.people.com.cn`、`finance.people.com.cn`、`society.people.com.cn`、`cpc.people.com.cn`、`politics.people.com.cn` 的样例页均存在唯一 `#rm_txt_zw` 正文容器，正文分别排除标题、责编、客户端下载和页脚；这些页面标题均以 ` --频道--人民网` 结尾。
- 旗帜网页面唯一正文容器为 `.w1200.flag-text-con.clearfix`，包含 14 个正文段落和编辑信息；标题页后缀为 `--旗帜网`。
- 先锋文汇页面唯一正文容器为 `#font_area`，包含 8 个正文段落和 8 张文章图片；页面标题以 `_先锋文汇_共产党员网` 结尾。
- 学习强国为动态页面，`document.title` 只有“学习强国”；真实标题位于唯一 `.render-detail-title`，正文位于唯一 `.render-detail-article-content`，包含 14 个正文段落和 2 张图片。
- 人民论坛网正文使用唯一 `.article-content`，包含 20 个正文段落和 1 张图片；标题以 `_理论_人民论坛网` 结尾。
- 半月谈正文使用唯一 `#detail_content`，包含 4 个正文段落；标题以 `-半月谈` 结尾。
- 党建网指定 URL 当前实际返回标题“外交部：中国发展带给世界的不是“冲击”而是机遇”，正文唯一容器为 `#tex.article`，包含 4 个段落；页面内容与 URL 路径语义不一致，按当前真实 DOM 接入并记录风险。

#### 目标、非目标与实施范围

- 目标：在现有 `InterfaceList` 中加入上述站点映射；为学习强国增加可选标题选择器能力；为各精确来源增加 userscript `@match` 与 Markdown 导入 API CORS 规则；保持下载 Markdown 与预检/草稿创建共用同一正文选择器。
- 非目标：不创建通用 `body` 回退、不抓取站点导航/推荐/页脚、不改 Markdown block key、图片处理、session/finalize、页面模型、Worker、迁移、生产发布或数据清理；浏览器验收不点击预检或创建草稿。
- 实际修改文件：`wagtailblog3/static/vendor/Script/downlaod_markdown.js`、`wagtailblog3/settings/base.py`、`wagtailblog3/apps/blog/test_markdown_import_cors.py`、本方案；重新生成 Git ignored TEST userscript。`systemctl.md` 不修改。

#### 数据、服务、安全与异常路径

- 数据与服务影响：仅修改浏览器脚本映射、标题读取逻辑和 CORS 来源清单；不写数据库、不上传媒体、不启动或重启服务。现有测试 runserver 若早于配置变更启动，需按 23.23 的精确进程刷新流程重新加载设置。
- 安全边界：userscript 可用 `*://*.people.com.cn/*` 覆盖匹配，但后端 CORS 只精确允许本批次实际子域，不放开整个 `people.com.cn` 通配 origin；HTTP 来源仅按实际 URL 精确允许。
- 异常与残余风险：人民网频道模板目前共用 `#rm_txt_zw`，未来改版仍可能导致容器缺失；学习强国类名可能因前端构建变化而变化；党建网当前 URL 与内容不一致，后续应以页面真实标题和正文为准。

#### 测试、验收与回滚

- 自动测试：静态断言锁定所有新增 `@match`、host/selector/title 规则、学习强国标题选择器、精确 CORS origins 和禁止 body 回退；正式与 TEST userscript 运行 `node --check`。
- WSL2 验证：运行 `blog.test_markdown_import_cors`、`manage.py check` 和 `makemigrations --check --dry-run`；必要时核对测试 runserver 重新加载后的 OPTIONS/PNA 响应头。
- 浏览器验收：在真实页面逐站确认入口、标题和正文容器；桌面/移动视口检查弹窗无溢出、控制台无本脚本错误；不调用博客 API 写入链路。
- 回滚：恢复本批次 userscript、CORS、测试和方案文档最小 diff，重新生成旧 TEST 副本；不涉及数据库或生产服务回滚。

#### 模型/推理强度建议

- 建议：多站点 DOM 证据收集使用 `gpt-5.6-luna + 中推理`；脚本映射、标题字段扩展、CORS 与回归测试使用 `gpt-5.6-terra + 中推理`。
- 升级条件：若人民网出现多个不兼容模板、学习强国需要接口级解析、CORS 需要通配放行、或涉及生产/数据写入，升级 `gpt-5.6-sol + 高推理`。
- 验证门禁：每个站点必须有唯一非 body 选择器；标题字段必须有真实 DOM 证据；CORS 必须精确到实际 origin；自动测试和浏览器只读验收全部通过后交付。

#### 实施记录

- 2026-08-21（调研完成，待代码验证）：已完成 11 个站点样例页的 Browser Skill 只读核验并停止会话；确认人民网频道统一 `#rm_txt_zw`，学习强国需要 `.render-detail-title` 作为标题源，党建网 URL 与实际文章内容不一致。尚未修改代码、调用博客 API、创建 session/媒体/页面/revision 或变更服务。
- 2026-08-21（代码实施与自动验证完成）：已将 11 个站点加入 `InterfaceList` 和 userscript `@match`，学习强国使用 `.render-detail-title` 读取标题；CORS 仅增加本批次精确 host 的 HTTP/HTTPS 规则；正式脚本版本为 `0.3.15`，TEST 副本 SHA-256 为 `b7d386cfdacdfddb8a9c090b849ca09f40359dc18e58f3adda81e3105202ad30`。实际修改文件为 `wagtailblog3/static/vendor/Script/downlaod_markdown.js`、`wagtailblog3/settings/base.py`、`wagtailblog3/apps/blog/test_markdown_import_cors.py` 和本方案；未修改 `systemctl.md`，未涉及迁移、数据库、Worker、生产服务或真实导入。
- 2026-08-21（WSL2 验证通过）：在 `wagtailblog-test`、`WAGTAILBLOG_ENV=test` 下运行 `python manage.py test blog.test_markdown_import_cors --verbosity 2`（15/15 通过）、`python manage.py test blog.test_markdown_import_api blog.test_markdown_import_prepare --verbosity 1`（20/20 通过）、`python manage.py check`（无问题）及 `python manage.py makemigrations --check --dry-run`（No changes detected）；正式与 TEST userscript 均通过 `node --check`。
- 2026-08-21（浏览器只读验收）：Browser Skill 逐站打开人民网 5 个频道、旗帜网、先锋文汇、学习强国、人民论坛网、半月谈和党建网，确认正文选择器均可在真实 DOM 找到；学习强国等待动态内容加载后 `.render-detail-title` 与 `.render-detail-article-content` 均存在。当前浏览器扩展仍加载旧 TEST 脚本 `0.3.14`，因此新增站点未显示新入口；未将旧脚本表现计作新版本验收，也未点击预检、创建草稿或调用博客写入 API。交付前需在用户的 AdGuard 中安装/刷新生成的 `output/userscript-blog-import/downlaod_markdown.blog-import-test.user.js`（或正式脚本）后，再进行入口和标题字段的人工复核。
- 2026-08-21（回滚点与残余风险）：回滚点为恢复上述 4 个代码/文档文件及旧 TEST 副本；无数据回滚。残余风险为站点模板变化、学习强国动态类名变化，以及党建网指定 URL 与实际文章内容不一致；浏览器扩展尚未切换到 0.3.15，属于验收前置操作而非代码失败。

### 23.28 TEST userscript 版本与旧模板/CORS 复核（2026-08-21）

#### 根因与实施

- 检查发现 `tools/build_userscript_blog_import_test.ps1` 的参数默认值仍为 `0.3.0-test.1`，导致 TEST 文件头版本与正文 `blogImportVersion=0.3.15` 不一致；已将默认值改为 `0.3.15-test.1` 并重新生成 TEST 副本。
- 当前 TEST 文件元数据版本为 `0.3.15-test.1`，包含 `fallback_els: [".rm_txt_con.cf"]` 和 `credentials: 'omit'`；SHA-256 为 `37744285a7b811a2e780bd5ecf726e17dfbe38e63cd5e9af10a2e668e2e19b3c`。

#### CORS 双向验证

- 隔离测试服务 `0.0.0.0:8080` 对 `http://opinion.people.com.cn` 的 OPTIONS 返回 200、精确 `Access-Control-Allow-Origin`、POST、Authorization/Content-Type 和 `Access-Control-Allow-Private-Network: true`。
- Browser Skill 从旧人民网页面实际发起无效 Bearer POST，浏览器成功读取 401 JSON，而不是 CORS `TypeError`；未创建 session、媒体或草稿。
- 自动测试 `blog.test_markdown_import_cors` 15/15 通过，TEST/正式 userscript `node --check` 通过。

#### 用户侧切换与回滚

- 用户需在 AdGuard 中删除/停用旧的 `0.3.0-test.1` 副本，安装新的 `output/userscript-blog-import/downlaod_markdown.blog-import-test.user.js`，再刷新文章页；旧脚本继续运行时，代码修复不会生效。
- 回滚为恢复构建脚本默认版本并重新生成旧 TEST 文件；不涉及生产服务或数据回滚。

### 23.27 人民网旧模板正文容器兼容（2026-08-21）

#### 背景与现状证据

- URL `http://opinion.people.com.cn/n1/2025/1020/c1003-40585017.html` 的页面标题为“引领未来，中国做对了什么 --观点--人民网”，但页面没有 `#rm_txt_zw`；真实正文唯一位于 `.rm_txt_con.cf`，包含正文段落及文章内容。
- 该页面仍来自 `opinion.people.com.cn`，浏览器 Origin 和 CORS 规则与现有人民网频道相同，不能通过新增任意域名解决容器问题。

#### 实施

- 在同一 `opinion.people.com.cn` 映射中保留新模板首选 `#rm_txt_zw`，增加旧模板回退选择器 `.rm_txt_con.cf`；正文提取仍只允许这些精确容器，不回退到 `body`。
- `articleData()` 现在按 `el` 后 `fallback_els` 顺序选择第一个存在的容器；标题仍按 ` --` 截断。
- 正式脚本仍为 `0.3.15`；TEST 副本已重新生成，SHA-256 为 `bcd8697969c29e1d5ba2037ea16e0035e1c5a43b3c6a07c0c25043a45a91123a`。

#### 验证、CORS 与回滚

- Browser Skill 只读核验：旧页面 `.rm_txt_con.cf` 匹配 1 个，标题截断结果为“引领未来，中国做对了什么”；未点击预检或创建草稿。
- `blog.test_markdown_import_cors` 15/15 通过；正式 userscript `node --check` 通过；`git diff --check` 通过。`opinion.people.com.cn` 的 HTTP CORS/PNA 精确规则未改变，继续由现有后端配置提供。
- 回滚仅需移除 `fallback_els` 及选择器回退逻辑并重新生成 TEST 副本；不涉及数据库、媒体、Worker 或生产服务。

#### 模型/推理强度建议

- 建议：单站点 DOM 兼容使用 `gpt-5.6-luna + 中推理`；脚本与 CORS 回归使用 `gpt-5.6-terra + 中推理`。
- 升级条件：若人民网出现需要按 URL 分流、正文位于跨域 iframe 或需改生产代理配置，再升级到 `gpt-5.6-sol + 高推理`。

### 23.26 测试组装任务队列隔离（2026-08-21）

#### 根因与范围

- 两个指定人民网页面点击创建后长期显示“正在组装未发布草稿”。测试库中的最新会话状态为 `ready`，`assembly_requested_at` 已写入但未转为终态；测试 Worker 日志没有组装任务。
- Celery inspect 显示测试 Worker `markdown-import-test@ming` 与生产 Worker `maintenance@ziliao` 同时监听 Redis DB 2 的同名 `maintenance` 队列。测试任务可能被生产 Worker 领取，并在生产库找不到测试 session，导致测试库会话永远停在 `ready`。

#### 实施与验证

- 未修改应用代码或生产配置；停止精确的旧测试 runserver PID 5438 和测试 Worker PID 2181/2183，按仓库既有 `tools/start_test_stack.sh` 重新启动测试栈。
- 当前测试网站 PID 6088 监听 `0.0.0.0:8080`；测试 Worker PID 6089（子进程由 Celery 管理）仅监听 `markdown-test-maintenance`，使用 Redis broker DB 12、结果 DB 13。Celery inspect 仅发现该测试节点，生产 `maintenance@ziliao` 不再出现在测试 broker 的节点列表。
- 该修复只影响后续新建的导入会话；此前已停在旧 DB 2/`maintenance` 队列的 `ready` 会话不自动重投，避免未经确认重复创建草稿。用户需在隔离测试栈下重新预检并创建。

#### 模型/推理强度建议

- 建议：队列/进程只读诊断使用 `gpt-5.6-luna + 中推理`；跨环境队列隔离和服务切换复核使用 `gpt-5.6-terra + 中推理`。
- 升级条件：若生产也存在跨环境 broker、需要重投已有生产会话或涉及数据补偿，升级到 `gpt-5.6-sol + 高推理`，先备份、确认影响和回滚顺序。

### 23.25 CORS 配置刷新与双向响应验证（2026-08-21）

#### 背景与现状证据

- 用户再次报告“博客接口跨域请求失败”。核查发现代码中的人民网 HTTP/HTTPS 来源规则已经存在，但测试 runserver PID 4767 是在本批次 CORS 配置加入前启动，且使用 `--noreload`，因此实际进程未加载新来源。
- 旧进程对 `http://opinion.people.com.cn` 的 PNA OPTIONS 响应只有 `Vary: origin`，没有 `Access-Control-Allow-Origin`；这解释了浏览器端报错，而不是脚本正文解析失败。

#### 目标、非目标与实施

- 目标：让测试服务实际加载当前 CORS 配置，并同时验证浏览器预检响应与实际 Bearer POST 响应。
- 非目标：不放开任意来源，不修改 Token、API 协议、数据、Worker、生产服务或 `systemctl.md`。
- 实施：停止已核准的测试 runserver PID 4767，以相同工作目录、`wagtailblog-test` 环境、`WAGTAILBLOG_ENV=test`、`127.0.0.1:8080 --noreload` 启动替代 PID 5438；未执行真实导入。

#### 验证与回滚

- `http://opinion.people.com.cn` 和 `https://www.qstheory.cn` 的 OPTIONS 均返回精确 `Access-Control-Allow-Origin`、POST、Authorization/Content-Type 及 `Access-Control-Allow-Private-Network: true`。
- 使用无效 Bearer 的实际 POST 返回 401 JSON，同时返回 `Access-Control-Allow-Origin: http://opinion.people.com.cn`；恶意来源仍不返回 allow-origin。
- Browser Skill 从真实 `http://opinion.people.com.cn` 页面向 `http://127.0.0.1:8080` 发起同源策略下的无效 Token POST，浏览器成功读取 HTTP 401 和 JSON body（未出现 `TypeError: Failed to fetch`），证明前端 fetch 与后端 CORS 已连通；请求未创建任何导入数据。
- 回滚点为测试进程 PID 5438 和 `/tmp/wagtailblog-test-runserver-8080.log`；停止该进程即可回到未运行状态，不涉及数据库回滚。

#### 模型/推理强度建议

- 建议：使用 `gpt-5.6-luna + 中推理` 做响应头核查，`gpt-5.6-terra + 中推理` 做进程刷新和双向 CORS 验证。
- 升级条件：若生产 Nginx/uWSGI 也出现同样问题，或需要修改生产配置、HTTPS、代理头和服务单元，再升级到 `gpt-5.6-sol + 高推理` 并执行生产发布门禁。

### 23.29 旧人民网页面 CORS 回归测试收尾（2026-08-21）

#### 实施记录

- 为实际 Bearer 响应回归测试开放 `SimpleTestCase` 的 `default` 测试数据库权限；OPTIONS 测试仍保持原有无数据库行为，未改变生产认证或接口逻辑。
- 旧页面 `http://opinion.people.com.cn/n1/2025/1020/c1003-40585017.html` 继续使用 `.rm_txt_con.cf` 回退正文容器，CORS 仍只精确允许 `http://opinion.people.com.cn`。

#### 验证结果

- WSL2 `wagtailblog-test`：`python manage.py test blog.test_markdown_import_cors --verbosity 1`，16/16 通过；实际无效 Bearer POST 返回 401 且带精确 `Access-Control-Allow-Origin`。
- 正式与 TEST userscript 均通过 `node --check`；`git diff --check` 无本次新增格式错误。
- TEST 副本版本为 `0.3.15-test.1`，SHA-256 为 `37744285a7b811a2e780bd5ecf726e17dfbe38e63cd5e9af10a2e668e2e19b3c`。未创建 session、媒体、BlogPage 草稿或 revision，未发布生产。

#### 模型/推理强度实际使用与残余风险

- 实际使用：脚本兼容与测试修复按 `terra + 中推理` 完成，浏览器只读复核按 `luna + 中推理` 完成；未触发生产发布或高风险升级条件。
- 用户侧仍需停用旧的 `0.3.0-test.1` 副本并加载新的 TEST 文件；若仍显示跨域错误，应先确认请求实际命中 `http://127.0.0.1:8080` 和新脚本版本。站点模板变化仍是后续残余风险。

### 23.30 跨域错误再次复现与边界确认（2026-08-21）

#### 浏览器与服务端证据

- Browser Skill 在真实 `opinion.people.com.cn` 页面打开当前导入面板，填写 `http://127.0.0.1:8080` 后发起同样的 Bearer POST；使用无效测试 Token 时，页面显示 `token_not_valid`，证明 userscript 的原生 `fetch` 已跨域到达 Django 并能读取 JSON 响应。
- 当前测试服务的 OPTIONS 返回 200 和精确 `Access-Control-Allow-Origin: http://opinion.people.com.cn`；实际 POST 返回 401，并带同一 allow-origin。Django 日志记录了 `/zh-hans/blog/api/markdown-import/userscript/prepare/` 的 POST 401，没有网络层失败。
- 因此本次复核没有发现后端 CORS 缺口；“博客接口跨域请求失败”只会在浏览器未加载当前脚本、请求指向另一地址/端口、服务进程未重载配置，或生产代理未同步 CORS 配置时出现。

#### 用户侧定位顺序

- AdGuard 只保留 `0.3.15-test.1`，停用旧的 `0.3.0-test.1`/`0.3.14` 副本；刷新页面后必须看到“打开博客 Markdown 导入预检”。
- 博客地址严格填写 `http://127.0.0.1:8080`；不要混用 `localhost`、旧端口、生产域名或带错误路径的地址。
- 若仍报错，浏览器网络面板应检查请求 URL 是否为 `/zh-hans/blog/api/markdown-import/...`，以及 OPTIONS 是否返回 allow-origin；若没有请求到达 8080，则问题在脚本副本或地址，若到达但无响应头，则是实际运行的服务/代理未加载当前 CORS 配置。

#### 实施与影响

- 本次仅完成浏览器、HTTP 和日志核验并更新方案文档，未修改 API、Token、数据库、Worker、生产服务或 `systemctl.md`，未创建 session、媒体或草稿。
- 回滚点为恢复本方案文档记录；无数据回滚。生产环境仍需单独按发布门禁同步当前 CORS 配置，不能用本地 8080 的验证替代生产验收。

### 23.31 正式提交与生产发布（2026-08-21）

#### 发布范围与提交

- 用户已明确授权提交并部署最新 Markdown 导入 userscript、只读 prepare API、精确 CORS 配置、测试和维护文档。
- 代码提交 `b3f2ecf0c328e64d7f33b2edfa09eccbc7e81666` 已推送 `origin/main`；生产仓库从 `ab88116b3cc4e68cf125e28e5552ba566ff35eeb` 安全 fast-forward 到同一 SHA，生产工作树干净。
- `systemctl.md` 已增加“Markdown 导入 userscript 与 CORS 重要维护规则”，明确以后新增网站必须同步更新 `@match`、真实正文选择器、精确 CORS、回归测试、版本和浏览器验收。

#### 测试、发布与服务影响

- WSL2 `wagtailblog-test` 定向测试 41/41 通过；`manage.py check` 通过；`makemigrations --check --dry-run` 为 `No changes detected`；正式与 TEST userscript 均通过 `node --check`；TEST 副本 `0.3.15-test.1` SHA-256 为 `05f404d5cc1fd182e048d5ba939c65350a69a576f9e957fead459ebf1308dff2`。
- 生产 `WAGTAILBLOG_ENV=production` 下 `manage.py check` 通过，迁移 dry-run 无变更；`collectstatic --noinput` 完成 1 个新增静态文件、1492 个未修改文件和 842 个 post-process。
- 仅重启 `wagtailblog3.service`（uWSGI/Django）；`wagtailblog3-celery-maintenance.service`、Beat、Filebeat 未重启且四个服务最终均为 `active/enabled`。生产后台 HTTP 200；API OPTIONS 为 200，实际无效 Bearer POST 为 401 JSON，并返回 `Access-Control-Allow-Origin: http://opinion.people.com.cn`。
- 未执行迁移、索引、Token 创建、session、媒体上传、BlogPage 草稿、revision、MongoDB 正文或页面发布；`systemctl.md` 已更新，未修改 unit、端口、Nginx 或基础设施。

#### 回滚与残余风险

- 代码回滚点为生产上一已验证 SHA `ab88116b3cc4e68cf125e28e5552ba566ff35eeb`；回滚时将生产仓库 fast-forward/恢复到该 SHA，重新 `collectstatic` 并重启 `wagtailblog3.service`，不回滚数据库内容。
- 残余风险为外部网站模板变化、用户 AdGuard 中残留旧 userscript，以及未来新增网站忘记同步 CORS。生产数据写入和草稿创建仍需用户在脚本界面显式操作。

#### 模型/推理强度实际使用

- 只读调研和浏览器证据使用 `luna + 中推理`；Django/userscript 实现、测试和发布复核使用 `terra + 中推理`。本批未触发不可逆迁移、生产数据修复或跨系统安全升级条件，未使用 `sol`。

### 23.32 userscript 正式版本递增（2026-08-21）

#### 背景与实施

- 复核发现上一生产提交中的正式脚本仍标记为 `0.3.15`，TEST 副本为 `0.3.15-test.1`；虽然代码功能已更新，但 AdGuard 可能因版本号相同继续运行旧副本。
- 已将正式脚本 `@version` 与运行时 `blogImportVersion` 升至 `0.3.16`，TEST 构建默认值升至 `0.3.16-test.1`，并同步更新版本静态回归断言。

#### 验证与发布门禁

- TEST 副本已重新生成，SHA-256 为 `417869fb558a140e20ff9cfb14e4440a1caaaf200bb292c701b5f966c21fd819`；正式与 TEST userscript 均通过 `node --check`。
- WSL2 定向测试 41/41 通过，`manage.py check` 通过，`makemigrations --check --dry-run` 为 `No changes detected`。本批仅修改版本标识和测试断言，不创建数据、不改变 API/CORS 规则。
- 已提交并推送 `4308d3773e963a3a281bdd2c8257e40614da31f8`，本地 `HEAD`、`origin/main` 与生产仓库均已同步到该 SHA。生产已完成 `collectstatic --noinput`，并重启 `wagtailblog3.service`；服务为 `active/enabled`，失败 unit 为 0，后台入口返回 HTTP 200。生产 prepare API 的 CORS OPTIONS 返回 HTTP 200，并正确返回 `http://opinion.people.com.cn` 的精确允许来源及私有网络响应头。
- 正式脚本版本为 `0.3.16`；用户侧应停用旧的 `0.3.15-test.1`，安装新的 `0.3.16-test.1` 并刷新文章页面。本批未创建草稿、session、媒体或 revision，也未修改数据库内容。

#### 回滚与模型实际使用

- 回滚仅需恢复上一提交中的两个版本标识、构建默认值和断言，再重新生成 TEST 副本；不涉及生产数据库或服务数据回滚。
- 实际使用 `terra + 中推理` 完成版本递增与测试，未触发高风险发布升级条件。
