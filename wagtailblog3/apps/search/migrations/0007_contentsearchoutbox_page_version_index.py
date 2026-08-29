from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("search", "0006_contentsearch_generation_fields"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="contentsearchoutbox",
            index=models.Index(
                fields=["page_id", "-content_version", "-id"],
                name="srch_outbox_page_ver_idx",
            ),
        ),
    ]
