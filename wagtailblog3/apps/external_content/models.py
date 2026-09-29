"""
Wagtail Page 正文外置通用协议 - MySQL 状态模型定义。

维护页面级不可变 Manifest 快照、正式/草稿/审批指针与单调递增 publication_generation。
不采用数据库物理外键级联删除，保留页面删除与清理过程中的审计证据。
"""

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