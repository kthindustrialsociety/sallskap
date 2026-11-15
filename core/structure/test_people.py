"""Tests for people and ties (structure.md §2 and §3), written before the code. The
rules: merging only on a verified email, an unverified email never joining an existing
person, a non-member never holding a board role or lead, membership only of
associations, ending a tie never deleting it, and membership counting upwards."""

from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from accounts import sync
from accounts.tests import CaddyClient, make_user

from . import examples
from .models import EmailAddress, Membership, Person, Position, Reason, Unit

ASSOCIATION, TEAM = Unit.Kind.ASSOCIATION, Unit.Kind.TEAM
Role = Position.Role
User = get_user_model()
TODAY = timezone.localdate()
LONG_AGO = date(2020, 1, 1)


def unit(kind, key, parent=None, **fields):
    return Unit.objects.create(kind=kind, key=key, name=key.title(), parent=parent, **fields)


def person(name="Someone", *addresses, verified=False):
    p = Person.objects.create(name=name)
    for address in addresses:
        EmailAddress.objects.create(person=p, address=address, verified_at=timezone.now() if verified else None)
    return p


def member(p, association, start=LONG_AGO, **fields):
    return Membership.objects.create(person=p, association=association, start=start, **fields)


def position(p, where, role, start=LONG_AGO, **fields):
    return Position.objects.create(person=p, unit=where, role=role, start=start, **fields)


class Tree(TestCase):
    """The tree in structure.md §1, small: a root, two associations inside it, and teams."""

    def setUp(self):
        self.root = unit(ASSOCIATION, "example")
        self.robotics = unit(ASSOCIATION, "robotics", self.root)
        self.chemistry = unit(ASSOCIATION, "chemistry", self.root)
        self.development = unit(TEAM, "development", self.robotics)
        self.uavs = unit(TEAM, "uavs", self.development)
        self.website = unit(TEAM, "website", self.root)


# ---- People and their addresses ---------------------------------------------------


class Accounts(Tree):
    def test_a_new_account_gets_a_person_with_its_address_verified(self):
        user = User.objects.create_user("Ada@Example.org", name="Ada")
        p = user.person
        self.assertEqual(p.name, "Ada")
        address = p.emails.get()
        self.assertEqual(address.address, "ada@example.org")
        self.assertIsNotNone(address.verified_at)

    def test_a_person_needs_no_account(self):
        p = person("Bo", "bo@example.org")
        self.assertIsNone(p.user)

    def test_the_sub_is_still_the_accounts_id(self):
        user = User.objects.create_user("ada@example.org")
        self.assertEqual(sync.sub_of(user), str(user.pk))
        self.assertEqual(sync.sub_of(user.person.user), str(user.pk))

    def test_an_account_for_someone_already_known_joins_them(self):
        p = person("Bo", "bo@example.org", verified=True)
        member(p, self.root)
        user = User.objects.create_user("bo@example.org")
        p.refresh_from_db()
        self.assertEqual(p.user, user)
        self.assertEqual(Person.objects.count(), 1)
        user.refresh_from_db()
        self.assertEqual(user.name, "Bo")  # the account takes the person's name

    def test_an_account_never_joins_someone_on_an_unverified_address(self):
        p = person("Bo", "bo@example.org")
        user = User.objects.create_user("bo@example.org")
        self.assertNotEqual(user.person, p)
        self.assertEqual(Person.objects.count(), 2)

    def test_the_name_is_kept_in_step_both_ways(self):
        user = User.objects.create_user("ada@example.org", name="Ada")
        user.name = "Ada L."
        user.save()
        self.assertEqual(Person.objects.get(user=user).name, "Ada L.")
        p = user.person
        p.name = "Ada Lovelace"
        p.save()
        user.refresh_from_db()
        self.assertEqual(user.name, "Ada Lovelace")

    def test_changing_the_name_tells_the_tools(self):
        user = User.objects.create_user("ada@example.org", name="Ada")
        p = user.person
        p.name = "Ada L."
        p.save()
        self.assertTrue(user.__class__.objects.get(pk=user.pk).access_sync.pending)

    def test_a_changed_login_address_is_added_verified(self):
        user = User.objects.create_user("ada@example.org")
        user.email = "ada@example.net"
        user.save()
        addresses = dict(user.person.emails.values_list("address", "verified_at"))
        self.assertEqual(set(addresses), {"ada@example.org", "ada@example.net"})
        self.assertIsNotNone(addresses["ada@example.net"])

    def test_a_login_address_another_account_holds_is_refused(self):
        other = User.objects.create_user("bo@example.org")
        EmailAddress.objects.create(person=other.person, address="shared@example.org", verified_at=timezone.now())
        user = User(email="shared@example.org")
        with self.assertRaises(ValidationError):
            user.full_clean()

    def test_deleting_an_account_keeps_the_person(self):
        user = User.objects.create_user("ada@example.org")
        p = user.person
        member(p, self.root)
        user.delete()
        p.refresh_from_db()
        self.assertIsNone(p.user)

    def test_deleting_an_account_nothing_was_tied_to_deletes_the_person(self):
        user = User.objects.create_user("ada@example.org")
        pk = user.person.pk
        user.delete()
        self.assertFalse(Person.objects.filter(pk=pk).exists())

    def test_the_login_address_cannot_be_removed(self):
        user = User.objects.create_user("ada@example.org")
        with self.assertRaises(ValidationError):
            user.person.emails.get().delete()


