"""Operator, the one group (structure.md §4 and §5): the people who run the server. It's
given by hand, and everything else someone can reach comes from their memberships and
positions. Its permissions in the admin page are set here, every time the app starts."""

OPERATOR = "operator"  # its fixed key: renaming the group changes nothing
NAME = "Operator"

# Passwords to change when someone leaves the vault.
FOLLOW_UPS = ("accounts.view_followup", "accounts.change_followup")
# The tree, people, their ties, grants, seat rules and the audit log: shown, and never
# changed in the admin page, as operators never pass can() (structure.md §4 and §7).
# Changes go through structure/actions.py. Merging two people only needs to see them.
SHOWN = ("unit", "person", "emailaddress", "membership", "position", "reason", "grant", "seatrule", "auditentry")

PERMISSIONS = (
    "accounts.view_user",
    "accounts.add_user",
    "accounts.change_user",
    "mfa.view_authenticator",
    "mfa.delete_authenticator",  # how someone who lost their phone starts over
    *FOLLOW_UPS,
    *(f"structure.view_{model}" for model in SHOWN),
)


def ensure_roles(using="default", **kwargs):
    """Create the Operator group if it's missing and set its exact permissions. Runs after
    every migrate, and changes nothing if everything is already right."""
    from django.contrib.auth.models import Group, Permission

    from .models import GroupRole

    link = GroupRole.objects.using(using).filter(key=OPERATOR).select_related("group").first()
    if link:
        group = link.group
    else:
        group, _created = Group.objects.using(using).get_or_create(name=NAME)
        GroupRole.objects.using(using).create(group=group, key=OPERATOR)
    wanted = []
    for permission in PERMISSIONS:
        app_label, codename = permission.split(".")
        wanted.append(Permission.objects.using(using).get(content_type__app_label=app_label, codename=codename))
    group.permissions.set(wanted)
