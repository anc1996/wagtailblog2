# Mixin 编码恢复与回归记录

> 日期：2026-10-06｜范围：源码完整性、Alias 同步边界及隔离回归｜当前状态：v3 候选 461 项业务回归、87 项工具测试、静态门禁和独立提交闭包审核 PASS；本地提交前快照，实际提交以 Git 为准

## 1. 授权与安全边界

- 用户要求查阅 `refactor/frontend-assets-baseline` 历史，并授权测试通过后本地提交；不推送、不连接生产。
- 保留当前 `feat/external-content-guides` 分支及全部既有修改，不切分支、不整体回退。
- Page `711/715` 不属于本轮写入或恢复范围；不猜测其他数据库。
- 测试只使用受保护的本机专用 MySQL/Mongo 端口 `13306/13307`，凭据通过受控文件注入，禁止回显。
- 本轮源码恢复不包含数据库迁移、历史正文修复或服务重启。

## 2. 历史核查结论（L2）

| 对象 | 核查事实 | 使用边界 |
|---|---|---|
| `refactor/frontend-assets-baseline` | 分支顶端为 `359ffc2`；其文件树没有 `wagtailblog3/apps/external_content/mixins.py`，该路径的分支日志为空 | 不能作为本文件损伤前副本 |
| `98a9681` | 当前功能分支最后一次提交该文件的版本，严格 UTF-8 解码成功，无 U+FFFD | 可追溯的早期基线，不含近期复制、锁和 Alias 修复 |
| `mixins.py.orig` | 可解析为 Python，但含 85 个 U+FFFD | 只作调查线索，不作可信恢复文件 |
| 本轮修改前 `mixins.py` | 严格 UTF-8 解码和 AST 解析成功，无 U+FFFD；仍有破损标点、残缺注释和失去缩进的 docstring | 不宣称已恢复为损伤前精确副本 |

可追溯基线 SHA-256：

`8ec5020fbe26577bf2a6d90a5b97af58e4cc9d5023865a6096c36bd0b3ae3351`

本轮修改前工作副本 SHA-256：

`51c1f6a5838d05afec6d3e69fb821e11ea1039f7aab9f3e0cd802c53e7bffc60`

修改前副本保留在 `output/mixins-recovery-2026-10-06/mixins.before.py`；该目录只用于本机证据，不提交。

## 3. 恢复策略与验证边界

不使用旧版本整文件覆盖。依托历史可读文案和当前代码语义，修复可以确定的损伤注释、docstring 和标点；为上下文管理器和复制入口补充类型标注。

本轮文案恢复前后 AST 对比：去除 docstring、函数类型标注及新增 `typing.Iterator` 导入后完全一致。该结果证明文案恢复没有改变现有可执行逻辑，不证明前一轮所有业务修改均正确。

前一轮待复核的业务改动继续保留：

- `update_aliases()` 保存并恢复当前递归层的 `alias_update_source_page_id`。
- `save()` 仅对该同步源的直属 Alias 放行暂用源 Revision 的中间态；源页本身仍必须校验发布 Revision。
- 普通 Alias 保存仍被拒绝；源指针、六键身份和正文哈希仍由专用生命周期校验。
- 删除异常回归测试用额外 savepoint 保护 `TestCase` 的外层事务。

## 4. 最初批次执行步骤与准出（历史）

本节保留最初失败批次及责任边界；当前准出状态以第 9 节为准，不将早期“待执行”或 FAIL 作为最新结论。

| 步骤 | 责任 | 当前证据 |
|---|---|---|
| 历史和差异核对、修改前副本保留 | 主线程 | 已完成；见第 2 节 |
| 文案恢复及 AST/UTF-8 检查 | 主线程 | 已完成；可执行逻辑保持不变 |
| Alias 身份、递归上下文及事务独立复核 | Sol 子代理 | 首轮 FAIL；见第 6 节 |
| 复制和批量删除定向测试 | Gemini 子代理 | 基线 21 项通过，退出码 0 |
| 完整 `external_content.tests` + `guides.tests` | Gemini 子代理 | 基线 346 项，10 个错误，退出码 1；不能宣称通过 |
| 静态门禁及提交依赖闭包检查 | 主线程 | 待执行 |
| 本地提交 | 主线程 | 仅在测试、独立复核及提交范围检查通过后执行 |

测试命令使用 WSL2/Conda 环境：

```bash
python tools/run_external_content_tests.py \
  external_content.tests.test_copy \
  external_content.tests.test_deletion_bulk_regression --keepdb -v 1

python tools/run_external_content_tests.py --keepdb -v 1
```

本轮证据路径：

- `output/test-results/mixins-recovery-2026-10-06-targeted.log`
- `output/test-results/mixins-recovery-2026-10-06-full.log`

