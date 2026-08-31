# WagtailBlog3 测试与生产维护手册

本文档记录同一套 `main` 分支代码在测试电脑和生产服务器上的启动、停止、
健康检查与回滚方式。环境差异只允许存在于未跟踪的环境文件、运行目录和服务配置中，
不得通过修改 `settings/base.py` 或维护不同代码分支制造环境差异。

- 测试项目：`/mnt/f/openclaw/workspace/wagtail/wagtailblog2`（Windows 路径为
  `F:\openclaw\workspace\wagtail\wagtailblog2`），Conda 环境为
  `/root/anaconda3/envs/wagtailblog-test`；
- 生产项目：`/home/source/Django/wagtail/wagtailblog3`，Conda 环境为
  `/root/anaconda3/envs/wagtailblog`；
- 测试网站默认由维护人员在 WSL 中使用 Django `runserver` 启动；
- 生产应用必须由 systemd 管理，不使用 `start.sh` 或 Django `runserver`。

## 服务拓扑

生产入口由 Nginx 监听 6050，并通过
`/home/source/Django/wagtail/wagtailblog3/wagtailblog3.sock` 转发给 uWSGI。
uWSGI 的 6051 是仅供本机诊断的 HTTP 端口，不作为对外入口。

必须运行的应用服务如下：

| 服务 | 职责 | 是否开机启动 |
| --- | --- | --- |
| `wagtailblog3.service` | uWSGI / Django 网站 | 是 |
| `wagtailblog3-celery-maintenance.service` | 日志索引同步和维护队列；执行 Markdown 导入会话组装、媒体精确补偿、Mongo 清理意图和内容搜索 Delivery 的租约消费与重试 | 是 |
| `wagtailblog3-celery-beat.service` | 补偿日志索引 outbox、内容搜索、Mongo 清理意图和 BlogPage 删除意图的 pending/过期租约任务，每日调度博客分析明细清理；定时投递过期 Markdown 导入会话和媒体 cleanup retry | 是 |
| `wagtailblog3-filebeat.service` | 采集项目日志并写入 Elasticsearch | 是 |

## 分支与环境配置

测试和生产代码仓库都固定使用 `main` 分支。部署或启动前分别执行：

```bash
git branch --show-current
git rev-parse --short HEAD
git status --short --branch
```

只有通过测试的精确 commit 才能部署到生产。不得部署未提交的工作区内容，也不得
因为环境不同而直接修改 `settings/base.py`。

### Codex 工具与发布门禁

Codex MCP、Skill 和插件只运行在开发协作环境中，不属于生产服务拓扑，也不得安装为生产
常驻进程或写入 systemd unit。仓库的 pre-commit hook 与 GitHub Actions 是提交和远端检查
门禁，不代表生产已经同步或验收通过；文档-only 变更无需因此重启任何服务。

生产状态检查和代码同步可使用已安装的 `wagtailblog-ssh-ops` 插件，或使用 WSL2 中现有的
SSH 工作流。两种写入口不得在同一任务中并发使用；无论使用哪一种，都必须先核实生产路径、
分支、远程地址、工作树状态和目标 SHA，再依次执行 `fetch`、差异清单检查和
`merge --ff-only`。工具具备 SSH、同步或重启能力不等于已经获得生产数据、迁移、环境文件、
systemd unit、端口、队列或服务重启授权，相关操作继续执行本文的备份、确认与回滚门禁。

数据库、Redis、MongoDB、MinIO、Wagtail Elasticsearch、SMTP 和 Django 密钥统一放在：

```text
wagtailblog3/settings/.env.test
wagtailblog3/settings/.env.production
```

测试电脑只保留 `.env.test`，生产服务器只保留 `.env.production`。这两个文件已加入
`.gitignore`，仓库只提交 `wagtailblog3/settings/.env.example`。
所有入口继续使用 `wagtailblog3.settings.dev`，因为测试和生产都由同一用户维护，
都需要保留当前开发/调试行为。`WAGTAILBLOG_ENV=test|production` 只负责让
`settings/base.py` 选择对应的基础设施环境文件；它不会根据两个文件谁存在而自动判断环境。

- 未设置 `WAGTAILBLOG_ENV` 时默认选择 `.env.test`；
- `WAGTAILBLOG_ENV=test` 时只读取 `.env.test`；
- `WAGTAILBLOG_ENV=production` 时只读取 `.env.production`；
- 生产模式缺少 `.env.production` 时拒绝启动；
- 两个文件即使同时存在，也只读取 `WAGTAILBLOG_ENV` 指定的一个；
- 由于 `load_dotenv(..., override=False)`，进程或 systemd 已提供的变量优先于文件内容。

### BlogPage AI 元数据生成

`AI_METADATA_*` 变量仅为 BlogPage 编辑器的按需 Responses API 请求提供配置，不新增
systemd unit、端口、Celery 队列或定时任务。测试环境可在 `.env.test` 配置
`AI_METADATA_PROVIDER=openai`、`AI_METADATA_API_KEY`、`AI_METADATA_BASE_URL`、
`AI_METADATA_MODEL` 和可选的 `AI_METADATA_REASONING_EFFORT`；
`AI_METADATA_RESPONSE_STORAGE` 必须保持 `false`，应用会拒绝在其为 `true` 时启动请求。

生产环境在未获得授权时默认不配置这些变量，也不得为调试加载 `.env.production` 或使用
`runserver`。本次已获授权启用生产元数据生成：变量写入生产 `.env.production`，由
`wagtailblog3.service` 读取；Worker、Beat、Filebeat 和 Nginx 不受影响。后续变更仍属于受保护
正文向外部服务传输和生产环境配置变更，必须单独说明 provider、数据范围、`store=false` 证据、
超时/失败行为、回滚方式，并获得授权后才可修改环境文件和重启服务。

### 搜索高亮回滚开关

`SEARCH_HIGHLIGHTS_ENABLED` 默认 `true`。设为 `false` 后，前台搜索回退到 WP1 的 Wagtail
`live().public()` 搜索和原有标题/简介摘要，不读取 ES 高亮片段，也不修改 MySQL、MongoDB 或
Elasticsearch 索引。该开关仅用于已部署 WP2 代码后的紧急展示回滚；修改生产 `.env.production`
前仍须执行生产配置变更确认，并仅重启 `wagtailblog3.service` 后验证首页、后台和搜索。恢复
`true` 同样只需重启该服务，Worker、Beat、Filebeat 和 Elasticsearch 无需重启。

生产 systemd unit 使用：

```ini
EnvironmentFile=/home/source/Django/wagtail/wagtailblog3/wagtailblog3/settings/.env.production
```

### WP5 游标分页与建议开关

以下开关默认关闭，且不改变现有 systemd 服务、队列或端口。测试环境只有在完成
游标签名、`search_after`、公开边界和浏览器验收后，才可单独启用对应测试进程配置：

- `CONTENT_SEARCH_CURSOR_ENABLED`：仅对独立 `blog` 搜索启用签名游标；关闭后回到有窗口限制的 `page=` 分页。
- `CONTENT_SEARCH_CURSOR_MAX_AGE_SECONDS`：游标最长有效期，默认 900 秒。
- `CONTENT_SEARCH_PIT_ENABLED`：可选短生命周期 PIT 一致性；默认关闭，异常时不影响旧搜索。
- `SEARCH_SUGGESTIONS_V2_ENABLED`：启用拆分后的建议协议；默认关闭时继续使用既有 Wagtail 历史词查询。
- `SEARCH_POPULAR_SUGGESTIONS_ENABLED`：公开经过安全清理的热门历史词。
- `SEARCH_TITLE_SUGGESTIONS_ENABLED`：从 `CONTENT_SEARCH_TITLE_SUGGESTIONS_READ_ALIAS` 读取公开标题候选，并执行 `live().public()` 二次校验。

