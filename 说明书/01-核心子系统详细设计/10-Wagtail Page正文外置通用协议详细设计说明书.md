# Wagtail Page 正文外置通用协议详细设计说明书

> 状态：未实施的设计方案；本次仅修订文档，不代表通用能力已经存在。
> 核查日期：2026-09-28。已核对本地仓库源码，并确认 WSL2 测试环境安装 Wagtail 8.0、相关生命周期源码可定位；逐入口调用、事务与信号语义仍须在实施阶段 0 核验。未执行数据库或生产操作。
> 范围：可选正文外置、独立页面类型、全站搜索聚合、索引发布一致性与类型化搜索分析。
> 技术基线：Python 3.13、Django 5.2.8、Wagtail 8.0；依赖精确版本以 requirements.txt 为准。

## 1. 目标与实施边界

未来页面声明自己的业务字段、StreamField 块和编辑面板后，通过继承 `ExternalContentPageMixin` 并定义模型级 `content_storage = {"body": "mongo"}` 显式选择外置字段。未列字段保持 Wagtail/MySQL 原生存储；字段类型只用于从注册表选择 codec，不自动决定存储位置。首期实现可处理同页多个外置字段的通用内核，但首个业务试点 `guides.GuidePage` 仅声明一个 `body: StreamField`；专用测试 Page 用两个 StreamField 外置字段和一个原生字段证明内核可复用。试点后依次开放 RichTextField、TextField codec；内容型 JSONField 在确定业务结构后另行设计。未继承的普通页面保持自身原有存储行为；既有 BlogPage 虽不继承新 mixin，仍保留其现行 Mongo 专用链路。首个试点是全新、无历史数据的 `guides.GuidePage`，其原生 MySQL 父页为 `GuideIndexPage`；现有 HomePage、PortfolioPage 不加 mixin，不调整其字段或正文归属。继承是开发者的能力入口，背后仍需通用应用安装、模型迁移、启动检查和完整生命周期服务。

独立的 GuidePage 可以在前台拥有类型入口；全站搜索“全部”由博客文章、GuidePage 与其他页面组成。正文存储策略、搜索类别和业务功能分别配置，不能把“使用 Mongo”直接等同于“博客文章”。MySQL 页面也可以注册独立搜索类别。

首期包括新页面的草稿、修订、预览、发布、恢复、Workflow、定时发布、复制、别名、翻译、取消发布与删除契约，以及三类搜索和按类型查询统计。未完成验证的生命周期入口必须明确阻断，不得在正文已外置后交回不兼容的默认流程；真实页面全面接入以前须通过全部生命周期门禁。

首期不包含：给 HomePage、PortfolioPage 加 mixin，修改其字段或正文归属，迁移其存量数据；自动迁移 BlogPage 历史正文；未经登记的字段类型与任意 Page 自动外置；作者/媒体模型拆分；所有页面统一物理索引；历史类型统计回填；未经授权的 Git 提交或生产发布。多字段内核及其双外置字段测试是首期验收项，不等于首期开放全部字段类型。

## 2. 已有能力与必须拆开的耦合

以下均为核查到的代码事实，拟建模块不列入现状。

| 现有入口 | 已有行为 | 通用化边界 |
|---|---|---|
| `wagtailblog3/apps/blog/models.py`：BlogPage、BlogPageForm | StreamField 用于编辑，Page 行清空 body；序列化写 Mongo 版本与旧草稿；历史反序列化恢复正文；包含发布、读取、删除入口 | 字段/Revision 适配应抽取；博客作者、分类、标签、AI 面板保持业务层 |
| 同文件：BlogPublicationState | 保存 draft/published/approved 正文版本、哈希、模式与 publication_generation | 是博客专属状态，不是现成通用页面状态表 |
| `wagtailblog3/mongo.py`：MongoManager | 操作 blog_content、blog_page_revision_bodies、content_body_versions | 新版版本已有 aggregate_type/id/hash/schema；save/get 正文版本当前要求 list |
| `wagtailblog3/mongodb.py` | MongoDBStreamFieldAdapter 含博客块名特判和部分异常降级 | 不能原样当作严格通用 codec；未知或损坏内容写入不能降级为空 |
| `wagtailblog3/apps/blog/services/publication.py` | BlogPublicationService、Workflow 指定 Revision 围栏 | 锁定 BlogPage、读取 BlogPublicationState；不是任意 Page 发布服务 |
| `wagtailblog3/apps/blog/services/page_deletion.py`、blog/signals.py、blog/wagtail_hooks.py | 删除意图、引用检查、后台删除防线 | 存在 BlogPage 类型限定；祖先删除和混合页面批量删除必须一起审计 |
| `wagtailblog3/apps/search/signals.py` | 发布、下架与权限变化触发搜索投影 | 部分正式指针维护被 CONTENT_SEARCH_PRODUCER_ENABLED 包围 |
| `wagtailblog3/apps/search/services/outbox.py`、document.py、delivery.py、scope.py、rebuild.py | 持久 Outbox、搜索状态、投递、权限重算、索引重建 | 多处绑定 BlogPage、blog_page 身份及博客正式状态 |
| `wagtailblog3/apps/search/services/federated_query.py`、pages_query.py | all 联合博客流与普通 Page 流；普通流排除 BlogPage | 当前是两来源模型，未提供通用三类型注册表 |
| `wagtailblog3/apps/search/analytics.py` | 基于 QueryDailyHits 的按日热词与趋势 | 没有搜索类型、单次事件、点击或小时数据 |
| `wagtailblog3/apps/home/models.py`、portfolio/models.py | HomePage.body 为 RichTextField；PortfolioPage.body 为 StreamField | 均走 MySQL；不能在无迁移授权时改变存量归属 |

