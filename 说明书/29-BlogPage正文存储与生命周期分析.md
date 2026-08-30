# BlogPage 正文存储与生命周期改造最终记录

> 状态：截至 2026-08-30 的统一基线。
>
> 本文把早期调研、分批实现、测试、生产迁移和后续设计合并为一份最终记录。它不是新的生产变更授权。数据库迁移、正文回填、Elasticsearch 重建或 alias 切换、数据清理、服务重启和回滚，仍须针对准确目标单独确认。

## 1. 文档目的与结论

### 1.1 最终架构

项目采用以下四层架构：

```text
MySQL / Wagtail
  页面树、元数据、权限、Workflow、Revision 元数据
  当前草稿/审批/正式正文指针、publication_generation、Outbox
             |
             | 版本 ID + SHA-256 + schema
             v
MongoDB
  不可变正式正文版本、草稿和历史 Revision 正文快照
             |
             | MySQL Outbox 至少一次投递
             v
Elasticsearch
  仅保存可重建的已发布搜索投影
             |
             v
Redis / 页面缓存 / 前台查询
```

核心原则是：MySQL 决定内容是否公开以及公开哪个正文版本；Mongo 保存大正文；Elasticsearch 不是权威库，只是可重建投影。跨库不追求分布式事务，而使用不可变版本、MySQL 事务内状态与 Outbox、幂等 Delivery、版本围栏和只读对账实现最终一致性。

### 1.2 当前结论

- MySQL `blog_blogpage.intro` 应保留。它是列表、SEO、RSS、后台和搜索元数据，不是正文冗余。
- MySQL `blog_blogpage.body` 字段定义应保留，以维持 Wagtail `StreamField` 的表单、校验、序列化和 Revision 契约；持久值保持 `[]` 是当前正文分离设计。
- `BlogPage.mongo_content_id` 明确允许 `NULL`，只是旧 Mongo `blog_content` 的兼容指针，不是新版正式正文的必填身份。
- 新版正式正文身份是 `BlogPublicationState.published_body_version_id`、`published_body_sha256`、`published_body_schema_version` 三元组。
- 13 篇测试文章虽然 `mongo_content_id=NULL`，但 State 完整、Mongo 不可变正文可读、正文块完整；这不是空正文或登记失败。搜索代码不得把旧 ID 当作必填条件。
- 早期文章没有草稿或历史 Revision 是正常历史状态。正式正文登记和搜索恢复不要求补造草稿，也不要求回写历史 Revision。
- 精选、人工运营内容继续使用 `BlogPage`。未来千万级采集文章不应全部进入 Wagtail Page 树，应在容量和产品边界明确后建设独立 `Article` 目录。

### 1.3 未来数据的唯一执行方案（历史数据不作为门槛）

从本节方案启用之后，新增文章和后续编辑只按新版链路处理。早期文章缺少草稿、Revision、旧 `mongo_content_id` 或存在状态不一致时，统一归类为历史兼容数据，不阻塞新文章写入、发布或搜索；历史数据是否补登记另立只读核对和迁移批次。

未来一篇文章的生命周期固定为：

```text
创建/编辑草稿
  -> Mongo 写入不可变 body_version
  -> MySQL Revision + BlogPublicationState.draft 指针
  -> 提交 Workflow（可选）/定时发布（可选）
  -> 发布事务：校验正文版本，切换 published 指针和 generation，写 Outbox
  -> maintenance Worker：Outbox -> Delivery -> ES v005 当前 serving 索引
  -> 搜索读取：read alias + MySQL live().public() 二次过滤
```

- 保存草稿只产生 Mongo 版本和 Wagtail Revision，不写公开 ES。
- 发布时必须校验批准版本仍等于待发布版本；校验通过后在同一 MySQL 事务内更新 State 和 Outbox。
- 编辑已发布文章不会原地修改旧正文，而是创建新版本；新版本发布后 generation 单调递增，旧 Delivery 自动 superseded。
- 取消发布、删除或权限收紧均写 tombstone Outbox；ES 删除/墓碑投影成功前，MySQL 前台查询继续作为防线。
- Outbox/Delivery 失败只进入 retry/dead 和告警，不回写空正文、不使用旧 `mongo_content_id` 顶替新版正文。
- 搜索索引只保留已发布内容投影，物理索引采用版本化名称（例如 `content-v005-<build>`），通过 read alias 原子切换；索引损坏时重建新物理索引，不在 serving 索引上做破坏性重建。

100 万篇规模下的运行规则：

1. MySQL 保存目录和状态，不保存大正文；所有列表查询必须走索引字段（站点、状态、日期、generation），正文按需从 Mongo 批量读取。
2. Mongo 正文按 `aggregate_id + created_at`、`body_version_id` 建索引，正文版本不可变；GC 只能依据清理意图、Revision/发布指针和备份引用执行。
3. Outbox、Delivery、State 使用 `(page_id, content_version)` 和 `(target_id, event_id)` 幂等键；消费者按稳定主键分页、租约和 checkpoint 扫描，禁止逐篇全表无界扫描。
4. ES 使用 alias、单调 generation 和 hash/body_version 围栏；批量导入采用 bulk + 游标断点，单批失败可重试，不阻塞其他批次。
5. 对账任务只读扫描 State→Mongo→ES 三元组，按时间窗口和 page_id checkpoint 分片；发现差异生成补偿事件，不直接修改正文。
6. 当 BlogPage 页面树、权限或后台操作成为瓶颈时，先用 1 万、10 万、100 万脱敏数据压测并记录 p95/p99、写入吞吐、ES bulk 错误、Mongo/ MySQL 磁盘和锁等待，再决定独立 Article catalog、读副本、Mongo/ES 分片；不预先改造 Wagtail 核心 Page 表。

## 2. 数据权威与不变量

### 2.1 数据归属

| 数据 | 权威位置 | 说明 |
| --- | --- | --- |
| 页面树、站点、标题、摘要、日期 | MySQL/Wagtail | 用于列表、权限、路由和后台管理 |
| 发布、Workflow、定时发布状态 | MySQL/Wagtail | 公开可见性的唯一判定来源 |
| 当前正文版本和公开代际 | `BlogPublicationState` | 保存草稿、审批、正式指针和 `publication_generation` |
| 正式正文 | Mongo `content_body_versions` | 不可变，按版本 ID/hash/schema 精确读取 |
| 草稿/历史正文 | Mongo Revision 快照 | 与 Wagtail Revision 对应，不进入公开搜索 |
| 旧正式正文 | Mongo `blog_content` | 仅供未登记旧页面兼容读取，不再作为新版身份 |
| 搜索状态与事件 | MySQL Search State/Outbox/Delivery | 可审计、可重试、按 Target 分发 |
| 搜索文档 | Elasticsearch 版本化物理索引 | 可重建，只保存公开投影 |
| 图片和附件 | Wagtail/MinIO | 不把二进制资源塞入正文版本 |

### 2.2 不可破坏的不变量

1. 已登记公开页面必须有可读取的正式正文三元组；缺失或哈希不一致必须显式失败，不能静默回退旧正文或空正文。
2. Mongo 正式正文版本不可变。编辑、恢复和再次发布只能创建或引用新版本，不能原地覆盖已发布版本。
3. Wagtail Revision 不可变。恢复历史版本应产生新的工作 Revision，不能改写旧 Revision 的 JSON 或正文快照。
4. `body=[]` 只表示 MySQL 不保存正文，不等于文章正文为空；Mongo 正文数组本身为空则是允许的有效内容。
5. 公开搜索只接受已发布版本，草稿、预览和历史 Revision 永远不能进入公开索引。
6. 页面状态、正式指针、`publication_generation` 和搜索 Outbox 必须在同一个 MySQL 事务边界内更新。
7. Delivery 必须以内容版本和 generation 做幂等围栏；迟到 upsert 不得覆盖较新版本或复活 tombstone。
8. 取消发布和删除必须生成 tombstone。不能只修改 Wagtail `live` 状态而留下公开搜索文档。
9. 删除流程先在 MySQL 记录墓碑和清理意图，再在事务提交后回收 Mongo；只要仍有 Revision、正式指针、备份或审计引用，就不得物理删除。
10. `ContentSearchState` 的 tombstone 永久保留。ES、缓存和 Delivery 均可重建或归档，但不能删除用于防复活的状态围栏。
11. 所有回填、重建和清理任务必须可 dry-run、可断点、可重试、可对账，并在写入前核对目标、manifest/hash 和备份。

