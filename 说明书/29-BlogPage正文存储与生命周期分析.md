# BlogPage 正文存储与生命周期分析

> **统一执行版（架构师 / 数据库工程师 / Django-Wagtail 工程师）**
>
> 本文不是生产变更授权，而是把当前代码证据、测试数据证据和千万级演进目标整理成一份可分批验收的改造基线。任何迁移、回填、索引重建、快照回收、发布或服务重启，都必须在对应工作包单独取得授权。

## 0. 总体决策与执行顺序

### 0.1 一句话架构

采用“**MySQL/Wagtail 目录与事务状态 + Mongo 不可变正文版本 + Elasticsearch 已发布搜索投影 + MySQL Outbox 异步投递**”四层架构：MySQL 决定文章是否公开以及当前正文版本，Mongo 保存正文和历史快照，Elasticsearch 只保存可重建的公开搜索文档，Outbox 负责跨库副作用的至少一次投递和补偿。

当前 `blog_content` 和 `blog_page_revision_bodies` 保留为兼容层；目标不是用 Mongo 取代 Wagtail，而是让 Wagtail 的 Revision、权限和发布流程继续存在，同时停止正文的原地覆盖和跨库无门槛双写。

### 0.2 必须先锁定的业务边界

| 决策项 | 本方案默认选择 | 不能含糊的原因 |
| --- | --- | --- |
| `blog_content` 语义 | 仅表示已发布正文；草稿和历史正文不进入其中 | 否则前台、搜索和后台预览会混淆公开内容与工作副本 |
| MySQL `intro` | 继续作为元数据权威字段 | 列表、SEO、RSS、Wagtail 页面搜索和权限过滤都需要低延迟读取 |
| MySQL `body` | 保留字段和 StreamField 契约，持久值继续压缩为空值表示 | Wagtail 表单、校验、Revision 反序列化和比较依赖该字段；删列不是简单去重 |
| Mongo `title`/`intro` | 过渡期保留镜像，禁止立即清理 | 先完成读取方审计、双读观察和可恢复迁移，再收缩双写面 |
| Wagtail Page 范围 | 精选/人工运营内容继续使用；海量导入文章进入独立 `Article` 目录 | 千万级页面树、权限、Revision 和后台浏览不应未经压测全部承载 |
| 分布式事务 | 不引入 MySQL-Mongo 两阶段提交 | 以 MySQL 事务提交为公开可见性门槛，用 Outbox 和对账处理跨库失败 |

### 0.3 数据不变量（所有实现和测试的共同契约）

1. 公开页面必须同时满足：MySQL 页面为公开状态、`published_body_version_id` 非空、Mongo 版本存在且哈希校验通过。
2. Mongo 正文版本不可变；编辑、恢复和再发布都生成或引用版本，不原地覆盖已发布正文。
3. Wagtail Revision 不可变；恢复旧版本只能生成新 Revision，不能改写旧 Revision 的 JSON 或快照。
4. Revision 指针必须按原始 `_id` 类型读取，同时兼容历史 ObjectId 和 `rev_<page>_<uuid>` 字符串；非法、缺失、超时、空正文和反序列化失败必须是不同状态。
5. 正文读取失败不得静默回退为正常空正文，也不得把历史预览静默替换成当前正式正文。
6. 公开搜索只接受已发布版本；草稿、预览和历史版本永远不能进入公开索引。
7. 每个公开变更在同一个 MySQL 事务内更新页面状态、正文版本指针、`publication_generation` 和 Outbox 事件。
8. 搜索、缓存和媒体清理由提交后的消费者执行，并以内容身份、版本和 generation 做幂等围栏；旧事件不能覆盖新版本。
9. 删除先写 MySQL 墓碑和 Outbox，再在提交后延迟清理 Mongo；只要仍有 Revision、正式指针、备份或审计引用，就不得物理回收。
10. 所有重建任务必须可暂停、可重试、可对账和可回滚；Elasticsearch 是投影，不是正文或元数据权威库。

### 0.4 四个专业视角的分工

| 视角 | 负责的设计问题 | 交付门禁 |
| --- | --- | --- |
| 架构 | 权威来源、跨库边界、版本代际、Article 与 BlogPage 分层 | ADR、状态机、故障矩阵、回滚设计 |
| 数据库 | 表/集合/索引、唯一性、引用、容量、备份和对账 | DDL 评审、执行计划、备份恢复演练、数据抽样 |
| Django/Wagtail | `serializable_data`、`from_serializable_data`、表单、预览/比较/恢复、保存删除钩子 | 单元/集成测试、Wagtail 8.0 后台验收、类型化错误呈现 |
| 搜索与运维 | Outbox、Delivery、ES alias、重建、监控、服务顺序 | 版本围栏测试、重放演练、SLO、systemd/日志/告警检查 |

### 0.5 推荐执行顺序

```text
M0 只读基线与备份门禁
  -> M1 Revision 读取兼容与错误分类（先修 P0，不改数据）
  -> M2 Mongo 正文版本双写和引用登记
  -> M3 Wagtail 发布/恢复与 MySQL Outbox 原子编排
  -> M4 搜索投影、generation 围栏和在线重建
  -> M5 Article 目录与批量导入通道
  -> M6 对账、GC、旧字段和旧集合收缩
```

每个阶段都必须有“旧路径可读、失败可重试、指标可观察、上一阶段可回退”的验收结果；未通过上一阶段，不得进入下一阶段。

### 0.6 风险优先级总表

| 优先级 | 当前问题 | 目标动作 | 允许的回滚边界 |
| --- | --- | --- | --- |
| P0 | 字符串 Revision 指针被 ObjectId-only 读取器拒绝；历史预览静默回退正式正文 | 先实现类型安全读取和显式错误状态，再开放恢复/比较 | 只回滚读取适配器；不删除旧快照 |
| P0 | `pre_delete` 在 MySQL 提交前删除 Mongo，且与 `BlogPage.delete()` 重复 | 信号只记录事务内意图，提交后消费者清理 | 保留 Mongo 正文和墓碑，停止消费者即可 |
| P1 | Mongo 先写、MySQL 后写，失败后产生孤儿或正文漂移 | 不可变版本 + MySQL 指针 + Outbox + 对账 | 双写期间保留旧字段和旧读路径 |
| P1 | 正文快照被多个 Revision 共享，物理删除会破坏历史 | 引用登记/审计 + 延迟 GC | GC 前可停止回收，不做在线删除 |
| P1 | 恢复旧 Revision 或迟到搜索事件覆盖新版本 | `publication_generation`、`body_version_id`、哈希围栏 | ES 使用旧 alias/索引回退，MySQL 指针不回退数据 |
| P2 | Mongo 元数据镜像、旧 MySQL 正文和 Revision 没有收缩策略 | 完成读取方审计、备份和观察期后再收缩 | 只删除兼容代码，不直接删数据 |
| P2 | 千万篇全部作为 Wagtail Page 的后台和树性能风险 | 独立 Article 目录，按压测结果决定分片 | Article 通道与 BlogPage 通道可独立停用 |

### 0.7 本文阅读和实施映射

- 第 1-4 节：当前事实、字段权威、生命周期和基础风险。
- 第 5-21 节：MySQL、Mongo、ES、Outbox、容量、备份、工作包和测试门禁。
- 第 22-34 节：`apps/search` 专项兼容和千万级搜索演进。
- 第 35-37 节：Wagtail 8.0 历史、真实测试数据和浏览器验收证据。
- 本节 0：跨章节统一决策；若专项章节与本节冲突，以最新实测证据和本节不变量为准，并在实施记录中登记变更。

### 0.8 整个项目的唯一主线与子代理分工

后续不再把 M0-M6、WP0-WP8、S0-S5 当成三套独立计划。**M 主线是唯一依赖顺序**；WP 和 S 只是交付包映射。每一批必须有唯一 owner、明确前置条件、可验收产物、自动化测试、回滚开关和“是否需要生产授权”标记。

| 主线 | 风险/目标 | 唯一 owner | 协作角色与文件边界 | 前置条件 | 交付物与验收门禁 | 回滚开关 | 生产授权 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| M0 基线与容量 | 只读盘点，建立事实和容量基线 | `arch` | `data` 查 MySQL/Mongo/ES；`ops` 查备份、服务和监控；`qa` 固化测试数据 | 无 | 指针类型/孤儿/共享引用/Outbox/ES alias 报表；正文只输出长度、字节和 hash；备份恢复点记录 | 停止报告任务，不改业务 | 否；生产只读采集另行授权 |
| M1 Revision 读取兼容 | 修复字符串/ObjectId 指针和静默回退 P0 | `backend` | `arch` 定义错误状态；`qa` 做 Wagtail 8 预览/比较/恢复；`review` 审查异常边界 | M0 报表；禁止触碰真实数据 | `mongo.py` 统一 pointer resolver；`models.py` 显式 `invalid/missing/unavailable/empty/deserialize`；Page 38/544 回归通过；缺失正文不再伪装为空或正式正文 | feature flag 回到旧只读路径；不删快照 | 否，测试环境先行 |
| M2 删除与引用保护 | 消除事务回滚丢正文和共享快照误删 P0/P1 | `backend` | `data` 设计 manifest/ref；`ops` 设计重试/租约；`review` 做事务审查 | M1 读取可定位；M0 已知共享指针 | `pre_delete` 只写 MySQL intent；提交后 Delivery/GC；共享指针删除安全；外层回滚 Mongo 不变 | 停止 GC/Delivery，保留墓碑和快照 | 测试通过后再申请生产灰度 |
| M3 正文版本化 | 建立不可变 `body_version_id`、hash、schema | `data` | `backend` repository/Revision；`arch` 版本契约；`qa` 故障注入 | M1/M2 通过；新字段和集合设计评审 | 新旧双读双写、正文不可变、Revision 引用可审计；`makemigrations --check` 和迁移演练通过 | 停止新写，继续旧字段双读；不物理删除 | 需要 DDL/回填授权 |
| M4 Wagtail 发布编排 | 发布/恢复/定时任务与 MySQL 指针、generation、Outbox 对齐 | `backend` | `arch` 状态机；`search` Outbox 消费；`qa` 并发和回滚；`review` 事务边界 | M3 版本可读；发布场景清单完成 | 首发/再发/取消发布/旧 Revision 恢复/并发发布通过；Mongo 失败或 MySQL 回滚不改变旧公开版本 | 关闭新发布编排，保留旧发布入口和 Outbox | 需要发布灰度授权 |
| M5 搜索身份与投影 | typed identity、generation 围栏、ES 在线重建 | `search` | `data` 正文批量读取；`backend` Article producer；`qa` 召回/延迟；`ops` alias/监控 | M4 稳定；先完成 S0 只读一致性 | `content_kind/namespace + aggregate_id + body_version_id + generation + hash`；旧事件拒绝；BUILDING/CATCHING_UP/SERVING 和 alias 回滚演练通过 | alias 切回旧 serving；停止消费者，保留 Outbox | 需要索引创建、回填和切 alias 授权 |
| M6 Article 海量通道 | 将千万级采集内容从 Wagtail Page 树分离 | `arch` | `backend` Article/导入；`data` 表和索引；`search` Producer；`qa` 峰值压测；`ops` 资源评估 | M5 identity 契约；百万级压测和权限/租户决策 | source_key 幂等、批量导入断点、无半成品公开、独立路由和唯一编辑入口；禁止逐篇 `add_child()` | Article feature flag 关闭，BlogPage 独立运行 | 需要新表、回填和容量资源授权 |
| M7 对账、GC 与收缩 | 在观察期后停旧写、回收无引用版本 | `data` | `backend` 删除引用；`search` ES 对账；`ops` 备份/告警；`review` 不可逆操作审查 | M1-M6 连续稳定；恢复演练通过 | 连续观察期零 P0/P1 漂移；GC 仅处理墓碑、无引用、过保留期且已备份对象；MySQL `body` 字段仍保留 | 立即停止 GC，恢复双读和旧 alias；不恢复已物理删除数据 | 每个收缩动作单独确认 |

**子代理执行协议：**

- `arch` 负责主线编排、ADR、不变量、状态机、namespace、依赖和回滚；不得代替 backend 直接修改运行时代码。
- `backend` 负责 `wagtailblog3/apps/blog/models.py`、`signals.py`、`mongo.py` 和新增正文/清理服务及测试；修改 Django/Wagtail 运行时代码前必须阅读第 22 号方案，新增中文 docstring/类型标注，并报告 `compileall`、`check`、迁移检查和测试结果。
- `data` 负责 MySQL DDL、Mongo 集合/索引、引用审计、回填和 GC 设计；默认只读，任何真实数据写入、迁移或清理都必须由主 agent 单独授权。
- `search` 负责 `apps/search` 的 State、Outbox、Delivery、document、rebuild、Mongo 批量读取和 ES alias；不得把草稿/历史正文送入公开索引。
- `qa` 负责单元、集成、故障注入、Wagtail 历史预览/比较/恢复、性能基线和浏览器验收；浏览器产物统一写入 `output/playwright/`，用户指定使用 `browser-skill` 时按其生命周期执行。
- `ops` 负责备份、readiness、systemd、日志、队列、alias 和发布观察；不自行执行生产迁移、重启、回滚或删除。
- `review` 必须独立审查并发、权限、敏感数据、迁移和回滚；发现证据不足时退回批次，不以静态检查替代数据或浏览器证据。

旧编号映射：WP0 对应 M0，WP1 对应 M2，WP2 对应 M3，WP3 对应 M4，WP4 对应 M1-M3 的兼容迁移，WP5 对应 M6，WP6 对应 M5，WP7 对应 M6-M7，WP8 对应 M7；S0-S5 作为 M5 内部交付包执行，不能提前绕过 M1-M4 的正文身份和发布契约。

## 背景与现状证据

- 用户关注 MySQL `blog_blogpage.intro`、`body` 与 MongoDB `blog_content`、`blog_page_revision_bodies` 的重复与一致性问题。
- `BlogPage` 在 `wagtailblog3/apps/blog/models.py` 中声明 `intro`、`mongo_content_id` 和 `StreamField body`。`body` 必须作为 Wagtail 模型字段存在，但项目保存逻辑的目标是使 MySQL 中的值为 `[]`。
- `BlogPage.save()` 先尝试写入 `blog_content`，再临时将 `body` 设为 `[]` 调用父类保存并恢复内存正文；`post_save` 信号和 `after_edit_page` hook 也会额外清空 MySQL `body`。
- Revision 序列化会将正文写入 `blog_page_revision_bodies`，在 MySQL Revision JSON 中仅保存 `mongo_draft_pointer` 和 `body: []`。恢复 Revision 时优先读取草稿快照，缺失时回退到正式 Mongo 正文。
- 当前 WSL2 测试环境实际为 Django 5.2.8、Wagtail 8.0。Wagtail `Page.add_child()` 会保存新节点；当前 `BlogPage.save()` 未以 `live` 或发布状态作为正式 Mongo 写入条件。
- 后台 URL `/admin/pages/3/?q=...` 是 Wagtail 页面浏览器自动补全，不搜索 Mongo 正文或 `intro`；独立前台内容搜索才从 MySQL 投影 `title`、`intro`，并从 Mongo 正文生成 `body_text`。

## 目标与非目标

目标：厘清字段权威来源、保存/发布/修改/删除顺序，并提出不丢失正文和草稿的优化方向。

非目标：本次不删除列、数据、Revision 或索引；不执行迁移、回填、重建搜索索引、发布或服务操作。

## 字段归属结论

| 数据 | 权威来源 | 是否可直接删除 | 原因 |
| --- | --- | --- | --- |
| MySQL `body` | 无正式业务正文，持久值应为 `[]` | 否 | `StreamField` 仍是 Wagtail 编辑、校验、序列化和 Revision 对象重建的模型契约。删除列不是去重，而是模型结构改造。 |
| Mongo `blog_content.body` | 已发布/正式正文 | 否 | 详情页渲染和正文搜索均读取它。 |
| Mongo `blog_page_revision_bodies.body` | 每个草稿/历史 Revision 的正文快照 | 否 | 用于后台编辑、预览与历史版本恢复。 |
| MySQL `intro` | 文章元数据 | 否 | 列表、详情页、SEO/OG、RSS、普通 Wagtail 搜索及独立内容搜索均读取 MySQL `intro`。 |
| Mongo `title`、`intro` 副本 | 当前为镜像 | 不应立即删除 | 当前主要读取方未依赖该副本，但必须先完成兼容核验和存量迁移方案，不能把“看似未读”当作可直接删数据。 |
| MySQL `mongo_content_id` | 正式 Mongo 文档指针 | 否 | 正式正文读取、搜索投影与删除清理均依赖它。 |

用户提供的旧行中 MySQL `body` 非空，而较新的行是 `[]`，与上述演进一致：前者应视为历史或绕过现有保存网关的残留，而非当前的双写设计目标。

## 当前生命周期

### 草稿和历史版本

1. Wagtail 调用 `BlogPage.serializable_data()`。
2. 当前内存 `StreamField` 正文写入 Mongo `blog_page_revision_bodies`。
3. MySQL 的 Wagtail Revision `content` 保存草稿指针，`body` 固定为 `[]`。
4. 打开编辑页、预览或历史版本时，先按指针恢复草稿；草稿不存在时回退到 `blog_content` 正式正文。

### 常规新建、保存和发布

1. 新页面经 `parent.add_child(instance=page)` 保存到 MySQL；Wagtail 8 的实现明确该方法会保存节点。
2. 当前 `BlogPage.save()` 在该保存中写 `blog_content`，即使页面尚未发布；Mongo 先得到 `page_id=None`，MySQL 获得主键后再回填 `page_id`。
3. 后续 `save_revision()` 保存草稿快照；Revision 发布时由 Wagtail 将该 Revision 应用于页面。
4. 发布后的页面详情通过 `serve()` 读取正式 Mongo 正文；独立内容搜索读取 MySQL 标题/摘要和 Mongo 正文投影。

Markdown 导入是特例：它设置 `_markdown_import_draft_only`，在 `add_child()` 阶段跳过正式 Mongo 写入，只保存草稿 Revision。这证明目前普通后台新建与 Markdown 导入的“正式内容”语义并不一致。

### 修改、取消发布和删除

- 完整 `save()` 会先写 Mongo，再写 MySQL；`update_fields` 不含 `body` 时会跳过正式 Mongo 写入。
- `unpublish()` 仅执行 Wagtail 取消发布并记录搜索墓碑；正式 Mongo 正文保留，供再次发布或恢复。
- `BlogPage.delete()` 在 MySQL 事务中记录搜索删除事件并删除页面，事务块结束后再尝试清理 Mongo 正式正文和该页全部 Revision 快照。
- 但 `blog.signals.delete_blog_content_from_mongodb` 是 `pre_delete` 信号，也会在 MySQL 删除提交前清理相同 Mongo 数据，造成重复清理和跨库回滚风险。

## 风险与优化设计

### P0：删除前清理 Mongo 可能不可恢复

`pre_delete` 里的 Mongo 删除发生在 MySQL 删除最终提交前。若之后数据库删除失败或外层事务回滚，页面仍在 MySQL，但正文和草稿已经被删除。`BlogPage.delete()` 后续的清理也与信号重复。

建议：只保留一个删除编排服务，但保留 `pre_delete` 作为单页、子树和级联删除的事件捕获器。捕获器只能在 MySQL 事务内记录删除意图/搜索墓碑，不能访问 Mongo；提交后由 outbox/Delivery 按精确 `page_id`、正文版本和哈希清理，失败必须保留可审计的重试状态。

### P1：保存跨 MySQL/Mongo 无原子事务

现有顺序是 Mongo 在前、MySQL 在后，且 Mongo 异常只记录日志后仍继续 MySQL 保存。新页面 MySQL 保存失败会留下 Mongo 孤儿；旧页面 MySQL 保存失败则可能形成“Mongo 正文新、MySQL 元数据/Revision 旧”。失效 `mongo_content_id` 再遇到同页已有 Mongo 文档时，也可能撞上 Mongo 的 `page_id` 唯一索引。

建议：把“正式正文已写入”的版本、哈希和待确认状态纳入可重试的事务外盒（outbox/补偿记录），以 MySQL 成功提交为可见性门槛。过渡期先显式处理旧 `page_id` 唯一文档的指针失效和重复键；目标态改为幂等插入不可变正文版本，不能再原地覆盖，也不能把“记录日志后继续”当成一致性方案。

### P1：正式正文与草稿正文边界不一致

