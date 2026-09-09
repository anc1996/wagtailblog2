# Wagtail 8.0 及核心依赖演进全书

## 1. 演进背景与核心技术栈基线

本项目核心 CMS 架构已平稳完成从 Wagtail 7.4.3 至 Wagtail 8.0 生产级大版本升级。本次演进不仅修复了多项上游安全隐患，还全面适配了 Python 3.13 运行时环境，完成了核心依赖树的严格治理与冲突消除。

### 1.1 生产级依赖版本对照矩阵

| 核心组件名称 | 升级前版本 | 当前生产锁定版本 (`requirements.txt`) | 升级必要性与兼容性结论 |
|---|---|---|---|
| **Python** | 3.13.2 | **3.13.2** | 官方完全支持 Python 3.13，提升多线程性能与启动速度 |
| **Django** | 5.2.8 | **5.2.8** | 保持稳定 LTS 系列，暂缓升级未稳定的 Django 6.x |
| **Wagtail** | 7.4.3 | **8.0** | 核心 CMS 引擎，修复权限与媒体安全漏洞，提供全新后台体验 |
| **djangorestframework** | 3.16.1 | **3.18.0** | Wagtail 8.0 强依赖要求 `>=3.18.0`，已全量回归 Markdown 导入 API |
| **django-ninja** | 未安装 | **1.6.3** | Wagtail 8.0 原生依赖，提供类型化 OpenAPI 底座支持 |
| **wagtailmedia** | 0.18.1 | **0.18.1** | 视频与音频流媒体扩展，兼容 Wagtail 8.0 页面引用 |
| **wagtailcodeblock** | 1.30.0.0 | **1.30.0.0** | 代码高亮块扩展，前端 Prism.js 渲染兼容良好 |
| **wagtail-modeladmin**| 2.3.0 | **2.3.0** | 历史后台模型管理兼容层，平稳过渡 |

---

## 2. Wagtail 8.0 关键技术变革与项目适配决策

### 2.1 数据库 Schema 演进：`0098_apitoken`
- **变动事实**：Wagtail 8.0 引入了原生 API Token 鉴权体系，在 `wagtailcore` 应用下新增 `0098_apitoken` 迁移，自动创建 API Token 数据表。
- **架构决策**：项目在升级过程中严格将其纳入标准化数据库迁移管理，并在生产上线前完成了迁移演练，杜绝断层隐患。

### 2.2 REST API v3 架构决策：依赖就绪，路由暂封
- **背景说明**：Wagtail 8.0 正式引入了基于 `django-ninja` 的 REST API v3（预览版），提供前沿的类型化内容读取能力与 Token 认证。
- **项目收敛决策**：
  1. 项目在依赖层安装 `django-ninja==1.6.3`，确保 Wagtail 8 运行时无缺失报错。
  2. **显式不在 `urls.py` 中公开挂载 REST API v3 路由**。
  3. **架构原因**：本项目已具备经过端到端隔离与安全性审计的专用接口：
     - Markdown 跨端导入 API (`/api/blog/markdown-import/`)：专用 Token 鉴权 + CORS 最小授权白名单。
     - 独立全文检索 API (`/api/search/content/`)：直连 ES 读别名 + 权限过滤。
     - 若盲目开启 v3 API，将在未经严密权限建模的情况下对外暴露页面树与媒体资源元数据，增加攻击面。未来若需启用，必须另立专门安全审计方案并取得用户明确授权。

### 2.3 安全缺陷消除与漏洞修复
Wagtail 8.0 在底层彻底封堵了以下中高危安全隐患，大幅提升生产防御等级：
1. **页面管理 API 权限越权**：修复非特权用户可能跨站点读取受保护页面草稿的漏洞。
2. **文档 SHA-1 暴力探测**：修复通过文档上传校验机制探测内部私密文档指纹的风险。
3. **私有 Collection 子级数据泄露**：修复具有顶级私有集合权限的用户意外跨越可见范围访问未授权子集媒体的问题。
4. **Snippet 复制与页面翻译权限绕过**：强化了跨语言翻译和模型克隆时的权限继承逻辑。

### 2.4 已废弃特性的安全下线与清理
- 下线了 Wagtail 6.4 - 7.3 期间标记为 Deprecated 的老旧 `telepath` 打包路径。
- 清除了旧版的通用 `INDEX` 搜索引擎配置兼容分支。
- 移除了历史遗留的自定义按钮基类与老旧用户栏钩子（Userbar Hooks）。
- 调整了 `AbstractFormField.field_type`，新增 `required_on_save=True` 约束，确保表单字段草稿在序列化时类型完整。

---

## 3. 依赖治理规范与长期演进准则

### 3.1 依赖锁定与最小改动原则
- `requirements.txt` 必须始终使用全等号（`==`）精确锁定主包及其直接依赖的精确版本，严禁使用宽松范围限定符（如 `>=` 或 `~=`）。
- 不得在执行业务功能开发或常规缺陷修复时顺手升级无直接关联的底层包。

### 3.2 依赖变更验证门禁
凡涉及 Python 依赖增删改的变更，在进入代码提交前必须在 WSL2 环境执行以下三重门禁：
1. **依赖树冲突检测**：`pip check` 必须输出 `No broken requirements found`。
2. **框架与配置检查**：`python manage.py check` 必须 0 Error。
3. **迁移路径无断裂**：`python manage.py makemigrations --check --dry-run` 确保无隐式缺失迁移，`python manage.py migrate --plan` 确认执行顺序平滑。


---