这些 flag 只属于应用配置，不新增 Worker、Beat、Filebeat 或 Nginx 依赖。生产修改 `.env.production`
前仍须单独授权、记录回滚值，并按实际影响重启 `wagtailblog3.service`；标题索引创建、回填和 alias
切换另行执行数据/索引变更门禁。

### 生产空内容索引创建门禁

发布包含 `search_create_production_content_index` 的代码后，生产创建独立内容搜索空索引必须使用
该命令，不能通过伪造 `WAGTAILBLOG_ENV=test` 调用测试命令。命令默认仅输出 dry-run JSON，不连接
Elasticsearch 或写入 MySQL；它不会创建 read alias、回填 Mongo 正文、启用 Producer/Consumer 或改变前台搜索。

执行前由受保护的生产环境文件显式提供以下非凭据配置，默认均为空或 `false`：

```ini
CONTENT_SEARCH_PRODUCTION_CONNECTION_NAME=<独立 ES 连接名，不能是 default>
CONTENT_SEARCH_PRODUCTION_INDEX_PREFIX=<含 prod 标识的精确前缀>
CONTENT_SEARCH_PRODUCTION_BACKUP_ROOT=/home/source/Django/wagtail/backups
CONTENT_SEARCH_PRODUCTION_INDEX_CREATE_ENABLED=false
```

命令要求精确的 `--target`、`--index-name` 和已校验的 `--backup-reference`；仅当备份目录内存在
`checksums.sha256`、独立连接已配置、所有搜索读写 flag 仍关闭，且同时提供 `--confirm` 和
`--confirm-production-index-create` 时才会创建 template、物理索引及禁用状态的 Target/Build 记录。
创建前必须单独获得生产索引写入授权；创建后立即把
`CONTENT_SEARCH_PRODUCTION_INDEX_CREATE_ENABLED` 恢复为 `false`。该命令只由一次性管理命令进程读取，
不因该开关变更重启 Django、Worker、Beat 或 Filebeat。

### 生产 Elasticsearch snapshot 备份

生产现有 Elasticsearch 由 Docker Compose 项目
`/home/software/docker/compose/elasticsearch8/docker-compose.yml` 管理，容器为
`elasticsearch8.17.0`，端口为 `9200`/`9300`，数据目录为
`/home/software/docker/containers/elasticsearch8.17.0/data`，日志目录为
`/home/software/docker/containers/elasticsearch8.17.0/logs`。

2026-08-11 在生产部署搜索代码前创建了独立备份目录：

```text
/home/source/Django/wagtail/backups/wagtailblog3-pre-search-20260811-221511/
```

为创建可恢复的 Elasticsearch snapshot，保留原 compose 文件后增加了
`docker-compose.snapshot-override.yml`，将以下目录以读写方式挂载到容器：

```text
/home/source/Django/wagtail/backups/wagtailblog3-pre-search-20260811-221511/elasticsearch/snapshot-repository
/usr/share/elasticsearch/snapshots
```

override 同时向 Elasticsearch 传入
`-Epath.repo=/usr/share/elasticsearch/snapshots`。容器使用原有 image、数据卷、日志卷、端口和
`restart=always`，仅因新增 snapshot 挂载执行过一次 `--force-recreate`。重建后 9200 立即恢复，
32 个索引仍可见，57 个 primary shard active，项目四个 systemd 服务均 active/enabled。

snapshot repository 和快照名称均为：

```text
wagtailblog3-pre-search-20260811-221511
pre-search-20260811-221511
```

快照状态为 `SUCCESS`，46 个索引、46/46 shard 成功、失败 0；快照文件约 52 MiB。快照使用
`include_global_state=false`，恢复时不得覆盖生产集群级设置。当前单节点仍为 `yellow`，15 个未分配
副本是单节点无副本的既有容量状态；不得为了“变绿”在本备份窗口擅自修改副本数或删除索引。

备份目录中的 `snapshot-status.json`、`repository-list.json`、`snapshot-create.json`、
`elasticsearch8-docker-compose.yml.before-snapshot` 和
`elasticsearch8-snapshot-override.yml` 是恢复所需证据。删除 snapshot、repository、数据卷或
override 前必须单独备份并获得授权。

恢复 ES 容器配置时，先确认应用、Kibana、Filebeat 的依赖状态；保留数据卷和 snapshot 目录，
优先执行：

```bash
cd /home/software/docker/compose/elasticsearch8
docker compose -f docker-compose.yml -f docker-compose.snapshot-override.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.snapshot-override.yml up -d --force-recreate elasticsearch8
```

恢复后检查 `_cluster/health`、snapshot 状态、索引计数、Kibana、Filebeat、Django 首页和旧搜索。
禁止使用 `docker compose down -v`、`docker volume rm` 或删除 snapshot 目录作为普通故障处理。

### WP6 测试 Elasticsearch 资源状态

原先在 WSL2 测试主机 `192.168.20.5:9210` 创建的独立 Elasticsearch 8.17.0 原型已经退役并
删除。容器 `wagtailblog-test-search-wp6-es`、专用 Docker 网络、数据卷、证书卷、密钥卷和快照卷
均已按精确清单清理；删除前的 inspect、索引清单、内存占用和删除证据保存在 Git ignored 的
`output/search-test-cutover/` 下。该清理只影响测试主机，不影响生产。

当前测试环境复用生产主机上的现有单节点 Elasticsearch：

| 项目 | 当前值 |
| --- | --- |
| ES 地址 | `http://192.168.20.2:9200` |
| 测试物理索引 | `wagtailblog-test-content-v003` |
| 测试 read alias | `wagtailblog-test-content-read` |
| 测试命名空间 | `wagtailblog-test-*` |
| 生产命名空间 | `wagtailblog-prod-*` |

测试和生产共用主机与 ES 服务，但通过独立索引前缀、alias、MySQL、MongoDB 和 Redis DB 隔离。
任何测试索引清理都必须先逐项核对 alias、Target、Build 和备份，不得使用通配符删除。
`192.168.20.5:9210` 当前应连接失败，这是独立测试 ES 已删除的预期结果。
生产内容搜索复用现有 `elasticsearch8.17.0` 单节点，不新增第二个 Elasticsearch 容器或 JVM。
旧 Wagtail 搜索继续使用 `default` 连接；新内容索引必须使用非 `default` 的逻辑连接名和
`wagtailblog-prod-content-*` 前缀，避免写入旧页面索引、日志索引或 Kibana 索引。

生产 `.env.production` 仅在获得本次索引写入授权后配置：

```ini
CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_ENABLED=true
CONTENT_SEARCH_PRODUCTION_CONNECTION_NAME=content_production
CONTENT_SEARCH_PRODUCTION_INDEX_PREFIX=wagtailblog-prod-content
CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_URL=http://127.0.0.1:9200
CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_AUTH_MODE=none
CONTENT_SEARCH_PRODUCTION_INDEX_CREATE_ENABLED=false
```

其中 `CONTENT_SEARCH_PRODUCTION_INDEX_CREATE_ENABLED` 只在一次性空索引创建命令执行窗口内临时为
`true`，命令完成后必须恢复为 `false`。该配置只注册逻辑连接，不启用 Producer、Consumer、影子读、
前台查询、回填或 alias 切换；这些动作仍按搜索实施计划分别授权。生产单节点不能配置副本，
`CONTENT_SEARCH_INDEX_REPLICAS=0`，集群保持单节点可接受的 `yellow` 状态。

旧 Wagtail 页面索引、日志索引、图片/文档索引和 Kibana 索引不得按“旧索引”一并删除。只有完成
新索引回填、增量追平、影子观察、读切换和回滚观察期后，才可针对明确列出的旧业务索引单独备份、
审计引用并授权删除。