普通后台新建的未发布页会提前写正式 Mongo，Markdown 导入则不会。若“正式”定义为已发布内容，则普通路径不符合定义；若定义为当前工作副本，则 `blog_content` 不能再被视为只包含公开正文，前台/搜索的读取和权限边界需重新审计。

建议：先由产品确认 `blog_content` 的语义，再统一两条创建路径。推荐语义是“仅已发布正文”，草稿只进入 Revision 集合；实施前必须补齐新建草稿、首次发布、编辑草稿、取消发布、再发布的回归测试。

### P2：元数据副本和 Revision 保留策略

Mongo 的 `title`、`intro` 是 MySQL 元数据镜像，增加双写面和漂移可能；`blog_page_revision_bodies` 当前没有可见的保留策略，会随 Revision 增长。

建议：最终把 `blog_content` 收敛为 `page_id`、`body`、`schema_version`、`content_hash`、`updated_at` 等正文必要字段，元数据只保留在 MySQL；Revision 快照保持不可变，并另行设计保留、备份和恢复策略。不得在未备份和未获授权前清理任何存量快照。

## 建议实施步骤

1. 先做只读一致性审计：统计非空 MySQL `body`、空或重复 `mongo_content_id`、Mongo 孤儿、MySQL 悬空指针，以及两端正文哈希/长度差异；不得输出正文。
2. 写测试锁定生命周期和故障补偿，特别是 MySQL 回滚、Mongo 失败、重复删除、首次草稿和首次发布。
3. 保留 `pre_delete` 作为级联删除捕获器，但其中只写 MySQL 清理意图，禁止 Mongo I/O；提交后由可重试任务清理，先灰度验证，不删除历史数据。
4. 统一正式/草稿写入语义并引入幂等版本号或哈希。
5. 仅在审计、备份、迁移方案和生产授权完成后，处理历史非空 MySQL `body` 与 Mongo 元数据副本；列删除应是最后一步，并且需验证 Wagtail StreamField 契约是否有可替代实现。

## 面向千万文章的企业演进方向

### 总体原则：每份数据只有一个权威来源

| 层 | 职责 | 不能承担的职责 |
| --- | --- | --- |
| MySQL / Wagtail | 页面树、文章目录、标题、摘要、日期、发布状态、权限、分类标签、编辑工作流、当前正文指针、事务 Outbox | 大正文、全文检索主库、跨库同步的最终确认 |
| MongoDB | 正文及其不可变版本、草稿和 Revision 正文快照 | 页面树、发布权限、列表查询和搜索排序的权威来源 |
| Elasticsearch | 已发布内容的可再生搜索投影 | 正文或元数据的权威存储、草稿存储 |
| Redis / CDN | 页面、摘要、正文片段和热点查询缓存 | 永久数据或一致性来源 |
| MinIO | 图片、附件和超大二进制资源 | Markdown/StreamField 正文的默认替代品 |

千万级规模下，MySQL `intro` 留在 MySQL 是正确的：它是高频列表和元数据查询的一部分，长度远小于正文。应停止的是把完整 `body` 落回 MySQL，而不是删除 Wagtail 的 `body` 字段定义。

### 推荐数据模型：正文不可变，MySQL 只切换指针

当前 `blog_content` 是每页一份可原地更新的文档。长期建议 BlogPage 与 Article 共用一个不可变正文契约，例如逻辑上的 `content_body_versions`：

```text
Mongo body version
  body_version_id     全局唯一且不可变
  aggregate_type      blog_page / article
  aggregate_id        MySQL 内容聚合 ID
  body_version        聚合内单调递增正文版本
  body                StreamField 原始块数据
  body_sha256         用于幂等、校验和检索版本比对
  schema_version      块结构升级门槛
  created_at
  source_revision_id  可选，连接 Wagtail Revision

MySQL BlogPage / 文章目录
  aggregate_id
  title, intro, date, 发布状态、关系、SEO 元数据
  published_body_version_id
  publication_generation
```

发布流程变为“先写入并校验一个不可变正文版本，再在 MySQL 事务中原子切换已发布指针”。若 MySQL 事务失败，Mongo 只会留下未引用版本，不会覆盖已发布内容；之后由对账任务在保留期后处理。草稿 Revision 可继续是独立不可变快照，或与正文版本共享底层存储，但前提是保留语义和权限边界完全清楚。

这比当前的“按 ObjectId 原地更新 + Mongo 先写 + MySQL 后写”更能抵抗重试、并发编辑和跨库故障。它不要求分布式事务；目标是至少一次投递加版本围栏，而非不现实的跨 MySQL/Mongo exactly-once。

### 写入、发布、删除必须走 Outbox

1. 编辑草稿：生成新的 Mongo 草稿版本，MySQL Revision 仅保存指针和版本号。
2. 发布：Mongo 正文版本成功且哈希校验通过后，在 MySQL 同一事务内更新发布指针、版本号和 Outbox 事件。
3. 异步消费者：按稳定内容 ID、`publication_generation` 和 `body_sha256` 幂等地建立 Elasticsearch 文档、刷新缓存和记录处理结果；正文版本与公开投影 generation 分离。
4. 删除：MySQL 先写删除墓碑和 Outbox；提交后消费者删除搜索投影并标记 Mongo 版本可回收。真正物理清理必须经过保留期、备份和对账。
5. 重试：每个消费者保存状态、次数、最后错误和租约；失败进入明确的 retry/dead 状态，不能静默吞掉。

这也意味着应移除运行时 `MongoManager()` 初始化时创建/删除索引的副作用。索引变更必须是独立、受控、可审计的运维操作，不能随着一次页面读取或保存触发。

### 千万级目录与 Wagtail 的边界

Wagtail `Page` 适合编辑型 CMS，但不应假定“千万条全都作为常规后台可浏览 Page”仍有良好管理体验：页面树、权限过滤、Revision、日志和后台浏览查询都会成为成本中心。推荐分层：

- 编辑型/精选内容继续使用 `BlogPage` 和完整 Wagtail 工作流。
- 海量导入文章先在同一 Django 项目内建立独立的 MySQL `Article` 目录模型，保留必要的 `wagtail_page_id` 可选关联；第一阶段不拆文章目录微服务，也不为每篇批量文章执行 `add_child()`、后台钩子和逐条 ORM `save()`。
- 前台列表、归档和 API 读取文章目录；Wagtail 负责站点页面、专题、内容编辑和运营入口。
- 若业务坚持全部文章都是 Wagtail Page，必须先完成百万级压测和容量验证，并接受后台浏览与 Revision 表的额外成本；不要直接对 Wagtail 核心 `Page` 表做未经验证的分区或分片。

这不是立即重写架构的建议，而是千万级目标下必须尽早确认的产品边界。最重要的问题是：千万篇文章是否都需要 Wagtail 的人工编辑、页面树和 Revision 能力？若答案是否定的，独立文章目录是更稳的方向。

### 存储、索引与读路径

- Mongo 正式正文由 MySQL 的稳定 `body_version_id` 定点读取，按文章列举版本则必须携带 `article_id`；索引与未来分片键必须覆盖这两条路径。Revision 至少需要唯一 Revision 指针及 `(page_id/article_id, created_at)` 索引。
- 正文单文档接近 MongoDB 16 MiB 限制时，不应继续扩展单文档；改用可分块正文版本或经设计评估后改由对象存储承载超大正文，MySQL/Mongo 保留指针和哈希。
- MySQL 列表查询使用覆盖索引、发布时间/主键游标分页和预取关系，避免深度 OFFSET、全表排序和正文查询。读副本只在复制延迟、发布可见性和回退策略明确后使用。
- Elasticsearch 只索引已发布版本的标题、摘要、正文纯文本和筛选字段；文档带稳定内容 ID、`publication_generation`、`body_version_id` 和 `body_sha256`，消费者拒绝旧 generation。索引使用版本化物理索引加 read alias，容量达到阈值时在线重建，不把 ES 当主库。
- 热点详情可缓存“元数据版本 + 正文版本”组合；发布/删除事件按版本精确失效，缓存不能绕过发布权限。

### 容量、备份和可观测性门槛

容量不能只按“1000 万篇”估算，必须先从真实样本获得：平均/95/99 分位正文 BSON 大小、每篇平均 Revision 数、附件量、索引倍数、峰值写入与查询 QPS。粗略容量模型为：

```text
Mongo 物理容量 ≈ 正文版本总大小 × 索引系数 × 副本数 × 预留空间
Elasticsearch 容量 ≈ 已发布纯文本与筛选字段大小 × 分词/倒排倍数 × 副本数 × 预留空间
MySQL 容量 ≈ Page/Article 元数据 + 关系表 + Revision 元数据 + Outbox/审计记录 + 索引
```

至少建立以下监控和只读对账：MySQL 指针缺失、Mongo 孤儿版本、页面/正文版本不匹配、Outbox 延迟和死信、Elasticsearch 版本落后、缓存命中率、单文档大小接近上限、Mongo 分片热点、MySQL 复制延迟与慢查询。备份必须覆盖 MySQL 的时间点恢复、Mongo 的快照/Oplog 恢复和 MinIO 媒体；Elasticsearch 可重建但也需要索引快照用于恢复速度。

### 分阶段路线

1. **当前到百万级前**：保持 MySQL 元数据 + Mongo 正文；先修复删除顺序、保存幂等性、Mongo 索引初始化副作用，并补齐生命周期故障测试和对账任务。
2. **百万级验证**：采用生产等比例样本压测目录查询、详情读取、发布、Revision、搜索回填和恢复演练；据测量结果决定 Mongo 副本集、ES 分片和 MySQL 读副本，不凭文章数预设分片数。
3. **千万级建设**：将海量文章目录从 Wagtail Page 工作流中明确分离，建立批量导入、版本化正文、Outbox 消费、版本化 ES alias、软删除和延迟回收。
4. **分片触发条件**：当单 Mongo 副本集的磁盘、写吞吐、工作集或恢复窗口已无法满足容量门槛时，再以与定点读取一致的稳定键实施分片；分片前必须完成全量备份、迁移演练和回滚设计。

## 企业方案 V2：可实施的详细设计

### 1. 架构决策与适用范围

本方案按“最终累计约一千万篇文章、读多写少、存在批量采集和少量人工运营”设计，不假定当前单机配置可直接承载该规模，也不按文章数量直接推导节点数或分片数。

一千万篇文章不能全部平铺为同一个 `BlogIndexPage` 的直接子页面。当前 Wagtail 8 使用 Treebeard 物化路径，`steplen=4`、36 字符 alphabet，单父节点理论直接子项上限约为 `36^4 - 1 = 1,679,615`。即使通过多层目录绕开硬上限，页面树、权限、Revision 和后台浏览成本仍然存在。因此本方案明确采用两条内容通道，而不只是把独立 Article 当作可选优化：

| 内容类型 | 存储入口 | 适用场景 |
| --- | --- | --- |
| 精选运营内容 | Wagtail `BlogPage` | 需要页面树、权限、预览、工作流、定时发布和人工运营 |
| 海量文章 | 同一 Django 项目内的独立 `Article` 模型 | 采集、同步、机器生成、批量导入、低比例人工编辑 |

第一阶段不拆微服务。独立 `Article` 与 Wagtail 共用当前 Django 项目、认证、MySQL、Mongo、ES、Redis、MinIO 和 maintenance 队列，先降低运维和跨服务契约复杂度。

两条通道不得同时编辑同一文章。Article 提升为 BlogPage 时必须记录来源映射、唯一编辑入口、唯一公开 URL、重定向和搜索去重状态。

### 2. 实施前必须取得的容量事实

| 指标 | 决策用途 |
| --- | --- |
| 正文 BSON 大小的平均、P95、P99、最大值 | Mongo 容量、网络流量、单文档上限风险 |
| 每篇正式版本数、Revision 数和保留年限 | 正文版本总量，不能只按文章数估算 |
| 日新增、峰值导入/发布速率 | 批量写、Outbox、Worker 吞吐 |
| 详情、列表、搜索 QPS 与冷热分布 | 缓存、读副本、索引策略 |
| 标签、分类、作者关系基数 | MySQL 关系表和 ES 筛选成本 |
| RPO、RTO、删除隔离期、合规保留 | 备份、恢复和物理回收 |
| BlogPage 与 Article 的预计比例 | Wagtail 容量边界和后台规模 |

测试库只读小样本只能用于量级感知，不能作为采购依据。后续采样只输出数量、字节、长度和哈希，不输出正文。

### 3. 目标逻辑架构

```text
人工运营                          批量采集/导入
   |                                   |
Wagtail BlogPage                  Article Import Job
   |                                   |
   +------------ MySQL 目录与状态 -----+
                        |
         body_version_id + publication_generation
                        |
                 Mongo 不可变正文
                        |
          +-------------+--------------+
          |                            |
     MySQL Outbox                 详情读取/缓存
          |
 maintenance Worker + Beat 补偿扫描
          |
 Elasticsearch / Redis / Feed / 延迟 GC
```

权威规则：

- MySQL 决定文章是否存在、是否公开、当前公开正文指针、路由、权限和元数据。
- Mongo 保存不可变正文版本，不决定哪个版本公开。
- Elasticsearch、Redis 和 Feed 都是可重建投影。
- Celery 只负责唤醒和执行；事件存在与完成状态必须由 MySQL 持久记录证明。

### 4. 三种版本必须分离

```text
body_version_id
    Mongo 不可变正文身份；正文相同也可以属于不同 Revision。

publication_generation / aggregate_version
    MySQL 单调递增的公开投影版本；标题、intro、标签、权限、正文、发布和删除变化都递增。

schema_version
    StreamField 块结构/序列化契约版本；用于兼容读取和内容迁移。
```

不能只用正文哈希控制 ES 和缓存。只修改 `title`、`intro`、标签或权限时，正文哈希不变，但公开投影必须更新。现有 `ContentSearchState.content_version` 已体现公开版本围栏，后续应复用该模式。

### 5. MySQL 详细数据模型

#### 5.1 Article 目录

```text
id                         BIGINT 主键
site_id, locale_id         站点和语言
source_type, source_key    来源和导入幂等键
route_key / slug           稳定路由
title, intro               权威元数据
visibility                 public/private/restricted
workflow_state             draft/in_review/approved
publication_state          never_published/published/unpublished/archived/deleting/deleted
draft_body_version_id      当前草稿正文指针，可空
published_body_version_id  当前公开正文指针，可空
published_body_sha256
body_schema_version
publication_generation     公开投影单调版本
first_published_at, last_published_at, scheduled_at
deleted_at, created_at, updated_at
wagtail_page_id            可选精选映射，不代表双重编辑权
```

建议约束：

- `(site_id, locale_id, source_type, source_key)` 唯一，保证重复导入幂等。
- `(site_id, locale_id, route_key)` 对有效记录唯一；删除后 URL 是否复用必须单独决定。
- `publication_state=published` 时必须有公开正文指针；跨库存在性由发布服务和对账保证。
- 工作流状态与公开状态分开，支持“审核新草稿但继续展示旧正式版”。

候选索引必须用真实 SQL 和 `EXPLAIN` 决定：

```text
(site_id, locale_id, publication_state, last_published_at DESC, id DESC)
(site_id, locale_id, route_key)
(source_type, source_key)
(publication_state, deleted_at)
```

列表统一使用 `(last_published_at, id)` 或主键游标，禁止深 OFFSET。`intro` 是 RichText/Text，不承诺加入覆盖索引；应设业务长度上限并保持列表索引窄小。

#### 5.2 ArticleRevision

```text
id, article_id, revision_number
body_version_id, body_sha256, body_schema_version
metadata_snapshot
created_by_id, created_at
submitted_at, approved_at, superseded_at, deleted_at
```

建议唯一约束 `(article_id, revision_number)`。Revision 身份和正文去重是两回事；第一阶段采用“一条 Revision 一个不可变正文版本”，不引入引用计数复杂度。

#### 5.3 BlogPage 过渡字段

采用只增不删的 expand 阶段增加可空字段：

```text
live_body_version_id
live_body_sha256
live_body_schema_version
publication_generation
```

现有 `mongo_content_id` 保留为兼容指针；MySQL `body` 保留并继续持久为 `[]`。只有双读、回填、影子比对、切换和完整回滚窗口通过后，才停止旧指针写入。

#### 5.4 无外键生命周期状态与删除清单

Article 软删除后目录行仍在，可以直接核验状态；Wagtail BlogPage 硬删除后 Page 行不存在，因此需要不引用 Page 外键的持久清理依据：

```text
ContentLifecycleState
  aggregate_type, aggregate_id       联合唯一
  final_publication_generation
  desired_state                      active/deleted/purged
  tombstoned_at, updated_at

ContentDeletionManifest
  manifest_id
  aggregate_type, aggregate_id
  final_publication_generation
  status, retain_until
  created_at, completed_at

ContentDeletionItem
  manifest_id
  item_type                          live_body/revision_body/media_reference
  body_version_id / legacy_pointer
  body_sha256                        可空，旧数据兼容
  status, attempts, last_error_code
```

这些表不对 `Page` 或 `BlogPage` 建外键，避免页面级联删除时清理依据一起消失。BlogPage `pre_delete` 在同一 MySQL 事务内固化聚合 ID、最终 generation 和已知正式指针；每条 Revision 的 `pre_delete` 把其 JSON 中的精确草稿 pointer 追加到清单。单条 Revision 删除也使用同类清理项。捕获器只解析 MySQL 已有元数据，不查询或删除 Mongo。事件回滚时清单一并回滚；重复 signal 依靠唯一约束幂等。

### 6. MongoDB 详细数据模型

#### 6.1 不可变正文集合

建议 BlogPage 和 Article 共用新集合 `content_body_versions`，通过聚合类型隔离身份，避免为两条内容通道维护两套正文仓储契约：

```javascript
{
  _id: <应用预生成的稳定 version_id>,
  aggregate_type: <"blog_page" | "article">,
  aggregate_id: <int64>,
  body_version: <int64>,
  body: <StreamField 块数组>,
  body_sha256: <64 位十六进制>,
  schema_version: <int>,
  source_revision_id: <可空 MySQL Revision ID>,
  idempotency_key: <稳定重试键>,
  created_at: <UTC BSON datetime>
}
```

规则：

- 正文只插入，不原地更新。
- `version_id` 在写 Mongo 前生成；重试必须使用相同 ID 和哈希。
- 同 ID 且哈希一致是幂等成功；聚合类型、聚合 ID 或哈希不同是永久契约错误。
- 哈希基于规范化原始块 JSON，固定键排序和编码；Markdown 保持字符串，`markdown_block` key 不变。
- 时间使用 BSON UTC datetime，不再以 ISO 字符串作为主要查询字段。
- Mongo 不复制权威标题、摘要、权限和发布状态。
- 图片、音视频、附件仍在 MinIO，正文只存引用；接近 Mongo 16 MiB 上限时明确拒绝并进入专门处理流程。

建议初始索引：

```text
unique(_id)
unique(aggregate_type, aggregate_id, body_version)
unique(aggregate_type, aggregate_id, idempotency_key)
index(aggregate_type, aggregate_id, created_at DESC)
index(source_revision_id)  仅在确有反查需求时建立
```

#### 6.2 当前 Revision 指针 P0 风险

当前 `save_blog_revision_body()` 会为连续相同正文复用同一 Mongo OID，但删除任一 Wagtail Revision 时，`pre_delete` 会无引用检查删除该 OID，其他 Revision 因此可能失去正文。

推荐修复：以后每条 Revision 独占不可变正文版本；现有共享指针不做立即清理。若坚持内容去重，则必须新增显式引用关系并在零引用、隔离期和备份门禁后 GC，不能继续“共享指针 + 单条直接删除”。

Revision 集合至少需要 `(page_id/article_id, created_at DESC)` 索引，并直接保存 `body_sha256`，避免每次读取上一份完整正文重新计算 MD5。运行时建连不得创建或删除索引，索引变更改为独立管理命令和 runbook。

#### 6.3 分片决策

一千万篇不是自动分片条件。先建设三成员副本集和恢复能力；当未来 12 个月容量超过可用空间 70%、P99 在正确索引/缓存后仍超 SLO、复制延迟/IOPS/缓存持续饱和，或恢复时间超过 RTO 时，再进入分片评审。

候选 shard key 是 hashed `aggregate_id`，因为主要路径按内容聚合定点读且可避免连续 ID 写热点。分片后按正文版本 ID 读取也必须同时携带 `aggregate_type + aggregate_id`，避免只按全局 `_id` 形成 scatter-gather。最终选择前必须验证唯一索引约束、按聚合列举版本、延迟 GC 和 reshard 回滚；不得使用单调时间作为正文主分片键。

