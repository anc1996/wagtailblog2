# Wagtail Page 正文外置通用协议分步实施与逐节点审核计划

> 状态：研发实施计划，尚未实施。本文描述的目录、模型、索引字段和任务均为拟建项；除“当前实现基线”表列内容外，不应视为仓库现有能力。当前已获授权在本地分支实现、验证并按完成的功能阶段提交本地 Git；隔离测试库须按安全门禁操作。生产备份、灰度、部署、数据修复和 Git 推送均须另行明确授权。

**目标：** 建立模型级字段存储策略：继承 `ExternalContentPageMixin` 的 Page 通过 `content_storage = {"body": "mongo"}` 明确选择外置字段；未列字段仍完整使用原生 MySQL。框架自动匹配已登记 Field 的 codec，并由同一 coordinator 处理任意已登记字段集合，生命周期代码不按字段类型复制。首个业务 Page 只外置一个 `body: StreamField`，专用测试 Page 用两个 StreamField 外置字段和一个 MySQL 字段验收通用内核。之后扩展 RichTextField、TextField codec；内容型 JSONField 待具体业务 schema 确定。独立搜索类型与存储策略分别登记。

**设计契约：** 以 `10-Wagtail Page正文外置通用协议详细设计说明书.md` 为接口权威；Revision Manifest 固定为 `_external_content_manifest`，第一版 `fields.<field_name>` 包含 codec、schema_version、version_id、payload_sha256，并记录 `content_storage_schema`。新通用版本集合提议为 `external_content_versions`；首期只开放 StreamField codec，但运行时能编排同页多个已登记字段。BlogPage 原有集合、指针和 Revision 读取由兼容适配器保留。

## 计划基线

| 项目 | 规划假设 |
|---|---|
| 技术基线 | Python 3.13、Django 5.2.8、Wagtail 8.0、MySQL 8.4、MongoDB / PyMongo 4.11、Elasticsearch 8.19、Redis、Celery 5.5。实施前重新核对锁文件和运行环境。 |
| 试点页面 | 新建无历史数据的独立 `guides.GuidePage`，只外置 body；`guides.GuideIndexPage` 为原生 MySQL 父页。二者及前台体验以《11-专题指南页面需求与详细设计说明书》为业务契约。现有 `PortfolioPage`、`HomePage` 不加 mixin、不迁移、不改动正文归属。 |
| 人力估算 | 1 名后端全职、前端约 0.5 人、测试/审核约 0.5 人；按一轮主要实现及一次返工预留估算。 |
| 预计周期 | 约 9–11 个工作周；多字段内核、Wagtail 生命周期发现、搜索游标修正及备份恢复演练均在此基线内。每阶段以退出门禁为准，不以日历日期替代审核。 |
| 生产状态 | 本计划不读取生产数据库、不生成生产备份、不运行生产迁移或服务操作。生产执行必须有独立变更授权和 Maker-Checker 批准。 |

阶段 0–7 的工作量按顺序合计约 47 个后端工作日（4+4+7+9+6+8+4+5），覆盖通用多字段 StreamField 内核、单字段业务试点、搜索和恢复演练；前端、测试和文档评审可并行，因此日历估计 9–11 周，不代表所有人员各投入 47 天。RichTextField/TextField codec 与类型专项回归初估另需 2–3 个工作周，须在试点结束后重新估算；内容型 JSONField 尚无业务 schema，不计工期。生产发布预案另计 3–5 个工作日，且不计入研发测试周期；若阶段门禁未通过，后续日期顺延，不压缩验证或恢复演练。

## 当前实现基线与待建边界

