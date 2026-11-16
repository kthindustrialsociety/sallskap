"""Tests for seat rules (structure.md §4), written before the code. First a table of the
seats a set of rules gives over the tree in structure.md §1, and one of what each event
does to them; then the rules about rules, what a seat gives, the made-up installations
and the admin page."""

from contextlib import contextmanager
from datetime import timedelta
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import TestCase
from django.urls import reverse

from accounts.tests import CaddyClient, make_user

from . import examples
from .authority import can, units_where
from .models import Grant, Membership, Permission, Person, Position, SeatRule, Unit
from .test_authority import LONG_AGO, TODAY, Federation, User, grant, holds, someone, unit

ASSOCIATION, TEAM = Unit.Kind.ASSOCIATION, Unit.Kind.TEAM
Role = Position.Role
P = Permission
BOARD = list(Position.BOARD_ROLES)


def rule(where, role, from_roles, held_in, by, each_association_under=False, start=LONG_AGO, **fields):
    return SeatRule.objects.create(
        unit=where, role=role, from_roles=list(from_roles), held_in=held_in,
        each_association_under=each_association_under, made_by=by, start=start, **fields,
    )


def seats(**filters):
    return Position.objects.filter(seat_rule__isnull=False, **filters)


@contextmanager
def days_later(days):
    """As if it were that many days after today, for core."""
    with mock.patch("django.utils.timezone.localdate", return_value=TODAY + timedelta(days=days)):
        yield


# ---- The tables -----------------------------------------------------------------------
#
# The tree in test_authority.py, with someone in each place its table names, and these
# rules, all made by the root's chair long ago. The first four are §4's three examples.

RULES = {
    # name: (where the seats are, the role there, the roles it comes from, held in, or in
    # each association directly under it)
    "board": ("root", Role.BOARD_MEMBER, [Role.CHAIR], "root", True),  # the chairs on the root's board
    "ext-board": ("ext", Role.PARTICIPANT, BOARD, "root", False),  # the extended board: the root's board,
    "ext-chairs": ("ext", Role.PARTICIPANT, [Role.CHAIR, Role.VICE_CHAIR], "root", True),  # and each association's chair and vice chair
    "dev-lead": ("dev", Role.LEAD, [Role.VICE_CHAIR], "rob", False),  # Robotics' vice chair leads Development
    "ext-hack": ("ext", Role.PARTICIPANT, [Role.LEAD], "hack", False),  # a seat from a project's lead
}

# Who holds a seat by which rule: all from long ago, and open. Nobody else holds one.
# Seats come from appointed positions only, so chair@chem, on the root's board by a
# seat, is on the extended board by ext-chairs alone.

SEATS = """
                  board  ext-board  ext-chairs  dev-lead  ext-hack
chair@root          .        x          .          .         .
treasurer@root      .        x          .          .         .
secretary@root      .        x          .          .         .
vice_chair@rob      .        .          x          x         .
chair@chem          x        .          x          .         .
lead@hack           .        .          .          .         x
"""

# What each event does to the seats above. A seat not named keeps its line in the table.
# Days count from today; long is long ago, - is open, and none is no seat at all. Two
# lines for one seat are two seats, one after the other, by the same rule from the same
# position. hack-help is a rule an event makes: a participant seat in the hackathon for
# Robotics' treasurer.

EVENTS = """
event                                   holder            rule         start   end
robotics-gets-a-chair                   chair@rob         board        0       -
robotics-gets-a-chair                   chair@rob         ext-chairs   0       -
robotics-elects-a-chair-for-next-month  chair@rob         board        30      -
robotics-elects-a-chair-for-next-month  chair@rob         ext-chairs   30      -
chemistry-changes-chair                 chair@chem        board        long    0
chemistry-changes-chair                 chair@chem        ext-chairs   long    0
chemistry-changes-chair                 next@chem         board        0       -
chemistry-changes-chair                 next@chem         ext-chairs   0       -
a-chair-ends-today                      chair@chem        board        long    0
a-chair-ends-today                      chair@chem        ext-chairs   long    0
a-chair-ends-next-month                 chair@chem        board        long    30
a-chair-ends-next-month                 chair@chem        ext-chairs   long    30
a-chairs-end-is-taken-back              chair@chem        board        long    -
a-chairs-end-is-taken-back              chair@chem        ext-chairs   long    -
robotics-vice-chair-leaves              vice_chair@rob    ext-chairs   long    0
robotics-vice-chair-leaves              vice_chair@rob    dev-lead     long    0
a-rule-ends-today                       chair@chem        board        long    0
a-rule-is-made-today                    treasurer@rob     hack-help    0       -
a-rule-is-moved-later                   treasurer@rob     hack-help    60      -
a-rule-starts-after-its-holder-leaves   treasurer@rob     hack-help    30      30
a-rule-is-made-after-its-holder-left    treasurer@rob     hack-help    none    -
chemistry-moves-into-robotics           chair@chem        board        long    0
chemistry-moves-into-robotics           chair@chem        ext-chairs   long    0
chemistry-moves-out-and-back            chair@chem        board        long    -
chemistry-moves-out-and-back            chair@chem        ext-chairs   long    -
chemistry-is-away-for-a-month           chair@chem        board        long    0
chemistry-is-away-for-a-month           chair@chem        board        30      -
chemistry-is-away-for-a-month           chair@chem        ext-chairs   long    0
chemistry-is-away-for-a-month           chair@chem        ext-chairs   30      -
a-chairs-end-is-put-back-after-a-move   chair@chem        board        long    -10
a-chairs-end-is-put-back-after-a-move   chair@chem        ext-chairs   long    -10
a-past-chair-comes-in-with-physics      past@phys         board        none    -
a-past-chair-comes-in-with-physics      past@phys         ext-chairs   none    -
an-association-is-added                 chair@phys        board        0       -
an-association-is-added                 chair@phys        ext-chairs   0       -
the-hackathon-is-archived               lead@hack         ext-hack     long    0
the-hackathon-comes-back                lead@hack         ext-hack     long    -
the-hackathon-comes-back-a-month-later  lead@hack         ext-hack     long    0
the-hackathon-comes-back-a-month-later  lead@hack         ext-hack     30      -
a-team-with-seats-is-archived           lead@hack         ext-hack     long    0
a-team-with-seats-is-archived           treasurer@rob     hack-help    long    -
a-team-with-seats-is-archived           next@rob          hack-help    none    -
"""


