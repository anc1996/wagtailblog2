# 多智能体协同交互协议与异构模型调度规范 (AGENTS INTERACTION PROTOCOL)

> **版本**：v3.0 (2026-10-14)
> **适用范围**：Codex 多智能体协作系统（包含 OpenAI、Google、Anthropic、xAI 跨厂商异构模型）
> **核心目标**：建立基于“ai-memory 共享认知总线（全面替代 output/ 物理文件黑板）”的低成本、零上下文污染、强数据核验、高鲁棒性、跨会话永不遗忘的工业级多智能体协同机制。

---

## 1. 异构模型能力分工与性价比矩阵 (80/20 调度法则)

在分布式多智能体系统中，来自不同厂商（Google, OpenAI, Anthropic, xAI）的大模型具有不同的优势、推理深度与成本特征。本项目推行**以性价比为导向的 80/20 协同分工**：

| 角色代码 (`agent_type`) | 运行模型 | 推理级别 | 厂商与特性 | 核心职责 (适合干啥) | 行为禁区 (绝对不能干啥) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`gemini`** | `gemini-3.8-flash` | `high` | Google<br>1M+ 上下文，超高吞吐，**成本极低 (近乎免费)** | **80% 任务首选（先锋落地编码与测试执行主力）**：<br>1. **常规代码实现**：编写 TDD 单元测试用例代码、轻量业务函数、参数校验器、序列化/表单转换、辅助工具函数（Utils/Helper）及代码脚手架；<br>2. **自动化测试执行**：接管 `browser-skill` 执行真实 E2E 浏览器点选、表单录入与异常连点；<br>3. **测试套件运行**：运行数百项单元测试/回归测试；<br>4. **快照抓取与初稿**：抓取三库（MySQL/Mongo/Redis）只读数据快照并格式化，输出交付报告初稿。 | 1. 严禁对 MongoDB 执行任何物理硬删；<br>2. 严禁在无契约指导下私自修改高风险核心架构（事务/锁序）；<br>3. 严禁越权直接发起 Git 本地提交或合流；<br>4. 严禁在日志中输出明文凭据。 |
| **`sol`**<br>(主代理) | `gpt-6.1-sol` | `high` | OpenAI<br>旗舰级深度推理，全局观最强，单次成本较高 | **20% 核心中枢（主脑架构师与项目总管）**：<br>1. 需求拆解与架构设计（定义分布式事务与 RB 回滚边界）；<br>2. 任务结构化拆解并签发“五要素任务包”；<br>3. 复核子代理交付报告与三库快照对账；<br>4. 核心疑难 Bug 根因诊断与后端核心代码最小修复；<br>5. 负责功能分支的本地 Git 原子提交把关。 | 1. 严禁亲自执行冗长繁琐的浏览器 UI 逐项点选；<br>2. 严禁不看证据盲目轻信子代理结论；<br>3. 严禁越权执行 `git push` 或合流 `main`。 |
| **`grok`** | `grok-4.7` | `high` | xAI<br>非共识批判思维，擅长找茬与边界挑战 | **只读对抗性质检员（安全与逻辑审计）**：<br>1. 以只读身份审查 Sol 的方案设计与 Gemini 的代码实现；<br>2. 专门寻找并发死锁、事务未回滚、伪造测试、异常被吞等风险；<br>3. 提出阻断性缺陷（Blocker）。 | 1. 严禁赋予任何文件修改权限（纯只读）；<br>2. 严禁直接执行测试脚本或生产操作。 |
| **`sonnet`** | `claude-sonnet-5-5` | `high` | Anthropic<br>深厚框架机制洞察力，代码地道严谨 | **疑难攻坚特战队（深层框架修复）**：<br>1. 攻坚 Wagtail 内部页面树（Treebeard）、页面别名（Alias）、复杂 Signals 钩子等疑难机制缺陷；<br>2. 实施最小外科手术式修复。 | 1. 日常简单任务不主动调用，避免成本浪费；<br>2. 不修改已排除的底层原生脚本。 |
| **`opus`** | `claude-opus-5-5` | `high` | Anthropic<br>顶尖哲学与架构推演能力，成本最高 | **战略储备（重大架构第二意见）**：<br>仅在发生重大架构争议、跨存储核心取舍不决时被动触发。 | 严禁用于日常编码、测试与普通审查。 |
| **`astra` / `luna`** | `gpt-6-astra`<br>`gpt-6-luna` | `high` | OpenAI | **弹性与备用工程代理**：在特定受控范围内承担独立分析或备用工程实现。 | 不替代默认的 Sol 主控中枢。 |