| 领域 | 当前代码事实 | 本计划新增/修改方向 |
|---|---|---|
| Blog 正文与 Revision | `wagtailblog3/apps/blog/models.py` 的 `BlogPage.serializable_data()` 写现有 `content_body_versions` 与兼容的 `blog_page_revision_bodies`，Revision 保存 `mongo_body_version_id`、哈希、schema 及旧指针；读取仍是 Blog 专用逻辑。 | 保留旧键和旧集合的只读/写入兼容；通过 adapter 接入通用接口，禁止存量 Revision 批量改写。 |
| Mongo 版本存储 | `wagtailblog3/mongo.py` 有 `MongoManager` 与 `content_body_versions`，版本身份按 `aggregate_type`、`aggregate_id`、哈希和 schema 组织，现有正文适配要求 list。 | 新通用集合及字段身份校验为拟建项。试点 StreamField 使用 list payload；RichText/Text 字符串与内容型 JSON 需要各自严格 codec 和分阶段验收。复用现有 Mongo client 生命周期。 |
| 发布状态 | `blog.models.BlogPublicationState` 与 `blog.services.publication.BlogPublicationService` 是 BlogPage 专属状态与服务；`search/signals.py` 的发布维护受搜索生产开关控制。 | 先将外置正文正式指针维护从可选搜索开关解耦，再提供页面类型无关的发布服务；仍委托 Wagtail 原生发布动作。 |
| 搜索索引 | `search/services/content_query.py`、`delivery.py`、`rebuild.py`、`bootstrap.py` 等路径多处直接引用 BlogPage。普通 Page 查询排除 BlogPage 后使用 Wagtail 搜索。 | 注册类型与投影 adapter；外置正文从指定正式 Mongo 版本构建索引文档；Outbox、重建、投递、公开复核覆盖新类型。 |
| 搜索 UI / 联邦 | `templates/search/search.html` 当前为 `all`、`blog`、`pages`；`core.py` 由博客独立索引与普通 Page 搜索合并。 | 增加稳定 `guides` 类型；类型集合互斥，`all` 对三路公开结果去重。页面、异步片段、REST API、缓存键、游标签名及结果卡片共用该契约。 |
| 搜索统计 | `search/analytics.py` 读取 Wagtail `QueryDailyHits`；此结构没有查询类型维度，旧数据不能推算类型。 | 新增受控的类型化统计事件/聚合；`search_scope` 与实际 `result_type` 分开定义。一次 all 请求只统计一次查询。 |
| 既有页面边界 | `PortfolioPage.body` 为 StreamField；`HomePage.body` 为 RichTextField；两者目前由 MySQL 存储。 | 两者均不作为试点，也不加 mixin；新建独立 StreamField 业务 Page 与双外置字段专用测试 Page 验证协议。RichText codec 不属于首期；作者、媒体、博客 AI、Feed、评论与阅读量不自动成为通用 mixin 的职责。 |

## 工期路线与阶段门禁

### 阶段 0：代码与生命周期基线（第 1 周，约 4 工作日）

- [ ] 盘点现有 Page 类型、正文字段、数据库迁移、已注册 Wagtail 搜索字段及页面模板；输出 `model_label -> field -> codec -> 使用方` 矩阵。
- [ ] 逐入口检查当前 Wagtail 8.0 安装源码及本仓库集成：创建页、`save_revision()`、`serializable_data()`、历史预览/恢复、`Revision.publish()`、`PublishPageRevisionAction`、Workflow、定时发布/取消、Copy、Alias、翻译、单页/子树删除。
- [ ] 记录发布状态、Outbox、删除意图和搜索索引的真实事务边界；验证 `CONTENT_SEARCH_PRODUCER_ENABLED` 关闭时 BlogPage 正式指针是否仍被正确推进。
- [ ] 只读盘点开发/测试环境中受影响记录的数量、Mongo collection/index、搜索索引 mapping、Outbox 积压与缓存 key namespace；正文内容不得写入报告。生产指标另需访问授权。
- [ ] 为当前缺口建立测试清单，不在此阶段修复业务逻辑。

**阶段门禁 0：** 代码事实与目标契约分栏；列出所有会写 Mongo/MySQL/ES 的入口、未覆盖入口和现存兼容格式。不能证明 Revision 与发布动作绑定精确正文版本时，暂停后续实现并修订方案。

### 阶段 1：契约、备份与恢复预演（第 1–2 周，约 4 工作日）

- [ ] 锁定 manifest key `_external_content_manifest`、动态 `fields`、`content_storage_schema`、新集合结构、模型/字段/版本身份、StreamField codec 规范化方式、各字段空列占位值、公开类型键及 `all` 的互斥集合语义。首个业务试点只声明一个 body，但首期内核须支持字段集合。
- [ ] 建立模型级 `content_storage` opt-in 规则：mixin 只接管声明为 mongo 的字段；Django/Wagtail check 在启动时验证字段存在、按最具体 Field 类型匹配唯一兼容 codec、名称不重复且不属于关系/权限/路由/状态字段，search type 注册无冲突。自定义 Field 子类需明确兼容声明；无继承、无登记时不得为该页面启用通用正文流程。不得按字段类型或长度自动外置。
- [ ] 约定模型策略版本、Manifest 协议版本和 codec/schema 版本分别演进；现有字段改存储位置须另立迁移、历史 Revision 读取、对账和回退计划，不能只改配置。
- [ ] 保留旧字段名映射、旧 StreamField 块解码器和旧策略只读能力；无 Manifest 的 Revision 仅在明确属于旧 Blog/原生 MySQL 格式时走 legacy 分支，新协议 Page 缺 Manifest 须失败关闭。
- [ ] 建立不可变版本、幂等键、哈希冲突处理、孤儿版本延迟清理、Revision identity binding 和旧数据双读顺序契约。
- [ ] 在隔离测试环境演练备份、还原和数据一致性检查。测试库内容必须可丢弃且与生产凭据/目标隔离。