## 3. 当前数据模型

### 3.1 MySQL Blog 模型

`BlogPage` 继续保留：

```text
page_ptr_id
date
intro
mongo_content_id     可空；旧 blog_content 兼容指针
body                 Wagtail StreamField 契约；MySQL 持久值通常为 []
```

`BlogPublicationState` 是新版生命周期状态：

```text
page_id
draft_body_version_id / sha256 / schema_version
approved_revision_id / approved_revision_created_at
approved_body_version_id / sha256 / schema_version
published_body_version_id / sha256 / schema_version
publication_generation
created_at / updated_at
```

其他已实现的 Blog 持久模型：

- `MongoCleanupIntent`：事务后 Mongo 清理意图、状态、租约、owner、重试和错误信息。
- `BlogPublicationConsistencyCheckpoint`：稳定游标、扫描上界、租约和周期对账状态。
- `LegacyBlogRegistrationAudit`：按页面、正文 hash 和 schema 唯一记录旧文章登记尝试，支持幂等和失败审计。

对应迁移为 `blog.0029` 至 `blog.0034`。

### 3.2 Mongo 集合

`content_body_versions` 保存不可变正文：

```text
_id / body_version_id
aggregate_type       当前为 blog_page，未来可扩展 article
aggregate_id         BlogPage 主键
body                 原始 StreamField block 数组
body_sha256
body_schema_version
source_revision_id   可选
created_at
```

同一聚合、hash 和 schema 的重复写入复用已有版本；读取时校验 `_id`、aggregate、hash 和 schema。

兼容集合：

- `blog_content`：旧的每页一份可变正式正文。仅无新版 State 指针的旧页面允许读取。
- `blog_page_revision_bodies`：旧 Revision 正文快照，兼容 ObjectId 和 `rev_<page>_<uuid>` 字符串 ID。

目前不删除兼容集合，不清理 Mongo 中的 `title`/`intro` 镜像，也不启用不可变正文版本 GC。任何收缩都要先完成读取方审计、备份和恢复演练。

### 3.3 MySQL Search 模型

现有搜索链路包括：

- `ContentSearchState`：每页当前搜索状态、content version、正文版本、generation、hash 和 tombstone。
- `ContentSearchOutbox`：不可变 upsert/tombstone 事件。
- `ContentSearchTarget`：物理索引目标及 building/serving 角色。
- `ContentSearchDelivery`：每个事件到每个 Target 的状态、租约、重试和错误。
- `ContentSearchBuild`：固定扫描上界、checkpoint、catch-up 和 READY 门禁。
- `ContentSearchScopeJob`：页面权限范围变化任务；当前只有创建端，消费者尚未实现。

对应扩展迁移为 `search.0006` 和 `search.0007`。

### 3.4 Elasticsearch 文档

独立内容索引使用 strict mapping 和版本化物理索引。公开文档至少携带：

```text
content_kind / namespace
page_id 或稳定 aggregate_id
title / intro / body_text
content_version
body_version_id / body_sha256 / body_schema_version
publication_generation
searchable
```

`mongo_content_id` 不写入公开搜索文档，也不应成为文档构建前置条件。查询通过 read alias，命中后仍须按 MySQL `live().public()` 和权限进行二次过滤。

## 4. 生命周期与事务边界

### 4.1 保存草稿和 Revision

1. 编辑器得到 `StreamField` 正文。
2. `serializable_data()` 把正文写入不可变版本和兼容 Revision 快照。
3. MySQL Revision JSON 保持 `body=[]`，保存 Mongo 正文指针及 hash/schema。
4. 打开编辑、预览、比较或恢复时，`from_serializable_data()` 按 Revision 自身指针读取正文。
5. 非法指针、快照缺失、正文损坏和 Mongo 暂不可用分别呈现明确错误；不可恢复冲突返回 409，暂时不可用返回 503。

历史 Revision 没有新版指针时保留兼容读取。不得用当前已发布正文冒充历史草稿。可选的 Revision 绑定命令只做只读审计，不属于旧文章登记或搜索门禁。

### 4.2 常规发布

```text
锁定 Page + Revision + BlogPublicationState
  -> 读取并校验 Mongo 正文归属/hash/schema
  -> 切换 published 指针
  -> publication_generation + 1
  -> 同一 MySQL 事务写 Search State + Outbox
  -> 事务提交
  -> Delivery 异步写 ES / 失效缓存
```

Mongo 在 MySQL 提交前只产生不可变候选版本。MySQL 回滚时，旧公开指针不会改变，未引用 Mongo 版本留给后续对账处理。

### 4.3 Workflow 审批

审批完成时冻结批准的 Revision ID、创建时间和正文三元组。实际发布前再次锁定并校验批准 Revision；审批后产生的新草稿不能悄悄替换已批准正文。项目配置的 Workflow 完成动作进入统一发布服务，而不是只依赖 `page_published` 信号。

### 4.4 定时发布

定时发布以 Wagtail 调度器传入且已到期的批准 Revision 为准，不能简单采用页面最新 Revision。执行时重新校验批准时间、正文三元组和 generation，再进入与常规发布相同的指针切换和 Outbox 事务。

### 4.5 取消发布和删除

取消发布：

1. 锁定状态并递增 generation。
2. 写 tombstone State/Outbox。
3. 清除公开正文指针或更新公开状态。
4. 提交后由 Delivery 把 ES 文档改为 `searchable=false`。

删除：

1. 在 MySQL 事务内记录 tombstone 和 `MongoCleanupIntent`。
2. Wagtail/MySQL 删除成功提交后才唤醒 maintenance 消费者。
3. 消费者再次核对引用，使用租约、owner、重试和失败状态执行旧正式正文/Revision 快照清理。
4. 新版不可变正式正文目前保留，不执行物理 GC。

该顺序消除了旧 `pre_delete` 在 MySQL 提交前删除 Mongo、事务回滚后正文丢失的风险。

### 4.6 公开读取

- 已登记页面只按 `published_body_version_id/hash/schema` 读取不可变正文；读取失败不得回退旧 `blog_content`。
- 没有 `BlogPublicationState` 正式指针的旧页面才允许通过 `mongo_content_id` 兼容读取。
- `mongo_content_id=NULL` 对已登记页面完全合法。
- 搜索批量重建应批量读取 State 指针对应正文，避免逐页 N+1；只有无新版指针的旧页面才读取 `blog_content`。

## 5. 已完成改造汇总

早期逐轮编码记录已压缩为以下可验证里程碑。表中的“完成”表示代码或指定环境证据已存在，不自动表示当前工作区未提交改动已经部署。

| 里程碑 | 完成内容 | 主要结果 |
| --- | --- | --- |
| Revision 兼容 | 指针类型适配、严格正文恢复、409/503 错误分类 | 历史预览不再把缺失正文静默替换为正式正文 |
| 删除安全 | CleanupIntent、事务后唤醒、lease/retry/reclaim | MySQL 回滚不会因 `pre_delete` 提前清理而丢正文 |
| 正文版本化 | `content_body_versions`、hash/schema、insert-once | 已发布正文不再原地覆盖 |
| 发布状态 | `BlogPublicationState`、草稿/审批/正式指针、generation | 正文身份与 Wagtail 页面状态可对账 |
| 发布编排 | 常规、Workflow、scheduled 校验 | 审批版本和调度 Revision 有围栏 |
| 发布一致性 | State 与 Outbox 同事务、取消发布/删除 tombstone | 公开变更可异步重放且可审计 |
| 搜索投递 | State/Outbox/Target/Delivery、external version、generation | 迟到事件可标记 superseded |
| 在线重建 | 固定上界、checkpoint、catch-up、strict consistency、alias | 可构建新物理索引并保留旧索引回滚 |
| 对账 | 页面、State、Mongo、Search State/Outbox 的只读扫描 | 支持稳定游标、租约和周期执行 |
| 旧文章登记 | dry-run、expected-hash、单页 apply、审计锁和幂等 | 可把旧 `blog_content` 安全登记为不可变正式版本 |
| 生产迁移 | Blog 0029-0033、Search 0006-0007、v005 | 生产已完成 schema 和搜索投影迁移 |
| 存量登记 | 生产 1098 篇、测试 156 篇 | State、不可变正文和搜索身份完成登记与对账 |