### 7. Elasticsearch 投影契约

```text
aggregate_type, aggregate_id
publication_generation
body_version_id, body_sha256
searchable
title, intro, body_text
published_at, locale_id, category_ids, tag_ids
```

当前仓库已有 `ContentSearchState -> ContentSearchOutbox -> ContentSearchDelivery`，包括提交后唤醒、独立目标 Delivery、租约、重试、死信、superseded 和 external version。Article 接入时应采用 expand-contract 把 `page_id` 演进为 `aggregate_type + aggregate_id`，旧 BlogPage 事件在兼容期仍可处理；不要另建第二套 ES 队列。

写入规则：

- ES 文档 ID 使用带类型的稳定内容 ID；
- publication generation 作为版本围栏，旧事件不得覆盖新文档；
- tombstone 版本高于历史 upsert，迟到事件不能复活删除内容；
- 回填使用固定扫描上界、主键游标和增量追平；
- 查询命中后批量回 MySQL 验证公开和权限状态；
- 新索引追平并校验后切 read alias，旧索引保留为回滚点。

### 8. 保存、发布和删除状态机

#### 8.1 批量导入 Article

```text
1. 按 source_key 在 MySQL 幂等取得/创建不可见 Article
2. 生成固定 body_version_id 和 idempotency_key
3. Mongo bulk insert/upsert-on-insert 写不可变正文
4. 批量读取 ID、归属、hash、schema 做最小校验
5. MySQL 分批锁定 Article，写草稿或待发布指针
6. 需要公开时切 published 指针并递增 publication_generation
7. 同一 MySQL 事务写搜索和生命周期事件
8. 提交后只发送 Celery 唤醒
```

Mongo 失败时 Article 保持不可见；Mongo 成功而 MySQL 失败只留下安全孤儿，由隔离期后的 GC 处理。导入不得逐篇调用 `add_child()`、Wagtail hooks 和普通 ORM `save()`。

#### 8.2 保存草稿

```text
1. 应用预生成 `revision_token/idempotency_key`、正文版本 ID 和规范化 hash
2. Mongo 幂等插入不可变正文版本
3. 读回最小字段验证归属、hash、schema_version
4. MySQL 事务写 Revision 元数据和 draft 指针
5. 不改变公开指针，不产生公开搜索 upsert
```

Mongo 不可用时草稿保存明确失败，不能生成指向空正文的成功 Revision。Wagtail Revision 使用数据库生成的主键，不能为了预先取得 ID 插入空 Revision；MySQL Revision 落库后再把其主键记录到映射元数据。MySQL Revision 保存失败只留下带稳定 token 的未引用版本，不得立即删除可能已被并发流程引用的正文。

#### 8.3 发布

```text
1. 从待发布 Revision 取得精确 body_version_id
2. 校验发布权限、审核状态和预期 publication_generation
3. 读取 Mongo 最小字段，确认正文存在、归属、hash、schema
4. MySQL select_for_update 锁定目录行
5. 再次比较期望 generation，拒绝并发旧发布
6. 原子切换 published 指针、状态和发布时间
7. publication_generation + 1
8. 同事务插入 Search Outbox/领域 Outbox
9. 提交后唤醒 Worker
```

Wagtail BlogPage 仍必须通过 `Revision.publish()`；不能直接修改 `live`、`live_revision` 或页面树。当前 Wagtail 8 发布 Action 先保存 Revision 重建出的页面对象，再发送 `page_published`，而搜索 receiver 也会读取正式 Mongo，因此不能只增加一个 receiver 并依赖 signal 注册顺序。实施时必须建立唯一、可测试的 BlogPage 发布编排入口，并专项覆盖定时发布、工作流、别名页和历史 Revision 发布。

#### 8.3.1 Workflow 审批：批准版本必须冻结

Wagtail Workflow 的“批准”不是“已经发布”。审批完成时应把以下值作为不可变审批快照写入 MySQL 审计/状态：`revision_id`、`body_version_id`、`body_sha256`、`schema_version`、页面的 `publication_generation` 以及审批人和时间。审批通过后仍允许编辑新草稿，但新草稿不能悄悄改变已批准版本。

实际发布前必须在同一发布编排入口重新读取并锁定页面状态，校验批准 Revision 仍是目标 Revision，Mongo 正文存在且归属、hash、schema 均匹配，页面权限和发布时间仍有效，generation 也未被更高版本占用。任一校验失败都标记为“审批过期/需重新审核”，不得自动发布或回退到当前正式正文。审批动作、发布动作和 Outbox 写入必须使用幂等键，保证重试不会产生第二次代际推进。

最小验收场景包括：提交审核后编辑、批准后编辑、批准时 Mongo 不可用、驳回后重新提交、审批与发布并发、批准版本预览，以及批准版本被恢复后再次发布。后台页面可以继续使用 Wagtail 原生 Workflow URL，版本冻结和一致性校验放在 BlogPage 发布服务中。

#### 8.3.2 Scheduled Publishing：两个异步执行上下文

Wagtail 的预约发布由 `PublishPageRevisionAction` 及其调度任务执行；搜索/内容 Outbox Worker 是另一条异步链路。两者不共享事务、队列或重试状态，不能假设“定时任务成功”就已经完成搜索投影。

到期任务必须选定精确 Revision，并在执行前重新校验 go-live 时间、当前批准状态、`body_version_id`、正文 hash/schema、页面权限和期望 generation，然后调用 Wagtail 的 `Revision.publish()`。发布成功后应在可控的 MySQL 事务边界内写入 publication Outbox；如果 Wagtail 核心动作无法被外层事务包裹，则将 `page_published` 仅作为通知，并由对账任务发现“已发布但缺 Outbox”的记录。定时任务重复执行、Worker 暂停或进程在发布后崩溃都必须依靠 `(content_kind, aggregate_id, generation, operation)` 幂等键恢复，不能重复推进公开版本。

定时发布验收还要覆盖时区和时钟漂移、取消后残留任务、改期、重复到期、发布后 Outbox 延迟以及 Worker 长时间不可用；Outbox 延迟只能造成搜索最终一致，不能阻塞 Wagtail 页面发布。

#### 8.3.3 `page_published` 信号：通知和修复入口

`page_published` receiver 只负责缓存/搜索唤醒、审计或对账提示，不负责写入或删除 Mongo，也不负责推进正式指针。不能依赖 receiver 注册顺序，因为 Wagtail 8 的发布 Action 会先保存由 Revision 重建的页面对象，再发送信号，信号实例也不保证包含外部投影所需的最新提交状态。

发布服务应先完成 Wagtail `Revision.publish()`，再在可控的 MySQL 事务中切换 `body_version_id`、递增 generation 并写 Outbox；`transaction.on_commit()` 只用于唤醒消费者。receiver 若需要生成投影，必须重新读取已提交的页面和版本状态，不能把信号携带对象当作唯一事实来源。必须增加“已发布但无 Outbox/状态未对齐”的定期修复扫描，并保留可审计的补偿结果。

#### 8.3.4 `with_content_json()` / `Revision.as_object()` 契约

Wagtail 8 的历史预览、比较和恢复依赖 Revision 的 JSON 序列化/还原契约。`serializable_data()` 应输出完整页面 JSON：`body` 保持逻辑上的空占位，同时携带 `mongo_body_version_id`、兼容期的旧指针字段、`body_sha256` 和 `body_schema_version`。实际实现不能凭字符串形式猜测 Wagtail 类型；当前代码写入字符串 `'[]'`，必须通过 Wagtail 8 的 `with_content_json()`、`Revision.as_object()` 契约测试确认该字段应为字符串还是列表，并固定版本化格式。

`from_serializable_data()` 使用统一指针解析器，同时支持 ObjectId 和兼容期的 `rev_<page>_<uuid>` 字符串；成功时返回完整 StreamValue。指针缺失、归属不符、hash/schema 不符、Mongo 不可用或反序列化失败时，必须返回明确的领域错误/不可用状态，不能静默回退到 `body=[]` 或当前正式正文。预览、比较和恢复都要显示可诊断的不可用结果，并记录 Revision ID 与正文版本 ID。

直接调用 `with_content_json()`、`Revision.as_object()` 的往返测试，以及新旧指针格式、空 body 占位、hash/schema 保持和旧 Revision 恢复测试，属于切换新实现前的发布门禁。

#### 8.3.5 Wagtail 8.0 专项发布门禁

| 门禁 | 失败处理 |
| --- | --- |
| Workflow 批准快照与实际 Revision/body_version 不一致 | 阻止发布，标记需重新审核 |
| 预约发布重复执行或取消后残留任务 | 幂等吸收并写审计，不推进第二代 |
| Wagtail 发布完成但缺 publication Outbox | 对账任务补建，搜索投影保持待处理 |
| `page_published` receiver 触发 Mongo I/O | 测试失败，禁止上线 |
| `with_content_json()` 往返 hash/pointer 不一致 | 阻止兼容层切换，保留旧读路径 |

以上改造不改变 Wagtail 历史列表、权限和现有 URL；只在 BlogPage 的序列化、发布编排、对账和正文仓储边界增加一致性适配。

#### 8.4 编辑、定时和取消发布

- 编辑已发布文章只产生新草稿；公开详情继续读旧 published 指针。
- 预约发布时不切正式指针；到期执行 Wagtail 发布 Action 时重新校验版本、正文和状态。
- 取消发布在 MySQL 事务内递增 generation 并写 tombstone；Mongo 正文和 Revision 保留。

#### 8.5 删除和物理回收

默认状态：

```text
published/unpublished -> deleting -> deleted -> purge_eligible -> purged
```

正确流程：MySQL 先写删除状态、永久 tombstone 和生命周期事件；ES/缓存先停止公开；Mongo 进入隔离期；GC 到期后再次核对 MySQL 状态、最终 generation、全部 Revision/媒体引用和备份覆盖，最后按精确 ID/hash 删除并记录审计。

不能简单移除 `pre_delete`：Wagtail 删除子树时可能绕过具体 `BlogPage.delete()`，但 Django 仍发送级联 `pre_delete`。因此保留 `pre_delete` 捕获器，其中只能写 MySQL cleanup intent/outbox，禁止 Mongo I/O。`transaction.on_commit()` 只用于唤醒；真正可靠性来自已提交 Outbox。

### 9. Outbox 与消费者契约

#### 9.1 搜索投递复用现有实现

现有搜索 Outbox 继续负责 ES：

- `ContentSearchState` 保存期望公开版本；
- `ContentSearchOutbox` 保存已提交的 upsert/tombstone；
- `ContentSearchDelivery` 保存每个 ES 目标的租约、重试和结果；
- Beat 扫描 pending/retry/过期租约，Celery 只负责唤醒和执行；
- Delivery 比较最新 State、正文 ID 和哈希，旧事件进入 superseded。

#### 9.2 内容生命周期独立投递

正文清理、缓存失效、引用投影等动作不能共用一个完成状态。建议采用“一个领域事件 + 每个目标一条 Delivery”，字段至少包括：

```text
event_id
aggregate_type, aggregate_id, publication_generation
event_type
body_version_id, body_sha256
status, attempts, available_at
locked_by, lock_expires_at
last_error_code, 脱敏截断消息
created_at, completed_at
```

消费者统一要求：

- 至少一次投递，所有操作幂等；
- 领取时使用行锁、跳过已锁行和有限租约；
- ES/缓存执行前读取最新状态，旧 generation 标为 superseded；
- Mongo GC 不以 generation 过旧直接跳过或删除，而是按精确正文 ID/hash 重新检查所有当前引用、隔离期和备份条件；仍被引用时取消/延后该清理项；
- 删除 tombstone 保存永久单调围栏，任何较低 generation 的 upsert 都不能复活内容；
- 429、5xx 和网络错误指数退避，契约错误进入 dead；
- 死信告警，人工重放精确到一条 Delivery 并记录原因；
- 全部必需目标完成并超过审计期后，Outbox/Delivery 才可归档；
- 事件和错误记录只保存 ID、版本、哈希和错误码，不保存正文或凭据。

恢复消费者所需吞吐应满足：

```text
恢复吞吐 > 实时事件速率 + backlog / 目标追平时间
```

### 10. 读取、缓存与降级

#### 10.1 详情页

```text
1. MySQL 按 route_key 读取公开目录和 published_body_version_id
2. Redis 按 aggregate_id:publication_generation 读取组合缓存
3. 未命中时按 version_id 从 Mongo 定点读取正文
4. 校验 article/page ID、hash、schema_version
5. 渲染并写有限 TTL 缓存
```

权限文章不得与公开缓存混用。Mongo 暂时不可用时，只能在明确时效和权限边界内返回最近一次已验证缓存；无缓存则返回可观察的 503，不能用任意草稿、空正文或旧 MySQL `body` 返回 200。

#### 10.2 列表、归档和 Feed

只读 MySQL 元数据，不读取 Mongo 正文。模板和 serializer 禁止隐式访问 `body`；使用游标分页和批量关系预取。Feed 只包含公开摘要和链接，并由 publication generation 事件失效。

#### 10.3 搜索

ES 返回稳定 ID、高亮和投影字段；应用批量回 MySQL 验证公开性、权限和删除状态，再按 ES 顺序组装结果。搜索结果页不能为每条命中读取 Mongo 完整正文。

### 11. 正文引用、媒体与 GC

StreamField 正文包含图片、文档、音视频、嵌入和内部页面引用。Mongo 没有外键，不可变正文版本会使历史引用长期存在，因此需要可重建的正文引用投影：

```text
body_version_id
reference_type
reference_id
block_id / path
```

引用清单可在正文写入时生成并在异步任务中复核。媒体或内部对象物理删除前必须检查所有仍在保留期、正式、草稿或历史 Revision 的引用；不能只检查当前公开版本。

GC 使用主键/时间游标、固定扫描上界和 checkpoint。版本只有同时满足以下条件才可删除：

- 超过隔离期；
- MySQL 无公开、草稿、Revision 或精选映射引用；
- 无未完成 Outbox/Delivery；
- 备份已覆盖该版本；
- 不处于恢复、审计或法律保留状态。

GC 先标记候选，下一独立批次再次核验后物理删除。不得直接给受保护 Revision 集合添加 TTL。若未来有短期自动保存，应拆到明确不承担历史恢复责任的 `ephemeral_autosaves` 集合后再评估 TTL。

### 12. 一致性对账

所有扫描只输出计数、ID、长度、哈希和错误码，不输出正文：

- MySQL 公开指针在 Mongo 是否存在；
- Mongo 的聚合 ID、正文版本、hash、schema 是否匹配；
- Wagtail/Article Revision 指针是否存在；
- Mongo 未引用版本按隔离期分类；
- ES publication generation 是否落后、超前或哈希不一致；
- tombstone 后是否仍有 searchable 文档；
- Outbox pending/retry/processing/dead、最老延迟和 lease reclaim；
- Mongo 单文档大小、索引大小、磁盘水位和分片热点。

禁止自动用任意草稿修复正式正文。公开指针缺失时先停止该文章公开或使用已经确认的旧正式版本；任何修复均需精确目标、审计和数据操作授权。

### 13. 容量模型与当前小样本

定义：

```text
N   = 文章数
Vf  = 每篇保留的正式正文版本数
Vr  = 每篇保留的 Revision 数
Bc  = 正式正文平均物理字节
Br  = Revision 平均物理字节
R   = Mongo 副本数
H   = 运维余量系数
Be  = 每篇 ES primary 平均字节
E   = ES 数据份数（primary + replicas）
G   = 同时保留的 ES 索引代数
```

```text
Mongo = N × (Vf × Bc + Vr × Br) × R × H
ES    = N × Be × E × G ÷ 目标最大磁盘使用率
MySQL = 目录 + 关系 + Revision 元数据 + State/Outbox/Delivery + 二级索引
```

本次 data agent 对测试库做了只读小样本测量：156 篇 live BlogPage；Mongo 正式正文约 160 条、约 15.8 KB/条；Mongo Revision 约 191 条、约 8.56 KB/条；精简 ES 约 12.76 KB/篇 primary；MySQL Wagtail Revision 237 行、约 33.6 KB/行。样本很小且不代表未来语料，只说明把一千万篇全部放进完整 Wagtail Revision 流程成本很高。

仅作量级示例：若 `N=1000 万、Vf=2、Vr=3`，按该小样本估算，Mongo 单份约 573 GB，三副本加 30% 余量约 2.2 TB；ES primary 约 128 GB，一副本、在线重建保留两代、磁盘最多使用 70% 时约需 730 GB。不得据此直接采购，必须用真实 P95/P99 和版本分布重算。

ES primary 分片数按以下方式从实测目标分片大小推导：

```text
ceil(预计 primary 总量 / 经压测确定的目标分片大小)
```

当前生产单节点 ES 即使配置副本也不具备节点级高可用；进入千万级前必须先建设多节点和故障域，不应把增加 replica 数当作已经高可用。

### 14. 建议 SLO 与压测阶梯

以下只是待业务确认的起始验收值，不是当前能力声明：

- 公开详情可用性不低于 99.9%；
- 缓存命中详情 P95 小于 200 ms，未命中 P95 小于 500 ms；
- 列表/归档 P95 小于 300 ms；
- 搜索 P95 小于 800 ms；
- 正常发布到 ES 可见 P95 小于 30 秒；
- 无死信时 Outbox 最老积压小于 5 分钟；
- 故障解除后的消费速度持续高于事件产生速度；
- 备份恢复满足已确认 RPO/RTO。

压测按 `10 万 -> 100 万 -> 300 万 -> 1000 万目录行` 递增，保留真实正文大小、语言、标签和 Revision 分布，覆盖：

- 冷/热详情、列表游标、标签/分类筛选、搜索；
- 峰值批量导入、草稿、发布和并发编辑；
- ES 在线回填与实时增量并行；
- Mongo/ES/Redis/Celery 分别故障，ES 429/503；
- Worker 被杀死、租约过期、MySQL 回滚和节点切换；
- 备份恢复、索引重建、alias 回滚和磁盘高水位。

采集 P50/P95/P99、QPS、错误率、慢查询、复制延迟、WiredTiger cache eviction、ES heap/GC/merge、Outbox lag、死信率和恢复时间。测试数据必须是合成数据或经授权的测试样本。

### 15. 跨库备份与恢复检查点

- MySQL：物理全备 + binlog PITR，千万级不能只依赖 `mysqldump`。
- Mongo：副本集快照 + Oplog/PITR；逻辑导出只用于小范围迁移。
- Elasticsearch：可从 MySQL + Mongo 重建，快照用于缩短 RTO。
- MinIO：版本化、对象清单和独立备份。
- Redis：可丢失缓存，按版本键重建。

每次一致性检查点应记录：

```text
checkpoint_id
MySQL binlog / backup position
Mongo clusterTime / oplog position
最后完成的 publication_generation / Outbox 水位
ES serving alias 和索引 generation
MinIO 清单版本
```

恢复原则是 Mongo 恢复点不能早于 MySQL 可见正文指针；不可变 Mongo 恢复到稍晚时间只会多出安全孤儿。若 Mongo 无法恢复到不早于 MySQL 指针的时间点，必须把 MySQL 回退到兼容检查点，或将受影响文章暂时设为不可用并执行精确恢复，不能继续启动公开读取。恢复时先冻结消费者和写入，恢复 MySQL/Mongo/MinIO，运行指针/hash 对账，确认 ES alias，重放检查点后的 Outbox，最后依次恢复 Django、maintenance Worker、Beat 和 Filebeat。不能只以备份命令成功或 unit active 宣称恢复完成。

### 16. 安全与删除边界

- Mongo 正文读取必须先经过 MySQL 站点、公开状态和权限判断，不能凭 version ID 直接对外暴露。
- 草稿、历史正文和删除隔离区使用独立权限边界；日志、ES 和 Outbox 不保存原始正文。
- 合规删除与普通 GC 分开，前者需明确对象范围、法定保留、备份例外、执行证据和不可逆授权。
- 批量修复、GC、恢复和重放命令默认 dry-run，要求环境门禁、精确 ID/游标和显式 `--confirm`。

### 17. 分阶段实施工作包

#### WP0：只读基线与容量采样

新增只读一致性/容量命令、checkpoint 和测试，不修改数据。验收是获得正文/Revision 大小分布、指针完整性、Outbox/ES 差异和真实查询计划，且不输出正文。

#### WP1：先消除当前数据丢失路径

- `pre_delete` 改为只写 MySQL cleanup intent，禁止 Mongo I/O；
- 所有物理清理由持久 Delivery 在提交后执行；
- 修复共享 Revision pointer 删除问题；
- Mongo 索引管理移出应用启动。

