# Wagtail 扩展依赖升级方案

## 1. 方案状态

- 方案日期：2026-08-14
- 当前状态：实施中，仅限测试环境
- 目标分支：`main`
- 当前基线：`ceaf0ad35ce88a0501121dfb2b5fd9fae8e54983`
- 生产操作：本方案不授权生产安装、同步、重启或数据操作

## 2. 背景与现状证据

当前测试环境为 Python 3.13.2、Django 5.2.8、Wagtail 7.4.2。依赖清单中的相关版本为：

| 依赖 | 当前版本 | PyPI 最新版本 | 实际使用情况 |
| --- | --- | --- | --- |
| `wagtail-ai` | 3.1.0 | 3.1.1 | `apps/blog/models.py`、`ai_backends.py`、后台钩子使用 |
| `wagtailmedia` | 0.17.2 | 0.18.1 | `apps/blog/blocks.py` 的音频/视频选择块使用 |
| `wagtail-modeladmin` | 2.3.0 | 2.4.0 | 仅在 `INSTALLED_APPS` 注册，未发现业务导入 |
| `wagtail-markdown` | 0.14.1 | 0.14.1 | 未发现项目代码使用；项目使用自定义 Vditor Markdown 块 |
| `wagtail-video` | 1.0.1 | 1.0.1 | 未发现项目代码使用 |

`admin.py` 中的 `ModelAdmin` 来自 Django，不是 `wagtail-modeladmin`。因此本批次只升级实际使用且有新版本的 `wagtail-ai` 与 `wagtailmedia`。`wagtail-modeladmin` 不在本批次升级，后续可单独评估移除其注册和依赖；`wagtail-markdown`、`wagtail-video` 不升级。

官方变更记录显示：`wagtail-ai 3.1.1` 修复图片模型导入路径和上传 MIME 类型；`wagtailmedia 0.18.x` 增加 Wagtail 7.2 至 7.4 支持，并加强媒体选择器权限检查，同时删除不再需要的旧兼容模板。两者均满足当前 Wagtail 7.4.2、Django 5.2.8 的依赖范围。

## 3. 目标与非目标

### 3.1 目标

1. 将项目依赖清单中的 `wagtail-ai` 更新到 `3.1.1`。
2. 将项目依赖清单中的 `wagtailmedia` 更新到 `0.18.1`。
3. 在 WSL2 `wagtailblog-test` 环境完成安装、Django 检查、迁移一致性检查和博客相关测试。
4. 重启 8080 测试服务，确认首页、后台登录入口和进程状态正常。

### 3.2 非目标

- 不升级 Wagtail 核心、Django、Python 或其他依赖。
- 不升级或移除 `wagtail-modeladmin`、`wagtail-markdown`、`wagtail-video`。
- 不修改 BlogPage、StreamField、Markdown 存储 key、MongoDB 正文、Wagtail revision 或媒体数据。
- 不执行数据库迁移、不发布页面、不保存真实内容、不操作生产环境。
- 不修改 systemd、Nginx、uWSGI、Celery、Beat、Filebeat 或 `systemctl.md`，因为本批次不改变服务定义。

## 4. 设计与实施步骤

1. 记录工作区、当前版本、8080 服务和 Git 状态。
2. 写入本方案并检查目标版本的依赖解析结果。
3. 更新 `requirements.txt` 的两个精确版本号。
4. 停止正在运行的测试 `runserver`，在 `wagtailblog-test` 中执行精确版本安装。
5. 执行 `python manage.py check`、`makemigrations --check --dry-run`、`migrate --plan` 和博客相关测试。
6. 启动 8080 测试服务，检查首页、后台入口、日志和进程。
7. 将实际修改、测试结果、回滚点和残余风险追加到本方案实施记录。

## 5. 文件范围

### 5.1 计划修改

- `requirements.txt`：仅修改 `wagtail-ai` 和 `wagtailmedia` 的版本号。
- `说明书/15-Wagtail扩展依赖升级方案.md`：记录方案和实施结果。

### 5.2 明确不修改

- `wagtailblog3/apps/blog/blocks.py`、`models.py`、`wagtail_hooks.py`、`ai_backends.py`。
- 所有迁移文件、模板、静态文件、环境文件和凭据文件。
- 生产项目、生产环境文件和生产服务。