核心运行代码最初集中提交于 `726978aaf1c2394167185c4ff45037de2a3ba3d5`；生产 ES v005 mapping/search 修复提交为 `e887cf12b9b200d15affdf872a7e4a7b157db7a7`。后续登记、审计和测试改进形成多个独立提交。每次发布仍以当次 `git log`、`origin/main` 和生产 HEAD 实查为准，不能把这些 SHA 当成永久发布目标。

## 6. 已验证证据

### 6.1 自动化验证基线

已完成过的相关门禁包括：

- Revision、发布、Workflow、scheduled、取消发布、删除、对账和搜索代际定向测试。
- Blog + Search 全量 483 项通过；曾出现的两项缺列错误已定位为迁移测试未恢复 schema 的夹具污染。
- 搜索正文、Delivery、rebuild 相关 48 项定向测试通过。
- 多个批次执行过 `compileall`、`python manage.py check`、`makemigrations --check --dry-run` 和 `git diff --check`。
- MySQL 对条件唯一约束的 Wagtail warning 是已知数据库能力提示，不等同于本改造测试失败。

这些是历史实施证据。当前未提交搜索修复在提交前必须重新运行对应门禁，不能只引用旧结果。

### 6.2 生产迁移与服务

2026-08-29 生产完成：

- 代码和 schema 部署；成功应用 `blog.0029` 至 `blog.0033`、`search.0006` 和 `search.0007`。
- 四个服务 `wagtailblog3.service`、maintenance Worker、Beat、Filebeat 均通过 active/enabled 验收。
- 首页 HTTP 200，生产 `manage.py check` 无错误。
- 迁移备份位于 `/home/source/Django/wagtail/backups/wagtailblog3-markdown-import-20260829-100724/`。

生产 `blog.0034` 是否已应用必须在下一次部署前用 `showmigrations blog` 重新核实，不能仅按测试环境记录推断。

### 6.3 生产 ES v005

2026-08-29 完成：

- snapshot repository：`wagtailblog3-pre-search-20260811-221511`。
- snapshot：`pre-search-20260811-221511`，状态 `SUCCESS`，`include_global_state=false`。
- 备份目录：`/home/source/Django/wagtail/backups/wagtailblog3-pre-search-20260811-221511/`。
- 新物理索引：`wagtailblog-prod-content-v005`。
- read alias：`wagtailblog-prod-content-read`。
- 全量回填 1098/1098 成功，缺失 0、失败 0，并完成 catch-up 和 alias 原子切换。
- 旧索引 `wagtailblog-prod-content-v003` 保留。
- 临时 BlogPage 1193 完成草稿、预览、Workflow 审批、发布、搜索命中和删除后不可搜索验收。

生产 v005 已工作，但集群为单节点，副本未分配时整体 health 可能为 yellow；必须持续观察 Delivery dead/retry、alias 唯一指向、磁盘水位和 bulk 错误。

### 6.4 生产存量登记

生产共登记 1098 篇旧 BlogPage：

- 每篇通过 dry-run 计算 expected-hash，再经受控单页 apply。
- 对账结果：State 1098、Mongo 不可变正式版本 1098、v005 upsert 文档 1098。
- `state_missing`、`mongo_missing`、`live_pointer_missing`、`outbox_missing`、`search_identity_mismatch` 均为 0。
- 全量备份目录：`/home/source/Django/wagtail/backups/wagtailblog3-registration-all-20260829-160000/`。
- ES snapshot：`wagtailblog3-search-snapshots/pre-register-all-20260829-162500`，状态 `SUCCESS`。

旧 live Revision 未绑定新版指针不影响正式正文或搜索。生产孤儿 `page_id=14` 经独立授权和定向备份后已删除；备份位于 `/home/source/Django/wagtail/backups/wagtailblog3-orphan-page14-20260829-170000/`。

### 6.5 测试环境存量登记

测试环境 156 篇 BlogPage 均已具备 State 和 Mongo 不可变正式正文：

- 初次已有现代版本 1 篇，剩余 155 篇登记成功。
- 登记后 `state_missing=0`、Mongo 正文缺失 0、Outbox 身份不一致 0。
- 定向备份位于 `/home/source/Django/wagtail/test-backups/registration-20260829-180000/`。
- 历史孤儿 `blog_content.page_id in (14,609,621)` 和一条 `page_id=null` 文档经授权、备份后清理；保留 tombstone 和所有正式版本。

测试库的 13 篇 `mongo_content_id=NULL` 页面已核实：13/13 State 完整，13/13 正式版本可读，正文文本长度约 341 至 9713；其中 12 篇含一个 block，1 篇含 17 个 block。问题是搜索旧字段前置判断，不是正文缺失。

## 7. 当前工作区与环境状态

本节记录 2026-08-30 文档整理时的快照；执行任何写操作前必须重新查询。

### 7.1 Git 状态

- 分支 `main`，本地领先 `origin/main` 8 个提交。
- HEAD 为 `e37788f`。
- 工作区已有未提交的搜索修复、测试和管理命令；这些改动属于此前开发，不是本次文档压缩产生。
- 本次文档任务只修改本文件，不提交、不推送、不部署。

### 7.2 未提交搜索修复

当前工作区已实现但尚未提交：

- 当正式 State/正文已存在时，搜索不再要求 `mongo_content_id` 非空。
- Delivery 对同代事件可补齐允许为空的 hash，并继续拒绝错误 hash。
- `search_drain_pending_deliveries`：默认 dry-run，生成 manifest/SHA；确认执行时限制小批并交给既有消费者。
- 排空命令的 manifest 已绑定 Delivery 重试预算、State/Event hash、Target 身份；确认执行遇到首个非 `succeeded/superseded` 结果即非零停止。
- 非公开 upsert 不再伪成功，而是以 `content_search_page_not_public` 进入 retry，避免权限任务未收敛时残留公开 ES 正文。
- alias 切换在执行前重新检查 fresh Build gate；回滚命令改为一次 alias API 原子绑定明确的旧物理索引。
- `search_archive_tombstones`：当前仅只读报告，不归档、不删除。
- `bind_blog_revision_bodies`：可选只读审计，不提供 apply，也不是搜索恢复前提。

本轮相关 66 项定向测试、`compileall`、Django check、迁移漂移检查和 `git diff --check` 已通过；提交前仍应重跑并复核实际 diff。

### 7.3 测试 v005 恢复现场

2026-08-30 受控恢复在首次 rebuild 永久错误处停止后的状态：

```text
Target: content-v005-fix
Index: wagtailblog-test-content-v005
role: building
Build: failed
checkpoint: 530 / 632
last_error: content_search_state_hash_mismatch
catch_up_streak: 0
Delivery: pending=76, processing=0, retry=0, dead=0
completed Delivery: succeeded=79, superseded=21
```

原 99 条 Delivery 已按五个 manifest 批次全部处理，结果为 79 `succeeded`、21 `superseded`，无 retry/dead。随后首次 rebuild 在 checkpoint 530 失败，并物化 76 条新的 pending Delivery；不得删除、手工改状态或直接复用历史 manifest。只读诊断确认 page 604、608、610–620 的 State `content_hash` 为空，但 13 篇的正式正文版本均可读取；这不是 `mongo_content_id=NULL` 或 Mongo 正文缺失。

## 8. 未完成事项与优先级

