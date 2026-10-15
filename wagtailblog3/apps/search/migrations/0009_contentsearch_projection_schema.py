"""D3 P-03：仅追加投影字段，保留旧 State/Outbox 的 NULL，不做数据回填。"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("search", "0008_contentsearchscopejob_leases"),
    ]

    operations = [
        migrations.AddField(
            model_name="contentsearchstate",
            name="model_label",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="contentsearchstate",
            name="search_type",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="contentsearchstate",
            name="snapshot_kind",
            field=models.CharField(blank=True, max_length=32, null=True),
        ),
        migrations.AddField(
            model_name="contentsearchstate",
            name="published_snapshot",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="contentsearchstate",
            name="snapshot_digest",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="contentsearchstate",
            name="projection_schema_version",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="contentsearchoutbox",
            name="model_label",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="contentsearchoutbox",
            name="search_type",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="contentsearchoutbox",
            name="snapshot_kind",
            field=models.CharField(blank=True, max_length=32, null=True),
        ),
        migrations.AddField(
            model_name="contentsearchoutbox",
            name="published_snapshot",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="contentsearchoutbox",
            name="snapshot_digest",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="contentsearchoutbox",
            name="projection_schema_version",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        # 只为既有目标声明旧能力；不把旧事件或目标批量标记为 v2。
        migrations.AddField(
            model_name="contentsearchtarget",
            name="projection_schema_version",
            field=models.PositiveIntegerField(default=1),
        ),
    ]