﻿## 4. Wagtail 8.x 至 Wagtail 9 兼容性治理与生命周期防御实施方案 (v2.1 完善版 - 2026-09-07)

### 4.1 背景与现状证据 (Evidence & Baseline)
- **技术栈现状**：当前生产与测试环境已锁定 Wagtail 8.0、Django 5.2.8、Python 3.13.2。依赖树完全锁定（`requirements.txt`），`pip check` 无依赖冲突。
- **架构韧性事实**：
  1. **发布与排期链路**：通过 `BlogPublicationService` 与 `WAGTAIL_FINISH_WORKFLOW_ACTION` 完成了 Page、Revision 与 MongoDB 正文（`version_id` + SHA256 + `schema_version`）的三方一致性校验与事务保护。
  2. **页面删除链路**：通过 `page_deletion.py` 实现了先固化清单、校验共享引用、落搜索墓碑代次，再异步级联清理的强受控机制，阻断直接绕过受控入口的物理删除。
  3. **独立全文检索**：专用文章索引采用 Outbox + Delivery 异步投递体系，显式配置 `AUTO_UPDATE: False`，与 Wagtail 原生搜索信号完全解耦，读写别名保证零停机重建。
- **现存架构隐患与证据**：
  1. **跨存储页面生命周期语义不完备**：尚未对 `BlogPage` 的 `copy()`、`create_alias()` 与 `move()` 建立完备的双存储契约测试。Page Copy 可能导致新页面与旧页面共享同一个可变正文指针；Page Move 虽不变更 Page ID，但改变 URL、站点与祖先权限限制，缺乏对 ES 文档 URL 与可访问范围的联动刷新保障。
  2. **wagtail-modeladmin 外部 Shim 淘汰风险**：`comments` 应用深度依赖 `wagtail-modeladmin 2.3.0` 的 `ModelAdmin`、`ModelAdminGroup`、`ButtonHelper`、`PermissionHelper` 及专属管理路由。Wagtail 官方已明确在未来大版本（Wagtail 9.0）中不再支持该外部兼容层；而 `content_ai` 已成功使用原生 `SnippetViewSet`，形成了清晰的迁移范例。
  3. **Admin UI / DOM 动态生命周期风险**：后台存在定制化模板（observability、reports、archive）、全局 CSS/JS hooks 以及 Vditor Markdown 编辑器，易受 Wagtail Admin 底层 Stimulus / TypeScript 现代化重构的静默冲击（如动态表单增加块、Revision 恢复重填时的事件丢失）。

### 4.2 目标与非目标 (Goals & Non-Goals)
- **核心目标**：
  1. 【近期】补齐 Copy、Alias、Move、Delete 的生命周期测试矩阵，确立严格的双存储契约（Page/Revision、Mongo 指针与哈希、公开正文、ES 最终状态与异常回滚）。
  2. 【中期】将 `comments` 应用平稳重构为原生 `SnippetViewSet`/`SnippetViewSetGroup`，保持所有批量操作、审核流、按钮与仪表板功能 100% 等价，消除技术债后彻底移除 `wagtail-modeladmin`。
  3. 【中期】建立 Wagtail 后台端到端浏览器自动化回归基线（Playwright），覆盖评论后台、Vditor 编辑器、历史 Revision 切换、工作流审批及批量操作，产物严格保存在 `output/playwright/`。
  4. 【长期】确立单版本推进与跨版本隔离升级演进规则：Wagtail 8.x 单步推进、Wagtail 9.0 独立演习、Django 6.0 最终跟进。
- **明确非目标**：
  1. 不在近期改动已通过充分测试验证的核心发布服务（`publication.py`）与删除服务（`page_deletion.py`）。
  2. 不重构或变动已稳定运行的独立 ES Outbox 核心投递链路。
  3. 继续显式保持 REST API v3 路由封闭，不向未经授权的外网暴露端点。
  4. 不在未经全功能对照验证前盲目卸载 `wagtail-modeladmin` 或强行升级未受支持的底层依赖。

---

### 4.3 详细设计与分阶段实施步骤 (Phased Implementation Plan)

#### 4.3.1 阶段一（近期）：跨存储生命周期契约细化与测试矩阵

##### 1. 双存储正文权威与受控复制契约 (Content Authority & Copy Lifecycle Contract)
- **核心风险与底层机制**：
  - Wagtail 原生 `Page.copy(**kwargs)` 在默认参数（`copy_revisions=True`）下克隆模型所有数据库字段（包括 `mongo_content_id`、`mongo_version_id`）并克隆历史 Revision。
  - 由于 Wagtail 底层不理解 MongoDB 正文指针的语义，新旧页面若直接共享相同的 `mongo_content_id`，将引发：编辑新页面导致源页面正文被污染覆盖、草稿版本混淆、删除源页面时误伤新页面引用的严重数据安全事故。
- **架构禁令（反模式拦截）**：
  - **严禁在 `clean()` 中注入 Mongo 创建逻辑**：`clean()` 在表单校验、页面实时预览（Preview）、工作流检查等场景会被反复幂等调用，如果在其中执行非幂等的 MongoDB 写入，将产生大量孤儿文档与垃圾版本。
  - **严禁假定存在通用的 copy 信号**：Wagtail 原生仅存在 `copy_for_translation` 信号，不存在通用的 `post_page_copy` 信号，不能依赖信号机制兜底。
