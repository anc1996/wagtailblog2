# Mongo 孤儿正文可视化审计与安全清理实施方案

> 日期：2026-09-03
> 状态：方案评审阶段（待用户核准后正式实施）
> 目标：在 Wagtail Admin 后台实现 Mongo 孤儿正文的可视化巡检、正文内容预览（Body 展开）与受控安全清理
> 模型基线：全流程及所有子代理统一采用 gemini-3.8-flash-high

---

## 1. 背景与现状证据

### 1.1 业务背景与痛点
在 BlogPage 正文与生命周期分层改造中，正文存储迁移至 MongoDB（不可变版本集合 content_body_versions、草稿/修订集合 blog_page_revision_bodies 及历史兼容集合 blog_content）。

- **历史遗留孤儿**：在早期的页面删除、失败测试或异常中断流程中，部分页面从 MySQL 删除了，但在 MongoDB 中仍残留了历史正文快照；
- **盲目删除风险**：之前的工具只有命令行只读脚本 orphan_report.py，并且因为担心误删，代码中强行禁用了 --apply。管理员面对一堆孤儿 ID，无法看到里面存的具体正文是什么内容，既不敢盲目删除，也无法确认是否属于无用数据；
- **核心诉求**：用户明确提出——在 Wagtail Admin 后台提供专用面板，能够可视化查看孤儿清单，但在清理之前，必须能够点击打开查看该文档底层 body 存的具体内容，确认无误后再执行清理。

### 1.2 现状数据证据（测试环境核验）
在测试库执行 orphan_report 得到真实基线：
- content_body_versions：169 篇
- blog_page_revision_bodies：205 篇
- blog_content：156 篇
- 扫描出的潜在孤儿候选：10 项（其中真正完全无主的死孤儿 orphan_candidate 为 1 项，历史 Revision 快照引用的 referenced_missing_page 为 9 项，正文均在 Mongo 中处于静默占用状态）。

---

## 2. 目标与非目标

### 2.1 目标
1. **建立后台可视化审计面板**：
   - 在 Wagtail Admin 的“报告 (Reports)”菜单下增加【Mongo 孤儿正文治理】（/admin/reports/mongo-orphans/）；
   - 清晰展现集合分布、孤儿类型统计、关联 Page ID、创建时间与大小。
2. **实现“先看后删”的 Body 预览抽屉/模态框（核心亮点）**：
   - 针对每一条孤儿记录，提供【查看正文】交互；
   - 异步从 MongoDB 读取对应原始数据并智能解析：
     - 提取标题线索（# 标题）与字符统计；
     - 渲染可读的 Markdown 正文预览；
     - 提供可折叠的原始 StreamField JSON 块结构；
     - 明确标记该正文被判定为孤儿的原因证据。
3. **实现多重防护的受控安全清理**：
   - 支持单条清理与批量清理；
   - **执行时并发二次校验（Fencing Token Check）**：清理动作执行前，必须再次瞬时复核该 Mongo ID 是否被现有有效页面、活动发布意图或搜索 Target 引用，一旦发现被引用立即阻断，绝对禁止误删；
   - 提供强阻断二次确认弹窗；
   - 记录详尽的操作审计日志（操作人、时间、删除了哪个集合的哪个 Mongo ID）。
4. **命令行动力对齐**：
   - 将 orphan_report.py 的底层扫描与清理逻辑重构为公共服务 MongoOrphanService，使命令行与 Web 后台复用同一套严密逻辑；
   - 解除命令行 --apply 封印，提供交互式确认与 dry-run 保护。

### 2.2 非目标
- 不删除任何被现有公开或草稿页面（BlogPage / BlogPublicationState）引用的正文；
- 不在无管理员人工确认的前提下进行后台自动定时物理删除（必须保持人工透视与显式授权）；
- 不引入外部重度前端框架，严格基于 Wagtail 8.0 原生设计组件与轻量原生 JS 实现。

---

## 3. 架构设计与技术实现方案

### 3.1 总体分层架构