其中 `full.log` 已被后处理为失败概要，不再作为完整原始日志使用。后续复跑另存原始输出，禁止覆盖为概要；报告与原始日志分开保存。

测试数量与退出码以本轮实际输出为准，不能沿用历史 `345`、`344` 或定向测试数字。

## 5. 提交与回滚原则

- 本地提交只包括明确审阅、实际验证的完整依赖集，不使用 `git add .`。
- `mixins.py` 引用了尚未跟踪的生命周期模块，不能只提交该文件形成不可运行的快照；先核对依赖闭包。
- 代理配置、临时补丁、损坏副本、浏览器证据和无关改动不混入本次业务提交。
- 未通过复核或回归时保留工作区和修改前副本，记录阻断；不强行提交。
- 有本地提交不等于专项完成；真实登录态验收和故障恢复矩阵仍需各自证据。历史数据审计只在将来另行要求时执行，不是本轮准出条件。
- 回滚优先针对本次提交和明确文件，禁止通过整体重置丢弃他人修改。

## 6. 首轮复核与回归发现

首轮 Sol 只读复核为 FAIL，必须先处理，不能以定向测试通过替代：

1. 直接 `CreatePageAliasAction.execute()` 插入页面与外置状态初始化没有共同事务，初始化失败可能留下 SQL 页面、树和日志。
2. 仅凭 `in_alias` 布尔量放行过宽；缺少来源 ID 时 `None == None` 可能豁免非 Alias 页面。
3. Wagtail 原生支持 Alias-of-Alias 递归；当前外置状态校验强制要求来源有本地正式 Revision，与别名不建本地 Revision 的契约冲突。
4. `update_aliases()` 的 `revision/_content` 未绑定合法来源的正式快照，可能形成外来输入自洽但来源错误。

全套基线另发现测试夹具问题：`GuideLifecycleTestCase` 在 `TestCase` 的事务内执行 MySQL DDL；动态 `LifecycleTestPage` 表缺失进一步触发级联操作错误。该失败须修复并重跑，不能改成跳过用例。

后续步骤：

- Gemini：新增独立 Alias 契约回归文件，覆盖错误窗口、外来输入、链式别名和 Action 失败回滚。
- Gemini：单独修复动态测试模型表的生命周期；只改测试夹具，不修改业务契约。
- 主线程：先验证回归确实暴露缺口，再实施最小修复，测试与业务编辑串行。
- 独立代理：修复后再次审查，再单线程执行定向与全套回归；全部通过前不提交。

这些修改仅针对本机测试仓库源码和隔离夹具；不修改 `711/715`，不清理 Mongo 版本，不进行生产操作。

### Alias 最小修复契约与执行边界

1. `prepare_alias_update(source_page, revision=None, content=None)` 在活跃事务中返回数据库重读的根源正式 Revision 和独立完整 content 快照；不修复状态，不接受草稿、陈旧 Revision、外来快照或非 Manifest 元数据篡改。
2. A → B → C 保留直属 `alias_of/source_page_id`；B/C 不建本地 Revision，`source_revision_id` 记录根 A 正式 Revision，Manifest 保留根身份。
3. `update_aliases` 在同一事务内完成 canonical 校验、直属状态同步和原生递归；所有上下文精确恢复存在性和值。末级失败回滚根发布及所有已同步层。
4. 直接 `CreatePageAliasAction` 的插入、树计数、日志与各递归层状态初始化在共同事务中执行。授权窗口必须有非空且匹配的直属来源身份，不能只依赖布尔开关。
5. Alias 来源链及递归目标闭包使用 NOWAIT 围栏，获锁后复核拓扑；仅明确的 MySQL NOWAIT 错误转为领域阻断，其余数据库异常透传。不保证原生树/M2M 的所有 SQL 绝对无死锁，不在原事务重试。
6. 首次未来排期仅在受控发布 Action、未上线且无正式指针、Revision 归属和未来时间均有效时跳过正式 Alias 同步；到期发布进入正常严格门禁。

执行顺序：完整契约红测试 → 文件边界内实现 → 定向绿测试 → 隔离工具安全测试 → 全套单线程回归 → 独立复核 → 完整依赖闭包本地提交。

回滚边界：MySQL 事务失败不得留下 Page/State/树/日志的部分推进；本次恢复不物理删除 Mongo 不可变版本，遗留无引用版本交由既有安全期和引用核验流程处理。

责任边界：主线程 `mixins.py` 及排期回归；Sol `copying.py/coordinator.py/locking.py`；Gemini 契约测试、DDL 夹具和隔离工具安全；Grok 独立只读复核。数据库测试单线程，测试运行期间冻结业务文件。

### 2026-10-06 契约 RED 与实施进度