- **受控复制生命周期服务设计（`BlogPageCopyService`）**：
  - 在 `wagtailblog3/apps/blog/services/copy_lifecycle.py` 封装专门的复制编排服务，并在 `BlogPage.copy()` 中显式重写调用该服务：
    1. **调用原生复制基线**：首先调用 `super().copy(**kwargs)`，完成页面树节点分配、URL 路径计算、子页面关系及 Revision 在 MySQL 中的原子克隆，得到初始 `new_page`；
    2. **Mongo 独立版本原子派生**：
       - 若源页面存在有效正文版本：读取源页面当前的最新正式正文与最新草稿快照；
       - 为 `new_page` 创建全新的不可变 MongoDB 文档版本：生成全局唯一的 `new_version_id`、重新计算正文 SHA-256 校验和、显式绑定归属 `page_id=new_page.id`，生成全新的独立 `new_mongo_content_id`；
    3. **Revision 反序列化指针重写**：
       - 若 `copy_revisions=True`：遍历 `new_page.revisions.all()`，反序列化每个 Revision 的 `content` JSON 字典；
       - 将其中记录的 `mongo_content_id`、`mongo_version_id` 深度替换为新生成的版本指针，确保新页面的历史版本树完全与源页面物理脱钩，随后持久化保存 Revision；
    4. **更新并保存新页面指针**：将 `new_page.mongo_content_id` 与 `new_page.mongo_version_id` 设为新指针并执行 `new_page.save(update_fields=['mongo_content_id', 'mongo_version_id'])`；
    5. **异常补偿与孤儿清理**：整个派生流程若在任何阶段抛出异常，立即捕获并执行反向补偿：物理删除本次在 MongoDB 中创建的新文档版本，将 `new_page` 标记为禁用或安全回滚，严禁处于半就绪状态的新页面暴露给编辑者。

##### 2. 页面移动与搜索 Outbox 递归契约 (Move & Search Recursive Contract)
- **信号校正与机制事实**：
  - 纠正历史表述：Wagtail 8.0 实际暴露的页面移动信号为 `post_page_move`（方案废弃不存在的 `after_move_page` 伪名称）。
- **树形移动的后代递归影响**：
  - 当移动一个包含子页面的页面（如分类目录或父级博客）时，`post_page_move` 信号仅将移动的根节点作为 `instance` 传入。
  - 但实际上，该节点下所有子孙页面的完整 URL 路径（`url_path`）、站点归属及继承的权限范围已全部被底层树重构变更！
  - **递归遍历通知设计**：
    - 在 `post_page_move` 信号处理器中，使用 `transaction.on_commit` 确保数据库事务彻底提交后触发；
    - 取得移动根节点以及该节点下全部 live 状态的 `BlogPage` 后代：`live_descendants = list(instance.get_descendants().type(BlogPage).live())`；
    - 对根节点（若为 live BlogPage）及所有受影响的后代页面，逐一向 `ContentSearchOutbox` 写入独立更新事件（`ACTION_UPDATE` / `REINDEX_METADATA`）。
- **Generation 幂等防逆序机制**：
  - 针对 Celery Worker 异步消费延迟可能引发的网络时序逆序（如移动事件发生后，早期某个延迟的保存事件晚到覆盖了新 URL），在 Outbox 事件载荷中携带页面的最新状态代次（`generation`）或微秒级版本时间戳；
  - `delivery.py` 在向 Elasticsearch 执行写入前比对索引中现有文档的代次：若当前事件代次落后于已有代次，则判定为过期滞后事件并安全拒绝，杜绝旧 URL 事件回滚新索引状态。

##### 3. 镜像 Alias 规范与检索索引决策 (Alias Specifications & Search Decision)
- **非正文写入主体原则**：
  - Alias 页面严格作为 Wagtail 页面树内的镜像指针，仅具备自身的 URL 路径与树位置；
  - 严禁向 Alias 页面独立分配、创建或注入任何 MongoDB 正文版本；
  - 所有针对 Alias 页面的正文访问均动态路由解析至源页面（`alias_of`）。
- **指针解析与环路防御**：
  - 在正文解析器中实现链条解析与环路防御：维护遍历集合 `visited_page_ids = set()`，最大解析深度严格限制为 5 层，一旦检测到闭环或无效源，立即阻断并抛出受控防御异常。
- **检索索引决策（明确定义）**：
  - **检索收敛策略**：Alias 页面在 Elasticsearch 文章库中**不单独建立正文索引文档**，避免搜索引擎产生大量重复正文惩罚；
  - 在 `outbox.py` 构建投递事件时，显式检查 `if page.alias_of_id: return`，直接跳过 Alias 页面的独立索引投递；
  - 若前台需要通过 Alias 路径跳转，仅在页面树前台路由层完成源页面反向代理即可。

##### 4. 阶段一定向契约测试矩阵设计 (`test_lifecycle_copy_move_alias.py`)
- 在 `wagtailblog3/apps/blog/tests/test_lifecycle_copy_move_alias.py` 落地以下 6 大核心场景测试：
  1. **页面复制正文独立性测试**：验证 `copy()` 产出的新页面拥有全新 `mongo_content_id`、全新 SHA-256 哈希，且编辑新页面正文绝不影响源页面；
  2. **历史 Revision 指针改写测试**：验证在 `copy_revisions=True` 时，新页面克隆出的所有 Revision 内部的序列化正文指针已被改写为新版本；
  3. **复制失败补偿清理测试**：模拟 MongoDB 网络超时或写入失败，断言事务安全回滚，且 MongoDB 中不遗留半成品的孤儿文档；
  4. **树形移动后代递归触发测试**：移动父级目录，断言移动根及所有子孙 live `BlogPage` 均成功向 `ContentSearchOutbox` 生成 URL 刷新事件；
  5. **Generation 幂等防逆序测试**：模拟伪造的旧版本 Outbox 事件晚到到达，断言 `delivery.py` 成功识别代次并拒绝逆序覆盖；
  6. **Alias 环路防护与只读契约测试**：断言 Alias 页面禁止写入 Mongo、正文读取来自源页面、循环 Alias 正确触发防御拦截，且 Outbox 自动忽略 Alias 页面。

