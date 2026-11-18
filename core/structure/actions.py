"""Every change to units, ties, grants and seat rules: structure.md §4 and §7. One function
for each change. Each asks can() exactly once, today, about the unit the change is decided
on, and leaves one row in the audit log, in the same transaction as the change.

A way in, such as the API, the member pages or a command on the server, logs the person in
and checks their account, two-factor authentication included. It passes their person and
says which way in it is (`via`). An action that finishes without having asked can()
exactly once is stopped and undone, so one can't be added without its check. Actions don't
call each other. Each works on its own copies of what it's given, read afresh, so what it
records as before is what was stored, and a refusal leaves the caller's objects as they were.

The models keep structure.md's rules whatever writes to them: a ValidationError is a rule
broken, and NotAllowed is someone without the authority. Both leave nothing written."""

import functools
from contextvars import ContextVar
from dataclasses import dataclass, field

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models, transaction
from django.utils.translation import gettext as _

from .authority import can
from .models import AuditEntry, Grant, Membership, Permission, Person, Position, Reason, SeatRule, Unit, path_and_above

P = Permission
ACTIONS = {}  # every action by its name: what C1's API gives an endpoint each


class NotAllowed(PermissionDenied):
    """The person can't do this, in this unit, today (structure.md §4)."""


@dataclass
class _Doing:
    asked: list = field(default_factory=list)
    unit: Unit | None = None
    done: tuple | None = None  # (what changed, its id, its changes)


_doing = ContextVar("doing", default=None)


def check(actor, permission, unit):
    """The one question each action asks: can this person do this, in this unit, today?"""
    doing = _doing.get()
    if doing is None:
        raise RuntimeError("check() belongs inside an action: see @action.")
    doing.asked.append(permission)
    doing.unit = unit
    if not can(actor, permission, unit):
        raise NotAllowed(_("%(person)s can't do that in %(unit)s: it needs %(permission)s there, which they don't have today.") % {
            "person": actor, "unit": unit, "permission": Permission(permission).label,
        })


def _wrap(function):
    name = function.__name__

    @functools.wraps(function)
    def run(actor, *, via, **arguments):
        AuditEntry.Via(via)  # a way in that isn't one is a bug
        actor = Person.objects.filter(pk=actor.pk).first() if isinstance(actor, Person) and actor.pk else None
        if actor is None:
            raise NotAllowed(_("Only a person can do that."))
        arguments = {name: _fresh(value) for name, value in arguments.items()}
        if _doing.get() is not None:
            raise RuntimeError(f"{name} was called from inside another action. Actions don't call each other.")
        doing = _Doing()
        token = _doing.set(doing)
        try:
            with transaction.atomic():
                result = function(actor, **arguments)
                if len(doing.asked) != 1:
                    raise RuntimeError(f"{name} asked can() {len(doing.asked)} times. Every action asks exactly once.")
                if doing.done is None:
                    raise RuntimeError(f"{name} said nothing of what it changed, so it can't be logged.")
                obj, object_id, changes = doing.done
                AuditEntry.record(name, via, account=actor.user_id, person=actor.pk, unit=doing.unit, obj=obj, object_id=object_id, changes=changes)
        finally:
            _doing.reset(token)
        return result

    return run


def action(function):
    """Makes `function` an action: see the top of this file."""
    run = _wrap(function)
    ACTIONS[function.__name__] = run
    return run


def _fresh(value):
    """A saved row passed in, read again, so the action changes its own copy."""
    if isinstance(value, models.Model) and value.pk is not None:
        return type(value)._default_manager.get(pk=value.pk)
    return value


def _done(obj, before=None, object_id=None, after=None, **more):
    """What the action changed, for its row: each field before and after, and `more`.
    `before` is None for something new."""
    after = AuditEntry.fields(obj) if after is None else after
    changes = AuditEntry.compare(before or {}, after, obj._meta.label_lower)
    _doing.get().done = (obj, object_id if object_id is not None else obj.pk, {**changes, **more})


def _set(obj, changes, allowed):
    """For the change actions: only what can change, as the rest is fixed once set."""
    unknown = sorted(set(changes) - set(allowed))
    if unknown:
        raise TypeError(f"These can't be changed here: {', '.join(unknown)}. What can: {', '.join(allowed)}.")
    for name, value in changes.items():
        setattr(obj, name, value)


def _above(unit):
    """Where moving, archiving, bringing back, deleting and serving are decided: one level up."""
    if unit.parent_id is None:
        raise ValidationError(_("%(unit)s is the root: it can't be moved, archived or deleted, and nothing is above it.") % {"unit": unit})
    return unit.parent


