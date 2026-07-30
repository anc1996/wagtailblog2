import uuid

from django.conf import settings
from django.db import models


class LogClearAudit(models.Model):
    """独立保存日志清理审计，避免记录随文件日志一起被清除。"""

    idempotency_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="log_clear_audits",
        verbose_name="操作人",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="操作时间")
    completed_at = models.DateTimeField(null=True, blank=True, verbose_name="完成时间")
    ip_address = models.GenericIPAddressField(null=True, blank=True, verbose_name="客户端 IP")
    target_type = models.CharField(max_length=20, default="legacy", verbose_name="目标类型")
    target = models.CharField(max_length=80, verbose_name="清理目标")
    kind = models.CharField(max_length=20, blank=True, default="", verbose_name="日志类型")
    scope = models.CharField(max_length=20, verbose_name="清理范围")
    files_before = models.PositiveIntegerField(default=0, verbose_name="文件数")
    bytes_before = models.PositiveBigIntegerField(default=0, verbose_name="清理前字节数")
    bytes_freed = models.PositiveBigIntegerField(default=0, verbose_name="释放字节数")
    succeeded_files = models.PositiveIntegerField(default=0, verbose_name="成功文件数")
    failed_files = models.PositiveIntegerField(default=0, verbose_name="失败文件数")
    succeeded = models.BooleanField(default=True, verbose_name="是否成功")
    state = models.CharField(max_length=20, default="completed", verbose_name="最终状态")
    details = models.JSONField(default=dict, blank=True, verbose_name="执行明细")

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "日志清理记录"
        verbose_name_plural = "日志清理记录"
        permissions = (
            ("view_logs", "可以查看系统日志"),
            ("manage_logs", "可以管理系统日志"),
        )
        indexes = [
            models.Index(fields=("created_at", "state")),
            models.Index(fields=("target_type", "kind")),
            models.Index(fields=("user", "created_at")),
        ]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M:%S} {self.target}"