**备份门禁（任何共享测试数据改动前、任何生产改造前分别执行）：**

1. 对代码和未跟踪文档先记录 `git status`、文件清单与 SHA-256；生成受访问控制的源码归档，不能把密钥或真实环境文件打包进源码备份。
2. 在批准的维护窗口暂停会写正文/Revision 的操作，记录统一恢复点时间及页面/Revision 高水位；避免把非原子 MySQL/Mongo 快照误报为跨库强一致快照。
3. 按现有运维标准分别对 MySQL 与 MongoDB 做全量可恢复备份，保留 Mongo 相关旧集合、索引及版本文档；同时记录 Elasticsearch 当前 serving alias、物理索引名、mapping/settings。ES 是可重建投影，但 alias 回退信息仍须保存。备份加密、限制访问并记录校验摘要。
4. 将 MySQL 与 Mongo 备份还原到隔离命名空间，使用只读对账检查 Page/Revision 指针、Mongo 身份/哈希、版本引用；记录恢复耗时、差异和批准人。未完成恢复演练不得进入写入型试点或发布。
5. Redis 缓存可按设计重建；类型化统计数据属于 MySQL 权威数据，必须包含在 MySQL 备份中。不得把“命令成功”当作恢复证据。

生产备份命令由运维根据实际拓扑、Mongo 复制集/一致性模式、备份凭据注入方式和存储策略另行编制；本计划不假定主机/认证参数，也不包含生产执行命令。

**阶段门禁 1：** 设计评审通过；测试环境恢复演练通过；生产备份预案有 owner、保留期限、恢复目标和验收证据。无单独授权不得访问生产数据执行以上动作。

### 阶段 2：多字段存储内核和 StreamField Codec（第 2–3 周，约 7 工作日）

**拟建代码入口：** `wagtailblog3/apps/external_content/`（应用名及包布局实施前以 `INSTALLED_APPS` 和现有 imports 核验），包含 `pointers.py`、`registry.py`、`store.py`、`codecs/streamfield.py`、`checks.py` 和专用测试。

- [ ] 先写失败测试：同页/字段/版本身份校验、跨 Page/字段拒绝、规范化哈希、Mongo 重复键后读取并核对现存文档、schema 不匹配拒绝、损坏 payload 拒绝。
- [ ] 实现 `ExternalContentManifest`：顶层绑定 `content_storage_schema`、`model_label`、`page_id`，`fields.<field_name>` 绑定 codec、schema_version、version_id、payload_sha256；读 Revision 时与 `Revision.content_type/object_id` 及实际 Page 身份交叉验证。第一版即接受已登记字段集合，键语义变化才升级协议。
- [ ] 新版本写入 Mongo 不可变集合 `external_content_versions`。MySQL 只保存 Manifest；不在 Revision JSON 写入外置正文 payload。Mongo 写入后 MySQL 失败允许遗留不可见孤儿，交由延迟、引用复核清理，不做同步删除补偿。
- [ ] 按实际 Mongo 拓扑核定 write concern、journal 与故障切换读取保证；复制集优先以 majority+journal 确认。普通 ACK/立即重读不足以证明持久性；无法满足所需恢复保证时阻断公开写入上线并记录 RPO 决策。
- [ ] 实现首期 `StreamFieldV1Codec` 往返和块结构校验；`markdown_block` 的 key 与原始 Markdown 字符串必须保持不变，HTML 转换仍只发生于既有模板渲染层。Codec 只做无损转换与身份校验，不独立实现发布、复制或删除。
- [ ] 共享 `MongoClient` 生命周期；不得为每次读取创建未关闭的独立 Client。