验收：外层事务回滚时 Mongo 不变；Wagtail 单页、子树和 QuerySet/级联删除都生成精确意图；重复删除幂等；Worker 停机后任务不丢；存量 Revision 均可恢复。

#### WP2：BlogPage 不可变正文版本

新增版本集合、可空兼容字段、正文 repository、规范化 SHA-256、双写/双读和生命周期测试，不删除旧集合。

验收：草稿不影响正式正文；发布失败不改变在线版本；并发发布只有期望 generation 成功；历史版本可重新发布；Mongo/MySQL/Celery 分别故障时结果明确。

#### WP3：Wagtail 发布编排与 Outbox 对齐

建立唯一发布编排入口，覆盖立即发布、定时首发、定时更新、撤回计划、工作流批准、历史版本回滚和别名页。正文确认、MySQL 指针切换、publication generation 和搜索 Outbox 必须形成一个可测试事务契约，不依赖 signal 顺序。

#### WP4：在线兼容迁移

1. 加可空新字段和新集合，旧读写不变；
2. 开启受控双写，新代码同时生成新版本和保留旧文档；
3. 按主键游标、限速、固定上界回填，保存 checkpoint；
4. 只比较 ID、hash、长度和 schema；
5. 开启影子读，对比新旧但仍返回旧路径；
6. 特性开关切到新读路径；
7. 保留旧读路径和旧文档一个完整回滚观察窗口；
8. 停止旧写；旧数据清理另行授权。

验收：中断可续跑，无 N+1，无正文输出；任一阶段均可关闭开关回到旧读路径；回滚不删除新旧正文。

#### WP5：独立 Article 目录与批量导入

新增 Article、Revision、关系、staging、专用路由和最小后台，保持精选 BlogPage URL 不变。验收：来源重试不重复、无公开半成品、批量导入不逐篇触发 Wagtail Page 保存，权限/slug/locale/分类/删除均有测试。

#### WP6：统一搜索投影

BlogPage 与 Article 生成同一公开文档契约；创建新物理索引、回填、增量追平、校验后切 alias。验收：全量可重建；旧事件不能复活内容；read alias 只指向一个 serving 索引；旧索引可立即回滚。

#### WP7：百万到千万级验证与基础设施演进

用代表性数据完成压测、容量预测、备份恢复和单节点故障演练。只有证据触发时才引入 MySQL 读副本、Mongo 分片或 ES 扩容。

#### WP8：收缩旧结构

新链路稳定运行完整观察窗口且恢复演练通过后，才停止旧 Mongo 镜像元数据写入、归档旧指针并评估清理。MySQL `body` 字段本方案仍不删除。

### 18. 预计修改与明确不在同批完成的范围

后续预计涉及：

- `blog/models.py`、`signals.py`、正文 repository/生命周期服务和定向测试；
- `mongo.py` 职责拆分，移除启动索引副作用；
- `search/models.py`、Outbox/Delivery/document/rebuild 服务及迁移；
- 新 Article 模型、服务、路由、后台、导入和测试；
- maintenance/Beat 路由及 `systemctl.md`；
- 每个工作包独立方案记录和生产 runbook。

不能在同一个发布批次中同时完成生产数据修复、Mongo 分片、ES 全量重建、Article 上线、旧数据清理和服务拓扑变化。每项必须有独立备份、压测、回滚点与生产授权。

### 19. 测试矩阵

| 范围 | 必测场景 |
| --- | --- |
| 草稿 | 新建、相同正文连续保存、不同正文、Mongo 失败、MySQL Revision 失败、历史恢复 |
| 发布 | 首次/再次发布、发布旧 Revision、并发、定时首发/更新、撤回计划、工作流、别名页、MySQL 回滚 |
| 编辑 | 已发布页保存草稿但在线正文不变、仅元数据发布、正文与元数据同时发布 |
| 删除 | 单页、子树、级联、外层事务回滚、重复事件、Worker 停机、隔离期、精确 GC |
| 搜索 | 旧 upsert 晚于 tombstone、回填与增量并发、alias 切换、ES 不可用、MySQL 二次过滤 |
| 导入 | 重复 source_key、部分批次失败、断点恢复、无 Wagtail hook 放大、峰值吞吐 |
| 兼容 | 旧 `mongo_content_id`、旧 Revision pointer、旧块缺 ID、Markdown key/字符串不变 |
| 恢复 | 两库时间线不齐、Redis 全丢、ES 重建、Celery 租约恢复、Outbox 水位重放 |

### 20. 发布与回滚门禁

所有结构变化采用 expand -> 双写/双读 -> 影子校验 -> 切换 -> 观察 -> contract：

1. 先增加表、字段、集合和索引，不删除旧结构；
2. 测试环境按哈希/长度对账并运行故障测试；
3. 新代码兼容旧指针，旧代码仍能读取旧正式集合；
4. 测试通过后提交精确 commit，推送并核对 GitHub 检查；
5. 生产前另行说明 MySQL/Mongo/ES/MinIO 备份、容量、时长和回滚；
6. 生产迁移、回填、索引创建、alias 切换、服务重启和 GC 分别授权；
7. 验收失败先关闭新读路径或切回旧 alias，不删除新旧正文；
8. 观察窗口和恢复演练通过后才讨论旧结构清理。

内容回滚通过重新指向已验证的不可变版本完成，不改写历史正文；ES 回滚只切 read alias；永久 tombstone 和事件审计不能随正文删除。

### 21. 最终决策摘要

1. `intro` 继续由 MySQL 权威保存；MySQL `body` 保留字段定义并持久为 `[]`。
2. 正式正文从 Mongo 单文档原地覆盖演进为“不可变版本 + MySQL 公开指针”。
3. 精选内容使用 BlogPage；一千万海量文章使用同项目独立 Article。
4. ES 复用现有搜索 Outbox/Delivery；正文回收使用独立生命周期 Delivery。
5. `pre_delete` 保留为 MySQL 意图捕获器，禁止跨库物理删除。
6. 一条 Revision 对应一个正文版本，先消除共享 pointer 删除风险。
7. 先修复数据丢失路径，再迁移；先压测再分片；所有生产数据操作独立授权。

## 实际修改与不修改的文件

- 修改：仅本文档。
- 不修改：`BlogPage`、Mongo 管理器、信号、搜索代码、迁移、环境文件、服务配置与任何数据库数据。

## 数据和服务影响

本次仅阅读源码和测试环境已安装的 Wagtail 实现，不执行数据写入、迁移、索引操作、队列投递、服务重启或生产操作。`systemctl.md` 无需更新。

## 测试与验收

- 已核对 WSL2 `wagtailblog-test` 中 Django 5.2.8、Wagtail 8.0，以及 `Page.add_child`、`save_revision`、`save` 的实际源码。
- 已核对 BlogPage、Mongo 管理器、删除信号、Markdown 导入和搜索文档构建调用点。
- 未运行写入型生命周期测试或数据库一致性审计，以避免在当前分析阶段改变数据。

## 回滚点与残余风险

本次只有文档变更，V2 方案的回滚点为移除本轮新增章节并恢复对应早期摘要。残余风险是现有跨库保存/删除的一致性问题仍未修复；不得据本文档直接执行数据清理、迁移或生产改动。

## 模型/推理强度建议

- 事实收集：`gpt-5.6-luna` 低到中推理，适用于只读代码、配置和聚合审计。
- 设计与实现：`gpt-5.6-terra` 高推理，适用于跨文件的 Wagtail 生命周期和 outbox 改造。
- 升级条件：迁移、Mongo/MySQL 一致性修复、历史正文清理、生产发布或回滚使用 `gpt-5.6-sol` 高到 xhigh 推理，并要求独立复核。
- 验证门禁：生命周期故障测试、只读一致性审计、备份恢复演练、测试环境验证、生产操作的单独书面授权。
- 实际使用：主 agent 汇总实际仓库证据，并安排 `arch`、`data`、`review` 三个只读角色独立复核。Context7 成功解析 Wagtail 文档库但查询阶段连接失败，因此版本契约改由 WSL2 已安装 Wagtail 8.0 源码核对；未向外部发送项目源码、正文、凭据或日志。

## 实施记录

### 2026-08-27：只读分析完成

- 状态：完成分析，未实施运行时代码或数据改动。
- 实际修改文件：新增本文档。
- 验证：已读取实际模型、信号、Mongo 管理器、搜索投影与 Wagtail 8 已安装源码；未执行写入型测试。
- 数据/服务影响：无；未提交 Git。
- 回滚点：删除本文档。
- 残余风险：跨库保存和 `pre_delete` 清理风险仍存在，需独立方案和授权后处理。

### 2026-08-27：千万级企业方案 V2 完成

- 状态：完成详细架构、数据模型、状态机、Outbox、迁移、容量、压测、备份恢复和回滚设计；未实施运行时代码。
- 实际修改文件：仅更新本文档。
- 独立复核：`arch`、`data`、`review` 均完成只读审查；据此补入 Treebeard 单父节点边界、共享 Revision pointer 删除风险、级联删除捕获器、三种版本分离、在线迁移和跨库恢复水位。
- 验证：核对 BlogPage/Mongo/搜索 Outbox/Delivery 代码及 Wagtail 8 已安装发布源码；`git diff --check` 通过，未跟踪文档的 `git diff --no-index --check` 未报告空白错误。未运行写入型生命周期测试、迁移、压测或生产检查。
- 数据/服务影响：无；未写 MySQL、MongoDB、Elasticsearch、Redis 或 MinIO，未投递 Celery，未重启服务；`systemctl.md` 无需更新。
- Git：工作区未提交；原有 28 号说明书保持未跟踪，29 号说明书为本任务文档。
- 回滚点：移除 V2 章节及本实施记录，不影响运行时行为。
- 残余风险：当前同步 Mongo 保存、同步删除和共享 Revision pointer 风险仍在代码中；只有 WP1 获得实现授权并通过测试后才能宣称修复。

## 企业方案 V2 补充：搜索改造影响分析

### 22. 结论：保留搜索底座，扩展内容身份与结果适配层

面向 `BlogPage + Article` 和千万级公开文章，当前搜索模块不需要推倒重写。版本化物理索引、read alias、严格 mapping、MySQL State/Outbox/Delivery、租约与重试、外部版本、批量 Mongo 读取、固定上界回填、checkpoint、PIT/search-after 和高亮处理都应继续复用。

但这也不是只在查询中增加一个 `Article.objects` 分支即可完成。当前独立内容搜索从状态表、事件、ES `_id`、重建游标、查询回填到模板，均把内容身份写死为 `BlogPage.page_id`。引入 Article 时需要一次中等偏大的兼容性改造，主要修改数据契约和适配层，不修改 Elasticsearch 作为可重建投影的定位。

搜索相关链路必须先分清：

| 链路 | 当前用途 | 千万级目标 |
| --- | --- | --- |
| Wagtail Page Explorer `/admin/pages/<id>/?q=` | 按 Page 标题自动补全，并叠加页面树和后台权限 | 只继续管理精选 Wagtail Page/BlogPage；不承载 Article 正文搜索 |
| 独立内容索引 | 公开 BlogPage 的标题、摘要、Mongo 正文全文搜索 | 扩展为 BlogPage 与 Article 的统一公开内容索引 |
| Wagtail 默认 Page 索引 | 普通非 BlogPage 页面搜索 | 第一阶段保持不变，与统一内容索引组成全站搜索 |
| 独立标题建议索引 | 公开 BlogPage 标题联想 | 增加类型化身份和增量投递，覆盖 BlogPage 与 Article |

因此，最重要的边界是：不能为了让海量 Article 出现在 Page Explorer 中而把它们重新建成 Page。Article 应有独立后台列表；后台正文检索若确有业务需要，应使用与公开索引隔离的受权限保护索引，不能把草稿或非公开正文写入公开 read alias。

### 23. 当前可直接复用的能力

以下实现已具备企业搜索底座的关键属性：

- `services/content_index.py`：版本化物理索引、严格字段白名单、analyzer profile、分片/副本/刷新间隔配置和 `best_compression`；
- `models.py`、`services/outbox.py`、`services/delivery.py`：事务内 State/Outbox、按目标 Delivery、至少一次投递、租约回收、重试、死信和 superseded；
- `services/elasticsearch.py`：Bulk 写入、请求字节估算和 ES external version，迟到事件不能覆盖新版本；
- `services/rebuild.py`：固定扫描上界、主键游标、批量读取、按字节切 Bulk、checkpoint、增量追平和 alias 切换门禁；
- `services/content_query.py`、`services/cursor.py`、`services/highlights.py`：公开性二次检查、签名游标、可选 PIT 和安全高亮；
- `ContentSearchTarget` 与 `SearchIndexBuild`：新旧物理索引双投递、在线构建、校验和回滚基础；
- maintenance Worker 与 Beat：已有可靠唤醒加补偿扫描，不需要为 Article 搜索新增一套 Worker 或队列。

这些机制的职责正确，Article 接入应通过通用内容源接口扩展它们，不能复制出第二套 `ArticleSearchOutbox`、第二套索引切换命令或第二条 Celery 队列。

### 24. 必须修改的公开搜索文档契约

当前 mapping 版本是 `v003`，ES 文档 `_id` 等于十进制 `page_id`。Article 主键与 Page 主键可能相同，所以新契约必须使用类型化身份：

```text
content_key             "blog_page:38" / "article:38"，同时作为 ES _id
aggregate_type          blog_page / article
aggregate_id            MySQL 主键
publication_generation  每次公开投影变化单调递增，作为 ES external version
body_version_id         Mongo 不可变正文版本指针
body_sha256              只校验正文
projection_sha256        校验标题、摘要、正文和筛选字段组成的完整公开投影
operation                upsert / tombstone
searchable               是否允许公开命中
title, intro, body_text
date, first_published_at, locale_id
tag_ids, category_ids
```

相关性或日期相同时，最终稳定排序键也必须使用 `content_key`（或等价的 `aggregate_type + aggregate_id`），不能继续只用裸 `page_id`。否则即使 `_id` 已命名空间化，混合类型的 search-after 游标仍会发生排序碰撞。

`body_sha256` 与 `projection_sha256` 不能继续混为一个 `content_hash`。只改标题、摘要、分类或公开状态时，正文哈希可以不变，但搜索投影版本必须递增。`publication_generation` 继承当前 `content_version` 的防迟到职责，正文自身的 `body_version` 不参与比较大小。

墓碑继续写入同一个 `_id`，仅保留身份、generation、`searchable=false` 和 `operation=tombstone`。不能立即物理删除墓碑，否则迟到的旧 upsert 可能重新创建已取消发布或删除的文档。

新 mapping 必须创建新物理索引；若实施前没有其他 mapping 版本占用，候选版本为 `v004`。不得原地修改当前 `v002/v003` serving 索引。新字段完成回填、增量追平和一致性校验后，才切换 read alias。

### 25. State、Outbox 和 Delivery 的兼容演进

当前 `ContentSearchState.page_id` 是主键，Outbox 唯一约束是 `(page_id, content_version)`。这两个结构无法区分相同数字 ID 的 BlogPage 与 Article。目标约束应为：

```text
ContentSearchState:  unique(aggregate_type, aggregate_id)
ContentSearchOutbox: unique(aggregate_type, aggregate_id, publication_generation)
ES document:         _id = content_key
```

实施采用 expand-contract：

1. 新增可空 `aggregate_type`、`aggregate_id`、`content_key`、`publication_generation`、`body_version_id`、`body_sha256` 和 `projection_sha256`，旧字段仍保留；
2. 存量 BlogPage 行按 `aggregate_type=blog_page` 和原 `page_id` 回填，只比较 ID 和哈希，不读取输出正文；
3. Producer 双写新旧身份，Consumer 优先处理新身份并兼容旧 BlogPage 事件；
4. 新索引只接受新契约，旧 serving 索引继续由兼容 Delivery 投递；
5. alias 切换并完成观察窗口后停止旧事件写入；旧字段、旧索引和旧事件归档另行授权。

直接把现有 `page_id` 主键在线改成联合主键风险过高。实施时应优先评估新建通用 State 表并在线回填，或增加代理主键和联合唯一约束；选择必须以 MySQL 版本、表行数、DDL 算法和锁影响实测为准。

`ContentSearchScopeJob` 是 Wagtail 页面树访问限制的专用任务，`root_page_id` 可以保留，不必强行通用化。Article 没有页面子树，应由 Article 发布/权限服务直接生成类型化 upsert 或 tombstone。

### 26. 内容源适配器与正文读取

当前 `document.py`、`mongo.py` 和 `delivery.py` 直接调用 `BlogPage`、`mongo_content_id`、`get_full_text_for_search()` 及旧 `blog_content` 集合。目标态增加一个小而明确的内容源协议：

```text
identity(object) -> aggregate_type, aggregate_id, content_key
is_public(object) -> bool
metadata(object) -> title, intro, dates, locale, tag/category ids, canonical URL
body_pointer(object) -> body_version_id, body_sha256
body_text(version_document) -> normalized plain text
```

分别实现 `BlogPageSearchSource` 与 `ArticleSearchSource`。Outbox、Delivery、重建和一致性检查只依赖该协议，不在核心循环中散落 `if aggregate_type == ...`。

Mongo 批量读取继续保留一次请求读取一个批次的模式，但从旧 `blog_content._id/page_id` 改为按 `content_body_versions` 的 `aggregate_type + aggregate_id + body_version_id` 精确读取。若未来 Mongo 使用 hashed `aggregate_id` 分片，仅按全局版本 ID 查询可能形成 scatter-gather，因此搜索回填请求必须携带完整聚合身份。兼容期执行旧、新双读或影子对比，不在循环中回退为逐篇 Mongo 查询。

搜索消费者只读取 Outbox 指向的已发布不可变正文版本。草稿、Wagtail Revision 正文和 Article 草稿版本永远不能进入公开索引。

### 27. 在线重建与千万级回填

现有 `SearchIndexBuild` 只有 `scan_upper_bound_page_id` 和 `checkpoint_page_id`，只适合一类 BlogPage。目标态为每次 Build 建立分来源游标，例如子表：

```text
SearchIndexBuildCursor
  build_id
  aggregate_type
  scan_upper_bound_id
  checkpoint_id
  scanned / succeeded / missing / failed
  unique(build_id, aggregate_type)
```

回填顺序可以先扫描数量较小的 BlogPage，再扫描 Article；每类内容都在启动时固定自己的最大公开主键。构建期间产生的发布、取消发布和删除事件继续双投递到 serving 与 building Target，回填结束后追平所有未完成 Delivery，再做一致性门禁。

千万级回填必须保持：

- MySQL 按主键游标扫描，不使用深 OFFSET；
- Article 目录批次只选索引需要的列，分类/标签必须批量预取；
- Mongo 按完整版本身份批量读取；
- ES Bulk 同时受文档数和 UTF-8 请求字节限制；
- checkpoint 只在整批成功后推进，中断可续跑；
- 回填限速并监控 MySQL 复制延迟、Mongo cache/磁盘、ES heap/merge 和线上查询延迟；
- Build 失败不切 alias，旧 serving 索引持续提供查询。

分片数不能按“一千万篇”直接拍定。先用代表性的标题、摘要、正文长度和中文分词结果测量主分片实际字节、segment 数、merge 压力与查询并发，再确定新物理索引的主分片和副本数。

### 28. 查询结果与页面模板需要较明显的适配

当前 `ContentSearchResults` 把 ES `page_id` 批量回填为 `BlogPage`，搜索模板依赖 `pageurl`、`result.specific`、`content_type` 和 Page 属性，API 也直接调用 `page.get_url()`。Article 不是 Page，不能伪装成 Page 对象。

目标态增加只读 `SearchResultItem` 展示对象，至少包含：

```text
content_key, aggregate_type, aggregate_id
title, url, type_label, date, intro
featured_image_ref（可空）
matched_field, highlight_fragments, title_highlight
```

ES 每次返回类型化 ID、排序值和高亮；应用按类型分组，以最多两次 MySQL 查询批量验证 BlogPage 与 Article 的公开状态，再按 ES 原顺序组装 `SearchResultItem`。模板改读 `result.url/result.title/result.type_label`，不再要求所有结果都支持 `pageurl` 和 `.specific`。API 明确增加 `content_key` 与 `content_type`，原数值 `id` 在兼容窗口保留，但客户端不能再把不同类型的相同 ID 当成同一对象。

搜索结果页不能读取 Mongo 正文。正文纯文本和高亮来自 ES，标题、摘要、URL、公开状态和权限边界由 MySQL 批量确认。