class Addresses(Tree):
    def test_addresses_are_kept_in_lowercase(self):
        p = person("Bo", "Bo@Example.ORG")
        self.assertEqual(p.emails.get().address, "bo@example.org")

    def test_a_verified_address_belongs_to_one_person(self):
        person("Bo", "bo@example.org", verified=True)
        with self.assertRaises((ValidationError, IntegrityError)), transaction.atomic():
            person("Bo again", "bo@example.org", verified=True)

    def test_the_same_address_can_be_on_several_people_unverified(self):
        known = person("Bo", "bo@example.org", verified=True)
        signup = person("Bo", "bo@example.org")
        self.assertNotEqual(known, signup)
        self.assertEqual(EmailAddress.objects.filter(address="bo@example.org").count(), 2)

    def test_an_address_is_fixed_once_set(self):
        address = person("Bo", "bo@example.org").emails.get()
        address.address = "other@example.org"
        with self.assertRaises(ValidationError):
            address.save()


class Merging(Tree):
    def test_verifying_an_address_someone_holds_verified_makes_them_one(self):
        known = person("Bo", "bo@example.org", verified=True)
        member(known, self.robotics)
        signup = person("", "bo@example.org", "bo@example.net")
        Reason.objects.create(person=signup, kind=Reason.Kind.APPLICANT, unit=self.chemistry, legal_basis=Reason.Basis.CONSENT, expires=TODAY + timedelta(days=90))
        kept = signup.emails.get(address="bo@example.org").verify()
        self.assertEqual(kept, known)
        self.assertFalse(Person.objects.filter(pk=signup.pk).exists())
        self.assertEqual(set(known.emails.values_list("address", flat=True)), {"bo@example.org", "bo@example.net"})
        self.assertEqual(known.emails.get(address="bo@example.org").verified_at is not None, True)
        self.assertEqual(known.reasons.count(), 1)
        self.assertEqual(known.name, "Bo")

    def test_verifying_an_address_nobody_else_holds_merges_nothing(self):
        a = person("Bo", "bo@example.org")
        b = person("Bo", "bo@example.net", verified=True)
        self.assertEqual(a.emails.get().verify(), a)
        self.assertEqual(Person.objects.filter(pk__in=[a.pk, b.pk]).count(), 2)

    def test_two_unverified_rows_are_never_merged(self):
        person("Bo", "bo@example.org")
        person("Bo", "bo@example.org")
        self.assertEqual(Person.objects.count(), 2)

    def test_the_person_with_the_account_is_kept(self):
        known = person("Bo", "bo@example.org", verified=True)
        user = User.objects.create_user("bo@example.net", name="Bo")
        EmailAddress.objects.create(person=user.person, address="bo@example.org")
        kept = user.person.emails.get(address="bo@example.org").verify()
        self.assertEqual(kept, user.person)
        self.assertFalse(Person.objects.filter(pk=known.pk).exists())
        self.assertEqual(sync.sub_of(kept.user), str(user.pk))

    def test_two_people_with_accounts_are_never_merged(self):
        a = User.objects.create_user("a@example.org")
        b = User.objects.create_user("b@example.org")
        EmailAddress.objects.create(person=b.person, address="a@example.org")
        with self.assertRaises(ValidationError):
            b.person.emails.get(address="a@example.org").verify()
        self.assertEqual(Person.objects.count(), 2)
        self.assertIsNone(b.person.emails.get(address="a@example.org").verified_at)
        with self.assertRaises(ValidationError):
            Person.merge(a.person, b.person)

    def test_merging_by_hand_moves_every_tie(self):
        a = person("Bo", "bo@example.org", verified=True)
        b = person("Bo B.", "bo@example.net", verified=True)
        member(a, self.robotics)
        member(b, self.chemistry)
        position(b, self.website, Role.PARTICIPANT)
        kept = Person.merge(a, b)
        self.assertEqual(kept, a)
        self.assertEqual(set(a.member_of().values_list("key", flat=True)), {"example", "robotics", "chemistry"})
        self.assertEqual(a.positions.count(), 1)
        self.assertEqual(a.emails.filter(verified_at__isnull=False).count(), 2)

    def test_two_memberships_of_one_association_become_one(self):
        a = person("Bo")
        b = person("Bo")
        member(a, self.robotics, start=date(2024, 1, 1))
        member(b, self.robotics, start=date(2022, 1, 1))
        Person.merge(a, b)
        membership = a.memberships.get()
        self.assertEqual((membership.start, membership.end), (date(2022, 1, 1), None))


