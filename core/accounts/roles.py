"""The built-in groups and their rules, all in one place. To change who can see
what, edit this table: it's applied every time the app starts."""

from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _


@dataclass(frozen=True)
class Role:
    key: str  # never changes, even if the association renames the group
    name: str
    admin: bool = False  # may enter the admin page at all
    requires_2fa: bool = False
    permissions: tuple[str, ...] = ()  # what they see and can change once inside


# Passwords to change when someone leaves: Board usually holds them, and Operators see it happen.
FOLLOW_UPS = ("accounts.view_followup", "accounts.change_followup")

ROLES = (
    Role("board", "Board", admin=True, requires_2fa=True, permissions=("accounts.view_user", *FOLLOW_UPS)),
    Role("treasurer", "Treasurer", admin=True, requires_2fa=True),
    Role("events", "Events", admin=True),
    Role("developer", "Developer"),
    Role(
        "operator",
        "Operator",
        admin=True,
        requires_2fa=True,
        permissions=(
            "accounts.view_user",
            "accounts.add_user",
            "accounts.change_user",
            "auth.view_group",
            "auth.add_group",
            "auth.change_group",
            "mfa.view_authenticator",
            "mfa.delete_authenticator",  # how someone who lost their phone starts over
            *FOLLOW_UPS,
        ),
    ),
    Role("member", "Member"),
)

ADMIN_ROLES = [role.key for role in ROLES if role.admin]
TWO_FACTOR_ROLES = [role.key for role in ROLES if role.requires_2fa]


def ensure_roles(using="default", **kwargs):
    """Create any missing group and set each role's exact permissions. Runs after
    every migrate, and changes nothing if everything is already right."""
    from django.contrib.auth.models import Group, Permission

    from .models import GroupRole

    for role in ROLES:
        link = GroupRole.objects.using(using).filter(key=role.key).select_related("group").first()
        if link:
            group = link.group
        else:
            group, _created = Group.objects.using(using).get_or_create(name=role.name)
            GroupRole.objects.using(using).create(group=group, key=role.key)
        wanted = []
        for permission in role.permissions:
            app_label, codename = permission.split(".")
            wanted.append(
                Permission.objects.using(using).get(content_type__app_label=app_label, codename=codename)
            )
        group.permissions.set(wanted)
