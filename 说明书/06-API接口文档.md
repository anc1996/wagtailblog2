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