```text
Wagtail Admin 界面 (/admin/reports/mongo-orphans/)
  ├── 概览卡片 (总文档数、孤儿统计、空间估算)
  ├── 候选表格 (按集合筛选、Page ID 检索、状态徽章)
  └── 交互动作：
        ├── [查看正文] ──> GET /admin/reports/mongo-orphans/preview/ (JSON/HTML 模态框)
        └── [安全清理] ──> POST /admin/reports/mongo-orphans/cleanup/ (CSRF + Superuser)
                 │
                 ▼
后端服务层: MongoOrphanService (wagtailblog3/apps/blog/services/mongo_orphan.py)
  ├── scan_orphans()                 # 内存级快照与交叉核验，输出分类
  ├── get_orphan_body_preview()      # 提取正文 Markdown、字符数、标题线索
  └── delete_orphan_document()       # Fencing 复核 + 物理删除 + 审计记录
                 │
                 ▼
底层存储与命令行:
  ├── MongoDB: content_body_versions / blog_page_revision_bodies / blog_content
  └── CLI: manage.py orphan_report [--apply]
```

### 3.2 严格的三层分类与处置规则

1. **referenced_page（活跃保护中）**：
   - 条件：关联的 page_id 在 MySQL 中真实存在，且被 BlogPublicationState 或 BlogPage 引用；
   - 处置：**绝对禁止清理**，界面显示绿色【活跃引用中】徽章，无删除按钮。
2. **referenced_missing_page（历史修订引用）**：
   - 条件：关联的页面已在 MySQL 物理删除，但历史 Wagtail Revision 表中仍有快照指针；
   - 处置：显示黄色【历史快照孤儿】徽章。管理员**必须点击查看正文**，确认该历史版本已完全无追溯价值后，经二次确认才允许清理。
3. **orphan_candidate（完全死孤儿）**：
   - 条件：页面不存在、Revision 不存在、State 不存在、删除意图也不存在；
   - 处置：显示灰色/红色【完全无主孤儿】徽章。支持在预览正文后一键安全清理。

### 3.3 Body 预览数据结构契约（JSON API）

接口：GET /admin/reports/mongo-orphans/preview/?collection=<coll>&id=<mongo_id>
响应数据包含：
- collection: 集合名
- mongo_id: 文档主键
- page_id: 关联页面编号
- body_version_id: 正文版本标识
- category & category_display: 分类标识与中文标签
- created_at: 创建时间
- char_count: 纯文字符数统计
- block_count: StreamField 块数量
- title_hint: 智能提取的文章标题线索
- markdown_content: 提取格式化后的完整 Markdown 正文
- raw_blocks_json: 原生 JSON 块结构预览
- can_delete: 是否允许执行物理清理
- reason: 判定原因与证据说明

### 3.4 物理清理的安全门禁（Fencing Token Check）

当管理员在界面或命令行点击“确认清理”时，服务端执行以下原子化防线：
1. **身份门禁**：仅限超级管理员（request.user.is_superuser）有权提交清理请求；
2. **实时并发复核（Fencing）**：
   - 重新从 MySQL 校验：该 mongo_id 或 body_version_id 是否出现在任何 BlogPublicationState 中；
   - 重新校验是否有尚未完成的 PageDeletionIntent 正在使用；
   - 若发现任何存活引用，立即抛出 ValidationError，拒绝删除；
3. **执行物理清理**：
   - 调用 Mongo 驱动执行 collection.delete_one({"_id": ObjectId(mongo_id)})；
4. **记录审计日志**：
   - 写入系统审计日志：mongo_orphan_deleted user=admin collection=... id=... timestamp=...

---

## 4. 任务拆解与子代理角色分工

按照规范，全流程统一由 gemini-3.8-flash-high 模型驱动，各专业角色分工明确如下：

