#!/usr/bin/env bash
set -euo pipefail

# 测试网站与维护 Worker 必须使用同一组队列参数，避免组装任务进入无人消费的默认队列。
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="/root/anaconda3/envs/wagtailblog-test/bin/python"
export WAGTAILBLOG_ENV=test
export CELERY_MAINTENANCE_QUEUE=markdown-test-maintenance
export CELERY_BROKER_DB=12
export CELERY_RESULT_DB=13

cd "$repo_root"
mkdir -p output

if [[ -f output/test-runserver-8080.pid ]] && kill -0 "$(cat output/test-runserver-8080.pid)" 2>/dev/null; then
    kill "$(cat output/test-runserver-8080.pid)" || true
fi
if [[ -f output/test-worker-markdown.pid ]] && kill -0 "$(cat output/test-worker-markdown.pid)" 2>/dev/null; then
    kill "$(cat output/test-worker-markdown.pid)" || true
fi

nohup "$python_bin" manage.py runserver 0.0.0.0:8080 --noreload \
    > output/test-runserver-8080.log 2>&1 < /dev/null &
echo $! > output/test-runserver-8080.pid

nohup "$python_bin" -m celery -A wagtailblog3 worker \
    -Q "$CELERY_MAINTENANCE_QUEUE" --loglevel=INFO \
    --hostname=markdown-test-isolated@%h \
    > output/test-worker-markdown.log 2>&1 < /dev/null &
echo $! > output/test-worker-markdown.pid

echo "test web: http://0.0.0.0:8080"
echo "test worker queue: $CELERY_MAINTENANCE_QUEUE"
