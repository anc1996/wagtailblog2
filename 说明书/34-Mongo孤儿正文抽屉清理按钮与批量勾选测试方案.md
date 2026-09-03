# Mongo 孤儿正文抽屉清理按钮与批量勾选测试方案

> 日期：2026-09-03
> 目标：彻底修复抽屉内“已确认无追溯价值，安全清理”按钮显示，以及表格第一列批量 Checkbox 勾选交互，并通过 BrowserSkill 实地验证
> 模型基线：全流程及所有子代理统一使用 gemini-3.8-flash-high

---

## 1. 缺陷根因与现状分析

### 1.1 缺陷现象
- **缺陷 1（抽屉按钮缺失）**：在 `/admin/reports/mongo-orphans/` 点击【查看正文】后，弹出的底层正文审计抽屉底部只有【关闭预览】，未显示【已确认无追溯价值，安全清理】按钮；
- **缺陷 2（批量无法打勾）**：列表第一列每行均显示横杠 `-`，表头的全选复选框打勾后无法选中任何行，批量工具条未触发。

### 1.2 根因定位
1. **服务层契约缺陷**：在 `MongoOrphanService.scan_orphans()` 中，候选条目的 `can_delete` 字段写死为 `category == "orphan_candidate"`。而当前测试库与真实业务中，大量的历史快照孤儿属于 `referenced_missing_page`。导致扫描返回的候选对象中 `can_delete` 全为 `False`；
2. **模板条件判断走入死分支**：模板 `mongo_orphans.html` 中使用 `{% if item.can_delete %}` 判断是否渲染复选框。因为后端返回全为 `False`，模板全部落入 `{% else %}<span>-</span>{% endif %}` 分支，页面直接渲染成横杠 `-`，导致 Checkbox 元素根本不存在；
3. **前端预览抽屉按钮唤醒条件**：前端 JS 虽有文本切换代码，但必须确保不管行内 `can_delete` 初始值如何，在打开预览抽屉、异步拉取到带有合法 page_id 且非活体保护的文档时，底部按钮必须强制唤醒（`display: inline-flex !important`）并渲染清晰的红色高危警示样式。

---

## 2. 详细修复方案

### 2.1 后端服务层修正 (`mongo_orphan.py`)
- 凡是属于 `orphan_candidate`（完全死孤儿）或 `referenced_missing_page`（页面已在 MySQL 物理删除，仅历史修订快照残留的孤儿），在扫描结果中均标记为可受控清理候选（`can_delete = True`）；
- 只有真实存活页面 `referenced_page` 与异步删除处理中的页面 `referenced_pending` 坚决保持 `can_delete = False`。

### 2.2 前端模板与样式修复 (`mongo_orphans.html` & `mongo-orphans.css`)
- 确保第一列为所有可清理候选渲染标准复选框 `<input type="checkbox" class="orphan-item-check" ...>`；
- 批量工具栏增加平滑显示与计数样式；
- 抽屉底部按钮增加高对比度样式。

### 2.3 交互脚本精细化修正 (`mongo-orphans.js`)
- 点击【查看正文】展开抽屉后：
  - 若分类为 `referenced_missing_page`：按钮文本为 `已确认无追溯价值，安全清理`；
  - 若分类为 `orphan_candidate`：按钮文本为 `以此为据安全清理`；
  - 强制设置可见性与样式，确保用户百分之百能看到并点击；
- 复选框联动：点击行复选框或表头全选，准确统计数量并平滑滑出批量操作条。

---

## 3. 验证步骤与验收门禁

1. **自动化测试**：`test_mongo_orphan_cleanup.py` 全部通过；
2. **静态文件更新**：执行 `collectstatic --noinput`；
3. **测试栈服务重启**：重启 8080 端口与 Worker/Beat；
4. **BrowserSkill 实地演练**：
   - 浏览器打开 `/admin/reports/mongo-orphans/`；
   - 验证表格第一列全部呈现 Checkbox，勾选后批量工具条浮现；
   - 点击任一历史快照的【查看正文】，验证抽屉右下角清晰呈现【已确认无追溯价值，安全清理】红色按钮；
   - 截图保存至 `output/playwright/mongo-orphans/` 作为确凿证据。


