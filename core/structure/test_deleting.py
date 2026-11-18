"""Tests for deleting people with nothing left (structure.md §3 and §7), written before the
code. First a table: who is kept and who is deleted, the day before their 30 days are up
and the day they are. Then what's kept of someone deleted and what isn't, the audit log's
row, a kept position that nobody can change, the worker, the command, the admin page, and
both made-up installations."""

import io
import json
from datetime import date, datetime, time, timedelta
from unittest import mock

from django.apps import apps
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts import sync
from accounts.models import AccessSync, FollowUp
from accounts.tests import CaddyClient, make_user

from . import actions, deleting, examples
from .authority import can
from .models import AuditEntry, EmailAddress, Grant, Membership, Permission, Person, Position, Reason, SeatRule, Unit

ASSOCIATION, TEAM = Unit.Kind.ASSOCIATION, Unit.Kind.TEAM
Role = Position.Role
User = get_user_model()
TODAY = timezone.localdate()
YESTERDAY = TODAY - timedelta(days=1)
ENDED = TODAY - timedelta(days=30)  # whoever's last tie ended that day is due today, and not yesterday
LONG_AGO = date(2020, 1, 1)
LATER = TODAY + timedelta(days=60)
CORE, SERVER = AuditEntry.Via.CORE, AuditEntry.Via.SERVER


def unit(kind, key, parent=None):
    return Unit.objects.create(kind=kind, key=key, name=key.replace("-", " ").title(), parent=parent)


def address_of(name):
    return name.lower().replace(" ", "-").replace(",", "") + "@example.org"


def added_on(p, day):
    """As if the person had been added that day."""
    Person.objects.filter(pk=p.pk).update(created_at=timezone.make_aware(datetime.combine(day, time(12))))
    p.refresh_from_db()
    return p


def person(name, address=None, verified=True, account=False, added=LONG_AGO):
    """Someone added long ago, unless `added` says otherwise, as nearly everyone is."""
    if account:
        p = User.objects.create_user(address or address_of(name), name=name).person
    else:
        p = Person.objects.create(name=name)
        EmailAddress.objects.create(person=p, address=address or address_of(name), verified_at=timezone.now() if verified else None)
    return added_on(p, added)


def member(p, association, start=LONG_AGO, end=None):
    return Membership.objects.create(person=p, association=association, start=start, end=end)


def holds(p, where, role, start=LONG_AGO, end=None):
    return Position.objects.create(person=p, unit=where, role=role, start=start, end=end)


def reason(p, where, start=LONG_AGO, expires=LATER):
    return Reason.objects.create(
        person=p, unit=where, kind=Reason.Kind.ALUMNUS, legal_basis=Reason.Basis.CONSENT, source="The alumni offer", start=start, expires=expires,
    )


def runs():
    return list(AuditEntry.objects.filter(action="delete_people").order_by("pk"))


# ---- Who goes, and when: the table ------------------------------------------------------


