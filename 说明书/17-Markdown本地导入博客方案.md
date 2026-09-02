# Markdown 本地导入博客方案（维护摘要）

## 目标与范围

从本地 UTF-8 Markdown 创建 Wagtail `BlogPage` 未发布草稿，保留原文 Markdown，并按原顺序组装现有 StreamField 块：`markdown_block`、`image_block`、`video_block`、`audio_block`、`embed_block`、`mermaid_chart`。只创建草稿，不自动发布、不覆盖既有页面。

客户端负责文件读取、路径解析、预检及用户确认；服务端负责认证、权限、内容/媒体校验、幂等、页面组装和失败补偿。服务端不得主动抓取用户提交的 URL。

## 已实现功能

- Markdown 解析与块顺序保持；Mermaid fenced code 仅写入 `mermaid_chart.code`，普通代码和表格保留在 `markdown_block`。
- 本地图片、用户确认后的 HTTPS 远程图片、音视频和受支持平台 embed 的拆分、校验、上传及 Markdown 引用重写。
- 导入 session/batch/artifact、幂等键、分片上传、失败 artifact 重试、Celery maintenance 组装与 Beat 清理补偿。
- `MarkdownImportToken` 专用 Bearer 认证：Token 仅创建时明文显示，数据库只保存哈希和前缀；支持 scope、过期、撤销、最近使用审计。
- API 提供 limits、destinations、prepare、session、artifact、finalize 等受认证入口；返回页面/修订编辑地址供人工复核。
- Windows CLI/EXE 与浏览器 userscript 复用同一 API，不复制解析器或存储协议。

## 关键入口

- 解析/组装：`wagtailblog3/apps/blog/services/markdown_import_parser.py`、`markdown_import_service.py`
- 认证/会话/媒体：`markdown_import_auth.py`、`markdown_import_sessions.py`、`markdown_import_media.py`
- API：`wagtailblog3/apps/blog/markdown_import_api.py`
- 模型：`wagtailblog3/apps/blog/models.py`（Token、session/batch/artifact）
- 后台：`wagtailblog3/apps/blog/admin.py`、`wagtailblog3/apps/blog/wagtail_hooks.py`
- 客户端：`tools/markdown_import/`、`wagtailblog3/static/vendor/Script/downlaod_markdown.js`

## 数据安全与不可变约束

- 不把 Token、正文、MongoDB 草稿、revision pointer、完整本地路径或图片二进制写入日志、文档或外部模型。
- 不改变既有 StreamField block key、Markdown 原文、MongoDB 正文和 revision 保存顺序。
- 所有请求重新校验用户、scope、目标 `BlogIndexPage` 权限及过期/撤销状态。
- 远程 URL 仅允许显式 HTTPS 并执行 SSRF、重定向、DNS、大小及 MIME 校验；图片请求不得携带博客 Bearer Token。
- AI 元数据仅接收受限文本上下文；失败回退人工填写，不阻断草稿创建。

## 测试、发布与回滚门禁

WSL2 `wagtailblog-test`：`python manage.py check`、`makemigrations --check --dry-run`、相关 `blog.tests.test_markdown_import_*`、`compileall`、`git diff --check`；userscript 另运行 `node --check`。涉及浏览器时检查桌面/移动视口、键盘、错误状态和 Token 不泄露。

发布前提交并推送已验证 commit，生产工作树干净后 `fetch` + `merge --ff-only`；迁移、队列、服务重启和真实导入另行确认。回滚恢复上一个已验证 commit，未经授权不得删除 BlogPage、MongoDB、媒体或审计数据。

## 未完成/后续

- 大规模多文件导入、断点续传压力测试和真实浏览器 userscript 全流程仍需单独验收。
- 生产 AI 配置、Token 轮换和外部站点兼容性需按环境分别验证。

## 实施记录摘要

- 2026-08-19：完成 session/batch/artifact、专用 Token、Wagtail Snippet 后台和客户端多文件基础能力；定向回归通过，未触碰生产数据。
- 2026-08-20：生产应用迁移至 0028 并完成备份、服务验收；仅新增导入相关表，不改正文/媒体。
- 2026-08-21～24：完成 userscript CORS、Tampermonkey 迁移、编辑链接和兼容性修复；生产按已验证 commit 发布。
