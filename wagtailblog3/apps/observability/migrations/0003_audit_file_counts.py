from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("observability", "0002_audit_fields")]

    operations = [
        migrations.AddField("logclearaudit", "succeeded_files", models.PositiveIntegerField(default=0, verbose_name="成功文件数")),
        migrations.AddField("logclearaudit", "failed_files", models.PositiveIntegerField(default=0, verbose_name="失败文件数")),
    ]
