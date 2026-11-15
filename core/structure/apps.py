from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models.signals import post_delete, post_migrate, post_save, pre_delete, pre_save
from django.utils.translation import gettext_lazy as _

# Every account has a person (structure.md §2). These keep the two in step.


def _account_saving(sender, instance, raw=False, update_fields=None, **kwargs):
    if raw or update_fields == frozenset({"last_login"}):
        return
    from .models import Person, account_address_problem

    # Before the account is written, so a refused address leaves nothing half done.
    problem = account_address_problem(instance)
    if problem:
        raise ValidationError(problem)
    stored = sender.objects.filter(pk=instance.pk).values("email", "name").first() if instance.pk else None
    instance._relink = (
        stored is None
        or stored["email"] != instance.email
        or stored["name"] != instance.name
        or not Person.objects.filter(user=instance.pk).exists()
    )


def _account_saved(sender, instance, raw=False, **kwargs):
    if not raw and getattr(instance, "_relink", False):
        from .models import link_account

        instance._relink = False
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
        user = settings.AUTH_USER_MODEL
        pre_save.connect(_account_saving, sender=user, dispatch_uid="structure.account_saving")
        post_save.connect(_account_saved, sender=user, dispatch_uid="structure.account_saved")
        pre_delete.connect(_account_deleting, sender=user, dispatch_uid="structure.account_deleting")
        post_delete.connect(_account_deleted, sender=user, dispatch_uid="structure.account_deleted")