### 8.1 已完成：nullable legacy-ID 搜索修复

该项已在测试 v005 收尾前完成并通过定向测试。范围包括：

1. 核对 `document.py`、`delivery.py` 和对应测试，确保有正式 State 时不读取或校验旧 `mongo_content_id`。
2. 修正 `register_legacy_blog_page.py` 中仍称 `--apply` 不支持的过时模块说明，使文档与已实现行为一致。
3. 搜索修复、Delivery 排空命令及测试保持边界清晰；当前工作区仍需按发布门禁提交。
4. Revision 可选审计和 tombstone 只读报告未混入搜索运行时逻辑。

验收：已通过相关定向测试、`compileall`、`manage.py check` 和 `git diff --check`；最终提交前仍需再次执行完整门禁。

回滚：只回退代码提交；保留 State、Outbox、Delivery、Mongo 正文和失败 Build 证据。

### 8.2 已完成：测试 v005 受控收敛与 alias 切换

该项已按以下顺序完成（详见第 16 节）：

1. 重新只读核对 Target、物理索引、alias、Build 上界/checkpoint、State→Mongo 三元组和 Delivery 状态。
2. Delivery 分批排空，最终 pending/processing/retry/dead 均为 0。
3. checkpoint 恢复至 632，连续两次 clean catch-up。
4. Build=READY，严格一致性各项均为 0。
5. 通过 fresh gate 后原子切换测试 read alias，并保留旧索引作为回滚点。

公开文档验收项：`missing/stale/ahead/hash_mismatch/body_version_mismatch/generation_mismatch/wrong_tombstone=0`。不可搜索 tombstone 的 extra 可以保留，但必须单独分类。任一批出现永久 4xx、dead 或租约异常立即停止，不删除审计记录，从 checkpoint 恢复。

### 8.3 已完成：alias 原子回切门禁

当前 `search_rollback_content_alias` 已改为在一次 Elasticsearch `_aliases` 操作中切回显式传入的旧物理索引；缺少旧索引或 alias 漂移时拒绝写入。

合格实现必须：

- 记录并显式传入 previous physical index。
- 在一次 Elasticsearch `_aliases` 操作中 remove 当前索引并 add 旧索引。
- 切换前校验 alias 集合未漂移且恰好指向预期索引。
- 同步或明确修复 MySQL Target/Build 角色状态。
- 通过“切到 v005，再切回 v001，再切回 v005”的测试，且每个时刻 alias 恰好指向一个索引。

测试已覆盖“切换目标缺少旧索引参数时拒绝”和成功回切；生产切换仍须单独备份、授权和验收。

### 8.4 P0：实现 `ContentSearchScopeJob` 权限闭环

当前 restriction signal 会创建 ScopeJob，代码已具备 maintenance 消费者、稳定主键分页、租约、重试和 rescan 标记；仍需在真实父子页面权限变化场景完成持续验收，并确认生产 Beat/Worker 已加载该任务。搜索 Delivery 在页面变为非公开时仍依赖 ScopeJob 收敛 tombstone。

实现应覆盖：

- 根页面和子树的稳定主键分页。
- 权限新增后 tombstone，权限删除后重新 upsert。
- 租约认领、过期回收、幂等重试和失败审计。
- 处理中再次发生权限变化时创建后继任务或设置 rescan，不能丢事件。
- 前台 MySQL `live().public()`/权限二次过滤继续保留。

验收必须证明父子页面权限收紧后 ES 无可搜索正文、权限恢复后可重新投影、消费者崩溃后可续跑。回滚是停止 dispatcher/consumer、保留任务和 Outbox，并回切旧 alias；不得清空任务表。

### 8.5 P1：tombstone 归档与清理策略

当前只实现只读候选报告。近期数据量不需要 apply，也不应为了“表干净”提前删除审计链。

达到表体积、备份窗口或扫描延迟阈值后再实现：

- 归档运行表、事件表和脱敏 Delivery 子表。
- manifest SHA-256、单例锁、fencing token、稳定游标和断点恢复。
- apply 前重新校验 State tombstone、所有 required Target 已完成、无活动租约和 active build。
- 归档成功不等于允许 purge；物理清理另立命令和授权。
- 将来 purge 顺序为 Delivery → Outbox，永久保留 `ContentSearchState` tombstone。

### 8.6 P1：运维、恢复和规模准备

- 为生产独立搜索启用有限超时的 systemd readiness：MySQL、Mongo、Redis、MinIO、ES 和 read alias 唯一 serving 指向。
- 完成 MySQL/Mongo/ES/MinIO 的联合恢复演练，记录 RPO/RTO 和失败回退顺序。
- 监控 `state_missing`、Mongo 版本缺失、Outbox oldest age、pending/retry/dead、lease reclaim、Build/checkpoint、catch-up streak、alias、ES bulk 错误、锁等待和搜索 p95/p99。
- Mongo CleanupIntent 当前会解析 Revision 引用；当 Revision 达十万量级、检查 p95 超过租约一半或出现扫描导致的 reclaim/dead 时，建设引用索引。正式版本 GC 前该索引是强制前提。

### 8.7 P2：千万级 Article 演进

当前每日新增约十余篇，现有 BlogPage 架构无需立即拆分。进入独立 `Article` 立项须同时满足：

1. 产品明确需要百万/千万级采集或导入内容。
2. 1 万/10 万脱敏合成数据压测和容量模型证明 BlogPage 无法满足已定义 SLO。
3. 海量 Article 的权限、后台、生命周期和人工编辑需求确实不同于 Wagtail Page。

目标方向：

```text
MySQL Article catalog
  tenant/source_key/title/intro/status/permission/body pointer/generation
Mongo immutable body versions
MySQL Outbox
Elasticsearch unified typed projection
可选 wagtail_page_id，仅供精选内容进入 Wagtail
```

批量导入使用 source key 幂等、游标断点和 bulk 写入，不能逐篇 `add_child()`、触发完整 Wagtail hook 和 ORM `save()`。在实测前不预设 Mongo 分片数、ES shard 数，不对 Wagtail 核心 Page 表做分区。

## 9. 明确不做的事项

- 不为 13 篇有效文章补造 `mongo_content_id`。
- 不把历史无草稿、无 Revision 绑定视为正文或搜索故障。
- 不用当前正式正文伪造历史 Revision 内容。
- 不删除 MySQL `body` 字段，不恢复把大正文写回 MySQL。
- 不把 Elasticsearch 当权威库，不手工改写 ES 来掩盖 State/Outbox 问题。
- 不删除失败 Delivery、Outbox、tombstone 或 Build 现场来让统计归零。
- 不在未备份、未对账、未授权时回收 Mongo 正文或历史快照。
- 不在当前阶段引入 Mongo 分片、MySQL 分区或千万级 Article 运行代码。

## 10. 主要代码与运维边界

| 范围 | 主要位置 | 职责 |
| --- | --- | --- |
| Blog 页面/读取 | `wagtailblog3/apps/blog/models.py` | StreamField、正文读写、发布/取消发布入口 |
| Mongo 适配 | `wagtailblog3/apps/blog/mongo.py` | 版本/快照存取与错误分类 |
| 发布服务 | `wagtailblog3/apps/blog/services/publication.py` | Revision 校验、State 指针和 Workflow/scheduled 围栏 |
| 发布对账 | `wagtailblog3/apps/blog/services/publication_consistency.py` | 只读扫描、checkpoint 和差异分类 |
| 清理任务 | `wagtailblog3/apps/blog/tasks.py`、`signals.py` | CleanupIntent 唤醒、租约、重试和引用保护 |
| 旧文登记 | `wagtailblog3/apps/blog/management/commands/register_legacy_blog_page.py` | dry-run、expected-hash、单页 apply 和审计 |
| 搜索文档 | `wagtailblog3/apps/search/services/document.py` | 正文来源和 ES 投影契约 |
| 搜索投递 | `wagtailblog3/apps/search/services/delivery.py` | Target Delivery、generation/hash 围栏 |
| 搜索重建 | `wagtailblog3/apps/search/services/rebuild.py` | checkpoint、catch-up 和 build gate |
| alias | `wagtailblog3/apps/search/services/alias.py` | read alias 查询和原子切换 |
| 运维基准 | `systemctl.md` | 环境、服务依赖、发布、健康检查和回滚 |

