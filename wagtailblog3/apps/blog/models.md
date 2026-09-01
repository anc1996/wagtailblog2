# Blog 应用模型与正文存储说明

本文以当前 `wagtailblog3/apps/blog/models.py`、`wagtailblog3/mongo.py` 和搜索服务实现为准。旧文档曾把 `BlogPage.body` 描述为 Mongo 正文主存储，这已经不准确。

## 1. 总体分层

```text
MySQL / Wagtail
  Page、BlogPage、title、intro、date、标签、分类、作者、发布状态
  BlogPublicationState、Wagtail Revision、PageDeletionIntent、Outbox 元数据

MongoDB
  content_body_versions           不可变正式正文版本
  blog_page_revision_bodies       草稿/历史 Revision 正文快照
  blog_content                    旧版兼容集合，仅供遗留页面读取或清理

Elasticsearch v005
  已发布内容的可重建搜索投影；不是正文权威库
```

MySQL 负责目录、权限、页面树、元数据和状态协调；MongoDB 负责正文版本；Elasticsearch 负责搜索。三者之间通过 MySQL State、Outbox 和 Delivery 传递版本化事件。

## 2. BlogPage

`BlogPage` 继承 Wagtail `Page`，其 MySQL 字段包括：

- `date`：文章日期。
- `intro`：文章简介和搜索摘要，保留在 MySQL，前台列表及搜索投影优先使用它。
- `tags`、`categories`、`authors`：文章组织和筛选关系。
- `featured_image`、`gallery_images`：媒体关系。
- `body`：Wagtail `StreamField`，仅作为后台编辑、校验和序列化接口。正式保存时模型会暂时把它写成 `[]` 再调用父类保存，避免正文落入 MySQL；对象内存中的正文随后恢复。
- `mongo_content_id`：可空的旧 `blog_content` 兼容指针。新页面和新版本不要求该字段；不能把它当作现代正文是否存在的判断条件。

正文块仍保持原有 `rich_text`、`markdown_block`、`code_block`、`mermaid_chart`、媒体和表格等 StreamField key，尤其不能改变 `markdown_block` 的存储 key。

## 3. MySQL 状态模型

### BlogPublicationState

每个 BlogPage 一行，以 `page_id` 为主键，保存正文指针而不是正文内容：

- `draft_body_version_id`、`draft_body_sha256`、`draft_body_schema_version`：最新草稿正文版本。
- `published_body_version_id`、`published_body_sha256`、`published_body_schema_version`：当前公开正文版本。
- `publication_generation`：公开投影的单调代次，防止旧事件覆盖新事件。
- `approved_revision_id` 及 approved body 字段：Workflow 审批通过时绑定的 Revision 和正文版本。

State 与页面发布动作在同一 MySQL 事务中更新；Mongo 正文写入成功后才允许切换指针。

### Wagtail Revision

Revision 继续由 Wagtail 管理历史、预览、比较和恢复元数据。其 `content` 保存页面字段及 Mongo 指针（`mongo_body_version_id`、hash、schema version），不保存现代正文副本。

历史预览/比较/恢复时，根据 Revision 指针读取 `content_body_versions`；指针缺失或正文校验失败必须报告明确错误，不能静默显示当前正式正文。早期没有 Mongo 指针的历史 Revision 才能走兼容读取路径。

### 删除编排模型

- `PageDeletionIntent`：页面级删除状态机，记录 manifest、step、租约、重试次数、已删计数和错误码。
- `MongoCleanupIntent`：单个 Mongo 指针的精确清理意图，支持幂等、重试和死信状态。
- `LegacyBlogRegistrationAudit`：旧文章登记现代正文版本的幂等审计记录。
- `BlogPublicationConsistencyCheckpoint`：只读对账任务的游标、租约和统计，不参与发布写入。

## 4. MongoDB 三个集合

### content_body_versions

现代正文的不可变版本库。关键字段为：`aggregate_type`、`aggregate_id`（BlogPage 主键）、`body_version_id`、`body_sha256`、`body_schema_version`、`body`、`created_at`。每次正式正文变化生成新版本，发布只切换 MySQL 的 `published_body_version_id`，不原地覆盖旧版本。

### blog_page_revision_bodies