### 29. 当前搜索中应先修正的规模与一致性问题

以下问题已存在，Article 接入前应纳入搜索工作包：

1. State、Outbox、ES `_id` 和排序只使用 `page_id`。同号 BlogPage 与 Article 会共用 State、互相 supersede、撞 Outbox 唯一约束或覆盖同一 ES 文档；这是 Article Producer 上线前必须阻断的 P0。
2. Delivery 固定按 `event.page_id` 回查 BlogPage。Article 事件在无同号 BlogPage 时会被静默标为成功，有同号 BlogPage 时甚至可能索引错误对象；不能在 typed source resolver 上线前产生 Article 搜索事件。
3. `ContentSearchScopeJob` 目前有模型和生产入口，但仓库中没有对应任务消费者。页面访问限制变化后可能长期不生成新 tombstone/upsert；MySQL `live().public()` 二次检查会挡住结果对象，却不能及时清除 ES 中的旧公开正文。
4. Delivery 处理 upsert 时若页面已不公开，会直接把该 Delivery 标为成功而不写墓碑。必须确保同一事务已有更高 generation 的 tombstone，或由消费者原子地请求状态重算，不能静默成功后依赖不存在的范围任务。
5. `query_content_search_page()` 先从 ES 取固定数量，再由 MySQL 剔除失效页面，不会继续向后补齐当前页；漂移增加时会出现短页或空页。
6. `ContentSearchResults.count()` 使用 ES 的原始 total，没有经过 MySQL 公开性校验。失效或受限文档会造成总数虚高，并可能泄露“存在某条不可见内容”的数量信息。
7. 联邦搜索每条来源只取前 100 个候选，但 `count()` 和分页器可以报告远大于 100 的总数；超过候选窗口的页可能为空或排序不完整，复合游标还可能跳过未返回的候选。
8. 每次内容查询都使用 `track_total_hits=True`。对千万级高频词做精确总数统计可能显著增加开销，应把普通结果页改为有界总数或关系值，仅在确有业务需求时单独执行精确统计。
9. 标题建议索引只提供 BlogPage 测试回填命令，没有与公开生命周期一致的类型化增量投递。若 Article 需要标题联想，必须把建议索引视为可重建投影，并处理发布、改名、取消发布和删除；若产品不需要，则明确只服务精选 BlogPage。
10. 未发现面向长期运行的已完成搜索事件归档策略；Delivery 物化仍逐事件 `get_or_create`。这与文章总量不直接等价，但在高发布吞吐下会增加 MySQL 往返和表增长，应先测量事件速率，再设计批量物化及“全部 required Delivery 终态 + 超过审计保留期”后的归档，不能未经授权删除 tombstone 审计。

短页修复应使用有限 over-fetch：按 search-after 继续取后续候选，直到填满页面、ES 无更多结果或达到受控扫描上限。不能为了填满 20 条结果无限扫描大量被 MySQL 拒绝的文档；拒绝比例超过阈值应触发一致性告警和投影修复。

联邦搜索第一阶段仍可保留“统一内容索引 + 普通 Wagtail Page 索引”两条流，但必须使用每来源游标和缓冲区完成稳定归并，去重键改为 `content_key`，不能再按裸 `pk` 去重。长期如果普通 Page 也需要稳定深分页和统一相关性，可把公开普通 Page 作为第三种 `aggregate_type` 投影到统一索引；这是后续优化，不是 Article 第一阶段的上线前提。

### 30. 后台搜索的企业边界

Wagtail Page Explorer 的 `q` 是页面标题自动补全，只搜索 Page 树中的对象。它不读取 `apps/search` 独立内容索引，也不搜索 `intro` 或 Mongo 正文。这个行为对精选 BlogPage 可以保留。

Article 后台应建立独立菜单和列表：

- 第一阶段支持精确 `article_id/source_key`、标题、状态、来源、时间范围和分类筛选；
- 标题模糊搜索走专用后台查询服务，不能在千万行 MySQL 上执行无前缀 `%LIKE%`；
- 默认只返回元数据，不读取 Mongo 正文；
- 若未来必须搜索草稿或非公开正文，新建与公开索引不同的私有物理索引和 read alias，并把角色/组织/可见范围写入可过滤安全字段；
- 私有索引查询仍要回 MySQL 做最终权限检查，审计管理员、查询条件和命中对象 ID，不记录正文；
- 公开索引不得通过开关变成后台草稿索引。

这样既不会让一千万 Article 压入 Wagtail Page 树，也不会扩大公开搜索索引的数据边界。

### 31. 搜索实施工作包细化

搜索部分建议拆成可独立回滚的六批，不能一次上线：

#### S0：修复现有一致性缺口

实现 ScopeJob 消费、非公开 upsert 的墓碑补偿、结果 over-fetch、总数语义和联邦 100 候选问题。先对现有 BlogPage 建立回归基线，不引入 Article。

#### S1：类型化身份与通用来源协议

增加 expand 字段/新 State 结构、`content_key`、source registry 和 BlogPage 适配器。旧索引和旧事件仍可运行，验证 ID 碰撞、旧事件重放和 tombstone 防复活。

#### S2：不可变正文版本接入

搜索事件携带 `body_version_id/body_sha256/projection_sha256`，Mongo reader 双读，影子比较旧正式正文与新版本正文的 hash/长度，不输出正文。

#### S3：Article Producer 与查询回填

Article 发布事务写同一 Search Outbox；Delivery、重建和查询支持 Article；引入 `SearchResultItem`，更新模板、API、标题建议和后台入口。

#### S4：新物理索引在线构建

创建新 mapping，开启新旧 Target 双投递，分别按 BlogPage/Article 固定上界回填，追平增量并执行类型化一致性校验。生产索引创建、回填和 alias 切换分别取得授权。

#### S5：观察与 contract

观察搜索延迟、短页率、MySQL 拒绝率、Outbox lag、死信、ES 版本落后和索引容量。完整回滚窗口与恢复演练通过后才停止旧身份写入；旧索引和旧数据清理另行授权。

### 32. 搜索测试与验收门禁

| 范围 | 必测场景 |
| --- | --- |
| 身份 | `blog_page:38` 与 `article:38` 同时存在且不冲突；旧裸 `page_id` 事件兼容 |
| 版本 | 旧 upsert 晚于新 upsert、旧 upsert 晚于 tombstone、正文不变但元数据变化、重复事件 |
| 权限 | 取消发布、访问限制、软删除、硬删除、权限变化；结果、总数、高亮均不泄露不可见内容 |
| 查询 | 相关性、日期排序、类型/语言/分类筛选、空正文、中文分词、短页补齐和受控停止 |
| 游标 | 下一页、上一页、PIT 过期、alias 切换、类型化稳定排序、相同分数与相同数值 ID |
| 联邦 | 内容与普通 Page 稳定归并、超过 100 条结果、总数语义、任一来源失败 |
| 回填 | 两来源独立 checkpoint、中断恢复、固定上界、增量追平、Bulk 限字节、Mongo/ES 短暂故障 |
| 建议 | BlogPage/Article 改名、发布、取消发布、删除、同名和 ID 碰撞 |
| 性能 | 百万到千万合成文档的 P50/P95/P99、吞吐、exact count 成本、ES heap/merge、MySQL 批量回查 |
| 回滚 | 查询开关回旧路径、read alias 回旧索引、旧 Consumer 处理兼容事件且不删除新旧数据 |

验收还必须证明每页搜索结果不读取 Mongo 正文、不产生 MySQL N+1、ES `_source` 不包含草稿指针或原始 StreamField 块、日志和 Outbox 不包含正文或凭据。

### 33. 本次搜索分析的数据、服务与回滚边界

本轮只读检查 `apps/search` 的模型、Producer、Delivery、文档构建、Mongo reader、ES client、重建、查询、联邦、建议、视图、模板、管理命令和测试。未修改运行代码、迁移、设置、`systemctl.md`、MySQL、MongoDB、Elasticsearch、Redis、Celery 或生产服务。

本次文档补充的回滚点是移除第 22 至 33 节及对应实施记录。任何 S0-S5 实施都需要单独方案、测试和生产授权；其中 MySQL DDL、ES 新索引创建/回填/alias 切换和存量数据处理不得由本文档视为已授权。

### 34. 搜索改造的模型/推理强度建议

- 只读绑定点统计、测试清单和文档整理：`gpt-5.6-luna` 中推理；
- S0 的现有缺陷修复、适配器和常规查询改造：`gpt-5.6-terra` 高推理；
- State/Outbox 在线迁移、跨库版本契约、千万级回填、alias 切换和生产回滚：`gpt-5.6-sol` 高到 xhigh 推理，并安排独立 review；
- 升级门槛：涉及生产 DDL、不可逆 contract、权限边界、正文泄漏、旧事件复活内容、跨服务恢复水位时升级；
- 验证门禁：定向单元/集成测试、合成容量压测、故障注入、只读一致性检查、新旧索引影子对比和精确生产授权。

### 2026-08-27：搜索影响专项分析完成

- 状态：完成 `apps/search` 与千万级 Article 方案的专项影响分析，未实施搜索代码。
- 实际修改文件：仅更新本文档。
- 事实依据：当前 mapping `v003`、`page_id` 身份、BlogPage State/Outbox/Delivery、external version、在线重建、MySQL 公开性回查、联邦结果、标题建议和 Wagtail Page Explorer 已逐项核对。
- 独立复核：`review` 角色完成只读审查，确认 typed identity 与来源解析是 Article 接入前置门禁，并补充同号对象误索引、联邦游标和高吞吐事件表风险。
- 结论：保留搜索底座；类型化身份、通用内容源、结果 DTO、分来源重建和标题建议需要兼容改造；Page Explorer 不接管海量 Article。
- 数据/服务影响：无；未创建索引、回填、切 alias、投递任务或重启服务，`systemctl.md` 无需更新。
- Git：工作区未提交；28、29 号说明书继续保持未跟踪状态。
- 回滚点：移除第 22 至 34 节及本记录。
- 残余风险：ScopeJob 无消费者、非公开 upsert 静默成功、短页/总数偏差和联邦 100 候选限制仍存在于当前代码，需 S0 获得实现授权后才能修复。

### 35. Wagtail 8.0 历史、草稿与 Mongo 正文兼容性核查

#### 35.1 核查范围与环境

- 测试环境使用 WSL2 的 `wagtailblog-test` Conda 环境，实际版本为 Django 5.2.8、Wagtail 8.0。
- 浏览器检查使用 `browser-skill` 的 `bsk`，没有使用 Playwright，也没有执行恢复、发布、保存、取消发布或删除。
- 只读检查的真实 BlogPage 为 `page_id=38`，标题为“初识Django”；访问地址为 `/admin/pages/38/history/`。
- 页面历史列表显示已发布、已保存草稿和当前草稿等日志，草稿动作包含“预览”“编辑”“与上一个版本进行比较”；预览入口可正常打开。

#### 35.2 Wagtail 8.0 的实际调用链

Wagtail 的历史页本身主要读取 MySQL 的 `PageLogEntry`、`Revision` 元数据，不需要读取 Mongo 正文。当前兼容链路如下：

```text
历史列表
  PageHistoryView -> PageLogEntry / Revision

预览
  PreviewRevision.get_revision_object()
  -> Revision.as_object()
  -> BlogPage.with_content_json()
  -> BlogPage.from_serializable_data()
  -> 按 mongo_draft_pointer 读取 Mongo 草稿正文

比较
  两个 Revision.as_object()
  -> EditHandler comparison
  -> 比较恢复后的 BlogPage 字段值

恢复
  旧 Revision.as_object()
  -> 生成新的 Revision
  -> 用户再次发布
  -> PublishPageRevisionAction
  -> BlogPage.save() / page_published
```

这说明 Wagtail 历史框架不要求正文必须存储在 MySQL `Revision.content.body` 中；它要求 `Revision.as_object()` 能够得到一个完整、可比较、可保存的页面对象。因此，保留 MySQL Revision 元数据并将正文替换为不可变 Mongo 版本指针是可行的，不能删除 Revision 或绕过 Wagtail 的恢复流程。

#### 35.3 当前实现的兼容点与缺口

当前 `BlogPage.serializable_data()` 将 MySQL Revision 的 `body` 固定保存为 `[]`，并写入 `mongo_draft_pointer`；`from_serializable_data()` 再通过指针读取 Mongo 草稿，缺失时回退到正式 Mongo 正文。这个设计可以解释现有历史页为什么能够打开，但也带来以下缺口：

1. Mongo 指针失效、正文暂时不可用、正文确实为空和正文反序列化失败，当前可能都降级为 `body=[]`。
2. 预览可能显示空正文；比较可能漏掉正文差异；恢复可能由旧版本生成一个空正文的新 Revision。
3. 相同正文复用 Mongo 快照指针时，删除某个 Revision 若直接物理删除快照，可能破坏其他 Revision 的历史读取。
4. 恢复旧 Revision 后再次发布时，必须保证旧版本正文不会覆盖更新版本的 MySQL 正式指针或搜索投影。

因此，历史列表无需重写，但正文读取、比较、恢复和删除引用管理必须改造。

#### 35.4 面向不可变正文版本的最小改造

建议按兼容迁移分阶段实施：

1. 在 Revision JSON 中新增 `mongo_body_version_id`、`body_sha256`、`body_schema_version` 和读取状态；过渡期继续写入 `mongo_draft_pointer`，新读路径优先使用不可变版本指针。
2. `from_serializable_data()` 返回可区分的结果：正文为空、快照不存在、Mongo 暂时不可用、结构无法反序列化。后台预览和比较遇到后三类必须显示明确错误或不可用提示，不能静默视为空。
3. 比较前分别加载两个 Revision 的正文版本；任一版本不可用时中止正文比较并提示历史快照不可用，不能报告“无变化”。
4. 恢复动作继续使用 Wagtail 的“旧 Revision 生成新 Revision”语义；新 Revision 可以引用旧的不可变正文版本，发布时只在 MySQL 事务内切换 `published_body_version_id` 并写 Search Outbox。
5. Revision 删除只删除 MySQL 引用或墓碑；Mongo 物理回收交给延迟 GC，只有确认没有 Revision、正式指针、备份或审计引用后才允许回收。
6. 增加一致性检查命令，至少检查 Revision 指针存在性、哈希、schema 版本、正式指针和搜索投影版本；命令只读并输出计数/ID，不输出正文。

#### 35.5 哪些 Wagtail 代码应保留，哪些代码需要适配

应保留：`PageHistoryView`、`PageLogEntry`、Wagtail `Revision.id`、用户/时间/审核元数据、权限校验、`Revision.as_object()` 调用模型、预览/比较/恢复 URL 和 `PublishPageRevisionAction`。

需要适配：`BlogPage.from_serializable_data()`、`BlogPage.serializable_data()`、表单初始化时的最新草稿恢复、历史预览的正文错误呈现、比较前的正文完整性校验、恢复/发布时的版本指针切换、Revision 删除信号及 Mongo GC。除非出现明确的性能证据，不修改 Wagtail 核心历史视图。

#### 35.6 浏览器验收与后续门禁

实施后必须用 `browser-skill` 在测试环境验证 BlogPage 的：历史列表、最新草稿预览、两个版本比较、恢复旧版本后生成新草稿、发布后正式正文和再次打开历史版本。测试数据必须使用专门的测试页面，禁止在生产页面执行恢复或发布。

验收至少包括：

- 正常快照能在预览和比较中显示正文；
- Mongo 快照缺失时后台明确显示“历史正文不可用”，不显示为正常空文章；
- 恢复旧版本不会修改旧 Revision，不会覆盖更高版本正文；
- 发布后 MySQL 正式指针、Mongo 正式版本、Search Outbox 的版本号一致；
- 删除一个 Revision 不会删除仍被其他 Revision 或正式正文引用的 Mongo 版本；
- 草稿、预览和历史正文不会进入公开搜索索引。

#### 35.7 测试库真实 Revision 的只读实测

本次使用测试库现有数据完成了两组互补核验，没有创建页面、保存草稿、发布、恢复、删除或修改 Mongo 文档。

**正常 ObjectId 指针链路（BlogPage 38）：**

- Page 38 当前共有 43 条 MySQL Revision。Revision 1059 的 `content.body` 是字符串 `[]`，`mongo_draft_pointer` 指向现存 Mongo 快照；该快照包含 12 个正文块。
- 访问 `/admin/pages/38/revisions/1059/view/` 后，页面显示完整正文、多个正文标题且可见文本非空。由于 MySQL Revision 不含正文，这直接证明预览通过 `Revision.as_object()` 和 `BlogPage.from_serializable_data()` 读取了 Mongo Revision。
- Revision 1042 与 1059 的 Mongo 正文哈希不同；访问 `/admin/pages/38/revisions/compare/1042...1059/` 后比较页出现 `Body` 变更行，证明正常 ObjectId 指针下比较流程能够取得 Mongo 正文。
- Revision 986、1041、1042 共用同一 Mongo 指针；全测试库还存在另外 9 组共享指针。这是当前“正文相同则复用最新快照”逻辑的真实结果，说明 Revision 删除不能直接物理删除其指针文档。

**字符串主键兼容故障（BlogPage 544-546）：**

- 测试库共有 157 条带 `mongo_draft_pointer` 的 Revision。147 条 ObjectId 指针可正常读取；另有 10 条使用 `rev_<page_id>_<uuid>` 字符串主键，涉及 Revision 947-956 和 Page 544-546。
- 这 10 份 Mongo 文档实际存在，不是物理丢失；但 `MongoManager.get_blog_revision_body()` 无条件调用 `ObjectId(content_id)`，因此把字符串主键判为非法并返回 `None`。
- Page 544 的 Revision 947-950 各自指向不同的 Mongo 正文哈希，但 `Revision.as_object()` 对四个版本都回退成同一份正式正文：恢复后均为 9 个块，哈希与 `blog_content` 正式正文完全相同。
- `/admin/pages/544/revisions/947/view/` 没有显示“历史正文不可用”，而是静默显示当前正式正文。`/admin/pages/544/revisions/compare/947...948/` 虽显示 `Body` 行，却没有增删标记，且比较对象来自同一份正式正文，不能表达两份真实历史快照的差异。

因此当前已经不是只有理论上的“缺失快照可能静默降级”：测试库存在可重复的 P0 兼容故障。第一修复门禁应是让 Revision 存储读取器按原始 `_id` 类型安全查询，至少兼容历史字符串主键和当前 ObjectId；随后再把“文档不存在、Mongo 不可用、ID 格式非法、正文为空、反序列化失败”拆成不同状态。修复前禁止对 Revision 947-956 执行恢复或基于其比较结果做内容判断。

本次没有找到“合法 ObjectId 指针但 Mongo 文档物理缺失”的天然样本，也没有通过删除快照进行故障注入；该分支仍需在取得实现授权后使用隔离测试数据验证。

本节结论：Wagtail 8.0 历史系统本身不需要大改，当前 Mongo 正文接入方式需要补齐“不可变版本指针、显式缺失错误、引用安全和发布代际校验”。在这些门禁完成前，不应清理旧 Mongo 快照、删除 MySQL `body` 字段或把千万级文章全部建成 Wagtail Page。

### 36. Wagtail 历史改造实施记录（2026-08-27）

- 状态：完成 Wagtail 8.0 源码链路核对和测试环境 BlogPage 38 的浏览器只读验收；未实施运行时代码改造。
- 实际修改文件：仅更新本说明书；未修改模型、迁移、模板、服务、数据库、MongoDB、Elasticsearch、Redis 或 Celery。
- 测试结果：Django shell 确认 Page 38 有 43 条 Revision，Revision 1059 从含 12 个块的 Mongo 快照恢复正文；`bsk` 确认历史列表有 85 条页面日志、1059 预览正文非空、1042 与 1059 的比较页包含正文变更。全库只读核验发现 10 条字符串主键快照被 ObjectId-only 读取器拒绝；Page 544 的 Revision 947 预览静默回退正式正文，947 与 948 的比较不能表达真实快照差异。未执行恢复、发布和删除路径。
- 浏览器工具：使用 `browser-skill`；没有生成 Playwright 截图、trace、视频、PDF、HAR 或报告。
- 环境检查：`python manage.py check` 返回 0 个问题；测试开发服务运行于 `0.0.0.0:8080`，未登录请求返回预期的后台登录重定向，登录态 `bsk` 验收已完成。启动时提示 1 个未应用的 `wagtailcore` 迁移，本次没有执行 `migrate`。
- 工具状态：所有 `bsk` session 已停止；CLI/浏览器扩展存在 protocol 1.0/1.1 漂移警告，但没有阻止上述只读页面核验。
- Git 状态：未提交；28、29 号说明书为未跟踪文件，本次没有修改运行时代码。`git diff --check` 无报错，29 号说明书独立空白检查和行尾空白扫描无诊断。
- 数据与服务影响：没有写入 MySQL、MongoDB、Redis 或 Elasticsearch，没有执行搜索重建、服务重启或 systemd 变更，`systemctl.md` 无需更新。
- 回滚点：移除第 35 至 36 节及本次记录即可回滚文档变更；运行时代码和数据无回滚动作。
- 残余风险：合法 ObjectId 快照物理缺失的提示尚未通过故障注入验证；恢复/发布后的版本代际和共享快照 GC 也未验证。这些必须在取得实现授权后用隔离测试数据完成。

