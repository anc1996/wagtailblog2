# wagtailblog3 生产与测试运维维护手册

> 更新日期：2026-09-09。本文档为系统服务启动、重启、配置联动、发布门禁与故障排查的唯一标准操作手册。历史废弃命令、弃用索引与旧备份路径不再作为操作依据。

---

## 1. 环境拓扑与存储边界

| 环境 | 主机 / IP | 项目源码路径 | Python Conda 环境 | 环境变量与配置文件 |
| :--- | :--- | :--- | :--- | :--- |
| **测试** | WSL2 (192.168.20.5) | `/mnt/f/openclaw/workspace/wagtail/wagtailblog2` | `/root/anaconda3/envs/wagtailblog-test` | `WAGTAILBLOG_ENV=test`<br>`wagtailblog3/settings/.env.test` |
| **生产** | 虚拟机 (192.168.20.2, 主机名 `ziliao`) | `/home/source/Django/wagtail/wagtailblog3` | `/root/anaconda3/envs/wagtailblog` | `WAGTAILBLOG_ENV=production`<br>`wagtailblog3/settings/.env.production` |

### 核心安全与隔离边界
1. **主分支唯一性**：代码主分支严格为 `main`（远程 `origin`），生产环境只允许通过快进合并（`git merge --ff-only`）部署已测试验证并推送到远程的 Commit SHA；
2. **严禁跨环境串台**：测试与生产环境严禁互用环境变量文件、Redis 数据库、Celery 队列、MinIO Bucket 或 MongoDB 数据库；
3. **同机多系统避让**：生产机宿主同时运行有 `shop`（商城系统），博客系统必须严格使用专属分配的 Redis 数据库，严禁向商城的 DB 2、3、4 投递未隔离的队列或缓存；
4. **核心资产受保护**：生产 MySQL、MongoDB 正文、Revision 历史、ES 索引、MinIO 媒体均属绝对受保护资产，严禁未经对话人明确授权执行 `flush`、`drop`、物理批量删除或破坏性重建。

---

## 2. Redis 数据库分配与连带配置映射

生产单实例 Redis（`192.168.20.2:6379`）已做严格物理划分，系统变更时严禁随意变更库号：

| 物理库 | 分配系统 | 角色用途 | 统一前缀协议 | 关联配置项 (settings/.env) |
| :--- | :--- | :--- | :--- | :--- |
| **DB 1** | **系统保留** | 原博客缓存库（已平滑迁移腾空，备用） | — | 历史闲置 |
| **DB 2~4**| **商城专用** | 商城系统 Session、验证码、历史、购物车 | (shop 独占) | 博客系统**严禁使用** |
| **DB 5** | **博客测试** | 测试环境核心缓存 | `wblog:test:...` | `.env.test` -> `REDIS_CACHE_DB=5` |
| **DB 6** | **博客测试** | 测试环境评论频率限制缓存 | `wblog:test:rate:...` | `.env.test` -> `REDIS_COMMENT_CACHE_DB=6` |
| **DB 7** | **博客测试** | 测试环境 Celery Broker 队列 | `wblog:test:broker:` | `.env.test` -> `CELERY_BROKER_DB=7` |
| **DB 8** | **博客测试** | 测试环境 Celery Result 结果 | `wblog:test:result:` | `.env.test` -> `CELERY_RESULT_DB=8` |
| **DB 10**| **商城专用** | 商城系统 Celery 任务结果存储后端 | (shop 独占) | 博客系统**严禁使用** |
| **DB 12**| **博客生产** | 生产环境评论/访问限流专属缓存 | `wblog:prod:rate:...` | `.env.production` -> `REDIS_COMMENT_CACHE_DB=12` |
| **DB 13**| **博客生产** | 生产环境核心业务缓存 (default cache) | `wblog:prod:...` | `.env.production` -> `REDIS_CACHE_DB=13` |
| **DB 14**| **博客生产** | 生产环境 Celery Broker 消息代理 | `wblog:prod:broker:` | `.env.production` -> `CELERY_BROKER_DB=14` |
| **DB 15**| **博客生产** | 生产环境 Celery Result 任务结果 | `wblog:prod:result:` | `.env.production` -> `CELERY_RESULT_DB=15` |

