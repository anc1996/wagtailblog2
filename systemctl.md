# wagtailblog3 当前维护手册

> 更新日期：2026-09-01。本文只保留当前有效的测试、生产启动和维护流程；历史命令、废弃索引和旧备份路径不再作为操作依据。

## 1. 环境与边界

| 环境 | 主机 | 项目目录 | Python | 配置 |
| --- | --- | --- | --- | --- |
| 测试 | WSL2 192.168.20.5 | /mnt/f/openclaw/workspace/wagtail/wagtailblog2 | /root/anaconda3/envs/wagtailblog-test | WAGTAILBLOG_ENV=test、.env.test |
| 生产 | 192.168.20.2（ziliao） | /home/source/Django/wagtail/wagtailblog3 | /root/anaconda3/envs/wagtailblog | WAGTAILBLOG_ENV=production、.env.production |

- Git 分支统一为 main，生产只能部署已验证并推送到 origin/main 的 SHA。
- 测试和生产不得互用环境文件、Redis DB、Celery 队列或 MongoDB 数据库。
- 不在生产执行 manage.py runserver、tools/start_test_stack.sh 或测试队列命令。
- 生产正文、草稿、Revision、MongoDB、ES 索引和备份均属于受保护数据；清理、迁移、alias 切换必须单独授权。

## 2. 测试环境启动与停止

测试环境唯一启动入口是仓库脚本，它会同时启动网站、隔离 maintenance Worker 和 Beat：

```bash
cd /mnt/f/openclaw/workspace/wagtail/wagtailblog2
source /root/anaconda3/bin/activate wagtailblog-test
export WAGTAILBLOG_ENV=test
test "$(git branch --show-current)" = "main"
test -f wagtailblog3/settings/.env.test
test ! -f wagtailblog3/settings/.env.production
bash tools/start_test_stack.sh
```

启动结果：

- 网站：http://192.168.20.5:8080
- Worker 队列：markdown-test-maintenance
- Redis broker/result DB：12/13
- PID、日志和 Beat schedule：output/test-*.pid、output/test-*.log、output/test-celerybeat-schedule

停止或重启：

```bash
bash tools/start_test_stack.sh stop
bash tools/start_test_stack.sh
```

禁止只启动网站；否则草稿组装、Mongo 清理和搜索 Delivery 可能无人消费。脚本会拒绝占用中的 8080、未知 Worker 和不属于本仓库的 PID。

测试检查：

```bash
source /root/anaconda3/bin/activate wagtailblog-test
export WAGTAILBLOG_ENV=test
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test blog search archive --keepdb --noinput
python -m compileall -q wagtailblog3
git diff --check
```

测试日志：

```bash
tail -f output/test-runserver-8080.log
tail -f output/test-worker-markdown.log
tail -f output/test-beat.log
```

## 3. 生产依赖与服务

生产应用服务：

- wagtailblog3.service：uWSGI/Django，读取 .env.production；生成 socket /home/source/Django/wagtail/wagtailblog3/wagtailblog3.sock 并监听 HTTP 调试端口 6051。
- Nginx 反向代理：监听端口 6050，通过 unix socket 转发至 uWSGI，静态资源 /static/ 映射至 staticfiles_collected/。
- wagtailblog3-celery-maintenance.service：只消费 maintenance 队列，处理搜索 Delivery、Mongo 清理、页面删除和补偿任务。
- wagtailblog3-celery-beat.service：调度补偿和周期任务，schedule 位于 logs/celery/celerybeat-schedule。
- wagtailblog3-filebeat.service：采集项目日志到 Elasticsearch。

基础依赖必须实际可用：MySQL mysqld.service、Redis redis.service、MongoDB mongodb-home.service、MinIO minio.service、Docker docker.service、Nginx nginx.service，以及本机 Elasticsearch HTTP 127.0.0.1:9200。

## 4. 生产开机启动

先确认基础设施和内容搜索 read alias，再启动应用服务：

```bash
systemctl is-active mysqld.service redis.service mongodb-home.service minio.service docker.service nginx.service
curl --fail --silent http://127.0.0.1:9200/_cluster/health
```

确认生产仓库：

```bash
cd /home/source/Django/wagtail/wagtailblog3
test "$(git branch --show-current)" = "main"
test -f wagtailblog3/settings/.env.production
test ! -f wagtailblog3/settings/.env.test
git status --short --branch
```

启动顺序：

