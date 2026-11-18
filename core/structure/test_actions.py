"""Tests for actions and the audit log (structure.md §4 and §7), written before the code.

First a table: for each action, people it allows and people it refuses, over the tree in
structure.md §1, with a grant, a seat, a position that hasn't started, one that has
ended, an archived team and an operator among them. Then the walk test, the audit log,
the commands on the server, the operators' own changes, the admin page and the made-up
installations. The grep test is in test_writes.py."""

import ast
import importlib
import inspect
import io
import json
from datetime import date, timedelta
from unittest import mock

from allauth.mfa.models import Authenticator
from django.apps import apps
from django.contrib import admin
from django.contrib.admin.models import ADDITION, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.models import Permission as AccountPermission
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.db import transaction
from django.forms.models import model_to_dict
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import FollowUp
from accounts.tests import CaddyClient, make_user

from . import actions, examples
from .actions import ACTIONS, NotAllowed
from .authority import can
from .models import AuditEntry, EmailAddress, Grant, Membership, Permission, Person, Position, Reason, SeatRule, Unit, ensure_root

ASSOCIATION, TEAM = Unit.Kind.ASSOCIATION, Unit.Kind.TEAM
Role = Position.Role
P = Permission
User = get_user_model()
TODAY = timezone.localdate()
LONG_AGO = date(2020, 1, 1)
LATER = TODAY + timedelta(days=30)
SERVER = AuditEntry.Via.SERVER

# Short names for the tree's units, as in test_authority.py.
KEYS = {
    "root": "example", "ext": "extended-board", "hack": "spring-hackathon-2027", "old": "autumn-hackathon-2025",
    "rob": "robotics", "ops": "operations", "dev": "development", "uavs": "uavs", "race": "drone-race-2027",
    "empty": "empty", "chem": "chemistry", "web": "chemistry-website", "lab": "lab-nights",
}


def unit(kind, key, parent=None, **fields):
    return Unit.objects.create(kind=kind, key=key, name=key.replace("-", " ").title(), parent=parent, **fields)


def someone(name, member_of=None, account=False):
    if account:
        p = User.objects.create_user(f"{name.replace('@', '.')}@example.org", name=name).person
    else:
        p = Person.objects.create(name=name)
    if member_of is not None:
        Membership.objects.create(person=p, association=member_of, start=LONG_AGO)
    return p


def holds(p, where, role, start=LONG_AGO, **fields):
    return Position.objects.create(person=p, unit=where, role=role, start=start, **fields)


class Tree(TestCase):
    """The tree in structure.md §1, with Example Chemistry beside Robotics, last year's
    project archived, and a team nothing was ever tied to:

        root              the root, an association
        ├─ ext            Extended board
        ├─ hack           Spring hackathon 2027, with rob and chem taking part
        ├─ old            Autumn hackathon 2025, archived
        ├─ rob            Example Robotics, an association
        │  ├─ ops         Operations, with chem taking part, and a co-lead from chem
        │  ├─ dev         Development: rob's secretary leads it by a seat rule, beside lead@dev
        │  │  ├─ uavs     UAVs
        │  │  │  └─ race  Drone race 2027
        │  │  └─ empty    nothing ever tied to it
        │  └─ web         Example Chemistry's website, run for chem
        └─ chem           Example Chemistry, an association
           └─ lab         Lab nights

    The root's treasurer has a grant to manage Robotics' memberships. Besides the holders
    named role@unit, there's a vice chair of the root who starts next month, one who
    ended yesterday, a member of Robotics, and an operator, who hold nothing else."""

    @classmethod
    def setUpTestData(cls):
        u = {}
        u["root"] = unit(ASSOCIATION, "example")
        for short, kind, parent in (
            ("ext", TEAM, "root"), ("hack", TEAM, "root"), ("old", TEAM, "root"), ("rob", ASSOCIATION, "root"),
            ("ops", TEAM, "rob"), ("dev", TEAM, "rob"), ("uavs", TEAM, "dev"), ("race", TEAM, "uavs"), ("empty", TEAM, "dev"),
            ("chem", ASSOCIATION, "root"), ("lab", TEAM, "chem"),
        ):
            u[short] = unit(kind, KEYS[short], u[parent])
        u["web"] = unit(TEAM, KEYS["web"], u["rob"], run_for=u["chem"])
        u["hack"].taking_part.add(u["rob"], u["chem"])

        def association(where):
            return u["chem"] if where in ("chem", "lab") else u["root"] if where in ("root", "ext", "hack", "old") else u["rob"]

        ids = cls.ids = {}
        for holder in (
            "chair@root", "treasurer@root", "vice_chair@rob", "treasurer@rob", "secretary@rob", "board_member@rob", "chair@chem",
            "lead@dev", "lead@uavs", "lead@hack", "lead@web", "lead@old", "participant@uavs",
        ):
            role, where = holder.split("@")
            p = someone(holder, member_of=None if role == "participant" else association(where), account=holder == "chair@root")
            holds(p, u[where], role)
            ids[holder] = p.pk
        # Chemistry takes part in Operations, and Operations has a co-lead who is a member of
        # Chemistry alone, so Chemistry can't stop taking part while they lead it.
        u["ops"].taking_part.add(u["chem"])
        colead = someone("colead@ops", member_of=u["chem"])
        holds(colead, u["ops"], Role.LEAD)
        future = someone("vice_chair@root from next month", member_of=u["root"])
        holds(future, u["root"], Role.VICE_CHAIR, start=LATER)
        ended = someone("vice_chair@root until yesterday", member_of=u["root"])
        holds(ended, u["root"], Role.VICE_CHAIR, end=TODAY - timedelta(days=1))
        ids.update(future=future.pk, ended=ended.pk, colead=colead.pk)
        ids["member@rob"] = someone("member@rob", member_of=u["rob"]).pk
        ids["operator"] = make_user("operator@example.org", ["operator"], two_factor=True)[0].person.pk

        chair = Person.objects.get(pk=ids["chair@root"])
        Grant.objects.create(permission=P.MANAGE_MEMBERSHIPS, role=Role.TREASURER, held_in=u["root"], unit=u["rob"], start=LONG_AGO, given_by=chair)
        SeatRule.objects.create(unit=u["dev"], role=Role.LEAD, from_roles=[Role.SECRETARY], held_in=u["rob"], start=LONG_AGO, made_by=chair)
        Reason.objects.create(
            person_id=ids["participant@uavs"], unit=u["rob"], kind=Reason.Kind.PARTNER_CONTACT, legal_basis=Reason.Basis.CONSENT,
            source="Met them at the spring fair", start=LONG_AGO, expires=TODAY + timedelta(days=365),
        )
        u["old"].archive()

    # Fresh from the database each time, as a case that's rolled back leaves objects in memory changed.

    def u(self, short):
        return Unit.objects.get(key=KEYS[short])

    def p(self, holder):
        return Person.objects.get(pk=self.ids[holder])

    def position(self, holder):
        return Position.objects.get(person=self.ids[holder], seat_rule=None)

    def participant(self):
        return self.position("participant@uavs")

    def seat(self):
        return Position.objects.get(person=self.ids["secretary@rob"], seat_rule__isnull=False)

    def membership(self):
        return Membership.objects.get(person=self.ids["member@rob"])

    def reason(self):
        return Reason.objects.get()

    def grant(self):
        return Grant.objects.get()

    def rule(self):
        return SeatRule.objects.get()