---

## 3. 生产 4 大应用服务与连带启动依赖矩阵

生产环境承载博客业务的 4 大核心 systemd 应用单元：

1. **`wagtailblog3.service`**：uWSGI Web 核心，处理动态 HTTP 请求，生成 unix socket 并监听内部调试端口 `6051`；
2. **`wagtailblog3-celery-maintenance.service`**：维护 Worker，消费 `maintenance` 队列（搜索索引 Delivery、页面删除、Mongo 补偿）；
3. **`wagtailblog3-celery-beat.service`**：定时调度器，调度周期性补偿任务，依赖 schedule 文件；
4. **`wagtailblog3-filebeat.service`**：日志收集器，收集应用运行日志并推送到 Elasticsearch。

### 连带依赖拓扑图 (Service Dependency Topology)

```
[底层基础服务层 (P0)]
  mysqld.service ──┐
  redis.service  ──┼──> [核心 Web 应用层 (P1)] ──> [反向代理 (P1)]
  mongodb-home   ──┤     wagtailblog3.service         nginx.service (6050)
  minio.service  ──┘         │
  elasticsearch  ────────────┼──> [后台异步队列层 (P2)]
                             │     wagtailblog3-celery-maintenance.service
                             │         ▲
                             │         │ (任务投递与消费)
                             └──> [定时调度器 (P3)]
                                   wagtailblog3-celery-beat.service
```

### 连带变更触发矩阵 (Cascade Action Matrix)

| 变更类型 | 必须连带重启的服务列表 | 是否需要静态收集 | 验证核验命令 |
| :--- | :--- | :--- | :--- |
| **仅模板 / CSS / JS 改动** | `wagtailblog3.service` | **是** (`collectstatic --noinput`) | 验证页面静态版本号 `?v=...` 与无 502 |
| **Python 代码 / View / 缓存逻辑** | `wagtailblog3.service` | 否 (若无静态变更) | 预检首屏、列表页与侧边栏响应时间 |
| **异步任务逻辑 (`tasks.py`)** | `wagtailblog3-celery-maintenance.service` | 否 | `celery inspect ping` 确认存活 |
| **定时任务周期 (`BEAT_SCHEDULE`)** | `wagtailblog3-celery-beat.service` | 否 | 检查 `celerybeat-schedule` 是否更新 |
| **环境变量 / Redis 分库变更** | **4 大服务全量平滑重启**<br>(uWSGI -> maintenance -> beat) | 否 | 检查各服务 `is-active` 并验证对应 Redis 库出现新键 |
| **系统版本发布 (Git Merge)** | **4 大服务全量平滑重启** | **是** (`collectstatic --noinput`) | 全套 Maker-Checker 流程验收 |

---

## 4. 生产标准操作指令表

### 4.1 基础依赖连通性核验
在启动或重启博客应用前，必须先确认底层存储服务全部健康：
```bash
systemctl is-active mysqld.service redis.service mongodb-home.service minio.service nginx.service
curl --fail --silent --max-time 3 http://127.0.0.1:9200/_cluster/health | grep -q '"status":"green"\|"status":"yellow"' && echo "ES 健康"
```