- 单线程真实 MySQL/Mongo Alias 契约测试：8 项，6 个失败、2 个错误，退出码 1，耗时 6.598 秒。原始日志为 `output/test-results/alias-contract-red-2026-10-06.raw.log`，不覆盖、不将原始日志改写为概要。
- 六项失败验证了错误授权窗口、外来 Revision/完整快照、非 Manifest 元数据篡改和直接 Action 初始化失败残留；两项链式用例在 B → C 创建时报告正式指针缺失，不能将其误记为通过。
- `test_alias_contract.py` 曾被误删；当前文件来自先前补丁内容的重建，不是损伤前可信副本。已重新运行上述 RED，而非依赖旧报告。
- 主线程已收窄 `mixins.py` 的授权窗口，接入 canonical 快照准备接口；Sol 正在实现其依赖及链式事务围栏。接口配套完成前不执行 GREEN，不宣称业务补丁已验证。
- Gemini 分别负责动态 DDL 夹具和原生删除事务测试；隔离 Runner 的安全修复另行串行放行，避免文件交叉修改。
- 后续顺序保持：定向 GREEN → 原生删除故障 RED/GREEN → 隔离工具安全 → 完整双库回归 → 独立复核 → 审阅依赖闭包本地提交。真实浏览器验收仍单独登记，不以 Python 测试替代。

## 7. 静态门禁与环境差异

- 隔离凭据注入、Socket Guard 和配置硬断言后执行 Django `check`：退出码 0，无问题。基础配置打印的搜索节点不代表连接行为；隔离设置关闭搜索后端，守卫仍只允许 `13306/13307`。
- WSL2 CSS token 检查：退出码 0。
- Windows `check_codex_agents.py`：退出码 0，5 个目录模型通过；WSL2 同脚本退出码 1，因为该用户环境未登记 `custom` provider。这是两端配置差异，不能据此宣称 Windows 子代理不可用，也不擅自修改 WSL 全局配置。
- 全工作树 `git diff --check` 暂未通过：既有生命周期、搜索和历史文档差异包含行尾空白。先检查拟提交范围，不为本轮恢复而清理无关搜索改动。

## 8. 接续执行与删除修复契约（2026-10-06）

当前仍未准出、未提交。参考分支不含目标文件的核查结论不变；沿用已有备份和最小补丁，不整文件覆盖。

| 工作 | 状态与责任 |
|---|---|
| Alias/canonical/排期/NOWAIT 补丁 | 主线程与 Sol 已实施；Grok 独立复核中，待真实回归 |
| 动态 MySQL DDL 夹具和隔离 Runner | Gemini 独占修复；收到停笔与安全证据后才使用 Runner |
| 原生删除故障、嵌套 scope 测试 | Gemini 补用例；主线程单进程执行 RED/GREEN |
| 删除最小补丁 | 主线程负责，先取得 RED；Sol 已只读评估边界 |
| 完整双库回归、门禁、依赖闭包提交 | 前述通过后执行；不等于真实浏览器验收 |

删除契约与回滚边界：

1. `post_delete` 的意图查询或保存失败必须传播原异常，不能把 SQL 删除误报成功；Mongo 不可变正文保留。
2. `safe_deletion_scope` 用独立集合承接本次窗口，结束时精确恢复原属性对象或属性缺席；不能用差集破坏外层重叠授权。
3. 原生 Collector 只在收集到外置模型时隔离上下文，进入时不预先授权所有待删 ID；成功登记或核验意图后才逐页加入窗口。
4. 已安装原生 Action 在 Collector 前写删除日志，Treebeard QuerySet 在 Collector 前更新 `numchild`。需覆盖入口外围事务，不能把 Collector 内部事务误当整条路径的事务。
5. wrapper 幂等注册，透传原生参数、返回值和数据库别名，不调用安全删除执行器造成重复删除；非 Page 的 Treebeard 模型不受 Page 专用包装影响。
6. 所有失败测试检查 Page/Revision/State/Intent、日志、树计数与线程上下文回滚；只操作测试夹具，Page `711/715` 不写入。

准出顺序：隔离工具安全 → Alias 定向 GREEN → 删除故障 RED/GREEN → NOWAIT 真实竞争 → 全套单进程回归 → 独立复核 → 静态门禁与精确提交。不删除记录或证据，不连接生产、不推送。

### 本轮接管与测试入口安全修复