任何运行时代码修改继续遵守第 22 号说明书：中文模块说明和必要 docstring、准确类型标注、不用 `Any` 隐藏未知契约，并报告 `compileall`、相关测试、Django check、迁移检查和 `git diff --check`。

## 11. 发布、数据与回滚门禁

### 11.1 测试环境

1. 固定 `WAGTAILBLOG_ENV=test` 和 WSL2 Conda `wagtailblog-test`。
2. 先 dry-run 和只读对账，再对准确目标申请 apply。
3. ES 恢复以小批次执行，保留 Build、Outbox 和 Delivery 证据。
4. alias 切换必须在 READY、两次 clean catch-up、严格一致性和原子回切测试后单独授权。

### 11.2 生产环境

1. 重新核实主机、目录、分支、commit、Conda、EnvironmentFile、服务名、ES alias 和备份状态。
2. 在 WSL2 完成测试、commit、push，并确认本地、`origin/main`、GitHub 远端为同一 SHA。
3. 生产工作树必须干净，使用 `fetch`、差异清单和 `merge --ff-only` 同步精确 commit。
4. 数据迁移、回填、alias、环境文件、unit 和服务重启分别说明影响、备份、顺序、验收和回滚，再取得确认。
5. 发布后检查四个服务、失败 unit、端口/socket、首页/后台、Django check、队列、Beat、Filebeat、ES、Outbox/Delivery 和应用日志。

### 11.3 回滚原则

- 代码：回退到上一个已验证 commit，但保留已应用的兼容表/列，除非反向迁移经过独立验证。
- 正文：只切换 MySQL 指针或恢复备份，不删除不可变版本、草稿或 Revision。
- 搜索：暂停受影响消费者，在一次 alias 操作中切回记录的旧物理索引；保留新旧索引和快照。
- 任务：停止 dispatcher/consumer，保留 State、Outbox、Delivery、ScopeJob、CleanupIntent 和 checkpoint。
- 数据清理：按 manifest 和定向备份恢复；不能通过创建伪造 Page/Revision 来补偿误删。

## 12. 验收清单

每个后续批次至少记录：

- 实际修改与明确不修改的文件。
- 环境、分支、commit、迁移和配置事实。
- 数据读取/写入范围，是否涉及正文、Revision、索引或服务。
- 定向测试、`compileall`、Django check、迁移漂移和 `git diff --check` 的精确结果。
- State→Mongo、State→Outbox/Delivery、State→ES 的一致性结果。
- Workflow、scheduled、取消发布、删除和权限变化中受影响的路径。
- 备份位置、manifest/hash、回滚命令和保留期。
- 未覆盖边界、历史数据污染和残余风险。

历史数据问题必须按事实分类：真正的 State/Mongo 缺失是故障；旧 Revision 无新版指针、早期无草稿、已删页面 tombstone 和 `mongo_content_id=NULL` 则可能是合法兼容状态，不能混为一类。

## 13. 模型与推理强度建议

| 任务 | 建议角色/模型 | 推理 | 升级条件 | 验证门禁 |
| --- | --- | --- | --- | --- |
| 只读状态、文档和常规测试 | Luna | 低/中 | 证据冲突或涉及真实写入 | 输出范围受控、无正文/凭据 |
| Django/Wagtail 局部实现 | Terra | 中/高 | 发布并发、权限或迁移 | 定向测试、check、迁移检查 |
| ES rebuild/alias 和跨库一致性 | Sol | 高/xhigh | 默认高风险 | 快照、strict consistency、回切演练 |
| 生产数据、清理和恢复 | Sol | 高/xhigh | 始终需要独立授权 | 备份、manifest、逐批验收、回滚 |
| UI/后台验收 | Terra 或 Luna | 中 | 复杂交互/可访问性问题 | BrowserSkill/Playwright 证据和日志 |

本轮文档压缩实际使用：主 agent 负责整合；Terra 高推理只读核对代码契约；Sol 高推理复核 P0/P1/P2、权限与回滚门禁；Luna 历史整理代理因服务 503 未完成，主 agent 使用本地标题索引和实施证据完成替代。MinerU 当前无已配置 collection，因此没有用其文档查询结果，未重复安装或扩大工具范围。

模型选择不能替代测试、备份、生产授权或回滚演练，也不得把源码、凭据、生产日志、Mongo 正文或个人数据发送给外部模型。

## 14. 本次文档压缩实施记录

日期：2026-08-30。

- 目标：把原约 1965 行、218 KB、包含重复编号和逐轮修改过程的方案，整理为当前架构、实施证据和后续计划的唯一维护版本。
- 实际修改：仅重写本说明书；未修改 `AGENTS.md`、运行时代码、迁移、测试、配置或 `systemctl.md`。
- 删除的内容：重复的早期提议、已经被实现取代的“尚未实现”描述、每个小批次反复出现的模型分工、测试和回滚模板、互相冲突的旧统计及重复章节编号。
- 保留的内容：数据权威、不变量、Wagtail 8.0 生命周期、迁移范围、生产备份与 v005 证据、存量登记结果、当前未提交状态、P0/P1/P2 和回滚边界。
- 数据/服务影响：无。未访问或修改 MySQL、Mongo、Redis、Elasticsearch、MinIO、Revision、正文、Outbox、alias 或 systemd 服务。
- 回滚点：Git 中恢复本文件重写前版本；不需要数据或服务回滚。
- 残余风险：工作区已有搜索修复、ScopeJob、管理命令和文档仍未形成新的发布提交；测试 v005 收敛和 alias 切换已完成。ScopeJob 已有消费者/租约骨架，但仍需在真实权限父子树场景持续验收；生产 readiness、联合恢复演练和千万级压测仍未完成。

## 15. 测试 ES v005 受控恢复停止记录

日期：2026-08-30。

- 授权目标：排空 99 条 Delivery，恢复 checkpoint 到 632，完成两次 catch-up 和严格一致性，全部通过后切换测试 alias 并验收；任一错误立即停止，不操作生产。
- 前置代码修复：排空命令 fail-fast 和 manifest 漂移保护；新版正文忽略旧 `mongo_content_id`；非公开 upsert 明确 retry；alias fresh gate 和旧物理索引原子回切。修改范围仅在 Search 服务、管理命令及测试，无迁移、配置或 unit 变化。
- 前置验证：Django 5.2.8、Wagtail 8.0、Blog 迁移到 0034、Search 到 0007；相关 66 项测试通过，`compileall`、`manage.py check`、`makemigrations --check --dry-run`、`git diff --check` 通过。测试库无 `ContentSearchScopeJob`。
- Delivery 结果：原 99 条按 20、20、20、20、19 五批处理完成；79 条 `succeeded`、21 条 `superseded`，当时 pending/processing/retry/dead 均为 0。
- 停止点：首次 `search_rebuild_content_index --resume-build --confirm` 返回永久错误 `content_search_state_hash_mismatch`；checkpoint 保持 530/632，Build failed 从 162 累计到 218，catch-up streak 为 0。命令物化了 76 条新 pending Delivery。
- 根因证据：只读重算 531–632 范围内 56 个公开页面，Mongo 正文缺失 0；page 604、608、610–620 共 13 条 State 的 `content_hash` 为空，而按正式正文计算的 hash 有效。rebuild 在写 ES 前拒绝空 State hash，因此未推进 checkpoint。
- 当前一致性：State 165、v005 索引文档 104；`missing=62`、`stale=24`、`extra=1`，其余 ahead/hash/body version/generation/wrong tombstone 均为 0。extra 为既有不可搜索 tombstone 990001。
- 未执行：未处理新产生的 76 条 Delivery，未重试 rebuild，未执行两次 catch-up，未切换 alias，未做 BrowserSkill 搜索验收。测试 read alias 仍唯一指向 `wagtailblog-test-content-replica0-v001`。
- 数据/服务影响：只修改测试 Search Delivery/Outbox/Build 状态和 v005 投影；未修改 BlogPage、Revision、Mongo 正文、生产数据、生产 ES、systemd 或服务进程。`systemctl.md` 无需更新。
- 回滚与下一门禁：保留所有 succeeded/superseded/pending Delivery、Build checkpoint 和 ES 文档作为审计现场，不反向改状态。下一批先重新 dry-run 新的 76 条 Delivery；若其能由消费者安全补齐 13 条空 hash，则分批排空后重新只读验证 State hash，再单次恢复 rebuild。任何 retry/dead 或 hash 仍为空时继续停止。
## 16. 测试 v005 收尾记录（2026-08-30）

