"""Who can do what, and where: structure.md §4. Everything is answered from a person's
current positions and the grants to the roles they hold, so the rule lives here and
nowhere else. From A6 on, every action asks can() exactly once.

Operators never pass: Operator is a group for running the server, not a role in the
tree. Neither do accounts: can() takes a person, and the way in checks the account."""

from functools import reduce
from operator import or_

from django.db.models import Q

from .models import GRANTABLE, Grant, Permission, Position, Tie, Unit, _today

P = Permission
R = Position.Role

# How far a position or grant reaches from the unit it's in.
ONLY = "only"  # that unit alone
SUBTREE = "subtree"  # that unit and everything inside it
BELOW = "below"  # everything inside it, but not the unit itself
OWN = "own"  # an association and its teams, stopping at the associations inside it
SERVED = "served"  # teams run for that association, or for one inside it

BOARD = (P.SEE_MEMBERS, P.MANAGE_MEMBERSHIPS, P.MANAGE_EVENTS, P.SEND_MAILINGS, P.MANAGE_DOCUMENTS)
HEAD = (*((permission, SUBTREE) for permission in P), (P.END_SERVICE, SERVED))

ROLES = {
    R.CHAIR: HEAD,
    R.VICE_CHAIR: HEAD,
    R.TREASURER: tuple((permission, OWN) for permission in (*BOARD, P.MANAGE_FINANCES)),
    R.SECRETARY: tuple((permission, OWN) for permission in BOARD),  # minutes are documents
    R.BOARD_MEMBER: tuple((permission, OWN) for permission in BOARD),
    # A lead appoints in the teams under theirs: their own team's lead is for the head above.
    R.LEAD: (
        *((permission, SUBTREE) for permission in (P.SEE_MEMBERS, P.MANAGE_PARTICIPANTS, P.MANAGE_EVENTS, P.SEND_MAILINGS, P.MANAGE_UNITS)),
        (P.APPOINT, BELOW),
    ),
    R.PARTICIPANT: (),
}

# Some permissions only mean something in one kind of unit.
ONLY_IN = {
    P.MANAGE_MEMBERSHIPS: Q(kind=Unit.Kind.ASSOCIATION),
    P.MANAGE_PARTICIPANTS: Q(kind=Unit.Kind.TEAM),
    P.END_SERVICE: Q(run_for__isnull=False),
}


def _reach(person, permission, on):
    """(scope, unit) for each position or grant giving the person this permission on that
    day. A position in a unit archived by then gives nothing, and nor does a grant to it."""
    not_archived = Q(unit__archived_at__isnull=True) | Q(unit__archived_at__date__gt=on)
    positions = list(Position.objects.current(on).filter(not_archived, person=person).select_related("unit"))
    for position in positions:
        for given, scope in ROLES[position.role]:
            if given == permission:
                yield scope, position.unit
    if positions and permission in GRANTABLE:
        held = reduce(or_, (Q(role=position.role, held_in=position.unit_id) for position in positions))
        for grant in Grant.objects.current(on).filter(held, permission=permission).select_related("unit"):
            yield (SUBTREE if grant.include_below else ONLY), grant.unit


def _units(scope, unit):
    if scope == ONLY:
        return Q(pk=unit.pk)
    if scope == SUBTREE:
        return Q(path__startswith=unit.path)
    if scope == BELOW:
        return Q(path__startswith=unit.path) & ~Q(pk=unit.pk)
    if scope == SERVED:
        return Q(run_for__path__startswith=unit.path)
    inside = Unit.objects.filter(kind=Unit.Kind.ASSOCIATION, path__startswith=unit.path).exclude(pk=unit.pk)
    return reduce(lambda q, path: q & ~Q(path__startswith=path), inside.values_list("path", flat=True), Q(path__startswith=unit.path))


def units_where(person, permission, on=None):
    """Every unit where the person has this permission on that day, today if not given:
    for lists, such as the members someone can see, which can't ask can() unit by unit.
    Archived units are included, as their history is still seen: what can be added to one
    is the models' rule, which refuses anything new there."""
    permission = Permission(permission)  # a mistyped key is a bug, not a no
    if person is None or person.pk is None:
        return Unit.objects.none()
    found = [_units(scope, unit) for scope, unit in _reach(person, permission, on or _today())]
    if not found:
        return Unit.objects.none()
    return Unit.objects.filter(reduce(or_, found)).filter(ONLY_IN.get(permission, Q()))


def can(person, permission, unit, on=None):
    """Whether the person can do this in this unit on that day, today if not given."""
    return units_where(person, permission, on).filter(pk=unit.pk).exists()


def requires_2fa(user):
    """A board role anywhere needs 2FA from the day it's recorded, even if it starts
    later, until it ends. User.requires_2fa adds the groups that need it."""
    if user.pk is None:
        return False
    board_roles = Position.objects.filter(person__user=user, role__in=Position.BOARD_ROLES)
    return Tie.overlapping(board_roles, _today(), None).exists()  # not ended by today
