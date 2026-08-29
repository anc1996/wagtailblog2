# Wagtail 后台页面搜索链路分析

## 背景与现状证据

- 用户提供的测试地址为 `http://192.168.20.5:8080/admin/pages/3/?q=文件处理`，它属于 Wagtail 管理后台的页面浏览器，不是站点前台 `/zh-hans/search/` 路由。
- 项目将 `admin/` 直接挂载至 `wagtail.admin.urls`，前台搜索单独由 `search.urls` 挂载至 `/<language>/search/`。
- WSL2 `wagtailblog-test` 环境已实测安装 Wagtail 8.0；默认 Wagtail 搜索后端实例为 `modelsearch.backends.elasticsearch8.Elasticsearch8SearchBackend`，配置后端为 `wagtail.search.backends.elasticsearch8`。
- 默认 Wagtail 页面索引前缀由环境配置提供，测试环境实际诊断输出为 `wagtailblog-test`；Elasticsearch 节点为测试配置指定的地址。管理后台自动补全使用 Modelsearch 的 `_edgengrams` 字段，而不是项目为普通全文索引配置的 IK 分词器。本文不记录环境文件中的凭据。

## 目标与非目标

目标：厘清该后台 `q` 参数实际使用的搜索实现、它与前台搜索的关系，以及“文件处理”如何参与匹配。

非目标：不修改页面、正文、MySQL、MongoDB、Redis 或 Elasticsearch；不重建/切换/清理索引；不改动服务、环境文件或生产环境。

## 设计与实施步骤

1. 检查项目 URL、搜索配置、前台搜索服务和测试环境实际安装版本。
2. 读取 Wagtail 管理后台页面浏览器的已安装实现，确认 `admin/pages/<id>/?q=` 的查询边界与字段。
3. 使用测试地址作只读浏览器访问，检查身份验证状态、页面呈现、控制台和请求；调试产物仅写入 `output/playwright/`。
4. 形成后台筛选、Wagtail 页面索引和项目独立 Blog 内容索引三者的分层结论。

## 修改与不修改的文件

- 修改：仅本文档，用于记录本次调研方案与实施证据。
- 不修改：`wagtailblog3` 运行时代码、迁移、测试、环境文件、`systemctl.md`、服务 unit 和数据。

## 数据与服务影响

本次所有 Django shell、源代码和浏览器检查均为读取操作。管理后台浏览器访问不会保存内容；不触发索引写入、Celery 投递、服务重启或生产操作。`systemctl.md` 无需更新。

## 测试与验收

- WSL2：确认 Python 环境、Django/Wagtail 版本、默认搜索后端类和搜索配置。
- 源码：确认后台和前台 URL 路由，追踪页面搜索与 Blog 内容搜索入口。
- 浏览器：访问用户提供的测试 URL；确认是否已认证，以及若可进入则验证结果、控制台和网络请求。
- 文档：`git diff --check`。

## 回滚点与残余风险

回滚点是删除本文档；它不影响运行时行为。残余风险是后台必须使用具有页面访问权限的已认证会话才能观察实际命中项；若会话未登录，只能基于 Wagtail 源码与配置解释流程。

## 模型/推理强度建议

- 事实收集：Luna，低至中推理，适合环境与调用点只读核对；门禁是实际代码和运行时输出一致。
- 常规实现：Terra，中推理；本任务无运行时代码实现。
- 升级条件：涉及索引写入、生产环境、内容数据、权限边界异常或相互矛盾的运行证据时，升级 Sol 高推理，并先取得相应授权。
- 实际使用：当前会话以实际可用模型完成只读分析；未调用外部模型，也未发送源码、凭据或内容数据。

## 实施记录

### 2026-08-27：只读调研完成

- 状态：完成，未修改运行时代码。
- 实际修改文件：新增本文档。
- 实际链路：Wagtail 8.0 将 `/admin/pages/<parent_page_id>/` 绑定到 `ExplorableIndexView`。非空 `q` 先将范围限定为页面 3 的后代和当前员工的可浏览页面，再通过 `PageQuerySet.autocomplete()` 查询；项目没有注册 `construct_explorer_page_queryset` hook 改写该范围。`Page` 仅为 `title` 声明 `AutocompleteField`，因此不匹配 BlogPage 简介、StreamField/MongoDB 正文或独立内容索引的 `body_text`。该自动补全由 `_edgengrams` 执行 `standard tokenizer + asciifolding + lowercase + edge-ngram`，按标题前缀候选匹配，并非 IK 中文全文检索。
- 实际后端：测试 Conda 中 Django 5.2.8、Wagtail 8.0、Python Elasticsearch 客户端 8.19.3；默认后端实例为 `modelsearch.backends.elasticsearch8.Elasticsearch8SearchBackend`，配置为 Wagtail Elasticsearch 8 后端。项目配置的 IK 分词器用于普通 Wagtail 全文搜索字段，不参与本后台的 `_edgengrams` 自动补全；两者均与前台独立 Blog 内容索引是不同读取路径。
- 精确只读验证：在 WSL2 `wagtailblog-test` 执行 `Page.objects.descendant_of(Page.objects.get(pk=3)).autocomplete("文件处理")`。Elasticsearch 候选总数为 `26`，但其前 20 项回查 MySQL 后只返回 `5` 个页面对象。该直接核验没有叠加某个后台用户的权限过滤，未输出标题、正文或其他受保护内容。
- HTTP 验证：未登录请求返回 `302` 到 `/admin/login/`，表明测试站点正常要求认证。Playwright CLI 的 Chromium 以 root 启动时被系统 sandbox 拒绝，未建立浏览器会话、未发出页面请求，也没有调试产物；因此未能以已认证浏览器观察 26 条候选的实际展示和网络面板。
- 检查：`git diff --check` 通过；工作树仅有本文档未跟踪变更。
- 数据/服务影响：无。未运行迁移、索引操作、Celery 投递、服务操作或生产操作；`systemctl.md` 无需更新；未提交 Git。
- 回滚点：删除本文档。
- 残余风险：26 个 ES 候选仅有 5 个可回查对象，说明 Page 索引可能陈旧或与数据库范围不一致；在没有索引诊断、备份和明确写入授权前不得重建或清理索引。不同后台用户权限下的可见候选数，仍需在已认证且可启动浏览器的会话中单独检查。