**阶段门禁 2：** Codec 往返无损；任意跨页、跨字段、错误 Revision 指针不能读出正文；异常不会回退到当前 live 正文；新增 Mongo 文档只有被完整 Manifest 引用后才可参与发布/搜索。

### 阶段 3：Page Mixin、Blog 兼容与试点 Page（第 3–5 周，约 9 工作日）

**拟建代码入口：** `external_content/mixins.py`、`services/revisions.py`、`services/publication.py`、`models.py`（若需要通用状态表）、以及 `blog/models.py` 的最小兼容 adapter。

- [ ] 实现无具体 Wagtail 父表的 opt-in mixin；子类保留自身字段定义并以 `content_storage` 选择 mongo 字段。未列字段完整走原生 MySQL；codec 注册表只自动匹配类型，不自动改变存储。不得通过继承 BlogPage 获取博客业务行为。
- [ ] 实现一次处理字段集合的 revision coordinator：显式 `prepare_revision` 收集全部外置字段并写齐 Mongo 不可变版本；受控 `serializable_data()` 仅将输出字典中的这些字段替换为占位值并写完整 Manifest，原生字段与子关系保留。普通导出、比较、Copy 调用序列化不得写 Mongo。所有字段版本成功后，才在一个 MySQL 事务提交 Page/Revision/草稿状态；任一失败不得留下部分可用 Revision。
- [ ] Revision 反序列化必须读取指定历史 Manifest，执行全身份/哈希/codec 校验。缺失或损坏时显式失败，不得以当前正式正文降级展示。
- [ ] 覆盖新页面先获取真实 Page 主键再保存 Revision；不能在 Page 无主键时把长正文作为 Revision fallback 写入 MySQL。
- [ ] 用统一发布服务准备并验证指定 Revision；正式发布必须继续走 Wagtail 原生 `PublishPageRevisionAction` / `Revision.publish()` 生命周期。正式正文指针、发布代次和 Outbox 在同一个 MySQL 事务内提交；Mongo 校验先于指针推进。
- [ ] 将 BlogPage 作为 legacy adapter 接入，不改变 `mongo_draft_pointer`、`mongo_body_version_id` 和旧集合读取；新页面以通用 Manifest 工作。存量正文搬迁属于独立、额外授权的项目。
- [ ] 在独立 `guides` 应用新建原生 MySQL `GuideIndexPage` 与 opt-in `GuidePage`，仅外置后者 body；summary、主题、难度、最后核查日期、封面及相关文章归 MySQL。另建专用测试 Page，两个 StreamField 外置字段加一个原生字段，验证 Page SQL 行/Revision 占位与原生字段透传。HomePage、PortfolioPage 保持 MySQL 行为；具体块、模板、导航与测试按配套《11-专题指南与正文外置分步实施计划》推进。
- [ ] 验证 Wagtail 草稿 Revision overwrite 的真实条件；已被审批、发布、排期或 Workflow 引用的 Revision 强制新建 ID。若不能安全识别引用，则外置 Page 一律新建 Revision ID；历史读取不靠 ID 推算 Mongo 版本。
- [ ] overwrite、审批、排期与发布共享 Page/Revision/状态锁序；锁内重读 Revision/完整 Manifest 指纹，丢弃事务外过期预取结果。

**阶段门禁 3：** 双外置字段及一个原生字段完整闭环；新建、草稿、预览、历史恢复、Wagtail 后台发布、Workflow/定时发布及取消均绑定精确 Revision/整份 Manifest；普通序列化无写副作用，已批准/发布 Revision 不被覆盖；重复发布幂等；Mongo 故障不造成 MySQL live/pointer 漂移；搜索生产开关关闭时发布状态仍正确。

### 阶段 4：页面生命周期补偿和删除安全（第 5–6 周，约 6 工作日）

- [ ] 核实 Wagtail copy/翻译/Alias 的真实对象身份和调用顺序。统一 coordinator 遍历源 Manifest 的全部字段，Copy 与 Alias 均派生目标 Page 身份的独立版本；Alias 保留来源关系但不得直接复用源 page_id 绑定的指针。解除 Alias、源删除及别名链需验收。
- [ ] 外置正文删除沿用持久删除意图模式：先按全部字段固定精确页面/Revision 版本清单并写搜索 tombstone，阻断新的草稿/公开读取；按授权操作终结原生 MySQL 页面树并保留独立删除清单，安全期后复核所有 Page、Revision、Alias/Copy 引用，只有无引用版本才允许 Mongo 延迟清理。任何直接 `Page.delete()`/Collector 入口均需验证不会绕过意图保护。
- [ ] 定时任务、Workflow、并发发布、删除与编辑竞态通过 Page/状态/Revision 锁及代次校验验证；旧事件不能复活新 tombstone。
- [ ] 失败补偿只允许重试或重新核验，不得自动覆写 Revision 或批量修正文档。