生产创建独立集群、放行端口、安装证书、配置 snapshot、写入索引、切换连接或重启服务仍需独立方案、
备份和再次授权，不能直接复制本节测试参数。

旧版根目录 `observability.env` 已于 2026-08-08 从生产项目移出，其日志环境变量已统一
由 `wagtailblog3/settings/.env.production` 提供。可恢复备份位于
`/home/source/Django/wagtail/backups/wagtailblog3-observability-env-retired-20260808/`；
不得重新让 systemd unit 引用该旧文件，也不得在 `database.py`、`email.py` 或其他源码中
写入环境专用值或凭据。

## 服务变更登记规则

以后每次功能更新、异步任务更新、定时任务更新、日志链路更新或外部依赖更新，
都必须先检查本次变更是否新增或改变了运行服务。只要涉及以下任一项，就必须
在本手册中同步登记后才能部署：

- 新增或修改 systemd service、timer、socket；
- 新增 Celery worker 队列或 Celery Beat 任务；
- 新增日志采集器、索引同步器或其他常驻进程；
- 新增数据库、缓存、消息队列、对象存储或容器依赖；
- 修改服务启动顺序、环境变量、数据目录、日志目录或健康检查方式。

每个新增服务必须在本手册至少记录：服务名、职责、监听的队列或端口、项目目录、
运行环境、数据和日志路径、依赖服务、是否开机启动、启动/停止/重启命令、健康检查
命令、失败重试策略和回滚处理。新增服务未登记时，不得宣布部署完成。

当前日志系统改造涉及并必须启动的服务为：

- `wagtailblog3-celery-maintenance.service`：消费 `maintenance` 队列，执行日志索引同步和维护任务；WP3B
  新增内容搜索 Outbox 的提交后唤醒任务，但 `CONTENT_SEARCH_CONSUMER_ENABLED=false` 时只记录延后状态，
  不领取 Outbox、不读取 Mongo 正文、不写入 Elasticsearch。启用后，WP3C 的 Delivery 消费使用短租约、
  指数退避和外部版本写入；必须只写已登记的物理索引，不能指向 Wagtail 现有索引或别名；
- `wagtailblog3-celery-beat.service`：每 30 秒调度内容搜索 Delivery 的 pending/过期租约补偿。
  该任务在 consumer flag 关闭时直接返回，不创建 Delivery、不读取 Mongo 或 Elasticsearch；
- `wagtailblog3-celery-beat.service`：调度定时任务和失败补偿；博客分析清理任务默认由
  `BLOG_ANALYTICS_CLEANUP_ENABLED=false` 禁用，只有完成备份、影响确认和独立授权后才可启用；
- `wagtailblog3-filebeat.service`：采集项目日志并写入 Elasticsearch。

后续如果新增其他服务，必须把它加入“必须运行的应用服务”表、重启验收命令、
日志查看命令和开机启动命令，并同时更新对应的 systemd unit 文件和部署记录。

依赖服务也必须可用：`mysqld.service`、`redis.service`、
`mongodb-home.service`、`minio.service`、`docker.service` 和 Nginx。
生产 Elasticsearch 与 Kibana 由 Docker 管理，既有生产 Elasticsearch 容器设置为
`restart=always`；测试不再运行第二个 Elasticsearch 容器。

不要启动只消费 `email` 或 `default` 队列的 Celery Worker，除非已经检查
Redis 中没有历史邮件任务，并明确需要处理这些任务。当前 Worker 只监听
`maintenance` 队列。

## 测试环境启动

测试环境当前在 Windows 主机 `192.168.20.1` 的 WSL2 `Debian`（Hyper-V 第二代虚拟机
`192.168.20.5`）中运行，使用 Conda 环境 `wagtailblog-test`，只允许
保留 `wagtailblog3/settings/.env.test`。测试环境的唯一标准启动入口是
`bash tools/start_test_stack.sh`；脚本统一启动 Django、隔离 maintenance Worker 和隔离 Beat，
网站监听 `0.0.0.0:8080` 并通过 `192.168.20.5:8080` 访问。不要再直接运行
`python manage.py runserver`，也不要用临时 systemd unit 或手工 Celery 命令作为日常启动方式；
这些入口容易遗漏队列和 Redis DB 隔离变量。

每个终端先执行公共准备步骤：

```bash
wsl
cd /mnt/f/openclaw/workspace/wagtail/wagtailblog2
source /root/anaconda3/bin/activate wagtailblog-test

test "$(git branch --show-current)" = "main"
test -f wagtailblog3/settings/.env.test
test ! -f wagtailblog3/settings/.env.production
export WAGTAILBLOG_ENV=test

python manage.py check
```

启动测试网站、maintenance Worker 和 Beat：

```bash
cd /mnt/f/openclaw/workspace/wagtail/wagtailblog2
bash tools/start_test_stack.sh
```

脚本会同时启动监听 `0.0.0.0:8080` 的测试网站、只监听
`markdown-test-maintenance` 的 Worker 和使用同一 broker 的 Beat，并统一使用测试环境队列参数。
运行 PID、Beat 调度文件和日志只写入 Git ignored 的 `output/`。浏览器访问
`http://192.168.20.5:8080/`，后台登录页为
`http://192.168.20.5:8080/admin/login/`。以后测试项目必须使用上述脚本，不要只执行
`python manage.py runserver`，否则组装任务可能进入无人消费的默认队列；userscript 的测试博客地址也必须填写 `http://192.168.20.5:8080`，不能使用 Windows `http://127.0.0.1:8080`。若 8080 已被占用，先定位占用进程，不要通过
反复启动制造多个测试实例。

独立内容搜索的发布唤醒、Delivery 消费和 ScopeJob 也必须使用同一组隔离变量：
`WAGTAILBLOG_ENV=test`、`CELERY_MAINTENANCE_QUEUE=markdown-test-maintenance`、
`CELERY_BROKER_DB=12`、`CELERY_RESULT_DB=13`。网站、Worker 与 Beat 任一进程遗漏这些变量时，
新页面的 Outbox 会停在 `pending`，或任务可能误投生产 `maintenance` 队列；不得以 MySQL 搜索回退掩盖该故障。

下面的 Worker 与 Beat 命令仅用于故障排查；正常启动必须使用脚本。故障排查时必须先停止
脚本管理的三进程，并显式导出与网站一致的隔离变量，禁止与标准测试栈并行运行：

```bash
export WAGTAILBLOG_ENV=test
export CELERY_MAINTENANCE_QUEUE=markdown-test-maintenance
export CELERY_BROKER_DB=12
export CELERY_RESULT_DB=13
/root/anaconda3/envs/wagtailblog-test/bin/python -m celery -A wagtailblog3 worker \
  --pool=solo \
  --without-gossip --without-mingle --without-heartbeat \
  --loglevel=INFO \
  --queues="$CELERY_MAINTENANCE_QUEUE" \
  --hostname=maintenance@test \
  --concurrency=1
```

故障排查需要单独观察定时任务时，以同一组隔离变量启动 Beat：

```bash
export WAGTAILBLOG_ENV=test
export CELERY_MAINTENANCE_QUEUE=markdown-test-maintenance
export CELERY_BROKER_DB=12
export CELERY_RESULT_DB=13
/root/anaconda3/envs/wagtailblog-test/bin/python -m celery -A wagtailblog3 beat --loglevel=INFO
```

`tools/start_test_stack.sh` 使用 `setsid` 为网站、隔离 Worker 和 Beat 创建独立会话；不要改回仅用
`nohup` 的启动方式。脚本按 PID 文件执行 Beat → Worker → 网站的停止顺序，并在端口或未知同名
Worker 已存在时拒绝启动，避免重复消费者。停止测试栈使用
`bash tools/start_test_stack.sh stop`，不要直接按 PID 猜测或强杀进程。Celery 会接管 `SIGHUP` 并尝试自重启，若仍绑定原终端，
Python 3.13 可能在自重启时触发 `celery.__main__` 相对导入错误，导致维护任务无人消费。