class Table(TestCase):
    """Each person is named for their case. The day before: yesterday. The day they're
    due: today, 30 days after ENDED."""

    # name: (kept yesterday, kept today)
    EXPECTED = {
        "an account": (True, True),
        "a deactivated account": (True, True),
        "a current membership": (True, True),
        "a membership ending later": (True, True),
        "a membership starting later": (True, True),
        "a membership ended today": (True, True),
        "an ended membership": (True, False),
        "an ended position": (True, False),
        "a position starting later": (True, True),
        "an ended seat": (True, False),
        "a live reason": (True, True),
        "a reason starting later": (True, True),
        "an expired reason": (True, False),
        "a reason that expired a day later": (True, True),
        "a grant they gave": (True, False),
        "a rule they made": (True, False),
        "merged": (True, False),
        "an unverified address, added then": (True, False),
        "never a tie, added long ago": (False, False),
        "never a tie, added lately": (True, True),
    }

    def setUp(self):
        root = self.root = unit(ASSOCIATION, "example")
        robotics = unit(ASSOCIATION, "robotics", root)
        website = self.website = unit(TEAM, "website", root)
        p = {name: person(name, account=name.endswith("account")) for name in self.EXPECTED if name != "merged"}

        member(p["an account"], root, end=ENDED)
        p["a deactivated account"].user.is_active = False
        p["a deactivated account"].user.save()
        member(p["a deactivated account"], root, end=ENDED)
        member(p["a current membership"], root)
        member(p["a membership ending later"], root, end=LATER)
        member(p["a membership starting later"], root, start=LATER)
        member(p["a membership ended today"], root, end=TODAY)
        member(p["an ended membership"], root, end=ENDED)
        holds(p["an ended position"], website, Role.PARTICIPANT, end=ENDED)
        holds(p["a position starting later"], website, Role.PARTICIPANT, start=LATER)
        reason(p["a live reason"], root)
        reason(p["a reason starting later"], root, start=LATER, expires=LATER + timedelta(days=365))
        reason(p["an expired reason"], root, expires=ENDED)
        member(p["a reason that expired a day later"], root, end=ENDED - timedelta(days=200))
        reason(p["a reason that expired a day later"], root, expires=ENDED + timedelta(days=1))

        # The root's chair until ENDED made a rule seating the root's board members on the
        # website team, which seated someone until then too.
        maker = p["a rule they made"]
        member(maker, root, end=ENDED)
        holds(maker, root, Role.CHAIR, end=ENDED)
        SeatRule.objects.create(unit=website, role=Role.PARTICIPANT, from_roles=[Role.BOARD_MEMBER], held_in=root, start=LONG_AGO, made_by=maker)
        seated = p["an ended seat"]
        member(seated, root, end=ENDED)
        holds(seated, root, Role.BOARD_MEMBER, end=ENDED)
        assert Position.objects.filter(person=seated, unit=website, seat_rule__isnull=False).exists()

        giver = p["a grant they gave"]
        member(giver, robotics, end=ENDED)
        holds(giver, robotics, Role.CHAIR, end=ENDED)
        Grant.objects.create(permission=Permission.MANAGE_FINANCES, role=Role.TREASURER, held_in=robotics, unit=robotics, start=LONG_AGO, given_by=giver)

        added_on(p["an unverified address, added then"], ENDED).emails.update(verified_at=None)
        added_on(p["never a tie, added lately"], TODAY)

        kept, gone = person("merged"), person("merged away")
        member(kept, root, end=ENDED)
        holds(gone, website, Role.PARTICIPANT, end=ENDED - timedelta(days=10))
        Person.merge(kept, gone)

    def outcome(self, day):
        deleting.delete_people(day)
        return {name: Person.objects.filter(name=name).exists() for name in self.EXPECTED}

    def test_the_day_before(self):
        self.assertEqual(self.outcome(YESTERDAY), {name: kept for name, (kept, _) in self.EXPECTED.items()})

    def test_the_day_they_are_due(self):
        self.assertEqual(self.outcome(TODAY), {name: kept for name, (_, kept) in self.EXPECTED.items()})

    def test_who_is_due_is_who_goes(self):
        due = set(deleting.due(TODAY).values_list("name", flat=True))
        self.assertEqual(due, {name for name, (_, kept) in self.EXPECTED.items() if not kept})

    def test_a_merged_persons_addresses_go_with_them(self):
        self.outcome(TODAY)
        self.assertFalse(EmailAddress.objects.filter(address__in=[address_of("merged"), address_of("merged away")]).exists())
        self.assertEqual(Position.objects.get(kept_name="merged").unit, self.website)  # what came from the one merged away


# ---- Someone who has left: what's kept and what isn't -----------------------------------