### 4.2 生产代码发布与平滑重启标准流 (Maker-Checker)
```bash
# 1. 切换至生产目录并核对当前分支
cd /home/source/Django/wagtail/wagtailblog3
test "$(git branch --show-current)" = "main"
test -z "$(git status --porcelain)"

# 2. 拉取远端主分支并执行快进合并
git fetch origin --prune
OLD_SHA=$(git rev-parse HEAD)
git merge --ff-only origin/main
NEW_SHA=$(git rev-parse HEAD)
echo "版本升级: $OLD_SHA -> $NEW_SHA"

# 3. 生产只读静态检查 (严禁执行 migrate)
source /root/anaconda3/bin/activate wagtailblog
export WAGTAILBLOG_ENV=production
python manage.py check
python -m compileall -q wagtailblog3

# 4. 收集静态资源 (严禁带 --clear 参数)
python manage.py collectstatic --noinput

# 5. 按照拓扑依赖顺序连带平滑重启应用服务
# 5.1 重启核心 Web
systemctl restart wagtailblog3.service
systemctl is-active wagtailblog3.service

# 5.2 首屏防 502 预检 (验证 uWSGI worker 已经就绪)
sleep 1
curl --fail --silent --show-error --max-time 15 'http://127.0.0.1:6050/zh-hans/公文材料/政务公开题材/' > /dev/null
echo "首屏动态响应预检通过"

# 5.3 重启异步 Worker 与定时调度
systemctl restart wagtailblog3-celery-maintenance.service
systemctl is-active wagtailblog3-celery-maintenance.service

systemctl restart wagtailblog3-celery-beat.service
systemctl is-active wagtailblog3-celery-beat.service

# 5.4 日志服务 (仅在日志规则变更时重启)
systemctl is-active wagtailblog3-filebeat.service || systemctl restart wagtailblog3-filebeat.service
```

### 4.3 生产服务连通性与性能验收命令
```bash
# 1. 检查各服务运行状态
systemctl is-active wagtailblog3.service wagtailblog3-celery-maintenance.service wagtailblog3-celery-beat.service wagtailblog3-filebeat.service

# 2. 检查端口与监听状态
ss -ltnp | grep -E ':6050|:6051'

# 3. 检查 Celery 节点探测
/root/anaconda3/envs/wagtailblog/bin/python -m celery -A wagtailblog3 inspect ping -d maintenance@ziliao --timeout=5

# 4. 验证分类列表页与侧边栏渲染 (检查是否包含近3年加号与总文章数)
curl -s 'http://127.0.0.1:6050/zh-hans/公文材料/政务公开题材/' | grep -E '1134|archive-year-list' | head -n 5
```

---

## 5. 测试环境运行与维护规范

测试环境完全独立于 WSL2 内部，唯一正规启动入口是仓库存放的守护脚本：

```bash
# 启动测试环境完整协议栈 (Web 8080 + 隔离 Worker + 隔离 Beat)
cd /mnt/f/openclaw/workspace/wagtail/wagtailblog2
source /root/anaconda3/bin/activate wagtailblog-test
export WAGTAILBLOG_ENV=test
bash tools/start_test_stack.sh

# 停止测试环境协议栈
bash tools/start_test_stack.sh stop
```

### 测试环境门禁校验清单
在每次向 `origin/main` 提交代码前，必须在 WSL2 下跑通：
```bash
python manage.py check
python tools/check_css_tokens.py
python manage.py test --keepdb blog.tests.test_blog_index_async archive.tests
python -m compileall wagtailblog3/
git diff --check
```

---

## 6. 故障排查与应急回滚流程 (Emergency Rollback)

若生产更新后页面出现 500、502 或 Worker 任务堆积异常，**立即执行 10 秒级快速回滚**：

```bash
# 1. 切回旧版本 SHA
cd /home/source/Django/wagtail/wagtailblog3
git reset --hard "$OLD_SHA"

# 2. 重新收集静态文件
/root/anaconda3/envs/wagtailblog/bin/python manage.py collectstatic --noinput

# 3. 连带重启应用服务
systemctl restart wagtailblog3.service
systemctl restart wagtailblog3-celery-maintenance.service
systemctl restart wagtailblog3-celery-beat.service

# 4. 检查服务恢复与错误日志
systemctl is-active wagtailblog3.service
journalctl -u wagtailblog3.service -n 50 --no-pager
```

---

## 7. 维护历史与变更记录

- **2026-09-09**：
  - 全面更新 Redis 多应用多环境物理库隔离拓扑（明确划分 DB 1、5~8、11、14、15，避让商城 DB 2~4）；
  - 建立 4 大服务连带启动依赖矩阵与配置变更触发联动表；
  - 补齐首屏防 502 自动化预检逻辑与侧边栏渲染验收标准；
  - 固化测试栈与生产环境的 Maker-Checker 发布门禁。
- **2026-09-01**：清理生产历史备份目录，重写服务启停与回滚流程。