仓库已有 `base` 应用，但尚无本文拟建的 `external_content` 通用正文应用及独立 `guides` 业务应用。`GuideIndexPage` 和 `GuidePage` 已确定为拟建业务模型，当前仓库尚无其实现；具体业务字段与页面详见《11-专题指南页面需求与详细设计说明书》。

### 2.1 搜索开关与正式指针必须先拆

`search/signals.py` 的 `record_blog_page_published` 在搜索生产开关内调用 `ensure_published_revision`；下架信号在同一开关内推进代次并清空正式指针。现有 `BlogPage.publish()` 自身也能推进正式指针，因此不能概括成“所有发布都依赖搜索开关”；问题在于后台直接 Action 等入口的补偿覆盖与搜索配置耦合。

设计要求：正文发布状态由正文生命周期服务维护，关闭搜索生产、停止消费者、切换查询后端均不得改变正文发布/取消发布正确性。先证明后台 Action、Revision.publish、Workflow 和定时命令在搜索关闭时仍正确，才可迁移通用服务。

搜索开关拆分为三类语义：持久搜索事件生产、消费者投递、前台查询切流。对已纳入搜索协议的页面，常规停机只暂停投递或查询，继续积累 Outbox；若确需暂停事件生产，必须登记可重建缺口，重新查询切流前完成对账。不得用搜索故障回退到错误正文。

## 3. 架构决定

| 编号 | 决定 |
|---|---|
| AD-01 | opt-in 使用 `ExternalContentPageMixin`；不新增具体 Page 中间父表，不继承 BlogPage 来获取 Mongo 能力 |
| AD-02 | Page 子类通过 `content_storage` 显式声明外置字段；未列字段归 MySQL。首期多字段内核支持已登记的 StreamField，业务试点仅登记 body；RichTextField、TextField codec 后续开放，JSONField 需独立业务评审；标题、关系、路由、权限、Locale 与 Revision 元数据仍归 MySQL |
| AD-03 | Revision 唯一命名空间为 `_external_content_manifest`；本文不使用其他 Manifest 顶层别名 |
| AD-04 | 通用新正文使用拟建 `external_content_versions`；旧三集合与 `blog_page` 聚合身份原样由兼容适配器读取，不原地重命名或改写 |
| AD-05 | 正文版本不可变；页面级 published_manifest 和 publication_generation 是公开正文权威，独立于搜索开关 |
| AD-06 | 发布复用 Wagtail 原生发布 Action；禁止以手动修改 live/live_revision 等字段替代发布 |
| AD-07 | 对已登记索引的页面，Wagtail 状态、正式指针、搜索 State/Outbox 同一 MySQL 事务；消息仅在提交后唤醒 |
| AD-08 | 搜索范围 blog/guides/pages 互斥，all 为并集；存储注册和搜索类型注册独立 |
| AD-09 | Copy/翻译/Alias 派生目标页独立正文身份；Alias 不伪造本地 Revision |
| AD-10 | 新分析区分 search_scope 和 result_type；旧 QueryDailyHits 仅提供无类型总量 |
| AD-11 | 首个试点只使用新建 Page 模型；HomePage、PortfolioPage 等未继承模型保持现有存储与生命周期行为 |
| AD-12 | 不按字段类型、长度或字段名自动外置；外置字段仍由 MySQL Revision Manifest 引用，包含页面/图片引用的内容继续接受关系与权限校验 |

### 3.1 分层与拟建入口

| 拟建或改造边界 | 职责 |
|---|---|
| `external_content/mixins.py` | MRO 协作、正文加载状态、保存/序列化接入；具体类继续继承 Wagtail Page |
| `external_content/registry.py`、checks.py | 读取模型级存储策略，按真实 Field 类型匹配 codec，检查未知字段、冲突、禁入字段及版本；不在检查中连接 Mongo 创建索引 |
| `external_content/manifest.py`、codecs/streamfield.py | 字段集合指针解析、规范化、严格编码/解码、各 Field 空哨兵规则；codec 不编排发布/复制/删除 |
| `external_content/store.py` | 不可变版本写入和身份/哈希读取验证；不承担 Page 发布或搜索 |
| `external_content/revisions.py`、publication.py、reading.py | 修订准备、精确发布、草稿/正式读取隔离 |
| `external_content/copying.py`、deletion.py | 复制/别名/翻译、删除清单与补偿 |
| `external_content/adapters/legacy_blog.py` | 旧 BlogPage 指针、旧集合与 BlogPublicationState 的兼容；不引入双重公开权威 |
| `search` 中类型注册、来源与投影接口 | 业务类别、公开查询、索引文档、搜索结果协议；复用现有 Outbox/Delivery 编排 |
| `search` 中类型分析采集与聚合接口 | 统一查询事件与按类型日聚合、去重规则和后台报表 |

上述路径均为拟建边界，不是已存在接口。实现时按职责拆分，避免将全部服务塞进 mixin。若 mixin 需要 Django 字段，必须采用 abstract=True 模型并核查迁移；首期优先由独立状态表保存指针，不在每个业务类复制旧 mongo_content_id。

### 3.2 字段准入与扩展顺序