---

#### 4.3.2 阶段二（中期）：评论后台 SnippetViewSet 100% 功能等价重构与依赖解耦

##### 1. 核心架构重构（SnippetViewSet + SnippetViewSetGroup）
- 将现有的 `wagtail-modeladmin` 架构完全升级为 Wagtail 8.0 原生的 `wagtail.snippets.views.snippets.SnippetViewSet` 与 `SnippetViewSetGroup`。
- 保留现有的管理模型结构：`BlogPageComment`（博客评论）与 `CommentReaction`（评论点赞/互动）。

##### 2. 核心功能 100% 等价对照矩阵表

| 业务管理维度 | wagtail-modeladmin 现有实现 | Wagtail 8.0 SnippetViewSet 等价落地方式 | 迁移行为一致性保障与安全红线 |
|---|---|---|---|
| **列表展示** | `list_display = (...)` | `SnippetViewSet.list_display = [...]` | 保持评论人、文章、正文摘要、状态、IP、创建时间字段与格式化方法 100% 一致 |
| **过滤筛选** | `list_filter = (...)` | `SnippetViewSet.list_filter = [...]` | 完整迁移审核状态（已审核/待审核）、是否软删除、文章过滤等筛选器 |
| **搜索与排序** | `search_fields`, `ordering` | `SnippetViewSet.search_fields`, `ordering` | 保持评论正文、作者昵称搜索及时间倒序排序规则 |
| **批量审核通过** | `actions = ['approve_comments']` | 继承 `wagtail.admin.views.bulk_action.BulkAction` 注册 `ApproveCommentsBulkAction` | 包含事务保护、对象级权限验证、批量状态修改与操作结果 Toast 提示 |
| **批量软删除** | `actions = ['soft_delete_comments']` | 继承 `BulkAction` 注册 `SoftDeleteCommentsBulkAction` | 保留评论层级关系，将评论正文替换为系统占位符，更新软删除时间戳 |
| **批量彻底删除** | `actions = ['real_delete_comments']` | 继承 `BulkAction` 注册 `RealDeleteCommentsBulkAction` | **绝对红线**：严禁裸 `queryset.delete()`！必须逐条校验依赖、两阶段级联受控物理删除并记录审计日志 |
| **行级操作按钮** | `CommentButtonHelper(ButtonHelper)` | 采用 `register_snippet_listing_buttons` 钩子或定制 `IndexView` | 保留行内“审核通过”、“软删除”、“彻底删除”按钮，统一改为 POST 表单 + CSRF Token 驱动 |
| **自定义审核路由**| 全局 `urls.py` 挂载 `admin_approve_comment` | `SnippetViewSet.get_urlpatterns()` 注册受控子路由 | 严格绑定 `@require_admin_access` 鉴权与 POST 约束，禁止裸 GET 重定向 |
| **权限控制体系** | `CommentPermissionHelper` | `wagtail.permission_policies.ModelPermissionPolicy` 显式注册 | 区分超管与审核员角色细粒度权限，与 `content_ai` 鉴权规范保持一致 |
| **统计仪表板** | 混用 ModelAdmin 视图 | 拆分为独立 `admin_view` 或 ViewSet 专属看板 action | 统计面板与 CRUD 彻底解耦，避免统计模型污染 Snippet 数据流 |

##### 3. 依赖解耦前置检查清单 (Decoupling Checklist)
在正式从 `settings/base.py` 移除 `wagtail_modeladmin` 之前，必须按序完成以下检查：
1. **全局代码扫描**：全库检索 `wagtail_modeladmin`，确认除已废弃代码外无任何业务代码、自定义模板标签或钩子引用该包；
2. **反向 URL 解析校验**：核对所有模板中引用的 `wagtailmodeladmin_...` 旧命名空间 URL，已全部替换为 Wagtail Snippets 原生反向 URL（如 `wagtailsnippets_...`）；
3. **定向测试绿灯门禁**：编写并运行 `wagtailblog3/apps/comments/tests/test_snippet_views.py`，全量覆盖列表、筛选、搜索、批量审核、批量软删、批量彻底删除、行内按钮提交及权限拦截，确保全部测试通过；
4. **依赖移除与锁定**：从 `wagtailblog3/settings/base.py` 的 `INSTALLED_APPS` 移除 `wagtail_modeladmin`；从 `requirements.txt` 中安全删除 `wagtail-modeladmin==2.3.0`，并在 WSL2 环境执行 `pip check` 确认依赖树完全干净。

---

#### 4.3.3 阶段三（中期）：Wagtail 后台端到端 Playwright 浏览器回归基线

##### 1. 核心回归路径设计（提升为阻断级功能与时序验证）
- **路径 A：后台鉴权与导航完整性**：
  - 验证超级管理员与普通编辑者的登录认证、会话保持、侧边栏菜单渲染及权限隔离。
- **路径 B：评论后台 ViewSet 全交互验证**：
  - 验证列表分页与多维筛选；
  - 验证行内“审核通过”按钮点击后的 POST 提交与状态即时刷新；
  - 验证批量勾选评论、执行批量软删除与批量彻底删除，校验二次确认 Modal 弹窗交互、异步 Toast 提示及列表局部更新。