Wagtail 草稿和历史 Revision 的正文快照库。关键字段为 Mongo 指针 `_id`、`page_id`、`body`、`created_at`。每次保存草稿或创建历史 Revision 可产生一条快照；它与 Wagtail `Revision` 的 `object_id`/指针配对，用于后台预览、比较和恢复。

### blog_content（遗留）

旧版可变正文集合，文档通常含 `_id`、`page_id`、`title`、`intro`、`body`。现代页面不再依赖它；只有没有现代 State/Revision 指针的遗留页面才允许兼容读取。新建、编辑和发布不能因为 `mongo_content_id` 为空而写入该集合。清理时必须按明确 ObjectId/指针逐条删除，不能按 `page_id` 无条件批量删除。

## 5. 保存、发布和读取流程

### 新建或编辑草稿

1. Wagtail 接收表单并校验 StreamField。
2. 写入 Mongo 不可变正文版本或 Revision 快照。
3. Revision 内容记录 `mongo_body_version_id`、hash 和 schema version。
4. MySQL `BlogPage.body` 以空列表持久化，`intro` 等元数据正常保存。
5. 草稿不会写入公开搜索 Outbox，也不会进入公开 ES 文档。

### 发布

1. 锁定 BlogPage、State 和目标 Revision。
2. 校验 Revision 指针、Mongo 正文、hash 和 schema version。
3. 在同一 MySQL 事务中切换 `published_body_version_id` 并递增 `publication_generation`。
4. Wagtail 完成发布后写入搜索 Outbox；Delivery Worker 将最新版本投影到 ES v005。
5. 旧事件若 generation 较小，必须被拒绝，不能覆盖新文档。

Workflow 审批和定时发布都必须在批准时、实际发布时再次校验 Revision 与正文版本是否漂移。

### 前台读取

BlogPage 详情页只读取 State 指向的 `published_body_version_id`。Mongo 不可用或正文无效时返回安全的空内容/错误状态，不能回退到草稿或其他正式版本。仅当 State 尚未建立时，才允许读取 `mongo_content_id` 指向的遗留 `blog_content`。

### 取消发布

取消发布在 MySQL 事务中清除公开状态并生成 tombstone Outbox；ES 仅保留不可搜索 tombstone，防止旧 upsert 重新出现。

### 删除

用户删除只创建 `PageDeletionIntent`，页面进入删除编排状态，不立即绕过清单删除 Mongo。Worker 按 manifest 和 `step`：

1. 检查其他页面是否引用正文版本；发现共享引用则进入 `blocked_reference`。
2. 投递 ES tombstone。
3. 精确删除该页面在 `content_body_versions`、`blog_page_revision_bodies` 及明确遗留 `blog_content` 指针下的文档。
4. 全部成功后才允许 Wagtail/MySQL 页面物理删除；部分失败保留已完成步骤并重试，超过阈值进入 `dead`。

这样可以避免“先删 MySQL、Mongo 清理失败”造成孤儿正文，也能保证批量删除和单页删除使用同一条链路。

## 6. 搜索边界

Wagtail `search_fields` 保留 `title`、`intro`、`body_text`、日期、标签和分类声明，供框架兼容和索引构建使用；生产前台搜索实际读取 ES 内容索引。搜索文档由已发布 Mongo 正文和 MySQL 元数据组装，包含 `page_id`、正文版本 hash、`publication_generation` 和 `searchable` 标志，可通过 rebuild 完全重建。

## 7. 数据保护与维护规则

- `intro`、标题、权限和页面树不应迁移到 Mongo 代替 MySQL。
- `body` 为空是现代存储设计的正常状态，不代表文章无正文。
- `mongo_content_id` 可空，不得作为现代文章登记或搜索的必填条件。
- 不得删除仍被 State、Revision、其他页面或清理意图引用的 Mongo 版本。
- 对账优先只读；修复命令必须显式 `--apply`、幂等并记录 hash。
- 生产 Mongo/ES 清理、迁移和 alias 切换必须先备份并取得单独授权。

## 8. 与旧文档的差异

旧版“`BlogPage.save()` 直接把正式正文写入 `blog_content`”和“`delete()` 同步删除 Mongo”描述已经废止。当前实现以不可变正文版本、State 指针、Wagtail Revision 快照、Outbox/Delivery 和删除状态机为准；旧集合只保留兼容读取和受控清理职责。
