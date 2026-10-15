---
name: wagtailblog-dev-workflow
description: Wagtail 8.0 与 Django 5.2.8 项目的按风险分级开发、双存储测试、浏览器验证和生产发布工作流。用于 wagtailblog2 的相关工程任务，不替代用户授权。
---

# wagtailblog2 专属工程工作流

本技能只提供项目特有的决策依据。先判断任务风险，再加载必要细节；小型文案、文档或配置修正不必强行创建设计文档或派发代理。

## 1. 任务路由

1. **小型任务**：主代理直接修改，做最小定向验证。
2. **中型任务**：先写短计划；仅对有清晰文件边界的独立工作并行派发。
3. **高风险任务**：涉及 MySQL/MongoDB 双存储、发布、复制、别名、删除、revision、`mongo_content_id`、Outbox、并发锁、数据修复或生产时，先确认契约、回滚边界和测试矩阵，再安排独立复核。
4. **生产任务**：必须有用户明确授权。应用补丁不默认重启基础设施或无关服务。

## 2. 不可破坏的项目契约

- BlogPage 正文、StreamField `body`、MongoDB 正文、草稿快照、revision pointer 和 `mongo_content_id` 是受保护资产。
- Markdown 以原始字符串保存；`markdown_block` 存储 key 稳定，HTML 只在模板渲染时生成。
- 发布流程核对 `BlogPublicationService` 及 `external_content` 的实际入口，确认 MySQL Page/Revision 与 Mongo 文档版本、哈希和指针的一致性。
- 复制与别名语义以当前实现和测试为准：不可变发布 manifest 可能有意复用源别名，不能套用“所有复制都必须立即产生独立 Mongo 副本”的旧规则。
- 删除必须先记录删除意图、校验引用和安全围栏，再按当前实现异步清理；禁止直接用裸 `delete()` 绕过生命周期。
- Outbox 事件与业务事务同批写入 MySQL，由异步消费者投影 Elasticsearch；不要在视图中假定同步 ES 已完成。
- 后台管理组件按现有代码演进。仓库目前同时存在 `wagtail-modeladmin`/ModelAdmin 与原生 SnippetViewSet；除非需求明确迁移，不卸载依赖、不批量改造。

## 3. 测试与验证

### 3.1 选择原则

- `manage.py check`、`compileall`、CSS token 检查属于静态/系统检查，不等于业务测试。
- Mock 单元测试只证明替身边界；涉及真实 MySQL/Mongo 的集成结论必须标明隔离环境和服务状态。
- 模板、后台和前端交互需要渲染或浏览器证据，不能只凭 Python 测试推断。
- 不要假设存在 `wagtailblog3/settings/test.py`，也不要假设不存在的 Django 测试标签；先核对目录和测试类。

### 3.2 常用命令

在 WSL2 的 `/root/anaconda3/envs/wagtailblog-test` 环境执行：

```bash
python manage.py check
python -m compileall -q wagtailblog3
python tools/check_css_tokens.py
git diff --check
```

按变更选择定向测试，例如：

```bash
# external_content 的单元与契约测试
python manage.py test \
  wagtailblog3.apps.external_content.tests.test_registry \
  wagtailblog3.apps.external_content.tests.test_copy \
  wagtailblog3.apps.external_content.tests.test_publication_state \
  --keepdb

# 博客发布/正文兼容性
python manage.py test \
  blog.tests.test_publication_service \
  blog.tests.test_publication_consistency \
  blog.tests.test_mongo_body_versions \
  --keepdb

# 搜索 Outbox
python manage.py test \
  search.tests.test_search_sync_delivery \
  search.tests.test_search_sync_producer \
  --keepdb
```

需要真实隔离存储时使用 `wagtailblog3.settings.external_content_test`，并先确认专用 MySQL/Mongo 服务和环境凭据已就绪：

```bash
python manage.py test \
  wagtailblog3.apps.external_content.tests \
  --settings=wagtailblog3.settings.external_content_test \
  --keepdb
```

该命令不会自动启动服务、创建未知数据库或清理数据；服务未就绪时报告阻塞原因，不绕过安全门禁。

## 4. 浏览器验证

- 无状态回归使用 Playwright，产物统一放在 `output/playwright/<task-name>/`，不得提交。
- 需要真实登录态、Wagtail 后台权限或前后台联动时使用 `browser-skill`；只验证必要的页面结果，严禁读取或输出 Cookie、Token、密码。
- 至少覆盖与变更相关的桌面视口（1280×800）和移动视口（375×667）；检查控制台错误、关键资源 404、表单提交结果。
- 浏览器不可用时，说明限制并改用模板渲染、Django 测试或静态检查，不伪造实机结果。

## 5. 发布边界

生产路径为 `/home/source/Django/wagtail/wagtailblog3`，环境为 `/root/anaconda3/envs/wagtailblog`。未经用户明确授权，不执行 SSH 写操作、`collectstatic`、服务重启、迁移或回滚。

按补丁影响范围处理服务：

- Web/Django 代码：通常只涉及 `wagtailblog3.service`。
- Maintenance 任务：再考虑 `wagtailblog3-celery-maintenance.service`。
- Beat 调度：再考虑 `wagtailblog3-celery-beat.service`。
- 日志采集：只有补丁确实影响 Filebeat 配置或日志链路时才考虑 `wagtailblog3-filebeat.service`。

不要因为“完整发布流程”默认重启 MySQL、MongoDB、Redis、Elasticsearch 或 Filebeat。涉及生产时由 Sol/审查角色制定原子步骤和回滚点，执行角色逐步回传退出码；用户授权只覆盖明确写出的范围。

## 6. 油猴脚本

源码唯一事实源：`wagtailblog3/static/vendor/Script/downlaod_markdown.js`。修改时同步生成 `tools/downlaod_markdown.user.js`；生产发布再按授权执行 `collectstatic`，验证收集结果后重启受影响的应用服务。
