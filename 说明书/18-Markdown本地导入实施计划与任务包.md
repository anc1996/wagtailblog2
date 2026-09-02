# Markdown 本地导入实施计划与任务包（维护摘要）

## 当前基线

项目运行于 Python 3.13、Django 5.2、Wagtail 7.4；依赖 MySQL、MongoDB、MinIO、Redis 和 Celery maintenance 队列。目标是把本地 Markdown 安全导入为未发布 `BlogPage`，并可追踪、可重试、可回滚。

## 已完成任务

- T1–T4：解析器、路径/URL 安全、媒体 artifact、幂等和批次状态。
- T5–T8：BlogPage/StreamField 组装、Mongo/revision 一致性、失败补偿、maintenance Worker 与 Beat 清理任务。
- T20：专用 `MarkdownImportToken`（scope、过期、撤销、审计）、Wagtail Snippet 创建入口、Windows 多文件客户端。
- T23：浏览器 userscript 直连 API、CORS/PNA 校验、Tampermonkey 兼容、成功后生成 Wagtail 编辑链接。

## 关键约束

- Token 明文只在创建成功时显示一次；数据库只保存哈希/前缀，日志和客户端 checkpoint 不保存 Token。
- 仅创建草稿，不自动发布；任何页面、revision、MongoDB 正文、媒体和 session 写入都必须经用户确认。
- 客户端不上传本地路径，不请求数据库，不让服务端盲抓远程 URL；服务端重新执行权限、SSRF、MIME、大小和幂等校验。
- 单媒体失败隔离为 Markdown 缺失提示；页面级组装失败才补偿本批次未引用媒体。不得删除受保护正文或历史 revision。

## 任务分配与门禁

只读检索/格式检查可用 Luna；常规 Django/客户端实现用 Terra；涉及认证、并发、媒体补偿、迁移、生产发布用 Sol 高推理复核。每个任务包需有目标文件、预期输出、定向测试和回滚点。

## 验证命令

```text
python manage.py check
python manage.py makemigrations --check --dry-run
python -m compileall wagtailblog3/apps/blog/services wagtailblog3/apps/blog/markdown_import_api.py
python manage.py test blog.tests.test_markdown_import_* --keepdb --noinput
node --check wagtailblog3/static/vendor/Script/downlaod_markdown.js
git diff --check
```

生产发布前须完成备份、迁移计划核对、静态文件收集、服务顺序重启和首页/后台/Worker/Beat 健康检查；未授权不得执行真实导入或数据修复。

## 实施记录摘要

- 2026-08-19：T20 Token、Snippet、客户端多文件能力完成，相关回归通过。
- 2026-08-20：生产应用迁移至 0028，完成备份和服务验收；未修改正文、媒体或索引。
- 2026-08-21～24：完成 API/CORS、userscript、编辑链接和 AI 元数据受限接口；已按已验证 commit 发布。

## 未完成项

真实多文件大规模压力、断点恢复、第三方站点兼容和生产 AI 请求仍需独立验收；任何新队列、迁移或生产数据操作必须新增方案并取得确认。