# ---- Ties -------------------------------------------------------------------------


class Ending(Tree):
    def test_ending_a_tie_sets_its_end_and_keeps_it(self):
        p = person()
        for tie in (member(p, self.root), position(p, self.website, Role.PARTICIPANT)):
            with self.subTest(tie=tie.__class__.__name__):
                tie.end_on()
                tie.refresh_from_db()
                self.assertEqual(tie.end, TODAY)
                self.assertFalse(tie.is_current())

    def test_a_tie_is_never_deleted(self):
        p = person()
        reason = Reason.objects.create(person=p, kind=Reason.Kind.ALUMNUS, unit=self.root, legal_basis=Reason.Basis.CONSENT, expires=TODAY + timedelta(days=900))
        for tie in (member(p, self.root), position(p, self.website, Role.PARTICIPANT), reason):
            with self.subTest(tie=tie.__class__.__name__):
                with self.assertRaises(ValidationError):
                    tie.delete()
                with self.assertRaises(ValidationError):
                    tie.__class__.objects.filter(pk=tie.pk).delete()
                self.assertTrue(tie.__class__.objects.filter(pk=tie.pk).exists())

    def test_a_tie_ends_after_it_starts(self):
        with self.assertRaises(ValidationError):
            member(person(), self.root, start=date(2025, 1, 1), end=date(2024, 1, 1))

    def test_what_is_current(self):
        p = person()
        future = member(p, self.robotics, start=TODAY + timedelta(days=30))
        ended = member(p, self.chemistry, start=LONG_AGO, end=TODAY)
        today = member(p, self.root, start=TODAY)
        self.assertFalse(future.is_current())
        self.assertFalse(ended.is_current())
        self.assertTrue(today.is_current())
        self.assertEqual(list(Membership.objects.current()), [today])
        self.assertTrue(future.is_current(on=TODAY + timedelta(days=30)))

    def test_a_person_and_unit_are_fixed_once_set(self):
        tie = member(person(), self.robotics)
        tie.association = self.chemistry
        with self.assertRaises(ValidationError):
            tie.save()

    def test_a_unit_anyone_was_ever_tied_to_cannot_be_deleted(self):
        team = unit(TEAM, "hackathon", self.root)
        position(person(), team, Role.PARTICIPANT, end=date(2021, 1, 1))
        self.assertIsNotNone(team.deletion_problem())
        with self.assertRaises(ValidationError):
            team.delete()