---

## 2. 异构协同的核心底座：ai-memory 共享认知总线（全面替代 output/ 物理文件黑板）

### 为什么不同公司的模型能够协同工作？
- **底层原理**：各厂商模型的分词器（Tokenizer）、提示词结构和上下文机制各异。如果在内存或同一条聊天记录中直接交互，会导致严重格式混乱与上下文膨胀。
- **全面升级：ai-memory 共享认知总线**：
  - **认知总线载体**：由运行在本地的 `ai-memory` MCP 服务充当（作用域锁定 `workspace = "default"`, `project = "wagtailblog2"`）；
  - **通信协议**：统一采用标准 **ai-memory 页面**：
    - 主代理任务派发存为：`tasks/<task_id>.md`
    - 子代理执行报告存为：`reports/<task_id>.md`
    - 规则与设计决策存为：`_rules/<rule_name>.md` 与 `decisions/<decision_name>.md`
  - **全面替代 output/ 物理黑板的原因与优势**：
    1. **彻底解决物理磁盘半截损坏与碎片堆积**：原有 `output/` 模式极易产生 `.tmp` 残留、多代理写冲突和杂乱散落文件。`ai-memory` 基于 SQLite + ACID 事务，单次写入具备原子性，天然具备版本回溯与历史链条；
    2. **结构化与语义检索共存**：所有智能体均可通过 `memory_read_page` 精确读取指定报告，也可通过 `memory_query` 进行混合全文与语义检索；
    3. **零上下文污染**：子代理启动时 `fork_context = false`，仅按需从 `ai-memory` 检索或读取任务规格，杜绝数万 Token 历史杂质污染；
    4. **跨对话框永久记忆**：通过 `pinned` 规则页面与 `memory_handoff_*` 机制，任何新建的会话或新对话框均可瞬时自召回全部协作成果，绝不遗忘。
  - **`output/` 目录的彻底收敛守则**：
    - 严禁再向 `output/` 目录写入多代理协作文本、JSON 或 Markdown 过程报告；
    - `output/` 仅用于承载**真实的无状态物理二进制产物**（如 Playwright E2E 录屏/截图到 `output/playwright/<task_name>/`）以及受控的研发一次性排查脚本；
    - `output/` 产物绝对禁止提交到 Git 仓库。

---

## 3. 子智能体生命周期感知、完成状态确认与零信任核验 (Lifecycle & Verification Protocol)

在分布式多智能体系统中，协同交互若缺乏生命周期监督，极易出现**子代理中途崩溃/网络中断（如 502/断网）、任务干了一半遗留半截状态、主代理死等或误把半拉子数据当成成功**的致命问题。为此，本项目推行“Codex 平台级会话看门狗 + ai-memory 原子入库 + 主代理三重零信任核验”防伪协议：