def _above_both(first, second):
    """The nearest unit containing both: where a move between them is decided. It can be
    one of them. manage_units always reaches a whole subtree, so whoever has it here has it
    at both ends."""
    theirs = set(path_and_above(second.path))
    return Unit.objects.get(path=[path for path in path_and_above(first.path) if path in theirs][-1])


def _appointing(role):
    """A participant is added with manage_participants, and every other role appointed."""
    return P.MANAGE_PARTICIPANTS if role == Position.Role.PARTICIPANT else P.APPOINT


def _taking_part(team):
    return sorted(team.taking_part.values_list("key", flat=True))


# ---- Units: structure.md §1 -----------------------------------------------------------

UNIT_CHANGES = ("name", "description", "primary_colour", "accent_colour", "end_date")


@action
def create_unit(actor, *, parent, kind, key, name, description="", primary_colour="", accent_colour="", end_date=None):
    """Decided by whoever has authority over the unit it goes into."""
    check(actor, P.MANAGE_UNITS, parent)
    unit = Unit(
        parent=parent, kind=kind, key=key, name=name, description=description,
        primary_colour=primary_colour, accent_colour=accent_colour, end_date=end_date,
    )
    unit.save()
    _done(unit)
    return unit


@action
def change_unit(actor, *, unit, **changes):
    """A unit's name, description, colours and end date. Its key and kind are fixed, and the
    root's name follows the config."""
    before, name = AuditEntry.fields(unit), unit.name
    _set(unit, changes, UNIT_CHANGES)
    check(actor, P.MANAGE_UNITS, unit)
    if unit.is_root and unit.name != name:
        raise ValidationError(_("The root's name follows the config: change name under [association] there."))
    unit.save()
    _done(unit, before)
    return unit


@action
def move_unit(actor, *, unit, parent):
    if parent is None:
        raise ValidationError(_("Every unit but the root sits inside another, so say which one."))
    check(actor, P.MANAGE_UNITS, _above_both(_above(unit), parent))
    before = AuditEntry.fields(unit)
    unit.parent = parent
    unit.save()
    _done(unit, before)
    return unit


@action
def archive_unit(actor, *, unit):
    """With everything under it. Its row names the units archived with it."""
    check(actor, P.MANAGE_UNITS, _above(unit))
    before = AuditEntry.fields(unit)
    with_it = list(unit.descendants().filter(archived_at__isnull=True).order_by("path").values_list("key", flat=True))
    unit.archive()
    _done(unit, before, archived_with_it=with_it)
    return unit


@action
def bring_back_unit(actor, *, unit):
    """That unit alone, and only if the one above it isn't archived."""
    check(actor, P.MANAGE_UNITS, _above(unit))
    before = AuditEntry.fields(unit)
    unit.unarchive()
    _done(unit, before)
    return unit


@action
def delete_unit(actor, *, unit):
    """Only if nothing ever pointed at it: otherwise it's archived."""
    check(actor, P.MANAGE_UNITS, _above(unit))
    before, pk = AuditEntry.fields(unit), unit.pk
    unit.delete()
    _done(unit, before, object_id=pk, after={})


@action
def add_taking_part(actor, *, team, association):
    """Another association involved in a team. It gets no say over it (structure.md §4)."""
    check(actor, P.MANAGE_UNITS, team)
    before = _taking_part(team)
    team.taking_part.add(association)
    _done(team, {}, after={}, taking_part=[before, _taking_part(team)])  # its own fields don't change
    return team


@action
def end_taking_part(actor, *, team, association):
    """Refused while a lead is a member only of that association (structure.md §3)."""
    check(actor, P.MANAGE_UNITS, team)
    before = _taking_part(team)
    team.taking_part.remove(association)
    _done(team, {}, after={}, taking_part=[before, _taking_part(team)])  # its own fields don't change
    return team


@action
def run_for(actor, *, team, association):
    """A service run for another association, set up one level up, like a move."""
    check(actor, P.MANAGE_UNITS, _above(team))
    before = AuditEntry.fields(team)
    team.run_for = association
    team.save()
    _done(team, before)
    return team


@action
def end_service(actor, *, team):
    """Either side's chair or vice chair can end it."""
    check(actor, P.END_SERVICE, team)
    before = AuditEntry.fields(team)
    team.run_for = None
    team.save()
    _done(team, before)
    return team


# ---- Positions: structure.md §3 and §4 --------------------------------------------------

POSITION_CHANGES = ("title", "appointed_by", "minutes", "start")


