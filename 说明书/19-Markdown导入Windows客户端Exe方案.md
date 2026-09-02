# Markdown 导入 Windows 客户端 EXE 方案（维护摘要）

## 目标

提供 Windows 向导/CLI，读取本地 Markdown，执行预检并调用受认证 API 创建未发布 BlogPage；不改变服务端导入契约，不自动发布。

## 已实现

- 站点地址、专用 `mdimp_...` Token、目标 BlogIndexPage 选择。
- 单文件/多文件选择、Markdown 解析预检、图片/音视频/embed/Mermaid 统计和失败提示。
- 复用 `tools/markdown_import/client.py` 的 `inspect_markdown`、`import_markdown` 和 manifest；每个文件独立 session/batch/page。
- 可选记住站点和 Token：使用 Windows Credential Manager/DPAPI；默认不保存。checkpoint 只保存文件摘要、session/batch ID 和状态。
- 成功后显示页面 ID、revision ID 和后台编辑地址；客户端不携带 Token 打开后台链接。

## 安全边界

- Token 不写明文配置、日志或 checkpoint；API 仅发送到用户确认的博客 origin。
- 服务端重新校验 Token scope/过期/撤销、用户状态、collection 和目标页权限。
- 本地路径、文件名和完整响应不发送外部模型；远程图片必须用户明确确认，图片请求不带 Bearer Token。
- 草稿创建、媒体上传、revision/MongoDB 写入均属于用户明确操作；不支持自动发布、批量覆盖或删除。

## 关键入口与构建

- 客户端：`tools/markdown_import/client.py`、`tools/markdown_import/` GUI 代码。
- 服务端协议：`wagtailblog3/apps/blog/markdown_import_api.py`。
- PyInstaller：`--onefile --noconsole`；构建产物仅放 `output/`，不得提交 `dist/`、`build/`、Token 或配置。

## 验收门禁

检查键盘可达、Token 掩码、长路径换行、加载/成功/失败状态和重试边界；运行客户端定向测试、`compileall`、`manage.py check`、迁移检查及 `git diff --check`。真实导入、生产 Token 和发布须另行授权。

## 未完成项

大文件断点恢复、压力测试、Windows 签名和真实多文件生产验收尚未完成；任何新存储、队列或服务变更需更新 `systemctl.md`。

## 实施记录摘要

2026-08-19～24 已完成 Token、客户端多文件、DPAPI 记忆、userscript/API 兼容和编辑链接；定向测试通过并按已验证 commit 发布。回滚为恢复上一个客户端/服务端 commit，不涉及数据回滚。
