"""Tests for authority (structure.md §4), written before the code. First a table of
every role, every permission and every unit of the tree in structure.md §1, then §4's
examples, grants, two-factor authentication, the made-up installations and the admin
page."""

from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.tests import CaddyClient, make_user

from . import examples
from .authority import can, units_where
from .models import Grant, Membership, Permission, Person, Position, Unit

ASSOCIATION, TEAM = Unit.Kind.ASSOCIATION, Unit.Kind.TEAM
Role = Position.Role
P = Permission
User = get_user_model()
TODAY = timezone.localdate()
LONG_AGO = date(2020, 1, 1)


def unit(kind, key, parent=None, **fields):
    return Unit.objects.create(kind=kind, key=key, name=key.title(), parent=parent, **fields)


def someone(name, member_of=None, account=False):
    if account:
        p = User.objects.create_user(f"{name.lower()}@example.org", name=name).person
    else:
        p = Person.objects.create(name=name)
    if member_of is not None:
        Membership.objects.create(person=p, association=member_of, start=LONG_AGO)
    return p


def holds(p, where, role, start=LONG_AGO, **fields):
    return Position.objects.create(person=p, unit=where, role=role, start=start, **fields)


def grant(permission, role, held_in, over, by, include_below=False, start=LONG_AGO, **fields):
    return Grant.objects.create(
        permission=permission, role=role, held_in=held_in, unit=over, include_below=include_below, start=start, given_by=by, **fields
    )


# ---- The table ------------------------------------------------------------------------
#
# The tree in structure.md §1, with Example Chemistry beside Robotics, and Chemistry's
# website as a service Robotics runs for it. Both associations take part in the hackathon.
#
#   root              the root, an association
#   ├─ ext            Extended board
#   ├─ hack           Spring hackathon 2027, with rob and chem taking part
#   ├─ rob            Example Robotics, an association
#   │  ├─ ops         Operations
#   │  ├─ dev         Development
#   │  │  └─ uavs     UAVs
#   │  │     └─ race  Drone race 2027
#   │  └─ web         Example Chemistry's website, run for chem
#   └─ chem           Example Chemistry, an association
#
# Each row is someone holding one role, and one permission: x where they have it, and .
# where they don't. A permission missing for someone is . everywhere. vice_chair is
# tested in Robotics and chair at the root, as the two are the same; so are secretary
# and board_member. The last three hold nothing that gives them anything.

UNITS = "root ext hack rob ops dev uavs race web chem".split()
NOBODY = ("participant@uavs", "operator", "member@rob")