### 37. 本阶段模型/推理强度实际使用

- 只读代码、Wagtail 8.0 调用链和文档整理：按建议使用常规开发档强度完成。
- 浏览器历史页验证：使用 `browser-skill` 完成最短只读路径，未扩大到生产操作。
- 尚未触发高风险实现升级；涉及 Revision 契约、跨库发布一致性、Mongo GC、搜索代际或生产迁移时，应升级为高推理并安排独立 review、故障注入和回滚演练。

### 38. 项目主线与子代理统筹记录（2026-08-27）

- 状态：完成整个项目的 M0-M7 唯一主线、WP/S 映射、owner/RACI、前置条件、交付物、验收门禁、feature flag 和生产授权边界整理。
- `architecture_review`：使用 `gpt-5.6-terra` 高推理，只读复核架构、依赖 DAG、Article 与 BlogPage 边界、namespace 和回滚；未修改文件。
- `django_backend_review`：使用 `gpt-5.6-terra` 中推理，只读复核 `BlogPage`、Mongo 读取器、信号、Wagtail 8.0 历史契约和后端测试工作包；未修改文件。
- `data_search_ops_review`：使用 `gpt-5.6-luna` 中推理，只读复核 MySQL/Mongo/ES 数据模型、Outbox、重建、容量、备份、监控和 systemd 发布门禁；未修改文件。
- 主 agent 负责冲突裁决和文档集成：将三方建议统一为“先 P0 读取/删除风险，再正文版本与发布，再搜索，再 Article，最后 GC/收缩”，没有授权任何生产操作。
- 实际修改文件：仅本说明书；未修改运行时代码、迁移、数据库、MongoDB、Elasticsearch、Redis、Celery、服务单元或 `systemctl.md`。
- 验证：`git diff --check` 无报错；文档行尾空白扫描为 0；未执行迁移、回填、索引重建、GC、发布或删除。
- 回滚点：删除第 0.8 节和本节即可回滚本轮统筹整理；不会影响前述事实记录和运行时系统。
- 残余风险：M1 错误状态在 Wagtail 8.0 后台如何呈现仍需隔离测试确认；Article 多租户/权限/保留策略和生产容量仍待业务确认，不能以本计划替代实现、压测、备份恢复和生产授权。

### 39. Wagtail 8.0 特有生命周期边界补充（2026-08-28）

- 状态：根据 Wagtail 8.0 的 Workflow、预约发布、`page_published` 和 `with_content_json()` 契约补充方案；仍为文档设计，未修改运行时代码或数据。
- Workflow：审批快照冻结 `revision_id`、`body_version_id`、hash、schema 和 generation；实际发布再次锁定并校验，任何漂移都转为需重新审核，不自动发布。
- 预约发布：明确 `PublishPageRevisionAction` 调度链与 Outbox Worker 是两个异步执行上下文；到期前后均做版本校验，发布后缺 Outbox 由对账任务补偿，重复任务以幂等键吸收。
- `page_published`：receiver 限定为通知、缓存/搜索唤醒和对账提示，不执行 Mongo 写入/删除或正式指针推进；发布服务完成 Wagtail 发布后再切换指针并写 Outbox。
- 序列化：`serializable_data()`、`from_serializable_data()` 必须通过 `with_content_json()`/`Revision.as_object()` 契约测试，兼容 ObjectId 与历史字符串指针；正文缺失、Mongo 不可用、hash/schema 不符不得静默回退到正式正文。
- 验收门禁：新增 Workflow 漂移、定时重复/取消、发布后崩溃、信号无 Outbox、序列化往返和历史字符串主键场景；Wagtail 历史列表、权限和 URL 保持不变。
- 实际修改文件：仅本说明书；未修改模型、迁移、模板、服务、搜索、数据库、MongoDB、Redis、Elasticsearch、Celery、systemd 或 `systemctl.md`。
- 验证与影响：文档变更后需执行 `git diff --check` 和 Markdown 空白扫描；本轮无数据库写入、无迁移、无发布、无服务重启。回滚只需移除本节及 8.3.1-8.3.5，不影响运行时行为。
- 残余风险：Wagtail 8.0 核心事务边界和 `body` 字段实际 JSON 类型仍需在实现阶段以隔离测试固定；未授权前不得通过故障注入删除 Mongo 快照，也不得在生产执行迁移或历史恢复。

### 40. M1/P0 实施批次：历史 Revision 正文兼容与显式失败（2026-08-28）

#### 40.1 目标、非目标与范围

目标是修复已在测试库 Page 544-546、Revision 947-956 复现的字符串 `_id` 指针读取失败，并消除“Revision 草稿快照读取失败后改读当前正式正文”的错误历史语义。历史预览、比较和恢复必须只使用该 Revision 指向的正文；没有该正文时应明确失败，不能显示另一版本的内容。

本批只修改 `wagtailblog3/mongo.py`、`wagtailblog3/apps/blog/models.py`、现有博客中间件、管理端错误模板及其定向测试；不修改 MySQL schema、Mongo 存量数据、Wagtail 核心、页面 URL、Workflow、定时发布、信号删除逻辑、搜索 Outbox、Celery 或服务配置。`body='[]'` 的实际 Wagtail JSON 兼容形态保留，待独立契约测试和版本化正文批次处理。

#### 40.2 实施设计

1. Mongo Revision 读取器按输入 `_id` 的真实 BSON 类型查询：有效 ObjectId 使用 ObjectId，其他非空字符串使用字符串 `_id`；不把历史 `rev_<page>_<uuid>` 转换为 ObjectId。
2. 读取器对“空指针、非法输入、文档不存在、Mongo 查询异常、文档缺 body”提供调用方可区分的失败结果或受控异常，并且日志不得输出正文。
3. `BlogPage.from_serializable_data()` 检测到 Revision 含 `mongo_draft_pointer` 键时，只从该指针恢复正文；读取失败时抛出稳定的历史正文不可用错误，不得回退到 `mongo_content_id` 的当前正式内容。空字符串指针同样属于损坏 Revision，而不是无指针旧数据。
4. 没有 `mongo_draft_pointer` 的遗留 Revision 由 Wagtail 从该 Revision 的 MySQL JSON body 还原，不能覆盖成当前正式 Mongo 正文。
5. 编辑页“最新草稿”与历史预览使用相同边界：最新 Revision 明确带指针但正文不可用时阻止表单初始化；存在无指针 Revision 时保留其既有 MySQL body/空状态，只有从未保存 Revision 的页面才可读取正式 Mongo 内容。管理端受控错误页只处理历史/恢复路径，保证不误显示、不误保存。

#### 40.3 验收与回滚

- ObjectId 指针仍能读取同一份 Revision 正文。
- 字符串 `_id` 指针能读取其对应快照，且不尝试 ObjectId 转换。
- 有指针但快照不存在或无正文时，`Revision.as_object()` 明确失败；不得返回正式 Mongo 正文。
- 无指针的历史 Revision 保持其 MySQL 历史正文，不读取当前正式 Mongo 正文。
- 必须运行目标单元测试、`compileall`、`python manage.py check`、`git diff --check`；不执行 Mongo 删除、恢复、迁移或生产操作。
- 回滚为还原上述两个运行时文件和定向测试；本批不产生 schema 或数据变更。

#### 40.4 模型/推理强度实际调度

- `gpt-5.6-sol` 高推理：只读复核 Wagtail Revision 契约、跨库边界和错误传播，禁止编辑。
- `gpt-5.6-terra` 高推理：实现 Mongo 读取器这一单文件边界，遵守运行时代码中文注释与类型门禁。
- `gpt-5.6-luna` 中推理：只读盘点现有测试夹具、验证命令和回归场景。
- 主 agent：集成 `models.py` 行为、审查 diff、执行 WSL2 定向测试并维护实施记录。若发现 Wagtail 8 异常处理要求变更 URL/视图或需要迁移、删除/修复 Mongo 数据，则停止本批并升级为 `sol` 高推理专项方案。

### 41. M1/P0 实施记录：历史 Revision 正文读取修复（2026-08-28）

- 状态：完成测试环境代码实现和只读后台验收；未执行存量数据修复、Mongo 删除、迁移、发布或生产操作。
- 实际修改文件：`wagtailblog3/mongo.py`、`wagtailblog3/apps/blog/models.py`、`wagtailblog3/apps/blog/middleware.py`、`wagtailblog3/templates/blog/admin/revision_body_unavailable.html`、`wagtailblog3/apps/blog/test_mongo.py`、`wagtailblog3/apps/blog/test_revision_body_errors.py`、`wagtailblog3/apps/blog/test_markdown_compat.py`、`wagtailblog3/apps/search/tests/test_lifecycle_baseline.py` 和本说明书。
- 读取器：支持 BSON ObjectId、ObjectId 字符串和历史字符串 `_id`；将历史 Mongo `body` 的合法 JSON 字符串规范化为列表，空列表仍是合法历史正文。空/损坏指针、快照缺失、缺 body、非法 JSON、非列表正文和 Mongo 不可用均提供稳定 code 与是否可重试标记；日志不记录正文或指针。
- Revision 边界：带 `mongo_draft_pointer` 键的 Revision（包括空字符串）只能读取该快照；校验快照 `page_id` 归属和块结构，失败时禁止读当前正式正文。无指针 Revision 保留 Wagtail/MySQL 历史 body，不被当前正式 Mongo 正文覆盖。编辑表单按同一规则拒绝损坏最新草稿，避免空正文保存。
- 管理端：Wagtail 8.0 核心预览、比较和恢复没有捕获该领域异常，因此项目中间件仅在后台页面路径将不可恢复故障转换为 HTTP 409、Mongo 暂不可用转换为 HTTP 503；模板使用 `role="alert"`、返回历史链接和可重试提示。该响应在构造 Revision 对象阶段中断，POST 恢复不会进入保存流程。
- 测试：WSL2 `wagtailblog-test` 执行 `python manage.py test --keepdb blog.test_mongo blog.test_revision_body_errors blog.test_markdown_compat search.tests.test_lifecycle_baseline`，60 项通过。认证管理员的真实 Wagtail 后台预览、比较和恢复 POST 在缺失快照时均返回 409，恢复 POST 前后 Revision 数量不变。预期模拟错误会产生分类日志；测试环境仍有 MySQL 不支持条件唯一约束的既有 `WorkflowState` 警告。
- 其他门禁：`python manage.py check` 为 0 问题；`python manage.py makemigrations --check --dry-run` 为 `No changes detected`；目标文件 `compileall`、`git diff --check` 和 `git diff --cached --check` 通过。`python manage.py migrate --plan` 仅列出测试环境既有、未应用的 `wagtailcore.0098_apitoken`，本批未执行 `migrate`。认证后台集成测试通过 `STORAGES` 临时切换到开发静态存储渲染受控错误页，未执行 `collectstatic`。新增文件和本说明书没有行尾空白；`models.py`、`mongo.py` 原有行尾空白未顺手格式化，且 diff 门禁未报告本批新增行问题。
- 浏览器验收：使用 `browser-skill` 仅访问 `/admin/pages/544/revisions/947/view/`。修复前该历史版本显示受控 409；兼容历史 JSON 字符串正文后，预览显示该快照的“先保存草稿”，未显示当前正式正文。未点击恢复、发布、取消发布、编辑、保存或删除；session 已停止，未生成 Playwright 产物。
- 数据与服务影响：测试仅使用 Django 测试数据库和内存 Mongo 替身；没有写入测试真实 Mongo 正文、Redis、Elasticsearch、MinIO 或生产环境。没有迁移、搜索重建、Celery 投递、systemd 变更或服务重启，`systemctl.md` 无需更新。测试开发服务器仅用于验收，地址为 `http://192.168.20.5:8080`。
- Git 与回滚：工作区未提交；本批可通过还原上述运行时文件、模板和测试回滚，不涉及 schema 或数据回滚。
- 残余风险：单条 Revision 删除和页面删除仍会直接处理 Mongo 指针，尚未解决共享 pointer 与跨库事务风险；不可变正文版本、Workflow/预约发布 generation、Outbox 对账、搜索投影和历史页面正文不可用的运营告警仍在后续 M2+ 工作包，不能因本批读取修复而视为完成。

### 42. M2/WP1 删除清理意图与跨库回收（2026-08-28）

#### 42.1 目标与边界

本批只处理 BlogPage、Wagtail Revision 删除时的 Mongo 清理一致性，不改变正文读取、发布、搜索投影或 Wagtail 历史页面协议。删除请求必须先在 MySQL 事务中记录可审计、可重试的 cleanup intent；事务回滚时不得触碰 Mongo，事务提交后才允许异步消费者执行物理删除。

#### 42.2 清理意图契约

每条意图至少包含 `intent_id`（幂等键）、`page_id`、`revision_id`（可空）、`mongo_id`、`kind`（page/revision）、`status`（pending/running/succeeded/failed）、`attempts`、`next_attempt_at`、错误分类和时间戳。页面删除为页面正文与该页历史快照分别记录意图；Revision 删除只记录其指针。写入使用 `transaction.on_commit` 唤醒既有 maintenance 队列，唤醒失败不影响已提交意图。

#### 42.3 引用保护与幂等

消费者删除前必须重新查询 MySQL：只有当目标 Mongo 指针不再被正式页面、任一 Revision、备份或其他保留引用使用时才物理删除。共享 pointer 只能在最后一个引用消失后回收。重复消费、超时重试和“已不存在”均收敛为成功；Mongo 暂不可用保留 pending/failed 并按退避重试，不能删除 MySQL 历史记录来掩盖失败。

#### 42.4 Wagtail 8.0 删除顺序

`pre_delete` 仅登记意图，不进行 Mongo I/O；页面和 Revision 的 MySQL 删除继续由 Wagtail 管理。级联删除产生的多个意图使用稳定幂等键去重。单 Revision 删除、页面删除和子树级联都必须覆盖；外层事务回滚测试需证明 Mongo mock 未收到删除调用。消费者完成后保留审计结果，后续再接入统一 GC/监控。

#### 42.5 实施记录

- 状态：M2/WP1 实施中；先完成模型/服务/信号与定向测试，再评估是否需要迁移和 maintenance worker 接入。
- 不执行：不应用迁移、不删除真实 Mongo 数据、不投递生产队列、不重启服务、不修改 `systemctl.md`，除非后续明确授权且完成备份、影响评估和回滚演练。
- 验收门禁：定向删除生命周期测试、`python manage.py check`、`makemigrations --check --dry-run`、`compileall`、`git diff --check`；若新增表只生成迁移并检查 `migrate --plan`。

### 43. M2/WP1 实施记录（2026-08-28）

- 状态：测试环境代码实现完成，未提交、未发布；未应用迁移。新增 `blog.MongoCleanupIntent` 及迁移 `0029_mongo_cleanup_intent.py`，页面/Revision 删除信号只写意图，事务提交后唤醒 `maintenance` 任务。
- 实际修改：`blog/models.py`、`blog/signals.py`、`blog/tasks.py`、`mongo.py`、`blog/test_mongo_cleanup_intent.py` 及本说明书。页面删除方法已移除提交后的同步 Mongo I/O；Revision 查询按 Wagtail 8.0 GenericRelation 的 `content_type/object_id` 契约处理。
- 清理语义：意图按目标 pointer 去重；worker 在删除前检查正式页面引用和 BlogPage Revision 指针引用，共享指针转为 RETRY 并延迟重试；Mongo 异常进入 RETRY/DEAD，字符串 `_id` 与 ObjectId 均可删除，已不存在按幂等成功处理。
- 验证：WSL2 `wagtailblog-test` 执行 `python manage.py test --keepdb blog.test_mongo blog.test_revision_body_errors blog.test_markdown_compat blog.test_mongo_cleanup_intent search.tests.test_lifecycle_baseline`，64 项通过；`python manage.py check`、`makemigrations --check --dry-run`、目标 `compileall` 和 `git diff --check` 通过。测试环境保留既有 `WorkflowState` 条件唯一约束警告。
- 数据/服务影响：未写入真实 Mongo 正文、Redis、Elasticsearch 或生产数据；未执行 `migrate`、Mongo 删除、搜索重建、Celery 生产投递、systemd 修改或服务重启，`systemctl.md` 无需更新。迁移仅生成，待独立授权后在目标环境应用。
- 未覆盖与风险：当前仍缺少统一 Beat 扫描 RETRY 意图、严格 lease/并发 claim、备份引用表和生产级 GC 审计；worker 的 Python 解析会随 Revision 总量增长，后续需索引化元数据或独立引用表。上述不阻塞本批删除安全边界，但在生产启用异步清理前必须补齐并压测。
- 回滚：在未应用 `0029` 前可删除本批代码/迁移并恢复原信号；若未来已应用迁移，回滚只允许停止消费者并保留意图表，不删除 Mongo 正文或历史快照。

### 44. M2B 清理任务补偿与租约实施记录（2026-08-28）

- 状态：完成测试环境实现；在 `0029` 基础上新增未应用迁移 `0030_mongocleanupintent_leases`。本批复用现有 maintenance Worker 和 Beat，不新增队列、unit、端口或环境文件。
- 实际修改：`blog/models.py`、`blog/tasks.py`、`blog/test_mongo_cleanup_intent.py`、`settings/database.py`、`systemctl.md`、迁移 `0030` 及本说明书。意图新增 `processing`、owner、到期租约和回收计数；任务使用 MySQL 行锁认领，并只允许匹配 owner 的 worker 写回结果。
- 补偿：`dispatch_pending_mongo_cleanup_retries` 每 60 秒只扫描到期 `pending/retry` 意图，投递到 maintenance；同时回收崩溃 worker 遗留的过期租约。Broker 唤醒异常只记日志，下一次 Beat 扫描补偿，不回滚已经提交的删除意图。
- 验证：WSL2 `wagtailblog-test` 执行 `python manage.py test --keepdb blog.test_mongo_cleanup_intent blog.test_mongo blog.test_revision_body_errors blog.test_markdown_compat search.tests.test_lifecycle_baseline`，67 项通过；`check`、`makemigrations --check --dry-run`、目标 `compileall`、`git diff --check` 和 `git diff --cached --check` 通过。
- 数据/服务影响：未应用 `0029/0030`、未运行真实 Celery worker/Beat、未删除测试或生产 Mongo 数据、未变更服务状态。`systemctl.md` 已补充未来部署时的 Worker 注册、Beat 日志、依赖、重启和回滚门禁。
- 回滚与风险：发布前可直接放弃未应用迁移和代码；已应用后先停止/回退 Worker 与 Beat，并保留所有意图审计行和 Mongo 正文。若 Mongo 物理删除成功而 MySQL owner 状态写回失败，租约到期后可能再次调用删除接口，依赖 Mongo 的“已不存在即幂等成功”语义；高吞吐量下 Revision TextField 逐行解析引用仍需在后续版本化引用表工作包中替换。

### 45. M3/P1 不可变正文版本兼容层（2026-08-28）

#### 45.1 目标、非目标与执行边界

目标是建立 BlogPage 的不可变 Mongo 正文版本身份，并让新的 Wagtail Revision 携带可审计的版本元数据。新正文版本不得原地覆盖；同一正文内容在同一页面重复保存时可按哈希复用同一版本。M3 不增加 BlogPage MySQL 列，版本指针、哈希和 schema 元数据暂存于 Revision JSON；旧 `mongo_content_id` 与旧读路径在兼容期保留。

本批不切换 `published_body_version_id`、不改变 `BlogPage.publish()`/Workflow/预约发布时序、不推进 `publication_generation`、不改搜索 Outbox payload、不回填或删除存量 Mongo 正文，不应用迁移。上述发布编排属于 M4，公开搜索投影属于 M5。

#### 45.2 最小设计

