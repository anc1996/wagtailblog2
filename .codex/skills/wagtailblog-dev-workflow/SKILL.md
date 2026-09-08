---
name: wagtailblog-dev-workflow
description: Wagtail 8.0 & Django 5.2.8 full-stack engineering workflow for wagtailblog2. Use for dual-storage (MySQL/MongoDB) data contracts, Elasticsearch outbox indexing, WSL2 testing, Playwright verification, and Maker-Checker production deployment.
---

# WagtailBlog2 专属工程与运维技能规范 (wagtailblog-dev-workflow)

本技能沉淀了 wagtailblog2 项目的核心开发契约、测试验证流水线与生产发布门禁，供全栈智能体在执行需求实现、排障、测试与发布时加载使用。

## 1. 核心技术基线与环境事实

- **技术栈**：Python 3.13、Django 5.2.8、Wagtail 8.0（严格锁定在
equirements.txt）、MySQL 8.4、MongoDB (pymongo 4.11)、Redis 5.2、Elasticsearch 8.19、Celery 5.5。
- **物理拓扑**：
  - Windows 主机 (192.168.20.1)：负责文件编辑，NTFS 工作目录 F:\openclaw\workspace\wagtail\wagtailblog2；
  - WSL2 测试环境 (192.168.20.5, Debian)：共享同一 NTFS 目录 /mnt/f/openclaw/workspace/wagtail/wagtailblog2；Conda 环境 /root/anaconda3/envs/wagtailblog-test；
  - 生产虚拟机 (192.168.20.2, 主机名 ziliao, SSH:22)：生产项目 /home/source/Django/wagtail/wagtailblog3；Conda 环境 /root/anaconda3/envs/wagtailblog。

## 2. 数据模型与双存储契约 (Dual-Storage Contract)

### 2.1 MongoDB 与 MySQL 协同规则
- **正文数据保护**：BlogPage 正文、StreamField body、MongoDB 正文、草稿快照、revision pointer 与 mongo_content_id 是绝对受保护数据；
- **Markdown 不可变性**：Markdown 必须保持原始 Markdown 字符串，markdown_block 存储 key 不得改变，渲染时才转换为 HTML；
- **生命周期受控**：
  - 页面发布：必须通过 BlogPublicationService 保证 MySQL Page/Revision 与 MongoDB 文档哈希及版本号强一致；
  - 页面复制与别名：禁止共享可变正文指针，必须显式派生独立 Mongo 副本；
  - 页面删除：严禁直接调用裸 delete()，必须固化清单、校验引用后异步执行级联清理。

### 2.2 Wagtail 8.0 规范演进
- **后台管理组件**：废弃外部兼容层 wagtail-modeladmin，全面采用 Wagtail 8.0 原生 SnippetViewSet 与 SnippetViewSetGroup；
- **StreamField 块定义**：遵循 Telepath 序列化规范，保持向后兼容。

## 3. 异步任务与搜索索引治理 (Outbox & Search)

- **Outbox 事务投递**：文章索引事件写入 MySQL Outbox 表，与业务事务同批提交，杜绝直接在视图内同步写 ES；
- **Delivery 消费服务**：异步 Worker 扫描 Outbox 表，批量投影文章数据并更新 Elasticsearch；
- **读写别名与零停机重建**：
  - 生产文章索引严格通过 wagtailblog3_blog_page_read 和 wagtailblog3_blog_page_write 别名对外服务；
  - 索引全量重建时先写入物理版本索引，数据同步并验证无误后原子切换别名，零停机无感知。

## 4. 本地与 WSL2 测试验证矩阵

所有测试与静态检查必须在 WSL2 的 wagtailblog-test 环境中执行：

| 验证场景 | 命令示例 | 核心目的 |
|---|---|---|
| **全系统检查** | python manage.py check | 核验 Django 5.2 / Wagtail 8.0 系统完整性 |
| **全工程语法编译** | python -m compileall -q wagtailblog3 | 阻断隐蔽语法错误与字节码损坏 |
| **快速单元回归** | python manage.py test blog search archive --keepdb | 复用测试数据库，极速验证三大核心业务 |
| **AI 元数据专项** | python manage.py test blog.tests.test_ai_metadata --keepdb | 验证提示词解析与异常保护 |
| **搜索异步管道** | python manage.py test search.tests.test_search_async --keepdb | 验证 Outbox 投递与异步投递重试契约 |
| **格式与冲突检查** | git diff --cached --check | 拦截空白符与未解 Git 冲突 |
| **语义样式检查** | python tools/check_css_tokens.py | 验证 CSS 设计 token 一致性 |

## 5. Playwright 端到端浏览器验证规范

- **产物唯一输出目录**：所有 Playwright 调试产物（截图、trace、视频、HTML 报告）**统一写入 output/playwright/<task-name>/**；
- **严禁泄漏**：output/ 目录受 .gitignore 保护，严禁将其提交至 Git 或同步到生产环境；
- **验证范围**：覆盖前台响应式视口（桌面 1280x800 与移动 375x667）、Wagtail 后台登录、富文本/Markdown 编辑与关键表单交互，检查控制台是否有 JS 报错与 404 资源。

## 6. 生产服务编排与发布门禁 (Maker-Checker)

### 6.1 生产 4 大 systemd 服务与重启顺序
1. wagtailblog3.service：uWSGI / Django 核心应用
2. wagtailblog3-celery-maintenance.service：maintenance 队列异步 Worker
3. wagtailblog3-celery-beat.service：定时任务与补偿调度
4. wagtailblog3-filebeat.service：日志收集至 Elasticsearch

**重启顺序**：基础设施（MySQL/Mongo/Redis/ES） -> Django/uWSGI -> Maintenance Worker -> Beat -> Filebeat。

### 6.2 Maker-Checker 双模型四眼发布门禁
- **指挥官 Sol (gpt-5.6-sol)**：核对生产提交 SHA、制定原子发布指令清单与回滚预案；
- **执行官 Gemini (gemini-3.8-flash-high)**：严格执行 Sol 下达的单步终端命令，如实回传退出码与日志；
- **红线约束**：未经对话人（用户）明确授权，严禁擅自执行 Git commit/push 或在生产环境触发服务更新。