TABLE = """
                                          root ext hack rob ops dev uavs race web chem
chair@root          see_members            x    x   x    x   x   x   x    x    x   x
chair@root          manage_memberships     x    .   .    x   .   .   .    .    .   x
chair@root          manage_events          x    x   x    x   x   x   x    x    x   x
chair@root          send_mailings          x    x   x    x   x   x   x    x    x   x
chair@root          manage_documents       x    x   x    x   x   x   x    x    x   x
chair@root          manage_finances        x    x   x    x   x   x   x    x    x   x
chair@root          manage_participants    .    x   x    .   x   x   x    x    x   .
chair@root          appoint                x    x   x    x   x   x   x    x    x   x
chair@root          manage_units           x    x   x    x   x   x   x    x    x   x
chair@root          give_grants            x    x   x    x   x   x   x    x    x   x
chair@root          end_service            .    .   .    .   .   .   .    .    x   .

vice_chair@rob      see_members            .    .   .    x   x   x   x    x    x   .
vice_chair@rob      manage_memberships     .    .   .    x   .   .   .    .    .   .
vice_chair@rob      manage_events          .    .   .    x   x   x   x    x    x   .
vice_chair@rob      send_mailings          .    .   .    x   x   x   x    x    x   .
vice_chair@rob      manage_documents       .    .   .    x   x   x   x    x    x   .
vice_chair@rob      manage_finances        .    .   .    x   x   x   x    x    x   .
vice_chair@rob      manage_participants    .    .   .    .   x   x   x    x    x   .
vice_chair@rob      appoint                .    .   .    x   x   x   x    x    x   .
vice_chair@rob      manage_units           .    .   .    x   x   x   x    x    x   .
vice_chair@rob      give_grants            .    .   .    x   x   x   x    x    x   .
vice_chair@rob      end_service            .    .   .    .   .   .   .    .    x   .

treasurer@root      see_members            x    x   x    .   .   .   .    .    .   .
treasurer@root      manage_memberships     x    .   .    .   .   .   .    .    .   .
treasurer@root      manage_events          x    x   x    .   .   .   .    .    .   .
treasurer@root      send_mailings          x    x   x    .   .   .   .    .    .   .
treasurer@root      manage_documents       x    x   x    .   .   .   .    .    .   .
treasurer@root      manage_finances        x    x   x    .   .   .   .    .    .   .

treasurer@rob       see_members            .    .   .    x   x   x   x    x    x   .
treasurer@rob       manage_memberships     .    .   .    x   .   .   .    .    .   .
treasurer@rob       manage_events          .    .   .    x   x   x   x    x    x   .
treasurer@rob       send_mailings          .    .   .    x   x   x   x    x    x   .
treasurer@rob       manage_documents       .    .   .    x   x   x   x    x    x   .
treasurer@rob       manage_finances        .    .   .    x   x   x   x    x    x   .

secretary@root      see_members            x    x   x    .   .   .   .    .    .   .
secretary@root      manage_memberships     x    .   .    .   .   .   .    .    .   .
secretary@root      manage_events          x    x   x    .   .   .   .    .    .   .
secretary@root      send_mailings          x    x   x    .   .   .   .    .    .   .
secretary@root      manage_documents       x    x   x    .   .   .   .    .    .   .

board_member@rob    see_members            .    .   .    x   x   x   x    x    x   .
board_member@rob    manage_memberships     .    .   .    x   .   .   .    .    .   .
board_member@rob    manage_events          .    .   .    x   x   x   x    x    x   .
board_member@rob    send_mailings          .    .   .    x   x   x   x    x    x   .
board_member@rob    manage_documents       .    .   .    x   x   x   x    x    x   .

chair@chem          see_members            .    .   .    .   .   .   .    .    .   x
chair@chem          manage_memberships     .    .   .    .   .   .   .    .    .   x
chair@chem          manage_events          .    .   .    .   .   .   .    .    .   x
chair@chem          send_mailings          .    .   .    .   .   .   .    .    .   x
chair@chem          manage_documents       .    .   .    .   .   .   .    .    .   x
chair@chem          manage_finances        .    .   .    .   .   .   .    .    .   x
chair@chem          appoint                .    .   .    .   .   .   .    .    .   x
chair@chem          manage_units           .    .   .    .   .   .   .    .    .   x
chair@chem          give_grants            .    .   .    .   .   .   .    .    .   x
chair@chem          end_service            .    .   .    .   .   .   .    .    x   .

lead@dev            see_members            .    .   .    .   .   x   x    x    .   .
lead@dev            manage_events          .    .   .    .   .   x   x    x    .   .
lead@dev            send_mailings          .    .   .    .   .   x   x    x    .   .
lead@dev            manage_participants    .    .   .    .   .   x   x    x    .   .
lead@dev            appoint                .    .   .    .   .   .   x    x    .   .
lead@dev            manage_units           .    .   .    .   .   x   x    x    .   .

lead@uavs           see_members            .    .   .    .   .   .   x    x    .   .
lead@uavs           manage_events          .    .   .    .   .   .   x    x    .   .
lead@uavs           send_mailings          .    .   .    .   .   .   x    x    .   .
lead@uavs           manage_participants    .    .   .    .   .   .   x    x    .   .
lead@uavs           appoint                .    .   .    .   .   .   .    x    .   .
lead@uavs           manage_units           .    .   .    .   .   .   x    x    .   .

lead@hack           see_members            .    .   x    .   .   .   .    .    .   .
lead@hack           manage_events          .    .   x    .   .   .   .    .    .   .
lead@hack           send_mailings          .    .   x    .   .   .   .    .    .   .
lead@hack           manage_participants    .    .   x    .   .   .   .    .    .   .
lead@hack           manage_units           .    .   x    .   .   .   .    .    .   .

lead@web            see_members            .    .   .    .   .   .   .    .    x   .
lead@web            manage_events          .    .   .    .   .   .   .    .    x   .
lead@web            send_mailings          .    .   .    .   .   .   .    .    x   .
lead@web            manage_participants    .    .   .    .   .   .   .    .    x   .
lead@web            manage_units           .    .   .    .   .   .   .    .    x   .
"""


def parse(table):
    rows = {}
    for line in table.strip().splitlines()[1:]:
        if line.strip():
            holder, permission, *marks = line.split()
            assert len(marks) == len(UNITS), line
            rows[holder, Permission(permission)] = {name for name, mark in zip(UNITS, marks) if mark == "x"}
    return rows


