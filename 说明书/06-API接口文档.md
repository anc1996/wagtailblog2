# **博客系统 - API接口文档**

**版本: 1.0**

**日期: 2025-06-16**

---

## **1. 引言 (Introduction)**

### **1.1 概述**
本API文档为开发者提供了与“Wagtail博客系统”进行程序化交互所需的所有信息。本系统API主要分为两部分：
1.  **Wagtail核心API (v2)**: 由Wagtail框架提供的一套功能强大的、用于访问内容（如页面、图片、文档）的只读API。
2.  **自定义API**: 为满足特定业务需求而开发的API，例如高级搜索功能。

### **1.2 基础URL (Base URL)**
所有API的端点（endpoint）都相对于你的域名。本文档中将使用以下基础URL作为示例：
`https://your_domain.com/api/v2/`

### **1.3 API自动文档 (Swagger UI)**
为了方便开发者进行交互式探索和测试，本系统通过`drf-yasg`集成了Swagger UI。强烈建议访问以下地址以获取实时、可交互的API文档：
* **Swagger UI**: `https://your_domain.com/swagger/`
* **ReDoc**: `https://your_domain.com/redoc/`

---

## **2. 认证 (Authentication)**

对于需要认证的接口，本API采用 **JWT (JSON Web Token)** 进行无状态身份验证。

### **2.1 获取访问令牌 (Access Token)**
你需要使用已注册用户的凭据向令牌端点发送一个POST请求，以获取访问令牌 (`access`) 和刷新令牌 (`refresh`)。

* **Endpoint**: `POST /api/token/`
* **Request Body**: `application/json`
    ```json
    {
        "username": "your_username",
        "password": "your_password"
    }
    ```
* **Success Response (200 OK)**:
    ```json
    {
        "refresh": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
        "access": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
    }
    ```

### **2.2 使用访问令牌**
在随后的所有需要认证的API请求中，你必须在HTTP请求头中包含 `Authorization` 字段。

* **Header**: `Authorization: Bearer <your_access_token>`

### **2.3 刷新访问令牌**
访问令牌 (`access`) 的有效期较短。当它过期后，你可以使用刷新令牌 (`refresh`) 来获取一个新的访问令牌，而无需用户重新输入密码。

* **Endpoint**: `POST /api/token/refresh/`
* **Request Body**: `application/json`
    ```json
    {
        "refresh": "<your_refresh_token>"
    }
    ```
* **Success Response (200 OK)**:
    ```json
    {
        "access": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
    }
    ```

---

## **3. 自定义API端点**

### **3.1 站内搜索 (Search)**
此端点提供了由后端搜索引擎（MongoDB/Elasticsearch）驱动的高级全文搜索功能。

* **Endpoint**: `GET /api/v2/search/`
* **描述**: 根据查询词在博客文章中执行全文搜索。
* **认证**: 无需认证。
* **查询参数 (Query Parameters)**:
    | 参数名 | 类型 | 必需 | 描述 |
    | :--- | :--- | :--- | :--- |
    | `q` | `string` | 是 | 要搜索的关键词。 |
    | `page` | `integer`| 否 | 请求的页码，默认为`1`。 |
* **Success Response (200 OK)**:
    ```json
    {
        "count": 25, // 结果总数
        "next": "https://your_domain.com/api/v2/search/?page=2&q=wagtail", // 下一页链接
        "previous": null, // 上一页链接
        "results": [ // 当前页的结果列表
            {
                "id": 15,
                "meta": {
                    "type": "blog.BlogPage",
                    "detail_url": "https://your_domain.com/api/v2/pages/15/",
                    "html_url": "https://your_domain.com/blog/my-first-post/",
                    "slug": "my-first-post",
                    "first_published_at": "2025-06-10T10:00:00Z"
                },
                "title": "我的第一篇Wagtail博客",
                "date": "2025-06-10",
                "authors": [
                    {
                        "id": 1,
                        "name": "张三"
                    }
                ],
                "highlight": "这是一篇关于如何使用 **Wagtail** 和Django技术栈来快速搭建..." // 搜索结果高亮片段 (如果有)
            },
            // ... more results
        ]
    }
    ```
* **示例 (cURL)**:
    ```bash
    curl -X GET "https://your_domain.com/api/v2/search/?q=wagtail&page=1"
    ```

---

## **4. Wagtail核心API端点**

以下是Wagtail提供的标准内容API，它们功能强大，支持丰富的过滤和查询选项。

### **4.1 页面列表 (Listing Pages)**
* **Endpoint**: `GET /api/v2/pages/`
* **描述**: 获取系统中所有发布状态的页面列表。
* **认证**: 无需认证。
* **常用查询参数**:
    | 参数名 | 示例 | 描述 |
    | :--- | :--- | :--- |
    | `type` | `blog.BlogPage` | 按页面模型类型过滤。 |
    | `fields`| `title,date,authors` | 指定响应中包含的字段，`*`表示所有。 |
    | `limit` | `10` | 每页返回的结果数量，默认为20。 |
    | `offset`| `20` | 起始偏移量，用于分页。 |
    | `order` | `-date` | 按字段排序，`-`表示降序。 |
    | `search`| `django` | 在Wagtail内置搜索后端中执行简单搜索。 |
    | `child_of`| `5` | 获取ID为5的页面的所有直接子页面。 |
    | `descendant_of` | `5` | 获取ID为5的页面的所有后代页面。 |
* **示例 (cURL)**: 获取最新的5篇博客文章，只返回标题和发布日期。
    ```bash
    curl -X GET "https://your_domain.com/api/v2/pages/?type=blog.BlogPage&fields=title,date&order=-date&limit=5"
    ```

### **4.2 页面详情 (Page Detail)**
* **Endpoint**: `GET /api/v2/pages/<id>/`
* **描述**: 获取单个页面的详细信息。
* **认证**: 无需认证。
* **Success Response (200 OK)**:
    响应体是一个JSON对象，包含了该页面的所有字段（包括`body` StreamField的JSON结构）和元数据。
    ```json
    {
        "id": 15,
        "meta": {
            "type": "blog.BlogPage",
            "detail_url": "https://your_domain.com/api/v2/pages/15/",
            "html_url": "https://your_domain.com/blog/my-first-post/",
            // ... more meta fields
        },
        "title": "我的第一篇Wagtail博客",
        "date": "2025-06-10",
        "body": [
            {
                "type": "rich_text",
                "value": "<p>这是段落内容。</p>",
                "id": "uuid-goes-here"
            },
            {
                "type": "code",
                "value": {
                    "language": "python",
                    "code": "print('Hello, World!')"
                },
                "id": "another-uuid-here"
            }
        ],
        "authors": [
            // ... author objects
        ]
    }
    ```
* **示例 (cURL)**:
    ```bash
    curl -X GET "https://your_domain.com/api/v2/pages/15/"
    ```

---

## **5. 通用响应与错误码**

* **`200 OK`**: 请求成功。
* **`201 Created`**: 资源创建成功 (用于POST请求)。
* **`204 No Content`**: 请求成功，但无内容返回 (用于DELETE请求)。
* **`400 Bad Request`**: 请求无效，例如参数错误或请求体格式不正确。响应体中通常会包含错误详情。
* **`401 Unauthorized`**: 未经授权。请求头中缺少有效的`Authorization`信息。
* **`403 Forbidden`**: 已认证，但无权访问该资源。
* **`404 Not Found`**: 请求的资源不存在。
* **`500 Internal Server Error`**: 服务器内部发生错误。