def day(cell):
    if cell == "long":
        return LONG_AGO
    return None if cell == "-" else TODAY + timedelta(days=int(cell))


def parse_events():
    """{event: {(holder, rule): [(start, end), ...]}}, with an empty list for no seat."""
    events = {}
    for line in EVENTS.strip().splitlines()[1:]:
        event, holder, name, start, end = line.split()
        rows = events.setdefault(event, {}).setdefault((holder, name), [])
        if start != "none":
            rows.append((day(start), day(end)))
    return events


class Seated(Federation):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        chair = cls.people["chair@root"]
        cls.rules = {
            name: rule(cls.u[where], role, from_roles, cls.u[held_in], chair, each_association_under=each)
            for name, (where, role, from_roles, held_in, each) in RULES.items()
        }

    def held(self):
        """{(holder, rule): [(start, end), ...]} for every seat there is, by the names above."""
        names = {p.pk: name for name, p in self.people.items()}
        rules = {r.pk: name for name, r in self.rules.items()}
        found = {}
        for seat in seats().select_related("seat_from").order_by("pk"):
            self.assertEqual(seat.seat_from.person_id, seat.person_id)
            self.assertIn(seat.seat_from.role, seat.seat_rule.from_roles)
            found.setdefault((names[seat.person_id], rules[seat.seat_rule_id]), []).append((seat.start, seat.end))
        return found

    def expected(self):
        expected = {}
        for line in SEATS.strip().splitlines()[1:]:
            holder, *marks = line.split()
            assert len(marks) == len(RULES), line
            expected.update({(holder, name): [(LONG_AGO, None)] for name, mark in zip(RULES, marks) if mark == "x"})
        return expected


class Table(Seated):
    def test_the_rules_give_these_seats(self):
        self.assertEqual(self.held(), self.expected())

    def test_every_event_has_its_steps(self):
        for event in parse_events():
            self.assertTrue(hasattr(Events, event.replace("-", "_")), event)