```bash
systemctl start wagtailblog3.service
systemctl start wagtailblog3-celery-maintenance.service
systemctl start wagtailblog3-celery-beat.service
systemctl start wagtailblog3-filebeat.service
```

验证：

```bash
systemctl is-active wagtailblog3.service wagtailblog3-celery-maintenance.service wagtailblog3-celery-beat.service wagtailblog3-filebeat.service
systemctl is-enabled wagtailblog3.service wagtailblog3-celery-maintenance.service wagtailblog3-celery-beat.service wagtailblog3-filebeat.service
ss -ltnp | grep -E ':6050|:6051'
curl --fail --silent --head https://<生产域名>/
source /root/anaconda3/bin/activate wagtailblog
export WAGTAILBLOG_ENV=production
python manage.py check
```

## 5. 生产代码发布与重启

仅当测试通过且用户明确授权时执行。生产仓库不干净或不是 main 时停止：

```bash
cd /home/source/Django/wagtail/wagtailblog3
git status --short --branch
git fetch origin --prune
git diff --name-status HEAD..origin/main
git merge --ff-only origin/main
git rev-parse HEAD
```

仅测试文件或文档变化：不重启服务。运行时代码、依赖、迁移、队列或服务配置变化：按以下顺序重启并逐项验收：

```bash
systemctl restart wagtailblog3.service
systemctl restart wagtailblog3-celery-maintenance.service
systemctl restart wagtailblog3-celery-beat.service
systemctl restart wagtailblog3-filebeat.service
```

若任一服务未恢复 active，立即停止后续操作，查看 journalctl -u <unit> -n 200 --no-pager，回退到上一个已验证 SHA；不得删除 Mongo 正文或 ES serving 索引。

## 6. 日志与队列排障

```bash
journalctl -u wagtailblog3.service -n 200 --no-pager
journalctl -u wagtailblog3-celery-maintenance.service -n 200 --no-pager
journalctl -u wagtailblog3-celery-beat.service -n 200 --no-pager
journalctl -u wagtailblog3-filebeat.service -n 200 --no-pager
/root/anaconda3/envs/wagtailblog/bin/python -m celery -A wagtailblog3 inspect ping -d maintenance@ziliao --timeout=10
```

搜索 Delivery、Outbox、页面删除和 Mongo 清理只允许通过项目管理命令或 maintenance Worker 处理；禁止手工直接修改状态表、Mongo 正文或 ES 文档。

## 7. 迁移、搜索和数据操作门禁

- 迁移前必须执行 python manage.py migrate --plan，审阅计划并完成 MySQL/Mongo/ES 备份；不得把迁移混入普通重启。
- ES 内容索引只使用项目的创建、rebuild、catch-up、alias 切换命令；不得使用通配符删除索引。
- BlogPage 删除先写 PageDeletionIntent、State tombstone 和 Outbox，再由 Worker 清理 content_body_versions、blog_page_revision_bodies、兼容 blog_content，最后物理删除 MySQL 页面。
- orphan_report --dry-run 默认只读；任何 --apply、Mongo 物理删除、alias 切换都需要单独确认和可恢复备份。

## 8. 当前备份策略

- 生产备份目录：/home/source/Django/wagtail/backups/。
- 当前保留回滚点：wagtailblog3-pre-blog-deletion-migration-20260901-091121/。
- 旧搜索、旧迁移、旧登记和历史测试备份已清理，不再作为回滚依据。
- 新建备份必须包含时间戳、范围说明、校验和和恢复步骤；未完成备份校验前不得执行生产迁移、Mongo 清理或 ES alias 切换。
- 测试调试文件统一放在仓库 output/，不提交、不部署；测试数据备份不长期保留。

## 9. 故障与回滚

1. 记录当前 Git SHA、服务状态、错误日志和受影响资源。
2. 停止本次涉及的应用服务，保留 Outbox、Delivery、Intent 和审计记录。
3. 代码问题回退到上一个已验证 SHA；服务配置问题恢复对应 unit/env 备份后执行 systemctl daemon-reload。
4. 恢复后重新执行依赖、Django check、服务、首页、搜索和队列验收。
5. 不对 Mongo 正文、Wagtail Revision、ES serving 索引执行未经授权的反向删除或恢复。

## 10. 维护记录

2026-09-01：清理生产历史备份目录，仅保留最新删除迁移回滚点；删除测试 output/test-data-backup-20260823；本文件重写为当前服务、启动、发布、备份和回滚流程。
