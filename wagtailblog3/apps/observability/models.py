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
    index_sync_state = models.CharField(
        max_length=20,
        default="not_required",
        db_index=True,
        verbose_name="Elasticsearch 同步状态",
    )
    index_sync_attempts = models.PositiveIntegerField(default=0)
    index_sync_deleted = models.PositiveBigIntegerField(default=0)
    index_sync_task_id = models.CharField(max_length=128, blank=True, default="")
    index_sync_last_error = models.TextField(blank=True, default="")
    index_sync_completed_at = models.DateTimeField(null=True, blank=True)
    details = models.JSONField(default=dict, blank=True, verbose_name="执行明细")

    class Meta:
        app_label = "observability"
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

    @property
    def duration_display(self) -> str:
        """返回格式化的执行耗时，优先使用 details 中的精确毫秒。"""
        if self.details and isinstance(self.details, dict) and "duration_ms" in self.details:
            try:
                ms = float(self.details["duration_ms"])
                if ms >= 1000:
                    return f"{ms / 1000:.2f} s"
                return f"{ms:.0f} ms"
            except (ValueError, TypeError):
                pass
        if self.completed_at and self.created_at:
            delta = (self.completed_at - self.created_at).total_seconds()
            if delta < 1:
                return f"{delta * 1000:.0f} ms"
            return f"{delta:.2f} s"
        return "-"

    @property
    def target_type_display(self) -> str:
        """返回目标类型的中文可读名称。"""
        mapping = {
            "domain": "业务领域",
            "file": "单文件",
            "business": "全部业务",
            "all": "全域系统",
            "legacy": "历史归档",
        }
        return mapping.get(self.target_type, self.target_type)

    @property
    def scope_display(self) -> str:
        """返回清理范围的中文可读名称。"""
        mapping = {
            "all": "当前+轮转",
            "current": "仅当前日志",
            "rotated": "仅轮转归档",
        }
        return mapping.get(self.scope, self.scope)

    @property
    def kind_display(self) -> str:
        """返回日志类型的中文可读名称。"""
        mapping = {
            "activity": "常规活动",
            "error": "错误异常",
            "": "全部类型",
        }
        return mapping.get(self.kind, self.kind or "全部类型")

    @property
    def changed_file_count(self) -> int:
        """返回实际发生物理截断或删除的文件数量。"""
        if not self.details or not isinstance(self.details, dict):
            return 0
        changed = self.details.get("changed_files")
        if isinstance(changed, list):
            return len(changed)
        return 0

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M:%S} {self.target}"


class LogIndexSyncJob(models.Model):
    """Durable outbox entry for synchronizing a file cleanup to Elasticsearch."""

    audit = models.OneToOneField(
        LogClearAudit,
        on_delete=models.CASCADE,
        related_name="index_sync_job",
    )
    state = models.CharField(max_length=20, default="pending")
    selector = models.JSONField(default=dict)
    attempts = models.PositiveIntegerField(default=0)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    es_task_id = models.CharField(max_length=128, blank=True, default="")
    deleted_documents = models.PositiveBigIntegerField(default=0)
    last_error = models.TextField(blank=True, default="")
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    dead_letter_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "observability"
        ordering = ("created_at",)
        indexes = [
            models.Index(
                fields=("state", "next_retry_at"),
                name="observ_idxsync_state_retry_idx",
            ),
            models.Index(
                fields=("created_at", "state"),
                name="observ_idxsync_created_idx",
            ),
        ]

    def __str__(self):
        return f"audit={self.audit_id} state={self.state}"