class Left(TestCase):
    """Bodil was Robotics' chair until ENDED, led Development before that, gave a grant and
    made two seat rules, one of which seated her. Her membership and her reason ended then
    too. Alva is the root's chair, and Cleo leads Development now."""

    def setUp(self):
        root = self.root = unit(ASSOCIATION, "example")
        robotics = self.robotics = unit(ASSOCIATION, "robotics", root)
        dev = self.dev = unit(TEAM, "development", robotics)
        self.alva = person("Alva Example", account=True)
        member(self.alva, root)
        holds(self.alva, root, Role.CHAIR)
        self.cleo = person("Cleo Example", account=True)
        member(self.cleo, robotics)
        holds(self.cleo, dev, Role.LEAD, start=ENDED)

        bodil = self.bodil = person("Bodil Example", "bodil@example.org")
        EmailAddress.objects.create(person=bodil, address="bodil@example.net")  # a second, unverified
        self.membership = member(bodil, robotics, end=ENDED)
        self.chair = holds(bodil, robotics, Role.CHAIR, end=ENDED)
        self.lead = holds(bodil, dev, Role.LEAD, end=ENDED - timedelta(days=100))
        self.reason = reason(bodil, robotics, start=ENDED, expires=ENDED)
        self.grant = Grant.objects.create(permission=Permission.MANAGE_FINANCES, role=Role.LEAD, held_in=dev, unit=robotics, start=LONG_AGO, given_by=bodil)
        self.seats = SeatRule.objects.create(unit=dev, role=Role.PARTICIPANT, from_roles=[Role.CHAIR], held_in=robotics, start=LONG_AGO, made_by=bodil)
        self.lead_rule = SeatRule.objects.create(unit=dev, role=Role.LEAD, from_roles=[Role.VICE_CHAIR], held_in=robotics, start=LONG_AGO, made_by=bodil)
        self.seat = Position.objects.get(person=bodil, seat_rule=self.seats)
        self.mine = [self.chair.pk, self.lead.pk, self.seat.pk]


