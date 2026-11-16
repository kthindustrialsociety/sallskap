"""Tests for keys to the tools (structure.md §5), written before the code. First a table
of ties and the keys they give over the tree in structure.md §1, then what changes keys
and asks the worker to tell the tools, the groups that went, and the made-up
installations."""

from datetime import date, timedelta
from unittest import mock

from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.contrib.auth.models import Group
from django.test import TestCase

from accounts import sync
from accounts.models import AccessSync, GroupRole, ToolAccess
from accounts.test_login_service import make_tool

from . import examples
from .access import for_tools, keys, known_keys
from .models import Membership, Person, Position, Reason, SeatRule, Unit
from .test_authority import LONG_AGO, TODAY, User, grant, unit

ASSOCIATION, TEAM = Unit.Kind.ASSOCIATION, Unit.Kind.TEAM
Role = Position.Role
TOMORROW = TODAY + timedelta(days=1)

# ---- The table ------------------------------------------------------------------------
#
# The tree in test_authority.py: the root (example), with Extended board, the hackathon
# that Robotics and Chemistry take part in, Robotics with Operations, Development, UAVs
# and Drone race 2027, and Chemistry's website, run by Robotics for Chemistry. Plus an
# archived project under the root, old.
#
# Each row is one person: the ties they hold, then every key they get today, or - for
# none. A tie is role@unit for a position, member@ or honorary@ for a membership, lapsed@
# for one that ended today, and alumnus@ for a reason. +1 starts tomorrow, and -0 ended
# today. Everything else started long ago. operator is the Operator group.
#
# Two seat rules hold: the chair of each association directly under the root sits on
# the root's board, and Robotics' vice chair leads Development. A grant gives the root's
# treasurer fees and treasury over everything, which gives no keys.

TABLE = """
member@root                       | example:member member
member@rob                        | example:member member robotics:member
honorary@chem                     | chemistry:member example:member member
lapsed@rob                        | -
alumnus@rob                       | -
chair@root member@rob             | board example:chair example:member member robotics:member
treasurer@root member@root        | board example:member example:treasurer member treasurer
secretary@root member@chem        | board chemistry:member example:member example:secretary member
board_member@rob member@rob       | example:member member robotics:board_member robotics:member
vice_chair@rob member@rob         | development:lead example:member member robotics:member robotics:vice_chair
chair@chem member@chem            | board chemistry:chair chemistry:member example:board_member example:member member
lead@dev member@rob               | development:lead example:member member robotics:member
participant@uavs                  | uavs:participant
lead@race member@rob              | drone-race-2027:lead example:member member robotics:member
participant@ext member@root       | example:member extended-board:participant member
lead@hack member@chem             | chemistry:member example:member member spring-hackathon-2027:lead
participant@hack                  | spring-hackathon-2027:participant
lead@web member@rob               | chemistry-website:lead example:member member robotics:member
participant@web member@chem       | chemistry-website:participant chemistry:member example:member member
lead@old member@root              | example:member member
treasurer@rob+1 member@rob        | example:member member robotics:member
treasurer@rob-0 member@rob        | example:member member robotics:member
participant@ops+1                 | -
operator                          | operator
operator member@root              | example:member member operator
nothing                           | -
"""


def parse(table):
    return [
        (left.split(), set() if right.strip() == "-" else set(right.split()))
        for line in table.strip().splitlines()
        for left, right in [line.split("|")]
    ]


