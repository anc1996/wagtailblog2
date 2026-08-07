# WagtailBlog3 生产维护手册

本文档对应生产服务器 `192.168.20.2` 的当前部署方式。项目目录为
`/home/source/Django/wagtail/wagtailblog3`，Conda 环境为
`/root/anaconda3/envs/wagtailblog`。应用由 systemd 管理，不使用
`start.sh` 或 Django `runserver`。

## 服务拓扑

浏览器访问 `http://192.168.20.2:6050`，宝塔 Nginx 监听 6050，并通过
`/home/source/Django/wagtail/wagtailblog3/wagtailblog3.sock` 转发给 uWSGI。
uWSGI 的 6051 是仅供本机诊断的 HTTP 端口，不作为对外入口。

必须运行的应用服务如下：

| 服务 | 职责 | 是否开机启动 |
| --- | --- | --- |
| `wagtailblog3.service` | uWSGI / Django 网站 | 是 |
| `wagtailblog3-celery-maintenance.service` | 日志索引同步和维护队列 | 是 |
| `wagtailblog3-celery-beat.service` | 每 30 秒补偿日志索引 outbox | 是 |
| `wagtailblog3-filebeat.service` | 采集项目日志并写入 Elasticsearch | 是 |

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

- `wagtailblog3-celery-maintenance.service`：消费 `maintenance` 队列，执行日志索引同步和维护任务；
- `wagtailblog3-celery-beat.service`：调度定时任务和失败补偿；
- `wagtailblog3-filebeat.service`：采集项目日志并写入 Elasticsearch。

后续如果新增其他服务，必须把它加入“必须运行的应用服务”表、重启验收命令、
日志查看命令和开机启动命令，并同时更新对应的 systemd unit 文件和部署记录。

依赖服务也必须可用：`mysqld.service`、`redis.service`、
`mongodb-home.service`、`minio.service`、`docker.service` 和 Nginx。
Elasticsearch 与 Kibana 由 Docker 管理，Elasticsearch 容器设置为
`restart=always`。

不要启动只消费 `email` 或 `default` 队列的 Celery Worker，除非已经检查
Redis 中没有历史邮件任务，并明确需要处理这些任务。当前 Worker 只监听
`maintenance` 队列。

## 常用命令

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

## 健康检查

在项目目录执行：

```bash
cd /home/source/Django/wagtail/wagtailblog3
set -a
. ./observability.env
set +a

/root/anaconda3/envs/wagtailblog/bin/python manage.py check
/root/anaconda3/envs/wagtailblog/bin/python -m celery -A wagtailblog3 inspect ping -d maintenance@ziliao --timeout=10
curl -fsS http://127.0.0.1:9200/_cluster/health
curl -fsS http://127.0.0.1:9200/wagtailblog-logs-read/_count
curl -I -H 'Host: wagtailblog.docs' http://127.0.0.1:6050/admin/login/
```

单节点 Elasticsearch 因其他索引副本未分配而显示 `yellow` 是预期状态；
`wagtailblog-logs-000001` 本身应为 `green`，因为它使用 1 分片、0 副本。

## 发布与静态文件

生产环境采用文件清单部署，不对生产目录执行 `git pull` 或 `rsync --delete`。
部署必须保留生产的 `wagtailblog3/settings/database.py`、`observability.env`、
`logs/`、`media/` 和 socket 文件。

代码切换后，在项目目录执行：

```bash
set -a
. ./observability.env
set +a

/root/anaconda3/envs/wagtailblog/bin/python manage.py check
/root/anaconda3/envs/wagtailblog/bin/python manage.py collectstatic --noinput
systemctl restart wagtailblog3.service
```

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