显式登记的字段须由单个 Page 修订拥有，通常按 Page/Revision 整体读取，不承担 MySQL 过滤、排序、权限判断或外键约束。候选为长篇 StreamField/RichTextField 正文、原始 Markdown 或转录稿等 TextField，以及业务结构已确定的内容型 JSONField。短摘要、标题、slug、日期、Locale、权限、作者、分类、标签、状态、统计键及关系字段继续由 MySQL 管理；图片和文件使用现有媒体存储。不能因 Django 字段类型为 TextField/JSONField 或内容达到长度阈值就自动外置，错误日志、提示词配置、搜索事件等其他业务模型也不自动接入。

模型策略只表达存储选择，例如 `content_storage = {"body": "mongo", "notes": "mongo"}`；未列出的 `summary` 等字段完整保留原生保存、Revision 和查询语义。框架依据实际 Django/Wagtail Field 类，从 codec 注册表按最具体兼容类型解析 `streamfield.v1` 等实现，并把选择结果固定在 Manifest；自定义子类必须显式声明兼容性，RichTextField/TextField 继承匹配不能误选通用 text codec。未知字段、同一字段重复/冲突 codec、关系/权限/路由/状态字段，以及未经支持的类型均在启动检查中失败。首个业务试点仅声明 `body`，专用测试 Page 声明两个 StreamField 外置字段及一个 MySQL 原生字段，走同一 coordinator 验证多字段闭环。其后开放 `richtext.v1` 与 `text.v1`；RichText 保存 Wagtail 序列化的原始富文本字符串，TextField 保存原始字符串（包括 Markdown），均不在入库时渲染或改写。RichText/StreamField 内嵌页面、图片等 chooser 引用仍须校验目标身份和删除影响。

实现入口采用以下职责契约，供工程师逐层落地：

| 输入或接口 | 行为与边界 |
|---|---|
| 模型类属性 `content_storage`、`content_storage_schema` | 开发时确定字段归属与正整数策略版本；例如 body 选 mongo、summary 未列则留 MySQL。它们不是数据库逐篇开关，也不替代 Django 字段定义。 |
| `resolve_storage_policy(model, schema_version)` | 从受版本管理的策略注册记录还原该版本的字段归属；启动时比较当前类声明与注册快照，同版本变更拒绝启动。历史策略及所需 codec 在仍有引用时不得移除。 |
| `resolve_codec(field)` | 对声明为 mongo 的真实 Field 选择唯一、最具体的已登记 codec；仅负责类型匹配，绝不自动把其他字段迁往 Mongo。 |
| `prepare_revision(page, fields, policy)` | 一次收集并写齐字段集合，返回完整不可变 Manifest；失败不返回部分结果。Mongo 成功但 MySQL 未提交的版本只进入延迟孤儿治理。 |
| `hydrate(page, manifest, context)` | 按指定 Revision 或正式 Manifest 逐字段验证身份、哈希并还原内存值；普通 SQL 行的占位值不能参与正文读取。 |

`content_storage_schema` 必须对应一份可持久追溯的策略快照或代码注册记录，单有版本数字不足以解释旧 Revision。策略记录至少固定 model_label、字段名、存储位置及当时 codec 兼容范围；代码升级时保留旧解码器和旧 MySQL 字段的历史 Revision 读取能力。新增全新 Page 的初始策略为 v1；已有字段变更存储位置是独立迁移项目。

历史字段重命名、删除或 StreamField 块定义变更时，策略快照须保存旧字段名映射与旧块解码能力，或提供经测试的只读兼容适配器；未证明旧 Revision 可还原前禁止移除旧解码器。无 Manifest 的 Revision 只有被明确识别为旧 Blog/原生 MySQL 格式时才走对应 legacy 读取；已经接入新协议的 Page 缺 Manifest 视为损坏，不得以当前策略补造。

内容型 JSONField 需先确定 schema、字段用途、查询需求与引用类型，再设计 `json.v1`；不把权限、工作流、Outbox 或报表状态 JSON 搬入 Mongo。第一版即对字段集合统一编排：先写齐并校验每个 Mongo 不可变版本，再以一个 MySQL Revision Manifest 原子引用整组字段；某一字段失败即不提交新 Revision，已写而未引用的版本进入延迟孤儿治理。增加同一协议已支持的字段无需改生命周期代码；改变 Manifest 键或语义时提升协议版本并保留旧版本解码。

## 4. 存储权威与 Manifest 协议

| 数据 | 权威存储 | 约束 |
|---|---|---|
| Page 树、标题、权限、关系、Locale、工作流、Revision | MySQL | 保留 Wagtail 生命周期与序列化关系 |
| 接入字段的正文 | Mongo 不可变版本 | 原始 Markdown 不渲染入库，块 ID 和引用 ID 稳定 |
| 草稿/批准/公开版本身份 | MySQL Revision 与页面级正文状态 | 草稿与公开指针隔离，禁止 latest_revision 代替批准版本 |
| 搜索期望与持久事件 | MySQL ContentSearchState/Outbox/Delivery | 保留现有 content_version 和事件状态机 |
| 搜索文档 | Elasticsearch | 可重建投影，不是正文事实源 |
| 缓存 | Redis/请求内存 | 版本化派生数据，不能补造正文身份 |

### 4.1 Manifest 唯一结构

以下是第一版通用字段集合契约，不是可直接导入的数据。`model_label` 使用 `_meta.label_lower`，首个业务模型为 `guides.guidepage`。`page_id` 必须是已持久化的 Wagtail Page 主键。`fields` 的键恰为该 Revision 的已外置字段集合；业务试点只有 body，专用测试 Page 至少有两个外置字段。字段名必须与该 Revision 的策略版本及真实模型字段对应。

