# Operator is the one group, and nothing renames it (structure.md §4), so it's known by its
# name. The table tying groups to keys goes. The group itself and who is in it stay.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0004_operator_is_the_one_group"),
    ]

    operations = [
        migrations.DeleteModel(name="GroupRole"),
    ]