class Federation(TestCase):
    """The tree above, with someone in each place the table names."""

    @classmethod
    def setUpTestData(cls):
        u = cls.u = {}
        u["root"] = unit(ASSOCIATION, "example")
        u["ext"] = unit(TEAM, "extended-board", u["root"])
        u["hack"] = unit(TEAM, "spring-hackathon-2027", u["root"], end_date=date(2027, 4, 25))
        u["rob"] = unit(ASSOCIATION, "robotics", u["root"])
        u["ops"] = unit(TEAM, "operations", u["rob"])
        u["dev"] = unit(TEAM, "development", u["rob"])
        u["uavs"] = unit(TEAM, "uavs", u["dev"])
        u["race"] = unit(TEAM, "drone-race-2027", u["uavs"], end_date=date(2027, 6, 5))
        u["chem"] = unit(ASSOCIATION, "chemistry", u["root"])
        u["web"] = unit(TEAM, "chemistry-website", u["rob"], run_for=u["chem"])
        u["hack"].taking_part.add(u["rob"], u["chem"])

        cls.people = {}
        for holder in sorted({holder for holder, _ in parse(TABLE)} | {"participant@uavs"}):
            role, where = holder.split("@")
            member_of = u["chem"] if where == "chem" else u["root"] if where in ("root", "ext", "hack") else u["rob"]
            p = someone(holder, member_of=None if role == "participant" else member_of)
            holds(p, u[where], role)
            cls.people[holder] = p
        cls.people["operator"] = make_user("operator@example.org", ["operator"], two_factor=True)[0].person
        cls.people["member@rob"] = someone("member", member_of=u["rob"])


class Table(Federation):
    def test_every_role_every_permission_every_unit(self):
        expected = parse(TABLE)
        self.assertEqual({holder for holder, _ in expected} | set(NOBODY), set(self.people))
        self.assertEqual({holder.split("@")[0] for holder, _ in expected} | {"participant"}, set(Role))
        wrong = []
        for holder, p in self.people.items():
            for permission in Permission:
                yes = expected.get((holder, permission), set())
                for name in UNITS:
                    if can(p, permission, self.u[name]) != (name in yes):
                        wrong.append(f"{holder} {permission} {name}: expected {'x' if name in yes else '.'}")
        self.assertEqual(wrong, [], "\n".join(wrong))

    def test_units_where_agrees_with_the_table(self):
        expected = parse(TABLE)
        for holder, p in self.people.items():
            for permission in Permission:
                with self.subTest(holder=holder, permission=permission):
                    found = {name for name in UNITS if self.u[name] in units_where(p, permission)}
                    self.assertEqual(found, expected.get((holder, permission), set()))

    def test_an_unknown_permission_is_an_error_not_a_no(self):
        with self.assertRaises(ValueError):
            can(self.people["chair@root"], "see_member", self.u["root"])

    def test_nobody_is_no_one(self):
        self.assertFalse(can(None, P.SEE_MEMBERS, self.u["root"]))
        self.assertFalse(units_where(None, P.SEE_MEMBERS).exists())


# ---- structure.md §4's examples ------------------------------------------------------


class Examples(Federation):
    def test_the_roots_treasurer_has_no_say_in_robotics_without_a_grant(self):
        treasurer = self.people["treasurer@root"]
        for name in ("rob", "ops", "web"):
            self.assertFalse(can(treasurer, P.MANAGE_FINANCES, self.u[name]), name)
        grant(P.MANAGE_FINANCES, Role.TREASURER, self.u["root"], self.u["root"], self.people["chair@root"], include_below=True)
        for name in ("rob", "ops", "web", "chem"):
            self.assertTrue(can(treasurer, P.MANAGE_FINANCES, self.u[name]), name)
        self.assertFalse(can(treasurer, P.APPOINT, self.u["rob"]))  # the grant gives that one permission only

    def test_developments_lead_appoints_uavs_lead(self):
        lead = self.people["lead@dev"]
        self.assertTrue(can(lead, P.APPOINT, self.u["uavs"]))
        self.assertTrue(can(lead, P.APPOINT, self.u["race"]))  # anyone higher up can do the same
        self.assertFalse(can(lead, P.APPOINT, self.u["dev"]))  # a co-lead is for the head above
        self.assertTrue(can(lead, P.MANAGE_PARTICIPANTS, self.u["dev"]))
        self.assertTrue(can(self.people["vice_chair@rob"], P.APPOINT, self.u["dev"]))

    def test_uavs_lead_approves_a_project_inside_it(self):
        # Creating a unit asks about the unit it goes into, archiving about the one above.
        self.assertTrue(can(self.people["lead@uavs"], P.MANAGE_UNITS, self.u["uavs"]))
        self.assertFalse(can(self.people["lead@uavs"], P.MANAGE_UNITS, self.u["dev"]))
        self.assertTrue(can(self.people["lead@dev"], P.MANAGE_UNITS, self.u["dev"]))

    def test_a_grant_passes_to_the_next_holder(self):
        g = grant(P.SEND_MAILINGS, Role.LEAD, self.u["ops"], self.u["rob"], self.people["chair@root"], start=date(2026, 1, 1))
        first = someone("First", member_of=self.u["rob"])
        old = holds(first, self.u["ops"], Role.LEAD, start=date(2025, 9, 1))
        self.assertTrue(can(first, P.SEND_MAILINGS, self.u["rob"]))
        old.end_on(TODAY)
        nxt = someone("Next", member_of=self.u["rob"])
        holds(nxt, self.u["ops"], Role.LEAD, start=TODAY)
        self.assertFalse(can(first, P.SEND_MAILINGS, self.u["rob"]))
        self.assertTrue(can(nxt, P.SEND_MAILINGS, self.u["rob"]))
        self.assertTrue(can(first, P.SEND_MAILINGS, self.u["rob"], on=TODAY - timedelta(days=1)))
        self.assertIsNone(Grant.objects.get(pk=g.pk).end)  # nothing about the grant changed

    def test_the_roots_chair_is_recorded_by_the_outgoing_chair_or_vice_chair(self):
        vice = someone("Vice", member_of=self.u["root"])
        holds(vice, self.u["root"], Role.VICE_CHAIR)
        for p in (self.people["chair@root"], vice):
            self.assertTrue(can(p, P.APPOINT, self.u["root"]))
        self.assertFalse(can(self.people["vice_chair@rob"], P.APPOINT, self.u["root"]))

    def test_an_association_taking_part_or_served_has_no_say(self):
        self.assertFalse(can(self.people["chair@chem"], P.MANAGE_EVENTS, self.u["hack"]))
        self.assertFalse(can(self.people["vice_chair@rob"], P.MANAGE_EVENTS, self.u["hack"]))
        self.assertFalse(can(self.people["chair@chem"], P.MANAGE_UNITS, self.u["web"]))
        self.assertTrue(can(self.people["chair@chem"], P.END_SERVICE, self.u["web"]))


