from django.db import migrations


def remove_groups(apps, schema_editor):
    """structure.md §5: access comes from memberships and positions, and Operator is the
    one group. Every other group goes, the built-in Board, Treasurer, Events, Developer
    and Member, and any made by hand, along with who was in them. Nobody is moved into a
    membership or position: record those in the admin page."""
    Group = apps.get_model("auth", "Group")
    Group.objects.exclude(role__key="operator").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0003_access_sync_follow_up"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    # Nothing to undo: the groups the earlier code wants are made again, empty, after every migrate.
    operations = [migrations.RunPython(remove_groups, migrations.RunPython.noop)]
