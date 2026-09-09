# -*- coding: utf-8 -*-
import subprocess
import sys

def run_ssh(cmd, title=''):
    if title:
        print(f"\n=== {title} ===")
    p = subprocess.run(['ssh', 'root@192.168.20.2', cmd], capture_output=True, text=True, encoding='utf-8', errors='replace')
    if p.stdout:
        print(p.stdout.strip())
    if p.stderr:
        print('STDERR:', p.stderr.strip())
    if p.returncode != 0:
        print(f"ERROR: Command failed with code {p.returncode}")
        sys.exit(p.returncode)
    return p.stdout

# 1. 检查并确保生产环境变量
env_script = '''
ENV_FILE=/home/source/Django/wagtail/wagtailblog3/wagtailblog3/settings/.env.production
if ! grep -q '^REDIS_KEY_PREFIX=' "$ENV_FILE"; then
    echo 'REDIS_KEY_PREFIX=prod' >> "$ENV_FILE"
    echo '已添加 REDIS_KEY_PREFIX=prod 到 .env.production'
else
    echo 'REDIS_KEY_PREFIX 配置已存在'
fi
grep -E '^(WAGTAILBLOG_ENV|REDIS_KEY_PREFIX|BLOG_ARCHIVE_PROGRESSIVE_V2)=' "$ENV_FILE"
'''
run_ssh(env_script, '1. 生产环境变量核验')

# 2. 生产 Git 快进合并
git_script = '''
cd /home/source/Django/wagtail/wagtailblog3
echo "分支: $(git branch --show-current)"
echo "部署前 HEAD: $(git rev-parse HEAD)"
git fetch origin --prune
git merge --ff-only origin/main
echo "部署后 HEAD: $(git rev-parse HEAD)"
'''
run_ssh(git_script, '2. Git 快进拉取 (ff-only)')

# 3. 生产静态检查 (严禁 migrate)
check_script = '''
cd /home/source/Django/wagtail/wagtailblog3
export WAGTAILBLOG_ENV=production
/root/anaconda3/envs/wagtailblog/bin/python manage.py check
'''
run_ssh(check_script, '3. 生产 Django 静态检查')

# 4. 静态文件收集 (杜绝 --clear)
static_script = '''
cd /home/source/Django/wagtail/wagtailblog3
export WAGTAILBLOG_ENV=production
/root/anaconda3/envs/wagtailblog/bin/python manage.py collectstatic --noinput
'''
run_ssh(static_script, '4. 静态资源收集')

# 5. 重启 4 大服务并预检
service_script = '''
echo "重启 wagtailblog3.service..."
systemctl restart wagtailblog3.service
systemctl is-active wagtailblog3.service

echo "预检首屏响应..."
curl --fail --silent --show-error --max-time 15 'http://127.0.0.1:6050/zh-hans/公文材料/政务公开题材/' > /dev/null
echo "首屏健康检查通过"

echo "重启 maintenance..."
systemctl restart wagtailblog3-celery-maintenance.service
systemctl is-active wagtailblog3-celery-maintenance.service

echo "重启 beat..."
systemctl restart wagtailblog3-celery-beat.service
systemctl is-active wagtailblog3-celery-beat.service

echo "重启 filebeat..."
systemctl restart wagtailblog3-filebeat.service
systemctl is-active wagtailblog3-filebeat.service
'''
run_ssh(service_script, '5. 平滑重启 4 大服务与预检')