| 路径 | 类型与含义 |
|---|---|
| `_external_content_manifest.protocol_version` | 整数 1；未知主版本拒绝使用 |
| `_external_content_manifest.content_storage_schema` | 正整数；模型级字段存储策略版本，历史 Revision 按记录版本解析，禁止按当前配置猜测 |
| `_external_content_manifest.model_label` | 非空小写 app_label.model_name |
| `_external_content_manifest.page_id` | 正整数 Page 主键，必须与 Revision 所属页面一致 |
| `_external_content_manifest.fields.<field_name>.codec` | 注册表解析出的稳定 codec ID；首期允许已登记 StreamField 的 `streamfield.v1` |
| `_external_content_manifest.fields.<field_name>.schema_version` | 正整数；该 codec 的 payload schema 版本，由 codec 明确支持 |
| `_external_content_manifest.fields.<field_name>.version_id` | 仓储返回的全局唯一不透明版本 ID |
| `_external_content_manifest.fields.<field_name>.payload_sha256` | 64 位小写十六进制 payload 摘要 |

Revision 中每个外置字段保留该 Field/codec 要求的空哨兵，未外置字段保留完整原生值；StreamField 的内存空值、数据库 JSON 空值和 Revision JSON 表达分别由适配器确定，不能把字符串 `"[]"`、列表 `[]` 和 None 混用。现有 Blog Revision 使用 `body='[]'` 是兼容事实，不是所有类型的通用约定。普通 Page SQL 行的外置字段同样只是占位值，`values("body")` 等 ORM 直读不得作为正文 API。

Manifest 解析必须校验键集合、类型、协议版本、存储策略版本和字段登记。缺失或部分新指针视为损坏，不能静默走旧读路径。指针身份包括 model_label、page_id、field_name、codec、schema_version、version_id、payload_sha256，任何一项不匹配均阻断历史恢复或发布。策略变更后仍按历史 Manifest 所记版本读取；不能将旧 Revision 的缺失字段解释成当前 Mongo 字段的空值。

### 4.2 新 Mongo 文档与哈希

拟建 `external_content_versions` 文档包含：model_label、page_id、field_name、codec、schema_version、version_id、payload_sha256、payload、created_at。首期 payload 直接保存 StreamField 原始块列表，不增加 blocks 包装。version_id 唯一；页内幂等唯一键为 model_label + page_id + field_name + codec + schema_version + payload_sha256。

规范化使用确定性 JSON 表达，明确 UTF-8、键排序、分隔符和禁止 NaN；列表顺序、块 ID、原始 Markdown 字符串保留。哈希覆盖规范化 payload，codec/schema/归属另行强校验。不允许用渲染 HTML 或搜索纯文本计算正文身份，也不能在每次读取时随机重建块 ID。

相同归属和相同内容复用版本；只允许插入，不用 `$set` 改写已存在正文。读取时重算摘要并校验完整身份。数据层必须抛出明确错误；不能把编码失败、未知块或 Mongo 超时解释为空正文。历史缺 ID 的修复仅在兼容层明确处理，不能在未经授权时写回原文。

现有 `content_body_versions` 的 body_data 是 list，存储字段名为 body，身份是 aggregate_type/aggregate_id/body_schema_version 等，不能直接替换为新文档结构。首期通过独立兼容适配器与新仓储并存；旧 BlogPage 专有的 `body_sha256` 名称保持不变。后续 RichText/Text 的字符串 payload、内容型 JSON 的结构化 payload 各自新增 codec 与往返校验，不重写通用多字段 Manifest、发布、复制和清理编排。

### 4.3 页面级正文状态

拟建 ExternalContentPublicationState 使用稳定 page_id 和 model_label 标识；保存 draft_revision_id/draft_manifest、approved_revision_id/approved_manifest、published_revision_id/published_manifest、publication_generation、更新时间。Revision ID 为逻辑身份并配合受控引用检查；不采用会在 Page 删除时自动清空审计证据的级联设计。

Alias 的 published_revision_id 为空，记录 source_page_id/source_revision_id 与目标页 published_manifest；源 Revision 只是来源证据，不能伪装为 Alias 自有 Revision。普通页面 published_revision_id 必须归属该页。所有引用均由清理服务复核，保留状态与墓碑直至安全终结。

公开代次由每次实际公开状态变化单调推进；同一动作的重复回调幂等。仅正文哈希相同不代表公开状态相同：标题、权限、URL 或发布 Revision 变化仍须正确更新投影。publication_generation 与搜索 content_version 分工不同，不相互替代。

## 5. Wagtail 生命周期接入

### 5.1 序列化与内存正文保护

Wagtail `save_revision()` 调用具体页面 `serializable_data()`；modelcluster 序列化还负责子关系。`from_serializable_data()` 用于重建历史实例，并有 check_fks/strict_fks 等参数契约。mixin 必须协作调用 super，保留参数与关系行为，不能自行生成只有 title/body 的简化 Revision。

受控保存 Revision 的顺序固定为：取得真实 page_id 和当前模型存储策略 → 显式 `prepare_revision` 收集全部外置字段并严格编码 → 写齐不可变 Mongo 版本、确认写入并校验指针 → 在同一个 MySQL 事务内保存原生字段、外置字段占位值、完整 Manifest 与草稿状态。受控序列化上下文先调用 `super().serializable_data()` 保留关系数据，再仅修改输出字典中的外置字段；不能先清空模型实例。若原生 Page SQL 保存确需临时替换实例字段，必须限定在受控上下文并用 `finally` 恢复，且不得在替换期间再生成 Revision。

