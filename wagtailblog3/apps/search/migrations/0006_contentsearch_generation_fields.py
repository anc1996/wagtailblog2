from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("search", "0005_searchindexbuild_backfill_state"),
    ]

    operations = [
        migrations.AddField(
            model_name="contentsearchstate",
            name="body_version_id",
            field=models.CharField(blank=True, max_length=128, null=True),
        ),
        migrations.AddField(
            model_name="contentsearchstate",
            name="publication_generation",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="contentsearchoutbox",
            name="body_version_id",
            field=models.CharField(blank=True, max_length=128, null=True),
        ),
        migrations.AddField(
            model_name="contentsearchoutbox",
            name="publication_generation",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
    ]