| 任务包 | 负责子代理角色 | 独立文件边界 | 核心职责与交付门禁 |
| :--- | :--- | :--- | :--- |
| **P1-A 服务层抽象** | **backend (后端)** | wagtailblog3/apps/blog/services/mongo_orphan.py | 抽象孤儿扫描、Body 智能解析（提取标题/字数/Markdown）、Fencing 校验与物理删除原子操作。 |
| **P1-B 管理命令对齐** | **backend (后端)** | wagtailblog3/apps/blog/management/commands/orphan_report.py | 复用服务层，解锁受控 --apply 与交互确认机制。 |
| **P1-C 后台视图与 API** | **backend (后端)** | wagtailblog3/apps/blog/views.py, wagtailblog3/apps/blog/wagtail_hooks.py | 注册报告菜单、实现面板主视图、正文预览 JSON API、安全清理 POST API。 |
| **P1-D 前端 UI/UX 界面** | **frontend (前端)** | wagtailblog3/templates/wagtailadmin/reports/mongo_orphans.html, static/blog/css/mongo-orphans.css, static/blog/js/mongo-orphans.js | 遵循 ui-ux-pro-max 规范，打造高可读表格、正文预览抽屉模态框、删除强阻断确认弹窗。 |
| **P1-E 自动化测试套件** | **qa (测试)** | wagtailblog3/apps/blog/tests/test_mongo_orphan_cleanup.py | 编写扫描分类测试、Body 预览接口测试、并发误删阻断测试、清理真实测试。 |
| **P1-F 浏览器模拟演练** | **qa / review** | bsk 真实浏览器演练 | 驱动浏览器打开后台 -> 点击查看正文 -> 验证内容完整呈现 -> 点击清理 -> 验证下架。 |

---

## 5. 详细实施计划与步骤

1. **第一阶段：后端服务与契约实现**
   - 新建 apps/blog/services/mongo_orphan.py，实现扫描、解析与安全删除；
   - 改造 orphan_report.py 接入公共服务；
2. **第二阶段：后台视图、API 与模板组件开发**
   - 编写 wagtail_hooks.py 挂载菜单与路由；
   - 编写 mongo_orphans.html 页面及预览抽屉模态框；
   - 实现轻量原生 JS 异步拉取正文预览与弹窗渲染；
3. **第三阶段：自动化测试与门禁验证**
   - 编写并在 WSL2 测试环境运行 test_mongo_orphan_cleanup.py；
   - 运行 manage.py check、compileall 与 git diff --check；
4. **第四阶段：BrowserSkill 真实用户演练**
   - 使用 bsk 在测试环境中模拟管理员打开后台面板、点击预览正文、执行单条清理并验证数据消失。

---

## 6. 数据、服务影响与回滚方案

### 6.1 数据与服务影响
- **数据影响**：仅在管理员显式确认后，物理删除被判定为完全孤儿（orphan_candidate）的 Mongo 文档；绝不触碰任何正在引用的活跃页面正文；
- **服务影响**：新增的管理报表与服务不影响线上网站正常运行，无需重启数据库或 Elasticsearch。

### 6.2 回滚方案
- 本次改动为纯新增管理视图、服务与测试文件，若发生任何异常，可一键撤销新增文件并重启 uWSGI 回退。

---

## 7. 验收标准与交付物

1. **自动化测试**：python manage.py test blog.tests.test_mongo_orphan_cleanup --keepdb 全部通过；
2. **界面交互验收**：
   - 后台“报告”菜单出现【Mongo 孤儿正文治理】；
   - 列表正确展现孤儿条目与分类；
   - 点击“查看正文”能完整清晰显示 Markdown 正文字符、标题与块结构；
   - 点击“安全清理”能弹出二次阻断确认并成功清理；
3. **代码门禁**：check、compileall、git diff --check 0 错误；
4. **文档归档**：在实施记录中追加真实的测试数据与浏览器截图/DOM 证据。
---

## 8. 第一阶段实施记录

- **实施日期**：2026-09-03
- **执行角色**：主代理与子代理（统一采用 `gemini-3.8-flash-high` 模型）
- **实施状态**：第一阶段代码开发、静态收集、单元测试、端到端接口验证与测试服务栈重启全部完成（待用户在测试环境直观验收确认）