`serializable_data()` 还可能用于复制、比较或普通导出，调用本身绝不写 Mongo。只有显式 `prepare_revision` 能创建版本；受控序列化上下文只消费已经准备好的完整字段指针。普通序列化按调用目的返回原生值或已存在的快照表示，不制造新版本，具体 Wagtail 路径在阶段 0 逐一验证。仓储写入幂等，对尚无主键实例不制造伪 page_id；复制必须通过独立 coordinator 派生目标身份。

每个外置字段分别区分“尚未加载”“显式空值”“已有完整值”。defer/only、metadata-only save、刷新实例和表单局部更新不能把未加载字段当成用户清空。新外置页面普通 save 不得悄悄吞掉非空字段；字段变更必须走已定义的修订保存入口，非法直存应明确拒绝。初始化创建页的元数据阶段有显式内部上下文并处于整体编排内。

### 5.2 逐入口行为

| 入口 | 设计要求与失败边界 |
|---|---|
| 新建/add_child | 保留提交正文，在 MySQL 编排内经原生树操作取得真实主键，再存 Mongo 与 Revision；失败回滚 MySQL，残留 Mongo 交延迟孤儿清理 |
| 保存草稿 | 只推进草稿 Revision/Manifest，不改公开正文和公开搜索；Mongo 失败不产生可用的空 Revision |
| 未保存预览 | 使用本次表单内存正文，不能误载公开版本覆盖用户输入；不产生公开指针或 Outbox |
| Revision 预览、比较 | 精确恢复指定 Manifest；版本缺失、身份错误或模式不支持显式报错，不回退到最新正文 |
| 历史恢复 | 从历史正文恢复编辑状态后生成新 Revision；旧 Revision 不改写，公开正文只在再次发布后切换 |
| 普通/后台发布 | 经通用生命周期适配器进入原生 Wagtail 发布 Action，执行第 6 节事务契约 |
| Workflow | 批准绑定准确 Revision 和正文身份；首期保持现有保守策略：最新 Revision 已漂移则阻断并要求重新审核，不自动发布新草稿 |
| 定时发布 | 按 Wagtail 已批准 Revision 和 approved_go_live_at 执行，校验到期、取消/替代状态及精确身份；不把最新草稿当排期版本 |
| 取消发布/到期 | 正式状态与公开代次独立维护；搜索墓碑与原生下架同事务，保留历史正文 |
| API/导入/脚本 | 统一调用生命周期入口；不能用 QuerySet.update/bulk_update 绕过正文版本和发布协议 |
| 直接 ORM 查询/模板/搜索提取 | 通过显式读取上下文获取正文；SQL 空哨兵不得被当作完整正文索引或展示 |

`Revision.publish()` 委托具体内容对象 publish，但 Wagtail 后台可直接使用 PublishPageRevisionAction，UnpublishAction 也可能绕过子类方法。必须在实施前核实这些入口的事务、信号和 Alias 时序，形成统一适配；仅覆盖 mixin.publish 不足以验收。信号接收器负责幂等边界接入，不在多个接收器重复推进正式指针。

Revision ID 不能单独代表正文版本。若 Wagtail 草稿保存复用同一 Revision 行，须在同一 MySQL 事务更新其完整 Manifest 与草稿状态，并确保旧 Manifest 没有被审批、发布、排期、Workflow 或其他持久引用；任何已有此类引用的 Revision 一律新建 ID，不允许覆盖。阶段 0 必须用实际 Wagtail 8.0 源码和测试确认何时传入 overwrite、谁持有目标 Revision；若不能可靠拦截覆盖，则外置 Page 全部采用新 Revision ID。历史读取始终以指定 Revision 的 Manifest 为准，不从 Revision ID 推算 Mongo 版本。

overwrite、审批、排期与发布共用 Page/Revision/状态的固定锁顺序。事务外预取只用于缩短校验准备时间；锁内必须重读目标 Revision，比较完整 Manifest 指纹及审批状态。若已变化，旧预取结果作废并重新读取校验，不能用陈旧正文继续发布。

### 5.3 Copy、Alias 与翻译

Copy 先按 Wagtail 选定的源状态恢复 Manifest 中全部字段，再创建目标 page_id 和目标独立版本。复制历史时，逐个目标 Revision 重新绑定全部字段的目标身份；不改源 Revision，也不假定所有复制操作都携带完整历史。普通 Page 元数据、子关系、权限及 copy_revisions 等原生选项必须保持语义。

Alias 跟随源已发布 Manifest 的全部字段，不拥有自己的草稿 Revision。源发布时在同一受控操作中为 Alias 目标 page_id 派生全部字段的独立版本、目标正式指针和搜索事件；对锁集合按稳定 Page ID 排序，失败整体回滚 MySQL，未引用的 Mongo 版本进入延迟孤儿治理。不得将源 page_id 绑定的版本指针直接作为目标 Manifest，也不得发布源页后留下已经宣称同步但正文不一致的 Alias。解除 Alias 时由原生转换过程生成独立页面修订，绑定已经校验的目标副本。未发布源、别名链、源删除和独立权限均列入门禁。

翻译产生独立页面身份与 Locale，遵守与 Copy 相同的正文隔离；权限与搜索必须以目标 Locale/页面身份判定。历史导入、复制和转换全部禁止共享旧 mongo_content_id 等可变指针。

## 6. 发布事务、索引与补偿

### 6.1 精确发布流程