class Events(Seated):
    """Each event in EVENTS is a test of its own, made below the class."""

    def check(self, event):
        getattr(self, event.replace("-", "_"))()
        expected = self.expected()
        for key, rows in parse_events()[event].items():
            expected.pop(key, None)
            if rows:
                expected[key] = rows
        self.assertEqual(self.held(), expected)

    # The steps of each event.

    def appoint(self, holder, where, role, start=TODAY, member_of=None):
        p = someone(holder, member_of=member_of or where)
        self.people[holder] = p
        return holds(p, where, role, start=start)

    def position(self, holder):
        return Position.objects.get(person=self.people[holder], seat_rule=None)

    def hack_help(self, start=LONG_AGO):
        self.rules["hack-help"] = rule(self.u["hack"], Role.PARTICIPANT, [Role.TREASURER], self.u["rob"], self.people["chair@root"], start=start)
        return self.rules["hack-help"]

    def robotics_gets_a_chair(self):
        self.appoint("chair@rob", self.u["rob"], Role.CHAIR)

    def robotics_elects_a_chair_for_next_month(self):
        self.appoint("chair@rob", self.u["rob"], Role.CHAIR, start=TODAY + timedelta(days=30))

    def chemistry_changes_chair(self):
        self.appoint("next@chem", self.u["chem"], Role.CHAIR)  # which ends the last chair's position today

    def a_chair_ends_today(self):
        self.position("chair@chem").end_on(TODAY)

    def a_chair_ends_next_month(self):
        self.position("chair@chem").end_on(TODAY + timedelta(days=30))

    def a_chairs_end_is_taken_back(self):
        self.position("chair@chem").end_on(TODAY)
        position = self.position("chair@chem")
        position.end = None
        position.save()

    def robotics_vice_chair_leaves(self):
        self.position("vice_chair@rob").end_on(TODAY)

    def a_rule_ends_today(self):
        self.rules["board"].end_on(TODAY)

    def a_rule_is_made_today(self):
        self.hack_help(start=TODAY)

    def a_rule_is_moved_later(self):
        r = self.hack_help(start=TODAY + timedelta(days=30))
        r.start = TODAY + timedelta(days=60)
        r.save()

    def a_rule_starts_after_its_holder_leaves(self):
        self.hack_help(start=TODAY + timedelta(days=30))
        self.position("treasurer@rob").end_on(TODAY + timedelta(days=10))

    def a_rule_is_made_after_its_holder_left(self):
        self.position("treasurer@rob").end_on(TODAY - timedelta(days=10))
        self.hack_help(start=TODAY)

    def chemistry_moves_into_robotics(self):
        self.u["chem"].parent = self.u["rob"]
        self.u["chem"].save()

    def chemistry_moves_out_and_back(self):
        self.chemistry_moves_into_robotics()
        chem = Unit.objects.get(pk=self.u["chem"].pk)
        chem.parent = self.u["root"]
        chem.save()

    def chemistry_is_away_for_a_month(self):
        self.chemistry_moves_into_robotics()
        with days_later(30):
            chem = Unit.objects.get(pk=self.u["chem"].pk)
            chem.parent = self.u["root"]
            chem.save()

    def a_chairs_end_is_put_back_after_a_move(self):
        self.chemistry_moves_into_robotics()
        self.position("chair@chem").end_on(TODAY - timedelta(days=10))

    def a_past_chair_comes_in_with_physics(self):
        # Physics' chair stepped down while Physics sat inside Robotics, before it moved.
        physics = unit(ASSOCIATION, "physics", self.u["rob"])
        self.appoint("past@phys", physics, Role.CHAIR, start=LONG_AGO).end_on(TODAY - timedelta(days=10))
        physics.parent = self.u["root"]
        physics.save()
        position = self.position("past@phys")
        position.title = "Founding chair"  # nothing about the dates
        position.save()
        unit(TEAM, "spare", self.u["root"]).archive()  # nor about Physics

    def an_association_is_added(self):
        physics = unit(ASSOCIATION, "physics", self.u["root"])
        self.appoint("chair@phys", physics, Role.CHAIR)

    def the_hackathon_is_archived(self):
        self.u["hack"].archive()

    def the_hackathon_comes_back(self):
        self.u["hack"].archive()
        Unit.objects.get(pk=self.u["hack"].pk).unarchive()

    def the_hackathon_comes_back_a_month_later(self):
        self.u["hack"].archive()
        with days_later(30):
            Unit.objects.get(pk=self.u["hack"].pk).unarchive()

    def a_team_with_seats_is_archived(self):
        self.hack_help()
        self.u["hack"].archive()
        self.appoint("next@rob", self.u["rob"], Role.TREASURER)  # gets no seat in an archived team


for _event in parse_events():
    setattr(Events, "test_" + _event.replace("-", "_"), lambda self, event=_event: self.check(event))


# ---- Rules about rules --------------------------------------------------------------