测试 Filebeat 只有在已安装并核对 `ops/filebeat/wagtailblog-test.service`、生成的
Filebeat 配置、Elasticsearch 地址以及数据/日志目录后才能启用。该服务不是启动
Django 网站的前置条件。修改或安装该 unit 后必须执行：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now wagtailblog-test.service
sudo systemctl is-active wagtailblog-test.service
sudo systemctl is-enabled wagtailblog-test.service
```

测试环境停止顺序为 Beat、Worker、网站：

```bash
cd /mnt/f/openclaw/workspace/wagtail/wagtailblog2
bash tools/start_test_stack.sh stop
```

脚本只会停止 PID 文件中、工作目录和命令行均匹配的受管进程；发现 PID 被复用或端口由未知进程占用时会拒绝操作，需先人工核对。

## 生产环境启动

生产环境不得执行 `tools/start_test_stack.sh`、`manage.py runserver` 或测试 Celery 命令。
生产唯一启动入口是既有 systemd unit；所有 unit 必须继续读取 `.env.production`，使用生产
`maintenance` 队列和生产 broker/result DB。启动或重启前必须先核对生产目录、分支、精确 commit、
环境文件、依赖服务和当前 unit 状态，再按本节顺序操作。

### 生产关机重启与新搜索前置条件

四个项目服务设置为 `enabled` 和 `Restart=always`，只能保证 systemd 会尝试启动进程，不能保证
MySQL、MongoDB、Redis、MinIO 或 Docker 内的 Elasticsearch 已经完成初始化。新内容搜索运行时的
实际依赖如下：

| 服务 | 启动前必须可用 | 原因 |
| --- | --- | --- |
| `wagtailblog3.service` | MySQL、MongoDB、Redis、MinIO、Elasticsearch | 页面、会话、正文读取、媒体和前台内容搜索都在请求路径上 |
| `wagtailblog3-celery-maintenance.service` | MySQL、MongoDB、Redis、Elasticsearch | Worker 从 MySQL 领取 Delivery、从 MongoDB 读取正式正文并写入 ES |
| `wagtailblog3-celery-beat.service` | MySQL、Redis；内容搜索恢复时还应确认 MongoDB 与 ES | Beat 调度 pending/过期租约补偿，任务最终由 Worker 访问 MongoDB 与 ES |
| `wagtailblog3-filebeat.service` | Elasticsearch | 日志写入 ES；短暂未就绪时 Filebeat 会退避重连 |

生产 Elasticsearch 容器 `elasticsearch8.17.0` 使用 Docker `restart=always`。正确恢复顺序是：

```text
Hyper-V 启动生产虚拟机
  → MySQL / MongoDB / Redis / MinIO / Docker
  → Elasticsearch 9200 返回 yellow 或 green
  → wagtailblog-prod-content-read 唯一指向 serving 索引
  → Django/uWSGI
  → maintenance Worker
  → Beat
  → Filebeat
  → 必要时 Nginx
```

单节点共享集群可以为 `yellow`，不能把全局 `green` 作为启动条件；必须单独检查当前内容索引
健康、read alias 和目标索引。`type=blog` 使用精简内容索引，`type=all/pages` 仍需要当前 Wagtail
Page 索引，因此不得把 Page 索引作为旧内容索引删除。

截至 2026-08-12，生产四个项目 unit 和基础设施 unit 均为 active/enabled，ES 容器也配置了
`restart=always`；但现有项目 unit 主要表达 `After=` 启动顺序，没有以有限超时的 `ExecStartPre`
等待依赖真正健康。服务器启动早期仍可能出现短暂搜索 503、Worker 重试或 Filebeat 连接错误。
建议后续在单独授权的 systemd 变更窗口增加不输出凭据的 readiness 脚本：总超时 120 至 180 秒，
依次检查数据库、缓存、对象存储、ES health、生产 read alias 和 serving 索引；失败退出非零，交由
现有 `Restart=always` 重试。该改造需要先备份 `/etc/systemd/system/wagtailblog3*.service`，再执行
`systemctl daemon-reload`、按依赖顺序重启和完整验收。

生产环境只允许保留 `wagtailblog3/settings/.env.production`，并必须由 systemd 在进程
启动前提供 `WAGTAILBLOG_ENV=production` 和对应的 `EnvironmentFile`。不得在生产环境
使用 `runserver`，不得手动执行 `start.sh`，也不得临时修改 `base.py` 选择环境。

首次安装、unit 文件或环境文件引用发生变化时执行：

```bash
systemctl daemon-reload
systemctl enable \
  wagtailblog3.service \
  wagtailblog3-celery-maintenance.service \
  wagtailblog3-celery-beat.service \
  wagtailblog3-filebeat.service
```

启动前检查代码和环境文件，不输出环境文件内容：

```bash
cd /home/source/Django/wagtail/wagtailblog3
test "$(git branch --show-current)" = "main"
git status --short --branch
test -f wagtailblog3/settings/.env.production
test ! -f wagtailblog3/settings/.env.test
test "$(stat -c '%a' wagtailblog3/settings/.env.production)" = "600"

set -a
. ./wagtailblog3/settings/.env.production
set +a
test "$WAGTAILBLOG_ENV" = "production"
/root/anaconda3/envs/wagtailblog/bin/python manage.py check
```

确认 MySQL、MongoDB、Redis、MinIO、Docker 和 Elasticsearch 等依赖健康后，按照依赖
顺序启动应用服务：

```bash
systemctl start wagtailblog3.service
systemctl start wagtailblog3-celery-maintenance.service
systemctl start wagtailblog3-celery-beat.service
systemctl start wagtailblog3-filebeat.service
```

### Markdown 导入客户端启动确认

浏览器 userscript 只在用户打开文章页面时运行，不是生产服务器上的 systemd 进程，不会随服务器开机自动执行；它提交导入会话后，由下方自动启动的 maintenance Worker 负责组装未发布草稿，Beat 负责过期会话和失败补偿。Windows 客户端 `markdown-importer.exe` 向生产网站上传时，网站、maintenance Worker 和 Beat
必须同时运行；否则媒体虽然可能上传完成，但草稿组装或会话过期补偿会无人处理。生产服务器已将这四个
项目服务设为 `enabled`，并且 maintenance Worker 使用 `Restart=always`。服务器正常开机后，先按上节
顺序启动或确认服务，再执行以下命令：

```bash
systemctl is-active \
  wagtailblog3.service \
  wagtailblog3-celery-maintenance.service \
  wagtailblog3-celery-beat.service \
  wagtailblog3-filebeat.service

/root/anaconda3/envs/wagtailblog/bin/python -m celery -A wagtailblog3 \
  inspect registered -d maintenance@ziliao --timeout=10
```

四个状态都应为 `active`；第二条命令的输出必须包含
`blog.tasks.assemble_markdown_import_session`、`blog.tasks.expire_markdown_import_sessions`、
`blog.tasks.cleanup_markdown_import_artifact` 和
`blog.tasks.dispatch_markdown_import_cleanup_retries`。满足后，客户端才可使用生产地址
`http://192.168.20.2:6050/zh-hans` 和生产后台创建的 `mdimp_...` Token 导入未发布草稿。

Nginx 只有在自身未运行或配置发生变化时才启动或重载：

```bash
nginx -t
systemctl start nginx.service
# 仅在配置已验证且发生变化时：systemctl reload nginx.service
```