- Runner 所有权已由 Gemini 交回主线程；单次同路径 Delete/Add 补丁曾被工具拒绝，没有形成实现，不计为已完成。
- 工具 RED：58 项，13 FAIL、1 ERROR、退出码 1，0.451 秒；证据 `output/test-results/runner-safety-red-sol-2026-10-06.raw.log`。它不使用 Django 测试库，不能替代业务回归。
- 主线程已实际重建 Runner，复用八键私有凭据契约：缺失失败关闭、逐键环境冲突拒绝后才统一注入、固定双库拓扑、错误码脱敏、初始化前守卫和单测试进程。旧 Runner 副本保存在 `output/mixins-recovery-2026-10-06/runner.before-safety.py`，不提交。
- Sol 安全复核又指出 FIFO 阻塞与路径编码异常边界；已补非阻塞打开、根目录打开纳入异常边界、编码及值异常脱敏，待行为回归和二次放行。
- Grok 已只读复核 Alias/首次排期/链式同步/NOWAIT：未发现阻断级缺陷；这不是数据库 GREEN，也不包含删除补丁。
- 主线程核对实际 DDL 夹具发现旧 TestCase 方法仍在事务内运行 DDL；Gemini 正在单文件修复，不能沿用此前“夹具已完成”的口头报告。
- 当前责任：主线程 Runner/helper 与复杂业务修复；GeminiTester 工具行为测试；GeminiDocs 指南 DDL 夹具；Sol 安全放行；Grok 业务独立审核。各写入范围隔离，所有数据库测试继续串行。
- 尚未本地提交、推送、重启应用或修改 `711/715`；没有连接生产。

### 测试数据范围澄清

用户确认隔离测试环境为空、不含历史 Page `711/715`。这两个 ID 仅用于描述旧环境故障，不是当前隔离库中的待修数据，也不是当前源码修复或生命周期回归的前置阻断。

- 当前只使用已明确的本机隔离 MySQL/Mongo 服务与测试创建的合成页面；不为寻找这两个 ID 查询其他数据库。
- 正文缺失、悬挂指针、已有删除意图、批量与父页级联回滚等故障通过新夹具复现，不恢复或写入历史页面。
- 非生产只读副本/脱敏导出只在将来另行要求审计历史数据时需要；本轮不索取该副本、不连接生产实时库。
- 单线程回归、安全入口审核和独立代码复核仍须完成；不能因测试库为空省略这些门禁。

### 2026-10-06 最新实测（接续批次）

| 批次 | 结果 | 原始证据 |
|---|---|---|
| 隔离凭据及 Runner 行为测试 | 63 项，0.604 秒，OK，退出码 0；无真实数据库操作 | `output/test-results/runner-safety-green-sol-2026-10-06.raw.log` |
| Alias、首次排期与复制原始回归 | 24 项，24.346 秒，1 FAIL / 7 ERROR，退出码 1；文件名中的 green 不代表结果 | `output/test-results/alias-copy-green-sol-2026-10-06.raw.log` |
| 新增快照编码 RED | 3 项，1 ERROR，退出码 1；真实暴露内存 datetime 编码拒绝 | `output/test-results/alias-snapshot-red-2026-10-06.raw.log` |
| 编码修复后 Alias、排期、复制及快照回归 | 27 项，22.592 秒，OK，退出码 0 | `output/test-results/alias-copy-after-encoding-2026-10-06.raw.log` |

修复依据是本机已安装 Wagtail 的 `Revision.content` 使用 `DjangoJSONEncoder`：内存 Revision 含日期对象，数据库 JSON 是日期字符串且采用毫秒精度。Alias 快照比较改为同一持久化编码，继续拒绝 NaN、无穷、任意对象及布尔/数值类型篡改，不使用 `default=str`。首次排期同时绑定 Revision 身份与完整快照；日期按相同编码精度核验。

RichText 断言改为检查原始 `.source`，仍比较明确正文字符串，不删除或跳过用例。Sol 正在独立复核本次编码边界，不将前述 27 项通过外推为整体完成。

### 原生删除 RED 与实施记录

- 原生删除及真实自动提交事务 RED：16 项，7.238 秒，13 FAIL / 2 ERROR，退出码 1；证据 `output/test-results/native-deletion-red-2026-10-06.raw.log`。
- 既有意图门禁 RED：5 个测试方法，4.923 秒，30 个子用例失败，退出码 1；证据 `output/test-results/native-deletion-intent-red-2026-10-06.raw.log`。禁止状态及其与 PENDING 混存均暴露了重新登记/推进漏洞。
- 主线程已落盘删除入口外围事务、Collector 空授权窗口、逐页意图核验、post_delete 原异常传播、嵌套上下文精确恢复补丁；已将登记门禁收窄为锁内全部意图检查，禁止状态不能被 PENDING 掩盖。尚未取得删除 GREEN，不宣称修复完成。
- Gemini 补充受控执行器输入身份与原对象标志恢复测试；主线程负责实现，数据库测试仅主线程单进程执行。Sol 继续只读审核 Alias，Grok 在删除定向通过后审核完整删除补丁。

### 接续检查点：排期边界与删除执行器

