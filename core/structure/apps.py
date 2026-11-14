from django.apps import AppConfig
from django.db.models.signals import post_migrate
from django.utils.translation import gettext_lazy as _


class StructureConfig(AppConfig):
    name = "structure"
    verbose_name = _("Associations and teams")

    def ready(self):
        from .models import ensure_root

        post_migrate.connect(ensure_root, sender=self)