新 Mongo 集合 `content_body_versions` 按 `(aggregate_type, aggregate_id, body_sha256, body_schema_version)` 唯一保存 `body_version_id`、`body_sha256`、`body_schema_version`、正文和创建时间；`body_version_id` 另设全局唯一索引。仓储必须使用 insert-once/幂等读取，禁止 `$set` 修改既存版本。BlogPage 新建草稿 Revision 时写入 `mongo_body_version_id`、`body_sha256`、`body_schema_version`，且 `from_serializable_data()` 优先读取该版本；旧 Revision 继续使用 `mongo_draft_pointer`。

本批只允许新字段双写与双读影子校验：新版本写入失败不得静默把错误版本伪装为正式正文；旧页面继续依赖 `mongo_content_id`。版本化正文的正式公开切换必须等待 M4 在 MySQL 事务和 Outbox 中实现。

#### 45.3 分工、模型与验证

- `arch/review`：`gpt-5.6-sol` 高推理，只读复核 Mongo 幂等键、Wagtail Revision 契约、并发和回滚边界。
- `backend`：`gpt-5.6-terra` 中高推理，实现独立版本仓储、BlogPage 双写/双读和隔离测试；开始前阅读第 22 号代码注释方案。
- `qa/data`：优先 `gpt-5.6-luna` 中推理盘点测试矩阵、迁移计划和无存量数据操作证据；不可用时由主 agent 完成等价只读检查。
- 门禁：版本仓储不变性、同内容幂等、不同内容生成独立版本、Revision 往返、旧 pointer 兼容、Mongo 故障和 MySQL 回滚测试；`check`、`makemigrations --check --dry-run`、`migrate --plan`、`compileall`、`git diff --check`。

#### 45.4 回滚与残余风险

未应用迁移前可停止新双写并保留旧 `mongo_content_id` 读路径；Mongo 新版本仅会形成安全孤儿，禁止在本批回收。已应用迁移后的回滚只停用新写/读开关并保留版本文档和指针，不得回写旧正文。生产数据回填、Mongo 分片、保留期 GC、公开指针、Workflow、定时发布、Outbox generation 和搜索消费者契约均需后续独立授权。

### 47. M4.1 发布候选状态与正文校验实施记录（2026-08-28）

- 状态：完成最小兼容层实现，未接入 `Revision.publish`、Workflow、定时发布或搜索消费者；未提交、未发布、未应用迁移。
- 实际修改：新增 `blog.BlogPublicationState` 及迁移 `0031_blogpublicationstate.py`；新增 `blog.services.publication.BlogPublicationService.lock_and_validate_revision`，在 MySQL 事务内锁定页面和 Revision，从 Revision JSON 提取 `mongo_body_version_id`、`body_sha256`、`body_schema_version`，调用 Mongo 版本读取接口并校验正文；新增 `blog/test_publication_service.py` 覆盖元数据缺失、Mongo 版本缺失、成功写入和外层事务回滚。
- 状态字段：保存 draft/published 正文版本三元组、`publication_generation`（本批不递增）及 approved Revision 元数据；不保存正文内容，不替换 Wagtail 正式发布指针。
- 验证：`python manage.py test --keepdb blog.test_publication_service blog.test_mongo_body_versions blog.test_markdown_compat blog.test_mongo blog.test_revision_body_errors blog.test_mongo_cleanup_intent search.tests.test_lifecycle_baseline` 共 78 项通过；`check`、`makemigrations --check --dry-run`、目标 `compileall`、`git diff --check` 和 `git diff --cached --check` 通过。`migrate --plan` 仅查看计划，未执行迁移。
- 数据/服务影响：未写入真实 MongoDB、MySQL 业务数据、Redis、Elasticsearch 或消息队列，未重启服务；迁移 `0031` 需后续独立授权和备份后应用。
- 回滚边界与残余风险：应用迁移前可删除本批代码和迁移；应用后回滚需先停用调用方并保留状态表。正式发布指针切换、generation 并发围栏、Workflow/定时发布协调、Outbox 与版本 GC 留待后续 M4/M5。

### 48. M4.2 BlogPage 发布前正文校验实施记录（2026-08-28）

- 状态：完成普通 `BlogPage.publish(revision, ...)` 的前置校验，未接入 Workflow、定时发布、`page_published`、Search Outbox 或正式正文指针切换；未提交、未发布、未应用迁移。
- 实际修改：`BlogPage.publish` 在 `super().publish` 前调用 M4.1 发布服务；复用 `blog.models.MongoManager` 注入边界，确保生命周期测试替身与页面保存使用同一 Mongo 适配器。校验失败直接抛出，Wagtail 页面发布字段不会被修改。
- 验证：`blog.test_publication_service` 7 项通过，联合 `search.tests.test_lifecycle_baseline` 与 `search.tests.test_search_sync_producer` 共 24 项通过；覆盖普通发布成功、Mongo 正文缺失时页面/Revision/状态不变及外层事务回滚。`check`、迁移检查、`compileall`、`git diff --check` 通过。
- 数据/服务影响：未写入真实业务数据，未执行 `migrate`、Celery、搜索投影或服务重启；迁移计划仍仅包含待授权的 `0029`-`0031`。
- 回滚边界与残余风险：删除本批 `BlogPage.publish` 接入即可回到 M4.1；正式指针和 generation 尚未切换，Wagtail `Revision.publish` 在 `as_object` 阶段仍可能先触发历史正文读取异常，Workflow/定时发布尚需复用同一校验契约。

### 46. M3/P1 实施记录（2026-08-28）

- 状态：完成测试环境兼容层实现，未提交、未发布、未应用迁移。
- 模型实际使用：架构复核由 `gpt-5.6-sol` 高推理完成；实现由 `gpt-5.6-terra` 高推理完成；主 agent 负责契约修正、测试集成和最终门禁。未调用外部模型传输源码、凭据或生产数据。
- 实际修改：`wagtailblog3/mongo.py` 新增 `content_body_versions` 插入一次仓储、规范化 JSON SHA-256、聚合身份/哈希/schema 唯一索引和严格读取校验；`wagtailblog3/apps/blog/models.py` 的 Revision 序列化双写 `mongo_body_version_id`、`body_sha256`、`body_schema_version`，反序列化和后台编辑表单优先读取不可变版本，旧 `mongo_draft_pointer` 保持兼容；新增 `blog/test_mongo_body_versions.py` 隔离测试，并为搜索生命周期测试替身补齐同一版本仓储契约。
- 验证：WSL2 `wagtailblog-test` 执行 M3/M1/M2/生命周期综合测试 74 项通过；新增 schema 版本隔离和后台表单新版本优先读取覆盖；`python manage.py check`、`makemigrations --check --dry-run`、目标 `compileall` 和 `git diff --check` 通过。
- 数据/服务影响：未写入真实 Mongo/MySQL/Redis/Elasticsearch，未执行迁移、搜索重建、Celery 投递或 systemd 操作；本批无 MySQL schema 变化，不生成迁移。
- 回滚与残余风险：可停用新字段双写/读取并保留旧指针路径；已插入的不可变版本只能作为安全孤儿保留，禁止本批 GC。正式发布指针、Workflow/定时发布 generation、搜索投影和版本引用索引仍属于 M4/M5，未在本批解决。

### 49. M4.3 Wagtail 8.0 Workflow 审批 Revision 围栏实施记录（2026-08-28）

- 状态：完成测试环境 Workflow 完成动作接入与审批 Revision 二次校验，未执行生产 Workflow 配置、迁移、发布或服务重启。
- 架构事实：Wagtail 8.0 默认 `publish_workflow_state` 会读取页面最新 Revision；审批完成后若产生新草稿，可能绕过已审批正文。本批通过 `WAGTAIL_FINISH_WORKFLOW_ACTION` 指向 `blog.services.publication.finish_workflow_action`，在 `WorkflowState.finish()` 的事务内锁定 WorkflowState，读取最终成功 TaskState 绑定的 Revision，并拒绝其与页面最新 Revision 不一致。
- 正文校验：完成动作调用 `BlogPublicationService.lock_and_validate_revision`，重新校验 `mongo_body_version_id`、SHA-256、schema 版本及 Mongo 正文归属；成功后仅发布精确审批 Revision，并保存 approved Revision 与正文版本元数据。正文缺失、篡改或 Revision 漂移会抛出受控异常，事务回滚且不切换正式页面。
- Wagtail 边界：`workflow_approved` 仍只作为通知/对账触发点，不承担 Mongo 写入；BlogPage 未新增继承层，复用 Wagtail 8.0 已提供的 WorkflowMixin 能力。非 BlogPage Workflow 继续回退 Wagtail 默认完成动作。
- 实际修改文件：`wagtailblog3/settings/base.py`、`wagtailblog3/apps/blog/services/publication.py`、`wagtailblog3/apps/blog/models.py`、`wagtailblog3/apps/blog/migrations/0031_blogpublicationstate.py`、`wagtailblog3/apps/blog/test_publication_service.py`、`wagtailblog3/apps/blog/test_workflow_publication.py` 及本说明书。迁移文件仅生成，未应用。
- 验证：WSL2 `wagtailblog-test` 执行 Workflow/发布定向测试 10 项通过；M1-M4.3 与搜索生命周期综合测试 91 项通过。`python manage.py check` 通过；`makemigrations --check --dry-run` 输出 `No changes detected`；`migrate --plan` 仅查看计划；`python -m compileall -q wagtailblog3` 和 `git diff --check` 通过。测试中保留 Wagtail `WorkflowState` 条件唯一约束的 MySQL 警告。
- 数据与服务影响：未写入真实业务 MySQL/MongoDB、Redis、Elasticsearch、MinIO 或消息队列；未运行真实 Celery/Beat，未修改 `systemctl.md` 服务单元。测试使用隔离数据库和内存 Mongo 替身。
- 回滚边界与残余风险：未应用迁移前可移除完成动作配置和新增服务逻辑，恢复默认 Wagtail Workflow 行为；若未来应用 `0031`，回滚必须先停用调用方并保留状态表。预约发布仍需单独验证 Workflow 关联和精确 Revision；公开正文指针切换、`publication_generation`、Search Outbox 幂等字段、审批后编辑自动重新审核和版本 GC 尚未完成。
- 模型/推理实际调度：`gpt-5.6-sol` 高推理完成 Wagtail 8.0 Workflow/事务边界复核；`gpt-5.6-terra` 高推理实现完成动作与服务接入；`gpt-5.6-luna` 中推理盘点测试矩阵与门禁；主 agent 修正并发编辑错误、补齐迁移字段、集成测试并执行最终验证。Context7 未提供 Wagtail 8.0 准确资料，本批以测试 Conda 环境中实际安装的 Wagtail 8.0 源码为准。

### 50. M4.4 Wagtail 8.0 定时发布 Revision 校验实施记录（2026-08-28）

- 状态：完成测试环境定时发布前置校验，未执行真实 `wagtail publish_scheduled`、迁移、生产发布或服务重启。
- Wagtail 8.0 定时命令按 `Revision.approved_go_live_at < now` 查询并调用该 Revision 的 `publish(log_action="wagtail.publish.scheduled")`。BlogPage 对该日志动作增加到期标记校验，再复用 M4.1 的 Mongo 不可变正文版本校验。
- 定时发布允许指定 Revision 不是页面最新 Revision，以保持 Wagtail 预约发布语义；页面存在更新草稿时仍只发布已批准且已到期的 Revision，不自动改发最新草稿。
- 未批准、未到期、Mongo 正文缺失或版本元数据损坏时，发布在 Wagtail 切换页面前失败，正式页面状态保持不变。重复执行依赖 Wagtail 到期标记被清理后的既有幂等行为；跨服务搜索投影幂等留待 M4.5。
- 实际修改：`wagtailblog3/apps/blog/models.py`、`wagtailblog3/apps/blog/services/publication.py`、`wagtailblog3/apps/blog/test_publication_service.py` 及本说明书。未新增模型字段或迁移。
- 验证：定向发布/Workflow/正文/清理/搜索生命周期测试 43 项通过；`check`、`makemigrations --check --dry-run`、`compileall`、`git diff --check` 通过；`migrate --plan` 仅查看，未应用 0029-0032。
- 模型/推理实际调度：架构复核使用 `gpt-5.6-sol`（外部服务本轮不可用，结论以测试 Conda 中 Wagtail 8.0 源码为准）；实现使用 `gpt-5.6-terra` 高推理；QA 由主 agent 按既定 `gpt-5.6-luna` 场景完成回归。未向外部工具发送源码、凭据或生产数据。
- 回滚与残余风险：移除定时日志动作分支即可恢复 M4.3 行为；若未来启用生产定时任务，必须先确认迁移已应用、Mongo 可用、任务失败重试与 Outbox 补偿策略。定时发布与 Workflow 审批关联尚未建立强制约束，Search Outbox generation、正式指针切换和版本 GC 仍未实施。

### 51. M4.5 Search Outbox 正文版本与公开代际围栏实施记录（2026-08-28）

- 状态：完成测试环境搜索事件的兼容扩展，未应用迁移、未重建 Elasticsearch、未切换生产 alias、未操作生产数据或服务。
- 数据契约：`ContentSearchState` 与 `ContentSearchOutbox` 新增可空 `body_version_id`、`publication_generation`；保留原 `content_version`、`mongo_content_id` 和 `content_hash`。新增迁移 `search.0006_contentsearch_generation_fields` 仅包含扩展字段，未回填存量事件。
- 生产者：发布/取消发布/删除事件优先读取 `BlogPublicationState.published_body_version_id` 与 `publication_generation`；状态不存在或尚未切换时显式回退旧字段，不伪造新的正文版本身份。墓碑事件同样携带可用代际和正文身份，但不包含正文。
- 消费者：Delivery 在读取 State 和 ES 写入前同时比较正文版本身份与公开代际；旧事件、缺失身份事件不能覆盖已有新代际，旧代际标记 `SUPERSEDED`，未来代际进入重试。ES external version 优先使用 `publication_generation`，为空时兼容 `content_version`。
- 索引与重建：正式搜索文档、墓碑、mapping 字段白名单、批量重建和只读一致性检查均携带新字段；旧索引/旧文档缺失字段按兼容路径处理，不把旧数据猜测为新正文版本。新 mapping 版本为 `v003`，需后续通过独立索引构建和 alias 切换流程启用。
- 实际修改：`wagtailblog3/apps/search/models.py`、`services/outbox.py`、`services/delivery.py`、`services/document.py`、`services/elasticsearch.py`、`services/content_index.py`、`services/rebuild.py`、`services/consistency.py`、`migrations/0006_contentsearch_generation_fields.py`、搜索测试替身和 `tests/test_search_generation.py`。
- 验证：新增代际测试、内容索引和 ES 单元测试共 31 项通过；`python manage.py check`、`makemigrations --check --dry-run`、`compileall`、`git diff --check` 通过；`migrate --plan` 仅查看并列出待授权的 blog.0029-0032 与 search.0006。完整旧搜索回归未在持久 `--keepdb` 库执行，原因是共享库尚未包含新列；尝试临时测试库时发现同名数据库已存在且 Django 需要交互确认，未删除或重建该库。
- 模型/推理实际调度：`gpt-5.6-sol` 高推理完成搜索幂等、代际和回滚边界审查；`gpt-5.6-terra` 高推理实现跨 State/Outbox/Delivery/ES 链路；`gpt-5.6-luna` 原计划 QA 因协作服务异常未完成，主 agent 执行新增测试与门禁。未向外部模型传输源码、凭据、正文或生产日志。
- 回滚与残余风险：应用迁移前可停止新字段双写和消费者围栏，保留旧 `content_version` 路径；应用迁移后回滚需保留新增列和 Outbox 审计，先停新消费者并回切旧索引 alias，禁止删除 Mongo 正文。正式 `published_body_version_id`/generation 切换、ES 新索引构建、历史事件回放、生产容量压测和版本 GC 仍未完成。

### 52. M4.6 发布后一致性、取消发布墓碑与只读对账（2026-08-28）

- 状态：完成测试环境第一子批，先建立发布后 State/Outbox 同事务集成测试，再实现正式正文指针代次推进、取消发布 tombstone 和只读对账命令；未执行迁移、自动修复、索引重建或生产操作。
- 发布事务：`BlogPage.publish()` 在外层 MySQL 事务中先校验 Revision 的 Mongo 正文版本，再递增 `BlogPublicationState.publication_generation` 并写入 `published_body_version_id/hash/schema`，随后进入 Wagtail 发布和 `page_published` 信号。信号内读取到的 State 与 Outbox 必须具有相同正文版本和 generation；事务回滚时两者均不可见。
- 取消发布：取消发布前保留当前正文身份并递增 generation，使 tombstone 携带最新代次；Wagtail 页面取消发布完成后再清空 `published_body_version_id/hash/schema`。这样搜索消费者可先处理墓碑，避免“已发布可搜、取消发布仍可搜”的窗口。
- 只读对账：新增 `blog_publication_consistency_check` 命令及服务，按 `BlogPublicationState.page_id` 游标扫描，关联 BlogPage、live Revision、ContentSearchState、最新 Outbox，并可只读校验 Mongo `content_body_versions`。只输出计数和有限 ID 样本，发现异常不自动选草稿、不修改任何表或外部系统。
- 实际修改：`wagtailblog3/apps/blog/models.py`、`apps/blog/services/publication.py`、新增 `apps/blog/services/publication_consistency.py`、新增 `apps/blog/management/commands/blog_publication_consistency_check.py`、`apps/search/tests/test_search_sync_producer.py` 及本说明书。
- 验证：发布/Workflow/搜索代次/对账相关测试 28 项通过；搜索生命周期测试 8 项通过；`python manage.py check`、`makemigrations --check --dry-run`、`compileall`、`git diff --check` 通过；`migrate --plan` 仅查看，未应用 0029-0032、search.0006。保留 Wagtail 在 MySQL 上条件唯一约束警告。
- 模型/推理实际调度：`gpt-5.6-sol` 复核 Wagtail 8.0 事务和对账边界；`gpt-5.6-luna` 先行盘点并设计测试建议；主 agent 按建议先补集成断言，再由 `gpt-5.6-terra` 实现搜索字段扩展后的发布指针、tombstone 和只读对账。
- 回滚与残余风险：未应用迁移前可移除指针推进和对账命令，保留 M4.5 旧字段路径；若未来应用迁移，回滚必须保留 State/Outbox/对账数据，先停消费者并回退索引 alias。当前对账仅扫描已有 `BlogPublicationState` 行，尚不能发现“完全缺失状态行”的页面；Mongo/ES/Redis 仍不参与 MySQL 原子事务，正式指针切换与 GC 仍需后续压测和生产授权。

### 50. M4.4 Wagtail 8.0 定时发布 Revision 精确校验（2026-08-28）

- 状态：完成测试环境定时发布前置校验，未接入搜索 Outbox、正式正文指针切换、Celery/Beat 生产任务或服务重启。
- 架构事实：Wagtail 8.0 `publish_scheduled` 管理命令按 `Revision.approved_go_live_at` 到期条件查询 Revision，再调用 `Revision.publish(log_action="wagtail.publish.scheduled")`。页面可在排期后产生更新草稿，因此定时执行不能强制要求排期 Revision 是页面最新 Revision；必须以调度器传入的 Revision 为准。
- 实际修改：`BlogPage.publish` 识别 `wagtail.publish.scheduled` 调用，要求传入 Revision 已有 `approved_go_live_at` 且已到期；随后继续复用 M4.1 发布服务锁定页面/Revision 并严格校验 Mongo `body_version_id`、SHA-256、schema 和正文归属。未到期或没有 Wagtail 审批标记时在 Wagtail 修改页面前失败。
- 验证：新增排期 Revision 已到期且存在更新草稿时仍发布原排期版本、缺少审批标记拒绝、尚未到期拒绝三项测试；`blog.test_publication_service` 共 11 项通过。排期校验不改变普通即时发布和 Workflow 完成路径。
- 数据/服务影响：未应用 `0029`-`0032` 迁移，未写入真实 MySQL/MongoDB、Redis、Elasticsearch 或消息队列，未运行真实 `publish_scheduled`、Celery/Beat，未修改 `systemctl.md`。
- 回滚与残余风险：未应用迁移前可移除定时校验函数和 `BlogPage.publish` 分支，恢复 Wagtail 默认排期行为；若未来启用生产排期，仍需补充排期取消/重排、重复执行幂等、Mongo 暂不可用重试和发布后 Outbox 对账。`page_published`、Search Outbox generation 与公开正文指针切换留待 M4.5/M4.6。

### 51. M4.5 搜索 Outbox 正文身份与公开代际围栏（2026-08-28）