class Departed(Left):
    def test_an_expired_reason_leads_to_deletion(self):
        due = person("Due Example")
        reason(due, self.robotics, expires=ENDED)
        not_yet = person("Not Yet Example")
        reason(not_yet, self.robotics, expires=ENDED + timedelta(days=1))
        deleting.delete_people(TODAY)
        self.assertFalse(Person.objects.filter(name="Due Example").exists())
        self.assertTrue(Person.objects.filter(name="Not Yet Example").exists())

    def test_her_ended_positions_survive_with_her_name_and_no_email(self):
        deleting.delete_people(TODAY)
        for position in Position.objects.filter(pk__in=self.mine):
            with self.subTest(position=position.pk):
                self.assertIsNone(position.person_id)
                self.assertEqual(position.kept_name, "Bodil Example")
                self.assertEqual(str(position).split(":")[0], "Bodil Example")
        self.assertEqual(Position.objects.get(pk=self.seat.pk).seat_from_id, self.chair.pk)  # a seat still shows where it came from
        self.assertEqual(Position.objects.get(pk=self.chair.pk).end, ENDED)
        self.assertFalse(EmailAddress.objects.filter(address__startswith="bodil@").exists())

    def test_nothing_else_of_hers_is_left(self):
        pk = self.bodil.pk
        deleting.delete_people(TODAY)
        self.assertFalse(Person.objects.filter(pk=pk).exists())
        self.assertFalse(Membership.objects.filter(pk=self.membership.pk).exists())
        self.assertFalse(Reason.objects.filter(pk=self.reason.pk).exists())
        # Whatever points at a person, now or in a later segment, points at her no more.
        for model in apps.get_models():
            for field in model._meta.get_fields():
                if field.is_relation and field.concrete and field.related_model is Person:
                    with self.subTest(model=model.__name__, field=field.name):
                        self.assertFalse(model._default_manager.filter(**{field.attname: pk}).exists())
        self.assertNotIn("Bodil", json.dumps(list(AuditEntry.objects.values_list("changes", flat=True))))
        self.assertNotIn("bodil", json.dumps(list(AuditEntry.objects.values_list("changes", flat=True))))

    def test_what_she_gave_stays_in_force(self):
        deleting.delete_people(TODAY)
        grant = Grant.objects.get(pk=self.grant.pk)
        self.assertIsNone(grant.given_by_id)
        self.assertIsNone(grant.end)
        self.assertTrue(can(self.cleo, Permission.MANAGE_FINANCES, self.robotics))  # Development's lead, by her grant
        for rule in SeatRule.objects.filter(pk__in=[self.seats.pk, self.lead_rule.pk]):
            self.assertIsNone(rule.made_by_id)
            self.assertIsNone(rule.end)
        pia = person("Pia Example", account=True)
        member(pia, self.robotics)
        holds(pia, self.robotics, Role.VICE_CHAIR)
        self.assertTrue(Position.objects.filter(person=pia, unit=self.dev, role=Role.LEAD, seat_rule=self.lead_rule).exists())

    def test_the_tools_are_told_nothing(self):
        before = list(AccessSync.objects.values_list("sub", "requested_at", "pending"))
        follow_ups = FollowUp.objects.count()
        deleting.delete_people(TODAY)
        self.assertEqual(list(AccessSync.objects.values_list("sub", "requested_at", "pending")), before)
        self.assertEqual(FollowUp.objects.count(), follow_ups)

    def test_a_kept_position_cant_be_changed(self):
        deleting.delete_people(TODAY)
        chair = Position.objects.get(pk=self.chair.pk)
        with self.assertRaises(ValidationError):
            actions.change_position(self.alva, position=chair, title="Founder", via=SERVER)
        with self.assertRaises(ValidationError):
            actions.end_position(self.alva, position=chair, day=LATER, via=SERVER)  # which would open it again
        chair.minutes = "Somewhere else"
        with self.assertRaises(ValidationError):
            chair.save()
        kept = Position.objects.get(pk=self.chair.pk)
        self.assertEqual((kept.end, kept.minutes, kept.title), (ENDED, "", ""))
        self.assertFalse(AuditEntry.objects.exclude(action="delete_people").exists())

    def test_the_database_keeps_no_open_position_without_a_holder(self):
        current = Position.objects.get(person=self.cleo, unit=self.dev, seat_rule=None)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Position.objects.filter(pk=current.pk).update(person=None)

    def test_a_position_is_always_someones_when_its_made(self):
        with self.assertRaises(ValidationError):
            Position(unit=self.dev, role=Role.PARTICIPANT, start=TODAY).save()
        with self.assertRaises(ValidationError):
            Grant(permission=Permission.MANAGE_EVENTS, role=Role.LEAD, held_in=self.dev, unit=self.dev, start=TODAY).save()
        with self.assertRaises(ValidationError):
            SeatRule(unit=self.dev, role=Role.PARTICIPANT, from_roles=[Role.TREASURER], held_in=self.robotics, start=TODAY).save()

    def test_the_tree_can_still_change_around_her_record(self):
        deleting.delete_people(TODAY)
        actions.archive_unit(self.alva, unit=self.dev, via=SERVER)
        actions.bring_back_unit(self.alva, unit=self.dev, via=SERVER)
        self.assertEqual(Position.objects.filter(pk__in=self.mine, person=None, kept_name="Bodil Example").count(), 3)

    def test_a_new_rule_seats_nobody_deleted(self):
        deleting.delete_people(TODAY)
        board = unit(TEAM, "extended-board", self.root)
        actions.make_seat_rule(self.alva, unit=board, role=Role.PARTICIPANT, from_roles=[Role.LEAD], held_in=self.dev, start=LONG_AGO, via=SERVER)
        self.assertEqual(list(Position.objects.filter(unit=board).values_list("person__name", flat=True)), ["Cleo Example"])

    def test_forget_refuses_anyone_still_held(self):
        held = person("Held Example")
        member(held, self.root)
        for who in (self.cleo, held):
            with self.subTest(who=who.name), self.assertRaises(ValidationError):
                deleting.forget(who, TODAY)
        self.assertTrue(Person.objects.filter(pk__in=[self.cleo.pk, held.pk]).count() == 2)
        self.assertTrue(Membership.objects.filter(person=held).exists())

    def test_a_failure_rolls_back_that_person_alone(self):
        other = person("Other Example")
        member(other, self.root, end=ENDED)
        real = deleting.forget

        def forget(who, today):
            if who.pk == self.bodil.pk:
                real(who, today)
                raise RuntimeError("partway")
            return real(who, today)

        with mock.patch.object(deleting, "forget", side_effect=forget), mock.patch.object(deleting.logger, "exception") as logged:
            counts = deleting.delete_people(TODAY)
        logged.assert_called_once()  # logging is off in tests (accounts/tests.py), so the call is what's checked
        self.assertEqual((counts["people"], counts["failed"]), (1, 1))
        self.assertFalse(Person.objects.filter(pk=other.pk).exists())
        self.assertTrue(Person.objects.filter(pk=self.bodil.pk).exists())
        self.assertEqual(Position.objects.filter(pk__in=self.mine, person=self.bodil).count(), 3)
        self.assertEqual(EmailAddress.objects.filter(person=self.bodil).count(), 2)
        self.assertEqual(runs()[-1].changes["failed"], 1)

    def test_made_up_people_alone_when_asked(self):
        other = person("Other Example")
        member(other, self.root, end=ENDED)
        deleting.delete_people(TODAY, people=[other.pk])
        self.assertTrue(Person.objects.filter(pk=self.bodil.pk).exists())
        self.assertFalse(Person.objects.filter(pk=other.pk).exists())


