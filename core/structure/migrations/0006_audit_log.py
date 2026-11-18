# The audit log (structure.md §7), and the end of Django's own admin history, which kept
# the names and addresses of what operators changed, with nothing to delete them. The
# audit log records those changes instead, without names.

import django.utils.timezone
from django.db import migrations, models


def forget_admin_history(apps, schema_editor):
    apps.get_model("admin", "LogEntry")._base_manager.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('admin', '0003_logentry_add_action_flag_choices'),
        ('structure', '0005_custom_is_an_ordinary_key'),
    ]

    operations = [
        migrations.CreateModel(
            name='AuditEntry',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('at', models.DateTimeField(db_index=True, default=django.utils.timezone.now, verbose_name='when')),
                ('action', models.CharField(db_index=True, max_length=50, verbose_name='action')),
                ('via', models.CharField(choices=[('admin', 'the admin page'), ('server', 'a command on the server'), ('core', 'core itself')], max_length=20, verbose_name='way in')),
                ('actor_account', models.IntegerField(blank=True, null=True, verbose_name='by account')),
                ('actor_person', models.IntegerField(blank=True, db_index=True, null=True, verbose_name='by person')),
                ('unit', models.IntegerField(blank=True, help_text='The unit the permission check asked about.', null=True, verbose_name='unit')),
                ('unit_key', models.CharField(blank=True, db_index=True, max_length=40, verbose_name='unit')),
                ('object_type', models.CharField(blank=True, max_length=50, verbose_name='kind of thing')),
                ('object_id', models.IntegerField(blank=True, null=True, verbose_name='which')),
                ('changes', models.JSONField(blank=True, default=dict, verbose_name='changes')),
            ],
            options={
                'verbose_name': 'audit log entry',
                'verbose_name_plural': 'audit log',
                'ordering': ['-at', '-pk'],
                'indexes': [models.Index(fields=['object_type', 'object_id'], name='audit_object')],
            },
        ),
        migrations.RunPython(forget_admin_history, migrations.RunPython.noop),
    ]
