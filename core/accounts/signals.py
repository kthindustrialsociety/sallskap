"""Every way someone's access can change marks them as waiting: see accounts/sync.py.
Changes to ties and the tree mark them in structure/access.py."""

from allauth.idp.oidc.models import Client
from allauth.mfa.models import Authenticator
from django.db.models.signals import m2m_changed, post_delete, post_save

from . import sync
from .models import ToolAccess, User


def user_saved(sender, instance, created, update_fields=None, **kwargs):
    # A new account can't have reached any tool yet, and logging in saves last_login.
    if not created and update_fields != frozenset({"last_login"}):
        sync.request_for(instance)


def user_deleted(sender, instance, **kwargs):
    sync.request_for_deleted(instance)


def groups_changed(sender, instance, action, reverse, **kwargs):
    """Someone's groups, changed from their account's side, the only one there is: there's
    no page for groups, and nothing deletes Operator."""
    if not reverse and action in ("post_add", "post_remove", "post_clear"):
        sync.request_for(instance)


def two_factor_changed(sender, instance, **kwargs):
    # Whether tools may hear about keys from roles that require it depends on this.
    sync.request_for(instance.user)


def tool_rules_changed(sender, **kwargs):
    sync.request_for_everyone()


def connect():
    post_save.connect(user_saved, sender=User)
    post_delete.connect(user_deleted, sender=User)
    m2m_changed.connect(groups_changed, sender=User.groups.through)
    post_save.connect(two_factor_changed, sender=Authenticator)
    post_delete.connect(two_factor_changed, sender=Authenticator)
    post_save.connect(tool_rules_changed, sender=Client)
    post_save.connect(tool_rules_changed, sender=ToolAccess)
    post_delete.connect(tool_rules_changed, sender=ToolAccess)
