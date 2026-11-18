"""What an operator does to the tree from the server, outside the actions: structure.md §4
and §7. There's one thing: recording the root's chair, which is how a new installation gets
its first one, as nobody can act before someone holds a position."""

from django.db import transaction

from .models import AuditEntry, Membership, Position, Unit, _today

SERVER = AuditEntry.Via.SERVER


@transaction.atomic
def record_root_chair(person, start=None, member_since=None):
    """Records `person` as the root's chair from `start`, today if not given, which ends the
    current chair's position that day. A chair has to be a member, and nobody else can make
    them one yet, so if they aren't a member of the root that day, this makes them one from
    `member_since`, or from the same day. Returns the position, and the membership if one was
    made. Each is a row in the audit log, from the server, with nobody as the actor."""
    root = Unit.objects.root()
    start = start or _today()
    membership = None
    if not person.is_member(root, on=start):
        membership = Membership(person=person, association=root, start=member_since or start)
        membership.save()
        AuditEntry.record("add_membership", SERVER, unit=root, obj=membership, changes=AuditEntry.compare({}, AuditEntry.fields(membership), "structure.membership"))
    ended = list(Position.objects.filter(unit=root, role=Position.Role.CHAIR, start__lt=start, end__isnull=True).values_list("pk", flat=True))
    position = Position(person=person, unit=root, role=Position.Role.CHAIR, start=start)
    position.save()
    changes = AuditEntry.compare({}, AuditEntry.fields(position), "structure.position")
    AuditEntry.record("record_root_chair", SERVER, unit=root, obj=position, changes={**changes, **({"ended": ended} if ended else {})})
    return position, membership
