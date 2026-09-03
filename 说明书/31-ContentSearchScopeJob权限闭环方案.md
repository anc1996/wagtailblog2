# ContentSearchScopeJob 页面权限搜索闭环端到端验证方案

> 日期：2026-09-03
> 状态：方案草案（待用户确认后方可实施代码）
> 模型基线：全协作链路与子代理统一采用 `gemini-3.8-flash-high`

---

## 1. 背景与现状证据

### 1.1 业务背景与隐患
在 Wagtail 博客系统中，页面以树状层级组织（如 `BlogIndexPage` -> `BlogPage`）。管理员可以随时在后台为某个父页面设置访问限制（`PageViewRestriction`，如私密密码保护、仅登录可见、仅特定用户组可见）。

- **MySQL 视角**：Wagtail 原生遵循树状继承规则，给父页面加锁后，子页面在 SQL 查询 `Page.objects.live().public()` 时自动被排除。
- **Elasticsearch 视角**：ES v005 保存的是由 Outbox 异步投递的已发布文档。当父页面加锁时，子页面在 MySQL 中并未单独触发发布/下线动作；若无异步巡检机制将 ES 中的子页面投影打上墓碑（`searchable: false`），则外部用户仍可通过前台关键词检索或搜索补全，窥见受保护文章的标题、摘要与关键词片段。

### 1.2 现有代码现状与能力断点
通过对代码库及 Wagtail 8.0 源码的摸排核验：
1. **已具备的能力**：
   - `wagtailblog3/apps/search/models.py`：定义了 `ContentSearchScopeJob` 状态机（`PENDING` -> `PROCESSING` -> `SUCCEEDED` / `RETRY` / `DEAD`），支持 `checkpoint_page_id` 断点与 `rescan_requested` 重扫标记；
   - `wagtailblog3/apps/search/signals.py`：已监听 `PageViewRestriction` 的 `post_save` 和 `pre_delete`，调用 `ContentSearchOutboxService.request_scope_recalculation(instance.page_id)`；
   - `wagtailblog3/apps/search/services/scope.py`：实现了租约管理、分批扫描子树（`path__startswith=root.path`）、对齐 `_reconcile_page` 意图；
   - `wagtailblog3/settings/database.py`：已在 `CELERY_BEAT_SCHEDULE` 中配置 `dispatch-pending-content-search-scope-jobs` 轮询任务。
2. **存在的断点与盲区（本方案重点解决）**：
   - **缺少真实的端到端全链路验收**：现有测试 `test_search_scope.py` 仅 mock 了调度器并测试了 `ContentSearchState` 的内存标记，**未打通 Wagtail 信号 -> ScopeJob -> Outbox -> Delivery -> ES 物理索引的完整链路**；
   - **页面跨层级移动漏网（Wagtail 8.0 边缘场景）**：当一篇公开文章通过后台或代码被**移动（Move）**到受限制的父目录下，或者从受限制目录移动到公开目录下时，仅触发 `wagtail.signals.post_page_move`，并不会触发 `PageViewRestriction` 的信号，导致移动操作产生权限搜索漏洞；
   - **缺少多层级继承与大批量分页验证**：尚未在两级以上父子树、跨越 `batch_size` 分页界限的真实场景下验证检查点推进；
   - **缺少真实 ES 搜索验证**：未验证页面被锁后在 ES 中是否真正返回 0 条结果、解锁后是否重新恢复高亮检索。

---

## 2. 目标与非目标

### 2.1 目标
1. **摸透并对接 Wagtail 8.0 权限机制**：
   - 深入结合 Wagtail 8.0 `PageViewRestriction` 增删改信号；
   - 补全 `post_page_move`（页面树移动）对搜索范围的重算支持，消除移动页面带来的权限漏洞。
2. **构建端到端闭环测试套件**：
   - 编写完整的自动化测试 `wagtailblog3/apps/search/tests/test_search_scope_e2e.py`；
   - 覆盖四种典型业务场景：
     - 场景 A（父页面加锁）：父级设置密码限制 -> 子页面与孙子页面自动下发 tombstone -> ES 中搜不出该文章；
     - 场景 B（父页面解锁）：父级解除密码限制 -> 子树自动生成 UPSERT 事件并投递 -> ES 中重新被检索出来；
     - 场景 C（页面移动跨越权限边界）：公开文章移入受保护父页面 -> 触发墓碑；私密文章移出到公开父页面 -> 恢复公开搜索；
     - 场景 D（断点续跑与大批量分页）：在小批次设置下验证多批次递进、Worker 租约回收与幂等重试。