**阶段门禁 4：** copy、翻译、Alias、子树删除、重复 Worker、旧 Outbox 重放、并发发布均无错页读取、共享引用误删或已撤销内容复活。

### 阶段 5：搜索类型注册与三类联邦搜索（第 6–8 周，约 8 工作日）

**现有相关入口：** `apps/search/core.py`、`services/pages_query.py`、`services/federated_query.py`、`services/content_query.py`、`services/document.py`、`services/outbox.py`、`services/delivery.py`、`services/rebuild.py`、`views.py`、`api.py`、`templates/search/` 及 `static/search/js/search.js`。

- [ ] 建立独立 Search Type Registry：稳定键 `blog`、`guides`、`pages`；注册 Page 模型、显示名、日期语义、projection adapter、公开范围和结果卡片字段。外置存储能力和是否进入搜索分别配置，避免无意公开。
- [ ] 定义互斥集合：`blog` 只含 BlogPage；`guides` 只含显式注册的 GuidePage；`pages` 排除 BlogPage、独立注册类别及所有尚未注册公开搜索适配器的外置 Page；`all` 为三者并集，以 Wagtail Page 主键去重。审计默认 Wagtail 索引旁路。
- [ ] 扩展正式正文投影：遍历指定 published Manifest 的全部外置字段读取校验 Mongo payload，按字段投影规则提取纯文本供 ES；原生字段按现有语义投影。Outbox 快照携带整份 Manifest，文档携带 `page_id`、`model_label`、`search_type`、locale、发布时间、Manifest 摘要和发布代次。原始 Markdown/StreamField 不写入前端 API。
- [ ] Outbox 状态及消费者以 MySQL `ContentSearchState`/Outbox 的期望代次和操作为权威。下架写 tombstone 或等价保留代次的不可复活标记，不从 ES 物理删文档后依赖文档本身判断新旧。
- [ ] 在创建事件与重建投影时按 Page 类型调度 adapter；Mongo 不可读时保留可重试状态，不用空正文覆盖原索引。Search generation 不可依赖页面列表的临时当前状态。
- [ ] 为 BlogPage、GuidePage、普通 Page 共用结果协议；ES 命中后的 Page hydration 按 `model_label` 还原具体 Page，并按当前 `.live().public()`、权限、locale、正式 Manifest 摘要与公开代次复核。过期命中、摘要及缓存条目丢弃并触发重新投影。
- [ ] 将新类型贯穿 HTML、异步 API、REST API、建议词入口（若适用）、缓存实现命名空间、URL 参数、分页游标签名、日期过滤和结果卡片。缓存版本递增，旧 all/blog/pages key 不得被新实现误复用。
- [ ] 修订跨来源分页算法。现有联邦游标固定两路，推进来源 offset 时必须保留未消费候选；三路新增前通过稳定排序键、source-specific continuation 和重复/缺失测试，不能简单把每路 offset 都加一页大小。

**阶段门禁 5：** 有独立 专题指南 筛选；all 同时包含三类且去重；相同页面不重复计数；日期/相关性排序可解释；旧页不漏、新页不重；匿名权限、父级限制、取消发布和索引陈旧均不能泄露内容；分页跨来源无跳项/重项。

### 阶段 6：搜索分析和可观测性（第 8–9 周，约 4 工作日）

- [ ] 明确定义统计事件：`search_scope`（用户选择的 all/blog/guides/pages）、`result_type`（返回/点击结果类型）、规范化词、时间、入口、总数、零结果标记和可选耗时；不得存储正文、Cookie、身份令牌或不必要个人信息。
- [ ] 明确 HTML 首屏、AJAX 更新、REST API、缓存命中、重试、分页、联邦三路执行的唯一计数点。一次 all 查询只产生一次 query event，分页及搜索后端 fan-out 不重复累计查询量。
- [ ] 通过 MySQL 事件表/聚合表或现有搜索统计服务扩展实现可审计、限期保留的 type-aware 统计；不得把无类型的 `QueryDailyHits` 伪装成类型报表。历史旧行只保留总量，类型统计从新采集生效日起计算。
- [ ] 后台分析报表限定时间范围、权限和聚合维度；点击率若纳入首期，必须先定义点击去重及机器人流量边界，否则仅纳入后续阶段。

