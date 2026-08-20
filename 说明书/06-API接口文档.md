# WagtailBlog3 API 接口文档

## 1. 路由总表

根路由见 `wagtailblog3/urls.py`；应用路由见 `apps/blog/urls.py`、`archive/urls.py`、`comments/urls.py`、`search/urls.py`。具体路径以源码 urlpatterns 为准，新增接口必须同步测试和本文。

## 2. 搜索接口

| 视图 | 用途 | 关键参数 |
|---|---|---|
| `search.views.search` | HTML/fragment 搜索 | `query,type,start_date,end_date,order_by,page,cursor` |
| `search.views.search_results_api` | 结果 JSON | 同上，返回 fragment、分页和 canonical |
| `search.api.search_api` | REST JSON | `q/query,type,page/per_page,cursor` |
| `search.api.search_suggestions_api` | 联想 | `q/query` |

成功结果由 `format_search_results_for_api()` 统一格式化，包含标题、URL、日期、摘要/高亮和分页；不包含正文全文、Mongo ID 和内部异常。503 错误码为 `search_unavailable`；游标无效/窗口超限为 400。

## 3. 写接口

评论、反应、后台管理等写操作在各 app views/forms 中实现，使用 Django session、CSRF、权限和速率限制。接口修改必须检查模板 AJAX 调用、状态码、匿名用户、重复提交和 CSRF 失败路径。

## 4. 文档验证

接口变更执行 Django tests，并用 Playwright 验证浏览器 URL、响应 JSON、空结果、错误页、移动端分页和控制台无异常。调试文件只写入 `output/playwright/`。

## 5. Markdown 导入接口

Markdown 导入接口位于 `/blog/api/markdown-import/`，同时支持 JWT Bearer 和已登录 Django Session；所有接口要求认证，导入还要求 `blog.add_blogpage` 与目标 `BlogIndexPage` 的 `can_add_subpage(BlogPage)` 权限。

| 方法 | 路径 | 作用 | 是否写入内容 |
|---|---|---|---|
| GET | `limits/` | 返回图片大小、远程图片协议、存储别名和音视频深度探测能力 | 否 |
| GET | `destinations/` | 返回当前用户可新增 `BlogPage` 的索引页 | 否 |
| POST | `duplicate-titles/` | 返回目标索引页下的同标题页面提示，不阻断导入 | 否 |
| GET | `ai/templates/?target_parent_id={id}` | 返回当前启用的博客 AI 提示词模板摘要 | 否 |
| POST | `ai/suggest/` | 按显式模板为受限纯文本生成简介和标签建议 | 否 |
| POST | `preview/` | 校验目标权限和块计划，返回块/媒体计数 | 否 |
| POST | `import/` | 接收 JSON manifest 与 multipart 媒体，创建未发布草稿和 revision | 是 |
| POST | `sessions/` | 只接收 JSON manifest，创建可恢复的大批量导入会话 | 是（审计行） |
| GET | `sessions/{session_id}/` | 读取本人会话的上传、组装和缺失媒体状态 | 否 |
| POST | `sessions/{session_id}/artifacts/{artifact_id}/upload/` | 一次只上传一个媒体并重新校验 SHA-256、大小和 Wagtail 表单 | 是（该媒体） |
| POST | `sessions/{session_id}/finalize/` | 所有 artifact 已终态后投递异步页面组装 | 是（任务状态） |

`import/` 的 `manifest` 至少包含 `target_parent_id`、UUIDv4 `idempotency_key`、非空 `intro`、`blocks` 和 `artifacts`；`tags` 可选但必须为字符串列表。每个 artifact 的 `upload_field` 必须对应 multipart 文件，并携带客户端计算的 `size_bytes`/`sha256`，服务端会重新计算，内容不一致只失败该媒体。服务器不根据远程 URL 抓取互联网。远程图片由 CLI 在显式 `--allow-external-images` 下下载和解码后上传，服务端仍使用 Wagtail 表单执行最终格式、内容、collection 和大小校验。

导入正文按原顺序组装为 `markdown_block`、`image_block`、`video_block`、`audio_block`、`embed_block` 和 `mermaid_chart`；失败媒体在原位生成独立缺失 Markdown 块，不回滚其他成功媒体。Mermaid 只写 `code`/`renderer`，embed 只写 `title`/`embed_url`，图片和音视频写 chooser 主键。接口不会调用 `publish()`。

响应状态字段为 `success`、`partial_success`、`processing`、`failed` 或 `cleanup_retry`；同一用户重复使用同一幂等键且请求指纹一致时返回原批次，不重复创建页面，指纹不同返回 HTTP 409 `idempotency_conflict`。错误只返回稳定错误码，不返回本地路径、正文、Token、存储凭据或内部异常。

大批量客户端应使用 `sessions/`，而不是旧的 `import/` 单次 multipart。服务端目前采用受认证的逐 artifact 上传：每次请求只含一个文件，客户端可按同一会话逐项重试，完成后由 `maintenance` 队列组装一个未发布页面。`limits/` 同时返回会话数量、总大小和单文件限制。会话过期时不会扫描存储桶，而是只清理数据库中已记录的成功 artifact；当前不向客户端暴露 MinIO/S3 写入凭据或任意对象路径。

AI 建议接口使用同一 Markdown 导入认证和目标索引页权限。模板列表只返回 `id/name/description/version`，不返回提示词正文；客户端必须为每个 Markdown 文件显式选择模板。`ai/suggest/` 只接受客户端从解析计划提取的受限纯文本 `context`、`template_id`、`target_parent_id` 和语言，服务端每次重新校验模板存在、启用且完整。响应只返回简介与 3 至 5 个标签，不覆盖标题，不写 session、batch、页面或媒体；外部模型请求保持 `store=false`。模板失效、AI 未配置或生成失败不阻断用户手工填写元数据后继续导入。