停止应用时使用相反顺序，先停止生产流量或进入维护窗口，再停止 Filebeat、Beat、
Worker 和网站；不要为了处理短暂的 Elasticsearch 连接失败连续重启 Filebeat。

## 生产服务常用命令

以下命令可在任意目录以 root 执行：

```bash
systemctl status wagtailblog3.service
systemctl status wagtailblog3-celery-maintenance.service
systemctl status wagtailblog3-celery-beat.service
systemctl status wagtailblog3-filebeat.service

systemctl restart wagtailblog3.service
systemctl restart wagtailblog3-celery-maintenance.service
systemctl restart wagtailblog3-celery-beat.service
systemctl restart wagtailblog3-filebeat.service

systemctl is-active wagtailblog3.service wagtailblog3-celery-maintenance.service wagtailblog3-celery-beat.service wagtailblog3-filebeat.service
systemctl is-enabled wagtailblog3.service wagtailblog3-celery-maintenance.service wagtailblog3-celery-beat.service wagtailblog3-filebeat.service
```

服务文件修改后执行：

```bash
systemctl daemon-reload
systemctl enable wagtailblog3.service wagtailblog3-celery-maintenance.service wagtailblog3-celery-beat.service wagtailblog3-filebeat.service
```

uWSGI 使用 `SIGTERM` 和 `die-on-term=true` 优雅停止。不要把 service 改回
`SIGQUIT`，否则可能触发重载并导致 systemd 停止超时。

## 日志与可观测性

项目文件日志位于 `logs/`，systemd 运行日志通过 journald 查看：

```bash
journalctl -u wagtailblog3.service -f
journalctl -u wagtailblog3-celery-maintenance.service -f
journalctl -u wagtailblog3-celery-beat.service -f
journalctl -u wagtailblog3-filebeat.service -f
```

Elasticsearch 日志索引使用：

```text
wagtailblog-logs-000001
wagtailblog-logs-read
wagtailblog-logs-write
wagtailblog-logs-normalize-v2
```

Filebeat 程序与数据目录：

```text
/home/software/filebeat/current
/home/software/filebeat/config/filebeat.yml
/home/software/filebeat/data
```

`data/` 保存采集 registry，不能在正常维护中删除。Filebeat 重启后 ES 尚未
就绪时会自行退避重试；看到短暂连接错误是正常恢复过程，不要马上重启服务。

Celery Beat 状态文件位于：

```text
/home/source/Django/wagtail/wagtailblog3/logs/celery/celerybeat-schedule
```

如果宿主机异常断电后 Beat 无法启动，先停止 Beat，再将损坏的 schedule 文件
改名留存，最后重新启动 Beat；不要直接删除日志或数据库数据。

### Markdown 导入媒体补偿

Markdown 导入不会新增 systemd unit 或 Worker，复用现有 `maintenance` 队列：

- `blog.tasks.cleanup_markdown_import_artifact(artifact_id)` 只接受明确的 artifact UUID；任务从 `MarkdownImportArtifact` 读取已记录的 storage alias、object name、模型标签和模型 ID，调用精确 cleanup，不按前缀或存储桶扫描。
- `blog.tasks.dispatch_markdown_import_cleanup_retries()` 由 Beat 每 60 秒调用，只查询 `cleanup_status=retry`、`cleanup_attempts` 未达到上限且 `cleanup_next_attempt_at` 已到期（初次没有时间戳也允许投递）的审计行，然后逐个投递 UUID 到 `maintenance` 队列。
- cleanup 成功将状态置为 `cleaned`；对象或模型已不存在视为幂等成功。引用保护、storage 不可用、删除异常会保留 `retry`、错误码、尝试次数和下一次退避时间；达到上限后不再自动投递，审计行保留供人工核查。
- 大批量会话导入复用同一 Worker：`blog.tasks.assemble_markdown_import_session(session_id)` 只处理已完成上传的会话，并可重复投递；`blog.tasks.expire_markdown_import_sessions()` 每 300 秒标记过期会话，再仅按已记录的 artifact UUID 投递精确 cleanup。两者都不扫描 bucket，不创建或发布页面以外的内容。
- 测试环境不得与生产共用该队列：测试通过 `CELERY_MAINTENANCE_QUEUE=markdown-test-maintenance`、`CELERY_BROKER_DB=12`、`CELERY_RESULT_DB=13` 启动独立 Worker；生产 `.env.production` 不设置这些覆盖项，继续使用 `maintenance`、DB 2/3。不同代码版本的 Worker 禁止混消费同一 Celery 队列。
- WSL2 测试启动必须同时启动网站、上述独立 Worker 和 Beat：在仓库根目录执行 `bash tools/start_test_stack.sh`。脚本使用 `output/test-runserver-8080.pid`、`output/test-worker-markdown.pid` 和 `output/test-beat.pid` 管理本次测试进程，网站、Worker 和 Beat 始终共享同一队列环境变量；不要只执行 `manage.py runserver`。
- 部署该功能时必须同时确认 maintenance Worker 已注册这两个任务、Beat 已出现 `expire-markdown-import-sessions`，并在 MySQL、MongoDB、MinIO 和 Redis 可用后再重启 Django、maintenance Worker、Beat。会话迁移、生产限额、MinIO 分片生命周期规则和队列并发均需另行授权。
- 任务依赖 MySQL、配置的对象存储和已有 Wagtail 媒体模型；不读取文章正文，不删除 MongoDB 正文或 revision，不创建页面，不发布内容。

发布包含上述任务或 Beat 路由的代码时，必须按“基础设施 → `wagtailblog3.service` → maintenance Worker → Beat”顺序重启并执行健康检查；本地测试不重启生产服务。回滚到上一个已验证 commit 后，同样恢复这三个受影响服务，已存在的 cleanup retry 审计行不得通过删除数据库记录来掩盖。

### Mongo 正文清理意图补偿

Mongo 正文或 Revision 快照的物理回收不在 `pre_delete` 中执行。页面或 Revision 删除仅在 MySQL 写入 `MongoCleanupIntent`，事务提交后由 `blog.tasks.cleanup_mongo_intent(intent_id)` 在既有 `maintenance` 队列中认领租约并再次核验引用；Beat 每 60 秒运行 `blog.tasks.dispatch_pending_mongo_cleanup_retries()`，补偿 broker 唤醒失败、到期重试和过期租约。

- Worker 只处理 `pending`/`retry` 意图；同一行只能有一个未过期 lease，过期 worker 的意图由 Beat 回收。共享 pointer 仍有 BlogPage 或 Revision 引用时保留为 `retry`，不得直接删除 Mongo。
- Mongo 连接或删除异常记录错误类别、尝试次数和退避时间；达到上限后标记 `dead` 并保留审计记录，禁止以删除意图记录替代故障处理。
- 部署包含此功能的迁移与任务代码前，必须确认 MySQL、MongoDB、Redis 可用，maintenance Worker 已注册 `cleanup_mongo_intent` 与 `dispatch_pending_mongo_cleanup_retries`，Beat 日志出现该周期任务；然后按“Django → maintenance Worker → Beat”顺序重启受影响服务。不得在未应用迁移或 Worker/Beat 代码版本不一致时启用该回收链路。
- 回滚时先停止或回退 Worker/Beat 到前一已验证 commit，并保留 `MongoCleanupIntent` 审计行和所有 Mongo 正文/草稿；不得批量清理意图、Mongo 内容或 Revision 数据。

### 内容搜索同步运维

内容搜索同步只允许向 `ContentSearchTarget` 中已登记且启用的物理索引写入。生产启用
`CONTENT_SEARCH_CONSUMER_ENABLED=true` 前，必须已完成目标索引创建、对应 migration、备份和
独立授权；默认关闭时 Beat 和 Worker 均不读取 Mongo 正文、不创建 Delivery、不写入 Elasticsearch。