**阶段门禁 6：** 搜索量按 scope 可对账；结果/点击类型有明确定义；all 不多记；缓存命中计数符合统一口径；敏感原查询数据有访问和保留策略。

### 阶段 7：完整回归、故障演练和测试环境灰度（第 9–11 周，约 5 工作日）

- [ ] 在 WSL2 的 `wagtailblog-test` 隔离配置运行 `manage.py check`、新增 `external_content` 测试、BlogPage 存量回归、Search API/UI 和 analytics 专项测试；运行 migration dry-run 与 `migrate --plan`，只检查不实际迁移共享或生产数据库。
- [ ] 做 Mongo 超时、写成功/MySQL 回滚、Mongo 哈希错、ES 不可用、Worker 重复/乱序、Outbox 租约失效、权限改变、数据库 deadlock 等故障注入。
- [ ] 在测试环境用合成或授权样本运行完整备份/恢复；核对 MySQL Revision Manifest、Mongo identity/hash、ES 文档类型/代次和 query analytics 计数。报告只输出统计及脱敏错误码。
- [ ] 演练 MySQL 回到早于 ES 的备份点：冻结投递、隔离旧 Worker/租约、推进持久 recovery_epoch；任务绑定物理目标索引，在新版本域重建、核对、切换只读 alias，再放行新 Worker。故障注入包含旧进程持有已发出写请求跨越 alias 切换。
- [ ] 测量编辑/预览/发布/搜索 p50/p95、ES 延迟、Outbox backlog、Mongo 读取失败、MySQL 行增长及 API 错误率，确定实际灰度阈值；阈值由测试数据制定，不能从假设填入。

**阶段门禁 7：** 关键定向测试全绿，恢复演练通过，故障注入无正文错页/错版，指标基线和回滚步骤经 Checker 审核。测试环境通过不等于生产授权。

### 后续字段扩展批次（阶段 7 验收后，另计；不强制等待其完成才发布首个试点）

- [ ] RichTextField 增加 `richtext.v1`：保存 Wagtail 原始富文本字符串，严格区分空串、未加载和值已改变；往返验证编辑器 markup 与内嵌 Page/图片引用，不把渲染 HTML 当正文身份。
- [ ] TextField 增加 `text.v1`：保留原始 UTF-8 字符串，包括 Markdown 的换行和标记；验证空串、长文本、特殊字符、预览/历史恢复和正式搜索投影。
- [ ] Page 只按字段名声明 mongo，新增 codec 由注册表按真实 Field 类型选择并固定到 Manifest；复用首期已验收的多字段 coordinator，不为 RichText/Text 分别重写发布、Workflow、Copy/Alias、删除和搜索事件流程。
- [ ] 对新增 codec 运行阶段 3–7 的类型专项往返、引用、投影、故障注入和备份恢复验收；跨类型多字段组合任一写失败不得留下可用 Revision，遗留版本进入延迟孤儿报告。RichText/Text 扩展初估 2–3 个工作周，复核组合数量后再定承诺工期；每次上线另走生产授权与 Maker-Checker。
- [ ] 内容型 JSONField 只在业务用途、schema、引用关系、查询需求和容量明确后立项；权限/工作流/Outbox/统计状态 JSON 不纳入外置候选，也不计入上述估算。

### 阶段 8：生产 Maker-Checker 发布预案（另计 3–5 工作日，只在授权后）

- [ ] Sol/架构审核人核对发布 SHA、迁移计划、已批准备份恢复点、索引版本与 alias 切换顺序、关闭写入开关方式和回退条件。
- [ ] 用户单独批准生产备份、生产迁移和发布窗口；Maker 按审核清单逐条执行并回报命令退出码；Checker 独立核对实际结果后才允许下一步。
- [ ] 首次发布只启用经批准的试点 Page type，不做 Blog 存量批量搬迁。先观察 Outbox/ES 类型分布、公开可见性、零结果率和错误率，再逐级扩大范围。
- [ ] 若出现正文哈希/身份错误、跨页命中、权限泄露、旧事件复活、指针/Revision 漂移或持续积压，停止扩大流量并按下述回滚方案执行。
- [ ] 生产验收及稳定观察期结束前，不清理旧集合、旧指针、旧索引或备份；上线闭环后按文档生命周期将设计稿与计划整理为精简实现方案。

