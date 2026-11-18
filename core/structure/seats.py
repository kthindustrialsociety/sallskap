"""Seats that come with another position: structure.md §4. Core alone makes, changes and
ends them, here, after a position, a seat rule or the tree changes. Position.save
refuses a seat saved from anywhere else.

A seat runs from the later of its rule's start and its source's start, the position it
comes from, to the earlier of their ends. The tree can also put a source out of the
rule's reach: its association moves out from under the rule's unit, or its team is
archived. Its seat then ends that day, and a new one starts the day it's back in reach.
Each seat keeps those two days, so its dates can always be worked out again from the
rule's and the source's, and a gap stays in the record."""

from collections import defaultdict

from django.db.models import Q

from .models import Position, SeatRule, Unit, _seating, _today


def _dates(seat, rule, source):
    ends = [day for day in (rule.end, source.end, seat.seat_until) if day is not None]
    start = max(day for day in (rule.start, source.start, seat.seat_since) if day is not None)
    end = min(ends) if ends else None
    return start, end if end is None or end >= start else start  # one that would never start ends on its start day


def _sync(rule, source, rows, today, tree=False, dates_changed=False):
    """Makes the seats this rule gives for this source, `rows`, oldest first, what they
    should be. `tree` says the tree changed today, which can put the source in or out of
    reach. Otherwise a missing seat is only made when the rule's or the source's dates are
    new or changed, so an edit to anything else never makes one reaching into the past."""
    latest = rows[-1] if rows else None
    in_reach = rule.takes(source) and not source.unit.archived
    if in_reach and tree and latest is not None and latest.seat_until is not None:
        if latest.seat_until >= today:  # back the same day it went: as if it never left
            latest.seat_until = None
        else:
            _add(rows, rule, source, since=today)
    elif in_reach and latest is None and (tree or dates_changed):
        _add(rows, rule, source, since=today if tree else None)
    elif not in_reach and latest is not None and latest.seat_until is None:
        latest.seat_until = today
    token = _seating.set(True)
    try:
        for seat in rows:
            seat.start, seat.end = _dates(seat, rule, source)
            if seat.pk is None or (seat.start, seat.end, seat.seat_until) != seat._was:
                seat.save()
    finally:
        _seating.reset(token)


def _add(rows, rule, source, since):
    if source.person_id is None:  # someone since deleted: their record is kept, and not added to
        return
    seat = Position(person_id=source.person_id, unit_id=rule.unit_id, role=rule.role, seat_rule=rule, seat_from=source, seat_since=since)
    seat.start, seat.end = _dates(seat, rule, source)
    if (seat.end is None or seat.end > seat.start) and not rule.unit.archived:  # nor anything new in an archived team
        rows.append(seat)


def _rows(rule, sources=None):
    """The seats a rule gave, by the position each comes from, in the order they were made:
    one query."""
    seats = Position.objects.filter(seat_rule=rule).order_by("pk")
    if sources is not None:
        seats = seats.filter(seat_from__in=sources)
    rows = defaultdict(list)
    for seat in seats:
        seat._was = seat.start, seat.end, seat.seat_until
        rows[seat.seat_from_id].append(seat)
    return rows


def _sync_rule(rule, tree=False, dates_changed=False):
    rows = _rows(rule)
    sources = rule.sources() | Position.objects.filter(pk__in=list(rows))
    today = _today()
    for source in sources.distinct().select_related("unit"):
        _sync(rule, source, rows[source.pk], today, tree=tree, dates_changed=dates_changed)


def after_position_saved(position, dates_changed=True):
    """For an appointed position: its seats, by every rule that could take it or gave it one."""
    position = Position.objects.select_related("unit").get(pk=position.pk)
    unit = position.unit
    reach = Q(held_in=unit.pk, each_association_under=False) | Q(seats__seat_from=position)
    if unit.kind == Unit.Kind.ASSOCIATION and unit.parent_id:
        reach |= Q(held_in=unit.parent_id, each_association_under=True)
    today = _today()
    for rule in SeatRule.objects.filter(reach).distinct().select_related("unit"):
        _sync(rule, position, _rows(rule, [position])[position.pk], today, dates_changed=dates_changed)


def after_rule_saved(rule):
    _sync_rule(SeatRule.objects.select_related("unit").get(pk=rule.pk), dates_changed=True)


def after_tree_changed(rules):
    """After a move, archiving or bringing back: the seats of the rules whose reach that can
    change. A seat it starts starts today, and one it ends ends today."""
    for rule in rules.select_related("unit"):
        _sync_rule(rule, tree=True)