以下命令只读取聚合状态和有限 ID，不输出正文、草稿、Mongo 指针、凭据或完整 ES 错误：

```bash
cd /home/source/Django/wagtail/wagtailblog3
set -a
. ./wagtailblog3/settings/.env.production
set +a

/root/anaconda3/envs/wagtailblog/bin/python manage.py shell -c "from django.db.models import Count; from search.models import ContentSearchDelivery; print(list(ContentSearchDelivery.objects.values('target__target_id', 'status').annotate(total=Count('pk')).order_by('target__target_id', 'status')))"
/root/anaconda3/envs/wagtailblog/bin/python manage.py shell -c "from search.models import ContentSearchDelivery, ContentSearchStatus; print(list(ContentSearchDelivery.objects.filter(status=ContentSearchStatus.DEAD).values_list('pk', 'event_id', 'target__target_id')[:20]))"
```

WP3D 提供更严格的精确目标命令。状态和一致性检查始终只读；一致性检查按 `page_id` 游标分批，不能使用
通配符索引。初始化命令默认只预演，`--confirm` 才会在 MySQL 创建缺失的 `ContentSearchState`，不会创建
Outbox/Delivery，也不会写 Mongo 或 Elasticsearch：

```bash
/root/anaconda3/envs/wagtailblog/bin/python manage.py search_sync_status --target TARGET_ID
/root/anaconda3/envs/wagtailblog/bin/python manage.py search_consistency_check --target TARGET_ID --after-page-id 0 --limit 1000 --strict
/root/anaconda3/envs/wagtailblog/bin/python manage.py search_bootstrap_state --target TARGET_ID --after-page-id 0 --limit 100
```

生产执行 `search_bootstrap_state --confirm` 前，必须先完成生产 MySQL 备份、影响范围说明、批次和 checkpoint
确认，并单独授权；命令不会替代正式的索引创建、回填或前台切换流程。

### WP4A 独立内容索引原型

`search_create_content_index` 仅用于测试环境的版本化精简索引原型。默认只输出计划；提供
`--confirm` 后才会创建精确的 ES composable template、物理索引、以及 `building + enabled=false` 的
`ContentSearchTarget/SearchIndexBuild` 记录。它不创建或切换 read alias、不启用 producer/consumer、
不投递 Delivery，也不修改 Mongo 正文：

```bash
cd /mnt/f/openclaw/workspace/wagtail/wagtailblog2
source /root/anaconda3/bin/activate wagtailblog-test
export WAGTAILBLOG_ENV=test

python manage.py search_create_content_index --target content-v001
python manage.py search_create_content_index --target content-v001 --confirm
```

命令拒绝非 `test` 环境、缺少 test 标识的内容索引前缀、通配符/别名式索引名和既有 target/index 覆盖。
共享测试库首次执行前必须已获得 migration 授权并应用 `search.0001` 至 `search.0004`。生产索引、template、
alias 或 target 的创建仍须完成 snapshot、磁盘双份空间、影响说明和单独授权，不能复用此测试命令。

### WP4B 在线回填和增量双投递

### 生产前台切换门禁（历史流程，WP8 发布后不可执行）

生产 `CONTENT_SEARCH_QUERY_ENABLED` 不得先于 read alias 启用。必须先保持该开关为 `false`，以已校验备份目录、精确生产 Target、`ready` Build、零未完成 Delivery 和生产切换开关为前提，使用 `search_switch_production_content_alias` 完成 ES alias 与 Target/Build serving 状态登记；随后才允许启用 query flag 并仅重启 `wagtailblog3.service`。若验收失败，先关闭 query flag 并重启该服务；不删除 alias、物理索引、Outbox、Delivery 或旧 Wagtail 索引。

生产 unit 的唯一环境文件为
`/home/source/Django/wagtail/wagtailblog3/wagtailblog3/settings/.env.production`。独立内容搜索运行时
不得只设置 `CONTENT_SEARCH_PRODUCTION_*`：实际查询、影子读取和同步使用通用
`CONTENT_SEARCH_CONNECTION_NAME`、`CONTENT_SEARCH_INDEX_PREFIX` 和
`CONTENT_SEARCH_READ_ALIAS`。当 Producer、Consumer、shadow、query、cursor、PIT、标题建议或
reconcile 任一开关开启时，这三项必须分别等于生产连接名、生产索引前缀和
`<生产索引前缀>-read`；应用会拒绝不一致配置，防止回退到测试默认值。

每次变更前后仅核对如下白名单，不输出凭据：

```bash
systemctl cat wagtailblog3.service
systemctl show wagtailblog3.service -p EnvironmentFiles
pid=$(systemctl show wagtailblog3.service -p MainPID --value)
tr '\0' '\n' < "/proc/$pid/environ" | grep -E \
  '^(WAGTAILBLOG_ENV|CONTENT_SEARCH_(CONNECTION_NAME|INDEX_PREFIX|READ_ALIAS|QUERY_ENABLED|PRODUCTION_CONNECTION_NAME|PRODUCTION_INDEX_PREFIX|PRODUCTION_QUERY_SWITCH_ENABLED))='
grep -nE '^(CONTENT_SEARCH_(CONNECTION_NAME|INDEX_PREFIX|READ_ALIAS|QUERY_ENABLED|PRODUCTION_CONNECTION_NAME|PRODUCTION_INDEX_PREFIX|PRODUCTION_QUERY_SWITCH_ENABLED))=' \
  wagtailblog3/settings/.env.production
```

`load_dotenv(..., override=False)` 会保留 systemd 或 SSH shell 已有变量；生产管理命令必须使用
`env -i` 提供最小 `PATH`、`HOME`、locale 和 `WAGTAILBLOG_ENV=production`，不能依赖交互 shell
export。切换命令的 dry-run 和 confirm 都必须显示生产 read alias；若返回测试 alias 或任一
`runtime_*_must_match_production_*` 拒绝项，停止，不写 ES alias 或 MySQL Target/Build。该命令不执行
系统重启；服务重启只发生在 alias 成功后开启 query flag 的独立步骤。

`search_rebuild_content_index` 默认只读预演。确认执行前，测试环境必须已经完成 WP4A 的 `search.0001`
至当前迁移、精确目标索引创建、State bootstrap，并同时打开测试用的
`CONTENT_SEARCH_PRODUCER_ENABLED` 与 `CONTENT_SEARCH_CONSUMER_ENABLED`。命令只接受当前环境前缀下的
物理 `ContentSearchTarget`，不接受 read alias 或通配符：

```bash
cd /mnt/f/openclaw/workspace/wagtail/wagtailblog2
source /root/anaconda3/bin/activate wagtailblog-test
export WAGTAILBLOG_ENV=test

python manage.py search_rebuild_content_index --target content-v001 --dry-run
python manage.py search_rebuild_content_index --target content-v001 --batch-size 200 --max-batch-bytes 4194304 --confirm
python manage.py search_rebuild_content_index --target content-v001 --check-catch-up --confirm
```

启动顺序由代码保证为：锁定并启用 `building` target、补齐目标注册后的事件 Delivery、再按公开页面
游标回填。回填使用 Mongo `_id in` 批读和 ES Bulk external version；整批写入成功后才推进 checkpoint，
失败停在原 checkpoint。回填完成只进入 `catching_up`；追平检查连续两次无 pending/processing/retry/dead、
公开页面与 State/ES 版本一致后才进入 `ready`，不会创建或切换 read alias，也不会启用前台独立搜索。
进程崩溃或批量失败使用 `--resume-build` 从 checkpoint 继续；失败目标的物理索引保留，不自动删除。