## 回滚与恢复原则

1. 发布前保留源代码版本、兼容读取路径、MySQL/Mongo 恢复点和 ES alias/mapping 记录；新 Mongo 版本不可变且在引用核验前不物理删除。
2. 发生故障先停止新 Page 类型接入/新格式写入，保留新旧格式双读和已发布 Revision 可读；不能只关搜索开关或移除 mixin 后就假定正文安全。
3. 若索引错误，暂停对应投影事件并回退 ES alias 或从当前权威 Page/Revision/Mongo 指针重建；通过 Outbox 的 MySQL 代次/tombstone 抑制迟到事件。
   若 MySQL 回到早于 ES 的备份点，必须先隔离旧 Worker 和旧租约并推进持久 recovery_epoch；投递绑定物理目标索引，在新版本域从恢复后的权威 Manifest 重建并核对，原子切换只读 alias 后再恢复投递；不得向仍持较高外部版本的旧索引直接发送新低版本事件。
4. MySQL/Mongo 还原必须由用户单独批准，并在隔离环境验证；生产恢复前冻结相关内容写入，评估备份点之后新增的 Page/Revision/正文。禁止用旧备份无差别覆盖新数据。
5. 不以回滚为由删除 Mongo 内容、清空日志、flush/drop 数据库或批量重绑 Revision。

## 验证矩阵

| 范围 | 核心案例 | 预期 |
|---|---|---|
| Opt-in 边界 | 普通 HomePage/PortfolioPage 与继承试点 Page 并存 | 不继承者不写通用 Mongo，不改变 MySQL 字段行为；继承者 Page/Revision 正文列只写占位值 |
| 字段准入 | 未登记 Text/JSON、短摘要、关系/权限/状态字段、自定义 Field 子类及含 chooser 的长正文 | 仅显式声明字段外置；codec 按类型无歧义匹配；未登记者保持 MySQL；chooser 引用仍校验关系与删除影响 |
| 首期多字段 | 专用 Page 两个 StreamField 外置字段和一个原生字段，任一 Mongo 写失败 | 历史/公开整份 Manifest 精确；任一字段失败不提交可用 Revision；原生字段不变 |
| 后续 codec | RichText/Text 无损往返，空值与未加载区分，跨类型多字段组合 | 不重写生命周期 coordinator；JSON 未定 schema 前不启用 |
| Revision | 新建、重复保存、草稿 overwrite、历史预览/恢复、普通导出/比较、损坏/缺失 Mongo、身份错配 | 已批准/发布 Revision 不覆盖；普通序列化不写 Mongo；精确版本恢复；异常显式失败，不串读 live body |
| 发布 | 后台 PublishPageRevisionAction、Workflow、定时、取消、并发和同版本重复发布 | 调用 Wagtail 原生语义；发布 pointer/generation/Outbox 原子且幂等 |
| 复制与删除 | Copy、跨 Locale、Alias、父子删除、Revision 清理、重试 | 目标身份正确；共享不可变版本引用安全；删除只清已核验无引用版本 |
| 搜索 | blog/guides/pages/all，标题与 Mongo 正文命中，权限、日期、排序、缓存、cursor | 类型互斥；all 三路并集去重；无漏项、重复项或私有结果 |
| 搜索分析 | all fan-out、零结果、cache hit、分页/API/AJAX、重试、类型点击 | 统一计数规则；scope 与 result type 分开；旧历史无类型时不伪造分类 |
| 运维 | Outbox 积压、MySQL 回退但 ES 较新、旧 Worker 迟到、搜索重建、备份还原、Mongo/ES 不可用 | 新版本域重建后恢复投递；可重试/可回滚，恢复报告能证明全部字段指针与 hash 一致 |

## 交付物

- 经评审的详细设计及本计划；
- 新协议代码与迁移清单（若评审后确有必要的数据库 schema 变更）；
- WSL2 定向测试报告、migration dry-run 输出和 fault-injection 记录；
- 隔离环境备份恢复报告、灰度指标和 Maker-Checker 审核记录；
- 生产稳定运行后整合形成的实现方案。当前对话已授权本地分支的分阶段提交；本计划不授予 push 或生产执行权限。