class Memberships(Tree):
    def test_membership_is_only_of_associations(self):
        with self.assertRaises(ValidationError):
            member(person(), self.website)

    def test_kinds(self):
        self.assertEqual(member(person(), self.root, kind=Membership.Kind.HONORARY).kind, "honorary")
        self.assertEqual(member(person(), self.root).kind, "ordinary")

    def test_one_current_membership_of_an_association_at_a_time(self):
        p = person()
        first = member(p, self.robotics)
        with self.assertRaises(ValidationError):
            member(p, self.robotics, start=date(2024, 1, 1))
        first.end_on(date(2025, 1, 1))
        member(p, self.robotics, start=date(2025, 1, 1))  # a new one after the last ends
        with self.assertRaises(ValidationError):
            member(p, self.robotics, start=date(2023, 1, 1), end=date(2026, 1, 1))  # overlapping an old one

    def test_membership_counts_upwards(self):
        p = person()
        member(p, self.robotics)
        self.assertEqual(list(p.member_of().values_list("key", flat=True)), ["example", "robotics"])
        self.assertTrue(p.is_member(self.root))
        self.assertTrue(p.is_member(self.robotics))
        self.assertFalse(p.is_member(self.chemistry))

    def test_a_direct_member_of_the_root_is_a_member_of_it_alone(self):
        p = person()
        member(p, self.root)
        self.assertEqual(list(p.member_of().values_list("key", flat=True)), ["example"])
        self.assertFalse(p.is_member(self.robotics))

    def test_a_member_of_both_is_counted_once(self):
        p = person()
        member(p, self.robotics)
        member(p, self.chemistry)
        member(p, self.root)
        self.assertEqual(sorted(p.member_of().values_list("key", flat=True)), ["chemistry", "example", "robotics"])

    def test_an_ended_membership_counts_for_nothing(self):
        p = person()
        member(p, self.robotics, end=date(2025, 1, 1))
        self.assertFalse(p.member_of().exists())
        self.assertTrue(p.is_member(self.root, on=date(2024, 6, 1)))

    def test_moving_an_association_moves_what_its_members_belong_to(self):
        inner = unit(ASSOCIATION, "inner", self.chemistry)
        p = person()
        member(p, inner)
        self.assertTrue(p.is_member(self.chemistry))
        inner.parent = self.robotics
        inner.save()
        self.assertFalse(p.is_member(self.chemistry))
        self.assertTrue(p.is_member(self.robotics))

    def test_a_membership_a_board_role_rests_on_cannot_end(self):
        p = person()
        membership = member(p, self.robotics)
        treasurer = position(p, self.robotics, Role.TREASURER)
        with self.assertRaises(ValidationError):
            membership.end_on()
        treasurer.end_on()
        membership.end_on()  # handed over first, so it can

    def test_a_membership_a_lead_rests_on_cannot_end(self):
        p = person()
        membership = member(p, self.robotics)
        position(p, self.uavs, Role.LEAD)
        with self.assertRaises(ValidationError):
            membership.end_on()

    def test_another_membership_can_carry_the_position(self):
        p = person()
        direct = member(p, self.root)
        member(p, self.robotics)
        position(p, self.root, Role.CHAIR)
        direct.end_on()  # still a member of the root, through Robotics

    def test_a_participant_needs_no_membership(self):
        p = person()
        membership = member(p, self.robotics)
        position(p, self.uavs, Role.PARTICIPANT)
        membership.end_on()


