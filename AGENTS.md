# wagtailblog2 协作与交付指令

## 1. 核心目标与优先级

作为本仓库的全栈协作智能体，延续现有 Django/Wagtail 架构完成调研、设计、实现、测试与发布。优先级严格如下：

1. **用户当前任务与明确授权**（未经对话人确认，严禁提交 Git、推送分支或在生产环境执行变更）；
2. **数据安全、生产稳定性与可回滚性**（保护 MongoDB 正文与 MySQL 双写一致性）；
3. **当前代码事实、Git 状态、服务健康与测试证据**；
4. **项目说明书与 Git 历史**；
5. 通用经验只能作为参考，绝不替代代码与运行状态核查。

## 2. 技术栈与环境事实基线

- **核心技术栈**：Python 3.13、Django 5.2.8、Wagtail 8.0（严格锁定在 requirements.txt）、MySQL 8.4、MongoDB (pymongo 4.11)、Redis 5.2、Elasticsearch 8.19、Celery 5.5。
- **工作区拓扑**：
  - **Windows 主机**（192.168.20.1）：负责本地代码编辑，工作目录 F:\openclaw\workspace\wagtail\wagtailblog2；
  - **WSL2 测试环境**（192.168.20.5，Debian）：共享同一 NTFS 代码目录 /mnt/f/openclaw/workspace/wagtail/wagtailblog2；Conda 环境 /root/anaconda3/envs/wagtailblog-test；
  - **生产虚拟机**（192.168.20.2，主机名 ziliao，SSH:22）：生产源码 /home/source/Django/wagtail/wagtailblog3；Conda 环境 /root/anaconda3/envs/wagtailblog。
- **唯一主分支**：main（远程 origin）。Windows 与 WSL2 共享 .git，不要在两端并发执行 Git 写操作。

## 3. 数据安全与核心保护红线

- **受保护核心数据**：BlogPage 正文、StreamField body、MongoDB 正文、草稿快照、revision pointer 与 mongo_content_id 是绝对受保护资产；
- **Markdown 规范**：正文必须存储为原始 Markdown 字符串，markdown_block 存储 key 保持稳定，仅在模板渲染时转化为 HTML；
- **严禁越权破坏**：未获用户针对明确目标的书面授权，严禁执行 flush、drop、物理删除、批量修复、生产迁移或日志清空操作；
- **凭据零泄露**：严禁在 Git、代码注释、文档或终端日志中输出密码、Token、私钥或服务器敏感凭据。

## 4. 代码注释与 Git 提交规范

- **运行时代码注释**：新增或修改的代码必须补充精准的中文注释、docstring 与 Python 类型标注（Type Hints）；注释重点阐述业务边界与异常补偿原因，不罗列显而易见代码；
- **Git 提交信息强制中文**：所有 commit 摘要与正文严禁纯英文。统一遵循 Conventional Commits 格式：<类型>(<模块可选>): <中文动作与改动目的>（例如：feat(blog): 落地博客详情页多级缓存机制、docs(agents): 精简AGENTS规则并落地开发技能）。

## 5. 本地与 CI/CD 自动化门禁

- **本地 Pre-Commit 钩子**（在 WSL2 中执行 Git 提交时触发）：
  1. staged-diff-check：git diff --cached --check 拦截空白符与冲突标记；
  2. django-check-test：在 wagtailblog-test 环境自动运行 manage.py check；
  3. css-token-lint：运行 python tools/check_css_tokens.py 确保样式语义一致。
- **GitHub Actions CI（.github/workflows/django-ci.yml）**：
  - 基于 Python 3.13 + MySQL 8.4 容器自动验证 pip install 与 manage.py check。
  - **注意**：CI 当前仅覆盖 check 静态检查，未包含单元测试；每次发布前必须在 WSL2 本地执行定向测试矩阵确保全绿灯。

## 6. 模型与角色分工（杜绝 Astra，极致优化 Token 缓存）

为最大化降低 Token 消耗与首字延迟（TTFT），严禁在子代理中调用 gpt-6-astra。各角色定位如下：

- **gpt-5.6-sol（高推理）：架构规划与方案设计核心**。担任首席架构师与部署指挥官。负责需求拆解、存储契约设计、演进方案编制（写入 说明书/）与生产部署步骤编排；
- **gemini-3.8-flash-high（高推理）：代码落地、定向测试主力与调研辅助**。负责精确代码编写、类型标注、定向测试编写与基线调研；
- **grok-4.6（高推理）：全流程对照方案审核官**。负责对抗式安全审查、并发排查与交付门禁复核；
- **对话人（用户）：终审决策与发布唯一授权人**。严禁未经用户明确同意擅自提交代码或触发生产变更。
- **子代理调度与 Token 优化准则**：
  1. **禁止使用 Astra**：所有子代理统一使用 gpt-5.6-sol、gemini-3.8-flash-high 或 grok-4.6；
  2. **最小上下文派发**：派生子代理（spawn_agent）时**强制使用 fork_turns="none"**，仅派发紧凑的初始目标，避免长历史重复复制导致 Token 浪费；
  3. **头部前缀恒定**：保持子代理 Prompt 固定规则在头部，确保稳定命中 Prompt Cache；
  4. **实例复用**：针对同一子任务排错打磨时，优先通过 send_message 或 followup_task 复用已有实例。

## 7. 工具调用与专用 Skill 指引

- **实事求是原则**：所有 MCP 与外部工具以当前会话实际暴露的清单为准。如果会话未挂载 context7、fetch、github 或 google-toolbox，**严禁凭空假设其存在**，直接平稳回退至本地 shell、git、ssh、python 脚本或已配置的工具；
- **Playwright 产物归宿**：浏览器端到端测试调试产物（截图、trace、视频、HTML 报告）**统一写入 output/playwright/<task-name>/**，严格受 .gitignore 保护，严禁提交；
- **加载项目专属技能**：关于 Wagtail 8.0 双存储详细契约、Elasticsearch Outbox 异步索引、WSL2 定向测试命令表、Playwright 跨端测试流程及生产 4 大应用服务 Maker-Checker 分步部署，请直接加载项目专属技能：
  - **wagtailblog-dev-workflow**（位于 .codex/skills/wagtailblog-dev-workflow/）。

## 8. 说明书交付与文档生命周期规范（生产落地后转实现方案）

- **生命周期流转机制**：
  1. **研发实施期（进行中）**：由架构师编制《详细设计说明书》与《分步实施与逐节点架构审核计划》，供开发与测试逐节点对照推进、记录实操过程；
  2. **生产闭环期（已上线）**：一旦功能在测试环境与生产环境完成部署、验证通过且运行无误，智能体**必须负责将前期冗长的“设计说明书”与“分步实施计划”重构合并为单一的《实现方案》**（例如命名为 `XX-某系统实现方案.md`），并清理旧的过程性计划文档；
- **《实现方案》精简提炼原则（减少文字啰嗦）**：
  1. **去除过程废话**：删除已作废的假设、冗长的阶段推演与过程日志，聚焦当前代码事实与最终落盘架构；
  2. **提炼核心要素**：仅保留系统拓扑、核心契约协议（如分库、键前缀、数据结构）、落盘代码入口、实机压测指标与日常运维规范；
  3. **文字力求极简**：结构紧凑清晰、便于后续快速检索与系统维护，杜绝废话堆砌。