| 批次 | 实测结果 | 原始证据 |
|---|---|---|
| 删除外围事务修复后 | 16 项，7.515 秒，OK，退出码 0 | `output/test-results/native-deletion-after-scope-2026-10-06.raw.log` |
| 四层 NOWAIT 真实连接竞争 | 7 项，4.924 秒，OK，退出码 0 | `output/test-results/lifecycle-nowait-2026-10-06.raw.log` |
| 删除执行器扩展 RED | 13 方法，10.888 秒，36 子用例失败，退出码 1 | `output/test-results/deletion-executor-red-2026-10-06.raw.log` |
| 排期时间边界 RED | 5 项，1.950 秒，2 ERROR，退出码 1 | `output/test-results/alias-scheduling-time-red-final-2026-10-06.raw.log` |
| 排期、完整快照、Alias、复制 GREEN | 31 项，23.542 秒，OK，退出码 0 | `output/test-results/alias-scheduling-time-green-2026-10-06.raw.log` |

- Sol 指出首次排期按日期编码字符串比较会误拒等价时区或亚毫秒精度。RED 已复现持久化 `+08:00` 快照和 `000456` 微秒边界；仅日期等价比较改为双方 aware datetime → UTC → 截断毫秒。未来判断仍使用完整精度，完整快照、六键及 SHA-256 门禁不放宽，相邻毫秒不一致仍拒绝。
- 删除执行器已改为共同事务内登记、锁内全部意图检查、唯一 PENDING 与调用参数及具体模型绑定；flag 和上下文精确恢复。Collector 继续固化版本清单，不信任调用方空清单。
- Grok 静态复核未发现当前 default 单库删除补丁的新运行时阻断；指出 executor 冗余墓碑保存为 Minor、非 default 路由不在当前保证内。晚期故障测试应定位在真实删除返回后，Gemini 正在修正测试注入点，尚未取得执行器 GREEN。
- 下一步：删除故障与 wrapper GREEN → 取消意图/竞争门禁核验 → 全量双库回归 → 独立审核和静态门禁 → 精确本地提交。真实 BrowserSkill 验收和测试 Web 重载仍未执行，不能宣称专项整体完成。

### 取消意图契约与接续进度

- Sol 独立复核排期 UTC/毫秒补丁为 PASS，日期边界 P1 已关闭；完整生命周期仍待全量回归。
- 原生 wrapper/atomicity/transactions 首次接续回归：21 项，11.304 秒，1 ERROR，退出码 1；证据 `output/test-results/native-deletion-wrapper-green-2026-10-06.raw.log`。错误来自透传测试使用无有效主键的 Collector 夹具，不是业务删除成功；Gemini 已改用独立真实页面和 Collection。
- 取消意图 RED：7 项，4.718 秒，8 FAIL / 1 ERROR，退出码 1；证据 `output/test-results/deletion-cancellation-red-2026-10-06.raw.log`。真实 MySQL 双连接竞争复现预读后状态推进仍被错误取消。
- 最小契约：用数据库预读所得 UUID 匹配锁内目标，复核页面归属；仅 PENDING/FAILED 可取消，已有回收墓碑的 FAILED 拒绝；CANCELLED 只读幂等；其他状态（含过期或缺租约 CLAIMED、GC_ELIGIBLE、未知状态）全部拒绝。成功清理全部 claim 字段，保留正文、版本清单、回收审计与代次；其他活跃意图仍冻结页面。
- 当前实施计划：①收取删除 guards/wrapper 与新增墓碑 RED；②主线程最小修复取消契约并串行 GREEN；③独立二审和完整双库回归；④静态门禁、依赖闭包和精确本地提交。数据库回归期间冻结业务与测试文件，代理只读核对安全 Web 入口，不启动旧应用栈。
- 用户明确隔离库无 `711/715`；本轮只用合成页面，不再将历史副本交接列为源码准出前置条件。

### 取消意图 GREEN 与完整回归检查点

| 批次 | 实测结果 | 原始证据 |
|---|---|---|
| 删除 guards/wrapper/atomicity/transactions 与新增回收墓碑 RED | 35 方法，21.214 秒，只有墓碑用例 1 FAIL，退出码 1；其余 34 方法通过 | `output/test-results/deletion-guards-and-collected-red-2026-10-06.raw.log` |
| 取消意图及原生删除/后台入口 GREEN | 45 方法，26.556 秒，OK，退出码 0 | `output/test-results/deletion-cancellation-and-native-green-2026-10-06.raw.log` |