# ---- What counts --------------------------------------------------------------------


class WhatCounts(Federation):
    def test_a_position_that_has_not_started_gives_nothing_yet(self):
        elected = someone("Elected", member_of=self.u["chem"])
        holds(elected, self.u["chem"], Role.TREASURER, start=TODAY + timedelta(days=30))
        self.assertFalse(can(elected, P.MANAGE_FINANCES, self.u["chem"]))
        self.assertTrue(can(elected, P.MANAGE_FINANCES, self.u["chem"], on=TODAY + timedelta(days=30)))

    def test_an_ended_position_gives_nothing(self):
        position = Position.objects.get(person=self.people["treasurer@rob"])
        position.end_on(TODAY)
        self.assertFalse(can(self.people["treasurer@rob"], P.MANAGE_FINANCES, self.u["rob"]))
        self.assertTrue(can(self.people["treasurer@rob"], P.MANAGE_FINANCES, self.u["rob"], on=TODAY - timedelta(days=1)))

    def test_a_position_in_an_archived_team_gives_nothing(self):
        lead = self.people["lead@hack"]
        self.assertTrue(can(lead, P.MANAGE_EVENTS, self.u["hack"]))
        self.u["hack"].archive()
        self.assertFalse(can(lead, P.MANAGE_EVENTS, self.u["hack"]))
        self.assertTrue(can(lead, P.MANAGE_EVENTS, self.u["hack"], on=TODAY - timedelta(days=1)))  # before it was archived
        self.assertTrue(can(self.people["chair@root"], P.MANAGE_UNITS, self.u["hack"]))  # its head above can bring it back

    def test_a_custom_title_gives_nothing_beyond_its_role(self):
        head_of_pr = someone("PR", member_of=self.u["rob"])
        holds(head_of_pr, self.u["rob"], Role.BOARD_MEMBER, title="Head of PR")
        self.assertEqual(set(units_where(head_of_pr, P.SEND_MAILINGS)), set(units_where(self.people["board_member@rob"], P.SEND_MAILINGS)))
        self.assertFalse(can(head_of_pr, P.APPOINT, self.u["rob"]))

    def test_operators_and_superusers_never_pass(self):
        boss = User.objects.create_superuser("boss@example.org").person
        for p in (self.people["operator"], boss):
            for permission in Permission:
                self.assertFalse(units_where(p, permission).exists(), (p, permission))


# ---- Grants -------------------------------------------------------------------------