- 状态：完成测试环境兼容实现，未应用迁移、未创建或切换 Elasticsearch 索引、未运行生产消费者。
- 实际修改：`search` State/Outbox 新增可空 `body_version_id` 与 `publication_generation`；producer 从 `BlogPublicationState` 读取已提交正文身份并写入事件；delivery 在 State、正文投影和 ES 写入前校验身份，旧事件只允许标记 superseded/retry，不得覆盖新代际；重建、mapping、只读一致性检查和 ES mget/scan 均携带新字段。保留 `content_version` 作为旧事件兼容版本。
- ES 语义：当事件含 `publication_generation` 时使用该值作为 external version；旧事件继续使用 `content_version`。正文身份字段为不可搜索的审计元数据。新索引 mapping 仍需通过后续在线重建发布，未修改真实 alias。
- 迁移：新增未应用 `search.0006_contentsearch_generation_fields`，四列均可空，允许旧事件/存量 State 平滑过渡。
- 验证：目标搜索索引、ES 读写、重建、一致性和代际围栏测试通过；`python manage.py check` 通过；`makemigrations --check --dry-run` 输出 `No changes detected`；`migrate --plan` 仅查看并包含 `search.0006`；`compileall` 与 `git diff --check` 通过。测试环境保留既有 Wagtail 条件唯一约束警告。
- 数据/服务影响：未写入真实 MySQL/MongoDB/Elasticsearch/Redis，未执行迁移、重建、Celery、Beat 或 systemd 操作；`systemctl.md` 无需更新。
- 回滚与残余风险：应用迁移前可移除新字段读写与 `0006`；应用后回滚应先停消费者并保留字段。当前 BlogPage 发布状态尚未在同一事务内递增 `publication_generation`/切换 `published_body_version_id`，因此新代际字段在存量页面上仍可为空；正式指针切换和事件补偿属于 M4.6。
- 模型/推理实际调度：架构边界由 `gpt-5.6-sol` 高推理复核；搜索后端实现由 `gpt-5.6-terra` 高推理完成；测试替身与回归由 `gpt-5.6-luna` 中推理覆盖；主 agent 负责兼容性修正和门禁。未向外部模型发送源码、凭据或正文数据。
- 模型/推理实际调度：`gpt-5.6-sol` 负责 Wagtail 8.0 调度源码与旧排期 Revision 兼容边界复核（外部模型本轮不可用时由本地源码核对）；`gpt-5.6-terra` 负责后端实现；`gpt-5.6-luna` 负责定向测试矩阵；主 agent 负责集成与门禁。
### 53. M4.6 第二子批：BlogPage 全量对账与只读调度（2026-08-28）

- 状态：完成测试环境实现，未应用迁移、未执行自动修复或生产操作。
- 对账游标：`check_blog_publication_consistency` 改为以 `BlogPage.pk` 为主游标，按批次一次读取页面、`BlogPublicationState`、live Revision、搜索状态和 Outbox；页面存在而状态行缺失时报告 `state_missing`，状态行无页面时继续报告 `page_missing`。`next_after_page_id` 基于页面批次末尾，避免 N+1 查询。
- 并发边界：`BlogPage.unpublish()` 先锁页面再锁 `BlogPublicationState`，与发布路径保持 Page→State 锁序；取消发布继续在同一 MySQL 事务中推进 generation、写 tombstone 并清空正式指针。
- 只读任务：新增 `blog.tasks.check_publication_consistency`，限制单轮扫描量、仅记录计数并返回游标；业务数据不写入 MySQL、MongoDB、Elasticsearch、Redis 或 Outbox，周期模式只维护独立 checkpoint 元数据；Beat 每 300 秒经 `maintenance` 队列触发。
- 实际修改：`apps/blog/services/publication_consistency.py`、`apps/blog/models.py`、`apps/blog/tasks.py`、`settings/database.py`、`systemctl.md` 及本说明书。
- 验证：`blog.test_publication_consistency` 4 项通过；其余全量门禁由主 agent 集成执行。未应用迁移，未写入真实业务数据。
- 残余风险：对账任务当前跳过 Mongo 读取以控制 maintenance 延迟；Mongo 正文存在性需由人工命令或后续分层任务执行。并发发布仍依赖数据库行锁，未进行多进程压力测试。

### 54. M4.6 第三子批：对账 checkpoint、租约与周期轮转（2026-08-28）

- 状态：完成测试环境实现，未应用迁移、未执行自动修复、未修改真实业务数据或生产服务。
- 持久化边界：新增独立模型 `BlogPublicationConsistencyCheckpoint`，保存固定 scope、`cursor_page_id`、周期 high-water (`scan_upper_bound_page_id`)、`cycle`、租约、最近批次计数和脱敏错误类型。该表只记录对账进度，不承载发布指针，也不参与 BlogPage 发布、取消发布或 Search Outbox 业务事务。
- 调度流程：Beat 每 300 秒投递 maintenance 队列。周期任务先用短 MySQL 事务和行锁抢占租约，再在事务外执行有界只读扫描；扫描完成后仅由原 owner 条件更新 checkpoint。达到 high-water 后清零游标、递增 `cycle` 并开始下一轮；新增页面留到下一轮，避免高并发写入造成扫描目标无限后移。有效租约存在时返回 `lease_busy`；异常时释放租约并记录异常类型，避免 maintenance 长期阻塞。
- 实际修改：`wagtailblog3/apps/blog/models.py`、`apps/blog/migrations/0033_blogpublicationconsistencycheckpoint.py`、`apps/blog/tasks.py`、`apps/blog/services/publication_consistency.py`、`apps/blog/test_publication_consistency.py`、`systemctl.md` 及本说明书；测试根节点缺失的前置兼容修正位于 `apps/blog/test_page_view_admin.py`。
- 验证：checkpoint 生命周期、租约冲突、异常释放、BlogPage 状态缺失、游标边界和管理命令只读测试已通过；随后执行 `blog` 应用全量回归及最终 Django/迁移计划/编译/空白检查。迁移 `blog.0033` 仅生成并在 `migrate --plan` 查看，未应用。
- 数据与服务影响：周期任务唯一允许的写入是 checkpoint 元数据；不写 BlogPublicationState、Search State/Outbox、MongoDB、Elasticsearch、Redis 或正文。没有新增 systemd unit，也没有执行 daemon-reload、重启或生产发布。
- 回滚边界与残余风险：停用 Beat/maintenance Worker 后可恢复到上一已验证代码；保留 checkpoint、State、Outbox 和 Mongo 正文，不执行删除或回填。当前仍未在多进程/高吞吐条件下压测租约与扫描时延；Mongo 正文一致性继续由显式命令或后续分层任务负责；Outbox 最新事件查询的历史扫描成本需在百万级压测后再优化。未来若按主键范围拆分多个 scope，必须为每个 scope 独立 high-water、租约和监控指标。
- 模型/推理实际调度：`gpt-5.6-sol` 完成 checkpoint 架构与并发边界复核；`gpt-5.6-terra` 完成后端模型、租约和调度实现；`gpt-5.6-luna` 完成测试先行与回归矩阵；主 agent 负责文档边界修正、集成和最终门禁。未向外部模型发送源码、凭据或正文数据。

### 55. M4.6 第四子批：小规模性能基线与边界修正（2026-08-29）

- 状态：完成测试环境实现，未应用迁移、未连接或修改生产库。
- 边界修正：对账服务在页面批次为空时的 orphan State fallback 同样应用 `upper_bound_page_id`，避免周期结束后把 high-water 之外新产生的 State 误报为 `page_missing`。
- 查询性能：`ContentSearchOutbox` 增加 `(page_id, content_version, id)` 复合索引（迁移 `search.0007`，仅生成未应用），匹配 latest event 的批量排序；未改变事件语义。按页面聚合最新 Outbox 的窗口/子查询优化暂留后续专项，避免在当前每日十几篇的规模引入数据库兼容性复杂度。
- 测试基线：新增可控规模测试，覆盖 15 篇日常文章的硬批次上限、high-water 外页面隔离，以及每页有限历史事件仍使用批量 Outbox 查询；不伪造百万条持久数据。
- 实际修改：`apps/blog/services/publication_consistency.py`、`apps/blog/test_publication_consistency_upper_bound.py`、`apps/blog/test_publication_consistency_scale.py`、`apps/search/models.py`、`apps/search/migrations/0007_contentsearchoutbox_page_version_index.py` 及本说明书。
- 验证：对账/边界/索引相关定向测试通过；`manage.py check`、`makemigrations --check --dry-run`、`compileall`、`git diff --check` 通过。`migrate --plan` 仅查看，新增 `search.0007` 与此前迁移仍未应用。
- 生产备份门禁：本批没有生产写操作，因此未执行备份。未来若要应用迁移，必须先获得独立授权，并完成 MySQL 含 schema/triggers/routines 的一致性备份、Mongo 正文/草稿/revision 备份、ES snapshot 及 systemd/env 清单，且先演练恢复。
- 残余风险与规模判断：当前日均十几篇，按现速达到百万篇需数百年，暂无分库分片必要。应先在测试环境生成 1 万/10 万级合成元数据做 EXPLAIN 和延迟基线；Mongo 正文校验继续作为低频分层任务；多进程租约超时、Outbox 历史事件线性增长和 ES 索引容量仍需专项压测与监控。未执行自动修复、GC、数据回填或生产服务操作。
- 模型/推理实际调度：`gpt-5.6-sol` 复核 high-water、备份和容量边界；`gpt-5.6-terra` 实现 Outbox 索引；`gpt-5.6-luna` 补充小规模性能/批次测试；主 agent 集成修正、更新文档并执行门禁。未向外部模型发送源码、凭据或正文数据。

### 56. M4.6 第五子批：租约失效与 Wagtail 8.0 排期字符串兼容（2026-08-29）

- 状态：完成测试环境修复，未应用迁移、未连接或修改生产库。
- 租约围栏：checkpoint 完成写回现在检查条件 `update()` 的影响行数；租约过期或被其他 worker 接管时返回 `lease_lost` 并记录 warning，不再误报 `completed`，游标由当前持有者决定是否推进。
- 排期兼容：`BlogPage.publish()` 使用统一 `_revision_content()` 解析 Wagtail 8.0 `Revision.content` 的 JSON 字符串/映射值，再判断 `go_live_at`；未来排期只保存草稿状态，不提前切换正式正文指针。测试隔离 Wagtail 核心 `Page.publish()`，避免把项目解析契约与核心反序列化边界混为一谈。
- 实际修改：`wagtailblog3/apps/blog/tasks.py`、`apps/blog/models.py`、`apps/blog/test_publication_consistency.py`、`apps/blog/test_publication_service.py` 及本说明书；未新增迁移。
- 验证：发布服务、checkpoint、high-water 和规模基线测试共 26 项通过；`manage.py check`、`makemigrations --check --dry-run`、`compileall`、`git diff --check` 通过。仍保留既有 Wagtail/MySQL 条件唯一约束警告。
- 尚未完成事项（需独立批次和门禁）：生产迁移 `blog.0029`-`0033`、`search.0006`-`0007`；MySQL/Mongo/ES/systemd 配置备份与恢复演练；低频 Mongo 正文存在性对账；Outbox/Delivery 审计保留和归档；1 万/10 万级合成数据 EXPLAIN、ES 容量和多进程租约压测；生产 Beat/maintenance 监控接入。
- 生产备份边界：由于本批没有生产操作，未执行备份。未来生产迁移或索引操作前必须先冻结写入、备份 MySQL schema+data/triggers/routines、Mongo 正文/草稿/revision、ES snapshot 和 systemd/env 清单，并完成恢复演练后再取得单独授权。
- 残余风险：Beat 默认 `check_mongo=False`，Mongo 正文缺失可能不会进入 300 秒快速对账；Outbox 历史事件仍长期增长；租约虽能识别失效，但尚未做多进程超时压力测试。当前日均十几篇，暂不需要分库分片，先观察 1～2 周周期耗时、`lease_lost`、`last_error`、pending/retry 和数据库增长指标。
- 模型/推理实际调度：`gpt-5.6-sol` 复核生产门禁与 Wagtail 8.0 兼容边界；`gpt-5.6-terra` 完成租约运行时修复；主 agent 完成排期字符串测试适配和集成；`gpt-5.6-luna` 负责回归验证。未向外部模型发送源码、凭据或正文数据。

### 57. 生产迁移准备方案（待单独确认执行）

本节是生产迁移 runbook，不构成当前执行授权。当前工作区存在未提交改动，必须先完成测试、代码审查、精确 commit 和 `origin/main` 推送；生产目录、分支、实际 HEAD、Wagtail 版本、已应用迁移、数据库名、服务名和 alias 必须在维护窗口开始前重新只读核实，不能使用历史记录中的固定值替代现场检查。

#### 57.1 迁移对象与前置门禁

- 本批 Django 迁移为 `blog.0029`、`blog.0030`、`blog.0031`、`blog.0032`、`blog.0033`、`search.0006` 和 `search.0007`；Wagtail `wagtailcore.0098` 是否需要执行必须以生产 `showmigrations wagtailcore` 为准，不能默认重复执行。
- 生产执行前必须确认 `main` 工作树干净、远程地址正确、目标 commit 已在测试环境通过；确认 MySQL、MongoDB、Redis、MinIO、Docker、Elasticsearch、read alias 和四个项目服务健康。
- 备份目录使用现场时间戳创建在 `/home/source/Django/wagtail/backups/`，保存命令输出、文件清单、SHA-256 校验和、数据库版本、ES alias/索引清单以及 systemd unit 和 `.env.production` 的脱敏元数据；凭据和正文内容不得复制到 Git 或报告。

#### 57.2 备份与恢复演练

1. MySQL 执行一致性备份，至少包含受影响 schema 和数据、triggers、routines、events，并记录 binlog 位点；备份完成后在隔离实例恢复，运行 `mysqlcheck`、关键表行数/索引核对和 `manage.py migrate --plan`。
2. Mongo 执行正式正文、`content_body_versions`、草稿和 Revision 快照的副本集快照或 `mongodump`，记录 clusterTime/oplog 位点；在隔离实例按 page/version 抽样恢复并校验 hash、schema 和 page 归属。
3. Elasticsearch 创建或确认可恢复 snapshot，记录 serving alias、当前索引、mapping 和文档计数；不在迁移窗口删除旧索引。MinIO 记录对象版本/清单，Redis 只作为可重建缓存记录状态即可。
4. 恢复演练必须证明 MySQL 正式正文指针不早于 Mongo 可恢复正文版本，且恢复后可以只读运行 `blog_publication_consistency_check`；演练未通过则不进入生产迁移。

#### 57.3 停写、迁移与恢复顺序

1. 维护窗口开始后先停止 `wagtailblog3.service`、`wagtailblog3-celery-maintenance.service` 和 `wagtailblog3-celery-beat.service`，确认没有新的页面保存、发布、取消发布、Mongo 清理或 Search Outbox 消费；Filebeat 可继续采集，除非日志目录或格式变更。
2. 在生产目录 `git fetch origin --prune`，只允许 `git merge --ff-only origin/main` 到已验证 commit；安装依赖前核对 Conda 环境和 lock/requirements 差异，不允许从未审阅分支部署。
3. 使用生产 `.env.production` 执行 `manage.py check`、`showmigrations blog search wagtailcore` 和 `migrate --plan`；再次核对计划只包含已批准迁移后，才执行 `manage.py migrate`。迁移仅改变 MySQL schema，不回填正文、不触发 Mongo 删除、不重建 ES。
   MySQL DDL 可能隐式提交，整组迁移不具备跨迁移原子回滚能力；若中途锁等待或失败，立即停止后续服务恢复，保留已执行的表/列/索引，依据 `showmigrations` 和数据库实际结构人工处理，禁止使用 `--fake` 或未经授权的反向迁移。
4. 迁移成功后运行 `manage.py check`、`makemigrations --check --dry-run` 和只读一致性命令；确认 `BlogPublicationConsistencyCheckpoint`、`BlogPublicationState`、Search generation 字段和 Outbox 索引存在。
   迁移本身不为存量 BlogPage 回填 `BlogPublicationState` 或 `published_body_version_id`；若对账发现 `state_missing` 或正文指针缺失，先保留搜索/发布开关关闭，输出只读报告，再以独立数据修复方案、备份和授权处理。
5. 按“基础设施 readiness -> `wagtailblog3.service` -> maintenance Worker -> Beat -> Filebeat（受影响时）-> Nginx（受影响时）”恢复。Beat/Worker 恢复前确认 Redis、Mongo、ES alias 和 MySQL 实际可用。

#### 57.4 生产验收与回滚

- 验收包括首页、BlogPage 详情、后台历史预览/比较、只读对账命令、四个服务 active/enabled、failed unit、socket/端口、Redis 队列、Beat 调度、Outbox pending/retry/dead、Mongo 正文指针/hash、ES alias/mapping/文档计数和错误日志。
- 迁移前代码阶段失败：停止新服务，回到上一个已验证 commit，保留备份和新增数据，不删除 Mongo 正文或 Outbox。迁移已部分或全部完成时，不默认执行反向迁移；先停服务、保留新增列/表和备份，确认旧代码是否兼容，再由数据库负责人决定恢复或前向修复。
- 任何 MySQL/Mongo/ES 恢复、索引 alias 切换、页面发布、自动修复、GC 或删除都需要单独书面授权；本 runbook 不授权这些动作。

#### 57.5 当前状态与后续事项

- 当前仅完成方案准备，未执行生产 SSH、备份、迁移、索引切换、服务重启或真实数据操作。
- 生产迁移前仍需先提交并推送当前工作区改动，完成恢复演练和维护窗口审批；每日十几篇文章不需要为迁移额外扩容或分片。
- 迁移后观察至少 1～2 周，记录发布成功率、对账周期、`lease_lost`、`last_error`、Outbox lag、Mongo 读取错误和数据库/ES 增长，再决定低频 Mongo 对账、归档和百万级压测的下一批次。
- 模型/推理实际调度：`gpt-5.6-sol` 负责迁移和回滚架构复核；`gpt-5.6-terra` 负责服务/依赖顺序核对；主 agent 负责把方案写入文档并保留授权边界。未向外部模型发送源码、凭据或生产正文数据。
### 58. 生产迁移实施记录（2026-08-29）

- 状态：已完成代码同步、数据库迁移、服务恢复和只读验收；未执行正文回填、Mongo 删除、ES 重建、alias 切换或自动修复。
- 运行时代码发布 commit：`726978aaf1c2394167185c4ff45037de2a3ba3d5`；随后仅文档实施记录提交为 `8d15cb48d7f37fc7fc88ab0ade8717935e571189`，生产与 `origin/main` 当前均在后者，生产工作树干净。
- 备份目录：`/home/source/Django/wagtail/backups/wagtailblog3-markdown-import-20260829-100724`。包含 MySQL schema/data/triggers/routines/events 导出、Mongo `blog_content` 与 `blog_page_revision_bodies` 导出及校验信息、ES 集群健康和 snapshot repository 状态、`.env.production`、四个 systemd unit、Nginx 配置及迁移前后服务状态。
- 迁移结果：成功应用 `blog.0029` 至 `blog.0033`、`search.0006` 和 `search.0007`；`wagtailcore.0098` 现场已是已应用状态。迁移未写入正文数据，也未回填 `BlogPublicationState`。
- 服务验收：`wagtailblog3.service`、`wagtailblog3-celery-maintenance.service`、`wagtailblog3-celery-beat.service`、`wagtailblog3-filebeat.service` 均 active/enabled；首页 HTTP 200；生产 `manage.py check` 无错误，仅保留既有 MySQL 条件唯一约束警告。
- 只读对账：扫描 1000 个页面，`state_missing=1000`；`mongo_missing`、`live_pointer_missing`、`revision_body_mismatch`、`outbox_missing`、`search_identity_mismatch` 等样本均为空。`state_missing` 是未回填存量 State 的已知结果，后续须单独制定只读观察、分批回填和回滚方案，不得由本次迁移自动修复。
- 回滚边界：代码可回退到迁移前 commit；MySQL DDL 已执行，不执行未经验证的反向迁移或 `--fake`。保留新增表/列及备份，必要时停止应用服务并依据备份恢复；不删除 Mongo 正文、草稿、Revision 或 Outbox。
- 残余风险：ES 无 snapshot repository 且集群为单节点 yellow；Mongo 正文对账当前为显式命令，Beat 默认不启用 Mongo 检查；存量 State 缺失、Outbox/Delivery 归档、百万级压测和恢复演练仍未完成。