1. 解析目标 Revision，确定真实页面类型和待发布正文身份；可在事务外预取不可变正文以缩短锁时间，但此结果不替代事务内身份围栏。
2. 开启覆盖整个发布 Action 的 MySQL 事务；按固定顺序锁 Page、必要子类行、正文状态、相关 Revision/Workflow 状态，校验 Revision 归属、删除意图及审批/排期约束。
3. 遍历 Manifest 全部字段并验证不可变 payload 完整性，建立并发清理围栏。任何缺失/损坏均阻断，不能提交正文未知的 live 状态。
4. 由原生 Wagtail Action 完成发布关系、日志、信号、别名更新和排期语义；正式指针推进安排在经核实的 Action 内部阶段，与搜索投影读取时序一致。仅安排未来发布时间不切换当前正式正文。
5. 对实际发布推进整份公开 Manifest 与代次，并在同一事务内记录搜索期望 State/Outbox。Outbox 快照绑定这次 Revision/Manifest/代次；不得事后读取最新草稿。
6. 提交后唤醒消费者。消息发送失败保留持久事件，由补偿扫描重试；MySQL 回滚时 Page、正式状态和搜索事件一起撤销。

所有入口必须共享这一外层事务。不能在 Wagtail Action 已提交后再用一个独立事务补写正式指针，并宣称发布原子性；若原生入口无法直接满足，需要适配原生 Action 调用层并验证，不能直接赋值 live=True 模拟发布。

MySQL 与 MongoDB 没有可假定的跨库原子事务：先确认所有 Mongo 版本持久写入，再提交 MySQL 引用；MySQL 失败只留下不可见孤儿，不能留下指向缺失正文的已发布状态。ES 投递走持久 Outbox，失败重试而不回滚已提交发布。写确认、超时后结果不确定及重复键分支均须按幂等身份重读校验；不得把跨库操作描述成同一个数据库事务。

Mongo 写入确认须按部署拓扑制定并验证 write concern、journal 与故障切换后的读取保证；复制集优先要求 majority 与 journal 确认。若实际拓扑不能提供所需持久保证，必须明确 RPO、阻断自动公开切换或停止上线，不得把普通 ACK 或紧接着重读成功等同于故障后仍可恢复。

### 6.2 Outbox 沿用与扩展

现有 ContentSearchState/Outbox 使用 page_id、content_version、operation、searchable、content_hash、body_version_id、publication_generation 等字段；operation 为 upsert/tombstone。它们没有旧示例中的 page 外键、event_type 或任意 payload 契约，禁止凭空按这些字段创建记录。

设计扩展通过明确迁移加入类型与公开快照身份，例如 model_label、search_type、published_revision_id、整份 Manifest 快照/摘要；最终迁移须同时更新 State、Outbox、Delivery、重建及旧事件解码。旧 Blog 专有 `body_version_id` 可保留兼容语义，但通用多字段事件以完整 Manifest 为权威，不能由单个 body 指针代表整页。不能在文档中假设扩展已经落表。

保留现有 content_version 单调顺序、消费者租约、重试、目标索引扇出和事件过期判断。投递前比较 MySQL 期望版本；索引写入也必须有原子版本围栏，禁止“先 ES GET 比代次，再无条件 INDEX”的竞态方案。迟到 upsert 不能让已取消发布或已删除页面复活。

墓碑保留版本与不可搜索状态，并清除标题、摘要、正文等内容；不能以简单物理 DELETE 丢掉版本围栏。Elasticsearch 投递失败不撤销已提交的页面发布，事件进入重试；正文未知的 upsert 不能覆盖为普通空文档。

### 6.3 权限与重建

公开搜索统一使用已发布、公开且在指定 Site/Locale 范围内的 Page。检查祖先 PageViewRestriction、移动、别名权限和下架；不能只检查 live 或不存在的通用 expired 属性。权限收紧与移动触发按子树重算，登记所有受管类型。

ES 候选返回前以 MySQL 公开范围、当前正式 Manifest 摘要及公开代次复核，缓存命中也不能跳过；版本不符的命中、摘要和缓存条目须丢弃并触发重新投影。命中总数、分面、摘要和推荐词不能泄露受限或已失效内容；不能把过滤前原始 ES count 当成权威公开计数。索引陈旧期间精确总数无法安全计算时，应给受控不可用/明确有界结果，而非虚构精确数字。

重建只读取正式快照，覆盖全部已登记类型；保留现有读写别名和构建目标流程。博客现有别名不在首期擅自更名；新类型可用独立索引来源，先统一逻辑文档/查询协议。默认 Wagtail 自动索引、update_index 与旧博客管道必须审计，避免草稿、空哨兵或重复来源进入公开结果。

MySQL 恢复到早于 ES 的备份点时，旧 ES 外部版本可能高于恢复后的 content_version，导致新事件长期被拒。恢复顺序必须冻结新投递、隔离旧 Worker 与旧 Outbox 租约，推进持久 `recovery_epoch` 围栏；每个投递任务绑定该 epoch 的物理目标索引，Worker 投递前核对持久 epoch。在新物理索引/版本域从恢复后的 MySQL 正式 Manifest 和 Mongo payload 重建，核对后原子切换只读 alias，再放行新 Worker；旧进程即使持有已发出的请求也只能写旧物理索引。不得在旧索引上直接续投较低版本，恢复演练必须覆盖旧 Worker 迟到事件与 alias 切换竞态。

## 7. 安全删除与版本回收

