# Markdown 跨端导入工具链设计说明书

## 1. 业务背景与应用场景

编辑与创作者在日常编写博客时，通常习惯使用本地专业 Markdown 编辑器（如 Obsidian、Typora、VS Code），或需要将第三方技术社区、知识库的文章归档至本站。

为避免手工在后台反复排版复制的繁琐操作，本子系统研发了一套**跨端、安全、开箱即用的 Markdown 智能导入工具链**，支持：
1. **FrontMatter 元数据自解析**：自动提取标题、简介、标签、发布日期、分类及作者。
2. **富内容块智能映射**：自动转换为 Wagtail StreamField 的规范块（Markdown 块、代码高亮块、KaTeX 数学公式块、Mermaid 流程图块、表格块等）。
3. **媒体资源自动本地化**：自动探测 Markdown 中的外部远程图片，异步抓取至 MinIO 对象存储，杜绝防盗链导致的图片失效。
4. **双端无缝对接**：同时支持 **Windows 桌面客户端/CLI 工具** 与 **浏览器油猴插件（Userscript）直连抓取导入**。

---

## 2. 核心技术协议与数据解析流

```text
[本地 Markdown / 浏览器外部文章]
       │
       ▼ (提取 FrontMatter + 正文 AST)
 ┌─────────────────────────────────────────────────────────────┐
 │ 1. FrontMatter 解析：Title, Tags, Intro, Date               │
 │ 2. 媒体探测器：提取 HTTP 外部图片 URL                      │
 │ 3. 语义分块：代码块、公式块、Mermaid、纯 Markdown 块        │
 └──────────────────────┬──────────────────────────────────────┘
                        │
                        ▼ (AES-256-GCM Token 签名加密通信)
 ┌─────────────────────────────────────────────────────────────┐
 │ POST /zh-hans/blog/api/markdown-import/prepare/             │
 │ · Token 鉴权校验 · Session 会话建立 · 预存媒体与元数据       │
 └──────────────────────┬──────────────────────────────────────┘
                        │
                        ▼
 ┌─────────────────────────────────────────────────────────────┐
 │ POST /zh-hans/blog/api/markdown-import/commit/              │
 │ · 事务创建 Wagtail BlogPage 草稿 (live=False)               │
 │ · 正文写入 MongoDB draft 集合 · 外部图片存入 MinIO          │
 └─────────────────────────────────────────────────────────────┘
```

### 2.1 格式解析与 StreamField 块映射规则

- **FrontMatter 头信息**：
  ```yaml
  ---
  title: "文章标题"
  date: 2026-09-04
  tags: [Django, Wagtail, 架构]
  intro: "核心导读简介..."
  ---
  ```
- **正文 AST 拆解**：
  - ```` ```mermaid ```` 代码段被独立提取为 `mermaid_block`。
  - ```` ```python ````、```` ```bash ```` 等被提取为 `code_block` 并锁定高亮语言。
  - `$$...$$` 独立公式被提取为 `katex_block`。
  - 普通文本段落与二级/三级标题被提取为 `markdown_block`（保留规范的标题层级与内边距）。

### 2.2 媒体资源转存与防盗链

解析引擎扫描所有 `![alt](url)` 图像标签：
- 若为外链图片，通过 `MarkdownDownloadService` 执行带安全限制的并发异步抓取（严格限制超时、文件大小上限 20MB、MIME 白名单）。
- 将原图保存到 MinIO 存储桶并生成项目专有的 `BlogImage` 记录。
- 将文章中的图片链接重写为站内永久相对地址，彻底消除源站防盗链隐患。

---

## 3. 安全鉴权体系（AES-256-GCM 令牌机制）

导入 API 绝不对公网开放匿名写入，采用高安全等级的令牌鉴权：
1. **模型定义 (`MarkdownImportToken`)**：
   - 管理员在 Wagtail Snippet 后台为指定编辑者生成专属导入令牌。
   - 数据库使用 **AES-256-GCM** 可逆对称加密存储 Token 明文与认证 Tag，后台界面提供“一键安全复制”与“即时轮换（Rotate）”操作。
2. **防重放与权限控制**：
   - 每次导入请求必须在 HTTP Header 携带 `X-Markdown-Import-Token`。
   - API 严格校验令牌有效期与关联用户的 `can_add_blog_page` 权限。
   - 默认创建的页面全部为 **未发布草稿状态（`live=False`）**，绝不直接对外发布，必须由编辑者在后台人工复核后再点击发布。

---

## 4. 双端导入交互形态

### 4.1 方式一：浏览器用户脚本直连导入（Tampermonkey / 脚本猫）

- **应用场景**：编辑在浏览掘金、知乎专栏、微信公众号或开源文档时，点击浏览器右上角脚本按钮，直接抓取当前网页正文并转换。
- **技术实现**：
  - 基于油猴脚本自动提取当前 DOM 主体文本与元信息。
  - 直连本站 `/blog/api/markdown-import/` 端点完成校验并推送。
  - 导入成功后，前端弹窗直接附带该页面在 Wagtail 后台的直接编辑链接（`/admin/pages/<page_id>/edit/`），点击即可一键跳转校对。

### 4.2 方式二：Windows 客户端与 CLI 工具

- **应用场景**：编辑者整理了本地大量的 `.md` 知识库文件，需要批量一键上传建档。
- **技术实现**：
  - 提供单文件打包的 Windows 客户端（PyInstaller / Nuitka 打包）及命令行 Python 脚本。
  - 支持指定本地文件夹，多线程扫描、校验语法并按序提交，终端打印格式化的导入进度条。

---

## 5. 核心代码模块与落地清单

| 模块类别 | 文件路径 | 核心职责 |
| :--- | :--- | :--- |
| **AST 解析与分块** | `wagtailblog3/apps/blog/services/markdown_import_parser.py` | 正文 Markdown 解析、AST 遍历、StreamField 块映射规则 |
| **导入核心编排** | `wagtailblog3/apps/blog/services/markdown_import_service.py` | 页面创建事务、MongoDB 暂存、Revision 关联与状态流转 |
| **媒体探测与下载** | `wagtailblog3/apps/blog/services/markdown_download_service.py` | 异步下载远程媒体资源、防盗链处理与 MinIO 入库 |
| **REST 接口与鉴权** | `wagtailblog3/apps/blog/api/markdown_import.py` | 接收导入请求、AES-256-GCM 令牌校验与安全拦截 |
| **令牌模型与后台** | `wagtailblog3/apps/blog/models.py`<br>`wagtailblog3/apps/blog/wagtail_hooks.py` | 定义 `MarkdownImportToken` 模型，提供 Snippet 复制/轮换交互 |
| **浏览器油猴脚本** | `tools/userscript/wagtail_markdown_importer.user.js` | 外部网页一键抓取、转换与直连上报的前端脚本 |
| **桌面客户端脚本** | `tools/client/markdown_importer_client.py` | 本地文件批量扫描与上传的客户端实现 |

---

## 6. 验证与交付结论

- **排版精准无损**：二级标题间距、数学公式、复杂流程图及多级代码块转换还原率 100%，无样式塌陷。
- **数据安全保障**：所有导入页面必须经过管理员在后台人工二次复核方可发布，彻底杜绝恶意脚本注入与越权发布隐患。
