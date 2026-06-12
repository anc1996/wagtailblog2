# **博客系统 - API 接口文档 (API Reference)**

**版本: 2.0 (架构重构版)** **日期: 2025-06-13** ---

## **1. 引言 (Introduction)**

### **1.1 目的 (Purpose)**

本 API 接口文档旨在定义“WagtailBlog3 博客系统”经历底层异构解耦后，对外暴露的所有 RESTful 及 AJAX 异步接口。由于系统重构了检索与渲染链路，由传统的纯服务端渲染（SSR）全面演进为 **SSR + AJAX 惰性截流加载** 架构，本说明书重点规范了前端如何与后端的 ES8 / MySQL 双轨引擎进行高性能的数据交互。

### **1.2 接口基础约定 (Base Conventions)**

* **协议**: 生产环境强制使用 `HTTPS`。
* **数据格式**: 所有异步请求的 `Content-Type` 及 `Accept` 均默认为 `application/json`。
* **身份认证 (Auth)**:
* 检索类接口（AJAX Search, Suggestions）为公开只读接口，无需认证。
* 互动类接口（Comments, Reactions）采用 Django 原生 Session + CSRF Token 防护机制，或针对外部接入端点使用 JWT（JSON Web Token）认证。


* **基础路径**: 示例相对路径均以站点根域名为基准。

## **2. 全局响应规范 (Global Response Format)**

系统针对 AJAX 异步请求制定了统一的响应封装结构与 HTTP 状态码规范。

### **2.1 成功响应**

HTTP 状态码为 `200 OK`，正文返回 JSON：

```json
{
  "query": "检索词",
  "results": [ /* 数据列表 */ ],
  "total_count": 102,
  "current_page": 1
}

```

### **2.2 错误响应**

发生内部降级或熔断时，HTTP 状态码通常返回 `400` 或 `500`，正文必定包含 `error` 字段：

```json
{
  "error": "搜索处理错误: Elasticsearch 连接超时",
  "query": "检索词",
  "results": []
}

```

## **3. 核心异步检索接口 (Core AJAX Search APIs)**

该模块接口是本次系统重构的“吞吐量担当”。接口底层直接挂载了 `LazyChainedResultList` 惰性分页生成器与 O(1) MongoDB 批处理注射机制，彻底斩断了深度分页击穿和 OOM 风险。

### **3.1 惰性分页检索接口 (`Search AJAX API`)**

* **接口路径**: `/search/`
* **请求方法**: `GET`
* **前置依赖**: 必须在请求头中携带 `X-Requested-With: XMLHttpRequest` 以触发后端的 AJAX 路由分发。
* **功能描述**: 用于前台搜索结果页的“无限滚动加载”或“动态无刷新翻页”。根据是否携带搜索词，后端会自动将请求路由给 Elasticsearch 8 或 MySQL Coalesce 聚合引擎。

**请求参数 (Query Parameters)**:

| 参数名       | 类型     | 必填 | 描述                      | 架构层备注                          |
| ------------ | -------- | ---- | ------------------------- | ----------------------------------- |
| `query`      | `string` | 否   | 搜索关键词                | 为空时强制降级走 MySQL 原生时间排序 |
| `page`       | `int`    | 否   | 请求的页码，默认 1        | 激活后端的 Generator 惰性切片       |
| `type`       | `string` | 否   | 搜索类型，默认 `all`      | 可选 `all`, `blog`, `pages`         |
| `start_date` | `string` | 否   | 过滤起始日期 (YYYY-MM-DD) |                                     |
| `end_date`   | `string` | 否   | 过滤结束日期 (YYYY-MM-DD) |                                     |
| `order_by`   | `string` | 否   | 排序字段                  | 例如 `date_desc`, `title_asc`       |

**请求头示例 (Headers)**:

```http
GET /search/?query=异构解耦&page=2 HTTP/1.1
Host: yourdomain.com
X-Requested-With: XMLHttpRequest

```

**响应数据 (Response - 200 OK)**:

```json
{
  "query": "异构解耦",
  "results": [
    {
      "id": 12,
      "title": "深度重构：异构解耦单体极致性能演进",
      "url": "/blog/heterogeneous-architecture/",
      "date": "2025-06-13",
      "preview_text": "探讨 Wagtail 底层序列化拦截与 O(1) 无感注射..." // O(1)注射机制从 MongoDB Hash Map 中提取的纯文本摘要
    }
  ],
  "has_next": true,
  "has_previous": true,
  "total_count": 45,
  "current_page": 2,
  "total_pages": 3,
  "search_type": "all",
  "start_date": "",
  "end_date": "",
  "order_by": "date_desc"
}

```