- 取消实现用预读 ORM 的 UUID 主键匹配锁内意图并复核页面归属，取消仅允许无回收墓碑的 PENDING/FAILED；CANCELLED 不写入，成功清理四个 claim 字段，保留版本、安全期、代次、重试及回收审计。
- 8 个取消测试覆盖字符串 UUID、完整状态矩阵、幂等、回收墓碑、缺页、写入后故障回滚及真实 MySQL 独立连接的预读竞争。日志中的故障注入 ERROR 是预期路径，准出按 unittest 结尾和退出码判断。
- 当前代码和测试文件冻结；主线程正在单进程执行 `external_content.tests`、`guides.tests`、`blog.tests.test_revision_body_errors`、`blog.tests.test_public_body_reads`、`search.tests.test_search_type_isolation`、`search.tests.test_search_sync_producer`、`search.tests.test_search_sync_delivery`。原始日志写入 `output/test-results/full-lifecycle-regression-2026-10-06.raw.log`，结果尚未取得。
- Sol 取消二审、Grok 精确提交依赖闭包审核仍在执行；未 stage、commit 或 push。
- Gemini 只读核对发现旧 `start_test_stack.sh` 默认使用 dev 配置并启动 Worker/Beat，不能证明双库隔离；现有 Web 日志也不是 G4 就绪证据。本轮不运行旧脚本、不猜测 Web schema、不初始化数据库，真实 BrowserSkill 验收仍未完成。

### 完整回归失败与修复分工

- 完整回归已结束：455 项，176.241 秒，1 FAIL / 5 ERROR，退出码 1。证据仍为 `output/test-results/full-lifecycle-regression-2026-10-06.raw.log`；上一检查点的“正在执行”已过期，不能据此宣称准出。
- 身份错误分支及旧 Alias 单元夹具：Sol 独占 `coordinator.py`、`tests/test_coordinator.py`、`tests/test_lifecycle_unit.py`。普通跨页 Manifest 必须先报告身份不匹配；真正 Alias 仍执行 canonical 来源验证。
- Workflow 仅批准时间的首次排期：主线程独占 `mixins.py`、`tests/test_lifecycle.py`，核对原生 Revision 与发布 Action 后最小修复，不直接放宽完整快照、时区和正文门禁。
- Codec roundtrip 错误日志泄露完整载荷：Gemini 独占 `codecs/streamfield.py`、`tests/test_codecs.py`，先补脱敏 RED，再由主线程验证并放行最小修复；这是提交阻断项。
- Grok 只读复核新增补丁。所有双库测试由主线程串行执行，测试期间暂停被测文件写入。
- 搜索六文件的现有未提交改动不属于本轮准出范围，保留工作树、单独登记风险；测试通过也不代表真实 Elasticsearch 投影验收。
- 再次确认：隔离库原本为空，无历史 `711/715`，本轮不索取历史副本、不跨库寻找这些页面。只用合成夹具复现故障；尚未提交、推送、重启 Web 或完成浏览器验收。

### 排期、身份与日志补丁接续验证

| 批次 | 实测结果 | 原始证据 |
|---|---|---|
| 仅批准时间的 Workflow 排期有效 RED | 3 项，2.490 秒，1 ERROR，退出码 1；测试事务夹具修正后仍复现真实误拒 | `output/test-results/workflow-approved-only-red-final-2026-10-06.raw.log` |
| Workflow、Alias 契约与快照 GREEN | 68 项，53.579 秒，OK，退出码 0 | `output/test-results/workflow-approved-only-green-2026-10-06.raw.log` |
| Codec 日志脱敏 RED | 2 项，0.045 秒，2 FAIL，退出码 1；符合预期，未回显测试正文标记 | `output/test-results/codec-log-red-2026-10-06.raw.log` |
| Codec、Coordinator 与生命周期 unit GREEN | 58 项，0.835 秒，OK，退出码 0 | `output/test-results/codec-coordinator-green-2026-10-06.raw.log` |
| 隔离工具最终行为检查 | 87 项，0.323 秒，OK，退出码 0；不操作真实数据库 | `output/test-results/runner-safety-final-2026-10-06.raw.log` |
| 初始化与隔离配置预检 | 退出码 0；凭据校验和网络守卫先于 Django 初始化，不执行测试 | `output/test-results/isolation-preflight-final-2026-10-06.raw.log` |
| CSS token 最终检查 | 退出码 0，bridge 和 3 个 admin 样式通过 | `output/test-results/css-tokens-final-2026-10-06.raw.log` |
| 工作树完整生命周期复跑 | 462 项，175.622 秒，OK，退出码 0；旧 455 项失败记录保留 | `output/test-results/full-lifecycle-regression-final-2026-10-06.raw.log` |
| 隔离环境 Django check | 无问题，退出码 0；凭据、守卫及设置校验后执行 | `output/test-results/django-check-final-2026-10-06.raw.log` |
| 拟生命周期范围差异检查 | `git diff --check` 退出码 0；整个工作树退出码 2，含未纳入的搜索与旧文档空白 | `output/test-results/worktree-diffcheck-final-2026-10-06.raw.log` |