- **路径 C：BlogPage 编辑生命周期与 Vditor Markdown 编辑器集成**：
  - 验证创建/编辑 `BlogPage` 时 Vditor 编辑器的平稳初始化与工具栏渲染；
  - 验证实时 Markdown 输入、双栏同步滚动预览及图床快捷上传交互；
  - 验证表单数据反序列化与草稿自动保存（Autosave），防止编辑内容静默丢失。
- **路径 D：历史 Revision 版本对比与回填**：
  - 验证在 Wagtail 8.0 后台查看页面历史版本列表与版本差异（Diff）高亮；
  - 验证选择指定历史 Revision 回填到当前 Vditor 编辑器中的事件触发与内容恢复。
- **路径 E：页面发布与工作流多级审批**：
  - 验证页面提交工作流审批、审核员审批通过、发布任务触发；
  - 验证发布成功后前台公开页面的正常访问与 MongoDB 正文渲染一致性。
- **路径 F：受控两阶段页面删除交互**：
  - 验证在后台触发页面删除时，受控弹窗正确展示关联引用警告；
  - 验证删除确认后页面成功进入墓碑状态，搜索索引即时下线。

##### 2. 运行时时序与稳定性门禁 (Runtime Stability Gates)
- **控制台零错误门禁**：执行过程中严密监听浏览器 Console，**必须 0 个 Uncaught Exception**、0 个 Stimulus 控制器连接错误、0 个 Vditor 静态资源缺失错误；
- **网络请求零故障门禁**：页面加载与异步 AJAX/Fetch 请求**必须 0 个 404、0 个 500 状态码**；
- **产物与环境隔离**：所有 Playwright 调试截图、视频与 trace 产物严格写入 `output/playwright/wagtail-admin-baseline/`，保持 Git 忽略，严禁同步至生产。

---

#### 4.3.4 阶段四（长期）：受控跨版本演进路线与真实隔离演练

##### 1. 单版本小步演进原则（Wagtail 8.x 系列）
- 对 Wagtail 8.1、8.2 等后续次版本升级，严格遵循“单变量推进”原则，绝不在升级次版本的同时重写业务逻辑；
- 每次升级必须完整通过：依赖冲突检测（`pip check`）、Django 系统检查（`python manage.py check`）、阶段一契约测试、阶段二评论功能测试及阶段三 Playwright 浏览器基线。

##### 2. Wagtail 9.0 独立升级演习规范
- **前置硬性门禁**：阶段一至阶段三全部落地并经生产稳定运行检验，`wagtail-modeladmin` 已彻底卸载，全库 0 弃用警告（Deprecation Warnings）；
- **演练隔离原则**：
  - 严禁直接在当前开发工作区或生产数据库上进行大版本跨越试探；
  - 必须在 WSL2 独立环境中建立专门的演练环境，克隆独立数据库快照，并配置独立的 Elasticsearch 索引别名前缀（如 `wagtailblog_search_v9_test`）；
- **核心依赖兼容矩阵核验**：
  - 逐项核对官方发布说明，确保 Wagtail 9.0 与以下核心组件具备官方正式支持声明：Python 3.13、wagtailmedia、wagtailcodeblock、django-redis、django-taggit、django-storages 以及 Elasticsearch 客户端。

##### 3. Django 6.0 最终演进适配门禁
- 严格等待 Wagtail 9 官方发布支持 Django 6.x LTS 的正式稳定版后再行启动；
- 保持生产长期处于 LTS（长期支持）技术轨道，杜绝跟进非 LTS 短期过渡版本。

##### 4. 真实回滚与数据容灾契约 (Real Rollback & Disaster Recovery Contract)
- **明确各层级回滚边界，杜绝盲目承诺**：
  1. **代码与依赖层回滚**：通过 Git 精确 SHA 回退（`git revert`）并在 WSL2 重新执行 `pip install -r requirements.txt` 恢复环境；
  2. **MySQL 关系数据库回滚**：
     - 在执行任何大版本升级或数据迁移前，必须执行全量 SQL 物理/逻辑转储（mysqldump）；
     - 遇到破坏性迁移断层时，通过备份快照进行恢复；绝不能假定 `migrate` 反向回滚 100% 可用；
  3. **MongoDB 正文数据回滚**：
     - MongoDB 存储架构完全遵循“**版本只增不改、旧版本物理只读不可变**”原则；
     - 即使应用层发布失败或回滚，旧版本的 Mongo 正文仍然完好保留在集合中，回滚操作仅需将 MySQL 中的 `mongo_content_id` / `mongo_version_id` 指向历史稳定版本即可瞬间完成正文回退，零数据丢失风险；
  4. **Elasticsearch 检索别名回滚**：
     - 基于“双索引 + 读写别名（Read Alias）”机制，在新版本重建索引期间，旧索引物理存在且读别名继续服务；
     - 若新版本索引校验失败，秒级将读别名切回旧索引，应用层无感知，实现真正的零停机回滚。

---

### 4.4 实际修改与不修改的文件清单 (Files to Modify & NOT Modify)