class Grants(Federation):
    def setUp(self):
        self.chair = self.people["chair@root"]

    def test_a_grant_covers_one_unit(self):
        grant(P.SEE_MEMBERS, Role.LEAD, self.u["ops"], self.u["rob"], self.chair)
        lead = someone("Lead", member_of=self.u["rob"])
        holds(lead, self.u["ops"], Role.LEAD)
        self.assertTrue(can(lead, P.SEE_MEMBERS, self.u["rob"]))
        self.assertFalse(can(lead, P.SEE_MEMBERS, self.u["dev"]))
        self.assertFalse(can(lead, P.SEE_MEMBERS, self.u["chem"]))

    def test_or_that_unit_and_everything_under_it(self):
        grant(P.SEE_MEMBERS, Role.SECRETARY, self.u["root"], self.u["root"], self.chair, include_below=True)
        secretary = self.people["secretary@root"]
        self.assertEqual(set(units_where(secretary, P.SEE_MEMBERS)), set(self.u.values()))

    def test_a_subtree_inside_the_tree(self):
        grant(P.MANAGE_EVENTS, Role.LEAD, self.u["hack"], self.u["dev"], self.chair, include_below=True)
        self.assertEqual(
            {name for name in UNITS if can(self.people["lead@hack"], P.MANAGE_EVENTS, self.u[name])},
            {"hack", "dev", "uavs", "race"},
        )

    def test_it_goes_to_every_holder_of_the_role(self):
        grant(P.SEE_MEMBERS, Role.BOARD_MEMBER, self.u["rob"], self.u["chem"], self.chair)
        second = someone("Second", member_of=self.u["rob"])
        holds(second, self.u["rob"], Role.BOARD_MEMBER)
        for p in (self.people["board_member@rob"], second):
            self.assertTrue(can(p, P.SEE_MEMBERS, self.u["chem"]))

    def test_a_role_nobody_holds_gives_it_to_nobody_until_someone_does(self):
        grant(P.SEND_MAILINGS, Role.LEAD, self.u["ops"], self.u["rob"], self.chair)
        lead = someone("Lead", member_of=self.u["rob"])
        self.assertFalse(can(lead, P.SEND_MAILINGS, self.u["rob"]))
        holds(lead, self.u["ops"], Role.LEAD)
        self.assertTrue(can(lead, P.SEND_MAILINGS, self.u["rob"]))

    def test_only_the_role_in_that_unit_gets_it(self):
        grant(P.MANAGE_FINANCES, Role.TREASURER, self.u["root"], self.u["chem"], self.chair)
        self.assertTrue(can(self.people["treasurer@root"], P.MANAGE_FINANCES, self.u["chem"]))
        self.assertFalse(can(self.people["treasurer@rob"], P.MANAGE_FINANCES, self.u["chem"]))
        self.assertFalse(can(self.people["secretary@root"], P.MANAGE_FINANCES, self.u["chem"]))

    def test_a_grant_is_dated(self):
        g = grant(P.MANAGE_FINANCES, Role.TREASURER, self.u["root"], self.u["rob"], self.chair, start=TODAY + timedelta(days=7))
        treasurer = self.people["treasurer@root"]
        self.assertFalse(can(treasurer, P.MANAGE_FINANCES, self.u["rob"]))
        self.assertTrue(can(treasurer, P.MANAGE_FINANCES, self.u["rob"], on=TODAY + timedelta(days=7)))
        g.start = LONG_AGO
        g.save()
        self.assertTrue(can(treasurer, P.MANAGE_FINANCES, self.u["rob"]))
        g.end_on(TODAY)
        self.assertFalse(can(treasurer, P.MANAGE_FINANCES, self.u["rob"]))

    def test_it_outlives_the_person_who_gave_it(self):
        grant(P.MANAGE_FINANCES, Role.TREASURER, self.u["root"], self.u["rob"], self.chair)
        Position.objects.get(person=self.chair).end_on(TODAY)
        self.assertTrue(can(self.people["treasurer@root"], P.MANAGE_FINANCES, self.u["rob"]))

    def test_a_grant_to_a_role_in_an_archived_team_gives_nothing(self):
        grant(P.SEND_MAILINGS, Role.LEAD, self.u["hack"], self.u["root"], self.chair)
        self.assertTrue(can(self.people["lead@hack"], P.SEND_MAILINGS, self.u["root"]))
        self.u["hack"].archive()
        self.assertFalse(can(self.people["lead@hack"], P.SEND_MAILINGS, self.u["root"]))

    # ---- Who gives them, and what can't be given

    def test_appointing_managing_units_and_giving_grants_cannot_be_granted(self):
        for permission in (P.APPOINT, P.GIVE_GRANTS, P.MANAGE_UNITS, P.MANAGE_PARTICIPANTS, P.END_SERVICE):
            with self.subTest(permission), self.assertRaises(ValidationError):
                grant(permission, Role.TREASURER, self.u["root"], self.u["root"], self.chair)
        self.assertFalse(Grant.objects.exists())

    def test_the_database_refuses_them_too(self):
        g = grant(P.SEE_MEMBERS, Role.TREASURER, self.u["root"], self.u["root"], self.chair)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            Grant.objects.filter(pk=g.pk).update(permission=P.APPOINT)

    def test_only_a_chair_or_vice_chair_with_authority_there_gives_one(self):
        for giver in ("treasurer@root", "secretary@root", "lead@dev", "operator"):
            with self.subTest(giver), self.assertRaises(ValidationError):
                grant(P.SEE_MEMBERS, Role.LEAD, self.u["uavs"], self.u["dev"], self.people[giver])
        with self.assertRaises(ValidationError):  # Robotics' vice chair has no say at the root
            grant(P.SEE_MEMBERS, Role.TREASURER, self.u["root"], self.u["root"], self.people["vice_chair@rob"])
        with self.assertRaises(ValidationError):  # nor over all of it
            grant(P.SEE_MEMBERS, Role.TREASURER, self.u["chem"], self.u["root"], self.people["vice_chair@rob"], include_below=True)
        grant(P.SEE_MEMBERS, Role.LEAD, self.u["uavs"], self.u["dev"], self.people["vice_chair@rob"])

    def test_the_role_can_be_held_anywhere(self):
        # Only the unit covered has to be within the giver's authority.
        grant(P.MANAGE_FINANCES, Role.TREASURER, self.u["chem"], self.u["rob"], self.people["vice_chair@rob"])
        chem_treasurer = someone("Chem", member_of=self.u["chem"])
        holds(chem_treasurer, self.u["chem"], Role.TREASURER)
        self.assertTrue(can(chem_treasurer, P.MANAGE_FINANCES, self.u["rob"]))

    def test_the_giver_must_have_been_able_to_on_the_day_it_starts(self):
        late = someone("Late", member_of=self.u["chem"])
        holds(late, self.u["chem"], Role.VICE_CHAIR, start=date(2026, 3, 1))
        with self.assertRaises(ValidationError):
            grant(P.SEE_MEMBERS, Role.LEAD, self.u["web"], self.u["chem"], late, start=date(2026, 2, 1))
        g = grant(P.SEE_MEMBERS, Role.LEAD, self.u["web"], self.u["chem"], late, start=date(2026, 3, 1))
        g.start = date(2026, 2, 1)
        with self.assertRaises(ValidationError):
            g.save()

    def test_a_grant_that_would_give_nothing_is_refused(self):
        # Memberships are only of associations, and a team holds only teams.
        for over, below in (("ops", False), ("dev", True)):
            with self.subTest(over), self.assertRaises(ValidationError):
                grant(P.MANAGE_MEMBERSHIPS, Role.LEAD, self.u["ops"], self.u[over], self.chair, include_below=below)
        grant(P.MANAGE_MEMBERSHIPS, Role.LEAD, self.u["ops"], self.u["rob"], self.chair)
        grant(P.MANAGE_MEMBERSHIPS, Role.LEAD, self.u["web"], self.u["root"], self.chair, include_below=True)
        self.assertEqual(Grant.objects.count(), 2)

    def test_the_role_fits_the_unit(self):
        with self.assertRaises(ValidationError):
            grant(P.SEE_MEMBERS, Role.TREASURER, self.u["ops"], self.u["rob"], self.chair)
        with self.assertRaises(ValidationError):
            grant(P.SEE_MEMBERS, Role.LEAD, self.u["rob"], self.u["rob"], self.chair)

    def test_nothing_new_is_given_in_an_archived_unit(self):
        self.u["race"].archive()
        with self.assertRaises(ValidationError):
            grant(P.SEE_MEMBERS, Role.LEAD, self.u["dev"], self.u["race"], self.chair)
        with self.assertRaises(ValidationError):
            grant(P.SEE_MEMBERS, Role.LEAD, self.u["race"], self.u["dev"], self.chair)

    def test_what_who_and_where_are_fixed_once_given(self):
        g = grant(P.SEE_MEMBERS, Role.TREASURER, self.u["root"], self.u["rob"], self.chair)
        changes = {
            "permission": P.SEND_MAILINGS, "role": Role.SECRETARY, "held_in": self.u["chem"],
            "unit": self.u["chem"], "include_below": True, "given_by": self.people["vice_chair@rob"],
        }
        for field, value in changes.items():
            with self.subTest(field), self.assertRaises(ValidationError):
                fresh = Grant.objects.get(pk=g.pk)
                setattr(fresh, field, value)
                fresh.save()

    def test_a_grant_is_ended_and_never_deleted(self):
        g = grant(P.SEE_MEMBERS, Role.TREASURER, self.u["root"], self.u["rob"], self.chair)
        with self.assertRaises(ValidationError):
            g.delete()
        with self.assertRaises(ValidationError):
            Grant.objects.all().delete()
        g.end_on(TODAY)
        self.assertEqual(Grant.objects.get().end, TODAY)

    def test_a_unit_a_grant_ever_pointed_at_is_kept(self):
        covered = unit(TEAM, "covered", self.u["root"])
        held_in = unit(TEAM, "held-in", self.u["root"])  # nobody holds the role yet
        grant(P.SEE_MEMBERS, Role.LEAD, held_in, covered, self.chair).end_on(TODAY)
        for u in (covered, held_in):
            self.assertIsNotNone(u.deletion_problem(), u)
            with self.assertRaises(ValidationError):
                u.delete()
        self.assertIsNone(unit(TEAM, "untouched", self.u["root"]).deletion_problem())

    def test_merging_moves_the_grants_someone_gave(self):
        without = someone("Chair", member_of=self.u["chem"])
        holds(without, self.u["chem"], Role.VICE_CHAIR)
        g = grant(P.SEE_MEMBERS, Role.TREASURER, self.u["root"], self.u["chem"], without)
        with_account = User.objects.create_user("chair@example.org", name="Chair").person
        kept = Person.merge(without, with_account)
        self.assertEqual(Grant.objects.get(pk=g.pk).given_by, kept)