3. **在 WSL2 测试环境（192.168.20.5）真实完成测试跑通并提供无可挑剔的证据**。

### 2.2 非目标
- 不重写 Wagtail 原生权限模型，继续复用 `PageViewRestriction` 与 `PageQuerySet.public()` 契约；
- 不在信号处理中做同步深层树扫描（坚决遵守不可阻塞 Web 请求的原则，所有子树扫描均由 Celery maintenance 异步执行）；
- 不修改生产环境配置或生产数据（本轮严格在 WSL2 测试环境中实施与验证）。

---

## 3. 模型与推理强度建议

根据项目规范，本任务的各角色与模型分配如下（全会话统一采用 `gemini-3.8-flash-high`）：

| 环节 / 角色 | 模型标识 | 推理强度 | 职责范围与验证门禁 |
| :--- | :--- | :--- | :--- |
| **方案设计与架构分析** (`arch`) | `gemini-3.8-flash-high` | High | 梳理 Wagtail 8.0 树状路径、信号与 ES 投影生命周期，设计完备测试用例。 |
| **测试与边界实现** (`backend`/`qa`) | `gemini-3.8-flash-high` | High | 编写端到端测试用例，补齐 `post_page_move` 适配，跑通所有测试。 |
| **审查与门禁验证** (`review`) | `gemini-3.8-flash-high` | High | 执行 `manage.py check`、`compileall`、`git diff --check`，输出证据。 |

---

## 4. Wagtail 8.0 源码与架构深度对接分析

### 4.1 Wagtail 树状路径与继承原理
- Wagtail 的页面继承建立在 `django-treebeard` 的 Materialized Path 上（字段 `path`，如 `000100010002`）。
- 任何子孙页面的 `path` 必定以父节点的 `path` 为前缀（`path__startswith=parent.path`）。
- Wagtail 8.0 中判断页面是否公开的实现位于 `PageQuerySet.public()`：
  ```python
  def public(self):
      return self.exclude(self.private_q())
  ```
  其中 `private_q()` 遍历全表所有有效 `PageViewRestriction`，并通过 `descendant_of_q(restriction.page, inclusive=True)` 把该限制页及其所有后代全部标记为私密。

### 4.2 信号触发源与时序
1. **权限新增/更新**：后台提交权限表单 -> `PageViewRestriction.save()` -> 触发 `post_save`。
2. **权限删除（恢复公开）**：后台选择 None -> `PageViewRestriction.delete()` -> 触发 `pre_delete`。
   - 注意：在 `pre_delete` 信号触发时，数据库中该限制记录仍存在。我们在信号处理中仅创建 `ContentSearchScopeJob`，真正的对齐计算是在随后的 Celery Worker 事务外异步执行（或事务提交后的 `on_commit` 之后）。此时事务已完成，限制记录已物理删除，Worker 读取 `Page.objects.live().public()` 能够精准判定该子树已恢复公开！
3. **页面移动（Move）**：
   - 当调用 `page.move(target, pos)` 时，Wagtail 8.0 会发出 `wagtail.signals.post_page_move(sender, parent_page_before, parent_page_after, instance, ...)` 信号。
   - 若 `parent_page_before` 与 `parent_page_after` 的公开状态不一致，必须对被移动的页面节点生成 `ContentSearchScopeJob`。

---

## 5. 详细实施计划与代码变动范围

### 5.1 预计修改的文件
1. **`wagtailblog3/apps/search/signals.py`**：
   - 引入 `wagtail.signals.post_page_move` 监听器；
   - 识别页面跨越权限父目录移动的场景，触发 `request_scope_recalculation(instance.pk)`。
2. **`wagtailblog3/apps/search/tests/test_search_scope_e2e.py`**（新增完整端到端测试）：
   - 继承 `BlogLifecycleFixtureMixin`；
   - 建立真实的父子结构：`BlogIndexPage` -> `ParentBlogPage` -> `ChildBlogPage` -> `GrandchildBlogPage`；
   - 验证：
     1. 加锁：父页面设置密码 -> 触发任务 -> Worker 消费 -> Outbox 产生 TOMBSTONE -> Delivery 投递完成 -> ES 文档 `searchable=False`；
     2. 解锁：父页面移除密码 -> 触发任务 -> Worker 消费 -> Outbox 产生 UPSERT -> 抓取 Mongo 最新版本 -> ES 文档重新 `searchable=True`；
     3. 移动：子页面从私密父目录移入公开目录，自动触发恢复为公开；
     4. 分页断点：设置 `CONTENT_SEARCH_SCOPE_BATCH_SIZE=1`，验证两批次检查点顺利推进，任务最终为 `SUCCEEDED`。