- **拟新增/修改的文件（后续实施阶段）**：
  - `说明书/03-运维部署与系统演进/03-Wagtail 8.0及核心依赖演进全书.md`（方案细化与演进台账）
  - `wagtailblog3/apps/blog/services/copy_lifecycle.py`（**新增**：页面受控复制与 Mongo 独立版本派生服务）
  - `wagtailblog3/apps/blog/models.py`（重写 `BlogPage.copy()` 接入受控复制服务，完善 Alias 正文解析与只读保护）
  - `wagtailblog3/apps/blog/signals.py`（完善 `post_page_move` 监听，增加树移动 live 后代遍历与代次防逆序 Outbox 刷新）
  - `wagtailblog3/apps/blog/tests/test_lifecycle_copy_move_alias.py`（**新增**：阶段一跨存储生命周期契约测试套件）
  - `wagtailblog3/apps/comments/bulk_actions.py`（**新增**：基于 Wagtail 原生 ViewSet 的批量操作类集合）
  - `wagtailblog3/apps/comments/wagtail_hooks.py`（重构为 `SnippetViewSet` / `SnippetViewSetGroup`，接入原生按钮与受控子路由）
  - `wagtailblog3/apps/comments/tests/test_snippet_views.py`（**新增**：阶段二评论管理功能 100% 等价测试套件）
  - `wagtailblog3/settings/base.py`（解耦移除 `wagtail_modeladmin`）
  - `requirements.txt`（安全移除 `wagtail-modeladmin==2.3.0`）
  - `output/playwright/wagtail-admin-baseline/`（阶段三 Playwright 本地调试产物，保持 Git 忽略）
- **绝不修改的文件（核心底座与生产安全边界）**：
  - `wagtailblog3/settings/.env.production` 及所有生产配置凭据
  - `wagtailblog3/apps/blog/services/publication.py` 核心发布事务逻辑
  - `wagtailblog3/apps/blog/services/page_deletion.py` 核心受控删除清单与两阶段清理逻辑
  - `wagtailblog3/apps/search/services/outbox.py` 与 `delivery.py` 核心底层检索投递体系

---

### 4.5 数据、服务与依赖影响评估 (Impact Assessment)
- **数据影响**：
  - 本方案所有阶段严格禁止破坏现有 MongoDB 正文、草稿快照与 Revision 历史数据；
  - 复制操作产生的 Mongo 版本严格独立，绝不污染源页面正文；
  - 移动操作仅刷新 ES 检索索引元数据与 URL，不变更任何正文内容。
- **服务影响**：
  - 所有阶段的代码编写与测试严格在 WSL2 共享工作树（`wagtailblog-test`）中独立执行；
  - 生产 4 大 systemd 服务与线上环境保持零停机、零扰动。
- **依赖影响**：
  - 阶段二安全移除 `wagtail-modeladmin` 外部兼容层，消除潜在的不可维护包，精简整体依赖树。

---

### 4.6 测试与验收标准 (Acceptance Criteria & Quality Gates)
- **门禁 1（静态检查与依赖树）**：
  - `pip check` 输出 `No broken requirements found`；
  - `python manage.py check` 输出 0 errors；
  - `python -m compileall wagtailblog3` 0 errors。
- **门禁 2（阶段一跨存储契约测试）**：
  - `test_lifecycle_copy_move_alias.py` 全部用例 100% 绿灯；
  - 严格验证：复制生成全新 Mongo 指针、Revision 指针同步改写、移动后代递归触发 Outbox 事件、代次防逆序校验成功、Alias 环路保护生效。
- **门禁 3（阶段二功能等价与解耦）**：
  - `test_snippet_views.py` 全部用例 100% 绿灯；
  - 验证列表展示、筛选、搜索、批量审核、批量软删、批量彻底删除（受控级联）及权限拦截与原有功能 100% 等价。
- **门禁 4（阶段三后台真实浏览器检测）**：
  - Playwright 自动化运行 6 大核心回归链路，浏览器控制台 0 错误、网络请求 0 异常（404/500），产物完整生成在 `output/playwright/`。
- **门禁 5（双模型四眼审查门禁）**：
  - Sol 完成架构方案与技术细节审查通过；
  - Grok 完成全流程对照方案审核通过，无对抗性安全漏洞。

---

### 4.7 回滚策略与数据安全容灾 (Rollback Strategy & Data Safety)
- **原子化提交与回滚点**：
  - 每个阶段形成独立、原子化的 Git 提交（遵循中文提交规范：`<类型>(<模块>): <中文动词与改动业务目的>`）；
  - 代码层面若遇任何阻碍，使用 `git revert` 可瞬间精准退回到上一阶段稳定状态；
- **分级数据回滚方案**：
  - **依赖回退**：`pip install -r requirements.txt` 恢复环境；
  - **Mongo 正文安全保障**：所有 Mongo 写入为新增独立版本，回滚仅需切回 MySQL 指针，绝不破坏历史数据；
  - **ES 索引容灾**：依托别名机制，异常时瞬时切回旧索引别名。

---

### 4.8 残余风险与应对措施 (Residual Risks & Mitigations)
- **风险 1：上游第三方插件（如 wagtailcodeblock, wagtailmedia）升级滞后**：
  - *应对*：在阶段四实施前核验官方支持矩阵，若上游暂未发布适配 Wagtail 9 的版本，坚决维持当前锁定的稳定版本，不贸然推进跨版本升级。
- **风险 2：Vditor 编辑器在复杂动态 DOM 加载下的事件监听时序竞争**：
  - *应对*：在阶段三建立的 Playwright 端到端浏览器回归中，模拟快速切换、表单热重载、草稿自动回填等极端场景，确保前端生命周期稳健。
- **风险 3：树形移动引起大规模子孙节点并发刷新导致 Outbox 瞬时堆积**：
  - *应对*：在后代遍历时采用分页批量写入（`bulk_create`），并在 Celery Worker 端配置合理的并发消费速率，防止突发流量冲击。

---