- Workflow 补丁不改不可变 Revision 快照。仅在快照没有 `go_live_at` 时使用受控 Action 已从锁内批准时间设置的运行时日期，继续核验 aware datetime、UTC 毫秒一致性、Revision 归属、完整快照和无正式指针状态；空、错毫秒、naive 或篡改批准时间全部拒绝。
- 普通跨页先报 Manifest 身份不匹配，不尝试 Alias 来源解析或正文 IO；真正 Alias 继续核验持久化来源链、State、正式 Revision 和 canonical Manifest。旧单元夹具更新为当前 Action 围栏契约，不用布尔标志冒充正式来源。
- Codec 仅把往返失败分支中的完整载荷日志替换成固定错误码，保持比较和 Fail-Closed 不变。
- Sol 与 Gemini 已交回上述文件并停笔。主线程单进程完整复跑已结束，取得 462 项通过；原始日志另存，不覆盖 455 项失败证据。日志中的预期故障注入及既有 `LocMemCache.delete_pattern` 异常不等于 unittest 失败，也不在本轮修复范围。
- 上述完整复跑使用当前整个工作树，包含未纳入提交的搜索改动。提交前另验证精确候选快照，不能将工作树通过冒充提交依赖闭包通过；审核报告、候选快照和本地提交结果随后登记。
- 搜索六文件保留工作区但排除本次生命周期提交：错误分类/捕获边界、游标和聚合搜索指南覆盖仍需单独闭环，不能宣称搜索功能准出。安全 Web-only 入口及真实 BrowserSkill 验收仍未完成。

### 二审阻断项与修复契约

- Grok 二审：Workflow approved-only 排期、Coordinator 跨页身份与原子加载静态 PASS；Codec 局部日志补丁通过，但两个 Important 未关闭，禁止提交。
- I1：递归删除任意 `null` 键会掩盖 Wagtail 丢弃的未知属性。主线程独占 Codec 及其测试，改为按 StreamBlock/StructBlock/ListBlock schema 比较；仅允许已声明且默认持久化值为 `null` 的缺省字段补齐。原载荷、摘要、显式 null、包装层键及稳定 ID 均不改写。
- I2：未知输入块 type 被 encode 异常回显，Coordinator 在首字段产生新版本后记录原异常文本。主线程处理 Codec 固定索引消息，Sol 独占 Coordinator 及其测试，警告不得带原异常、正文或输入字段；保持异常传播，未引用版本留待异步 GC，不同步删除。
- Codec 有效 RED：25 方法、0.085 秒、7 个子用例失败、退出码 1；证据 `output/test-results/codec-null-and-encode-red-2026-10-06.raw.log`，复现顶层及嵌套未知 null 静默丢失、未知 type 回显。
- Codec 定向 GREEN：25 方法、0.201 秒、OK、退出码 0；证据 `output/test-results/codec-null-and-encode-green-2026-10-06.raw.log`。尚需 Coordinator RED/GREEN、独立复核、完整回归和精确候选快照，不以此定向结果宣称专项完成。

### 日志与 Schema 二审闭环（接续批次）

| 批次 | 结果 | 原始证据 |
|---|---|---|
| Coordinator 部分写入日志有效 RED | 2 项、0.030 秒、6 个子用例失败、退出码 1 | `output/test-results/coordinator-log-red-2026-10-06.raw.log` |
| Coordinator 日志修复 GREEN | 2 项、0.009 秒、OK、退出码 0 | `output/test-results/coordinator-log-green-2026-10-06.raw.log` |
| Codec、Coordinator、生命周期 unit 整合 | 66 项、1.114 秒、OK、退出码 0 | `output/test-results/codec-coordinator-review-green-2026-10-06.raw.log` |

- Grok 已二审 I1 与 encode 脱敏：PASS，无已确认 Critical/Important；允许当前 Guide schema 的合法缺省值，未知 null 键继续拒绝。非阻断 Minor 是隐藏异常 context 与自定义叶子容器类型边界；已将 Codec docstring 收窄为抑制标准 traceback 展示，不宣称物理清除隐藏 context。
- Sol 已修复 Coordinator warning：仅固定错误码、安全整数页面 ID 或 `None`、新版本计数；不带异常对象、正文、版本 ID 或 traceback。原异常继续传播，保留新版本账本、不返回部分 Manifest、不执行同步 GC。两个测试分别使用真实 Codec 和存储边界故障，检查日志模板、参数、格式化输出及异常渠道。
- Grok 已完成 I2 独立只读二审：PASS，原日志阻断可关闭，无未解决 Critical/Important；此前日期、取消意图及 Alias 事务审核结论保留。静态审核不代替精确候选完整回归。
- 本批次没有修改历史页面、重启服务或连接生产。精确候选完整回归尚待，不以 66 项定向 GREEN 代替提交准出。

## 9. 精确候选最终验证（2026-10-06）

### 范围、来源与冻结边界