WP4B 不新增 systemd unit，复用 `wagtailblog3-celery-maintenance.service` 和
`wagtailblog3-celery-beat.service` 的现有 maintenance 队列与补偿任务；本地测试代码变更不要求重启生产
服务。生产执行 migration、创建/写入生产索引、启用双投递、回填、服务重启或切换别名仍需备份、影响说明、
回滚点和单独授权，不能直接复用上述测试命令。

死信或重试必须先修正目标索引、mapping 或外部依赖，再经过生产数据操作确认后精确重放一个
Delivery；不得批量重置 Outbox、Delivery 或 State：

```bash
/root/anaconda3/envs/wagtailblog/bin/python manage.py search_replay_delivery EVENT_UUID TARGET_ID --reason '已修正的原因' --confirm
```

该命令会打印环境、Delivery、事件、目标和物理索引，并拒绝缺少 `--confirm`、consumer 关闭、有效
租约、已成功或已过期的 Delivery。它只重新排队指定行；实际 ES 写入仍由 maintenance Worker 按租约执行。

## 健康检查

在项目目录执行：

```bash
cd /home/source/Django/wagtail/wagtailblog3
set -a
. ./wagtailblog3/settings/.env.production
set +a

/root/anaconda3/envs/wagtailblog/bin/python manage.py check
/root/anaconda3/envs/wagtailblog/bin/python -m celery -A wagtailblog3 inspect ping -d maintenance@ziliao --timeout=10
/root/anaconda3/envs/wagtailblog/bin/python -m celery -A wagtailblog3 inspect registered -d maintenance@ziliao --timeout=10
curl -fsS http://127.0.0.1:9200/_cluster/health
curl -fsS http://127.0.0.1:9200/wagtailblog-logs-read/_count
curl -I -H 'Host: wagtailblog.docs' http://127.0.0.1:6050/admin/login/
```

单节点 Elasticsearch 因其他索引副本未分配而显示 `yellow` 是预期状态；
`wagtailblog-logs-000001` 本身应为 `green`，因为它使用 1 分片、0 副本。

## 发布与静态文件

### Markdown 导入 userscript 与 CORS 重要维护规则

Markdown 导入脚本属于跨站点浏览器入口，新增网站不能只修改 `@match` 或正文选择器，必须在同一批次完成以下配套变更：

- 在 `wagtailblog3/static/vendor/Script/downlaod_markdown.js` 增加精确 `@match`、`InterfaceList` 正文容器和标题规则；正文选择器必须来自真实页面 DOM，不得回退到 `body`。
- 在 `wagtailblog3/settings/base.py` 的 `CORS_ALLOWED_ORIGIN_REGEXES` 增加精确的 HTTP/HTTPS 来源（仅在该网站确实提供 HTTP 页面时保留 HTTP），不得使用任意来源通配符；`CORS_URLS_REGEX` 仍只覆盖 Markdown 导入 API。
- 在 `wagtailblog3/apps/blog/test_markdown_import_cors.py` 和相关 userscript 静态测试中锁定来源、预检头、实际 Bearer 响应、`@match`、正文容器和版本；同步更新 TEST 构建脚本并重新生成 `output/userscript-blog-import/` 下的本地调试副本。
- 当前已登记的人民数据库页面包括 `https://jhsjk.people.cn/article/<id>`；其正文容器为 `.d2txt_con.clearfix`，标题使用 `.d2txt > h1`，页面标题不能作为文章标题回退值。
- 先在 WSL2 `wagtailblog-test` 环境运行 Django 定向测试、`manage.py check`、迁移检查和两个 userscript 的 `node --check`，再提交和发布。浏览器验收必须确认入口、标题、正文、OPTIONS 和实际响应；不得用 401/Token 错误误判为 CORS 失败。

该类改动会影响 Django/uWSGI 的 CORS 配置，生产同步后必须重启 `wagtailblog3.service`；只有实际涉及任务代码或服务配置时才按本文件既有顺序重启 maintenance Worker、Beat、Filebeat。userscript 版本必须递增，在 Tampermonkey 等用户脚本管理器中停用旧副本，避免同一页面运行多个版本。生产发布不创建草稿、session、媒体或 revision，除非另有明确授权。

当前测试和生产目录都按 Git 工作树维护，并使用 `main` 分支。每次部署仍必须先重新
确认生产目录确实是干净且安全的 Git 工作树、远程地址正确，并确定测试通过的精确
commit；不得直接对未知状态的生产目录执行 `git pull`，不得使用 `rsync --delete`。
如果生产目录不再是 Git 工作树，则退回经过校验的文件清单部署方式。

无论采用 Git 还是文件清单部署，都必须保留生产的
`wagtailblog3/settings/.env.production`、`logs/`、`media/`、静态文件目录和运行时
socket/PID 路径。旧版 `observability.env` 只能保留在生产备份目录中，不得重新复制到
项目根目录。环境文件、凭据、日志、媒体和运行数据不得进入 Git。

生产备份统一存放在 `/home/source/Django/wagtail/backups/`：

- MySQL、MongoDB 备份文件直接放在该目录；
- 应用回滚文件使用 `wagtailblog3-YYYYMMDD-HHMMSS/` 子目录；
- 当前已验证部署的回滚点为
  `/home/source/Django/wagtail/backups/wagtailblog3-20260808-190106/`；
- 备份目录禁止使用 `--delete` 全量同步，回滚前先校验文件清单和校验和。

代码切换后，在项目目录执行：

```bash
set -a
. ./wagtailblog3/settings/.env.production
set +a

/root/anaconda3/envs/wagtailblog/bin/python manage.py check
/root/anaconda3/envs/wagtailblog/bin/python manage.py collectstatic --noinput
systemctl restart wagtailblog3.service
systemctl restart wagtailblog3-celery-maintenance.service
systemctl restart wagtailblog3-celery-beat.service
systemctl restart wagtailblog3-filebeat.service
```

只有本次修改确实影响对应组件时才重启该组件；Nginx 配置未变化时不重载 Nginx。
执行迁移前必须另行确认迁移计划、数据库备份和回滚兼容性，不能把迁移混入普通
代码启动命令。

迁移、索引创建、数据库恢复、日志清理和页面发布都会改变生产数据，必须在明确
授权后单独执行。不要把 BlogPage 正文、MongoDB 草稿或 revision pointer 当作
普通 MySQL 字段处理。

## 重启验收

服务器重启后，先等待 Docker 和 Elasticsearch 完成恢复，再检查：

```bash
systemctl --failed --no-pager
systemctl is-active mysqld.service redis.service mongodb-home.service minio.service docker.service nginx.service
systemctl is-active wagtailblog3.service wagtailblog3-celery-maintenance.service wagtailblog3-celery-beat.service wagtailblog3-filebeat.service
docker ps --format '{{.Names}} {{.Status}}'
```

随后执行“健康检查”章节命令。ES 启动期间 Filebeat 可能先输出连接错误；当日志
中出现 `Connection to backoff ... established` 后，采集会自动恢复。

## 生产内容搜索历史运行基线（2026-08-12）

- 生产代码 SHA：`0f2a55ad329329ce19289c2b668ce27c3aa5a8b5`。
- 前台搜索：`CONTENT_SEARCH_QUERY_ENABLED=true`，read alias `wagtailblog-prod-content-read` 唯一指向 `wagtailblog-prod-content-v002`。
- 同步：producer/consumer 开启；v002 Target 为 enabled/serving；v001 Target 为 disabled/retired，历史 Delivery 保留。
- 一次性门禁：影子读、生产索引创建、生产重建和生产查询切换均关闭；`.env.production` 不再保留 v001 shadow target。
- Elasticsearch：继续使用唯一 `elasticsearch8.17.0` 容器，不停止、不删除数据卷；v002 为 1 primary/0 replica、green。
- 已删除资源：v001、v001 template、误生成内容索引、陈旧空索引和无 alias 的测试命名页面索引。删除前 snapshot 均为 SUCCESS。
- 必须保留：v002、v002 template、`wagtailblog-logs-000001` 及日志 alias、`wagtailblogwagtailcore_page_dz66xgy`、MySQL 搜索审计记录和 MongoDB 正文/草稿/revision。
- 恢复点：`/home/source/Django/wagtail/backups/wagtailblog3-pre-search-20260812-181627/`；快照 `pre-retire-old-content-20260812-181627` 与 `pre-delete-test-residual-20260812-182144`。