class Tree(TestCase):
    """The tree above, with someone for each row of the table, each with an account."""

    @classmethod
    def setUpTestData(cls):
        u = cls.u = {}
        u["root"] = unit(ASSOCIATION, "example")
        u["ext"] = unit(TEAM, "extended-board", u["root"])
        u["hack"] = unit(TEAM, "spring-hackathon-2027", u["root"], end_date=date(2027, 4, 25))
        u["old"] = unit(TEAM, "autumn-hackathon-2025", u["root"], end_date=date(2025, 11, 15))
        u["rob"] = unit(ASSOCIATION, "robotics", u["root"])
        u["ops"] = unit(TEAM, "operations", u["rob"])
        u["dev"] = unit(TEAM, "development", u["rob"])
        u["uavs"] = unit(TEAM, "uavs", u["dev"])
        u["race"] = unit(TEAM, "drone-race-2027", u["uavs"], end_date=date(2027, 6, 5))
        u["chem"] = unit(ASSOCIATION, "chemistry", u["root"])
        u["web"] = unit(TEAM, "chemistry-website", u["rob"], run_for=u["chem"])
        u["hack"].taking_part.add(u["rob"], u["chem"])

        cls.rows = parse(TABLE)
        cls.users = []
        for number, (ties, _) in enumerate(cls.rows):
            user = User.objects.create_user(f"row{number}@example.org", name=" ".join(ties))
            for tie in sorted(ties, key=lambda tie: not tie.split("@")[0] in ("member", "honorary", "lapsed")):  # memberships first
                cls.tie(user, tie)
            cls.users.append(user)
        cls.chair = User.objects.get(name="chair@root member@rob").person
        SeatRule.objects.create(unit=u["root"], role=Role.BOARD_MEMBER, from_roles=[Role.CHAIR], held_in=u["root"], each_association_under=True, made_by=cls.chair, start=LONG_AGO)
        SeatRule.objects.create(unit=u["dev"], role=Role.LEAD, from_roles=[Role.VICE_CHAIR], held_in=u["rob"], made_by=cls.chair, start=LONG_AGO)
        grant("manage_finances", Role.TREASURER, u["root"], u["root"], cls.chair, include_below=True)
        u["old"].archive()

    @classmethod
    def tie(cls, user, tie):
        if tie == "nothing":
            return
        if tie == "operator":
            user.groups.add(Group.objects.get(role__key="operator"))
            return
        what, where = tie.split("@")
        start, end = LONG_AGO, None
        if where.endswith("+1"):
            where, start = where[:-2], TOMORROW
        elif where.endswith("-0"):
            where, end = where[:-2], TODAY
        person, unit = user.person, cls.u[where]
        if what in ("member", "honorary", "lapsed"):
            kind = Membership.Kind.HONORARY if what == "honorary" else Membership.Kind.ORDINARY
            Membership.objects.create(person=person, association=unit, kind=kind, start=start, end=TODAY if what == "lapsed" else end)
        elif what == "alumnus":
            Reason.objects.create(person=person, unit=unit, kind=Reason.Kind.ALUMNUS, legal_basis=Reason.Basis.CONSENT, start=start, expires=date(2099, 1, 1))
        else:
            Position.objects.create(person=person, unit=unit, role=what, start=start, end=end)


class Table(Tree):
    def test_every_row_gets_exactly_its_keys(self):
        wrong = []
        for user, (ties, expected) in zip(self.users, self.rows):
            found = set(keys(user))
            if found != expected:
                wrong.append(f"{' '.join(ties)}: expected {sorted(expected)}, got {sorted(found)}")
        self.assertEqual(wrong, [], "\n".join(wrong))

    def test_keys_are_sorted_and_each_once(self):
        for user in self.users:
            found = keys(user)
            self.assertEqual(found, sorted(set(found)))

    def test_tomorrows_keys_come_tomorrow(self):
        user = User.objects.get(name="treasurer@rob+1 member@rob")
        self.assertNotIn("robotics:treasurer", keys(user))
        self.assertIn("robotics:treasurer", keys(user, on=TOMORROW))
        self.assertEqual(set(keys(User.objects.get(name="participant@ops+1"), on=TOMORROW)), {"operations:participant"})

    def test_yesterdays_keys_were_there_yesterday(self):
        user = User.objects.get(name="treasurer@rob-0 member@rob")
        self.assertIn("robotics:treasurer", keys(user, on=TODAY - timedelta(days=1)))
        self.assertIn("robotics:member", keys(User.objects.get(name="lapsed@rob"), on=TODAY - timedelta(days=1)))

    def test_an_archived_project_gave_its_keys_until_it_was_archived(self):
        user = User.objects.get(name="lead@old member@root")
        self.assertIn("autumn-hackathon-2025:lead", keys(user, on=TODAY - timedelta(days=1)))

    def test_a_seat_gives_the_same_keys_as_an_appointed_position(self):
        seated = User.objects.get(name="chair@chem member@chem")
        self.assertTrue(Position.objects.filter(person=seated.person, unit=self.u["root"], seat_rule__isnull=False).exists())
        appointed = User.objects.create_user("appointed@example.org")
        Membership.objects.create(person=appointed.person, association=self.u["chem"], start=LONG_AGO)
        Position.objects.create(person=appointed.person, unit=self.u["root"], role=Role.BOARD_MEMBER, start=LONG_AGO)
        root_keys = lambda user: {key for key in keys(user) if not key.startswith("chemistry:")}  # noqa: E731
        self.assertEqual(root_keys(seated), root_keys(appointed))

    def test_a_grant_gives_no_keys(self):
        treasurer = User.objects.get(name="treasurer@root member@root")
        before = keys(treasurer)
        grant("see_members", Role.TREASURER, self.u["root"], self.u["rob"], self.chair)
        self.assertEqual(keys(treasurer), before)

    def test_a_turned_off_account_still_has_its_keys_and_the_tools_turn_it_away(self):
        user = User.objects.get(name="member@root")
        user.is_active = False
        user.save()
        self.assertEqual(set(keys(user)), {"example:member", "member"})