class Rules(Federation):
    def setUp(self):
        self.chair = self.people["chair@root"]

    def board_rule(self, **fields):
        return rule(self.u["root"], Role.BOARD_MEMBER, [Role.CHAIR], self.u["root"], self.chair, each_association_under=True, **fields)

    # ---- When seats start and end

    def test_a_new_chair_gets_the_seat_the_moment_they_are_appointed(self):
        self.board_rule()
        new = someone("New", member_of=self.u["rob"])
        self.assertFalse(can(new, P.MANAGE_EVENTS, self.u["root"]))
        holds(new, self.u["rob"], Role.CHAIR, start=TODAY)
        self.assertTrue(can(new, P.MANAGE_EVENTS, self.u["root"]))
        self.assertTrue(seats(person=new, unit=self.u["root"], role=Role.BOARD_MEMBER).current().exists())

    def test_ending_a_rule_ends_the_seats_it_gave(self):
        r = self.board_rule()
        r.end_on(TODAY)
        self.assertEqual({s.end for s in r.seats.all()}, {TODAY})
        self.assertFalse(can(self.people["chair@chem"], P.MANAGE_EVENTS, self.u["root"]))

    def test_changing_a_rule_is_ending_it_and_making_a_new_one(self):
        r = self.board_rule()
        changes = {
            "unit": self.u["chem"], "role": Role.SECRETARY, "from_roles": [Role.CHAIR, Role.VICE_CHAIR],
            "held_in": self.u["rob"], "each_association_under": False, "made_by": self.people["vice_chair@rob"],
        }
        for field, value in changes.items():
            with self.subTest(field), self.assertRaises(ValidationError):
                fresh = SeatRule.objects.get(pk=r.pk)
                setattr(fresh, field, value)
                fresh.save()
        r.end_on(TODAY)
        wider = rule(self.u["root"], Role.BOARD_MEMBER, [Role.CHAIR, Role.VICE_CHAIR], self.u["root"], self.chair, each_association_under=True, start=TODAY)
        self.assertEqual({s.person for s in wider.seats.all()}, {self.people["chair@chem"], self.people["vice_chair@rob"]})
        self.assertEqual(seats(seat_rule=r).get().end, TODAY)

    def test_a_seat_stays_in_the_record_when_it_ends(self):
        self.board_rule()
        Position.objects.get(person=self.people["chair@chem"], seat_rule=None).end_on(TODAY)
        seat = seats(person=self.people["chair@chem"]).get()
        self.assertEqual(seat.end, TODAY)
        self.assertTrue(can(self.people["chair@chem"], P.MANAGE_EVENTS, self.u["root"], on=TODAY - timedelta(days=1)))
        with self.assertRaises(ValidationError):
            seat.delete()

    def test_a_rule_is_ended_and_never_deleted(self):
        r = self.board_rule()
        with self.assertRaises(ValidationError):
            r.delete()
        with self.assertRaises(ValidationError):
            SeatRule.objects.all().delete()

    # ---- By hand

    def test_a_seat_cannot_be_set_by_hand(self):
        r = self.board_rule()
        chem_chair = Position.objects.get(person=self.people["chair@chem"], seat_rule=None)
        with self.assertRaises(ValidationError):
            Position.objects.create(person=self.people["chair@chem"], unit=self.u["root"], role=Role.BOARD_MEMBER, start=LONG_AGO, seat_rule=r, seat_from=chem_chair)
        self.assertEqual(seats().count(), 1)

    def test_a_seat_cannot_be_changed_or_ended_by_hand(self):
        self.board_rule()
        seat = seats().get()
        with self.assertRaises(ValidationError):
            seat.end_on(TODAY)
        for field, value in (("title", "Ex officio"), ("start", TODAY), ("appointed_by", "The board")):
            with self.subTest(field), self.assertRaises(ValidationError):
                fresh = Position.objects.get(pk=seat.pk)
                setattr(fresh, field, value)
                fresh.save()
        self.assertIsNone(Position.objects.get(pk=seat.pk).end)

    def test_an_appointed_position_never_becomes_a_seat(self):
        r = self.board_rule()
        appointed = holds(someone("Plain", member_of=self.u["root"]), self.u["root"], Role.BOARD_MEMBER)
        appointed.seat_rule = r
        appointed.seat_from = Position.objects.get(person=self.people["chair@chem"], seat_rule=None)
        with self.assertRaises(ValidationError):
            appointed.save()

    # ---- Who sets them

    def test_whoever_can_appoint_in_the_unit_sets_its_rules(self):
        made = [
            ("chair@root", "root", Role.BOARD_MEMBER, [Role.CHAIR], "root", True),
            ("vice_chair@rob", "dev", Role.LEAD, [Role.VICE_CHAIR], "rob", False),
            ("lead@dev", "uavs", Role.PARTICIPANT, [Role.LEAD], "dev", False),  # appoints in the teams under theirs
        ]
        for maker, where, role, from_roles, held_in, each in made:
            with self.subTest(maker):
                rule(self.u[where], role, from_roles, self.u[held_in], self.people[maker], each_association_under=each)
        refused = [
            ("lead@dev", "dev", Role.PARTICIPANT, [Role.LEAD], "uavs"),  # not in their own team: that's the head above
            ("treasurer@root", "ext", Role.PARTICIPANT, [Role.TREASURER], "rob"),
            ("vice_chair@rob", "ext", Role.PARTICIPANT, [Role.VICE_CHAIR], "rob"),  # no say at the root
            ("operator", "ext", Role.PARTICIPANT, [Role.TREASURER], "rob"),
        ]
        for maker, where, role, from_roles, held_in in refused:
            with self.subTest(maker=maker, where=where), self.assertRaises(ValidationError):
                rule(self.u[where], role, from_roles, self.u[held_in], self.people[maker])
        self.assertEqual(SeatRule.objects.count(), len(made))

    def test_the_maker_must_have_been_able_to_on_the_day_it_starts(self):
        late = someone("Late", member_of=self.u["chem"])
        holds(late, self.u["chem"], Role.VICE_CHAIR, start=TODAY - timedelta(days=10))
        args = (self.u["chem"], Role.SECRETARY, [Role.CHAIR], self.u["chem"], late)
        with self.assertRaises(ValidationError):
            rule(*args, start=TODAY - timedelta(days=20))
        r = rule(*args, start=TODAY - timedelta(days=10))
        r.start = TODAY - timedelta(days=20)
        with self.assertRaises(ValidationError):
            r.save()

    # ---- Which seats

    def test_a_seat_is_never_a_chair(self):
        with self.assertRaises(ValidationError):
            rule(self.u["root"], Role.CHAIR, [Role.CHAIR], self.u["chem"], self.chair)
        self.assertFalse(SeatRule.objects.exists())

    def test_the_roles_fit_the_units(self):
        refused = [
            ("ext", Role.BOARD_MEMBER, [Role.CHAIR], "root", False),  # a board role in a team
            ("rob", Role.LEAD, [Role.CHAIR], "rob", False),  # a lead in an association
            ("ext", Role.PARTICIPANT, [Role.LEAD], "rob", False),  # leads aren't held in associations
            ("ext", Role.PARTICIPANT, [Role.LEAD], "root", True),  # nor in the associations under one
            ("ext", Role.PARTICIPANT, [Role.CHAIR], "dev", True),  # and a team has none under it
            ("ext", Role.PARTICIPANT, [], "root", False),  # it comes from some role
            ("ext", Role.PARTICIPANT, ["president"], "root", False),  # that the kit knows
        ]
        for where, role, from_roles, held_in, each in refused:
            with self.subTest(where=where, role=role, from_roles=from_roles, each=each), self.assertRaises(ValidationError):
                rule(self.u[where], role, from_roles, self.u[held_in], self.chair, each_association_under=each)
        self.assertFalse(SeatRule.objects.exists())

    def test_a_seat_that_needs_a_membership_comes_from_a_board_role_inside_that_association(self):
        refused = [
            ("root", Role.BOARD_MEMBER, [Role.LEAD], "hack", False),  # a lead needn't be a member of the root
            ("rob", Role.BOARD_MEMBER, [Role.CHAIR], "root", False),  # the root's chair needn't be one of Robotics
            ("chem", Role.BOARD_MEMBER, [Role.CHAIR], "root", True),  # nor Robotics' chair one of Chemistry
            ("dev", Role.LEAD, [Role.TREASURER], "root", False),  # Development belongs to Robotics
            ("dev", Role.LEAD, [Role.LEAD], "uavs", False),
        ]
        for where, role, from_roles, held_in, each in refused:
            with self.subTest(where=where, held_in=held_in, each=each), self.assertRaises(ValidationError):
                rule(self.u[where], role, from_roles, self.u[held_in], self.chair, each_association_under=each)
        allowed = [
            ("root", Role.BOARD_MEMBER, [Role.CHAIR], "chem", False),
            ("dev", Role.LEAD, [Role.VICE_CHAIR], "rob", False),
            ("ext", Role.PARTICIPANT, [Role.LEAD], "hack", False),  # a participant needs no membership
            ("uavs", Role.PARTICIPANT, [Role.TREASURER], "root", False),
        ]
        for where, role, from_roles, held_in, each in allowed:
            with self.subTest(where=where, held_in=held_in):
                rule(self.u[where], role, from_roles, self.u[held_in], self.chair, each_association_under=each)

    def test_a_move_that_would_break_a_rule_is_refused(self):
        lab = unit(TEAM, "lab", self.u["rob"])  # nobody in it, so only the rule stands in the way
        rule(lab, Role.LEAD, [Role.CHAIR], self.u["rob"], self.chair)  # Robotics has no chair yet
        lab.parent = self.u["chem"]
        with self.assertRaises(ValidationError):
            lab.save()
        self.assertEqual(Unit.objects.get(pk=lab.pk).parent, self.u["rob"])
        free = unit(TEAM, "free", self.u["rob"])
        free.parent = self.u["chem"]
        free.save()  # the same move, without a rule

    def test_seats_come_only_from_appointed_positions(self):
        self.board_rule()
        appointed = someone("Appointed", member_of=self.u["root"])
        holds(appointed, self.u["root"], Role.BOARD_MEMBER)
        ext = rule(self.u["ext"], Role.PARTICIPANT, [Role.BOARD_MEMBER], self.u["root"], self.chair)
        self.assertEqual({s.person for s in ext.seats.all()}, {appointed})  # not chair@chem, seated on the root's board

    def test_a_lead_by_seat_and_an_appointed_lead_are_co_leads(self):
        appointed = Position.objects.get(person=self.people["lead@dev"])
        rule(self.u["dev"], Role.LEAD, [Role.VICE_CHAIR], self.u["rob"], self.chair)
        self.assertIsNone(Position.objects.get(pk=appointed.pk).end)
        leads = Position.objects.current().filter(unit=self.u["dev"], role=Role.LEAD)
        self.assertEqual({p.person for p in leads}, {self.people["lead@dev"], self.people["vice_chair@rob"]})

    def test_two_positions_it_comes_from_give_two_seats(self):
        self.board_rule()
        physics = unit(ASSOCIATION, "physics", self.u["root"])
        chair = self.people["chair@chem"]
        Membership.objects.create(person=chair, association=physics, start=LONG_AGO)
        holds(chair, physics, Role.CHAIR)
        self.assertEqual(seats(person=chair, unit=self.u["root"]).count(), 2)

    def test_a_rule_that_repeats_a_current_one_is_refused(self):
        r = self.board_rule()
        with self.assertRaises(ValidationError):
            self.board_rule()
        with self.assertRaises(ValidationError):  # overlapping roles
            rule(self.u["root"], Role.BOARD_MEMBER, [Role.CHAIR, Role.VICE_CHAIR], self.u["root"], self.chair, each_association_under=True)
        with self.assertRaises(ValidationError):  # Chemistry is one of the associations under the root
            rule(self.u["root"], Role.BOARD_MEMBER, [Role.CHAIR], self.u["chem"], self.chair)
        rule(self.u["root"], Role.BOARD_MEMBER, [Role.VICE_CHAIR], self.u["chem"], self.chair)  # another role
        r.end_on(TODAY)
        self.board_rule(start=TODAY)
        self.assertEqual(seats(person=self.people["chair@chem"]).count(), 2)  # one ended, one from today

    def test_a_role_never_comes_from_itself(self):
        for where, role in (("root", Role.BOARD_MEMBER), ("dev", Role.LEAD), ("ext", Role.PARTICIPANT)):
            with self.subTest(where), self.assertRaises(ValidationError):
                rule(self.u[where], role, [role], self.u[where], self.chair)

    def test_nothing_new_in_an_archived_unit(self):
        self.u["race"].archive()
        with self.assertRaises(ValidationError):
            rule(self.u["race"], Role.PARTICIPANT, [Role.LEAD], self.u["uavs"], self.chair)
        with self.assertRaises(ValidationError):
            rule(self.u["ext"], Role.PARTICIPANT, [Role.LEAD], self.u["race"], self.chair)

    def test_a_unit_a_rule_ever_pointed_at_is_kept(self):
        seated = unit(TEAM, "seated", self.u["root"])
        source = unit(TEAM, "source", self.u["root"])  # nobody holds the role yet
        rule(seated, Role.PARTICIPANT, [Role.LEAD], source, self.chair).end_on(TODAY)
        for u in (seated, source):
            self.assertIsNotNone(u.deletion_problem(), u)
            with self.assertRaises(ValidationError):
                u.delete()
        seated.kind = ASSOCIATION
        with self.assertRaises(ValidationError):
            seated.save()

    def test_merging_moves_the_rules_someone_made_and_their_seats(self):
        maker = someone("Maker", member_of=self.u["chem"])
        holds(maker, self.u["chem"], Role.VICE_CHAIR)
        r = rule(self.u["chem"], Role.BOARD_MEMBER, [Role.CHAIR], self.u["chem"], maker)
        with_account = User.objects.create_user("maker@example.org", name="Maker").person
        kept = Person.merge(maker, with_account)
        self.assertEqual(SeatRule.objects.get(pk=r.pk).made_by, kept)
        self.assertEqual(seats(seat_rule=r).get().person, self.people["chair@chem"])
        chair_account = User.objects.create_user("chem-chair@example.org", name="Chem").person
        kept = Person.merge(self.people["chair@chem"], chair_account)
        self.assertEqual(seats(seat_rule=r).get().person, kept)


