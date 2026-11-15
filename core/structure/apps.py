from django.apps import AppConfig
from django.conf import settings
from django.db.models.signals import post_delete, post_migrate, post_save, pre_delete
from django.utils.translation import gettext_lazy as _


def _account_saved(sender, instance, raw=False, update_fields=None, **kwargs):
    # Logging in saves last_login alone, which changes nothing here.
    if not raw and update_fields != frozenset({"last_login"}):
        from .models import link_account

        link_account(instance)


def _account_deleting(sender, instance, **kwargs):
    from .models import Person

    instance._person_id = Person.objects.filter(user=instance).values_list("pk", flat=True).first()


def _account_deleted(sender, instance, **kwargs):
    # The person stays if anything was ever tied to them, as that record is kept.
    # Otherwise there's nothing left to hold them for.
    from .models import Person

    person = Person.objects.filter(pk=getattr(instance, "_person_id", None)).first()
    if person is not None and person.deletion_problem() is None:
        person.delete()


class StructureConfig(AppConfig):
    name = "structure"
    verbose_name = _("Associations and teams")

    def ready(self):
        from .models import ensure_root

        post_migrate.connect(ensure_root, sender=self)
        post_save.connect(_account_saved, sender=settings.AUTH_USER_MODEL, dispatch_uid="structure.link_account")
        pre_delete.connect(_account_deleting, sender=settings.AUTH_USER_MODEL, dispatch_uid="structure.account_deleting")
        post_delete.connect(_account_deleted, sender=settings.AUTH_USER_MODEL, dispatch_uid="structure.account_deleted")