### 8.1 实际修改与新增文件清单
| 文件路径 | 变更类型 | 说明 |
| :--- | :--- | :--- |
| `wagtailblog3/apps/blog/services/mongo_orphan.py` | 新增 | 核心服务层：实现 MySQL 活跃白名单收集、正文文档分类、正文富文本与 Markdown 智能反解析提取（标题/字数/块结构）、强阻断 Fencing Token 瞬时并发校验与原子删除。 |
| `wagtailblog3/apps/blog/management/commands/orphan_report.py` | 修改 | 命令行治理命令升级：底层全面复用 `MongoOrphanService`，保留默认只读输出，解除 `--apply` 封印并配合 `--yes` 强约束，实现安全清理。 |
| `wagtailblog3/apps/blog/views_mongo_orphans.py` | 新增 | 治理后台视图与 API：报表面板主视图 `mongo_orphans_report_view`、正文反解析预览 API `mongo_orphan_preview_api`、具备 Fencing 熔断的清理 API `mongo_orphan_cleanup_api`，严格限制超管访问。 |
| `wagtailblog3/apps/blog/wagtail_hooks.py` | 修改 | 注册超管专用报告菜单项【Mongo 孤儿治理】（路由 `/admin/reports/mongo-orphans/`）。 |
| `wagtailblog3/templates/wagtailadmin/reports/mongo_orphans.html` | 新增 | 治理控制台模板：统计卡片、分类徽章、候选表格、正文深度反解析模态框（支持先看）、强阻断二次确认弹窗（后删）及 Toast 提示。 |
| `wagtailblog3/static/blog/css/mongo-orphans.css` | 新增 | 遵循 `ui-ux-pro-max` 设计体系的响应式样式，适配 Wagtail 8.0 设计 Token。 |
| `wagtailblog3/static/blog/js/mongo-orphans.js` | 新增 | 异步预览拉取、标题/正文/块元信息渲染、二次阻断弹窗与 CSRF 安全删除交互。 |
| `wagtailblog3/apps/blog/tests/test_mongo_orphan_cleanup.py` | 新增 | 单元测试套件（覆盖分类、预览解析、Fencing 熔断、原子清理、视图鉴权、API 及命令行对齐）。 |

### 8.2 自动化测试与质量门禁证据
1. **定向单元测试套件**：
   ```bash
   python manage.py test blog.tests.test_mongo_orphan_cleanup blog.tests.test_orphan_report --keepdb
   ```
   - 结果：**21 项测试全部通过（Ran 21 tests in 4.797s, OK）**。
2. **页面删除与生命周期回归**：
   ```bash
   python manage.py test blog.tests.test_page_deletion blog.tests.test_mongo_cleanup_intent --keepdb
   ```
   - 结果：**18 项测试全部通过（Ran 18 tests, OK）**。
3. **代码静态检查与语法编译**：
   - `python manage.py check`：**System check identified no issues (0 silenced)**；
   - `python -m compileall ...`：全部新增与修改文件编译 0 报错；
   - `git diff --check`：0 空白/冲突问题。
4. **静态资源收集**：
   - 执行 `python manage.py collectstatic --noinput`，成功将新增 CSS/JS 写入 `staticfiles_collected` 并完成 `ManifestStaticFilesStorage` 哈希索引化。

### 8.3 测试环境服务重启与实测验证
- 严格遵循服务规范，在 WSL2 测试环境（`192.168.20.5`）通过 `tools/start_test_stack.sh` 重新启动测试服务栈：
  - **Django Web (8080)**：PID `149028`，监听 `0.0.0.0:8080`，HTTP 响应正常；
  - **Celery Worker**：PID `149029`，队列 `markdown-test-maintenance`；
  - **Celery Beat**：PID `149030`，调度文件写入 `output/`。
- **接口联通核验**：
  - 超管会话访问 `/admin/reports/mongo-orphans/`：返回 **HTTP 200**；
  - 预览 API 抓取历史正文版本 `6a9485e1d9c82da1ac2324c8`：返回 **HTTP 200**，成功解析出标题线索“验收正文首次版本”、字数 38、块类型 `rich_text` 及判定原因证据。

### 8.4 安全与发布门禁声明
- 本次实施全过程**仅在测试工作区与 WSL2 测试环境**（`192.168.20.5`）进行；
- **未**在生产服务器（`192.168.20.2`）执行任何操作；
- **未**执行 `git commit` 或 `git push`；
- 代码与服务已准备就绪，等待用户在测试后台（`http://192.168.20.5:8080/admin/reports/mongo-orphans/`）进行交互验收。