### 防御第一道：Codex 平台级生命周期看门狗 (`wait_agent` + 状态事件驱动)
1. **唯一会话绑定**：主代理通过 `spawn_agent` 派发任务时，Codex 底层生成全局唯一 `agent_id`。
2. **阻塞等待与超时看门狗**：主代理调用 `wait_agent(targets=[agent_id], timeout_ms=300000)` 进入挂起等待（默认上限通常为 3~5 分钟）。
3. **实时状态感知**：
   - **正常完成**：子代理执行完毕退出，Codex 底层 Runtime 立即向主代理推送 `completed` 状态通知；
   - **异常中断/网络故障（如 502 Bad Gateway / Connection Reset）**：Codex 底层捕获提供商异常，将子代理标记为 `failed`/`error` 并附带底层错误通知唤醒主代理；
   - **死循环/僵死超时**：若达到 `timeout_ms` 仍无响应，`wait_agent` 自动超时返回，主代理被强制唤醒，绝不会出现无限傻等！

### 防御第二道：ai-memory 交付报告原子入库与状态闭环机制
为了杜绝子代理中途遭遇网络中断或残留损坏半截数据，子智能体完成任务后必须通过 `memory_write_page` 写入 `reports/<task_id>.md`：
1. **原子事务写入**：`memory_write_page` 在后端执行完整的 SQL 事务写入，天然避免了物理文件写入中途被截断损坏的问题；
2. **报告结构化与完成状态闭环**：
   - 只有当所有业务动作、三库快照采样均 100% 成功闭环后，子代理才在报告中明确标明 `status: "SUCCESS"` 与对应元数据；
   - 若子代理自检失败或捕获到异常，则标明 `status: "FAIL"` 或 `status: "BLOCKED"`，并如实提供失败堆栈与诊断信息；
   - 报告规范结构见第 6 节标准模板。

### 防御第三道：主代理的三重“零信任”验收卡点 (Zero-Trust Gateways)
主代理从 `wait_agent` 唤醒后，**绝不单凭子代理对话里的“我已完成”一句话就认定通过**，必须在 ai-memory 认知总线上执行三重零信任核验：
1. **卡点 1（Runtime 状态核验）**：核验 Codex 会话状态是否为 `completed`；若为 `failed`/`timeout`，直接进入熔断处理；
2. **卡点 2（ai-memory 交付报告核验）**：主代理调用 `memory_read_page(path="reports/<task_id>.md", project="wagtailblog2", workspace="default")` 读取报告。**若页面不存在、内容不完整或其 `status` 字段非 `SUCCESS`，立即判定为任务异常中断/夭折，当次产出全部作废！**
3. **卡点 3（三库对账与红线核验）**：主代理核验报告中记录的真实三库快照，严格对账核心硬指标（例如：`MongoDB 物理删除调用数必须 == 0`、`MySQL 页面记录指针正确`、`Redis 防重锁无死锁并已释放`）。

### 异常熔断与接管策略
- **若发现子代理失联/超时/未写入成功报告**：主代理判定任务中断，记录异常日志；
- **自动重试与升级**：对于网络瞬断问题，主代理可重新派发一次干净任务；若连续 2 次失败，主代理终止委托，由 Sol 亲自接管排查或向用户汇报。

---

## 4. 任务派发铁律与标准流程

### 铁律一：零历史继承 (`fork_context = false`)
- **执行方式**：主代理调用 `spawn_agent` 时，**必须显式传递运行时参数 `fork_context=false`**：
  ```python
  spawn_agent(agent_type="gemini", fork_context=False, message="...")
  ```
- **核心价值**：
  - 子代理拥有 100% 干净的推理空间，不继承主会话数万 Token 的历史杂质与报错噪音；
  - 极大程度节省 Token 消耗，杜绝子代理因历史上下文而产生思维混淆。

### 铁律二：小步快跑、单流闭环（反 524 网关超时）
- **严禁打包长任务**：严禁一次性向子代理派发包含多个流程（如 Flow A~J 全量打包）的巨型长任务。长任务会导致子会话长挂数小时，必然触发反向代理（如 Cloudflare/Nginx）的 HTTP 524/504 网关超时熔断！
- **单流闭环原则**：每次派发**仅限 1 个独立 Flow**（例如单次仅派发 Flow A）；子代理在 5~10 分钟内完成操作、向 ai-memory 写入 `reports/<task_id>.md` 并在闭环后回传；
- **主代理步进推进**：主代理核验单流证据并登记通过后，再开启下一个 Flow 的派发。每个流程都有独立快照断点，中断随时可接续，零风险回溯。

