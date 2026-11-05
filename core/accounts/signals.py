"""Every way someone's access can change marks them as waiting: see accounts/sync.py."""

from allauth.idp.oidc.models import Client
from allauth.mfa.models import Authenticator
from django.contrib.auth.models import Group
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_delete

from . import sync
from .models import ToolAccess, User


def user_saved(sender, instance, created, update_fields=None, **kwargs):
    # A new account can't have reached any tool yet, and logging in saves last_login.
    if not created and update_fields != frozenset({"last_login"}):
        sync.request_for(instance)


def user_deleted(sender, instance, **kwargs):
    sync.request_for_deleted(instance)


def groups_changed(sender, instance, action, reverse, pk_set, **kwargs):
    if not reverse:  # someone's groups were changed: instance is the person
        if action in ("post_add", "post_remove", "post_clear"):
            sync.request_for(instance)
        return
    # A group's members were changed: instance is the group. Clearing it doesn't say who was in it.
    if action == "pre_clear":
        instance._members_before = list(instance.user_set.all())
    elif action == "post_clear":
        for user in instance._members_before:
            sync.request_for(user)
    elif action in ("post_add", "post_remove"):
        for user in User.objects.filter(pk__in=pk_set):
            sync.request_for(user)


def group_saved(sender, instance, created, **kwargs):
    # Renaming a group an Operator made changes the key that tools' access lists hold.
    if not created:
        for user in instance.user_set.all():
            sync.request_for(user)


def group_deleting(sender, instance, **kwargs):
    instance._members_before = list(instance.user_set.all())


def group_deleted(sender, instance, **kwargs):
    for user in instance._members_before:
        sync.request_for(user)


def two_factor_changed(sender, instance, **kwargs):
    # Whether tools may hear about a group that requires it depends on this.
    sync.request_for(instance.user)


def tool_rules_changed(sender, **kwargs):
    sync.request_for_everyone()


def connect():
    post_save.connect(user_saved, sender=User)
    post_delete.connect(user_deleted, sender=User)
    m2m_changed.connect(groups_changed, sender=User.groups.through)
    post_save.connect(group_saved, sender=Group)
    pre_delete.connect(group_deleting, sender=Group)
    post_delete.connect(group_deleted, sender=Group)
    post_save.connect(two_factor_changed, sender=Authenticator)
    post_delete.connect(two_factor_changed, sender=Authenticator)
    post_save.connect(tool_rules_changed, sender=Client)
    post_save.connect(tool_rules_changed, sender=ToolAccess)
    post_delete.connect(tool_rules_changed, sender=ToolAccess)
