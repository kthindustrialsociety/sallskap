"""Seats that come with another position: structure.md §4. Core alone makes, changes and
ends them, here, after a position, a seat rule or the tree changes. Position.save
refuses a seat saved from anywhere else.

A seat runs from the later of its rule's start and its source's start, the position it
comes from, to the earlier of their ends. It also ends the day its source's team is
archived, and the day a move takes the source out of the rule's reach. There's one seat
for each rule and source, so one that ended and is needed again opens again."""

from contextlib import contextmanager

from django.db.models import Q
from django.utils import timezone

from .models import Position, SeatRule, Unit, _seating, _today


@contextmanager
def _by_core():
    token = _seating.set(True)
    try:
        yield
    finally:
        _seating.reset(token)


def _window(rule, source):
    archived = timezone.localdate(source.unit.archived_at) if source.unit.archived_at else None
    ends = [day for day in (rule.end, source.end, archived) if day is not None]
    return max(rule.start, source.start), min(ends) if ends else None


def _sync(rule, source, since=None):
    """Makes the seat this rule gives for this source what it should be. `since` is the
    day the tree changed: a seat that change brings starts no earlier."""
    today = _today()
    seat = Position.objects.filter(seat_rule=rule, seat_from=source).first()
    takes = rule.takes(source)
    if takes:
        start, end = _window(rule, source)
    if seat is None:
        if takes and since is not None:
            start = max(start, since)
        if not takes or (end is not None and end <= start) or rule.unit.archived:
            return  # a seat that would never start isn't made, and nothing new goes into an archived team
        seat = Position(person_id=source.person_id, unit_id=rule.unit_id, role=rule.role, start=start, end=end, seat_rule=rule, seat_from=source)
    else:
        was = seat.start, seat.end
        if takes:
            if seat.start > today or start > seat.start:  # one that has begun keeps its start
                seat.start = start
            seat.end = end
        elif seat.end is None or seat.end > today:  # moved out of the rule's reach
            seat.end = today
        if seat.end is not None and seat.end < seat.start:  # it would never start, and isn't deleted
            seat.end = seat.start
        if (seat.start, seat.end) == was:
            return
    with _by_core():
        seat.save()


def _sources(rule):
    """Who the rule seats by today's tree, and who it gave a seat before."""
    given = Position.objects.filter(seats_given__seat_rule=rule)
    return (rule.sources() | given).distinct().select_related("unit")


def after_position_saved(position):
    """For an appointed position: its seats, by every rule that takes it or gave it one."""
    position = Position.objects.select_related("unit").get(pk=position.pk)
    unit = position.unit
    reach = Q(held_in=unit.pk, each_association_under=False) | Q(seats__seat_from=position)
    if unit.kind == Unit.Kind.ASSOCIATION and unit.parent_id:
        reach |= Q(held_in=unit.parent_id, each_association_under=True)
    for rule in SeatRule.objects.filter(reach).distinct().select_related("unit"):
        _sync(rule, position)


def after_rule_saved(rule):
    rule = SeatRule.objects.select_related("unit").get(pk=rule.pk)
    for source in _sources(rule):
        _sync(rule, source)


def after_tree_changed(since=None):
    """After a move, archiving or bringing back: every rule's seats again. Rules are few."""
    for rule in SeatRule.objects.select_related("unit"):
        for source in _sources(rule):
            _sync(rule, source, since=since)