# ---- Two-factor authentication -----------------------------------------------------------


class TwoFactor(Tree):
    def test_keys_that_need_it_wait_until_it_is_set_up(self):
        user = User.objects.get(name="chair@root member@rob")
        self.assertTrue(user.needs_2fa_setup)
        self.assertEqual(set(for_tools(user)), {"example:member", "member", "robotics:member"})
        TOTP.activate(user, generate_totp_secret())
        self.assertEqual(for_tools(user), keys(user))

    def test_a_board_role_anywhere_holds_back_its_own_keys(self):
        user = User.objects.get(name="board_member@rob member@rob")
        self.assertEqual(set(for_tools(user)), {"example:member", "member", "robotics:member"})

    def test_an_operator_without_it_gets_nothing_from_the_group(self):
        user = User.objects.get(name="operator member@root")
        self.assertEqual(set(for_tools(user)), {"example:member", "member"})

    def test_people_who_dont_need_it_get_everything(self):
        for name in ("lead@dev member@rob", "participant@uavs", "member@rob"):
            user = User.objects.get(name=name)
            self.assertEqual(for_tools(user), keys(user), name)


# ---- Which keys a tool may be limited to --------------------------------------------------


class Known(Tree):
    def test_the_four_and_every_key_a_unit_can_give(self):
        found = known_keys()
        self.assertLessEqual({"member", "board", "treasurer", "operator"}, found)
        self.assertLessEqual({"example:member", "example:chair", "robotics:treasurer", "uavs:lead", "uavs:participant"}, found)
        self.assertFalse({"uavs:member", "uavs:chair", "robotics:lead", "developer", "events", "custom:Board"} & found)

    def test_a_tool_limited_to_a_unit_key(self):
        from accounts.login_service import may_use

        tool = make_tool()
        ToolAccess.objects.create(tool=tool, group_keys=["robotics:member"])

        self.assertTrue(may_use(User.objects.get(name="member@rob"), tool))
        self.assertFalse(may_use(User.objects.get(name="member@root"), tool))


# ---- Telling the tools ---------------------------------------------------------------