- 环境：WSL2 `wagtailblog-test`，`WAGTAILBLOG_ENV=test`；仅操作测试 MySQL、MongoDB 和 Elasticsearch，未操作生产。
- Delivery：76 条新建 Delivery 按 20、20、16 三批执行，全部 `succeeded`；随后 catch-up 物化的 3 条 Delivery 全部 `superseded`。最终 `pending=0`、`processing=0`、`retry=0`、`dead=0`。
- Rebuild：从 checkpoint 530 恢复到 632，Build 进入 `ready`；13 条现代正文 State 的缺失 hash 通过 guarded 回填补齐，没有修改 Mongo 正文或旧 `mongo_content_id`。
- Catch-up：连续两次检查均无未收敛 Delivery 和公开文档差异，`catch_up_clean_streak=2`。
- Tombstone：为 7 个已删除页面的最新 tombstone Outbox 事件补建 Delivery 并成功写入 v005；清理 1 个无 State 的测试 ES 孤儿 tombstone `page_id=990001`，未触碰 MySQL/Mongo。
- 严格一致性：最终 `missing=0`、`stale=0`、`ahead=0`、`hash_mismatch=0`、`body_version_mismatch=0`、`generation_mismatch=0`、`wrong_tombstone=0`、`extra=0`。
- Alias：通过 fresh gate 后原子切换 `wagtailblog-test-content-read` 到 `wagtailblog-test-content-v005`；旧物理索引 `wagtailblog-test-content-replica0-v001` 保留为回滚点。
- BrowserSkill：访问 `/zh-hans/search/?query=初识Django&type=blog` 返回 1 条“初识Django”及正文摘要/详情链接，资源请求为 200，无应用 JS 异常；session 和临时 runserver 均已停止。
- 测试库仍提示 1 个历史 `wagtailcore` 未应用迁移；本批未擅自迁移，需独立维护批次处理。

## 17. 面向未来数据的实施路线

本路线不要求先修复全部历史文章，按风险和规模逐步启用：

| 阶段 | 目标 | 必须完成的门禁 |
| --- | --- | --- |
| F1 新写入统一 | 新建、编辑、Workflow、定时发布均写 Mongo body_version + MySQL State/Outbox | 发布集成测试、取消发布/删除 tombstone 测试、失败重试测试 |
| F2 搜索稳定运行 | v005 read alias 服务未来文章，Delivery/对账持续运行 | strict consistency、dead=0、alias 唯一指向、搜索抽样验收 |
| F3 权限与运维 | ScopeJob 父子树收敛、readiness、监控和恢复演练 | 权限收紧/恢复测试、服务启动依赖检查、RPO/RTO 记录 |
| F4 百万级压测 | 用脱敏合成数据验证吞吐、延迟和容量 | 1 万/10 万/100 万分阶段压测；未达 SLO 不进入下一阶段 |
| F5 Article 演进（触发式） | 仅当 BlogPage 不满足 SLO 或产品需要海量采集时拆分独立 Article catalog | 独立权限/生命周期设计、bulk 导入、双写/回滚和数据迁移方案单独审批 |

非目标：本路线不删除旧字段、不强制补造历史 Revision、不把历史错误对账归零、不在未压测前预设分片数量，也不直接对生产数据执行回填或清理。

## 18. 未来数据链路实现批次记录（2026-08-30）

- F1 生命周期门禁：补强 live BlogPage 直接删除路径。删除仍 live 页面时先递增 `publication_generation`，再写 tombstone，防止迟到 upsert 复活；新增集成测试验证 State 与最新 tombstone generation。
- F3 ScopeJob：接入 maintenance task/Beat 调度，补齐稳定主键分页、租约回收、连续失败计数、成功批次清零、rescan 和父子树定向测试。连续失败达到上限才进入 dead，不因大子树批次数提前死信。
- F2 搜索门禁：测试 v005 已完成 Delivery 清零、rebuild checkpoint 632、两次 clean catch-up、strict consistency 全部归零和 read alias 原子切换；本批未改生产索引。
- 验证：Blog/Workflow/搜索定向测试共 87 项通过；`manage.py check`、`makemigrations --check --dry-run`、`compileall`、`git diff --check` 通过。MySQL 条件唯一约束 W036 为既有能力提示。
- 数据/服务影响：仅修改代码、测试和方案文档；未回填或删除历史正文，未修改生产数据库、Mongo、ES、alias 或 systemd 服务。当前修改尚未形成发布 commit。
- 剩余门禁：真实父子页面权限场景验收、生产 readiness/联合恢复演练、百万级脱敏压测，以及正式 commit/push/生产发布仍需独立批次和授权。

## 19. 测试环境 500 篇合成索引压测（2026-08-30）

- 范围：仅测试 Elasticsearch，使用临时 `wagtailblog-test-bench-v005-20260830` 物理索引；500 条完全脱敏的公开 upsert 文档，不写入 BlogPage、BlogPublicationState、Outbox 或 Mongo。
- 结果：bulk 成功 500/500，耗时约 0.0309 秒；全文查询命中 500 条，耗时约 0.0037 秒；按 `page_id` 排序分页返回 100 条，耗时约 0.0031 秒。
- 版本围栏：对首条文档提交旧 `content_version=0`，被 ES 外部版本机制判定为 superseded=1，未覆盖新版本。
- 清理：压测结束后已删除临时物理索引，未改变测试 v005 serving alias。
- 解释：该结果只代表当前单节点测试 ES、短正文和单批 bulk 的基线，不等价于百万篇容量承诺；后续扩容仍须按 1 万、10 万、100 万脱敏数据分阶段压测。

## 20A. 生产页面 1194 审计与本批次修复（2026-08-30）

生产只读审计页面 1194（“习近平：提高防灾减灾救灾能力”）确认新版链路完整：MySQL `body=[]`，`BlogPublicationState` 有效，Mongo 不可变正文版本、Wagtail Revision、v005 Delivery 与搜索身份一致。旧 `mongo_content_id` 仍存在，是兼容字段，不作为新版正文必填条件。

审计发现两项根因并完成代码修复：

1. `BlogPage.save()` 对新页面仍调用旧 `blog_content` 写入，导致新建页面重新产生 legacy 文档。现在仅对“既有页面且已有旧指针、尚未登记新版 State”的兼容场景保留更新；新页面和新版页面不再创建或回填旧集合。
2. ES v003 作为可选旧 serving target 时仍接收 Delivery，并可能因自身 4xx 使 Outbox 变 dead。增量投递和 Outbox 收敛现在只考虑 enabled 的 building，或 required serving；alias 切换命令会在事务中退役同连接上其他 serving target。

实际修改文件：`wagtailblog3/apps/blog/models.py`、`wagtailblog3/apps/search/services/delivery.py`、`wagtailblog3/apps/search/management/commands/search_switch_production_content_alias.py`，以及对应三个搜索/生命周期测试文件；未修改迁移、配置、systemd、数据库、Mongo 正文、ES alias 或生产数据。

验证结果：定向生命周期、Delivery、正文索引测试 58 项通过；`compileall`、`python manage.py check`、`makemigrations --check --dry-run`（No changes detected）和 `git diff --check` 均通过。测试环境已知 MySQL W036 条件唯一约束提示仍存在，非本批次回归。生产历史事件 1125/v003 dead 未自动重放或清理。

