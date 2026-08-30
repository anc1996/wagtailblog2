from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("search", "0007_contentsearchoutbox_page_version_index"),
    ]

    operations = [
        migrations.AddField(
            model_name="contentsearchscopejob",
            name="attempts",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="contentsearchscopejob",
            name="locked_by",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="contentsearchscopejob",
            name="lock_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="contentsearchscopejob",
            name="rescan_requested",
            field=models.BooleanField(default=False),
        ),
    ]