def state():
    """Every row an action could touch, to show a refusal changed nothing."""
    models = (Unit, Unit.taking_part.through, Person, EmailAddress, Membership, Position, Reason, Grant, SeatRule, AuditEntry)
    return [list(model.objects.order_by("pk").values()) for model in models]


# ---- The table ------------------------------------------------------------------------
#
# For each action: (OK, who, its arguments, the unit can() is asked about) or (NO, who,
# its arguments). Every action also refuses NOBODY, with its first OK case's arguments.

OK, NO = "allowed", "refused"
NOBODY = ("operator", "future", "ended", "member@rob", "participant@uavs")


def new_team(parent):
    return lambda t: dict(parent=t.u(parent), kind=TEAM, key="new-team", name="New team")


CASES = {
    "create_unit": [
        (OK, "lead@dev", new_team("dev"), "dev"),  # a lead creates teams inside theirs
        (OK, "chair@root", lambda t: dict(parent=t.u("root"), kind=ASSOCIATION, key="physics", name="Physics"), "root"),
        (OK, "secretary@rob", new_team("dev"), "dev"),  # by a seat, as Development's lead
        (NO, "lead@uavs", new_team("dev")),
        (NO, "board_member@rob", new_team("rob")),
        (NO, "chair@chem", new_team("rob")),
        (NO, "lead@old", new_team("old")),  # their team is archived
    ],
    "change_unit": [
        (OK, "lead@uavs", lambda t: dict(unit=t.u("uavs"), name="UAV team", description="Flying things"), "uavs"),
        (OK, "chair@root", lambda t: dict(unit=t.u("root"), description="The federation", primary_colour="#1a5fb4"), "root"),
        (NO, "lead@uavs", lambda t: dict(unit=t.u("dev"), name="Dev")),
        (NO, "treasurer@rob", lambda t: dict(unit=t.u("rob"), description="Robots")),
        (NO, "lead@old", lambda t: dict(unit=t.u("old"), name="Old")),
    ],
    "move_unit": [
        (OK, "lead@dev", lambda t: dict(unit=t.u("race"), parent=t.u("dev")), "dev"),  # both places are inside Development
        (OK, "vice_chair@rob", lambda t: dict(unit=t.u("uavs"), parent=t.u("ops")), "rob"),
        (NO, "lead@uavs", lambda t: dict(unit=t.u("race"), parent=t.u("dev"))),
        (NO, "lead@dev", lambda t: dict(unit=t.u("uavs"), parent=t.u("ops"))),  # out of Development: for the head above both
        (NO, "chair@chem", lambda t: dict(unit=t.u("lab"), parent=t.u("rob"))),
    ],
    "archive_unit": [
        (OK, "lead@dev", lambda t: dict(unit=t.u("uavs")), "dev"),  # one level up
        (OK, "secretary@rob", lambda t: dict(unit=t.u("uavs")), "dev"),  # by a seat
        (OK, "chair@chem", lambda t: dict(unit=t.u("lab")), "chem"),
        (NO, "lead@uavs", lambda t: dict(unit=t.u("uavs"))),  # their own team: the head above archives it
        (NO, "board_member@rob", lambda t: dict(unit=t.u("ops"))),
    ],
    "bring_back_unit": [
        (OK, "chair@root", lambda t: dict(unit=t.u("old")), "root"),
        (NO, "lead@old", lambda t: dict(unit=t.u("old"))),
        (NO, "lead@hack", lambda t: dict(unit=t.u("old"))),
    ],
    "delete_unit": [
        (OK, "lead@dev", lambda t: dict(unit=t.u("empty")), "dev"),
        (NO, "board_member@rob", lambda t: dict(unit=t.u("empty"))),
        (NO, "lead@uavs", lambda t: dict(unit=t.u("empty"))),
    ],
    "add_taking_part": [
        (OK, "chair@chem", lambda t: dict(team=t.u("lab"), association=t.u("rob")), "lab"),
        (OK, "lead@dev", lambda t: dict(team=t.u("dev"), association=t.u("chem")), "dev"),  # a lead changes their own team
        (NO, "chair@chem", lambda t: dict(team=t.u("hack"), association=t.u("chem"))),  # taking part gives no say
        (NO, "vice_chair@rob", lambda t: dict(team=t.u("lab"), association=t.u("rob"))),
        (NO, "lead@uavs", lambda t: dict(team=t.u("dev"), association=t.u("chem"))),
    ],
    "end_taking_part": [
        (OK, "chair@root", lambda t: dict(team=t.u("hack"), association=t.u("chem")), "hack"),
        (OK, "lead@hack", lambda t: dict(team=t.u("hack"), association=t.u("chem")), "hack"),
        (NO, "chair@chem", lambda t: dict(team=t.u("hack"), association=t.u("chem"))),  # taking part gives no say
        (NO, "lead@uavs", lambda t: dict(team=t.u("hack"), association=t.u("chem"))),
    ],
    "run_for": [
        (OK, "vice_chair@rob", lambda t: dict(team=t.u("ops"), association=t.u("chem")), "rob"),  # one level up
        (OK, "lead@dev", lambda t: dict(team=t.u("uavs"), association=t.u("chem")), "dev"),
        (NO, "lead@uavs", lambda t: dict(team=t.u("uavs"), association=t.u("chem"))),  # their own team
        (NO, "chair@chem", lambda t: dict(team=t.u("ops"), association=t.u("chem"))),  # the side it would serve
    ],
    "end_service": [
        (OK, "chair@chem", lambda t: dict(team=t.u("web")), "web"),  # the side it's run for
        (OK, "vice_chair@rob", lambda t: dict(team=t.u("web")), "web"),  # the side that runs it
        (NO, "lead@web", lambda t: dict(team=t.u("web"))),
        (NO, "treasurer@rob", lambda t: dict(team=t.u("web"))),
    ],
    "appoint": [
        (OK, "secretary@rob", lambda t: dict(person=t.p("member@rob"), unit=t.u("uavs"), role=Role.LEAD), "uavs"),  # by a seat
        (OK, "lead@uavs", lambda t: dict(person=t.p("member@rob"), unit=t.u("uavs"), role=Role.PARTICIPANT, title="Pilot"), "uavs"),
        (OK, "chair@root", lambda t: dict(person=t.p("member@rob"), unit=t.u("rob"), role=Role.BOARD_MEMBER, start=LATER), "rob"),  # starts later: checked today
        (NO, "secretary@rob", lambda t: dict(person=t.p("member@rob"), unit=t.u("dev"), role=Role.LEAD)),  # a co-lead: for the head above
        (NO, "lead@uavs", lambda t: dict(person=t.p("member@rob"), unit=t.u("uavs"), role=Role.LEAD)),
        (NO, "treasurer@root", lambda t: dict(person=t.p("member@rob"), unit=t.u("root"), role=Role.BOARD_MEMBER)),
        (NO, "lead@old", lambda t: dict(person=t.p("member@rob"), unit=t.u("old"), role=Role.PARTICIPANT)),
    ],
    "change_position": [
        (OK, "lead@uavs", lambda t: dict(position=t.participant(), title="Pilot", minutes="Team meeting 3"), "uavs"),
        (OK, "lead@dev", lambda t: dict(position=t.participant(), start=date(2021, 1, 1)), "uavs"),
        (NO, "lead@web", lambda t: dict(position=t.participant(), title="Pilot")),
    ],
    "end_position": [
        (OK, "lead@uavs", lambda t: dict(position=t.participant()), "uavs"),
        (OK, "vice_chair@rob", lambda t: dict(position=t.position("lead@dev"), day=LATER), "dev"),
        (NO, "board_member@rob", lambda t: dict(position=t.participant())),
        (NO, "lead@dev", lambda t: dict(position=t.position("lead@dev"))),  # their own: for the head above
    ],
    "add_membership": [
        (OK, "board_member@rob", lambda t: dict(person=t.p("participant@uavs"), association=t.u("rob")), "rob"),
        (OK, "treasurer@root", lambda t: dict(person=t.p("participant@uavs"), association=t.u("rob")), "rob"),  # by a grant
        (NO, "treasurer@root", lambda t: dict(person=t.p("participant@uavs"), association=t.u("chem"))),  # the grant covers Robotics alone
        (NO, "board_member@rob", lambda t: dict(person=t.p("participant@uavs"), association=t.u("root"))),
        (NO, "lead@uavs", lambda t: dict(person=t.p("participant@uavs"), association=t.u("rob"))),
    ],
    "change_membership": [
        (OK, "board_member@rob", lambda t: dict(membership=t.membership(), kind=Membership.Kind.HONORARY), "rob"),
        (NO, "chair@chem", lambda t: dict(membership=t.membership(), kind=Membership.Kind.HONORARY)),
    ],
    "end_membership": [
        (OK, "treasurer@root", lambda t: dict(membership=t.membership()), "rob"),  # by a grant
        (NO, "lead@dev", lambda t: dict(membership=t.membership())),
    ],
    "add_reason": [
        (OK, "board_member@rob", lambda t: dict(
            person=t.p("member@rob"), unit=t.u("uavs"), kind=Reason.Kind.APPLICANT, legal_basis=Reason.Basis.CONSENT, expires=LATER,
        ), "rob"),  # the association the team sits in
        (NO, "lead@uavs", lambda t: dict(
            person=t.p("member@rob"), unit=t.u("uavs"), kind=Reason.Kind.APPLICANT, legal_basis=Reason.Basis.CONSENT, expires=LATER,
        )),
    ],
    "change_reason": [
        (OK, "board_member@rob", lambda t: dict(reason=t.reason(), expires=TODAY + timedelta(days=700)), "rob"),
        (NO, "lead@uavs", lambda t: dict(reason=t.reason(), expires=TODAY + timedelta(days=700))),
    ],
    "end_reason": [
        (OK, "vice_chair@rob", lambda t: dict(reason=t.reason()), "rob"),
        (NO, "chair@chem", lambda t: dict(reason=t.reason())),
    ],
    "give_grant": [
        (OK, "vice_chair@rob", lambda t: dict(permission=P.SEND_MAILINGS, role=Role.LEAD, held_in=t.u("ops"), unit=t.u("rob")), "rob"),
        (NO, "treasurer@rob", lambda t: dict(permission=P.SEND_MAILINGS, role=Role.LEAD, held_in=t.u("ops"), unit=t.u("rob"))),
        (NO, "chair@chem", lambda t: dict(permission=P.SEND_MAILINGS, role=Role.LEAD, held_in=t.u("ops"), unit=t.u("rob"))),
    ],
    "end_grant": [
        (OK, "vice_chair@rob", lambda t: dict(grant=t.grant()), "rob"),  # whoever could give it can end it
        (NO, "treasurer@root", lambda t: dict(grant=t.grant())),  # the role it goes to
    ],
    "make_seat_rule": [
        (OK, "chair@root", lambda t: dict(
            unit=t.u("ext"), role=Role.PARTICIPANT, from_roles=[Role.CHAIR, Role.VICE_CHAIR], held_in=t.u("root"), each_association_under=True,
        ), "ext"),
        (OK, "lead@dev", lambda t: dict(unit=t.u("uavs"), role=Role.PARTICIPANT, from_roles=[Role.LEAD], held_in=t.u("dev")), "uavs"),
        (NO, "lead@uavs", lambda t: dict(unit=t.u("uavs"), role=Role.PARTICIPANT, from_roles=[Role.LEAD], held_in=t.u("dev"))),
        (NO, "board_member@rob", lambda t: dict(unit=t.u("rob"), role=Role.BOARD_MEMBER, from_roles=[Role.CHAIR], held_in=t.u("rob"))),
    ],
    "end_seat_rule": [
        (OK, "vice_chair@rob", lambda t: dict(rule=t.rule()), "dev"),
        (NO, "secretary@rob", lambda t: dict(rule=t.rule())),  # their seat can't appoint in Development itself
    ],
}