@action
def appoint(actor, *, person, unit, role, start=None, end=None, title="", appointed_by="", minutes=""):
    """Checked today, even for a position that starts later. Appointing a chair ends the
    last one's position the day the new one starts, and the row names it."""
    check(actor, _appointing(role), unit)
    position = Position(person=person, unit=unit, role=role, end=end, title=title, appointed_by=appointed_by, minutes=minutes)
    if start is not None:
        position.start = start
    ended = list(position.chairs_it_ends().values_list("pk", flat=True))
    position.save()
    _done(position, **({"ended": ended} if ended else {}))
    return position


@action
def change_position(actor, *, position, **changes):
    """Its title, who appointed it, its minutes and its start. Ending it is its own action."""
    before = AuditEntry.fields(position)
    _set(position, changes, POSITION_CHANGES)
    check(actor, _appointing(position.role), position.unit)
    position.save()
    _done(position, before)
    return position


@action
def end_position(actor, *, position, day=None):
    """From `day`, today if not given. A seat ends with its rule or the position it comes from."""
    check(actor, _appointing(position.role), position.unit)
    before = AuditEntry.fields(position)
    position.end_on(day)
    _done(position, before)
    return position


# ---- Memberships and reasons ----------------------------------------------------------

MEMBERSHIP_CHANGES = ("kind", "start")
REASON_CHANGES = ("source", "legal_basis", "start", "expires")


@action
def add_membership(actor, *, person, association, kind=Membership.Kind.ORDINARY, start=None, end=None):
    check(actor, P.MANAGE_MEMBERSHIPS, association)
    membership = Membership(person=person, association=association, kind=kind, end=end)
    if start is not None:
        membership.start = start
    membership.save()
    _done(membership)
    return membership


@action
def change_membership(actor, *, membership, **changes):
    before = AuditEntry.fields(membership)
    _set(membership, changes, MEMBERSHIP_CHANGES)
    check(actor, P.MANAGE_MEMBERSHIPS, membership.association)
    membership.save()
    _done(membership, before)
    return membership


@action
def end_membership(actor, *, membership, day=None):
    """Refused while a board role or lead rests on it (structure.md §3)."""
    check(actor, P.MANAGE_MEMBERSHIPS, membership.association)
    before = AuditEntry.fields(membership)
    membership.end_on(day)
    _done(membership, before)
    return membership


@action
def add_reason(actor, *, person, unit, kind, legal_basis, expires, start=None, source=""):
    """Why data is held on someone, decided by those who manage the memberships of the
    association the unit is or sits in."""
    check(actor, P.MANAGE_MEMBERSHIPS, unit.association())
    reason = Reason(person=person, unit=unit, kind=kind, legal_basis=legal_basis, expires=expires, source=source)
    if start is not None:
        reason.start = start
    reason.save()
    _done(reason)
    return reason


@action
def change_reason(actor, *, reason, **changes):
    """Such as renewing an alumnus's consent with a later expiry."""
    before = AuditEntry.fields(reason)
    _set(reason, changes, REASON_CHANGES)
    check(actor, P.MANAGE_MEMBERSHIPS, reason.unit.association())
    reason.save()
    _done(reason, before)
    return reason


@action
def end_reason(actor, *, reason, day=None):
    check(actor, P.MANAGE_MEMBERSHIPS, reason.unit.association())
    before = AuditEntry.fields(reason)
    reason.end_on(day)
    _done(reason, before)
    return reason


# ---- Grants and seat rules: structure.md §4 ---------------------------------------------


@action
def give_grant(actor, *, permission, role, held_in, unit, include_below=False, start=None, end=None):
    """Given by the actor, who must also have been able to on the day it starts."""
    check(actor, P.GIVE_GRANTS, unit)
    grant = Grant(permission=permission, role=role, held_in=held_in, unit=unit, include_below=include_below, end=end, given_by=actor)
    if start is not None:
        grant.start = start
    grant.save()
    _done(grant)
    return grant


@action
def end_grant(actor, *, grant, day=None):
    """By whoever could give it."""
    check(actor, P.GIVE_GRANTS, grant.unit)
    before = AuditEntry.fields(grant)
    grant.end_on(day)
    _done(grant, before)
    return grant


@action
def make_seat_rule(actor, *, unit, role, from_roles, held_in, each_association_under=False, start=None, end=None):
    """Made by the actor, who must also have been able to appoint there on the day it starts."""
    check(actor, P.APPOINT, unit)
    rule = SeatRule(unit=unit, role=role, from_roles=list(from_roles), held_in=held_in, each_association_under=each_association_under, end=end, made_by=actor)
    if start is not None:
        rule.start = start
    rule.save()
    _done(rule)
    return rule


@action
def end_seat_rule(actor, *, rule, day=None):
    """By whoever can appoint in its unit. Its seats end the same day."""
    check(actor, P.APPOINT, rule.unit)
    before = AuditEntry.fields(rule)
    rule.end_on(day)
    _done(rule, before)
    return rule
