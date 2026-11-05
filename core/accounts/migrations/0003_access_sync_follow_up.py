from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.db.models.expressions
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0002_tool_access"),
    ]

    operations = [
        migrations.CreateModel(
            name="AccessSync",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sub", models.CharField(max_length=64, unique=True)),
                ("label", models.CharField(max_length=254)),
                ("requested_at", models.DateTimeField()),
                ("pending", models.BooleanField(db_index=True, default=True)),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("retry_at", models.DateTimeField(blank=True, null=True)),
                ("tools", models.JSONField(default=dict)),
                (
                    "user",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="access_sync",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="FollowUp",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("about", models.CharField(max_length=254, verbose_name="about")),
                ("sub", models.CharField(max_length=64)),
                ("tool", models.CharField(max_length=100, verbose_name="tool")),
                ("key", models.CharField(max_length=100)),
                ("text", models.TextField(verbose_name="what to do")),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, verbose_name="created")),
                ("done_at", models.DateTimeField(blank=True, null=True, verbose_name="done")),
                (
                    "done_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="done by",
                    ),
                ),
            ],
            options={
                "verbose_name": "follow-up",
                "verbose_name_plural": "follow-ups",
                "ordering": [django.db.models.expressions.OrderBy(models.F("done_at"), nulls_first=True), "-created_at"],
                "constraints": [
                    models.UniqueConstraint(condition=models.Q(("done_at__isnull", True)), fields=("sub", "tool", "key"), name="one_open_follow_up"),
                ],
            },
        ),
    ]
