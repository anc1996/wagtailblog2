# 浏览器脚本直连 Markdown 导入博客方案（维护摘要）

## 目标与现状

userscript 从博客园、微信公众号、知乎、CSDN、人民网上下文提取标题/正文/图片，调用本项目 Markdown 导入 API，避免复制到 Windows EXE。当前正式脚本位于 `wagtailblog3/static/vendor/Script/downlaod_markdown.js`，已迁移到 Tampermonkey，版本线为 0.3.19；AdGuard 专用探针已移除。

## 已实现流程

1. 页面脚本提取受限文本和图片引用，跳过脚本、样式、代码围栏及非正文节点。
2. 用户在面板确认博客地址、Token、目标 BlogIndexPage 和是否下载远程图片。
3. 通过 `GET limits/`、`GET destinations/`、`POST prepare/` 完成认证、权限和预检；随后按现有 session/artifact/finalize 协议上传并组装草稿。
4. 成功或部分成功时按本次 `page_id` 生成 `/admin/pages/<page_id>/edit/` 链接；失败或重新预检会清除旧链接。

## 权限与 Token 边界

- Token 只保存在 userscript 隔离存储（用户勾选“记住”后），不写 `localStorage`、`sessionStorage`、URL、Cookie、日志或页面 DOM；界面默认掩码并提供清除入口。
- Bearer 仅发送到用户锁定且验证过的博客 origin；图片下载请求绝不携带 Bearer。`@connect *` 仅用于多来源图片兼容，不等于允许向任意主机发送 Token。
- 服务端每次校验 Token scope/过期/撤销、用户状态、collection 和目标页权限；客户端输入、第三方页面和图片 URL 均视为不可信。
- 不把正文、Token、Cookie、完整 URL 查询参数或图片二进制发送外部模型；不自动发布、不覆盖页面、不绕过 CORS/权限。

## 关键入口

- 脚本：`wagtailblog3/static/vendor/Script/downlaod_markdown.js`
- 构建/测试副本：`tools/build_userscript_blog_import_test.ps1`
- 服务端 API：`wagtailblog3/apps/blog/markdown_import_api.py`
- CORS 回归：`wagtailblog3/apps/blog/test_markdown_import_cors.py`
- 协议与数据模型详见 `17-Markdown本地导入博客方案.md`、`18-Markdown本地导入实施计划与任务包.md`。

## 版本与验证记录

- 0.3.3：修复 AdGuard 响应字段兼容；0.3.11：完善表单/诊断；0.3.18：编辑链接；0.3.19：Tampermonkey 迁移并移除 AdGuard UI。
- 2026-08-24：正式/TEST 脚本 `node --check`、CORS 定向测试 19/19、`manage.py check`、迁移检查、compileall、`git diff --check` 通过；生产按已验证 commit 发布，仅重启 Django 服务。
- 未执行真实 Token、第三方站点写入、session/artifact、草稿、revision、MongoDB 正文或媒体创建；上述操作必须单独授权。

## 浏览器验收门禁

在 Tampermonkey 中停用旧脚本并安装同一版本 TEST 副本后，检查桌面/移动视口、表单键盘路径、Token 掩码、错误恢复、跨域请求头和链接不含 Token。Playwright 产物统一写入 `output/playwright/`。

## 未完成与回滚

第三方站点 DOM 变化、远程图片兼容、真实多文件导入和 AdGuard/旧浏览器差异仍需人工复测。回滚只恢复上一个脚本/TEST 构建及服务端 commit，不删除数据库正文、revision、媒体或审计记录。
