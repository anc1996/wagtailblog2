import uuid

from django.db import models
from django.utils import timezone


class ContentSearchOperation(models.TextChoices):
    UPSERT = "upsert", "写入"
    TOMBSTONE = "tombstone", "墓碑"


class ContentSearchStatus(models.TextChoices):
    PENDING = "pending", "待处理"
    PROCESSING = "processing", "处理中"
    RETRY = "retry", "待重试"
    SUCCEEDED = "succeeded", "已完成"
    SUPERSEDED = "superseded", "已过期"
    DEAD = "dead", "死信"


class ContentSearchTargetRole(models.TextChoices):
    SERVING = "serving", "服务中"
    BUILDING = "building", "构建中"
    RETIRED = "retired", "已退役"


class SearchIndexBuildStatus(models.TextChoices):
    CREATED = "created", "已创建"
    BACKFILLING = "backfilling", "回填中"
    CATCHING_UP = "catching_up", "追平中"
    READY = "ready", "已就绪"
    SERVING = "serving", "服务中"
    RETIRED = "retired", "已退役"
    FAILED = "failed", "失败"


class ContentSearchScopeJobStatus(models.TextChoices):
    PENDING = "pending", "待处理"
    PROCESSING = "processing", "处理中"
    RETRY = "retry", "待重试"
    SUCCEEDED = "succeeded", "已完成"
    DEAD = "dead", "死信"


class ContentSearchState(models.Model):
    """MySQL 中每个页面的搜索期望版本；它是 Outbox 投递的幂等基准。"""
    """页面公开搜索状态的权威版本，不依赖可被删除的 Page 外键。"""

    page_id = models.PositiveBigIntegerField(primary_key=True)
    content_version = models.PositiveBigIntegerField(default=0)
    desired_operation = models.CharField(
        max_length=16,
        choices=ContentSearchOperation.choices,
        default=ContentSearchOperation.UPSERT,
    )
    searchable = models.BooleanField(default=False)
    content_hash = models.CharField(max_length=64, null=True, blank=True)
    mongo_content_id = models.CharField(max_length=50, null=True, blank=True)
    # 新版本使用正文不可变指针和公开代际；旧页面可继续为空。
    body_version_id = models.CharField(max_length=128, null=True, blank=True)
    publication_generation = models.PositiveBigIntegerField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "内容搜索状态"
        verbose_name_plural = "内容搜索状态"

    def __str__(self) -> str:
        return f"page={self.page_id} version={self.content_version}"