# ---- What a seat gives ----------------------------------------------------------------


class WhatASeatGives(Federation):
    def setUp(self):
        self.chair = self.people["chair@root"]

    def test_a_seat_gives_exactly_its_role(self):
        # Robotics' chair, seated on the root's board, can do what the root's board
        # member can, and nothing in Chemistry.
        robotics_chair = someone("Robotics chair", member_of=self.u["rob"])
        holds(robotics_chair, self.u["rob"], Role.CHAIR)
        before = {permission: set(units_where(robotics_chair, permission)) for permission in Permission}
        board_member = someone("Board member", member_of=self.u["root"])
        holds(board_member, self.u["root"], Role.BOARD_MEMBER)
        rule(self.u["root"], Role.BOARD_MEMBER, [Role.CHAIR], self.u["root"], self.chair, each_association_under=True)
        for permission in Permission:
            with self.subTest(permission):
                self.assertEqual(set(units_where(robotics_chair, permission)), before[permission] | set(units_where(board_member, permission)))
                self.assertFalse(can(robotics_chair, permission, self.u["chem"]))

    def test_a_seat_needs_2fa_if_its_role_does(self):
        physics = unit(ASSOCIATION, "physics", self.u["root"])
        chair = someone("Physics", member_of=physics, account=True)
        rule(self.u["root"], Role.BOARD_MEMBER, [Role.CHAIR], self.u["root"], self.chair, each_association_under=True)
        source = holds(chair, physics, Role.CHAIR)
        # End the chair behind core's back, so the seat is all that's left to count.
        Position.objects.filter(pk=source.pk).update(end=TODAY)
        self.assertTrue(seats(seat_from=source).get().is_current())
        self.assertTrue(User.objects.get(pk=chair.user_id).requires_2fa)

        lead = someone("Hackathon", member_of=self.u["root"], account=True)
        holds(lead, self.u["hack"], Role.LEAD)
        rule(self.u["ext"], Role.PARTICIPANT, [Role.LEAD], self.u["hack"], self.chair)
        self.assertTrue(seats(person=lead).exists())
        self.assertFalse(User.objects.get(pk=lead.user_id).requires_2fa)  # a participant seat doesn't

    def test_a_grant_to_the_roots_board_members_reaches_its_seat_holders(self):
        grant(P.SEE_MEMBERS, Role.BOARD_MEMBER, self.u["root"], self.u["root"], self.chair, include_below=True)
        chem_chair = self.people["chair@chem"]
        self.assertFalse(can(chem_chair, P.SEE_MEMBERS, self.u["rob"]))
        rule(self.u["root"], Role.BOARD_MEMBER, [Role.CHAIR], self.u["root"], self.chair, each_association_under=True)
        self.assertTrue(can(chem_chair, P.SEE_MEMBERS, self.u["rob"]))
        Position.objects.get(person=chem_chair, seat_rule=None).end_on(TODAY)
        self.assertFalse(can(chem_chair, P.SEE_MEMBERS, self.u["rob"]))

    def test_otherwise_a_seat_is_indistinguishable_from_an_appointed_position(self):
        # Chemistry's chair on the root's board by a seat, then by appointment instead.
        grant(P.SEE_MEMBERS, Role.BOARD_MEMBER, self.u["root"], self.u["rob"], self.chair)
        chem_chair = self.people["chair@chem"]

        def answers():
            on_board = Position.objects.current().filter(unit=self.u["root"], role=Role.BOARD_MEMBER)
            return (
                {permission: set(units_where(chem_chair, permission)) for permission in Permission},
                {p.person for p in on_board},
                User.objects.get(pk=self.account.pk).requires_2fa,
            )

        self.account = User.objects.create_user("chem-chair@example.org", name="Chem")
        chem_chair = Person.merge(chem_chair, self.account.person)
        with transaction.atomic():
            rule(self.u["root"], Role.BOARD_MEMBER, [Role.CHAIR], self.u["root"], self.chair, each_association_under=True)
            by_seat = answers()
            transaction.set_rollback(True)
        holds(chem_chair, self.u["root"], Role.BOARD_MEMBER)
        self.assertEqual(by_seat, answers())
        self.assertIn(self.u["rob"], by_seat[0][P.SEE_MEMBERS])  # by the grant, which reached the seat


