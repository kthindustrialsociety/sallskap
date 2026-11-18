# What outlives someone deleted with nothing left (structure.md §3): their ended positions,
# with their name kept on them and no link to them, and the grants and seat rules they gave,
# without the link. Nothing is changed in rows already there.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0006_audit_log'),
    ]

    operations = [
        migrations.AlterField(
            model_name='position',
            name='person',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='positions', to='structure.person', verbose_name='person'),
        ),
        migrations.AddField(
            model_name='position',
            name='kept_name',
            field=models.CharField(blank=True, editable=False, max_length=150, verbose_name='held by, since deleted'),
        ),
        migrations.AlterField(
            model_name='grant',
            name='given_by',
            field=models.ForeignKey(blank=True, help_text='A chair or vice chair with authority over the unit it covers, on the day it starts.', null=True, on_delete=django.db.models.deletion.PROTECT, related_name='grants_given', to='structure.person', verbose_name='given by'),
        ),
        migrations.AlterField(
            model_name='seatrule',
            name='made_by',
            field=models.ForeignKey(blank=True, help_text='Someone who could appoint in the unit with the seats, on the day it starts.', null=True, on_delete=django.db.models.deletion.PROTECT, related_name='seat_rules_made', to='structure.person', verbose_name='made by'),
        ),
        migrations.AddConstraint(
            model_name='position',
            constraint=models.CheckConstraint(condition=models.Q(('person__isnull', False), ('end__isnull', False), _connector='OR'), name='kept_position_has_ended'),
        ),
    ]
