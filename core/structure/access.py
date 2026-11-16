"""Keys to the tools: structure.md §5. One function turns someone's current ties into the
keys the tools hear, in the login response and through the connectors, so the rule
lives here and nowhere else.

A position gives `<unit>:<role>`, and a membership `<association>:member` for its
association and every association above it. Four keys are the same in every
installation: `member`, `board` and `treasurer` from ties to the root, and `operator`
from the Operator group, the one group there is."""

from django.db.models import Q

from .models import Person, Position, Unit, _today

OPERATOR = "operator"
# Keys from roles that need two-factor authentication: they wait until it's set up.
TWO_FACTOR_KEYS = {"board", "treasurer", OPERATOR}


def keys(user, on=None):
    """Every key the account has on that day, today if not given, sorted."""
    on = on or _today()
    found = {OPERATOR} if user.pk and user.is_operator else set()
    person = Person.objects.filter(user=user).first() if user.pk else None
    if person is not None:
        found |= person_keys(person, on)
    return sorted(found)


def person_keys(person, on):
    found = set()
    not_archived = Q(unit__archived_at__isnull=True) | Q(unit__archived_at__date__gt=on)
    held = Position.objects.current(on).filter(not_archived, person=person).values_list("unit__key", "unit__parent", "role")
    for unit, parent, role in held:
        found.add(f"{unit}:{role}")
        if parent is None:  # the root
            if role in Position.BOARD_ROLES:
                found.add("board")
            if role == Position.Role.TREASURER:
                found.add("treasurer")
    member_of = person.member_of(on).values_list("key", flat=True)
    found |= {f"{association}:member" for association in member_of}
    if member_of:
        found.add("member")
    return found


def needs_2fa(key):
    return key in TWO_FACTOR_KEYS or key.rpartition(":")[2] in Position.BOARD_ROLES


def for_tools(user):
    """What the tools are told. Core won't let someone through to a tool until they've set
    up the 2FA a role requires, or every tool would be a way around the rule. Telling a
    tool about that role directly would be one too, so those keys wait for it as well."""
    found = keys(user)
    return [key for key in found if not needs_2fa(key)] if user.needs_2fa_setup else found


def known_keys():
    """Every key someone could have here, for limiting a tool to some of them."""
    found = {"member", "board", "treasurer", OPERATOR}
    for key, kind in Unit.objects.values_list("key", "kind"):
        roles = Position.BOARD_ROLES if kind == Unit.Kind.ASSOCIATION else Position.TEAM_ROLES
        found |= {f"{key}:{role}" for role in roles}
        if kind == Unit.Kind.ASSOCIATION:
            found.add(f"{key}:member")
    return found


def tell_tools(people):
    """Asks the worker to tell the tools about these people's accounts, in the same save
    as the change: see accounts/sync.py. People with no account have nothing to tell."""
    from accounts import sync

    for person in people.filter(user__isnull=False).select_related("user").distinct():
        sync.request_for(person.user)


def in_units(units):
    """Everyone with a tie to these units: a position in one, or a membership of one."""
    return Person.objects.filter(Q(positions__unit__in=units) | Q(memberships__association__in=units))
