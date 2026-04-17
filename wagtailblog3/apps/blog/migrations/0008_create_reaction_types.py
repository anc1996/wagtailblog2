# 在生成的迁移文件中编辑
from django.db import migrations


def create_reaction_types(apps, schema_editor):
    ReactionType = apps.get_model('blog', 'ReactionType')
    
    # 创建基本反应类型
    reaction_types = [
        {'name': '点赞', 'icon': 'fas fa-thumbs-up', 'display_order': 1},
        {'name': '喜爱', 'icon': 'fas fa-heart', 'display_order': 2},
        {'name': '惊讶', 'icon': 'fas fa-surprise', 'display_order': 3},
        {'name': '思考', 'icon': 'fas fa-lightbulb', 'display_order': 4},
    ]
    
    for rt in reaction_types:
        ReactionType.objects.create(**rt)


def remove_reaction_types(apps, schema_editor):
    ReactionType = apps.get_model('blog', 'ReactionType')
    ReactionType.objects.all().delete()

class Migration(migrations.Migration):

    dependencies = [
        ('blog', '0007_reactiontype_pageview_pageviewcount_reaction'),
    ]

    operations = [
        migrations.RunPython(create_reaction_types, remove_reaction_types),
    ]