class Positions(Tree):
    def test_board_roles_sit_in_associations(self):
        p = person()
        member(p, self.root)
        for role in Position.BOARD_ROLES:
            with self.subTest(role=role), self.assertRaises(ValidationError):
                position(p, self.website, role)

    def test_leads_and_participants_sit_in_teams(self):
        p = person()
        member(p, self.root)
        for role in (Role.LEAD, Role.PARTICIPANT):
            with self.subTest(role=role), self.assertRaises(ValidationError):
                position(p, self.root, role)

    def test_a_non_member_can_take_part_in_a_team(self):
        outsider = person("Partner", "someone@example.net")
        self.assertEqual(position(outsider, self.uavs, Role.PARTICIPANT, title="Advisor").title, "Advisor")

    def test_a_non_member_never_holds_a_board_role(self):
        outsider = person()
        for role in Position.BOARD_ROLES:
            with self.subTest(role=role), self.assertRaises(ValidationError):
                position(outsider, self.root, role)

    def test_a_non_member_is_never_a_lead(self):
        with self.assertRaises(ValidationError):
            position(person(), self.website, Role.LEAD)

    def test_a_board_role_needs_a_membership_of_that_association(self):
        p = person()
        member(p, self.chemistry)
        with self.assertRaises(ValidationError):
            position(p, self.robotics, Role.TREASURER)
        position(p, self.chemistry, Role.TREASURER)
        position(p, self.root, Role.BOARD_MEMBER)  # a member of the root, through Chemistry

    def test_a_lead_needs_a_membership_of_the_association_owning_the_team(self):
        robotics_member, chemistry_member = person(), person()
        member(robotics_member, self.robotics)
        member(chemistry_member, self.chemistry)
        position(robotics_member, self.uavs, Role.LEAD)
        with self.assertRaises(ValidationError):
            position(chemistry_member, self.uavs, Role.LEAD)
        position(chemistry_member, self.website, Role.LEAD)  # owned by the root, which Chemistry is inside

    def test_or_of_an_association_taking_part(self):
        hackathon = unit(TEAM, "hackathon", self.robotics)
        hackathon.taking_part.add(self.chemistry)
        p = person()
        member(p, self.chemistry)
        position(p, hackathon, Role.LEAD)

    def test_the_membership_must_hold_when_the_position_starts(self):
        p = person()
        member(p, self.robotics, start=date(2026, 1, 1))
        with self.assertRaises(ValidationError):
            position(p, self.robotics, Role.SECRETARY, start=date(2025, 1, 1))

    def test_a_new_chair_ends_the_last_one(self):
        old, new = person("Old"), person("New")
        member(old, self.robotics)
        member(new, self.robotics)
        before = position(old, self.robotics, Role.CHAIR)
        position(new, self.robotics, Role.CHAIR, start=date(2026, 7, 1))
        before.refresh_from_db()
        self.assertEqual(before.end, date(2026, 7, 1))
        self.assertEqual(Position.objects.filter(unit=self.robotics, role=Role.CHAIR).count(), 2)

    def test_a_chair_cannot_start_before_the_current_one(self):
        old, new = person("Old"), person("New")
        member(old, self.robotics)
        member(new, self.robotics)
        position(old, self.robotics, Role.CHAIR, start=date(2026, 1, 1))
        with self.assertRaises(ValidationError):
            position(new, self.robotics, Role.CHAIR, start=date(2025, 1, 1))

    def test_other_roles_can_have_several_holders(self):
        for role in (Role.VICE_CHAIR, Role.BOARD_MEMBER, Role.TREASURER):
            with self.subTest(role=role):
                for _ in range(2):
                    p = person()
                    member(p, self.robotics)
                    position(p, self.robotics, role)
        for _ in range(2):
            p = person()
            member(p, self.robotics)
            position(p, self.uavs, Role.LEAD)
        self.assertEqual(Position.objects.current().filter(unit=self.uavs, role=Role.LEAD).count(), 2)

    def test_nobody_is_added_to_an_archived_team(self):
        project = unit(TEAM, "hackathon", self.root)
        held = position(person(), project, Role.PARTICIPANT)
        project.archive()
        with self.assertRaises(ValidationError):
            position(person(), project, Role.PARTICIPANT)
        held.end_on()  # ending one there still works

    def test_the_role_is_fixed_once_set(self):
        p = person()
        member(p, self.robotics)
        held = position(p, self.robotics, Role.BOARD_MEMBER)
        held.role = Role.CHAIR
        with self.assertRaises(ValidationError):
            held.save()


class Reasons(Tree):
    def make(self, **fields):
        values = {"person": person(), "kind": Reason.Kind.ALUMNUS, "unit": self.root, "legal_basis": Reason.Basis.CONSENT, "expires": TODAY + timedelta(days=900)}
        values.update(fields)
        return Reason.objects.create(**values)

    def test_a_reason_always_expires(self):
        with self.assertRaises((ValidationError, IntegrityError)), transaction.atomic():
            self.make(expires=None)

    def test_kinds_and_legal_bases(self):
        self.assertEqual(set(Reason.Kind.values), {"applicant", "alumnus", "partner_contact", "contact_form"})
        self.assertEqual(set(Reason.Basis.values), {"consent", "contract", "legitimate_interest", "legal_obligation"})
        with self.assertRaises(ValidationError):
            self.make(kind="friend")

    def test_ending_a_reason_sets_it_to_expire_today(self):
        reason = self.make()
        self.assertTrue(reason.is_current())
        reason.end_on()
        reason.refresh_from_db()
        self.assertEqual(reason.expires, TODAY)
        self.assertFalse(reason.is_current())


# ---- The migration of today's accounts ----------------------------------------------