页面删除采用持久清单：记录模型、Page ID、每个 Revision 与正式/批准/草稿 Manifest 的全部字段指针、旧指针、搜索目标与待处理子页。先锁定页面并登记删除意图，阻断新写入和发布；同事务写 tombstone。清单必须覆盖父级删除、QuerySet 删除和混合批量入口，不能只覆盖 `GuidePage.delete()`。

执行顺序：持久删除意图并阻断该页所有草稿/公开读取入口的新使用 → 确认各搜索目标下架 → 在授权的删除操作中按原生 Wagtail 语义终结 MySQL 页面树和关联 Revision，同时保留独立、不可变的删除清单与版本围栏 → 安全期后复核全部存活引用 → 精确回收已无引用的 Mongo 版本。每步幂等，可重试；中间失败保留状态，不清空意图伪装完成。清单尚未终结或有任何引用时禁止物理回收，正文清理许可针对明确清单，不按页面前缀盲删。

引用集合按 Manifest 的每个字段遍历，包括存活 Revision、draft/approved/published Manifest、进行中 Workflow/排期、复制/别名任务、未终结删除及索引投递/重建任务。回收使用与发布/修订共用的 Page 锁及持久 claim，引用复核后禁止新引用进入；实际跨库步骤通过状态机完成。新写正文尚未提交 Revision 的窗口也必须受保护。

孤立版本不能只凭“当前找不到 Revision”立即删除；需满足安全期、无活动写入/复制 claim、无全量引用，并再次复核。Mongo 不可用、引用清单不完整或清理围栏失效时停在可恢复状态。

## 8. 页面类型与三类搜索

### 8.1 注册与结果契约

搜索类型注册表定义：稳定 key、显示名称、model_labels、公开 QuerySet 提供器、正文投影适配器、日期字段语义、摘要/结果模板与能力标志。`guides` 已确定显示为“专题指南”，对应 `guides.GuidePage`；URL 与统计使用稳定 key，展示改名不改变历史维度。

| type | 范围 |
|---|---|
| blog | BlogPage 文章 |
| guides | `guides.GuidePage`，显示名“专题指南” |
| pages | 其他可搜索的原生 Page；显式排除 blog、guides 及一切尚未注册公开搜索适配器的外置 Page |
| all | 上述三个互斥范围的并集，以 Page ID 去重 |

来源适配器统一返回 page_id、model_label、search_type、title、url、summary、published_at 和稳定排序身份；正文外置来源另携带 Manifest/代次。MySQL 页面不伪造 Mongo 指针。普通 Wagtail 来源先排除所有外置模型，再由已注册的外置搜索适配器明确纳入相应类别；未注册者不得凭默认 Wagtail 索引旁路进入搜索。首期沿用现有博客来源和 Wagtail 普通来源，增加 专题指南 来源；无需先迁移所有物理索引。

### 8.2 前台、API、缓存与游标

前台增加“全部 / 博客文章 / 专题指南 / 其他页面”。专题指南独立搜索入口固定 `type=guides` 并复用统一服务，不复制查询逻辑。`GuideIndexPage` 为原生 MySQL 页面，归其他页面；同步改造 search/views.py、api.py、core.py、模板、前端请求和建议词策略；建议词若仍仅服务博客，必须明确范围，不能冒充当前类别热词。

所有 HTML/AJAX/API 共用参数校验：规范化 query、合法 type、Site/Locale、日期范围、排序、分页模式。非法 type 明确拒绝，不能静默按普通页面查。日期使用每类登记语义，统一可比较的时区时间与主键稳定次序，不能把 date.toordinal 与 datetime.timestamp 直接混排。

缓存键包含 query、type、范围、日期、排序、页大小、游标状态及实现/索引代际。正文缓存按 page_id + Manifest/代次 + codec 版本隔离；预览和用户私有状态不得写进公开缓存。

现有 FederatedSearchResults 写死两来源且有候选上限。它的游标分别取各来源一页、合并仅返回一页后同时推进来源，存在未返回候选被越过的风险；三来源扩展必须先验证并修正消费协议。

新游标签名绑定全部查询条件、来源集合、注册表/协议版本；每路保留未消费候选或仅推进实际消费位置。游标中不存正文或敏感凭据。相关度以来源内排名做有界融合，日期排序用统一日期；分别规定总数是精确值还是候选窗口统计，不混用。来源错误不得伪装该类别零结果；首期 all 保持必需来源失败则整体受控报错的语义。

## 9. 类型化分析协议

### 9.1 已有数据限制

SearchAnalytics 当前只读 QueryDailyHits，能展示规范化词的日/周/月/年聚合，无法恢复类型、单次耗时、点击或小时分布。新类型上线后，这批历史仍标为“历史全站总量/未分类型”；禁止按结果占比猜分，也不能和新数据重复相加。

### 9.2 拟建事件与聚合

统一查询入口产生类型化事件，拟建字段：event_id、search_id、event_kind、occurred_at、query_normalized、search_scope、entrypoint、site_id、locale_id、status、result_count、duration_ms。status 区分成功、失败；result_count 只有权威公开计数可用时记录，否则为空，不用 0 代表后端故障。

search_scope 取 all/blog/guides/pages，表示用户选择。result_type 表示实际结果类别，仅在结果曝光/点击事件中使用；查询 all 不能被拆记为三次类型查询。若未来采集点击，再增加 result_page_id、result_type、rank 等事件字段；本期不把点击率写成已有指标。

查询、分页和点击分别记账：一次用户提交 query/type/filter 形成 search_id 和一条 query 事件；翻页为 page 事件，不增加查询次数；请求重试复用幂等事件身份。HTML、AJAX、API、缓存命中和独立 专题指南 入口使用同一规则，后台内部调用来源不计用户查询。