# ---- The audit log ------------------------------------------------------------------------


class AuditLog(Left):
    def test_one_row_counts_what_went(self):
        deleting.delete_people(TODAY)
        [row] = runs()
        self.assertEqual((row.via, row.actor_account, row.actor_person, row.unit, row.unit_key, row.object_type, row.object_id), (CORE, None, None, None, "", "", None))
        self.assertEqual(row.changes, {
            "people": 1, "addresses": 2, "memberships": 1, "reasons": 1, "positions_kept": 3, "grants_given": 1, "seat_rules_made": 2, "failed": 0,
        })

    def test_a_night_with_nothing_to_delete_leaves_no_row(self):
        deleting.delete_people(YESTERDAY)
        self.assertEqual(runs(), [])
        deleting.delete_people(TODAY)
        deleting.delete_people(TODAY)
        self.assertEqual(len(runs()), 1)

    def test_no_row_leads_to_a_deleted_person_but_through_their_kept_positions(self):
        """Dana's ties are recorded through the actions, she acts as Robotics' vice chair,
        then everything of hers ends. Only rows about her positions still lead to her name,
        through those positions."""
        dana = person("Dana Example")
        membership = actions.add_membership(self.alva, person=dana, association=self.robotics, start=LONG_AGO, via=SERVER)
        appointed = actions.appoint(self.alva, person=dana, unit=self.robotics, role=Role.VICE_CHAIR, start=LONG_AGO, via=SERVER)
        reasoned = actions.add_reason(
            self.alva, person=dana, unit=self.robotics, kind=Reason.Kind.APPLICANT, legal_basis=Reason.Basis.CONSENT,
            source="Her application, from dana@example.org", start=LONG_AGO, expires=LATER, via=SERVER,
        )
        nils = person("Nils Example", account=True)
        member(nils, self.robotics)
        actions.appoint(dana, person=nils, unit=self.dev, role=Role.PARTICIPANT, via=SERVER)  # she acts
        actions.end_position(self.alva, position=appointed, day=ENDED, via=SERVER)
        actions.end_membership(self.alva, membership=membership, day=ENDED, via=SERVER)
        actions.end_reason(self.alva, reason=reasoned, day=ENDED, via=SERVER)
        dana_pk = dana.pk
        deleting.delete_people(TODAY)
        self.assertFalse(Person.objects.filter(pk=dana_pk).exists())

        shown = admin.site._registry[AuditEntry]
        kept = set(Position.objects.filter(kept_name="Dana Example").values_list("pk", flat=True))
        self.assertIn(appointed.pk, kept)  # and her seat leading Development, which has no rows of its own
        for row in AuditEntry.objects.all():
            with self.subTest(row=row.action, pk=row.pk):
                self.assertNotIn("Dana", str(shown.who(row)))
                about_her_position = row.object_type == "structure.position" and row.object_id in kept
                self.assertEqual("Dana" in str(shown.what(row)), about_her_position)
                self.assertNotIn("dana", json.dumps(row.changes).lower())
        acted = AuditEntry.objects.get(action="appoint", actor_person=dana_pk)
        self.assertEqual(str(shown.who(acted)), "someone since deleted")

    def test_a_merged_persons_rows_lead_nowhere_once_they_are_deleted(self):
        gone = person("Gone Example")
        acted = AuditEntry.record("change_unit", SERVER, person=gone.pk, unit=self.dev, obj=self.dev)
        Person.merge(self.bodil, gone)
        shown = admin.site._registry[AuditEntry]
        self.assertEqual(str(shown.who(acted)), "Bodil Example, merged since")
        deleting.delete_people(TODAY)
        self.assertEqual(str(shown.who(acted)), "someone since deleted")


# ---- The worker and the command ------------------------------------------------------------