### 铁律三：签发“五要素任务包”并归档 ai-memory
主代理向子代理派发任务时，先调用 `memory_write_page` 写入 `tasks/<task_id>.md`，并在派发 message 中指明任务五要素：
1. **【任务目标与 Task ID】**：本次执行的精确流程名称与 ID（例如：`task_id = "flow_a_20261014_1000"`）；
2. **【唯一隔离前缀】**：全局隔离的测试数据前缀（例如：`r3e_flow_a_<timestamp>`），杜绝污染历史业务数据；
3. **【工具与环境】**：指明使用的工具（如 `browser-skill`）与目标服务地址（如 `http://127.0.0.1:8000`）；
4. **【红线守则】**：零物理删除（MongoDB 删除调用必须为 0）、凭据脱敏、禁止直接执行 Git push 或合流；
5. **【交付目标】**：明确要求子代理完成后将结构化交付报告写入 ai-memory 的 `reports/<task_id>.md`。

---

## 5. 标准派发与交互流程模板 (Sol -> Gemini)

主代理派发步骤：
1. 调用 `memory_write_page(project="wagtailblog2", workspace="default", path="tasks/flow_a_20261014.md", body="...")`；
2. 调用 `spawn_agent(agent_type="gemini", fork_context=false, message="...")`。

派发 message 示例文本：

```markdown
### 任务派发：Flow A 纯草稿生命周期验证

#### 1. 执行目标与范围
- 任务 ID：flow_a_20261014_1200
- 完整任务规格已存入 ai-memory：tasks/flow_a_20261014.md
- 目标：接管浏览器完成后台新建草稿 -> 修改正文 -> 未发布直接删除草稿全流程真实验证。

#### 2. 隔离前缀与数据安全
- 测试页面唯一前缀：r3e_flow_a_<timestamp>
- 严格禁止查询或修改任何非本前缀的历史数据。

#### 3. 真实动作执行指引 (通过 browser-skill)
1. 确认接管 Edge 浏览器当前已登录 root 的管理后台；
2. 导航至专题指南页面列表，点击新建指南文章；
3. 录入带唯一前缀的标题与正文（Markdown），点击“保存草稿”；
4. 验证成功提示，并修改正文后再次保存草稿；
5. 返回列表页或在编辑页直接点击“删除”，确认删除草稿。

#### 4. 三库快照采样 (只读探测)
- MySQL：确认页面记录已安全移出（或指针指向）；
- MongoDB：核验正文版本历史标记为 DeletionIntent，物理删除调用保持为 0；
- Redis：核验防重 Key（ID-01~06）状态流转并已安全释放。

#### 5. 交付路径
- 完成后将结构化交付报告写入 ai-memory：`reports/flow_a_20261014_1200.md`；
- 严禁向 output/ 目录写入文本或 JSON 文件（物理截图可按需写入 output/playwright/flow_a/）。
```

---

## 6. 标准报告写入模板 (Gemini 写入 ai-memory `reports/<task_id>.md`)

子代理执行完毕后，调用 `memory_write_page(project="wagtailblog2", workspace="default", path="reports/<task_id>.md", body="...")` 写入标准 Markdown 报告：

