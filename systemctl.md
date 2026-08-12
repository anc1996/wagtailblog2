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
| `wagtailblog3-celery-maintenance.service` | 日志索引同步和维护队列；可执行博客分析明细清理；内容搜索 Delivery 的租约消费与重试 | 是 |
| `wagtailblog3-celery-beat.service` | 补偿日志索引 outbox、内容搜索 pending/过期租约 Delivery，并每日调度博客分析明细清理检查 | 是 |
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

### WP6 测试独立 Elasticsearch 集群

WP6 测试集群运行在 WSL2 测试主机 `192.168.20.5`，不连接或替代 `192.168.20.2:9200` 的共享
测试 Elasticsearch，更不属于生产集群。选择该主机是因为 `192.168.20.2` 核验时可用内存约
3.1 GiB 且已有持续 swap，继续增加第二个 JVM 会影响共享 MySQL、MongoDB、Redis 和旧搜索。

| 项目 | 测试值 |
| --- | --- |
| 容器 | `wagtailblog-test-search-wp6-es` |
| 镜像 | `elasticsearch:8.17.0` |
| 地址 | `https://192.168.20.5:9210` |
| 集群 | `wagtailblog-test-content-secondary`，单节点 |
| 资源 | 容器上限 1.5 GiB；JVM heap 768 MiB |
| 重启策略 | `unless-stopped` |
| 数据卷 | `wagtailblog-test-search-wp6-data` |
| 证书卷 | `wagtailblog-test-search-wp6-certs-v2`；旧 `-certs` 卷仅作为测试回溯证据保留 |
| 密钥卷 | `wagtailblog-test-search-wp6-secrets` |
| 快照卷 | `wagtailblog-test-search-wp6-snapshots`，仓库名 `wagtailblog-test-wp6-repository` |
| 网络 | `wagtailblog-test-search-wp6` |

HTTP 和 transport 均启用 TLS；应用使用限制在 `wagtailblog-test-secondary-content-*` 的 API key。
该 key 可执行内容索引所需的读写和 template 管理，但无 snapshot 管理权限；snapshot 创建和恢复
使用独立管理员入口，避免应用凭据同时拥有备份删除能力。证书私钥、管理员凭据和 API key 仅放在
Git ignored 的测试运行目录或 Docker volume，不得写入 `.env.test`、本文、Git 或命令输出。

测试集群常用命令：

```bash
docker start wagtailblog-test-search-wp6-es
docker stop wagtailblog-test-search-wp6-es
docker restart wagtailblog-test-search-wp6-es
docker logs --tail 100 wagtailblog-test-search-wp6-es

cd /mnt/f/openclaw/workspace/wagtail/wagtailblog2
output/wp6/django-secondary.sh search_cluster_preflight \
  --connection content_secondary \
  --index wagtailblog-test-secondary-content-v001 \
  --strict
```

停止容器不会删除数据。不得把 `docker rm`、`docker volume rm`、索引删除、snapshot 删除或证书清理
作为普通回滚；这些清理操作必须先核对复现证据和回滚窗口并单独确认。应用读取回滚只需恢复
`CONTENT_SEARCH_CONNECTION_NAME=default` 并关闭新查询 flag，观察期继续保留旧、新目标双投递。
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
`restart=always`；WP6 测试容器按上一节使用 `unless-stopped`，两者不得混用。

不要启动只消费 `email` 或 `default` 队列的 Celery Worker，除非已经检查
Redis 中没有历史邮件任务，并明确需要处理这些任务。当前 Worker 只监听
`maintenance` 队列。

## 测试环境启动

测试环境当前在 Windows 主机 `192.168.20.1` 的 WSL2 `Debian`（Hyper-V 第二代虚拟机
`192.168.20.5`）中运行，使用 Conda 环境 `wagtailblog-test`，只允许
保留 `wagtailblog3/settings/.env.test`。测试网站、Worker 和 Beat 当前不由仓库内的
systemd unit 管理，需要在不同终端前台启动；关闭对应终端或按 `Ctrl+C` 即可停止。

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

启动测试网站：

```bash
python manage.py runserver 0.0.0.0:8000
```

浏览器访问 `http://127.0.0.1:8000/`，后台登录页为
`http://127.0.0.1:8000/admin/login/`。若 8000 已被占用，先定位占用进程，不要通过
反复启动制造多个测试实例。

需要验证异步维护任务时，在第二个终端完成公共准备步骤后启动只监听
`maintenance` 队列的 Worker：

```bash
python -m celery -A wagtailblog3 worker \
  --loglevel=INFO \
  --queues=maintenance \
  --hostname=maintenance@test \
  --concurrency=1
```

需要验证定时任务和失败补偿时，在第三个终端完成公共准备步骤后启动 Beat：

```bash
python -m celery -A wagtailblog3 beat --loglevel=INFO
```

测试 Filebeat 只有在已安装并核对 `ops/filebeat/wagtailblog-test.service`、生成的
Filebeat 配置、Elasticsearch 地址以及数据/日志目录后才能启用。该服务不是启动
Django 网站的前置条件。修改或安装该 unit 后必须执行：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now wagtailblog-test.service
sudo systemctl is-active wagtailblog-test.service
sudo systemctl is-enabled wagtailblog-test.service
```

测试环境停止顺序为 Beat、Worker、网站；前台进程均使用 `Ctrl+C` 优雅停止。

## 生产环境启动

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

### 生产前台切换门禁

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

## 生产内容搜索最终运行基线（2026-08-12）

- 生产代码 SHA：`c9054598417a2fb43f5a517a3d68bb135314fcb9`。
- 前台搜索：`CONTENT_SEARCH_QUERY_ENABLED=true`，read alias `wagtailblog-prod-content-read` 唯一指向 `wagtailblog-prod-content-v002`。
- 同步：producer/consumer 开启；v002 Target 为 enabled/serving；v001 Target 为 disabled/retired，历史 Delivery 保留。
- 一次性门禁：影子读、生产索引创建、生产重建和生产查询切换均关闭；`.env.production` 不再保留 v001 shadow target。
- Elasticsearch：继续使用唯一 `elasticsearch8.17.0` 容器，不停止、不删除数据卷；v002 为 1 primary/0 replica、green。
- 已删除资源：v001、v001 template、误生成内容索引、陈旧空索引和无 alias 的测试命名页面索引。删除前 snapshot 均为 SUCCESS。
- 必须保留：v002、v002 template、`wagtailblog-logs-000001` 及日志 alias、`wagtailblogwagtailcore_page_dz66xgy`、MySQL 搜索审计记录和 MongoDB 正文/草稿/revision。
- 恢复点：`/home/source/Django/wagtail/backups/wagtailblog3-pre-search-20260812-181627/`；快照 `pre-retire-old-content-20260812-181627` 与 `pre-delete-test-residual-20260812-182144`。

日常检查至少包含：四个项目服务 active/enabled、failed unit 为 0、生产搜索 HTTP 200、alias 只指向 v002、v002 green、Delivery 无 pending/processing/retry/dead。共享单节点因其他 Wagtail 索引副本未分配显示 yellow 时，不得通过删除未知索引处理。