### **3.2 搜索词补全建议接口 (`Search Suggestions API`)**

* **接口路径**: `/search/suggestions/` （示例路由）
* **请求方法**: `GET`
* **功能描述**: 用户在搜索框实时输入时，通过该接口获取实时的分词前缀匹配建议。极度轻量，命中 Redis L1 缓存或 ES8 的 Edge N-gram 索引。

**请求参数 (Query Parameters)**:

| 参数名 | 类型     | 必填 | 描述               |
| ------ | -------- | ---- | ------------------ |
| `q`    | `string` | 是   | 用户的当前输入片段 |

**响应数据 (Response - 200 OK)**:

```json
{
  "suggestions": [
    "异构解耦架构",
    "异构数据库同步",
    "异构存储防雪崩"
  ]
}

```

## **4. 用户互动接口 (User Interaction APIs)**

此类接口维持了博客系统与读者的动态连接，所有 POST 操作必须严格校验 CSRF Token 以及进行高频防刷锁定。

### **4.1 提交文章反应 (Submit Reaction)**

* **接口路径**: `/api/reactions/submit/` （示例路由）
* **请求方法**: `POST`
* **功能描述**: 读者点击文章底部的“点赞”、“收藏”等反应按钮时触发。

**请求头 (Headers)**:

```http
Content-Type: application/json
X-CSRFToken: <前端获取的 CSRF Token>

```

**请求体 (Request Body)**:

```json
{
  "page_id": 12,
  "reaction_type_id": 1
}

```

**响应数据 (Response)**:
系统会依靠底层的 `UNIQUE KEY (page_id, session_key, ip_address)` 数据库复合锁拦截雪崩刷单。

* **200 OK** (成功):

```json
{
  "status": "success",
  "message": "反应记录成功",
  "current_count": 42
}

```


* **403 Forbidden** (防刷拦截):

```json
{
  "status": "error",
  "message": "您已经对此文章表达过该态度"
}

```



### **4.2 异步提交评论 (Submit Comment)**

* **接口路径**: `/api/comments/add/` （示例路由）
* **请求方法**: `POST`
* **功能描述**: 提交文章评论，直接存入 MySQL 骨架数据库。包含邮件通知机制（由 Celery 异步队列在后台派发）。

**请求体 (Request Body - Form Data / JSON)**:

```json
{
  "page_id": 12,
  "parent_id": null, 
  "author_name": "架构爱好者",
  "author_email": "fan@example.com",
  "text": "这个 O(1) 注射设计太精妙了，彻底消灭了 N+1 慢查询！"
}

```

**响应数据 (Response - 201 Created)**:

```json
{
  "status": "success",
  "message": "评论提交成功，正在等待管理员审核。",
  "comment": {
    "id": 105,
    "author_name": "架构爱好者",
    "date": "刚刚"
  }
}

```

## **5. Wagtail Headless API (可选集成)**

虽然我们的系统前端模板主要基于 Django 服务端渲染构建，但在后续若需接入小程序或分离式前端（如 Vue/React 独立站），系统原生支持开放 Wagtail 的 Headless API (v2)。

* **端点**: `/api/v2/pages/`
* **功能**: 全量输出页面的结构化数据。
* **架构注意**: 必须在 Wagtail API 序列化器中进行异构拦截。由于 `body` 在 MySQL 中为空（`[]`），必须在 API 输出层触发 `get_content_from_mongodb()`，经由 `MongoDBStreamFieldAdapter` 重组为 JSON 后，再通过 API 返回给小程序端。

## **6. API 性能与安全约束 (Performance & Security Policies)**

1. **防穿透查询限流 (Rate Limiting)**: 针对 `/search_ajax/` 和 `/search/suggestions/`，Nginx 层必须配置 `limit_req` 模块，限制单一 IP 频率不超过 `10r/s`，防止恶意爬虫耗尽 ES8 或 MongoDB 的连接池。
2. **惰性拦截 (Lazy Blocking)**: 在 `/search_ajax/` 接口中，若传入的 `page` 参数远超实际分页深度（如 `page=9999`，尝试执行深度分页攻击），内部的 `LazyChainedResultList` 生成器将直接抛出 `EmptyPage` 异常，接口必须拦截并降级返回空数组 `results: []`，**坚决杜绝因深度遍历导致内存溢出 (OOM)**。
3. **严格的跨域策略 (CORS)**: 若互动类接口需要对外提供服务，必须通过 `django-cors-headers` 模块配置严格的白名单。对于同源请求，依然必须携带 `X-CSRFToken` 凭证。