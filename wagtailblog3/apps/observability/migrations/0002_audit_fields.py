from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("observability", "0001_initial")]

    operations = [
        migrations.AddField("logclearaudit", "completed_at", models.DateTimeField(blank=True, null=True, verbose_name="完成时间")),
        migrations.AddField("logclearaudit", "target_type", models.CharField(default="legacy", max_length=20, verbose_name="目标类型")),
        migrations.AddField("logclearaudit", "kind", models.CharField(blank=True, default="", max_length=20, verbose_name="日志类型")),
        migrations.AddField("logclearaudit", "state", models.CharField(default="completed", max_length=20, verbose_name="最终状态")),
        migrations.AddIndex("logclearaudit", models.Index(fields=["created_at", "state"], name="observ_audit_created_state")),
        migrations.AddIndex("logclearaudit", models.Index(fields=["target_type", "kind"], name="observ_audit_target_kind")),
        migrations.AddIndex("logclearaudit", models.Index(fields=["user", "created_at"], name="observ_audit_user_created")),
    ]