# ---- Two-factor authentication -------------------------------------------------------


class TwoFactor(Federation):
    def account(self, name, member_of=None):
        return someone(name, member_of=member_of, account=True)

    def test_a_board_role_anywhere_requires_it(self):
        # Robotics has no chair yet: the root and Chemistry have theirs.
        places = [(where, role) for where in ("root", "rob", "chem") for role in Position.BOARD_ROLES if role != Role.CHAIR]
        for where, role in [*places, ("rob", Role.CHAIR)]:
            with self.subTest(where=where, role=role):
                p = self.account(f"{role}-{where}".replace("_", "-"), member_of=self.u[where])
                holds(p, self.u[where], role)
                self.assertTrue(User.objects.get(pk=p.user_id).requires_2fa)
                self.assertTrue(User.objects.get(pk=p.user_id).needs_2fa_setup)

    def test_leads_participants_and_members_do_not_need_it(self):
        lead = self.account("lead", member_of=self.u["rob"])
        holds(lead, self.u["dev"], Role.LEAD)
        participant = self.account("participant")
        holds(participant, self.u["uavs"], Role.PARTICIPANT)
        plain = self.account("plain", member_of=self.u["rob"])
        for p in (lead, participant, plain):
            self.assertFalse(User.objects.get(pk=p.user_id).requires_2fa, p)

    def test_from_the_day_the_position_is_recorded(self):
        elected = self.account("elected", member_of=self.u["chem"])
        holds(elected, self.u["chem"], Role.SECRETARY, start=TODAY + timedelta(days=90))
        self.assertTrue(User.objects.get(pk=elected.user_id).requires_2fa)

    def test_until_it_ends(self):
        former = self.account("former", member_of=self.u["chem"])
        position = holds(former, self.u["chem"], Role.TREASURER)
        position.end_on(TODAY)
        self.assertFalse(User.objects.get(pk=former.user_id).requires_2fa)

    def test_set_up_it_stops_being_needed(self):
        p = self.account("treasurer-two", member_of=self.u["rob"])
        holds(p, self.u["rob"], Role.TREASURER)
        from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret

        TOTP.activate(User.objects.get(pk=p.user_id), generate_totp_secret())
        self.assertFalse(User.objects.get(pk=p.user_id).needs_2fa_setup)

    def test_operators_and_the_board_and_treasurer_groups_still_need_it(self):
        # The groups keep it until their members become positions (A5).
        for group in ("operator", "board", "treasurer"):
            with self.subTest(group):
                user, _ = make_user(f"{group}-group@example.org", [group])
                self.assertTrue(user.requires_2fa)

    def test_a_board_role_sends_someone_to_set_it_up_before_the_admin_page(self):
        user, _ = make_user("events@example.org", ["events"])  # staff, with no 2FA of its own
        client = CaddyClient()
        client.force_login(user)
        self.assertEqual(client.get(reverse("admin:index")).status_code, 200)
        Membership.objects.create(person=user.person, association=self.u["chem"], start=LONG_AGO)
        holds(user.person, self.u["chem"], Role.BOARD_MEMBER)
        response = client.get(reverse("admin:index"), follow=True)
        self.assertEqual(response.redirect_chain[-1][0], reverse("mfa_index"))

    def test_the_home_page_says_so(self):
        p = self.account("home", member_of=self.u["chem"])
        holds(p, self.u["chem"], Role.BOARD_MEMBER)
        client = CaddyClient()
        client.force_login(User.objects.get(pk=p.user_id))
        page = client.get(reverse("home"))
        self.assertContains(page, "Your role requires two-factor authentication")
        self.assertContains(page, reverse("mfa_index"))


