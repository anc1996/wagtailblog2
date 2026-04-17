针对你的需求（**开机自启、多项目共存、安全稳定**），我给出的最佳建议是：**弃用 Shell 脚本 (`start.sh`)，直接使用 Linux 原生的 Systemd 服务。**

这是目前生产环境（Production）的标准做法。它比 crontab 更智能，比 Supervisor 更底层（不需要额安装软件），且能完美解决你担心的“多项目冲突”问题。

### 为什么 Systemd 是最佳方案？

1. **多项目隔离**：每个项目对应一个 `.service` 配置文件，互不干扰。
2. **自动重启**：如果 uWSGI 进程崩溃，Systemd 会自动把它拉起来。
3. **依赖管理**：它会确保在网络服务启动后再启动你的网站，避免报错。
4. **日志标准化**：使用 `journalctl` 统一管理日志，不用到处找 `.log` 文件。

---

### 操作步骤 (请严格按照以下步骤执行)

#### 第一步：修改 `uwsgi.ini` (关键！)

在使用 Systemd 时，**必须**让 uWSGI 在前台运行，由 Systemd 来管理后台化。如果 uWSGI 自己后台化了（daemonize），Systemd 会认为服务启动后就退出了，从而导致启动失败或无限重启。

请编辑你的 `uwsgi.ini`，**注释掉** `daemonize` 这一行：

```ini
# 编辑文件
nano /home/source/Django/wagtail/wagtailblog3/uwsgi.ini

# 找到这一行，在前面加 # 注释掉，或者直接删除
# daemonize=logs/uwsgi.log  <-- 必须注释掉！！

```

*注意：日志将由 Systemd 接管，后续我会教你怎么看。*

#### 第二步：创建 Systemd 服务文件

我们不需要 `start.sh` 脚本了，直接让 Systemd 调用 Conda 环境里的 uWSGI。

创建服务文件：

```bash
sudo nano /etc/systemd/system/wagtailblog3.service

```

粘贴以下内容（已根据你的文件路径配置好）：

```ini
[Unit]
# 服务描述
Description=uWSGI instance for WagtailBlog3
# 在网络启动后才启动本服务
After=network.target

[Service]
# 用户和组 (根据你的 start.sh，你似乎是用 root 运行的)
User=root
Group=root

# 项目根目录
WorkingDirectory=/home/source/Django/wagtail/wagtailblog3

# 环境变量：确保使用 Conda 环境的 PATH
Environment="PATH=/root/anaconda3/envs/wagtailblog/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# 启动命令：直接指向 Conda 环境中的 uwsgi 可执行文件
# Systemd 会自动加载上面的 Environment，所以直接运行 uwsgi 即可
ExecStart=/root/anaconda3/envs/wagtailblog/bin/uwsgi --ini uwsgi.ini

# 进程挂掉后自动重启
Restart=always
# 杀进程的信号 (uWSGI 特有，保证优雅退出)
KillSignal=SIGQUIT
Type=notify
NotifyAccess=all

# 标准输出和错误日志记录到系统日志
StandardError=syslog

[Install]
WantedBy=multi-user.target

```

#### 第三步：启动并设置开机自启

执行以下命令让配置生效：

```bash
# 1. 重载系统服务配置
sudo systemctl daemon-reload

# 2. 启动服务
sudo systemctl start wagtailblog3

# 3. 设置开机自启
sudo systemctl enable wagtailblog3

# 4. 检查状态 (看到绿色的 active (running) 即为成功)
sudo systemctl status wagtailblog3

```

---

### 如何管理多个项目？

你提到有多个项目，Systemd 方案处理起来非常清晰安全：

假设你有一个新项目叫 `shop_project`：

1. 确保 `shop_project` 的 `uwsgi.ini` 里的 `socket` 文件路径不要和 `wagtailblog3` 冲突（例如用 `shop.sock`）。
2. 复制一份服务文件：`cp /etc/systemd/system/wagtailblog3.service /etc/systemd/system/shop_project.service`。
3. 修改 `shop_project.service` 里的 `WorkingDirectory`（项目路径）和 `Environment`（如果用了不同的 Conda 环境）。
4. 执行 `systemctl enable shop_project`。

这样，**每个项目都是独立的进程**，一个挂了不会影响另一个，重启其中一个也不会影响其他的。

### 常用维护命令

* **重启项目**：
`sudo systemctl restart wagtailblog3`
* **停止项目**：
`sudo systemctl stop wagtailblog3`
* **查看项目日志** (代替原来的 uwsgi.log)：
`sudo journalctl -u wagtailblog3 -f`
*（按 Ctrl+C 退出日志查看）*

### 总结

**最佳方案是 Systemd。**
它不需要你写复杂的 Shell 脚本来判断 PID 是否存在（Systemd 帮你做了），也不需要担心开机顺序问题。只要确保每个项目的 `uwsgi.ini` 里的 socket 路径不同，这就是最稳定、最专业的方案。