# ---- The made-up installations ---------------------------------------------------------


class MadeUp(TestCase):
    def build(self, name):
        spec = examples.EXAMPLES[name]
        root = Unit.objects.create(kind=ASSOCIATION, key=spec.root_key, name=spec.root_name)
        examples.build(name, root)
        return root

    def by(self, address):
        return Person.objects.get(emails__address=address)

    def names(self, positions):
        return sorted(p.person.emails.get().address.split("@")[0] for p in positions)

    def test_example_society_has_no_seat_rules(self):
        self.build("society")
        self.assertFalse(SeatRule.objects.exists())
        self.assertFalse(seats().exists())

    def test_example_federation_has_the_three_examples(self):
        root = self.build("federation")
        key = lambda k: Unit.objects.get(key=k)  # noqa: E731
        current = seats().current()
        self.assertEqual(SeatRule.objects.count(), 4)  # the extended board is two

        # The chair of each association directly under the root sits on its board...
        self.assertEqual(self.names(current.filter(unit=root, role=Role.BOARD_MEMBER)), ["kim", "quinn"])
        # ...and Quinn's predecessor did until Quinn was appointed.
        ended = seats().get(end__isnull=False)
        self.assertEqual((ended.seat_from.unit, ended.seat_from.role), (key("robotics"), Role.CHAIR))
        self.assertEqual(ended.end, ended.seat_from.end)
        self.assertEqual(ended.end, Position.objects.get(unit=key("robotics"), role=Role.CHAIR, end=None).start)

        # The extended board is the root's board and each association's chair and vice
        # chair, each once, though Quinn and Kim are also on the root's board by a seat.
        on_ext = Position.objects.current().filter(unit=key("extended-board"))
        self.assertEqual(self.names(on_ext), sorted(["una", "tor", "selma", "rikard", "ylva", "quinn", "pia", "kim"]))
        self.assertTrue(all(p.seat_rule_id for p in on_ext))

        # Development's lead is Robotics' vice chair, by a seat, and appoints UAVs' lead.
        lead = Position.objects.current().get(unit=key("development"), role=Role.LEAD)
        self.assertEqual((self.names([lead]), lead.seat_from.role), (["pia"], Role.VICE_CHAIR))
        self.assertTrue(can(lead.person, P.APPOINT, key("uavs")))

        # The grant to the root's board members reaches the chairs seated there, once it starts.
        quinn, chemistry = self.by("quinn@example.org"), key("chemistry")
        seen = Grant.objects.get(permission=P.SEE_MEMBERS)
        self.assertTrue(can(quinn, P.SEE_MEMBERS, chemistry))
        self.assertFalse(can(quinn, P.SEE_MEMBERS, chemistry, on=seen.start - timedelta(days=1)))
        self.assertFalse(can(quinn, P.MANAGE_EVENTS, chemistry))  # and the seat gives nothing else there

    def test_both_build_again_over_their_seats(self):
        root = self.build("federation")
        examples.build("federation", root, replace=True)
        examples.build("society", root, replace=True)
        self.assertFalse(SeatRule.objects.exists())
        self.assertFalse(seats().exists())