事务与回滚边界：alias API 切换发生在 ES 后，随后 MySQL target 退役事务失败时可能出现 ES 已切换而数据库未同步，需通过重试/回滚命令处理；本批次未执行 alias 切换。代码回滚只需恢复上一个已验证 commit，保留 State、Outbox、Delivery、Mongo 版本和 Build 审计证据；任何生产提交、推送、服务重启或 Target 行修正均需独立授权。

独立复核列出的后续风险按优先级登记：P1 为 alias 切换后的跨 ES/MySQL 补偿、共享 connection 时的 target 命名空间限定，以及 State 查询异常时 legacy 兼容写入的 fail-closed 策略；P2 为双投递诊断探针改用 active target 谓词、旧 Build 状态同步和历史 dead Outbox 的只读报告。它们不影响本批次已验证的新写入与 v005 投递修复，须单独设计、测试和授权。

## 20B. 生命周期日志诊断批次（2026-08-30）

为定位下一篇新建文章是否严格走新版链路，本批次在现有 `blog`、`mongo`、`search` 日志域补充结构化 key=value 事件，不记录标题、intro、正文、完整异常、凭据或请求体。关联字段约定如下：`page_id`、`revision_id`、`body_version_id`、`publication_generation`、`outbox_pk`、`event_uuid`、`delivery_id`、`target_id`、`operation`、`status`、`error_code`、`elapsed_ms`。

覆盖阶段：`blog_lifecycle_save_start/complete`、`blog_body_revision_start/done`、Revision 恢复 start/complete、发布/取消发布/删除 start/complete；`blog_publication_candidate_validated/state_promoted/generation_advanced/pointer_cleared`；Search signal、Outbox 创建、Delivery 成功/重试/死信。日志写入仍走 `observability.registry` 的既有 domain 文件，后台可用 `view_logs --module blog|search|mongo` 查询。

验收方法：新建页面后按 `page_id` 查询 blog activity、mongo activity、search activity/error，确认保存顺序为 immutable body version → Revision pointer → State/Outbox → Delivery → ES；删除须出现 generation advance → tombstone Outbox → Delivery 完成。若出现 `blog_body_save_mongo_done`（新页面）或 `event_uuid` 与 `outbox_pk` 混淆，应立即停留在诊断，不执行补偿写入。

本批次仅增加日志和测试所需代码，未执行真实页面保存、发布、删除或生产服务重启。已知后续缺口：scope job、Mongo cleanup 成功路径、ES 写入前后仍需进一步日志；QuerySet.delete 可能绕过 BlogPage.delete，属于独立 P1 设计，不在本批次扩大范围。

## 20. 生产代码同步记录（2026-08-30）

- 提交：`eceed3f4f57aecd8acfbd759f2c4add16f702233`，已推送 `origin/main` 并 fast-forward 同步生产。
- 备份：创建 `/home/source/Django/wagtail/backups/wagtailblog3-pre-future-lifecycle-20260830-203507/`，包含 MySQL dump、Mongo 三集合、环境/unit 清单、ES health/alias 和 SHA-256 校验。
- 迁移：生产应用 `blog.0034`、`search.0008`；未执行正文回填、历史 Revision 修复、ES 重建或数据清理。
- 服务：`wagtailblog3`、maintenance Worker、Beat、Filebeat 均 active/enabled；Django `check` 通过；生产工作树干净。
- 搜索：v005 Target enabled，1100 条 Delivery succeeded、无 pending/processing/retry/dead；read alias 唯一指向 `wagtailblog-prod-content-v005`。
- 严格对账：发现 `missing=4`（page 1179、1187、1188、1192），其余 stale/ahead/hash/body version/generation/wrong tombstone 均为 0；按历史兼容策略不处理，需未来独立历史数据核对批次确认。
- ES 集群为单节点 yellow（无主分片缺失，14 个副本未分配），属于既有容量状态；未擅自调整副本数或删除索引。
## 21. 生产新页面与测试回归复核（2026-08-31）

### 21.1 生产页面 1194 的事实

生产只读核对页面“习近平：提高防灾减灾救灾能力”（ID 1194）得到：

- MySQL `blog_blogpage.body=[]`，`mongo_content_id` 仍是兼容字段；
- `BlogPublicationState` 的 draft/published 指针、SHA-256、schema=1、generation=1 完整；
- Mongo `content_body_versions` 有正式不可变版本，`blog_page_revision_bodies` 有对应历史快照，旧 `blog_content` 没有该页面文档；
- Wagtail Revision 的 `mongo_body_version_id`、哈希和 schema 与 State/Mongo 一致；
- ES v005 的公开投影可由 State 指针读取，搜索正常。

因此生产“只看到 Mongo 正文集合增加”是新链路的正常表现：新页面正文写入 `content_body_versions`，旧 `blog_content` 不再是必写表；`mongo_content_id` 可空不影响正文、编辑或搜索。生产当前磁盘仍为旧兼容版 `acea8998`，不能把生产运行结果当作测试工作区未提交代码的等价证明。

### 21.2 Wagtail 8.0 编辑发布缺口与局部修复

Wagtail 8.0 的新建发布会调用 `Revision.publish()`，而编辑页发布调用 `PublishPageRevisionAction.execute()`，后者直接对 `revision.as_object()` 执行 `object.save()`，绕过 `BlogPage.publish()`。这解释了测试页面 634 已有 Mongo 正文版本但没有 State、前台正文为空的现象。

本批次增加 `BlogPublicationService.ensure_published_revision()`，并在 `page_published` 搜索信号收到确切 `revision` 时调用。该方法先锁定并校验 Revision 对应的 Mongo 正文版本，只有 State 指针缺失或版本变化时才切换正式指针，已有相同三元组时保持幂等，不重复增加 generation。随后再创建 Outbox；直接 `Revision.publish()` 的原有路径保持不变。

### 21.3 测试环境“很多错误”的分类

- 将 `wagtailblog3.apps.blog` 当作 Django 测试 label 会造成同一模型被第二个模块名加载，触发 `MongoCleanupIntent` 缺少 `app_label`；正确入口是 `manage.py test blog search`，该问题不是业务回归。
- 搜索 rebuild 夹具仍假设新页面存在旧 `blog_content`，在现代页面 `mongo_content_id=NULL` 时误报 `mongo_formal_content_unavailable`；夹具已改为读取 `content_body_versions`，未放宽生产严格校验。
- MySQL 条件唯一约束 `WorkflowState` 的 W036、模拟 Mongo 缺失、ES 429、broker 不可用等均为既有测试警告/故障注入，不能作为本批次失败依据。

### 21.4 本批次实施与验证

- 修改：`blog/services/publication.py`、`search/signals.py`、`blog/test_publication_consistency.py`、`search/tests/test_search_rebuild.py`；其余工作区改动属于前序搜索/生命周期批次。
- 回归覆盖：直接调用 Wagtail 8.0 `PublishPageRevisionAction` 后，页面 `live/live_revision`、State 正式指针和正文哈希一致。
- WSL2 `wagtailblog-test`：`manage.py test blog search --noinput` 共 514 项全部通过；`compileall` 通过；`git diff --check` 通过。
- 未执行：生产代码同步、生产迁移、Mongo/ES/Outbox 写入、服务重启；生产 1194 未修改。

### 21.5 后续门禁与残余风险

1. 提交/发布前仍需在独立批次完成完整 diff 审查，确认前序未提交文件没有互相覆盖，并重新运行迁移检查。
2. `page_published` 兼容层覆盖 Wagtail 8.0 编辑发布，但请求级事务是否由生产部署启用仍需按实际 settings 验证；若未启用，页面保存与信号补齐之间存在短暂可见窗口，必须通过监控和重试收敛。
3. 生产 `blog_content` 只保留兼容历史，不得因本次观察直接删除；删除需另行备份、对账和授权。

## 22. 测试环境新增内容无法搜索修复计划（2026-08-31）