class MigratingAccounts(TransactionTestCase):
    """Every account becomes a person with that account, the same sub and its address
    verified. Nothing in accounts changes."""

    accounts = ("accounts", "0003_access_sync_follow_up")
    before = [("structure", "0001_initial"), accounts]
    after = [("structure", "0002_people"), accounts]

    def tearDown(self):
        MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())

    def test_every_account_becomes_a_person(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.before)
        old = executor.loader.project_state(self.before).apps
        OldUser = old.get_model("accounts", "User")
        rows = [OldUser.objects.create(email=f"user{i}@example.org", name=f"User {i}", password="!") for i in range(3)]
        rows.append(OldUser.objects.create(email="noname@example.org", password="!"))
        accounts_before = list(OldUser.objects.order_by("pk").values())

        executor = MigrationExecutor(connection)
        executor.migrate(self.after)
        new = executor.loader.project_state(self.after).apps
        NewPerson = new.get_model("structure", "Person")
        NewUser = new.get_model("accounts", "User")
        self.assertEqual(list(NewUser.objects.order_by("pk").values()), accounts_before)  # untouched, so every sub is too
        self.assertEqual(NewPerson.objects.count(), len(rows))
        for row in rows:
            p = NewPerson.objects.get(user_id=row.pk)
            self.assertEqual(p.name, row.name)
            address = p.emails.get()
            self.assertEqual(address.address, row.email)
            self.assertIsNotNone(address.verified_at)

    def test_it_goes_back(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.after)
        executor = MigrationExecutor(connection)
        executor.migrate(self.before)


# ---- The two made-up installations ----------------------------------------------------


class Examples(TestCase):
    def build(self, name):
        spec = examples.EXAMPLES[name]
        root = Unit.objects.create(kind=ASSOCIATION, key=spec.root_key, name=spec.root_name)
        examples.build(name, root)
        return root

    def test_both_build_and_follow_every_rule(self):
        for name in examples.EXAMPLES:
            with self.subTest(name=name), transaction.atomic():
                self.build(name)
                for model in (Person, EmailAddress, Membership, Position, Reason):
                    for row in model.objects.all():
                        row.full_clean()
                transaction.set_rollback(True)

    def test_example_society_has_what_the_plan_lists(self):
        root = self.build("society")
        current = Position.objects.current()
        for role in (Role.CHAIR, Role.VICE_CHAIR, Role.TREASURER, Role.SECRETARY):
            self.assertEqual(current.filter(unit=root, role=role).count(), 1, role)
        self.assertEqual(current.filter(unit=root, role=Role.BOARD_MEMBER).count(), 2)
        for team in Unit.objects.filter(kind=TEAM, archived_at__isnull=True):
            self.assertTrue(current.filter(unit=team, role=Role.LEAD).exists(), team)
        archived = Unit.objects.get(archived_at__isnull=False)
        self.assertTrue(Position.objects.filter(unit=archived, end__isnull=False).exists())  # its history
        participants = current.filter(role=Role.PARTICIPANT)
        self.assertGreater(participants.count(), 2)
        self.assertTrue(any(not p.person.member_of().exists() for p in participants))  # a non-member
        self.assertTrue(Membership.objects.current().filter(kind=Membership.Kind.HONORARY).exists())
        lapsed = Membership.objects.filter(end__isnull=False)
        self.assertTrue(lapsed.exists())
        self.assertTrue(Reason.objects.filter(kind=Reason.Kind.ALUMNUS, person__memberships__in=lapsed).exists())
        self.assertTrue(Person.objects.filter(user=None, memberships__end=None).exists())  # a member with no account
        self.assertEqual(set(Membership.objects.values_list("association__key", flat=True)), {root.key})

    def test_example_federation_has_what_the_plan_lists(self):
        root = self.build("federation")
        robotics = Unit.objects.get(key="robotics")
        chairs = Position.objects.filter(unit=root, role=Role.CHAIR).order_by("start")
        self.assertEqual(chairs.count(), 2)  # an ended chair and their successor
        self.assertEqual(chairs[0].end, chairs[1].start)
        self.assertIsNone(chairs[1].end)
        two = [p for p in Person.objects.all() if len({pos.unit.association().key for pos in p.positions.current()} - {root.key}) == 2]
        self.assertTrue(two)  # positions in two associations
        direct = Person.objects.filter(memberships__association=root, memberships__end=None)
        self.assertTrue(any(list(p.member_of()) == [root] for p in direct))
        robotics_member = Membership.objects.current().filter(association=robotics).first().person
        self.assertTrue(robotics_member.is_member(root))
        advisory = Unit.objects.get(key="advisory-board")
        advisors = Position.objects.current().filter(unit=advisory, role=Role.PARTICIPANT)
        self.assertEqual(advisors.count(), 2)
        self.assertTrue(all(a.title == "Advisor" and not a.person.member_of().exists() for a in advisors))
        self.assertTrue(Reason.objects.filter(kind=Reason.Kind.ALUMNUS, person__in=[a.person for a in advisors]).exists())

    def test_replace_removes_the_example_people_and_keeps_everyone_else(self):
        root = self.build("federation")
        operator = User.objects.create_user("operator@example.com")
        examples.build("society", root, replace=True)
        self.assertTrue(Person.objects.filter(user=operator).exists())
        self.assertFalse(Person.objects.filter(emails__address__endswith="@example.net", positions__unit__key="advisory-board").exists())
        self.assertFalse(Unit.objects.filter(key="robotics").exists())
        self.assertEqual(set(Membership.objects.values_list("association__key", flat=True)), {root.key})


