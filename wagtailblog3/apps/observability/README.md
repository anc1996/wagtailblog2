# 项目日志规范

## 代码入口

- 业务代码只使用 `logger = logging.getLogger(__name__)`。
- 日志域、logger 命名空间和文件路径只在 `registry.py` 注册。
- Django `LOGGING` 只由 `config.py` 生成。
- Wagtail 后台日志中心、读取器、清理服务和审计均位于本应用中。

## 文件规则

每个业务域固定两个文件：

- `<domain>/<domain>.log`：INFO、WARNING；
- `<domain>/<domain>_error.log`：ERROR、CRITICAL，异常必须包含 traceback。

基础设施日志分为 `system/`、`celery/`、`email/`。`runtime/` 只保存 runserver/uWSGI 等进程输出，不参与业务 logger 路由。未注册 logger 的 ERROR 才进入 `system/error.log`，因此该文件是路由遗漏的兜底告警，不是常规业务错误文件。

## 级别规则

- `DEBUG`：仅开发诊断，默认不写业务文件；
- `INFO`：正常且值得审计的关键状态变化；
- `WARNING`：可恢复、已降级或需要关注，但请求仍可继续；
- `ERROR`：当前操作失败；
- `CRITICAL`：服务核心能力不可用，需要立即处理。

## 异常规范

未预期异常统一保留完整调用栈：

```python
try:
    save_content()
except Exception:
    logger.exception("保存内容失败 page_id=%s", page_id)
```

不要这样写：

```python
logger.error(f"保存失败: {exc}")
logger.error(traceback.format_exc())
```

业务校验失败（例如邮箱格式错误、缺少字段）可以使用 `warning` 或不带 traceback 的 `error`。不要记录密码、Token、完整邮件正文、Authorization 请求头或完整 POST 数据。

## 查看日志

```bash
python manage.py view_logs --module blog --kind error --lines 100
python manage.py view_logs --module mongo --kind activity --lines 50
```

可用模块由 `registry.LOG_DOMAIN_KEYS` 和 `registry.LOG_FILE_SPECS` 自动生成，不在管理命令中重复维护。

## 后台权限与清理

- `observability.view_logs`：查看日志概览、记录和清理审计；
- `observability.manage_logs`：预览和执行受控清理；
- “全部运行日志”额外要求超级管理员身份。

清理目标只接受 `registry.py` 注册项。当前文件使用原地截断以保留 writer 的文件描述符，轮转历史按 catalog 范围删除；服务会验证普通文件、符号链接、设备号和 inode，并把逐文件结果写入 `LogClearAudit.details`。执行 POST 必须携带短时签名预览和唯一幂等键。

## 轮转

本地文件使用 `ConcurrentRotatingFileHandler`，支持 uWSGI/Celery 多进程安全轮转。默认单文件 10 MiB、保留 5 份、UTF-8、延迟创建；没有记录时不会生成空文件。