class Telling(Tree):
    """Every change that can change someone's keys, or where a tool puts them, asks the
    worker to tell the tools about everyone it touches, in the same save."""

    def setUp(self):
        AccessSync.objects.all().delete()

    def asked(self):
        return set(AccessSync.objects.filter(pending=True).values_list("user__name", flat=True))

    def test_a_new_membership(self):
        user = User.objects.get(name="participant@uavs")
        Membership.objects.create(person=user.person, association=self.u["chem"], start=TODAY)
        self.assertEqual(self.asked(), {user.name})

    def test_ending_a_membership(self):
        Membership.objects.get(person__user__name="member@rob").end_on()
        self.assertEqual(self.asked(), {"member@rob"})

    def test_a_new_position_and_its_seat(self):
        user = User.objects.create_user("new-vice@example.org", name="new vice")
        Membership.objects.create(person=user.person, association=self.u["rob"], start=LONG_AGO)
        AccessSync.objects.all().delete()
        Position.objects.create(person=user.person, unit=self.u["rob"], role=Role.VICE_CHAIR, start=TODAY)
        self.assertEqual(self.asked(), {"new vice"})
        self.assertTrue(Position.objects.filter(person=user.person, seat_rule__isnull=False).exists())

    def test_a_new_chair_and_the_last_one(self):
        new = User.objects.create_user("new-chair@example.org", name="new chair")
        Membership.objects.create(person=new.person, association=self.u["chem"], start=LONG_AGO)
        AccessSync.objects.all().delete()
        Position.objects.create(person=new.person, unit=self.u["chem"], role=Role.CHAIR, start=TODAY)
        self.assertEqual(self.asked(), {"new chair", "chair@chem member@chem"})
        self.assertNotIn("chemistry:chair", keys(User.objects.get(name="chair@chem member@chem")))
        self.assertNotIn("example:board_member", keys(User.objects.get(name="chair@chem member@chem")))

    def test_archiving_a_project_asks_for_everyone_in_it(self):
        self.u["race"].archive()
        self.assertEqual(self.asked(), {"lead@race member@rob"})
        self.assertNotIn("drone-race-2027:lead", keys(User.objects.get(name="lead@race member@rob")))

    def test_archiving_a_team_asks_for_everyone_under_it(self):
        self.u["dev"].archive()
        self.assertEqual(self.asked(), {"lead@dev member@rob", "participant@uavs", "lead@race member@rob", "vice_chair@rob member@rob"})

    def test_bringing_one_back(self):
        self.u["old"].unarchive()
        self.assertEqual(self.asked(), {"lead@old member@root"})
        self.assertIn("autumn-hackathon-2025:lead", keys(User.objects.get(name="lead@old member@root")))

    def test_moving_a_unit_asks_for_everyone_in_the_moved_subtree(self):
        self.u["uavs"].parent = self.u["ops"]
        self.u["uavs"].save()
        self.assertEqual(self.asked(), {"participant@uavs", "lead@race member@rob"})

    def test_moving_an_association_asks_for_its_members_and_people_too(self):
        inner = unit(ASSOCIATION, "inner", self.u["chem"])
        user = User.objects.create_user("inner@example.org", name="inner member")
        Membership.objects.create(person=user.person, association=inner, start=LONG_AGO)
        AccessSync.objects.all().delete()
        inner.parent = self.u["rob"]
        inner.save()
        self.assertEqual(self.asked(), {"inner member"})
        self.assertEqual(set(keys(user)), {"inner:member", "robotics:member", "example:member", "member"})

    def test_merging_two_people_asks_for_the_one_kept(self):
        user = User.objects.get(name="nothing")
        other = Person.objects.create(name="Same person")
        Membership.objects.create(person=other, association=self.u["rob"], start=LONG_AGO)
        AccessSync.objects.all().delete()
        Person.merge(user.person, other)
        self.assertEqual(self.asked(), {"nothing"})
        self.assertIn("robotics:member", keys(user))

    def test_linking_a_person_to_another_account(self):
        user = User.objects.get(name="nothing")
        person = Person.objects.create(name="nothing")  # the account takes its person's name
        Membership.objects.create(person=person, association=self.u["rob"], start=LONG_AGO)
        old = user.person
        Person.objects.filter(pk=old.pk).update(user=None)
        AccessSync.objects.all().delete()
        person.user = user
        person.save()
        self.assertEqual(self.asked(), {"nothing"})

    def test_grants_and_reasons_change_no_keys_and_ask_nothing(self):
        grant("send_mailings", Role.LEAD, self.u["ops"], self.u["rob"], self.chair)
        Reason.objects.create(person=User.objects.get(name="nothing").person, unit=self.u["rob"], kind=Reason.Kind.APPLICANT, legal_basis=Reason.Basis.CONSENT, expires=date(2099, 1, 1))
        self.assertEqual(self.asked(), set())

    def test_people_with_no_account_need_nothing_sent(self):
        nobody = Person.objects.create(name="No account")
        Membership.objects.create(person=nobody, association=self.u["rob"], start=LONG_AGO)
        self.assertEqual(AccessSync.objects.count(), 0)