# ---- The admin page -------------------------------------------------------------------


class AdminPage(Tree):
    client_class = CaddyClient

    def enter(self, role):
        user, _ = make_user(f"{role}@example.org", [role], two_factor=True)
        self.client.force_login(user)
        return user

    def test_an_operator_adds_a_person_and_a_membership(self):
        self.enter("operator")
        response = self.client.post(reverse("admin:structure_person_add"), {
            "name": "Bo",
            "emails-TOTAL_FORMS": "1", "emails-INITIAL_FORMS": "0", "emails-0-address": "bo@example.org",
        })
        self.assertEqual(response.status_code, 302, response.content.decode()[:3000])
        p = Person.objects.get(name="Bo")
        self.assertIsNone(p.emails.get().verified_at)
        response = self.client.post(reverse("admin:structure_membership_add"), {
            "person": p.pk, "association": self.robotics.pk, "kind": "ordinary", "start": "2026-01-01", "end": "",
        })
        self.assertEqual(response.status_code, 302, response.content.decode()[:3000])
        self.assertTrue(p.is_member(self.root))

    def test_the_form_shows_a_broken_rule(self):
        self.enter("operator")
        p = person("Bo")
        response = self.client.post(reverse("admin:structure_position_add"), {
            "person": p.pk, "unit": self.robotics.pk, "role": "chair", "title": "", "start": "2026-01-01", "end": "", "appointed_by": "", "minutes": "",
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Position.objects.exists())

    def test_an_operator_ends_a_tie_and_cannot_delete_it(self):
        self.enter("operator")
        membership = member(person(), self.robotics)
        changelist = reverse("admin:structure_membership_changelist")
        self.client.post(changelist, {"action": "end_today", "_selected_action": [membership.pk]})
        membership.refresh_from_db()
        self.assertEqual(membership.end, TODAY)
        response = self.client.post(reverse("admin:structure_membership_delete", args=[membership.pk]), {"post": "yes"})
        self.assertEqual(response.status_code, 403)

    def test_an_operator_merges_two_people(self):
        self.enter("operator")
        a, b = person("Bo"), person("Bo")
        member(b, self.robotics)
        self.client.post(reverse("admin:structure_person_changelist"), {"action": "merge", "_selected_action": [a.pk, b.pk]})
        self.assertEqual(Person.objects.filter(name="Bo").count(), 1)
        self.assertTrue(Person.objects.get(name="Bo").is_member(self.robotics))

    def test_board_sees_people_but_cannot_change_them(self):
        self.enter("board")
        self.assertEqual(self.client.get(reverse("admin:structure_person_changelist")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:structure_position_changelist")).status_code, 200)
        response = self.client.post(reverse("admin:structure_person_add"), {"name": "Bo", "emails-TOTAL_FORMS": "0", "emails-INITIAL_FORMS": "0"})
        self.assertEqual(response.status_code, 403)

    def test_a_member_sees_nothing(self):
        user = User.objects.create_user("member@example.org")
        self.client.force_login(user)
        self.assertNotEqual(self.client.get(reverse("admin:structure_person_changelist")).status_code, 200)