## 6. 数据、服务与风险影响

本批次只改变代码依赖解析和测试 Python 环境，不改变数据库 schema 或受保护内容。升级期间需要短暂停止并重启测试 8080 服务；生产服务不受影响。

主要风险与控制措施：

| 风险 | 控制措施 | 失败处理 |
| --- | --- | --- |
| 媒体 chooser、模板或权限行为变化 | 执行博客测试，并检查 Wagtail 后台入口；必要时补充浏览器验收 | 测试环境回退到旧版本并重启服务 |
| AI 后台面板或自定义 backend 不兼容 | 执行 Django check、模型导入和相关测试；检查后台资源加载 | 回退 `wagtail-ai` 到 3.1.0 |
| 测试环境与依赖清单不一致 | 安装后重新读取三个包版本，保留 `requirements.txt` 精确锁定 | 不进入生产发布流程 |
| 8080 服务中断或旧进程继续持有模块 | 安装前停止旧进程，安装后重新启动并检查 PID、端口和 HTTP | 恢复旧包后重新启动服务 |
| StreamField 历史数据受影响 | 本批次不改 block 路径、不改序列化 key、不运行迁移或内容保存 | 仅回退依赖，不触碰数据库内容 |

## 7. 测试与验收

必须至少通过：

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py migrate --plan
python manage.py test wagtailblog3.apps.blog
```

服务验收包括：8080 进程存活、`GET /` 返回预期重定向、`GET /admin/` 返回登录重定向、日志无启动异常。涉及 Wagtail 后台编辑器或媒体 chooser 的失败时，必须追加桌面和移动视口浏览器验收；Playwright 产物只能写入 `output/playwright/`。

## 8. 回滚点

回滚目标为当前已验证版本：`wagtail-ai==3.1.0`、`wagtailmedia==0.17.2`，并恢复 `requirements.txt` 两行版本。回滚只涉及测试依赖和测试服务重启，不执行数据库回滚，不删除 MongoDB 正文、草稿、revision 或媒体对象。

生产发布必须另行说明备份、影响、顺序、服务操作和回滚，并获得明确确认；本方案不构成生产授权。

## 9. 模型/推理强度建议

- 模型角色：先用快速档做依赖与源码事实核对，再用常规开发档执行局部依赖升级和测试。
- 推理强度：事实核对低至中等；实现与兼容性验证中等。
- 选择理由：本批次是边界清楚的 Django/Wagtail 依赖补丁升级，不涉及 schema、数据迁移或生产发布。
- 升级条件：若发现 Wagtail 8 迁移、StreamField 序列化、媒体权限边界、跨服务依赖或生产回滚问题，升级为高强度复核，并暂停发布。
- 验证门禁：版本元数据、依赖解析、Django check、迁移计划、相关测试和服务 HTTP 检查全部通过后，才允许形成发布候选。
- 实际使用情况：当前会话未暴露底层模型标识；已使用 Django/Wagtail 技能、GitHub 官方仓库读取和 WSL2 代理后的 PyPI 查询完成核对。

## 10. 实施记录

### 2026-08-14 方案阶段

- 状态：方案已写入，代码和测试环境尚未修改。
- 现状：Git 工作区干净；测试服务监听 `0.0.0.0:8080`；当前版本为 Wagtail 7.4.2、`wagtail-ai` 3.1.0、`wagtailmedia` 0.17.2。
- 数据/服务影响：尚无；后续仅短暂重启测试服务。
- 回滚点：尚未产生新变更；现有 Git commit 和测试环境版本保持可用。

### 2026-08-14 测试环境实施完成

- 状态：完成测试环境依赖升级与服务验收，未进入生产发布。
- 实际修改文件：`requirements.txt`、`说明书/15-Wagtail扩展依赖升级方案.md`。
- 实际安装：WSL2 `wagtailblog-test` 已从 `wagtail-ai 3.1.0` 升级至 `3.1.1`，从 `wagtailmedia 0.17.2` 升级至 `0.18.1`；`pip check` 通过，未升级 Wagtail、Django 或其他包。
- 依赖配置：`requirements.txt` 已锁定 `wagtail-ai==3.1.1` 和 `wagtailmedia==0.18.1`。
- Django 检查：`WAGTAILBLOG_ENV=test python manage.py check` 通过；`makemigrations --check --dry-run` 输出 `No changes detected`。
- 迁移状态：`migrate --plan` 与 `showmigrations blog` 显示测试库已有未应用的 `blog.0024_alter_blogpage_body`。本批次未生成或应用迁移，也未修改 StreamField block 路径、Markdown key、MySQL、MongoDB、revision 或媒体数据。
- 测试结果：针对 Markdown、图片格式和后台图片上传的 45 项测试通过；使用正确应用标签执行 `python manage.py test blog` 的 90 项测试通过。`python manage.py test wagtailblog3.apps.blog` 因测试标签绕过 `INSTALLED_APPS` 的 `blog` 应用名而有 2 项导入失败，不作为本批次依赖升级失败依据。
- 服务验收：已停止旧 8080 测试进程；新 `runserver 0.0.0.0:8080 --noreload` 进程 PID 为 `143396`，监听正常。WSL2 和 Windows 主机均验证首页返回 `302 -> /zh-hans/`，后台入口返回 `302 -> /admin/login/?next=/admin/`。
- 数据/服务影响：仅短暂中断测试 8080 服务；测试框架创建并销毁隔离测试数据库；生产服务、生产数据、systemd 和环境文件均未操作。
- Git 状态：变更尚未提交；未创建 commit 或推送。
- 回滚点：将 `requirements.txt` 恢复为 `wagtail-ai==3.1.0`、`wagtailmedia==0.17.2`，在测试环境安装这两个旧版本后重启 8080 服务。无需数据库回滚。
- 残余风险：未使用真实管理员身份操作 AI 生成、媒体 chooser 上传和权限受限媒体选择；生产安装、迁移和服务重启仍需单独授权及完整发布门禁。`blog.0024_alter_blogpage_body` 的既有未应用状态需要在独立数据保护方案中处理，不能与本次依赖升级合并执行。

### 2026-08-14 测试库迁移授权

- 授权：用户明确授权在 `WAGTAILBLOG_ENV=test` 的测试库完成升级后的迁移并重启测试项目；生产环境仍未授权。
- 迁移目标：仅 `blog.0024_alter_blogpage_body`，当前测试库已应用至 `blog.0023_articleengagementsession_feedclientdaily_and_more`。
- 迁移内容：单个 `AlterField(BlogPage.body)`，用于同步 StreamField block 定义；不包含 `RunPython`、数据删除、批量修复、页面发布或 MongoDB 写入。
- 执行前门禁：检查 SQL 计划、确认环境变量为 `test`、确认生产无操作、停止 8080 测试进程避免新旧模型状态并存。
- 回滚：若迁移或启动验证失败，停止测试服务并执行 `python manage.py migrate blog 0023`，随后恢复已验证依赖版本或代码版本；不删除 MongoDB 正文、revision 或媒体对象。

### 2026-08-14 测试库迁移实施完成

- 状态：完成，仅测试环境。
- 实际操作：停止旧 8080 测试进程，在 `WAGTAILBLOG_ENV=test` 执行 `python manage.py migrate blog 0024 --noinput`。
- SQL 核验：`python manage.py sqlmigrate blog 0024` 输出 `-- (no-op)`；迁移只更新 Django 迁移记录，不执行数据库 DDL 或数据转换。
- 迁移结果：`Applying blog.0024_alter_blogpage_body... OK`；`showmigrations blog` 已确认 `0001` 至 `0024` 全部为已应用状态。
- 数据影响：未修改 MongoDB 正文、草稿、revision pointer、媒体对象或页面发布状态；未执行生产数据库、生产服务或环境文件操作。
- 验证：迁移后 `python manage.py check` 通过；保留 MySQL 关于 `wagtailcore.WorkflowState` 条件唯一约束不受支持的既有警告。此前已通过依赖检查、45 项受影响测试和 90 项 `blog` 测试。
- 服务验收：新 8080 测试进程 PID 为 `154481`，监听 `0.0.0.0:8080`。WSL2 与 Windows 主机均验证首页返回 `302 -> /zh-hans/`，后台入口返回 `302 -> /admin/login/?next=/admin/`。
- 回滚点：若需要撤销测试迁移记录，执行 `WAGTAILBLOG_ENV=test python manage.py migrate blog 0023`，随后重启 8080；因 SQL 为 no-op，不需要数据库内容回滚。
- Git 状态：仍未提交或推送；本批次代码文件变更只包括依赖锁定和本方案文档。
- 残余风险：真实管理员身份下的 AI 生成、媒体 chooser 上传与权限受限媒体选择仍待人工页面验收；生产迁移和发布必须另行获得授权。

### 2026-08-14 生产发布授权

- 状态：用户已明确授权提交、推送、同步生产依赖、核对并执行必要的生产迁移、重启项目及完成验收。
- 发布门禁：生产仓库已有未提交的 `requirements.txt` 与本方案文档改动，先使用 Git stash 保留原始改动，再只允许 fast-forward 到已验证 commit；不覆盖或删除脏改动。
- 环境边界：生产使用实际核实的 Conda 环境与 `.env.production`，不复制测试凭据或数据库内容；“一致”仅指代码 commit、锁定依赖版本和迁移状态一致。
- 数据保护：不复制或修改生产 BlogPage 正文、MongoDB 正文、草稿、revision、媒体对象或页面发布状态；仅在迁移门禁确认后执行必要迁移。

### 2026-08-14 生产实施完成

- 状态：已完成生产同步、依赖升级、迁移核验、应用服务重启与健康检查。
- 实际修改文件：`requirements.txt`、本方案文档；生产代码已 fast-forward 到 `a8cb1691fd1116155559c9560e0c7c4f8ca1f37b`，生产工作区干净。
- 依赖结果：生产 `/root/anaconda3/envs/wagtailblog` 中 `wagtail-ai==3.1.1`、`wagtailmedia==0.18.1`，与测试环境锁定版本一致；`pip check` 通过。
- 迁移结果：生产 `blog.0024_alter_blogpage_body` 已应用，`migrate --plan` 无待执行操作；未重复写入迁移或修改受保护内容。
- 服务结果：`wagtailblog3.service`、maintenance Worker、Beat、Filebeat 均为 `active/enabled`；未修改 unit，因此未执行 `daemon-reload`。
- HTTP 验收：生产首页返回 `200`，Nginx 监听 `0.0.0.0:80`，重启后相关 unit 日志无 error。直接访问 `/admin/` 返回 `404`，需按当前站点语言路由继续人工确认后台入口。
- 回滚点：代码回滚到发布前已验证的 `2924dd4`；依赖回滚到 `wagtail-ai==3.1.0`、`wagtailmedia==0.17.2` 后重启相关服务；不回滚数据库内容或 MongoDB 受保护数据。
- 残余风险：后台 URL 的语言前缀验收尚未完成；生产保留既有 `WorkflowState` 条件唯一约束警告。

### 2026-08-21 Wagtail 7.4.3 安全补丁测试升级方案

- 状态：实施中，仅限 WSL2 `wagtailblog-test`；本节不授权生产安装、Git 提交、推送、生产同步、迁移或服务重启。
- 背景与现状证据：`requirements.txt` 与 WSL2 运行环境均为 `Wagtail==7.4.2`、Django 5.2.8。Wagtail 7.4.3 官方发行说明包含 Pages 后台 API、文档 SHA1 探测、私有集合下级媒体 API、Snippet 复制和页面翻译权限的五项安全修复。项目启用 Wagtail 后台、多语言、`/documents/` 路由、自定义文档/图片模型和 Snippet，故需要应用该安全补丁。

#### 目标与非目标

- 目标：将仓库锁定版本和 WSL2 测试环境从 `Wagtail==7.4.2` 升级为 `Wagtail==7.4.3`，验证依赖、迁移计划、Django 配置和受影响应用测试。
- 非目标：不升级 Django、Python 或其他依赖；不改业务代码、模板、静态资源、迁移、StreamField block 路径或 `markdown_block` key；不执行数据库迁移、页面发布、真实内容保存、MongoDB/MinIO/Redis 写入或生产操作。

#### 设计与实施步骤

1. 核对 Git 状态、`WAGTAILBLOG_ENV=test`、测试 Conda 路径、当前版本和 7.4.3 依赖解析结果。
2. 仅将 `requirements.txt` 中的 Wagtail 精确版本更新为 `7.4.3`，并在 WSL2 `wagtailblog-test` 中安装该精确版本。
3. 验证安装版本与 `pip check`，执行 `manage.py check`、`makemigrations --check --dry-run` 和 `migrate --plan`；只读取迁移计划，不应用迁移。
4. 执行 `blog` 与 `base` 应用的测试，覆盖自定义后台、文档/图片模型、Snippet 和 FormPage 相关路径；失败时停止，不进入生产步骤。
5. 将实际版本、测试结果、未提交状态、数据/服务影响、回滚点和残余风险追加到本节。

#### 文件、数据与服务影响

- 实际计划修改：`requirements.txt` 的一行精确版本；本方案文档的本节和实施记录。
- 明确不修改：业务 Python、迁移、模板、静态文件、环境文件、`systemctl.md`、systemd unit、Nginx/uWSGI/Celery/Beat/Filebeat 配置和生产目录。
- 数据影响：不运行迁移或数据写入；不读取或修改 BlogPage 正文、StreamField、MongoDB 正文/草稿/revision pointer、媒体对象或 Token 明文。
- 服务影响：8080 测试端口核实为未监听，本次不停止、启动或重启测试服务；生产服务无影响。

#### 测试、验收与回滚

- 验收命令：`pip check`、读取 `wagtail.__version__`、`WAGTAILBLOG_ENV=test python manage.py check`、`makemigrations --check --dry-run`、`migrate --plan`、`python manage.py test blog base`、Git diff 检查。
- 验收标准：仅 Wagtail 从 7.4.2 变为 7.4.3，依赖无冲突；不产生新迁移；迁移计划无因本次升级新增的待执行迁移；Django 检查和受影响应用测试通过。
- 回滚点：将 `requirements.txt` 恢复为 `wagtail==7.4.2`，并在测试 Conda 中安装 `Wagtail==7.4.2`；不需要数据库、MongoDB 或媒体回滚。
- 生产确认：生产实际版本、生产仓库和服务状态须在独立发布步骤重新核实。用户需在测试验收后，另行确认生产安装、同步、重启和健康检查；本测试授权不构成生产授权。
- 残余风险：本次不以真实低权限后台用户复现漏洞，测试不能替代生产权限审计。若解析出现意外依赖变更、迁移计划变化、权限/文档/翻译测试失败或需要生产操作，停止并升级为高风险复核。

#### 模型/推理强度建议

- 模型角色与强度：依赖与发行说明核对适用 `gpt-5.6-luna` 低/中推理；本次局部依赖升级、Django 验证与记录适用 `gpt-5.6-terra` 中推理。
- 选择理由：这是同一 Wagtail 小版本内的安全补丁，变更限于一项精确依赖和测试环境，不涉及 schema、内容迁移或生产服务。
- 升级条件：出现迁移、跨服务配置、权限回归、数据保护风险、生产发布/回滚或无法确定的第三方兼容性时，改用 `gpt-5.6-sol` 高推理复核并暂停后续操作。
- 验证门禁：版本、`pip check`、Django 检查、迁移计划和受影响测试均通过，且 Git diff 仅限预定两文件，才可形成生产候选；模型选择不替代测试或生产授权。
- 实际使用：当前会话使用可用 Codex 模型完成核对、实现和测试；未调用外部模型，未外发源码、凭据、正文或生产日志。

#### 实施记录

- 2026-08-21：方案已写入，升级和测试待执行。基线为 WSL2 Python 3.13.2、Django 5.2.8、Wagtail 7.4.2，Git `main` 工作树干净；生产未访问。
- 2026-08-21：测试环境升级与验收完成，未进入生产发布。`requirements.txt` 已锁定 `wagtail==7.4.3`；WSL2 `/root/anaconda3/envs/wagtailblog-test` 已从 7.4.2 安装为 7.4.3，完整 requirements 安装未更新其他包，`pip check` 通过。
- Django 验收：`WAGTAILBLOG_ENV=test python manage.py check` 通过；`makemigrations --check --dry-run` 为 `No changes detected`；`migrate --plan` 为 `No planned migration operations`，未应用迁移。`blog base` 共 236 项测试通过；套件中的 `/zh-hans/blog/api/markdown-import/userscript/prepare/` 未授权日志为预期拒绝用例。
- 数据与服务影响：未执行迁移、内容保存、MongoDB/MinIO/Redis 写入、测试服务启停或任何生产 SSH、Git、数据库、服务操作。Django 测试框架创建并销毁了隔离测试数据库。`systemctl.md` 未修改，生产服务未触及。
- Git 与回滚：变更仍未提交，仅包含 `requirements.txt` 与本方案文档；回滚为将锁定版本改回 `wagtail==7.4.2` 并在测试 Conda 安装该版本，无数据库或受保护内容回滚步骤。
- 残余风险：仍未使用真实低权限后台用户逐项复现五个上游权限场景，也未核实生产实际安装版本。测试配置输出的 Redis 与 Elasticsearch 主机仍需在任何生产候选前重新确认环境隔离；本次不据此执行外部服务操作。
- 2026-08-21：用户授权启动已升级的测试栈以供手工检查。按仓库既有 `tools/start_test_stack.sh` 启动 WSL2 测试网站和隔离 Worker；网站 PID `45648` 以 `WAGTAILBLOG_ENV=test` 监听 `0.0.0.0:8080`，Worker PID `45649` 仅消费 `markdown-test-maintenance` 队列。首页验证为 `302 -> /zh-hans/`，后台验证为 `302 -> /admin/login/?next=/admin/`；测试 broker 上 `markdown-test-isolated@ming` 的 `celery inspect ping` 返回 `pong`。
- 数据与服务影响：未修改服务定义、环境文件、数据库、MongoDB、媒体、Redis 数据或生产服务器，故 `systemctl.md` 无需更新。测试栈会保持运行，回滚为停止 `output/test-runserver-8080.pid` 和 `output/test-worker-markdown.pid` 指向的精确进程；未创建 Git commit。

### 2026-08-21 Wagtail 7.4.3 生产发布授权与实施方案

- 授权：用户明确授权提交、推送并部署本次 Wagtail 7.4.3 安全补丁到生产仓库；不授权生产内容保存、MongoDB/MinIO/Redis 数据操作、迁移或环境文件修改。
- 发布前证据：生产目录 `/home/source/Django/wagtail/wagtailblog3` 位于 `main`，工作树干净，`HEAD=97a661b1e2f579ff549366b62234680d7688dfaf`，远程为预期 GitHub 仓库。生产 Conda `/root/anaconda3/envs/wagtailblog` 当前为 Wagtail 7.4.2，`pip check` 通过；uWSGI、maintenance Worker、Beat、Filebeat 均为 `active/enabled`，并使用 `.env.production`。
- 发布步骤：在 WSL2 提交并推送仅含 `requirements.txt` 与本方案的 commit，确认本地、`origin/main` 和 GitHub `main` 为同一 SHA；生产仅在干净 `main` 上 `fetch`、核对 `HEAD..origin/main` 文件清单后 `merge --ff-only`。安装精确 `requirements.txt`，再次执行 `pip check`、Wagtail 版本读取、`WAGTAILBLOG_ENV=production python manage.py check`、`makemigrations --check --dry-run` 和 `migrate --plan`。
- 迁移与数据门禁：本次预期无迁移；只有 `migrate --plan` 显示无操作时才继续。若出现任何待执行迁移、数据变更、受保护正文/草稿/revision/MongoDB/媒体风险或环境文件差异，停止发布并保留现状，不应用迁移。
- 服务影响与验收：依赖变更需按顺序重启 `wagtailblog3.service`、`wagtailblog3-celery-maintenance.service`、`wagtailblog3-celery-beat.service`；不修改 unit，故不执行 `daemon-reload`。Filebeat 配置与日志路径未变，不重启。重启后核对三项服务和既有 Filebeat 均为 `active/enabled`，检查 Django、首页、后台入口、socket/端口与近期错误日志。
- 回滚：发布前生产 commit `97a661b1e2f579ff549366b62234680d7688dfaf` 和 Wagtail 7.4.2 为回滚点。若安装或健康检查失败，停止本次涉及服务、fast-forward 以外不得改写历史；在确认生产仓库状态后恢复该 commit、安装 `Wagtail==7.4.2` 并按相同顺序重启，再执行 Django 与访问检查。无数据库、MongoDB 或媒体回滚。
- 模型/推理强度建议：生产发布和回滚判断属于高风险工作，建议 `gpt-5.6-sol` 高推理独立复核；当前会话使用可用 Codex 模型执行，并以 SSH 状态、Git、依赖和健康检查作为门禁。任何模型选择不替代生产授权、测试或回滚验证。