class NewDay(Tree):
    """Ties start and end on days with no save, such as a board taking office on 1 July.
    The worker asks about them just after midnight."""

    def setUp(self):
        AccessSync.objects.all().delete()

    def test_a_position_starting_tomorrow_is_asked_about_tomorrow(self):
        sync.request_for_new_days(TODAY, TOMORROW)
        asked = set(AccessSync.objects.values_list("user__name", flat=True))
        self.assertEqual(asked, {"treasurer@rob+1 member@rob", "participant@ops+1"})

    def test_ties_ending_today_are_asked_about_today(self):
        sync.request_for_new_days(TODAY - timedelta(days=1), TODAY)
        asked = set(AccessSync.objects.values_list("user__name", flat=True))
        self.assertEqual(asked, {"treasurer@rob-0 member@rob", "lapsed@rob"})

    def test_days_the_worker_missed_are_covered(self):
        sync.request_for_new_days(TODAY - timedelta(days=3), TOMORROW)
        asked = set(AccessSync.objects.values_list("user__name", flat=True))
        self.assertEqual(asked, {"treasurer@rob+1 member@rob", "participant@ops+1", "treasurer@rob-0 member@rob", "lapsed@rob"})

    def test_the_worker_does_it_once_the_day_changes(self):
        calls = []
        with mock.patch.object(sync, "request_for_new_days", side_effect=lambda since, today: calls.append((since, today))):
            day = sync.new_day(TODAY)  # nothing happens on the same day
            self.assertEqual((day, calls), (TODAY, []))
            with mock.patch("django.utils.timezone.localdate", return_value=TOMORROW):
                day = sync.new_day(TODAY)
        self.assertEqual((day, calls), (TOMORROW, [(TODAY, TOMORROW)]))


# ---- The same keys everywhere ------------------------------------------------------------


class SameKeys(Tree):
    def test_the_login_response_and_the_connectors_get_the_same_keys(self):
        from allauth.idp.oidc.adapter import get_adapter

        tool = make_tool()
        for user in self.users:
            if user.requires_2fa:
                TOTP.activate(user, generate_totp_secret())
            sync.request_for(user)
            row = AccessSync.objects.get(user=user)
            told = sync._person(row, tool, sync.tree()).groups
            claims = get_adapter().get_claims("userinfo", user, tool, ["openid", "groups"])
            self.assertEqual(list(told), claims["groups"], user.name)
            self.assertEqual(list(told), keys(user), user.name)

    def test_the_connectors_get_the_tree(self):
        tree = {unit.key: unit for unit in sync.tree()}
        self.assertEqual(tree["example"].association, "example")
        self.assertTrue(tree["example"].root)
        self.assertEqual(tree["robotics"].association, "robotics")
        self.assertEqual((tree["uavs"].association, tree["uavs"].team, tree["uavs"].root), ("robotics", True, False))
        self.assertEqual(tree["chemistry-website"].association, "robotics")  # the one that owns it, not the one it's for
        self.assertEqual(tree["spring-hackathon-2027"].association, "example")
        self.assertEqual(tree["drone-race-2027"].name, "Drone-Race-2027")


# ---- The groups that went ----------------------------------------------------------------


# Every group there was before A5, and what's left of it after the migration.
GROUPS = """
Board        gone
Treasurer    gone
Events       gone
Developer    gone
Member       gone
Operator     kept
Photography  gone
"""