### 22.1 现状证据与目标

- 测试页面 `635`“第三个”已发布，正式正文版本、`BlogPublicationState` 和 `ContentSearchState` 完整，但 Outbox 事件保持 `pending` 且没有 Delivery；read alias 的 v005 物理索引没有该文档。
- 测试环境只运行开发网站和 Filebeat，未运行 `maintenance` Worker/Beat；同时当前 alias 指向的 v005 Target 是 `serving + enabled + required=False`，不符合增量投递的 active Target 规则。
- 目标：测试环境新增、编辑发布和删除 BlogPage 都通过隔离队列写入 v005；前台 `blog`、`all` 搜索只显示公开文章，删除后的页面不再保留公开 ES 文档。
- 非目标：不修改生产正文、历史页面、生产 alias、索引 mapping 或数据库 schema；不把前台查询回退为 MySQL 扫描来掩盖异步投递故障。

### 22.2 实施与测试步骤

1. 运行代码将搜索任务投递改为读取 `CELERY_MAINTENANCE_QUEUE`，测试进程统一使用 `markdown-test-maintenance` 和独立 Redis DB；为队列选择增加回归测试。
2. 测试 alias 切换命令将当前索引标记为唯一 `required serving`，并退役其他 serving Target；为状态变更增加断言。
3. 使用 transient systemd 启动测试网站、maintenance Worker 和 Beat；修复测试 v005 Target 的既有错误标记后，让现有 Outbox 通过正常 Delivery 收敛。
4. BrowserSkill 在测试站完整验证新建草稿、编辑、发布、搜索、删除；日志、Outbox、Delivery、State 与 ES 的核验只输出页面 ID、版本、状态和错误码，不输出正文。
5. 由 Sol 审查变更，随后执行定向测试、`manage.py check`、迁移检查、`compileall`、`git diff --check`。全部通过后才提交、推送和按生产 runbook 同步；生产数据库、Mongo 和 ES 仅在已说明的部署步骤中写入。

### 22.3 数据、服务与回滚

- 测试写入：Target 标记、现有/验收页的 Outbox、Delivery、测试 ES 文档和 tombstone；验收临时页面会通过 Wagtail 删除链路生成 tombstone，不直接删除 ES 文档。
- 服务：测试 Django、maintenance Worker、Beat；`systemctl.md` 同步记录统一队列环境变量。生产服务在本测试阶段不重启。
- 回滚：停止测试 Beat、Worker、网站，保留 Outbox/Delivery/Target 审计记录；测试 alias 回切到变更前物理索引。代码发布失败时回退到发布前 commit，绝不反向删除 Mongo 正文或 Revision。

### 22.4 模型/推理强度建议与实际使用

- 只读证据与日志：Terra 中推理；队列、alias、跨服务投递和生产发布复核：Sol 高推理；浏览器流程：Terra 高推理。
- 升级条件：Delivery 出现 retry/dead、alias 多指向、生产工作树不干净、服务健康异常或浏览器流程与 State 不一致时停止并由 Sol 复核。
- 验证门禁：测试页面的 Outbox/Delivery 收敛、v005 命中、删除 tombstone、日志链路、全套测试与 Sol 审查均通过。

### 22.5 新建页面搜索与删除闭环验收（2026-08-31）

- 实际修复：测试 v005 Target 必须是唯一 `enabled + required + serving`；增量 Delivery 只投递到 enabled building 或 required serving Target。测试 Worker、Beat 与 Django 进程统一使用 `CELERY_MAINTENANCE_QUEUE=markdown-test-maintenance` 和隔离 Redis DB，避免页面进程、Worker 与 Beat 投递到不同队列。
- 回滚门禁：`search_rollback_content_alias` 不再允许把已退役 Target 直接重新公开。回滚候选必须为 enabled building Target，具有 READY Build 且 build gate 为 clean；否则在 ES alias 写入前拒绝。这样旧索引不会因退役期间的更新、取消发布或 tombstone 漏投递而重新暴露过期文档。
- 浏览器验收：在测试站通过 Wagtail 创建临时 BlogPage `636`，保存草稿两次、发布、访问前台详情，并分别验证 `type=blog` 与 `type=all` 搜索均唯一命中。随后通过 Wagtail 标准删除页删除该页面；前台 `type=blog` 搜索返回零结果。
- 数据证据：发布后 `BlogPublicationState`、`ContentSearchState`、Outbox 和 Delivery 均为版本 1 的成功 upsert，Mongo 正式不可变版本和 Revision 快照均存在。删除后 Page 不存在，State 为版本 2 tombstone 且 `searchable=false`，Outbox/Delivery 均 succeeded；ES v005 仅保留 `searchable=false` 的 tombstone，不含可搜索孤儿文档。
- 日志证据：`blog`、`mongo` 与 `search` 日志按 page_id 记录了不可变正文写入、Revision 恢复、发布 State 提升、Outbox upsert、发布信号和删除 tombstone；日志未输出标题、intro 或正文。测试环境周期性对账中出现的历史 Revision 差异属于既有测试数据污染，不阻塞本次新页面闭环。
- Sol 审查：先发现并阻断了“退役索引回滚可能复活已删除文档”的 P0；补齐重建/追平门禁、retired Target 拒绝测试与 non-clean gate 拒绝测试后复审通过。未发现可阻断提交的 P1/P2。
- 验证：`search.tests.test_search_wp4c` 19 项通过；`blog.test_markdown_import_tasks + search.tests.test_search_wp4c` 24 项通过；`manage.py test blog search --noinput --keepdb` 519 项通过；`manage.py check` 通过；`makemigrations --check --dry-run` 为 No changes detected；`compileall wagtailblog3` 与 `git diff --check` 通过。MySQL 对 Wagtail WorkflowState 条件唯一约束的 W036 是既有能力提示。
- 数据/服务影响：本轮仅写入并删除测试临时 Page 及其测试 Mongo/Outbox/Delivery/ES tombstone；未写入生产 MySQL、Mongo、Redis、Elasticsearch alias 或生产服务配置。生产部署不需要迁移、历史回填或 alias 切换。
- 回滚：代码回滚点为本次提交前的 `main`。若生产部署后出现异常，先停止受影响的 maintenance 消费，再回退代码并保留 State、Outbox、Delivery、Build 与 Mongo 版本证据；不删除正文、Revision 或 tombstone。

### 22.6 生产 v005 required 状态修复与搜索验收（2026-08-31）

- 背景：生产 `prod-content-v005` 已是 serving 且 enabled，但历史状态 `required=false`，与新版增量投递的 required-serving 门禁不一致；v003 虽 enabled 但非 required，不作为活动投递目标。
- 变更：在生产备份完成后，仅通过事务锁定并更新 `ContentSearchTarget(target_id=prod-content-v005, connection_name=content_production).required` 为 `true`。未修改 ES 索引、read alias、正文、Mongo、Revision、Outbox 或 Delivery。
- 备份：备份目录 `/home/source/Django/wagtail/backups/required-fix-20260831/`，文件 `content_search_target.dumpdata`，SHA-256 `e436fa9a37838e73c06aaa12898dcd87de04733347dd071f70c6655fd641412f`。
- 核验：`search_sync_status` 显示 v005 为 `enabled=true, required=true, role=serving`，且不存在第二个同时满足三条件的 Target；v005 Delivery `succeeded=1101`，pending/processing/retry/dead 均为 0。Django `check` 无问题，四个应用服务均 active，生产搜索 API 查询 Django 返回 HTTP 200 和结果。
- 服务影响：此前已按代码发布重启 Django/uWSGI、maintenance Worker、Beat；本次单字段状态修复无需再次重启，未重启基础设施、Nginx、Filebeat、Elasticsearch、MySQL、MongoDB 或 Redis。
- 回滚边界：如需回滚，仅恢复备份中的 `required=false` 或回退代码至上一已验证 commit；不得删除正文、ES 文档、alias、State、Outbox 或 Delivery。恢复后必须重新执行唯一性、服务、Django check 与搜索验收。
