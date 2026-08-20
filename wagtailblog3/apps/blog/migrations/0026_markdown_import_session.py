# Generated for the large Markdown import session protocol.

import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("blog", "0025_markdown_import_batch_artifact"),
    ]

    operations = [
        migrations.CreateModel(
            name="MarkdownImportSession",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("session_id", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("manifest", models.JSONField()),
                ("status", models.CharField(choices=[("created", "已创建"), ("uploading", "上传中"), ("ready", "待组装"), ("assembling", "组装中"), ("success", "成功"), ("partial_success", "部分成功"), ("failed", "失败"), ("expired", "已过期")], default="created", max_length=24)),
                ("total_artifacts", models.PositiveIntegerField(default=0)),
                ("total_bytes", models.BigIntegerField(default=0)),
                ("completed_artifacts", models.PositiveIntegerField(default=0)),
                ("expires_at", models.DateTimeField()),
                ("assembly_requested_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("batch", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="upload_session", to="blog.markdownimportbatch")),
            ],
            options={"verbose_name": "Markdown 大批量导入会话", "verbose_name_plural": "Markdown 大批量导入会话"},
        ),
        migrations.AddField(
            model_name="markdownimportartifact",
            name="session",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="artifacts", to="blog.markdownimportsession"),
        ),
        migrations.AddField(
            model_name="markdownimportartifact",
            name="uploaded_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name="markdownimportsession",
            index=models.Index(fields=["status", "expires_at"], name="blog_md_session_stat_exp_idx"),
        ),
    ]