日常检查至少包含：四个项目服务 active/enabled、failed unit 为 0、生产搜索 HTTP 200、alias 只指向 v002、v002 green、Delivery 无 pending/processing/retry/dead。共享单节点因其他 Wagtail 索引副本未分配显示 yellow 时，不得通过删除未知索引处理。

Hyper-V 虚拟机本身是否随 Windows 主机自动启动，不由 Linux systemd 决定。当前普通 Windows
PowerShell 无权读取 `Get-VM`，因此尚未核实生产虚拟机 `ziliao` 的 `AutomaticStartAction`；需要在
Windows 管理员 PowerShell 中单独确认。

## WP8 code-only 发布服务规则（待执行）

WP8 只删除应用内影子与旧 DSL 代码，不新增服务、不改 unit、不改端口、不改 Elasticsearch、数据库或 Filebeat 配置。生产发布前仍需核对 MySQL、MongoDB、Redis、MinIO、Docker 和 Elasticsearch 已可用，以及生产内容 read alias 只指向当前 serving 索引。

本批仅依次重启 `wagtailblog3.service`、`wagtailblog3-celery-maintenance.service`、`wagtailblog3-celery-beat.service`。不重启 `wagtailblog3-filebeat.service`、Nginx、Elasticsearch、MySQL、MongoDB、Redis 或 MinIO；若任一已重启服务未恢复 active，停止后续操作并按发布前代码 SHA 回滚。

`CONTENT_SEARCH_QUERY_ENABLED`、`CONTENT_SEARCH_FEDERATED_ALL_ENABLED` 与 `CONTENT_SEARCH_SHADOW_*` 是 WP8 前的历史开关。WP8 部署后 Django 不再读取它们；不得将其写入 unit、drop-in 或开机恢复脚本。producer/consumer、内容 alias、maintenance Worker 和 Beat 的既有启动依赖保持不变。

访问限制变更会在事务提交后创建 `ContentSearchScopeJob`，由 Beat 每 30 秒向 `maintenance` 队列投递 `search.tasks.dispatch_pending_content_search_scope_jobs`，Worker 执行 `search.tasks.consume_content_search_scope_job`。该任务按 BlogPage 子树和主键检查点重算公开状态，生成必要的 tombstone/upsert；租约过期可重试，失败仅记录脱敏错误。部署包含该任务或迁移时需确认 maintenance Worker、Beat 均已注册新任务并按既有顺序重启；回滚时停止 Worker/Beat，保留 ScopeJob、State 与 Outbox，不清理任务或搜索索引。

## Wagtail 8.0 与正文生命周期迁移 runbook

本节前半适用于 Wagtail 7.4.3 -> 8.0 的依赖、迁移和默认 Page 搜索索引升级；根据 `说明书/27-Wagtail 8.0升级可行性方案.md` 的实施记录，Wagtail 8.0 升级已在 2026-08-27 完成。正文生命周期迁移（`blog.0029`-`blog.0033`、`search.0006`-`search.0007`）仍未执行，不能把历史 Wagtail 8 部署记录视为本批迁移授权；现场状态必须以生产 `showmigrations`、commit、服务和数据库检查为准。

升级前必须在隔离环境完成依赖安装、`pip check`、`manage.py check`、迁移计划审阅和受影响测试；生产数据库、MongoDB 正文/草稿/revision、媒体和 Elasticsearch 必须先完成可恢复备份。Wagtail 8 的 `wagtailcore.0098_apitoken` 只允许在已审阅的维护窗口执行，禁止把 API token 表或迁移回滚作为默认删除动作。

Wagtail 默认 Page 索引更新必须在旧 7.4.3 代码下先执行并记录 `update_index --backend default`，再升级代码后复核；该命令会重建 default 后端登记的多个 Wagtail 模型索引，不得误认为只写 Page，也不得触碰 `wagtailblog-prod-content-v002` 等自建内容索引或日志索引。任何 alias 切换、内容索引重建或 outbox 批量处置均需单独授权。

维护顺序为：确认 MySQL、MongoDB、Redis、MinIO、Docker、Elasticsearch 可用并冻结应用写入；停止 `wagtailblog3.service`、`wagtailblog3-celery-maintenance.service`、`wagtailblog3-celery-beat.service`；安装已验证依赖并执行已审阅迁移；按“基础设施 -> Django/uWSGI -> maintenance Worker -> Beat -> Filebeat（仅日志格式变更时） -> Nginx（仅实际受影响时）”顺序恢复。任一检查失败即停止后续启动，切回上一个已验证 commit 与旧依赖环境；不删除 serving 索引、MongoDB 正文或 revision 数据。

本项目正文生命周期改造的生产迁移对象为 `blog.0029`-`blog.0033`、`search.0006`-`search.0007`；是否包含 `wagtailcore.0098` 必须以生产 `showmigrations` 结果为准。执行前必须完成 MySQL（含 schema、数据、triggers、routines、events）、Mongo 正文/草稿/revision、Elasticsearch snapshot 和 systemd/env 清单备份及恢复演练，并取得独立迁移授权。迁移只改变 MySQL schema，不自动回填正文、删除 Mongo、重建 ES 或切换 alias；失败时停止后续服务恢复，保留新增表/列和备份，禁止未经数据库负责人确认执行反向迁移。
### BlogPage 发布一致性只读对账

Beat 每 300 秒向 `maintenance` 队列投递 `blog.tasks.check_publication_consistency`。该任务按 `BLOG_PUBLICATION_CONSISTENCY_BATCH_SIZE` 限制 BlogPage 批次，读取 BlogPage、BlogPublicationState、Revision、Search State/Outbox；周期模式只更新独立的 `BlogPublicationConsistencyCheckpoint` 游标、high-water、租约和统计元数据，不执行业务数据修复、删除、发布或外部写入。checkpoint 使用 MySQL 行锁租约，租约冲突返回 `lease_busy`，扫描异常释放租约并记录脱敏错误类型；需监控 `last_error`、租约过期和周期推进。启用前需先应用对应迁移（当前为 `blog.0033`）并确认 maintenance Worker 已注册任务；回滚时先停 Beat/Worker，再恢复上一个已验证代码版本，保留 checkpoint 与业务 State/Outbox 数据。

页面不可恢复删除编排：`blog.tasks.process_page_deletion` 和 `blog.tasks.dispatch_page_deletion_retries` 仅使用 `maintenance` 队列。删除入口先在 MySQL 创建 `PageDeletionIntent`、State tombstone 和搜索 Outbox；Worker 等待当前 serving alias 的 ES tombstone 成功后，按固化清单检查其他页面引用，再精确清理 `content_body_versions`、`blog_page_revision_bodies` 和兼容 `blog_content`，最后完成 Wagtail 页面物理删除。Beat 每 30 秒投递到期或租约过期的页面删除意图；状态、step、lease、已删除计数和错误码必须纳入日志与告警。部署前需先应用 `blog.0035`、`blog.0036`，确认 maintenance Worker 注册新任务并在测试环境完成新增→草稿→编辑→发布→搜索→删除闭环；生产 `--apply`、历史孤儿清理和服务重启仍需独立备份及明确授权。回滚只回退代码/服务，不恢复已物理删除的 Mongo 正文。
