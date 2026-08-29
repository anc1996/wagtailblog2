from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("blog", "0031_blogpublicationstate")]

    operations = [
        migrations.AddField(
            model_name="blogpublicationstate",
            name="approved_body_version_id",
            field=models.CharField(blank=True, max_length=128, null=True),
        ),
        migrations.AddField(
            model_name="blogpublicationstate",
            name="approved_body_sha256",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="blogpublicationstate",
            name="approved_body_schema_version",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