# ---- The made-up installations ---------------------------------------------------------


class MadeUp(TestCase):
    def build(self, name):
        spec = examples.EXAMPLES[name]
        root = Unit.objects.create(kind=ASSOCIATION, key=spec.root_key, name=spec.root_name)
        examples.build(name, root)
        return root

    def by(self, address):
        return Person.objects.get(emails__address=address)

    def test_example_society_has_no_grants_and_answers_from_positions_alone(self):
        root = self.build("society")
        self.assertFalse(Grant.objects.exists())
        treasurer = self.by("cleo@example.org")
        # One association, so the board reaches every team in it.
        self.assertEqual(set(units_where(treasurer, P.MANAGE_FINANCES)), set(Unit.objects.all()))
        self.assertTrue(can(self.by("gun@example.org"), P.APPOINT, Unit.objects.get(key="photography")))
        self.assertFalse(can(self.by("hampus@example.org"), P.APPOINT, Unit.objects.get(key="photography")))
        self.assertFalse(can(self.by("kaj@example.org"), P.MANAGE_EVENTS, Unit.objects.get(key="autumn-hackathon-2025")))
        for permission in (P.GIVE_GRANTS, P.APPOINT, P.MANAGE_UNITS):
            self.assertTrue(can(self.by("alva@example.org"), permission, root))
            self.assertFalse(can(self.by("cleo@example.org"), permission, root))

    def test_example_federation_has_the_three_grants(self):
        root = self.build("federation")
        robotics, chemistry = Unit.objects.get(key="robotics"), Unit.objects.get(key="chemistry")
        grants = Grant.objects.all()
        self.assertEqual(grants.count(), 3)
        self.assertTrue(grants.filter(include_below=True).exists())
        self.assertTrue(grants.filter(include_below=False).exists())

        # The root's treasurer has fees and treasury in every society, by a grant that
        # outlived the chair who gave it.
        selma = self.by("selma@example.org")
        for association in (root, robotics, chemistry):
            self.assertTrue(can(selma, P.MANAGE_FINANCES, association))
        finances = grants.get(permission=P.MANAGE_FINANCES)
        self.assertFalse(Position.objects.current().filter(person=finances.given_by, role=Role.CHAIR).exists())

        # The root's board members see every society's members.
        board_member = Position.objects.current().get(unit=root, role=Role.BOARD_MEMBER, seat_rule=None).person
        self.assertTrue(can(board_member, P.SEE_MEMBERS, chemistry))

        # Operations' lead sends Robotics' mailings, and the lead has changed since.
        mailings = grants.get(permission=P.SEND_MAILINGS)
        leads = Position.objects.filter(unit=mailings.held_in, role=Role.LEAD).order_by("start")
        self.assertEqual(leads.count(), 2)
        before, after = leads
        self.assertEqual(before.end, after.start)
        self.assertGreater(before.end, mailings.start)
        self.assertTrue(can(after.person, P.SEND_MAILINGS, robotics))
        self.assertFalse(can(before.person, P.SEND_MAILINGS, robotics))
        self.assertTrue(can(before.person, P.SEND_MAILINGS, robotics, on=before.end - timedelta(days=1)))
        self.assertFalse(can(after.person, P.SEND_MAILINGS, chemistry))

    def test_two_factor_follows_board_roles_in_both(self):
        needs = {
            "society": {"alva", "bruno", "cleo", "dag", "ebba", "frans"},
            "federation": {"una", "tor", "selma", "rikard", "ylva", "quinn", "pia", "wilma", "kim"},
        }
        for name in examples.EXAMPLES:
            with self.subTest(name), transaction.atomic():
                self.build(name)
                found = {user.email.split("@")[0] for user in User.objects.all() if user.requires_2fa}
                self.assertEqual(found, needs[name])  # not the leads, participants, members, or Viktor, the chair before
                transaction.set_rollback(True)


