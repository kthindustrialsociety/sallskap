from django.apps import AppConfig
from django.db.models.signals import post_migrate


class AccountsConfig(AppConfig):
    name = "accounts"

    def ready(self):
        from . import signals
        from .roles import ensure_roles

        post_migrate.connect(ensure_roles, sender=self)
        signals.connect()