### 5.2 绝不修改的文件与边界约束
- 绝不修改生产环境配置 `.env.production`；
- 绝不修改正文存储格式、MongoDB 集合名或存储 key；
- 绝不修改 `ContentSearchTarget` 路由逻辑；
- 严格遵循中文注释规范与员工交付门禁。

---

## 6. 数据、服务影响与回滚方案

### 6.1 数据与服务影响
- **数据安全**：测试用例均在 Django 测试数据库隔离运行（或使用 `--keepdb` 隔离运行），不修改测试环境真实 MySQL `wagtailsoftblog_test` 和 MongoDB 中的历史业务正文。
- **服务影响**：无需重启任何生产服务；测试过程中测试环境 Web 与 Worker 维持运行。

### 6.2 回滚方案
- 如方案未获通过或测试未达预期，直接删除新增的测试文件及撤回 `signals.py` 的局部改动，系统即刻回到当前干净 Git 状态（`HEAD`）。

---

## 7. 验收标准与测试门禁

1. **功能验收**：
   - `python manage.py test search.tests.test_search_scope_e2e --keepdb` 全部通过；
   - 联同既有测试 `python manage.py test search.tests.test_search_scope search.tests.test_search_wp5 --keepdb` 全部通过；
2. **工程门禁**：
   - `python manage.py check` 无任何新增错误；
   - `python -m compileall -q wagtailblog3` 无语法错误；
   - `git diff --check` 干净无格式瑕疵。

---

## 8. 实施记录与测试证据（2026-09-03）

### 8.1 实施状态
- **状态**：已完成实现与全套端到端自动化测试验证，未向 Git 提交，未推送到生产环境。
- **模型/推理强度实际使用**：全流程统一采用 `gemini-3.8-flash-high`（架构分析 High、实现 High、测试审查 High）。

### 8.2 实际修改文件
1. `wagtailblog3/apps/search/signals.py`：
   - 新增导入 `wagtail.signals.post_page_move`；
   - 增加 `request_scope_recalculation_on_page_move` 信号监听器，解决页面树跨权限节点移动时的搜索泄密隐患。
2. `wagtailblog3/apps/search/tests/test_search_scope_e2e.py`（新增）：
   - 包含 5 个完整的端到端集成用例：
     1. `test_parent_restriction_adds_tombstone_to_all_descendants`：三级继承加锁墓碑投递；
     2. `test_parent_restriction_removal_restores_all_descendants_to_upsert`：解锁恢复全量 UPSERT 与正文哈希；
     3. `test_moving_public_page_under_restricted_parent_tombstones_page`：移动进受限父目录打墓碑；
     4. `test_moving_restricted_page_under_public_parent_restores_page`：移出受限父目录恢复公开搜索；
     5. `test_multilevel_subtree_batching_and_checkpoint_advancement`：小批次检查点稳健递进至 SUCCEEDED。
3. `说明书/31-ContentSearchScopeJob权限闭环方案.md`：
   - 记录方案、Wagtail 8.0 源码机理与实施测试记录。

### 8.3 精确测试证据
在 WSL2 (`192.168.20.5`) 测试环境下执行：
```bash
# 1. 专项端到端测试（5 项通过）
python manage.py test search.tests.test_search_scope_e2e --keepdb
# 输出：Ran 5 tests in 10.108s. OK.

# 2. 关联搜索套件联合回归（22 项通过）
python manage.py test search.tests.test_search_scope search.tests.test_search_scope_e2e search.tests.test_search_wp5 --keepdb
# 输出：Ran 22 tests in 31.591s. OK.

# 3. 系统静态检查与代码编译
python manage.py check
python -m compileall -q wagtailblog3
git diff --check
# 全部通过，0 错误，0 格式告警。
```

### 8.4 数据与服务影响
- 仅在 Django 隔离测试数据库中运行测试，未修改测试环境与生产环境真实数据库（MySQL、MongoDB、Elasticsearch）；
- 生产服务未做任何重启或推送操作。

### 8.5 回滚点与残余风险
- **回滚点**：撤销 `search/signals.py` 改动并删除 `test_search_scope_e2e.py` 即可完全恢复；
- **残余风险**：当前改动已在 WSL2 测试环境完全通过，等待用户亲自核查满意并获得明确同意授权后，方可在 WSL2 执行 Git 提交及后续发布。