---

## 实施与自动化验证记录（2026-09-03）

### 1. 实际修复与核对文件
- `wagtailblog3/apps/blog/services/mongo_orphan.py`：
  - 将 `scan_orphans` 与 `get_orphan_body_preview` 的 `can_delete` 判定修正为 `category in {"orphan_candidate", "referenced_missing_page"}`；
  - 调整 `delete_orphan_document` 防护边界：存活页面引用与删除中任务继续由顶部 Fencing Token 绝对熔断阻断；允许完全孤儿以及主页面已在 MySQL 物理删除的历史快照残留正文受控清理；
- `wagtailblog3/templates/wagtailadmin/reports/mongo_orphans.html`：
  - 第一列复选框已向 `referenced_missing_page` 正确开放；
  - 批量工具条与全选框文案对齐为“可清理孤儿条目”；
- `wagtailblog3/static/blog/js/mongo-orphans.js`：
  - 抽屉异步加载预览数据后，动态激活 `#btn-preview-delete`，文本为【已确认无追溯价值，安全清理】（带危险操作红色高亮视觉）；
- `wagtailblog3/apps/blog/tests/test_mongo_orphan_cleanup.py`：
  - 补充针对历史快照残留文档原子清理的单元测试用例，全套 23 项测试保持全绿通过。

### 2. BrowserSkill (bsk) 真实浏览器自动化验收证据
- 验收环境：WSL2 测试环境 `192.168.20.5:8080`
- 自动化工具：`bsk` 驱动本地 Chromium 浏览器
- 产物目录：`output/playwright/mongo-orphans/`（遵循 Playwright 产物隔离规范）
  1. `output/playwright/mongo-orphans/01_batch_selection.png`：首列全选复选框已激活勾选，9 项历史快照条目均被打勾，顶部浮动批量工具条“已选中 9 个可清理孤儿条目”与【批量安全清理选中文档】红底按钮正常呈现；第 4 行 `blocked_unknown` 保持 `-` 禁用保护；
  2. `output/playwright/mongo-orphans/02_drawer_preview_button.png`：点击第一行【查看正文】后，右侧抽屉完整渲染文章 Markdown 反解析文本、标题、元数据及底层 JSON 块，抽屉右下角成功呈现醒目红底按钮【已确认无追溯价值，安全清理】；
  3. `output/playwright/mongo-orphans/03_delete_confirm_dialog.png`：点击抽屉清理按钮后弹出二次确认对话框，带黄色警告与 Fencing Token 安全阻断机制说明；
  4. `output/playwright/mongo-orphans/04_batch_cleanup_modal.png`：点击顶部批量工具条按钮后弹出批量安全清理二次确认对话框。

### 3. 孤儿类型动态识别增强（2026-09-03 追加）
- **背景与问题**：
  - 用户清理完 9 条历史快照后，剩余的 1 条 `content_body_versions` 正文版本（`page_id=638`，`body_version_id=33b1018ec559...`）显示为【未知受阻正文 (暂不支持清理)】且没有删除按钮；
  - 经查证，`page_id=638` 在 MySQL 的页面表、Revision 表中均已删除，但由于该页面在引入 `PageDeletionIntent` 前已删除，旧服务层未将搜索同步系统的墓碑记录（`ContentSearchState.desired_operation == "tombstone"` 与 `ContentSearchOutbox.operation == "tombstone"`）纳入 `tombstone_pages` 收集白名单，导致被保守回退规则误归入 `blocked_unknown`。
- **动态联动优化**：
  - 在 `MongoOrphanService.collect_mysql_context()` 中动态联动 `ContentSearchState` 和 `ContentSearchOutbox` 的墓碑集合；
  - 页面编号 `638` 成功通过搜索墓碑确认为已物理删除页面，其对应的 Mongo 正文版本动态调整为 **【完全孤儿正文 (可安全清理)】(`orphan_candidate`)**；
  - 面板指标卡片动态调整：完全孤儿 1，历史残留 0，未知受阻 0；第一列复选框激活呈现，操作列呈现【查看正文】与【安全清理】按钮；
  - 运行全套 23 项单元测试与系统检查全绿通过。