- 基线为当前 `feat/external-content-guides` 分支 HEAD `01df6f832ef392d1f7d44d013f4170abea77f728`，未切分支或整体重置。
- 候选为 HEAD 完整归档加明确的 55 个源码/工具文件和 14 份活跃专项资料，共 69 项；不使用混合工作树的 462 项通过冒充提交证据。
- 最终被测源码位于 `output/lifecycle-commit-candidate-2026-10-06-v3/`；初始候选清单为 `output/lifecycle-candidate-manifest-2026-10-06-v3.json`。测试后 55 项源码/工具继续冻结，文档仅补充实际结果，最终文档另登记哈希。
- v2 已取得 461 项通过；提交检查发现未跟踪文件的行尾/末尾空白后，仅清理三份 Python 文件及两份历史资料。三份 Python 文件归一化文本一致；v3 重新运行完整回归，最终以 v3 字节为准。失败候选和平台错误产物保留，不覆盖旧证据。
- 搜索六文件增量、代理配置、AGENTS、临时补丁、损伤副本、归档资料及 output 证据均不纳入本次提交；提交迁移文件不代表授权执行迁移或 bootstrap。

### 新鲜证据

| 检查 | 实测结果 | 原始证据 |
|---|---|---|
| 最终候选完整生命周期回归 | 461 项，176.959 秒，OK，退出码 0 | `output/test-results/candidate-v3-lifecycle-regression-2026-10-06.raw.log` |
| 最终候选隔离工具行为测试 | 87 项，0.359 秒，OK，退出码 0；容器、授权和网络动作均为测试替身 | `output/test-results/candidate-v3-tools-regression-2026-10-06.raw.log` |
| 69 项 SHA-256、UTF-8、Python AST、实际测试凭据扫描和隔离 Django check | 通过，退出码 0；初始化前安装网络守卫 | `output/test-results/candidate-v3-static-and-django-check-2026-10-06.raw.log` |
| CSS token | 通过，退出码 0 | `output/test-results/candidate-v3-css-tokens-2026-10-06.raw.log` |
| 候选 Runner dry-run | 通过，退出码 0；未执行测试或迁移 | `output/test-results/candidate-v3-isolation-preflight-2026-10-06.raw.log` |
| v2 精确候选回归（保留追溯） | 461 项，177.418 秒，OK，退出码 0；不替代最终 v3 | `output/test-results/candidate-lifecycle-regression-2026-10-06.raw.log` |

完整回归从候选 cwd 使用候选 Runner，WSL2/Conda Python 3.13 单进程执行：

```bash
python tools/run_external_content_tests.py \
  external_content.tests guides.tests \
  blog.tests.test_revision_body_errors blog.tests.test_public_body_reads \
  search.tests.test_search_sync_producer search.tests.test_search_sync_delivery \
  --keepdb -v 1

python -m unittest \
  tools.tests.test_external_content_bootstrap \
  tools.tests.test_external_content_credentials \
  tools.tests.test_external_content_test_isolation -v
```

真实双库范围仅为本机专用 MySQL `127.0.0.1:13306/test_g4_external_content` 和 Mongo `127.0.0.1:13307/g4_external_content_tests`；凭据由受控入口加载，不回显。MySQL 条件唯一约束警告、预期故障注入 ERROR 以及既有 `LocMemCache.delete_pattern` 日志保留，最终 unittest 为 OK；没有借机修改无关缓存功能。日志中的 `/admin/pages/715/edit/` 是请求夹具，不代表读取或修复历史 Page 715。

### 当前准出与下一步

I1/I2 二审及 Grok 最终候选闭包审核均已 PASS，无未解决 Critical/Important。Grok 实际核验了 69 项白名单、HEAD 归档闭包、55 项源码/工具哈希、纯空白清理及搜索六增量排除；主线程在最终文档登记、秘密检查和精确 staged diffcheck 全部通过后执行已授权的本地提交。不预写提交哈希，实际结果以 Git 记录和交付消息为准；不推送、不连接生产、不修改历史数据。

最终文档收口候选为 `output/lifecycle-commit-candidate-2026-10-06-v4/`，清单为 `output/lifecycle-candidate-manifest-2026-10-06-v4.json`；55 项代码/工具必须与已审核且实测通过的 v3 完全一致，仅 14 份资料允许登记更新。v3 原候选、清单及日志保留，不覆盖；本文件为提交前快照，提交结果另由实际 Git 记录与忽略目录中的交付证据登记。

源码准出不等于专项整体完成。后续仍需安全 Web-only 入口与基础库 schema/页面树/站点/账号就绪、待审搜索六文件与真实 ES 投影，以及 MEDG/Edge 插件 BrowserSkill 真实用户生命周期验收；不能直接运行旧 Web/Worker/Beat 启动脚本，不能猜库或自动 migrate。