```markdown
# 任务交付报告: flow_a_20261014_1200

## 元数据与状态
- **status**: SUCCESS
- **agent_type**: gemini
- **task_id**: flow_a_20261014_1200
- **duration_seconds**: 38
- **test_prefix**: r3e_flow_a_20261014_120000
- **exit_code**: 0

## 1. 真实操作与交互证据
- **页面操作结论**：全流程 100% 成功执行，无异常阻断。
- **关键状态码**：HTTP 200，操作提示信息正确。
- **物理截图（如需）**：output/playwright/flow_a/final_success.png

## 2. 三库快照核验结论
| 存储介质 | 预期要求 | 实际观测值 | 结论 |
| :--- | :--- | :--- | :--- |
| **MySQL 页面指针** | 页面记录安全移出 | 对应页面 ID 已不存在 | PASS |
| **MongoDB 正文记录** | 标记为 DeletionIntent，版本保留 | 版本记录存在，hash 匹配 | PASS |
| **MongoDB 物理删除调用** | 严格保持为 0 | delete_one/delete_many = 0 | PASS |
| **Redis 防重状态** | processing -> succeeded/释放 | 状态流转符合预期，无死锁 | PASS |

## 3. 异常与风险审计
- 无异常，未发现阻断项。
```

子代理在自身会话结束时，向主代理返回简洁卡片告知报告已入库：
> “任务 flow_a_20261014_1200 已 100% 闭环执行完毕，验收报告已原子写入 ai-memory: `reports/flow_a_20261014_1200.md`，状态为 SUCCESS，三库对账已通过，MongoDB 物理删除为 0。”

---

## 7. 主代理闭环验收与本地开发工作流 (ai-memory 驱动)

1. **查阅 ai-memory 交付报告**：主代理 `Sol` 调用 `memory_read_page(path="reports/<task_id>.md", project="wagtailblog2", workspace="default")` 读取报告，核实验证事实以及 `MongoDB 物理删除 = 0` 红线；
2. **分支流程流转**：
   - **若 status == SUCCESS**：登记通过，推进下一个流程；
   - **若 status == FAIL / 异常中断**：主代理 `Sol` 介入进行根因分析（RCA），在后端实施最小 TDD 修复，然后重新派发复测；
3. **关闭子代理 (`close_agent`)**：确认任务完成后，主代理必须调用 `close_agent(target=...)` 释放子代理，不浪费并发配额；
4. **阶段里程碑本地提交**：当一个完整测试阶段通过后，主代理在特性分支执行本地 Git 原子提交，记录规范中文 Conventional Commit。

---

## 8. 新会话防遗忘与开箱自召回规程 (Session Continuity & Self-Routing)

为了保证在开启任何新对话框、切换新窗口或会话因不可抗力中断重连后，AI 智能体之间**“绝不遗忘、无缝接力”**，必须严格遵循以下三层持久化规程：

### 1. 架构规则与底线约束置顶 (`_rules/` + `pinned: true`)
- 本项目的核心协作规则（`_rules/agent_collaboration.md`）与安全红线（`_rules/core_safety.md`）必须通过 `memory_write_page` 写入并设置 `pinned: true`；
- 任何新对话框启动时，主智能体必须先调用：
  ```python
  memory_briefing(project="wagtailblog2", workspace="default", settled_first=True)
  ```
  瞬时将置顶的架构规则、高频最近页面和项目状态装载至上下文，开箱即知全貌。

### 2. 跨会话任务交接棒 (`memory_handoff_begin` / `memory_handoff_accept`)
- 当一个工作阶段结束或即将切换到新对话框时，当前会话的主智能体必须调用：
  ```python
  memory_handoff_begin(
      project="wagtailblog2",
      workspace="default",
      shared=True,
      summary="简明概述当前达到的阶段与分支状态",
      next_steps=["下一步要进行的具体 Flow 或代码工作"],
      open_questions=["待确认的边界问题"]
  )
  ```
- 新对话框打开时，智能体通过 `memory_handoff_accept`（或系统级 SessionStart 钩子）即刻消费该交接棒，实现 100% 认知无缝衔接。

### 3. 开发排查与决策实时检索 (`memory_query`)
- 在开展具体子系统研发、排查疑难 Bug 或设计新模块前，智能体必须先调用：
  ```python
  memory_query(query="...", project="wagtailblog2", workspace="default")
  ```
  检索历史决策（`decisions/`）、踩坑记录（`gotchas/`）和先验经验，杜绝重复踩坑。