class Groups(TestCase):
    def test_operator_is_the_one_group(self):
        self.assertEqual(list(Group.objects.values_list("role__key", flat=True)), ["operator"])

    def test_the_migration_keeps_operator_and_its_people_and_removes_the_rest(self):
        import importlib

        from django.apps import apps

        migration = importlib.import_module("accounts.migrations.0004_operator_is_the_one_group")
        operator = User.objects.create_user("op@example.org")
        operator.groups.add(Group.objects.get(role__key="operator"))
        someone = User.objects.create_user("someone@example.org")
        for line in GROUPS.strip().splitlines():
            name, _ = line.split()
            if name != "Operator":
                group = Group.objects.create(name=name)
                if name != "Photography":  # one made by hand
                    GroupRole.objects.create(group=group, key=name.lower())
                someone.groups.add(group)
                operator.groups.add(group)
        migration.remove_groups(apps, None)
        left = {name: ("kept" if Group.objects.filter(name=name).exists() else "gone") for name, _ in (line.split() for line in GROUPS.strip().splitlines())}
        self.assertEqual(left, dict(line.split() for line in GROUPS.strip().splitlines()))
        self.assertEqual(list(operator.groups.values_list("name", flat=True)), ["Operator"])
        self.assertFalse(someone.groups.exists())
        self.assertEqual(keys(operator), ["operator"])
        self.assertEqual(keys(someone), [])

    def test_a_renamed_operator_group_is_kept(self):
        Group.objects.filter(role__key="operator").update(name="Drift")
        import importlib

        from django.apps import apps

        importlib.import_module("accounts.migrations.0004_operator_is_the_one_group").remove_groups(apps, None)
        self.assertEqual(list(Group.objects.values_list("name", flat=True)), ["Drift"])


# ---- The made-up installations ---------------------------------------------------------


class MadeUp(TestCase):
    def build(self, name):
        root = Unit.objects.create(kind=ASSOCIATION, key="test", name="Test Association")
        examples.build(name, root)

    def keys_of(self, email):
        return set(keys(User.objects.get(email=email)))

    def test_example_society_has_only_its_own_keys(self):
        self.build("society")
        self.assertEqual(self.keys_of("alva@example.org"), {"board", "member", "test:chair", "test:member"})
        self.assertEqual(self.keys_of("cleo@example.org"), {"board", "member", "test:member", "test:treasurer", "treasurer"})
        self.assertEqual(self.keys_of("gun@example.org"), {"events:lead", "member", "test:member"})
        self.assertEqual(self.keys_of("mio@example.org"), {"member", "photography:participant", "test:member", "website:participant"})
        self.assertEqual(self.keys_of("nora@example.net"), {"spring-hackathon-2027:participant"})
        self.assertEqual(self.keys_of("kaj@example.org"), {"member", "test:member"})  # last year's project, archived
        self.assertEqual(self.keys_of("olof@example.org"), {"member", "test:member"})  # honorary
        self.assertEqual(self.keys_of("petra@example.org"), set())  # lapsed
        every = set().union(*(set(keys(user)) for user in User.objects.all()))
        self.assertEqual({key.split(":")[0] for key in every if ":" in key}, {"test", "events", "photography", "website", "spring-hackathon-2027"})

    def test_example_federation_seats_and_all(self):
        self.build("federation")
        self.assertEqual(self.keys_of("una@example.org"), {"board", "chemistry:member", "extended-board:participant", "member", "test:chair", "test:member"})
        self.assertEqual(self.keys_of("viktor@example.org"), {"member", "robotics:member", "test:member"})  # chair until July
        self.assertEqual(self.keys_of("quinn@example.org"), {
            "board", "extended-board:participant", "member", "robotics:chair", "robotics:member", "test:board_member", "test:member",
        })
        self.assertEqual(self.keys_of("pia@example.org"), {
            "development:lead", "extended-board:participant", "member", "robotics:member", "robotics:vice_chair", "test:member",
        })
        self.assertEqual(self.keys_of("astrid@example.org"), {"member", "robotics:member", "test:member"})  # her seat ended with her chair
        self.assertEqual(self.keys_of("wilma@example.org"), {
            "chemistry:board_member", "chemistry:member", "member", "robotics:member", "robotics:treasurer", "test:member",
        })
        self.assertEqual(self.keys_of("hedda@example.net"), {"spring-hackathon-2027:participant"})
        self.assertEqual(self.keys_of("greta@example.org"), {"advisory-board:participant"})  # an alumnus, no longer a member
        self.assertEqual(self.keys_of("leo@example.org"), {"chemistry-website:lead", "member", "robotics:member", "test:member"})