首期报表支持按 search_scope 的查询量、热词、趋势、可确认的零结果和失败量；新日聚合以日期、规范化词、search_scope 及所需站点/语言维度为键。采集失败不能中断搜索，但须有可观测丢失/重试状态。生产切换时确定旧总量与新数据的时间边界，不伪造可比精度。

仅收集完成指标所需字段，不采集正文、Cookie、Token、用户密码；查询词可能含用户输入信息，应按现有后台权限展示并制定留存期。

## 10. 兼容与渐进接入

1. 先固定现有 BlogPage 行为与历史指针读取测试，拆开搜索开关和正式状态耦合。
2. 新建通用多字段内核及专用测试 Page，以两个 StreamField 外置字段和一个 MySQL 原生字段验证完整闭环；不改 Home/Portfolio 存量正文。
3. 新业务 GuidePage 使用新 Manifest/集合和通用服务；注册独立搜索类型，完成索引、三类前台与分析采集。
4. BlogPage 通过 LegacyBlogRevisionAdapter 接入公共接口，保留旧 aggregate_type=blog_page、mongo_draft_pointer、mongo_body_version_id、body_sha256、body_schema_version 与 mongo_content_id 读取语义。
5. 同一页面在同一阶段只有一个正式指针权威和一个事件生产者。禁止 BlogPublicationState 与新状态同时独立推进；切换需要明确 checkpoint、逐页对账和可回退读路径。
6. 首个新 Page 的 StreamField 闭环验收后，在独立扩展批次接入 RichTextField、TextField codec；内容型 JSONField 只有具体业务模型和 schema 确认后再立项。新字段复用既有 coordinator、Manifest、发布、复制、删除流程，只扩展 codec 与针对该类型的无损/引用测试。各批次继续执行完整生命周期与备份恢复门禁，不改变未继承页面。

旧 Revision 原始内联正文、旧草稿指针和新版不可变指针分别解码。新协议指针失败不回落为旧正文；只有明确属于旧协议的数据才走 legacy 分支。现有 Blog 序列化兼容写旧草稿是当前事实，不意味着新页面也需双写旧集合。存量迁移、停写旧集合及删除旧数据分别需要后续授权和证据。

模型已经有历史 Revision 后，不能仅把 `content_storage` 从 MySQL 改为 Mongo，或从 Mongo 改回 MySQL。此类变更必须先发布新的 `content_storage_schema`，提供旧策略读路径与新策略写路径，盘点历史 Revision/正式指针，制定逐页迁移、双读校验、备份与回退计划；经另行授权后执行。未迁移历史始终按其 Revision 记录的策略读取。策略版本、Manifest 协议版本和 codec/schema 版本分别管理，不以一个数字混代。

## 11. 验收矩阵与未决实施点

| 门禁 | 必须证明 |
|---|---|
| 非接入页面 | Home/Portfolio 仍按原生 MySQL 行为运行；无额外 Mongo 调用 |
| 字段策略/Manifest | 模型显式选择外置字段；专用 Page 双 StreamField 外置 + 一个原生字段；原始 Markdown、嵌套块、chooser 引用和块 ID 往返；未知版本/跨页指针/哈希损坏失败关闭 |
| 后续 codec 扩展 | RichText 原始富文本与 TextField 原始字符串无损往返；生命周期 coordinator 无需分叉；内嵌引用仍受保护；JSON 需先有业务 schema |
| 创建与保存 | 新建失败不丢内存正文；SQL/Revision 不落大正文；metadata-only、defer 与显式空正文严格区分 |
| 修订与预览 | 双外置字段任一失败不产生可用 Revision；草稿覆盖不得改已批准/发布 Revision；普通序列化不写 Mongo；旧版恢复精确；未保存预览不串公开缓存 |
| 发布事务 | 原生 Action、Revision.publish、Workflow、定时、取消发布都受控；搜索关闭时正式指针仍正确 |
| 并发与补偿 | 同版本回调幂等；不同公开变更代次递增；Mongo/SQL/消息/ES 故障边界清楚 |
| Copy/Alias/翻译 | 目标独立身份；Alias 无虚假 Revision；复制历史和解除别名正确 |
| 删除 | 父级/批量入口不绕过；引用保护及清理 claim 关闭竞态；迟到索引事件不复活 |
| 搜索 | 三类互斥；权限、计数、日期、缺源错误、深分页、缓存和游标一致 |
| 分析 | all 一次计数；翻页/重试不重计；历史不猜分；失败不计零结果 |
| 兼容与恢复 | 旧 Blog 指针和 State/Outbox 可读；历史策略版本精确解码；MySQL 回退后隔离旧 Worker 并重建 ES 版本域；单一权威、单一事件生产者；回滚不改受保护正文 |

实施前需在测试环境核定的具体点：Wagtail 8.0 各 Action 的事务/信号接入位置、Alias 整体同步锁顺序、默认搜索后端的自动索引旁路、现有游标缺口复现与修复、类型统计留存/去重窗口，以及指南 `last_reviewed_at` 与统一发布时间过滤的边界。它们是实施门禁，不是本轮已经完成的验证。

本轮文档不提供可直接复制执行的发布、清库、迁移或伪 Outbox 示例。实现与测试按配套分步计划推进；代码落地、Git 提交、生产部署分别遵守用户授权。生产完整验证后再将设计与过程计划合并为精简实现方案，当前不得提前标注“已实现”。
