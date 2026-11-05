import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
        ("allauth_idp_oidc", "0003_client_allow_uri_wildcards"),
    ]

    operations = [
        migrations.CreateModel(
            name="ToolAccess",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("group_keys", models.JSONField(default=list)),
                ("session_hours", models.PositiveSmallIntegerField(blank=True, null=True)),
                (
                    "tool",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE, related_name="access", to="allauth_idp_oidc.client"
                    ),
                ),
            ],
        ),
    ]