# ---- The admin page -------------------------------------------------------------------


class AdminPage(Federation):
    client_class = CaddyClient

    def enter(self, group):
        user, _ = make_user(f"{group}-admin@example.org", [group], two_factor=True)
        self.client.force_login(user)

    def form(self, **fields):
        return {
            "permission": "send_mailings", "role": "lead", "held_in": self.u["ops"].pk, "unit": self.u["rob"].pk,
            "start": "2026-01-01", "end": "", "given_by": self.people["chair@root"].pk, **fields,
        }

    def test_an_operator_gives_a_grant(self):
        self.enter("operator")
        response = self.client.post(reverse("admin:structure_grant_add"), self.form())
        self.assertEqual(response.status_code, 302, response.content.decode()[:3000])
        self.assertEqual(Grant.objects.get().given_by, self.people["chair@root"])

    def test_the_form_refuses_a_giver_without_authority_there(self):
        self.enter("operator")
        response = self.client.post(reverse("admin:structure_grant_add"), self.form(given_by=self.people["treasurer@root"].pk))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Grant.objects.exists())

    def test_the_form_offers_only_what_can_be_granted(self):
        self.enter("operator")
        page = self.client.get(reverse("admin:structure_grant_add")).content.decode()
        self.assertIn('value="manage_finances"', page)
        self.assertNotIn('value="appoint"', page)

    def test_an_operator_ends_a_grant_and_cannot_delete_it(self):
        self.enter("operator")
        g = grant(P.SEND_MAILINGS, Role.LEAD, self.u["ops"], self.u["rob"], self.people["chair@root"])
        self.client.post(reverse("admin:structure_grant_changelist"), {"action": "end_today", "_selected_action": [g.pk]})
        self.assertEqual(Grant.objects.get().end, TODAY)
        self.assertEqual(self.client.post(reverse("admin:structure_grant_delete", args=[g.pk]), {"post": "yes"}).status_code, 403)

    def test_board_sees_grants_and_cannot_give_them(self):
        self.enter("board")
        self.assertEqual(self.client.get(reverse("admin:structure_grant_changelist")).status_code, 200)
        self.assertEqual(self.client.post(reverse("admin:structure_grant_add"), self.form()).status_code, 403)
        self.assertFalse(Grant.objects.exists())