# Allowed by can(), refused by the rules in structure.md, which the models keep: a
# ValidationError, and nothing written either.
RULES = [
    ("appoint", "chair@root", lambda t: dict(person=t.p("member@rob"), unit=t.u("old"), role=Role.PARTICIPANT)),  # nothing new in an archived team
    ("change_unit", "chair@root", lambda t: dict(unit=t.u("root"), name="Another name")),  # the root's name follows the config
    ("move_unit", "chair@root", lambda t: dict(unit=t.u("root"), parent=t.u("rob"))),
    ("move_unit", "chair@root", lambda t: dict(unit=t.u("dev"), parent=t.u("uavs"))),  # into itself
    ("move_unit", "chair@root", lambda t: dict(unit=t.u("ops"), parent=None)),  # every unit but the root sits in another
    ("archive_unit", "chair@root", lambda t: dict(unit=t.u("rob"))),  # only teams
    ("delete_unit", "chair@root", lambda t: dict(unit=t.u("uavs"))),  # people were tied to it
    ("end_position", "chair@root", lambda t: dict(position=t.seat())),  # a seat follows its rule
    ("end_position", "chair@root", lambda t: dict(position=Position.objects.get(person=t.ids["ended"]))),  # it ended yesterday, so not again today
    ("end_taking_part", "vice_chair@rob", lambda t: dict(team=t.u("ops"), association=t.u("chem"))),  # a lead rests on it
    ("run_for", "chair@root", lambda t: dict(team=t.u("lab"), association=t.u("chem"))),  # its own association
    ("make_seat_rule", "chair@root", lambda t: dict(unit=t.u("chem"), role=Role.CHAIR, from_roles=[Role.CHAIR], held_in=t.u("rob"))),  # never a chair
    ("end_membership", "chair@root", lambda t: dict(membership=Membership.objects.get(person=t.ids["lead@dev"]))),  # a lead rests on it
    ("change_membership", "chair@root", lambda t: dict(membership=Membership.objects.get(person=t.ids["lead@dev"]), start=LATER)),  # and from its start
]