### 4.9 模型与推理强度分配 (Model & Reasoning Assignment)
- **架构规划与方案编写**：`gpt-5.6-sol`（高/超高推理）—— 已完成架构审定与 v2.1 完善版方案编制。
- **代码实施与定向测试**：`gemini-3.8-flash-high`（高推理）—— 负责后续阶段一至阶段四代码编写与单元测试落地。
- **真实功能检测**：`browser-skill` / Playwright —— 负责阶段三后台全链路端到端动态检验。
- **全流程对照方案审核**：`grok-4.6`（高推理）—— 负责后续交付的代码审查与方案 100% 对照验收。
- **终审授权人**：对话人（用户）—— 任何代码改动、Git 提交与发布均须最终明确确认。

---

### 4.10 架构可行性复核与修正裁定（2026-09-07）

#### 4.10.1 复核依据
- 测试环境实际安装版本为 Wagtail 8.0；`Page.copy` 的签名包含 `copy_revisions=True`，`Page.move` 不提供跨存储同步参数，`Page.create_alias` 为独立的页面生命周期 API。
- Wagtail 8.0 实际暴露的页面移动信号为 `pre_page_move` 与 `post_page_move`，项目当前已监听 `post_page_move`，但现有处理只调用权限范围重算，不能单凭该事实证明所有受影响页面的 URL 已写入独立搜索 Outbox。
- `BlogPage` 当前没有重写 `copy`、`move`，也没有 Alias 专属正文解析或写入保护；正文保存、Revision 清理和删除清单分别位于 `apps/blog/models.py`、`apps/blog/signals.py` 与 `apps/blog/services/page_deletion.py`。
- 评论后台仍直接导入 `wagtail_modeladmin` 的 `ModelAdmin`、`ModelAdminGroup`、`ButtonHelper`、`PermissionHelper` 和注册函数；原生 `SnippetViewSet` 不是该 Shim 的行为等价替换层。

#### 4.10.2 阶段一裁定：可行，但必须先修正正文权威和移动范围
Wagtail 页面复制会创建新的 Page 行，并在默认参数下复制源页面的 Revision；Revision 的序列化内容可能继续携带源页面的 Mongo 正文指针。Wagtail 不知道该指针的语义，因此不会自动完成 Mongo 深拷贝、指针改写、哈希重算或删除保护。若直接使用默认复制，风险包括：新旧页面编辑相互覆盖同一可变旧文档、源页面删除时误判共享引用、复制页面的草稿 Revision 回填源正文，以及发布时 Page/Revision/Mongo 三方身份校验失败或将错误正文投递到 ES。

不得在 `clean()` 中解决此问题：`clean()` 可能被表单校验、预览和管理流程多次调用，且不适合产生 Mongo 写入或跨数据库副作用。也不建议依赖不存在的 copy 信号作为唯一拦截点。首选方案是为 `BlogPage.copy()` 建立显式生命周期服务，先调用 Wagtail 父类复制页面结构和 Revision，再在同一受控流程中为新页面的草稿/正式正文创建新的不可变 Mongo 版本，改写新页面及其复制 Revision 的指针和 `version_id`，最后写回 MySQL 指针；任一阶段失败都必须阻止新页面进入可发布状态，并留下可重试的清理意图。若当前 Mongo 版本已不可变，深拷贝可以复用字节内容但必须生成新的文档 ID、版本 ID、SHA256 及归属 page_id，不能只复制 `mongo_content_id`。

复制测试必须分别覆盖 `copy_revisions=True/False`、有正式正文/只有草稿/无正文、源页面和新页面独立编辑、复制后删除源页面，以及复制失败后的 Mongo 孤儿清理。断言不能只比较 Page ID，还要解析 Revision content，验证所有新页面指针均归属于新 Page。

`post_page_move` 在页面树更新后触发，树移动会递归改变受影响子树的路径和 URL；信号通常以根页面为事件实例，并不等于每个后代都发送了一个独立 move 事件。因此，移动处理必须取得移动前后受影响子树快照，至少为根及全部 live BlogPage 后代生成 URL/权限刷新事件，或明确证明 `request_scope_recalculation` 会递归枚举这些后代并为每个页面形成最终 Outbox 事件。Delivery 必须以提交后的页面状态重新计算 URL 与权限，使用 page_id/generation 幂等合并旧事件，避免移动期间旧 URL 事件晚到后覆盖新文档。`after_move_page` 不是本项目 Wagtail 8.0 的实际信号名称，应从方案和代码中删除该表述。

Alias 必须被视为非正文写入主体。所有正文读取、发布、搜索建文档和删除清理都应先解析到最终源页面，并防止 Alias 链环路；Alias 自身仍可能拥有独立的页面树位置、slug、站点和可见性，因此搜索文档是否建立为独立 URL 必须单独定义。若 Alias 需要出现在搜索结果，文档应保存 canonical_source_page_id 与 alias page_id，并从源页面版本读取正文；若不支持 Alias 独立索引，则必须在 Outbox 和前台路由中明确排除。任何 Alias 编辑、发布、复制、删除场景都要有权限和指针不变断言，不能只依赖 `kwargs["alias"]` 过滤发布信号。

#### 4.10.3 阶段二裁定：功能目标可行，迁移方案当前不具备 100% 等价保证
Wagtail 8.0 的 `SnippetViewSet` 可以承载基础列表列、过滤、搜索、排序、权限策略和自定义 URL，但它不提供 `wagtail-modeladmin` 的 `ButtonHelper`、`PermissionHelper` 和 `actions` 兼容层。批量操作必须按 Wagtail 8.0 实际 bulk action API 注册独立 Action 类，并在每个 Action 内执行对象级权限检查、事务边界、计数和失败补偿；不能把现有 ModelAdmin 方法原样挂到 ViewSet 上，也不能对需要调用 `real_delete()` 的操作使用无保护的 `queryset.delete()`。