# ---- The admin page -------------------------------------------------------------------


class AdminPage(Federation):
    client_class = CaddyClient

    def enter(self, group):
        user, _ = make_user(f"{group}-admin@example.org", [group], two_factor=True)
        self.client.force_login(user)

    def form(self, **fields):
        return {
            "unit": self.u["ext"].pk, "role": "participant", "from_roles": ["chair", "vice_chair"], "held_in": self.u["root"].pk,
            "each_association_under": "on", "start": "2026-01-01", "end": "", "made_by": self.people["chair@root"].pk, **fields,
        }

    def a_rule(self):
        return rule(self.u["ext"], Role.PARTICIPANT, [Role.CHAIR, Role.VICE_CHAIR], self.u["root"], self.people["chair@root"], each_association_under=True)

    def test_an_operator_sets_a_rule_and_the_seats_follow(self):
        self.enter("operator")
        response = self.client.post(reverse("admin:structure_seatrule_add"), self.form())
        self.assertEqual(response.status_code, 302, response.content.decode()[:3000])
        r = SeatRule.objects.get()
        self.assertEqual(r.from_roles, ["chair", "vice_chair"])
        self.assertEqual({s.person for s in r.seats.all()}, {self.people["chair@chem"], self.people["vice_chair@rob"]})

    def test_the_form_refuses_a_maker_who_cannot_appoint_there(self):
        self.enter("operator")
        response = self.client.post(reverse("admin:structure_seatrule_add"), self.form(made_by=self.people["treasurer@root"].pk))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(SeatRule.objects.exists())

    def test_the_form_never_offers_a_chair_seat(self):
        self.enter("operator")
        form = self.client.get(reverse("admin:structure_seatrule_add")).context["adminform"].form
        self.assertNotIn("chair", [value for value, _ in form.fields["role"].choices])
        self.assertIn("chair", [value for value, _ in form.fields["from_roles"].choices])

    def test_an_operator_ends_a_rule_and_its_seats_and_cannot_delete_it(self):
        self.enter("operator")
        r = self.a_rule()
        page = self.client.get(reverse("admin:structure_seatrule_change", args=[r.pk]))
        self.assertEqual(page.status_code, 200)
        self.client.post(reverse("admin:structure_seatrule_changelist"), {"action": "end_today", "_selected_action": [r.pk]})
        self.assertEqual(SeatRule.objects.get().end, TODAY)
        self.assertEqual({s.end for s in seats()}, {TODAY})
        self.assertEqual(self.client.post(reverse("admin:structure_seatrule_delete", args=[r.pk]), {"post": "yes"}).status_code, 403)

    def test_board_sees_rules_and_cannot_set_them(self):
        self.a_rule()
        self.enter("board")
        self.assertEqual(self.client.get(reverse("admin:structure_seatrule_changelist")).status_code, 200)
        self.assertEqual(self.client.post(reverse("admin:structure_seatrule_add"), self.form()).status_code, 403)
        self.assertEqual(SeatRule.objects.count(), 1)

    def test_a_seat_is_shown_and_never_changed(self):
        r = self.a_rule()
        seat = seats(person=self.people["chair@chem"]).get()
        self.enter("operator")
        url = reverse("admin:structure_position_change", args=[seat.pk])
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, str(r))
        self.assertEqual(self.client.post(url, {"title": "Ex officio"}).status_code, 403)
        appointed = Position.objects.get(person=self.people["chair@chem"], seat_rule=None)
        response = self.client.post(
            reverse("admin:structure_position_changelist"), {"action": "end_today", "_selected_action": [seat.pk, appointed.pk]}, follow=True,
        )
        self.assertEqual(Position.objects.get(pk=appointed.pk).end, TODAY)  # ending it ends the seat too
        self.assertEqual(Position.objects.get(pk=seat.pk).end, TODAY)
        self.assertEqual([m.level_tag for m in response.context["messages"]], ["warning", "success"])

    def test_a_position_is_never_made_a_seat_in_the_form(self):
        self.enter("operator")
        page = self.client.get(reverse("admin:structure_position_add")).content.decode()
        self.assertNotIn('name="seat_rule"', page)
        self.assertNotIn('name="seat_from"', page)

    def test_the_list_picks_out_seats(self):
        self.a_rule()
        self.enter("operator")
        listed = self.client.get(reverse("admin:structure_position_changelist"), {"seat": "yes"}).context["cl"].queryset
        self.assertEqual(set(listed), set(seats()))
