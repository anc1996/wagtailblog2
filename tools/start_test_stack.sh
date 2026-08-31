#!/usr/bin/env bash
set -euo pipefail

# 测试网站、维护 Worker 与 Beat 必须共享隔离队列，避免测试任务进入生产 broker。
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="/root/anaconda3/envs/wagtailblog-test/bin/python"
export WAGTAILBLOG_ENV=test
export CELERY_MAINTENANCE_QUEUE=markdown-test-maintenance
export CELERY_BROKER_DB=12
export CELERY_RESULT_DB=13

cd "$repo_root"
mkdir -p output

stop_managed_process() {
    local pid_file="$1"
    local expected_command="$2"
    local pid command process_cwd

    [[ -f "$pid_file" ]] || return 0
    pid="$(tr -d '[:space:]' < "$pid_file")"
    if [[ ! "$pid" =~ ^[0-9]+$ ]] || ! kill -0 "$pid" 2>/dev/null; then
        rm -f "$pid_file"
        return 0
    fi

    command="$(tr '\0' ' ' < "/proc/$pid/cmdline")"
    process_cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
    if [[ "$process_cwd" != "$repo_root" || "$command" != *"$expected_command"* ]]; then
        echo "refusing to stop unrelated process from $pid_file: pid=$pid" >&2
        exit 1
    fi

    kill "$pid"
    for _ in {1..20}; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.25
    done
    if kill -0 "$pid" 2>/dev/null; then
        echo "managed process did not stop: pid=$pid command=$expected_command" >&2
        exit 1
    fi
    rm -f "$pid_file"
}

case "${1:-start}" in
    start)
        ;;
    stop)
        # 停止只允许触及脚本创建且仍指向本仓库的三个进程。
        stop_managed_process output/test-beat.pid "celery -A wagtailblog3 beat"
        stop_managed_process output/test-worker-markdown.pid "celery -A wagtailblog3 worker"
        stop_managed_process output/test-runserver-8080.pid "manage.py runserver"
        echo "test stack stopped"
        exit 0
        ;;
    *)
        echo "usage: $0 [start|stop]" >&2
        exit 2
        ;;
esac

# 重启顺序与生产维护规则一致：先停 Beat，再停 Worker，最后停网站。
stop_managed_process output/test-beat.pid "celery -A wagtailblog3 beat"
stop_managed_process output/test-worker-markdown.pid "celery -A wagtailblog3 worker"
stop_managed_process output/test-runserver-8080.pid "manage.py runserver"

# 未纳入 PID 文件的旧进程必须人工核对，脚本不猜测并强杀未知进程。
if ss -ltnp | grep -Eq 'LISTEN[[:space:]].*:8080[[:space:]]'; then
    echo "port 8080 is already in use; stop the unmanaged process before retrying" >&2
    exit 1
fi
if pgrep -af 'celery.*-A wagtailblog3 worker.*markdown-test-maintenance' >/dev/null; then
    echo "an unmanaged markdown-test-maintenance worker is already running" >&2
    exit 1
fi

# Windows 主机通过 WSL2 地址访问；监听所有 WSL2 接口以避免只绑定 loopback。
setsid "$python_bin" manage.py runserver 0.0.0.0:8080 --noreload \
    > output/test-runserver-8080.log 2>&1 < /dev/null &
echo $! > output/test-runserver-8080.pid

# 显式创建独立 session，避免启动终端退出时的 SIGHUP 触发 Celery 自重启路径。
setsid "$python_bin" -m celery -A wagtailblog3 worker \
    -Q "$CELERY_MAINTENANCE_QUEUE" --pool=solo --without-gossip --without-mingle --without-heartbeat --loglevel=INFO \
    --hostname=markdown-test-isolated@%h \
    > output/test-worker-markdown.log 2>&1 < /dev/null &
echo $! > output/test-worker-markdown.pid

# Beat 与 Worker 使用同一隔离 broker；调度文件写入 output，不能污染 Git 工作树。
setsid "$python_bin" -m celery -A wagtailblog3 beat \
    --loglevel=INFO --schedule=output/test-celerybeat-schedule \
    > output/test-beat.log 2>&1 < /dev/null &
echo $! > output/test-beat.pid

web_ready=false
for _ in {1..30}; do
    stack_alive=true
    for pid_file in output/test-runserver-8080.pid output/test-worker-markdown.pid output/test-beat.pid; do
        pid="$(cat "$pid_file")"
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "test stack process failed to start: $pid_file" >&2
            stack_alive=false
            break
        fi
    done
    [[ "$stack_alive" == true ]] || break
    if curl --fail --silent --head --max-time 2 http://192.168.20.5:8080/admin/ >/dev/null; then
        web_ready=true
        break
    fi
    sleep 1
done
if [[ "$web_ready" != true ]]; then
    echo "test web health check failed; see output/test-runserver-8080.log" >&2
    stop_managed_process output/test-beat.pid "celery -A wagtailblog3 beat"
    stop_managed_process output/test-worker-markdown.pid "celery -A wagtailblog3 worker"
    stop_managed_process output/test-runserver-8080.pid "manage.py runserver"
    exit 1
fi

echo "test web: http://192.168.20.5:8080"
echo "test worker queue: $CELERY_MAINTENANCE_QUEUE"
echo "test broker/result db: $CELERY_BROKER_DB/$CELERY_RESULT_DB"
echo "test pids: web=$(cat output/test-runserver-8080.pid) worker=$(cat output/test-worker-markdown.pid) beat=$(cat output/test-beat.pid)"
