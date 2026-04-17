#!/bin/bash

# --- 请根据你的实际路径修改 ---
# Conda 的安装目录
CONDA_BASE_PATH="/root/anaconda3"
# 你的 Conda 环境名称
CONDA_ENV_NAME="wagtailblog"
# 你的项目根目录
PROJECT_DIR="/home/source/Django/wagtail/wagtailblog3"
# -----------------------------

# 切换到项目目录
cd $PROJECT_DIR

# 激活 Conda 环境
# 'source' 命令用于在当前 shell 加载环境配置
source $CONDA_BASE_PATH/bin/activate $CONDA_ENV_NAME

# 使用 exec 来执行 uWSGI
# exec 会让 uWSGI 进程替换掉当前的 shell 进程
# 这样宝塔面板就能正确地管理 uWSGI 的进程号(PID)
exec uwsgi --ini uwsgi.ini