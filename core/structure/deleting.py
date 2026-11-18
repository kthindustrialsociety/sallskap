"""Deleting people with nothing left: structure.md §3 and §7. Core does it itself, just
after midnight, in the worker (accounts/sync.py), and an operator can with
`manage.py delete_people`, which with --dry-run only says who.

Someone has nothing left when they have no account, active or not, no tie that's current
or starts later, and no reason that hasn't expired. They're deleted 30 days after the last
of those ended, or after they were added if they never had one. An address holds nobody,
and neither does a grant they gave or a seat rule they made. Each run works out afresh who
that is, so a night missed costs nothing, and someone a restore brings back goes again the
next night.

What's kept is the record of who held what: their ended positions, with their name and
no link to anything else of theirs. Grants and seat rules they gave stay as they were,
without the link to them. Their addresses, memberships and reasons go. The tools are told
nothing: someone without an account has no keys, and nothing in them."""

import logging
from collections import Counter
from datetime import datetime, time, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from django.utils.translation import gettext as _

from .models import AuditEntry, EmailAddress, Grant, Membership, Person, Position, Reason, SeatRule, _erase, _today

logger = logging.getLogger(__name__)

GRACE = timedelta(days=30)
# What a run's row counts, in this order.
COUNTS = ("people", "addresses", "memberships", "reasons", "positions_kept", "grants_given", "seat_rules_made", "failed")
# Each kind of tie a person can have. Sign-ups and subscriptions join these when they're built.
TIES = (Membership, Position, Reason)


def _ending_after(model, day):
    """Ties of this kind that still hold after `day`: open, or ending later."""
    end = model.end_field
    return model.objects.filter(Q(**{f"{end}__isnull": True}) | Q(**{f"{end}__gt": day}))


def due(today=None):
    """Everyone who on `today` has had nothing left for 30 days."""
    today = today or _today()
    since = today - GRACE  # whatever ended by this day has been over for 30 days
    added_since = timezone.make_aware(datetime.combine(since + timedelta(days=1), time.min))
    people = Person.objects.filter(user__isnull=True).exclude(created_at__gte=added_since)
    for model in TIES:
        people = people.filter(~Exists(_ending_after(model, since).filter(person=OuterRef("pk"))))
    return people.order_by("pk")


def forget(person, today=None):
    """Deletes one person, keeping their ended positions with their name, and returns what
    went, counted. It refuses someone with an account, or with a tie or reason that holds
    today or later. The 30 days are for due() to judge, so deleting someone who asks to be
    can use this too."""
    today = today or _today()
    if person.user_id is not None:
        raise ValidationError(_("%(person)s has an account, so they aren't deleted.") % {"person": person})
    for model in TIES:
        if _ending_after(model, today).filter(person=person).exists():
            raise ValidationError(_("%(person)s still has a tie or a reason, today or later, so they aren't deleted.") % {"person": person})
    counts = Counter(people=1)
    # A name is all that's kept. Someone who gave none is shown as since deleted, as their
    # address, the only other thing that told them apart, goes.
    counts["positions_kept"] = Position.objects.filter(person=person).update(person=None, kept_name=person.name)
    counts["grants_given"] = Grant.objects.filter(given_by=person).update(given_by=None)
    counts["seat_rules_made"] = SeatRule.objects.filter(made_by=person).update(made_by=None)
    counts["memberships"] = _erase(Membership.objects.filter(person=person))[0]
    counts["reasons"] = _erase(Reason.objects.filter(person=person))[0]
    deleted = person.delete()[1]  # with their addresses. It refuses if anything else still points at them
    counts["addresses"] = deleted.get(EmailAddress._meta.label, 0)
    return counts


def delete_people(today=None, people=None, via=AuditEntry.Via.CORE):
    """Deletes everyone due() finds, or only those of them whose ids are in `people`, as a
    made-up installation asks. One transaction, with a savepoint for each person: one that
    fails is rolled back alone, logged by id and tried again the next night, and a crash
    undoes the whole run, its row included. Leaves one row in the audit log, counts only,
    when anyone went or failed. Returns the counts."""
    today = today or _today()
    found = due(today)
    if people is not None:
        found = found.filter(pk__in=people)
    counts = Counter(dict.fromkeys(COUNTS, 0))
    # Core's database takes its write lock as a transaction begins (settings.py), so who's
    # found here can't change before the run is over.
    with transaction.atomic():
        for person in list(found):
            try:
                with transaction.atomic():
                    counts.update(forget(person, today))
            except Exception:  # one person's problem mustn't keep the others
                counts["failed"] += 1
                logger.exception("Couldn't delete person %s, who has nothing left. Trying again tomorrow.", person.pk)
        if counts["people"] or counts["failed"]:
            AuditEntry.record("delete_people", via, changes={name: counts[name] for name in COUNTS})
    return {name: counts[name] for name in COUNTS}
