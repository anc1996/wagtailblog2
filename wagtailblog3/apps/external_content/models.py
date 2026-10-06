"""
Wagtail Page 正文外置通用协议 - MySQL 状态模型定义。

维护页面级不可变 Manifest 快照、正式/草稿/审批指针与单调递增 publication_generation。
维护外置正文删除意图模型（包含持久 tombstone 墓碑与 claim 围栏）。
不采用数据库物理外键级联删除，保留页面删除与清理过程中的审计证据。
"""

from __future__ import annotations

import uuid
from django.db import models


class ExternalContentPublicationState(models.Model):
    """
    页面级正文外置发布状态模型。

    权威保存：
    1. page_id: 页面主键（逻辑关联，不设物理外键以防级联破坏审计证据）；
    2. model_label: 小写模型标识（如 guides.guidepage）；
    3. publication_generation: 页面公开状态变更代次（单调递增）；
    4. published_revision_id / published_manifest: 当前正式发布的不可变正文凭证；
    5. draft_revision_id / draft_manifest: 最新草稿凭证；
    6. approved_revision_id / approved_manifest: 工作流最新审批凭证；
    7. source_page_id / source_revision_id: Alias 别名源关系追踪。
    """

    page_id = models.PositiveBigIntegerField(
        primary_key=True,
        help_text="Wagtail Page 页面主键 ID",
    )
    model_label = models.CharField(
        max_length=255,
        db_index=True,
        help_text="规范化小写模型标识（如 guides.guidepage）",
    )
    publication_generation = models.PositiveBigIntegerField(
        default=0,
        help_text="公开状态变更单调递增代次",
    )

    published_revision_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        db_index=True,
        help_text="正式发布的 Revision ID（普通页面必填，Alias 页为空）",
    )
    published_manifest = models.JSONField(
        null=True,
        blank=True,
        help_text="正式发布的不可变 Manifest 快照",
    )
    published_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="正式发布生效时间",
    )

    draft_revision_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        help_text="最新草稿 Revision ID",
    )
    draft_manifest = models.JSONField(
        null=True,
        blank=True,
        help_text="最新草稿 Manifest 快照",
    )

    approved_revision_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        help_text="最新经工作流审批通过的 Revision ID",
    )
    approved_manifest = models.JSONField(
        null=True,
        blank=True,
        help_text="最新审批通过的 Manifest 快照",
    )

    source_page_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        db_index=True,
        help_text="若为 Alias 别名页面，记录其源页面 page_id",
    )
    source_revision_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        help_text="若为 Alias 别名页面，记录其源 Revision ID",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="状态记录创建时间",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="状态记录最后更新时间",
    )

    class Meta:
        db_table = "external_content_publication_state"
        verbose_name = "页面正文外置发布状态"
        verbose_name_plural = "页面正文外置发布状态列表"
        indexes = [
            models.Index(fields=["model_label", "publication_generation"], name="ext_content_model_gen_idx"),
        ]

    def __str__(self) -> str:
        return f"ExternalContentPublicationState(page_id={self.page_id}, model={self.model_label}, gen={self.publication_generation})"


class DeletionIntentStatus(models.TextChoices):
    """外置正文删除意图流转状态枚举。"""

    PENDING = "pending", "等待执行"
    MYSQL_DELETED = "mysql_deleted", "MySQL已删除"
    GC_ELIGIBLE = "gc_eligible", "度过安全期可回收"
    CLAIMED = "claimed", "GC认领处理中"
    COMPLETED = "completed", "安全回收全量完成"
    CANCELLED = "cancelled", "取消或恢复"
    FAILED = "failed", "回收异常需重试"


class ExternalContentDeletionIntent(models.Model):
    """
    外置正文安全删除意图模型（含 Tombstone 墓碑与 Claim 围栏）。

    保障页面删除时的跨库物理隔离与延迟安全垃圾回收：
    1. 意图登记时捕获待删除版本全量快照；
    2. 严格遵循 safe_gc_after 安全隔离期（默认 7 天）；
    3. 通过 claim 租约与 claim_expires_at 围栏实现多 Worker 安全抢占防并发冲突；
    4. 持久化记录 collected_versions 与 failed_versions 追踪逐版本回收进度。
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        help_text="删除意图全局唯一标识 UUID",
    )
    page_id = models.PositiveBigIntegerField(
        db_index=True,
        help_text="目标 Wagtail Page ID",
    )
    model_label = models.CharField(
        max_length=255,
        db_index=True,
        help_text="规范化小写模型标识（如 guides.guidepage）",
    )
    status = models.CharField(
        max_length=32,
        choices=DeletionIntentStatus.choices,
        default=DeletionIntentStatus.PENDING,
        db_index=True,
        help_text="删除意图流转状态",
    )
    manifest = models.JSONField(
        default=dict,
        help_text="固化的待删除版本清单（包含 body_version_ids、revision_ids、alias_page_ids 等）",
    )
    safe_gc_after = models.DateTimeField(
        db_index=True,
        help_text="安全隔离保护期截止时间，在此之前绝对禁止物理回收 Mongo 版本",
    )
    actor_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="操作人标识（如 user_id）",
    )
    request_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="请求链路追踪 ID",
    )

    # Tombstone 逐版本回收追踪与 Claim 围栏
    collected_versions = models.JSONField(
        default=list,
        blank=True,
        help_text="已成功完成物理回收的 version_id 列表 (持久 tombstone 墓碑)",
    )
    failed_versions = models.JSONField(
        default=dict,
        blank=True,
        help_text="跳过或回收失败的 version_id 及原因记录",
    )
    claimed_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="GC Worker 认领租约时间",
    )
    claimed_worker = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="认领该意图的 GC Worker 标识",
    )
    claim_expires_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="GC 认领租约过期时间（围栏保护）",
    )
    claim_token = models.UUIDField(
        null=True,
        blank=True,
        db_index=True,
        help_text="GC 认领隔离防并发令牌 (Token 围栏)",
    )
    generation = models.PositiveBigIntegerField(
        default=0,
        help_text="删除意图变更单调递增代次",
    )
    retry_count = models.PositiveIntegerField(
        default=0,
        help_text="回收重试次数",
    )
    last_error = models.TextField(
        null=True,
        blank=True,
        help_text="最近一次回收异常或失败原因",
    )

    completed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Mongo 版本安全回收完成时间",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="意图创建时间",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="意图更新时间",
    )

    class Meta:
        db_table = "external_content_deletion_intent"
        verbose_name = "正文外置删除意图"
        verbose_name_plural = "正文外置删除意图列表"
        indexes = [
            models.Index(fields=["status", "safe_gc_after"], name="ext_del_status_gc_idx"),
            models.Index(fields=["page_id", "status"], name="ext_del_page_status_idx"),
            models.Index(fields=["status", "safe_gc_after", "claim_expires_at"], name="ext_del_claim_idx"),
        ]

    def __str__(self) -> str:
        return f"ExternalContentDeletionIntent(id={self.id}, page_id={self.page_id}, status={self.status})"