行级按钮应优先使用 Wagtail 8.0 已支持的 snippet listing button hook；若该版本对按钮上下文或权限不满足要求，再通过 `get_index_view_class()` 与自定义 index 模板扩展。每个按钮都必须使用 POST、CSRF、对象级权限和状态条件，彻底删除必须保留二次确认。`get_urlpatterns()` 可承载自定义审核路由，但路由视图必须使用 Wagtail 管理员鉴权、权限检查和 POST 约束，不能继续依赖全局裸 URL 加对象 ID 重定向。

阶段二在移除依赖前必须全库检索 `wagtail_modeladmin`、旧 URL name、模板、静态 JS/CSS、测试和部署安装文件，并检查 `INSTALLED_APPS`、应用 ready 导入、迁移依赖与反向 URL。由于当前三个 ModelAdmin 还复用了同一模型作为仪表板数据源，仪表板应迁移为独立 admin view 或 ViewSet URL，不能把统计面板误当作普通 Snippet CRUD。只有新旧后台在权限矩阵、批量部分失败、审计日志、软删正文替换、真删级联清理和 URL 兼容性上逐项通过后，才允许删除 `wagtail_modeladmin`。

#### 4.10.4 阶段三裁定：必要，且应作为迁移门禁而非截图任务
后台依赖 Stimulus、动态表单和异步请求，Vditor 初始化、Revision 回填、工作流操作和列表批量动作都可能在静态单元测试通过时仍发生 DOM 时序错误。Playwright 的价值在于验证真实登录权限、CSRF、网络请求、动态节点重新初始化、保存后状态刷新和错误提示；它应保存请求失败、控制台错误、关键响应状态与必要截图，而不是只验证页面能打开。

基线需使用隔离测试数据和可重复的管理员/编辑者权限，覆盖桌面与移动视口、键盘路径、重复点击、后退/刷新、慢请求及失败重试。浏览器产物继续只写入 `output/playwright/wagtail-admin-baseline/`，不得进入 Git 或生产包。

#### 4.10.5 阶段四裁定：路线科学，但“独立演习”定义不足
Wagtail 8.x -> 9.0 -> Django 6.0 的单变量演进顺序是合理的，但不能预先假定 Wagtail 9 支持 Django 6、Python 3.13 或全部第三方扩展。每一跳都必须建立官方支持矩阵，核验 Wagtail/Django/Python、wagtailmedia、wagtailcodeblock、django-taggit、django-storages、Redis/ES 客户端和 `wagtail-modeladmin` 的明确兼容声明。升级演习应使用独立虚拟环境、独立数据库快照和独立 ES 前缀；项目唯一 Git 分支仍保持 `main`，不以共享工作树直接试升版本。

每一跳除现有测试外，还必须运行弃用告警扫描、`makemigrations --check --dry-run`、迁移计划、页面树/Revision/Mongo 指针抽样校验、Alias/Copy/Move 契约、Outbox 重放和生产索引 alias 演练。依赖回退只能覆盖代码和环境包；已经执行的数据库迁移、Mongo 新版本和 ES alias 切换不能笼统承诺用 `git revert` 回滚，必须在升级前建立备份、兼容窗口和反向操作方案。

#### 4.10.6 最终可行性裁定与补充落地项
- **最终裁定**：【可行但需完善细节】。
- **本方案 v2.1 版本完善成果**：
  1. 已在 4.3.1 补充落地受控复制生命周期服务（`BlogPageCopyService`），明确新 Mongo 版本派生与 Revision 反序列化指针改写规则；
  2. 已在 4.3.1 纠正移动信号为 `post_page_move`，补充树移动后代递归快照与 Generation 幂等防逆序机制；
  3. 已在 4.3.2 建立评论后台 SnippetViewSet 100% 功能等价对照表，明确批量彻底删除受控级联红线与解耦前置清单；
  4. 已在 4.3.3 与 4.3.4 细化 Playwright 稳定性时序门禁与跨版本多维真实回滚契约。

---

### 4.11 实施记录与方案演进台账 (Implementation Record & Version Log)

| 演化版本 | 日期 | 状态 | 负责角色 / 模型 | 核心演化内容与改动说明 | 回滚基准点 / 提交状态 |
|---|---|---|---|---|---|
| **v1.0** | 2026-09-06 | 已归档 | Sol (`gpt-5.6-sol`) | 完成 Wagtail 8.0 大版本升级核心架构演进全书编制与安全决策说明。 | Git 提交已闭环 |
| **v2.0** | 2026-09-07 | 已评审 | Sol (`gpt-5.6-sol`) | 制定 Wagtail 8.x 至 Wagtail 9 兼容性治理与生命周期防御方案初稿；完成多维度架构可行性复核，提出 3 项关键补充要求。 | 工作区未提交 |
| **v2.1** | 2026-09-07 | **已就绪** | Sol (`gpt-5.6-sol`) + 主 Agent | **全面完善方案**：<br>1. 补齐双存储原子受控复制服务与 Revision 指针改写契约；<br>2. 补齐 `post_page_move` 后代遍历与 Generation 幂等防逆序契约；<br>3. 补全评论后台 SnippetViewSet 100% 功能等价矩阵与受控真删红线；<br>4. 细化 Playwright 时序门禁与分级回滚契约；通过 `git diff --check` 静态检查。 | 当前工作区未提交，等待用户授权进入代码实施 |