class Worker(TestCase):
    def test_it_runs_once_the_day_has_changed(self):
        with mock.patch.object(sync, "delete_people") as job:
            self.assertEqual(sync.new_day(TODAY), TODAY)
            job.assert_not_called()
            self.assertEqual(sync.new_day(YESTERDAY), TODAY)
            job.assert_called_once_with(TODAY)

    def test_never_as_the_worker_starts(self):
        with mock.patch.object(sync, "delete_people") as job, mock.patch.dict("os.environ", {"SYNC_MODULES": ""}):
            call_command("sync_tools", "--once", stdout=io.StringIO())
        job.assert_not_called()

    def test_a_failed_run_doesnt_stop_the_worker(self):
        with mock.patch.object(sync, "delete_people", side_effect=RuntimeError("database is locked")), mock.patch.object(sync.logger, "exception") as logged:
            self.assertEqual(sync.new_day(YESTERDAY), TODAY)
        logged.assert_called_once()


class Command(Left):
    def test_a_dry_run_says_who_and_deletes_nobody(self):
        out = io.StringIO()
        call_command("delete_people", "--dry-run", stdout=out)
        self.assertIn("Bodil Example", out.getvalue())
        self.assertIn("1 ", out.getvalue())
        self.assertTrue(Person.objects.filter(pk=self.bodil.pk).exists())
        self.assertEqual(runs(), [])

    def test_by_hand_it_runs_and_is_logged_from_the_server(self):
        out = io.StringIO()
        call_command("delete_people", stdout=out)
        self.assertFalse(Person.objects.filter(pk=self.bodil.pk).exists())
        self.assertEqual([row.via for row in runs()], [SERVER])
        self.assertNotIn("Bodil", out.getvalue())


# ---- The admin page --------------------------------------------------------------------------


class AdminPage(Left):
    client_class = CaddyClient

    def test_it_shows_her_kept_positions_and_what_she_gave(self):
        deleting.delete_people(TODAY)
        operator, _ = make_user("operator@example.org", ["operator"], two_factor=True)
        self.client.force_login(operator)
        positions = self.client.get(reverse("admin:structure_position_changelist"))
        self.assertContains(positions, "Bodil Example")
        self.assertContains(self.client.get(reverse("admin:structure_position_change", args=[self.chair.pk])), "Bodil Example")
        self.assertContains(self.client.get(reverse("admin:structure_grant_change", args=[self.grant.pk])), "someone since deleted")
        self.assertContains(self.client.get(reverse("admin:structure_seatrule_change", args=[self.seats.pk])), "someone since deleted")
        log = self.client.get(reverse("admin:structure_auditentry_changelist"))
        self.assertContains(log, "delete_people")
        self.assertContains(log, "people: 1")


# ---- The made-up installations ---------------------------------------------------------------


class MadeUp(TestCase):
    def build(self, name, replace=False, root=None):
        root = root or Unit.objects.create(kind=ASSOCIATION, key="test", name="Test Association")
        examples.build(name, root, replace=replace)
        return root

    def test_example_federation_has_its_deleted_person(self):
        self.build("federation")
        self.assertFalse(Person.objects.filter(name="Bodil Example").exists())
        self.assertFalse(EmailAddress.objects.filter(address="bodil@example.org").exists())
        kept = Position.objects.get(kept_name="Bodil Example")
        self.assertEqual((kept.person_id, kept.unit.key, kept.role, kept.start, kept.end), (None, "uavs", Role.PARTICIPANT, date(2024, 1, 15), date(2025, 6, 1)))
        [row] = runs()
        self.assertEqual(row.via, SERVER)
        self.assertEqual((row.changes["people"], row.changes["memberships"], row.changes["positions_kept"]), (1, 1, 1))
        self.assertEqual(Person.objects.count(), len(examples.FEDERATION.people) - 1)  # everyone else is there

    def test_example_society_has_nobody_to_delete_until_ruts_reason_runs_out(self):
        self.build("society")
        self.assertEqual(runs(), [])
        self.assertEqual(list(deleting.due(TODAY)), [])
        self.assertEqual(list(deleting.due(date(2030, 7, 30))), [])
        self.assertEqual(list(deleting.due(date(2030, 7, 31)).values_list("name", flat=True)), ["Rut Example"])

    def test_example_federation_has_nobody_else_to_delete_later(self):
        self.build("federation")
        self.assertEqual(list(deleting.due(date(2031, 1, 1))), [])

    def test_replacing_one_removes_the_deleted_persons_record_too(self):
        root = self.build("federation")
        self.build("society", replace=True, root=root)
        self.assertFalse(Position.objects.filter(kept_name="Bodil Example").exists())