def first_allowed(name):
    return next(case for case in CASES[name] if case[0] == OK)


class Table(Tree):
    def act(self, name, actor, args):
        return ACTIONS[name](self.p(actor), via=SERVER, **args(self))

    def test_each_action_allows_whom_it_should_and_says_where_it_asked(self):
        for name, cases in CASES.items():
            for verdict, actor, args, *asked in cases:
                if verdict != OK:
                    continue
                with self.subTest(action=name, actor=actor), transaction.atomic():
                    self.act(name, actor, args)
                    row = AuditEntry.objects.get()
                    self.assertEqual((row.action, row.via, row.actor_person), (name, SERVER, self.ids[actor]))
                    self.assertEqual(row.unit_key, KEYS[asked[0]])
                    transaction.set_rollback(True)

    def test_each_action_refuses_whom_it_should_and_changes_nothing(self):
        before = state()
        for name, cases in CASES.items():
            refused = [(actor, args) for verdict, actor, args, *_ in cases if verdict == NO]
            refused += [(nobody, first_allowed(name)[2]) for nobody in NOBODY]
            for actor, args in refused:
                with self.subTest(action=name, actor=actor):
                    with self.assertRaises(NotAllowed):
                        self.act(name, actor, args)
                    self.assertEqual(state(), before)

    def test_rules_still_hold_and_a_broken_one_changes_nothing(self):
        before = state()
        for name, actor, args in RULES:
            with self.subTest(action=name, actor=actor):
                with self.assertRaises(ValidationError):
                    self.act(name, actor, args)
                self.assertEqual(state(), before)

    def test_an_end_already_set_only_moves_earlier(self):
        """Ending again later would give back what the tie gave, over days already past.
        Earlier puts a mistake right."""
        for name in ("end_position", "end_membership", "end_reason", "end_grant", "end_seat_rule"):
            _, actor, args, _ = first_allowed(name)

            def end(day):
                return ACTIONS[name](self.p(actor), via=SERVER, day=day, **args(self))

            with self.subTest(action=name), transaction.atomic():
                end(TODAY + timedelta(days=10))
                before = state()
                with self.assertRaisesMessage(ValidationError, "can only be ended earlier"):
                    end(TODAY + timedelta(days=20))
                self.assertEqual(state(), before)
                tie = end(TODAY)
                self.assertEqual(getattr(tie, tie.end_field), TODAY)
                transaction.set_rollback(True)

    def test_nobody_without_a_person_acts(self):
        with self.assertRaises(NotAllowed):
            actions.change_unit(None, unit=self.u("ops"), name="Ops", via=SERVER)
        with self.assertRaises(NotAllowed):
            actions.change_unit(Person(name="Not saved"), unit=self.u("ops"), name="Ops", via=SERVER)
        self.assertFalse(AuditEntry.objects.exists())


# ---- The walk test --------------------------------------------------------------------