class ContentSearchOutbox(models.Model):
    """发布事务产生的持久事件；状态只由数据库事务和投递器推进。"""
    """MySQL 持久事件日志，Celery 仅在后续工作包中作为唤醒机制。"""

    event_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    page_id = models.PositiveBigIntegerField()
    content_version = models.PositiveBigIntegerField()
    operation = models.CharField(max_length=16, choices=ContentSearchOperation.choices)
    content_hash = models.CharField(max_length=64, null=True, blank=True)
    mongo_content_id = models.CharField(max_length=50, null=True, blank=True)
    # Outbox 事件携带生成时的正文身份，消费者据此拒绝迟到事件。
    body_version_id = models.CharField(max_length=128, null=True, blank=True)
    publication_generation = models.PositiveBigIntegerField(null=True, blank=True)
    searchable = models.BooleanField(default=False)
    status = models.CharField(
        max_length=16,
        choices=ContentSearchStatus.choices,
        default=ContentSearchStatus.PENDING,
    )
    attempts = models.PositiveIntegerField(default=0)
    available_at = models.DateTimeField(default=timezone.now)
    locked_by = models.CharField(max_length=128, blank=True, default="")
    lock_expires_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=64, blank=True, default="")
    # 后续消费者只能写入脱敏、截断后的诊断信息，不能写正文或连接凭据。
    last_error_message = models.TextField(max_length=2000, blank=True, default="")
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "内容搜索事件"
        verbose_name_plural = "内容搜索事件"
        constraints = [
            models.UniqueConstraint(
                fields=("page_id", "content_version"),
                name="search_outbox_page_version_uq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("status", "created_at"),
                name="srch_outbox_stat_created_idx",
            ),
            models.Index(
                fields=("status", "available_at"),
                name="srch_outbox_stat_avail_idx",
            ),
            models.Index(
                fields=("page_id", "-content_version", "-id"),
                name="srch_outbox_page_ver_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"event={self.event_id} page={self.page_id} version={self.content_version}"


class ContentSearchTarget(models.Model):
    """一个 ES 连接及物理索引目标的配置快照，不持有正文数据。"""
    """逻辑投递目标只引用 settings 连接名和物理索引名，不保存连接密钥。"""

    target_id = models.SlugField(max_length=80, unique=True)
    connection_name = models.SlugField(max_length=80)
    index_name = models.CharField(max_length=255)
    role = models.CharField(
        max_length=16,
        choices=ContentSearchTargetRole.choices,
        default=ContentSearchTargetRole.SERVING,
    )
    required = models.BooleanField(default=True)
    enabled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "内容搜索目标"
        verbose_name_plural = "内容搜索目标"
        indexes = [
            models.Index(
                fields=("enabled", "role"),
                name="search_target_enabled_role_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.target_id}:{self.index_name}"


class ContentSearchDelivery(models.Model):
    """Outbox 到单一目标的租约状态，负责重试、过期回收和幂等结果。"""
    """一个事件到一个物理目标的可重试投递记录。"""

    event = models.ForeignKey(
        ContentSearchOutbox,
        on_delete=models.PROTECT,
        related_name="deliveries",
    )
    target = models.ForeignKey(
        ContentSearchTarget,
        on_delete=models.PROTECT,
        related_name="deliveries",
    )
    status = models.CharField(
        max_length=16,
        choices=ContentSearchStatus.choices,
        default=ContentSearchStatus.PENDING,
    )
    attempts = models.PositiveIntegerField(default=0)
    lease_reclaims = models.PositiveIntegerField(default=0)
    available_at = models.DateTimeField(default=timezone.now)
    locked_by = models.CharField(max_length=128, blank=True, default="")
    lock_expires_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=64, blank=True, default="")
    # 保存有限的脱敏诊断，避免错误记录意外成为正文或凭据副本。
    last_error_message = models.TextField(max_length=2000, blank=True, default="")
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "内容搜索投递"
        verbose_name_plural = "内容搜索投递"
        constraints = [
            models.UniqueConstraint(
                fields=("event", "target"),
                name="search_delivery_event_target_uq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("target", "status", "available_at"),
                name="srch_deliv_tgt_stat_avail_idx",
            ),
            models.Index(
                fields=("status", "lock_expires_at"),
                name="srch_deliv_stat_lease_idx",
            ),
            models.Index(
                fields=("status", "available_at"),
                name="srch_deliv_stat_avail_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"event={self.event_id} target={self.target_id} status={self.status}"


class SearchIndexBuild(models.Model):
    """一次物理索引构建及 alias 切换的审计状态。"""
    """独立内容索引构建的持久检查点，后续回填可从中断位置恢复。"""

    build_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    target = models.ForeignKey(
        ContentSearchTarget,
        on_delete=models.PROTECT,
        related_name="builds",
    )
    mapping_version = models.CharField(max_length=64)
    status = models.CharField(
        max_length=16,
        choices=SearchIndexBuildStatus.choices,
        default=SearchIndexBuildStatus.CREATED,
    )
    # 固定扫描上界，让构建期间新增或重新公开的页面由增量事件覆盖。
    scan_upper_bound_page_id = models.PositiveBigIntegerField(default=0)
    checkpoint_page_id = models.PositiveBigIntegerField(default=0)
    scanned = models.PositiveBigIntegerField(default=0)
    succeeded = models.PositiveBigIntegerField(default=0)
    superseded = models.PositiveBigIntegerField(default=0)
    failed = models.PositiveBigIntegerField(default=0)
    missing = models.PositiveBigIntegerField(default=0)
    last_batch_count = models.PositiveIntegerField(default=0)
    last_batch_bytes = models.PositiveBigIntegerField(default=0)
    catch_up_clean_streak = models.PositiveSmallIntegerField(default=0)
    last_error_code = models.CharField(max_length=64, blank=True, default="")
    # 只保存脱敏分类，不保存 ES/Mongo 异常原文、正文或连接信息。
    last_error_message = models.TextField(max_length=2000, blank=True, default="")
    last_checkpoint_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "搜索索引构建"
        verbose_name_plural = "搜索索引构建"
        indexes = [
            models.Index(
                fields=("status", "updated_at"),
                name="srch_build_stat_updated_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"build={self.build_id} target={self.target_id} status={self.status}"


class ContentSearchScopeJob(models.Model):
    """访问限制变化触发的范围重算任务，按根页面合并重复请求。"""
    """访问限制变化的范围重算请求，不能在 signal 内同步枚举整个页面子树。"""

    root_page_id = models.PositiveBigIntegerField()
    status = models.CharField(
        max_length=16,
        choices=ContentSearchScopeJobStatus.choices,
        default=ContentSearchScopeJobStatus.PENDING,
    )
    checkpoint_page_id = models.PositiveBigIntegerField(default=0)
    last_error_code = models.CharField(max_length=64, blank=True, default="")
    # 范围任务的错误信息同样只允许记录脱敏诊断，避免把页面正文带入审计表。
    last_error_message = models.TextField(max_length=2000, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "内容搜索范围任务"
        verbose_name_plural = "内容搜索范围任务"
        indexes = [
            models.Index(
                fields=("status", "created_at"),
                name="srch_scope_stat_created_idx",
            ),
            models.Index(
                fields=("root_page_id", "status"),
                name="srch_scope_root_stat_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"root={self.root_page_id} status={self.status}"