class Walk(Tree):
    def test_every_action_is_in_the_table_allowed_and_refused(self):
        self.assertEqual(set(CASES), set(ACTIONS))
        for name, cases in CASES.items():
            self.assertEqual({case[0] for case in cases}, {OK, NO}, name)

    def test_every_public_function_in_actions_is_an_action(self):
        module = ast.parse(inspect.getsource(actions))
        public = {node.name for node in module.body if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")}
        self.assertEqual(public - set(ACTIONS), {"action", "check"})

    def test_each_action_asks_can_once_about_the_unit_in_the_table(self):
        for name in ACTIONS:
            _, actor, args, asked = first_allowed(name)
            with self.subTest(action=name), mock.patch("structure.actions.can", wraps=can) as asking, transaction.atomic():
                ACTIONS[name](self.p(actor), via=SERVER, **args(self))
                self.assertEqual(asking.call_count, 1)
                person, _permission, where = asking.call_args.args[:3]
                self.assertEqual((person.pk, where.key), (self.ids[actor], KEYS[asked]))
                transaction.set_rollback(True)

    def test_an_action_that_asks_nothing_is_stopped_and_undone(self):
        def careless(actor, *, unit):
            unit.name = "Changed"
            unit.save()
            return unit

        with self.assertRaises(RuntimeError):
            actions._wrap(careless)(self.p("chair@root"), unit=self.u("ops"), via=SERVER)
        self.assertEqual(self.u("ops").name, "Operations")
        self.assertFalse(AuditEntry.objects.exists())

    def test_so_is_one_that_asks_twice(self):
        def twice(actor, *, unit):
            actions.check(actor, P.MANAGE_UNITS, unit)
            actions.check(actor, P.MANAGE_UNITS, unit)
            unit.name = "Changed"
            unit.save()
            return unit

        with self.assertRaises(RuntimeError):
            actions._wrap(twice)(self.p("chair@root"), unit=self.u("ops"), via=SERVER)
        self.assertEqual(self.u("ops").name, "Operations")

    def test_a_refused_change_leaves_the_callers_object_as_it_was(self):
        unit = self.u("uavs")
        with self.assertRaises(NotAllowed):
            actions.change_unit(self.p("lead@web"), unit=unit, name="Refused", via=SERVER)
        self.assertEqual(unit.name, "Uavs")
        actions.change_unit(self.p("lead@uavs"), unit=unit, description="Flying things", via=SERVER)
        self.assertEqual(self.u("uavs").name, "Uavs")

    def test_what_it_records_as_before_is_what_was_stored(self):
        unit = self.u("uavs")
        unit.name = "Never saved"
        actions.change_unit(self.p("lead@uavs"), unit=unit, description="Flying things", via=SERVER)
        self.assertEqual(AuditEntry.objects.get().changes, {"description": [None, "Flying things"]})
        self.assertEqual(self.u("uavs").name, "Uavs")

    def test_check_outside_an_action_is_a_mistake(self):
        with self.assertRaises(RuntimeError):
            actions.check(self.p("chair@root"), P.MANAGE_UNITS, self.u("ops"))

    def test_only_what_can_change_is_changed(self):
        with self.assertRaises(TypeError):
            actions.change_unit(self.p("chair@root"), unit=self.u("ops"), key="ops", via=SERVER)
        with self.assertRaises(TypeError):
            actions.change_position(self.p("lead@uavs"), position=self.participant(), role=Role.LEAD, via=SERVER)
        self.assertFalse(AuditEntry.objects.exists())


# ---- The audit log --------------------------------------------------------------------


class AuditLog(Tree):
    def test_a_row_says_who_what_where_when_and_how(self):
        chair = self.p("chair@root")
        before = timezone.now()
        position = actions.appoint(chair, person=self.p("member@rob"), unit=self.u("rob"), role=Role.BOARD_MEMBER, title="Head of PR", via=SERVER)
        row = AuditEntry.objects.get()
        self.assertEqual((row.action, row.via), ("appoint", "server"))
        self.assertEqual((row.actor_account, row.actor_person), (chair.user_id, chair.pk))
        self.assertEqual((row.unit, row.unit_key), (self.u("rob").pk, "robotics"))
        self.assertEqual((row.object_type, row.object_id), ("structure.position", position.pk))
        self.assertLessEqual(before, row.at)
        self.assertEqual(row.changes["role"], [None, "board_member"])
        self.assertEqual(row.changes["unit"], [None, "robotics"])
        self.assertEqual(row.changes["title"], [None, "Head of PR"])
        self.assertEqual(row.changes["start"], [None, TODAY.isoformat()])

    def test_a_change_shows_what_it_was_before(self):
        actions.change_unit(self.p("lead@uavs"), unit=self.u("uavs"), name="UAV team", via=SERVER)
        self.assertEqual(AuditEntry.objects.get().changes, {"name": ["Uavs", "UAV team"]})

    def test_it_names_nobody(self):
        member = self.p("member@rob")
        EmailAddress.objects.create(person=member, address="member@example.org", verified_at=timezone.now())
        actions.end_membership(self.p("board_member@rob"), membership=self.membership(), via=SERVER)
        actions.change_reason(self.p("board_member@rob"), reason=self.reason(), source="Asked again at the autumn fair", via=SERVER)
        text = json.dumps([model_to_dict(row) for row in AuditEntry.objects.all()], default=str)
        for personal in ("member@rob", "member@example.org", "spring fair", "autumn fair"):
            self.assertNotIn(personal, text)
        self.assertFalse(AuditEntry.objects.filter(actor_person=member.pk).exists())
        self.assertEqual(AuditEntry.objects.get(action="change_reason").changes, {"source": None})  # changed, not shown
        self.assertNotIn("person", AuditEntry.objects.get(action="end_membership").changes)

    def test_appointing_the_next_chair_names_the_position_it_ended(self):
        last = self.position("chair@chem")
        nora = someone("Nora", member_of=self.u("chem"))
        actions.appoint(self.p("chair@root"), person=nora, unit=self.u("chem"), role=Role.CHAIR, via=SERVER)
        self.assertEqual(AuditEntry.objects.get().changes["ended"], [last.pk])
        self.assertEqual(Position.objects.get(pk=last.pk).end, TODAY)

    def test_archiving_names_the_units_archived_with_it(self):
        actions.archive_unit(self.p("vice_chair@rob"), unit=self.u("dev"), via=SERVER)
        self.assertEqual(AuditEntry.objects.get().changes["archived_with_it"], ["empty", "uavs", "drone-race-2027"])

    def test_the_change_and_its_row_are_one(self):
        with mock.patch("structure.models.AuditEntry.record", side_effect=RuntimeError("the disk is full")):
            with self.assertRaises(RuntimeError):
                actions.change_unit(self.p("lead@uavs"), unit=self.u("uavs"), name="UAV team", via=SERVER)
        self.assertEqual(self.u("uavs").name, "Uavs")

    def test_the_way_in_is_recorded_and_must_be_one(self):
        actions.change_unit(self.p("lead@uavs"), unit=self.u("uavs"), name="UAV team", via=AuditEntry.Via.ADMIN)
        self.assertEqual(AuditEntry.objects.get().via, "admin")
        with self.assertRaises(ValueError):
            actions.change_unit(self.p("lead@uavs"), unit=self.u("uavs"), name="UAVs", via="telepathy")
        self.assertEqual(AuditEntry.objects.count(), 1)

    def test_rows_are_only_ever_added(self):
        actions.change_unit(self.p("lead@uavs"), unit=self.u("uavs"), name="UAV team", via=SERVER)
        row = AuditEntry.objects.get()
        row.action = "something else"
        for attempt in (row.save, row.delete, lambda: AuditEntry.objects.update(action="x"), lambda: AuditEntry.objects.all().delete()):
            with self.assertRaises(ValidationError):
                attempt()
        self.assertEqual(AuditEntry.objects.get().action, "change_unit")

    def test_it_never_stops_a_unit_or_person_from_going(self):
        actions.change_unit(self.p("lead@dev"), unit=self.u("empty"), name="Still empty", via=SERVER)
        actions.delete_unit(self.p("lead@dev"), unit=self.u("empty"), via=SERVER)
        self.assertFalse(Unit.objects.filter(key="empty").exists())
        self.assertEqual(AuditEntry.objects.count(), 2)


# ---- Commands on the server -----------------------------------------------------------


class RootChair(TestCase):
    """structure.md §4: kit chair records the root's chair, and for a new installation
    makes them a member of the root."""

    def setUp(self):
        self.root = Unit.objects.create(kind=ASSOCIATION, key="example", name="Example")
        self.alva = User.objects.create_user("alva@example.org", name="Alva").person

    def chair(self, *args):
        out = io.StringIO()
        call_command("root_chair", *args, stdout=out)
        return out.getvalue()

    def test_it_records_the_first_chair_and_makes_them_a_member(self):
        said = self.chair("alva@example.org", "--from", "2026-03-01", "--member-since", "2022-09-01")
        self.assertIn("Alva", said)
        position = Position.objects.get()
        self.assertEqual((position.person, position.unit, position.role, position.start), (self.alva, self.root, Role.CHAIR, date(2026, 3, 1)))
        self.assertEqual(Membership.objects.get().start, date(2022, 9, 1))
        rows = list(AuditEntry.objects.order_by("pk").values_list("action", "via", "actor_person", "unit_key"))
        self.assertEqual(rows, [("add_membership", "server", None, "example"), ("record_root_chair", "server", None, "example")])

    def test_from_today_and_a_member_already_gets_no_new_membership(self):
        Membership.objects.create(person=self.alva, association=self.root, start=LONG_AGO)
        self.chair("alva@example.org")
        self.assertEqual(Position.objects.get().start, TODAY)
        self.assertEqual(Membership.objects.count(), 1)
        self.assertEqual(list(AuditEntry.objects.values_list("action", flat=True)), ["record_root_chair"])

    def test_the_next_one_ends_the_last(self):
        self.chair("alva@example.org", "--from", "2026-03-01")
        bruno = User.objects.create_user("bruno@example.org", name="Bruno").person
        self.chair("bruno@example.org", "--from", "2027-03-01")
        alvas = Position.objects.get(person=self.alva)
        self.assertEqual(alvas.end, date(2027, 3, 1))
        self.assertEqual(AuditEntry.objects.filter(action="record_root_chair").first().changes["ended"], [alvas.pk])
        self.assertTrue(Position.objects.filter(person=bruno, end=None).exists())

    def test_someone_without_that_verified_address_is_refused(self):
        p = Person.objects.create(name="Bo")
        EmailAddress.objects.create(person=p, address="bo@example.org")
        for address in ("nobody@example.org", "bo@example.org"):
            with self.subTest(address), self.assertRaises(CommandError):
                self.chair(address)
        self.assertFalse(Position.objects.exists() or AuditEntry.objects.exists())

    def test_a_broken_rule_is_refused_with_nothing_written(self):
        with self.assertRaises(CommandError):
            self.chair("alva@example.org", "--from", "2026-03-01", "--member-since", "2026-04-01")
        self.assertFalse(Position.objects.exists() or Membership.objects.exists() or AuditEntry.objects.exists())

    def test_a_date_that_is_not_one_is_refused(self):
        with self.assertRaises(CommandError):
            self.chair("alva@example.org", "--from", "1 July")


class NoRootYet(TestCase):
    def test_kit_chair_says_there_is_no_association_yet(self):
        User.objects.create_user("alva@example.org")
        with self.assertRaises(CommandError):
            call_command("root_chair", "alva@example.org", stdout=io.StringIO())


class ServerRecords(TestCase):
    def test_kit_admin_is_recorded(self):
        call_command("create_admin", "op@example.org", stdout=io.StringIO())
        user = User.objects.get(email="op@example.org")
        row = AuditEntry.objects.get()
        self.assertEqual((row.action, row.via, row.actor_account, row.object_type, row.object_id), ("make_operator", "server", None, "accounts.user", user.pk))
        self.assertEqual(row.changes, {"email": None, "operator": [False, True]})  # a new account, its address not shown

    def test_and_so_is_taking_away_2fa_on_the_server(self):
        user, _ = make_user("op@example.org", two_factor=True)
        call_command("create_admin", "op@example.org", "--reset-2fa", stdout=io.StringIO())
        row = AuditEntry.objects.get()
        self.assertEqual((row.object_id, row.changes), (user.pk, {"operator": [False, True], "two_factor": [True, False]}))

    @override_settings(SALLSKAP_ROOT={"name": "Example Society", "key": "example-society"})
    def test_the_root_from_the_config_is_recorded_when_made_or_renamed(self):
        ensure_root()
        ensure_root()  # nothing changed, so nothing recorded
        with override_settings(SALLSKAP_ROOT={"name": "The Example Society", "key": "example-society"}):
            ensure_root()
        rows = list(AuditEntry.objects.order_by("pk").values_list("action", "via", "unit_key", "changes"))
        self.assertEqual(rows[0][:3], ("create_root", "server", "example-society"))
        self.assertEqual(rows[1], ("rename_root", "server", "example-society", {"name": ["Example Society", "The Example Society"]}))
        self.assertEqual(len(rows), 2)


# ---- Operators' own changes, in the admin page ----------------------------------------


class OperatorsChanges(TestCase):
    client_class = CaddyClient

    def setUp(self):
        self.operator, _ = make_user("operator@example.org", ["operator"], two_factor=True)
        self.client.force_login(self.operator)

    def rows(self, action):
        return AuditEntry.objects.filter(action=action, via="admin", actor_account=self.operator.pk)

    def test_adding_an_account_is_recorded_without_its_address(self):
        response = self.client.post(reverse("admin:accounts_user_add"), {"email": "bo@example.org", "name": "Bo Example", "is_active": "on", "groups": []})
        self.assertEqual(response.status_code, 302, response.content.decode()[:3000])
        user = User.objects.get(email="bo@example.org")
        row = self.rows("add_account").get()
        self.assertEqual((row.object_type, row.object_id), ("accounts.user", user.pk))
        self.assertEqual(row.changes["email"], None)
        self.assertEqual(row.changes["name"], None)
        self.assertNotIn("bo@example.org", json.dumps(row.changes))
        self.assertNotIn("Bo Example", json.dumps(row.changes))

    def test_changing_one_and_making_it_an_operator(self):
        user, _ = make_user("bo@example.org")
        operator = Group.objects.get(name="Operator")
        response = self.client.post(reverse("admin:accounts_user_change", args=[user.pk]), {"email": "bo@example.org", "name": "", "groups": [operator.pk]})
        self.assertEqual(response.status_code, 302, response.content.decode()[:3000])
        row = self.rows("change_account").get()
        self.assertEqual(row.changes, {"is_active": [True, False], "operator": [False, True]})

    def test_removing_someones_2fa(self):
        user, _ = make_user("bo@example.org", two_factor=True)
        record = Authenticator.objects.get(user=user)
        self.client.post(reverse("admin:mfa_authenticator_delete", args=[record.pk]), {"post": "yes"})
        self.assertFalse(Authenticator.objects.filter(user=user).exists())
        row = self.rows("remove_2fa").get()
        self.assertEqual((row.object_type, row.object_id), ("accounts.user", user.pk))

    def test_marking_a_follow_up_done(self):
        follow_up = FollowUp.objects.create(about="bo@example.org", sub="1", tool="vault", key="x", text="Change the shared passwords")
        self.client.post(reverse("admin:accounts_followup_changelist"), {"action": "mark_done", "_selected_action": [follow_up.pk]})
        row = self.rows("follow_up_done").get()
        self.assertEqual((row.object_type, row.object_id), ("accounts.followup", follow_up.pk))
        self.assertNotIn("bo@example.org", json.dumps(row.changes))

    def test_merging_two_people(self):
        a, b = Person.objects.create(name="Bo"), Person.objects.create(name="Bo")
        self.client.post(reverse("admin:structure_person_changelist"), {"action": "merge", "_selected_action": [a.pk, b.pk]})
        self.assertEqual(Person.objects.filter(name="Bo").count(), 1)
        row = self.rows("merge").get()
        self.assertEqual((row.object_type, row.object_id, row.changes), ("structure.person", a.pk, {"person": [b.pk, a.pk]}))

    def test_a_merge_core_makes_itself_is_recorded_as_cores(self):
        user, _ = make_user("bo@example.org")
        other = Person.objects.create(name="Robert")
        EmailAddress.objects.create(person=other, address="robert@example.org", verified_at=timezone.now())
        user.email = "robert@example.org"
        user.save()
        row = AuditEntry.objects.get(action="merge")
        self.assertEqual((row.via, row.actor_account, row.actor_person), ("core", None, None))

    def test_a_superuser_giving_permissions_or_deleting_an_account(self):
        root, _ = make_user("root@example.org", two_factor=True)
        root.is_superuser = True
        root.save()
        self.client.force_login(root)
        user, _ = make_user("bo@example.org")
        view_units = AccountPermission.objects.get(content_type__app_label="structure", codename="view_unit")
        response = self.client.post(reverse("admin:accounts_user_change", args=[user.pk]), {
            "email": "bo@example.org", "name": "", "is_active": "on", "groups": [], "user_permissions": [view_units.pk],
        })
        self.assertEqual(response.status_code, 302, response.content.decode()[:3000])
        changed = AuditEntry.objects.get(action="change_account")
        self.assertEqual((changed.actor_account, changed.changes), (root.pk, {"user_permissions": [[], ["structure.view_unit"]]}))
        self.client.post(reverse("admin:accounts_user_delete", args=[user.pk]), {"post": "yes"})
        self.assertFalse(User.objects.filter(pk=user.pk).exists())
        deleted = AuditEntry.objects.get(action="delete_account")
        self.assertEqual((deleted.object_id, deleted.changes["email"], deleted.changes["person_removed"]), (user.pk, None, True))
        self.assertFalse(LogEntry.objects.exists())

    def test_django_keeps_no_history_of_its_own(self):
        self.client.post(reverse("admin:accounts_user_add"), {"email": "bo@example.org", "name": "Bo", "is_active": "on", "groups": []})
        self.assertFalse(LogEntry.objects.exists())
        self.assertNotContains(self.client.get(reverse("admin:index")), "Recent actions")

    def test_the_update_deletes_the_history_it_kept(self):
        LogEntry.objects.create(user=self.operator, content_type=ContentType.objects.get_for_model(User), object_id="1", object_repr="Anna Example", action_flag=ADDITION)
        importlib.import_module("structure.migrations.0006_audit_log").forget_admin_history(apps, None)
        self.assertFalse(LogEntry.objects.exists())


# ---- The admin page: Operators see, and change nothing in the tree -----------------------


class AdminPage(Tree):
    client_class = CaddyClient
    SHOWN = (Unit, Person, Membership, Position, Reason, Grant, SeatRule, AuditEntry)

    def enter(self, user):
        self.client.force_login(user)

    def operator(self):
        return User.objects.get(email="operator@example.org")

    def test_an_operator_sees_everything_and_changes_nothing(self):
        actions.change_unit(self.p("lead@uavs"), unit=self.u("uavs"), name="UAV team", via=SERVER)  # so the log has a row
        self.enter(self.operator())
        before = state()
        for model in self.SHOWN:
            name = f"admin:structure_{model._meta.model_name}"
            obj = model.objects.order_by("pk").first()
            with self.subTest(model=model.__name__):
                self.assertEqual(self.client.get(reverse(f"{name}_changelist")).status_code, 200)
                self.assertEqual(self.client.get(reverse(f"{name}_change", args=[obj.pk])).status_code, 200)
                self.assertEqual(self.client.get(reverse(f"{name}_add")).status_code, 403)
                self.assertEqual(self.client.post(reverse(f"{name}_add"), {}).status_code, 403)
                self.assertEqual(self.client.post(reverse(f"{name}_change", args=[obj.pk]), {}).status_code, 403)
                self.assertEqual(self.client.post(reverse(f"{name}_delete", args=[obj.pk]), {"post": "yes"}).status_code, 403)
        self.assertEqual(state(), before)

    def test_merging_people_is_the_one_action_left(self):
        request = RequestFactory().get("/")
        request.user = self.operator()
        for model in self.SHOWN:
            with self.subTest(model=model.__name__):
                found = set(admin.site._registry[model].get_actions(request))
                self.assertEqual(found, {"merge"} if model is Person else set())

    def test_not_even_a_superuser_changes_the_log(self):
        actions.change_unit(self.p("lead@uavs"), unit=self.u("uavs"), name="UAV team", via=SERVER)
        root, _ = make_user("root@example.org", two_factor=True)
        root.is_superuser = True
        root.save()
        self.enter(root)
        row = AuditEntry.objects.get()
        self.assertEqual(self.client.get(reverse("admin:structure_auditentry_changelist")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:structure_auditentry_add")).status_code, 403)
        self.assertEqual(self.client.post(reverse("admin:structure_auditentry_change", args=[row.pk]), {"action": "x"}).status_code, 403)
        self.assertEqual(self.client.post(reverse("admin:structure_auditentry_delete", args=[row.pk]), {"post": "yes"}).status_code, 403)
        self.assertEqual(AuditEntry.objects.get().action, "change_unit")

    def test_the_log_shows_who_and_what_while_they_exist(self):
        actions.change_unit(self.p("lead@uavs"), unit=self.u("uavs"), name="UAV team", via=SERVER)
        self.enter(self.operator())
        page = self.client.get(reverse("admin:structure_auditentry_changelist")).content.decode()
        self.assertIn("lead@uavs", page)
        self.assertIn("UAV team", page)

    def test_someone_merged_since_is_shown_as_who_they_became(self):
        gone, kept = someone("Gone Example"), someone("Kept Example")
        AuditEntry.record("change_unit", SERVER, person=gone.pk, unit=self.u("uavs"), obj=self.u("uavs"))
        Person.merge(kept, gone)
        self.enter(self.operator())
        self.assertIn("Kept Example, merged since", self.client.get(reverse("admin:structure_auditentry_changelist")).content.decode())
        row = AuditEntry.objects.get(action="change_unit")
        self.assertContains(self.client.get(reverse("admin:structure_auditentry_change", args=[row.pk])), "Kept Example, merged since")

    def test_operators_may_only_view_the_tree_people_and_log(self):
        codenames = Group.objects.get(name="Operator").permissions.filter(content_type__app_label="structure").values_list("codename", flat=True)
        self.assertTrue(codenames)
        self.assertEqual([c for c in codenames if not c.startswith("view_")], [])
        self.assertIn("view_auditentry", codenames)


# ---- The made-up installations ----------------------------------------------------------


class MadeUp(TestCase):
    def build(self, name):
        root = Unit.objects.create(kind=ASSOCIATION, key="test", name="Test Association")
        examples.build(name, root)
        return root

    def test_both_are_built_through_the_actions(self):
        for name in examples.EXAMPLES:
            with self.subTest(name=name), transaction.atomic():
                root = self.build(name)
                rows = AuditEntry.objects.order_by("pk")
                made = {(row.object_type, row.object_id) for row in rows}
                ties = [
                    *(("structure.unit", pk) for pk in Unit.objects.exclude(pk=root.pk).values_list("pk", flat=True)),
                    *(("structure.membership", pk) for pk in Membership.objects.values_list("pk", flat=True)),
                    *(("structure.position", pk) for pk in Position.objects.filter(seat_rule=None).values_list("pk", flat=True)),
                    *(("structure.reason", pk) for pk in Reason.objects.values_list("pk", flat=True)),
                    *(("structure.grant", pk) for pk in Grant.objects.values_list("pk", flat=True)),
                    *(("structure.seatrule", pk) for pk in SeatRule.objects.values_list("pk", flat=True)),
                ]
                self.assertEqual([tie for tie in ties if tie not in made], [])
                first = [(row.action, row.actor_person) for row in rows[:2]]
                self.assertEqual(first, [("add_membership", None), ("record_root_chair", None)])  # as kit chair does
                rest = [row for row in rows[2:] if row.action != "delete_people"]  # core's own, last (test_deleting.py)
                self.assertTrue(all(row.via == "server" and row.actor_person for row in rest))
                self.assertGreaterEqual(len({row.actor_person for row in rest}), 5)  # not one all-powerful actor
                transaction.set_rollback(True)

    def test_in_example_federation_a_seat_and_leads_do_their_part(self):
        self.build("federation")

        def by(address, role, unit):
            position = Position.objects.get(person__emails__address=address, role=role, unit__key=unit, seat_rule=None)
            actor = AuditEntry.objects.get(action="appoint", object_id=position.pk).actor_person
            return Person.objects.get(pk=actor).emails.get().address

        self.assertEqual(by("nils@example.org", Role.LEAD, "uavs"), "pia@example.org")  # Development's lead by a seat
        self.assertEqual(by("mira@example.org", Role.LEAD, "drone-race-2027"), "nils@example.org")
        self.assertEqual(by("disa@example.org", Role.PARTICIPANT, "lab-nights"), "jana@example.org")
        self.assertEqual(by("una@example.org", Role.CHAIR, "test"), "viktor@example.org")  # the outgoing chair records the next

    def test_viktor_gives_his_grant_while_chair_and_it_outlives_him(self):
        self.build("federation")
        viktor = Person.objects.get(emails__address="viktor@example.org")
        una = Position.objects.get(person__emails__address="una@example.org", role=Role.CHAIR)
        grant = AuditEntry.objects.get(action="give_grant", actor_person=viktor.pk)
        successor = AuditEntry.objects.get(action="appoint", object_id=una.pk)
        self.assertLess(grant.pk, successor.pk)
        self.assertTrue(Grant.objects.filter(given_by=viktor, end=None).exists())

    def test_a_root_with_a_real_chair_is_left_alone(self):
        root = Unit.objects.create(kind=ASSOCIATION, key="test", name="Test Association")
        real = User.objects.create_user("real@example.com", name="Real Chair").person
        Membership.objects.create(person=real, association=root, start=LONG_AGO)
        chair = Position.objects.create(person=real, unit=root, role=Role.CHAIR, start=LONG_AGO)
        for replace in (False, True):
            with self.subTest(replace=replace), self.assertRaises(ValidationError):
                examples.build("society", root, replace=replace)
        self.assertFalse(root.descendants().exists() or AuditEntry.objects.exists())
        self.assertIsNone(Position.objects.get(pk=chair.pk).end)

    def test_the_first_chair_of_example_federation_is_also_a_member_of_the_root(self):
        root = self.build("federation")
        viktor = Person.objects.get(emails__address="viktor@example.org")
        self.assertEqual(set(viktor.memberships.values_list("association__key", flat=True)), {root.key, "robotics"})
