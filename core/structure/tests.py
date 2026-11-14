"""Tests for units (structure.md §1), written before the code, with the two made-up
installations from examples.py. The README shows how to run them in Docker."""

from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.tests import CaddyClient

from . import examples
from .models import Unit, ensure_root

ASSOCIATION, TEAM = Unit.Kind.ASSOCIATION, Unit.Kind.TEAM


def association(key, parent=None, **fields):
    return Unit.objects.create(kind=ASSOCIATION, key=key, name=key.title(), parent=parent, **fields)


def team(key, parent, **fields):
    return Unit.objects.create(kind=TEAM, key=key, name=key.title(), parent=parent, **fields)


class Tree(TestCase):
    """A small federation: root, an association inside it, and teams down three levels."""

    def setUp(self):
        self.root = association("example")
        self.robotics = association("robotics", self.root)
        self.development = team("development", self.robotics)
        self.uavs = team("uavs", self.development)
        self.race = team("drone-race-2027", self.uavs, end_date=date(2027, 6, 1))

    def refresh(self, *units):
        for unit in units:
            unit.refresh_from_db()


class Keys(Tree):
    def test_good_keys_are_accepted(self):
        for key in ("a1", "chemistry", "lab-nights-2027", "x" * 40):
            team(key, self.root)

    def test_bad_keys_are_refused(self):
        for key in ("", "Robotics", "1robotics", "-robotics", "robotics-", "drone--race", "x" * 41, "rob_otics", "rob.otics", "räven", "a b"):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                team(key, self.root)

    def test_custom_is_reserved(self):
        # Tools already see groups made by hand as custom:<name>, so a unit called
        # custom would give keys such as custom:lead that look like those.
        with self.assertRaises(ValidationError):
            team("custom", self.root)

    def test_a_key_cannot_change_once_set(self):
        self.uavs.key = "drones"
        with self.assertRaises(ValidationError):
            self.uavs.save()
        self.refresh(self.uavs)
        self.assertEqual(self.uavs.key, "uavs")

    def test_renaming_keeps_the_key(self):
        self.uavs.name = "Drones"
        self.uavs.save()
        self.refresh(self.uavs)
        self.assertEqual((self.uavs.key, self.uavs.name), ("uavs", "Drones"))

    def test_keys_are_unique(self):
        with self.assertRaises(ValidationError):
            team("uavs", self.robotics)

    def test_an_archived_unit_keeps_its_key(self):
        self.race.archive()
        with self.assertRaises(ValidationError):
            team("drone-race-2027", self.robotics)

    def test_the_database_refuses_a_bad_key_too(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Unit.objects.filter(pk=self.uavs.pk).update(key="Not-A-Key")


class Root(Tree):
    def test_there_is_only_one_root(self):
        with self.assertRaises(ValidationError):
            association("another")

    def test_the_database_allows_only_one_root(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Unit.objects.filter(pk=self.robotics.pk).update(parent=None)


    def test_the_root_cannot_be_moved(self):
        self.root.parent = self.robotics
        with self.assertRaises(ValidationError):
            self.root.save()

    def test_a_unit_cannot_become_a_second_root(self):
        self.robotics.parent = None
        with self.assertRaises(ValidationError):
            self.robotics.save()

    def test_the_root_cannot_be_deleted(self):
        for unit in (self.race, self.uavs, self.development, self.robotics):
            unit.delete()
        with self.assertRaises(ValidationError):
            self.root.delete()
        self.assertTrue(Unit.objects.filter(pk=self.root.pk).exists())

    def test_the_root_cannot_be_archived(self):
        with self.assertRaises(ValidationError):
            self.root.archive()

    def test_root_finds_it(self):
        self.assertEqual(Unit.objects.root(), self.root)


class RootFromConfig(TestCase):
    """core gets the root's name and key from the config, through the file kit writes."""

    @override_settings(SALLSKAP_ROOT={"name": "Example Society", "key": "example"})
    def test_the_root_is_created_from_the_config(self):
        ensure_root()
        root = Unit.objects.root()
        self.assertEqual((root.kind, root.key, root.name, root.parent), (ASSOCIATION, "example", "Example Society", None))

    @override_settings(SALLSKAP_ROOT={"name": "Example Society", "key": "example"})
    def test_running_again_changes_nothing(self):
        ensure_root()
        ensure_root()
        self.assertEqual(Unit.objects.count(), 1)

    def test_the_root_follows_the_configs_name(self):
        with self.settings(SALLSKAP_ROOT={"name": "Example Society", "key": "example"}):
            ensure_root()
        with self.settings(SALLSKAP_ROOT={"name": "Example Society, renamed", "key": "example"}):
            ensure_root()
        self.assertEqual(Unit.objects.root().name, "Example Society, renamed")

    def test_a_changed_key_in_the_config_stops_core(self):
        with self.settings(SALLSKAP_ROOT={"name": "Example Society", "key": "example"}):
            ensure_root()
        with self.settings(SALLSKAP_ROOT={"name": "Example Society", "key": "renamed"}):
            with self.assertRaises(ImproperlyConfigured):
                ensure_root()
        self.assertEqual(Unit.objects.root().key, "example")

    def test_a_config_key_used_by_another_unit_stops_core(self):
        with self.settings(SALLSKAP_ROOT={"name": "Example", "key": "example"}):
            ensure_root()
        team("website", Unit.objects.root())
        with self.settings(SALLSKAP_ROOT={"name": "Example", "key": "website"}):
            with self.assertRaises(ImproperlyConfigured):
                ensure_root()

    @override_settings(SALLSKAP_ROOT={"name": "Example", "key": "Not a key"})
    def test_a_bad_key_in_the_config_stops_core(self):
        with self.assertRaises(ImproperlyConfigured):
            ensure_root()
        self.assertFalse(Unit.objects.exists())

    def test_the_root_is_an_association(self):
        with self.assertRaises(ValidationError):
            Unit.objects.create(kind=TEAM, key="lonely", name="Lonely")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Unit.objects.bulk_create([Unit(kind=TEAM, key="lonely", name="Lonely", path="/lonely/")])

    @override_settings(SALLSKAP_ROOT={"name": "Example", "key": "example"})
    def test_migrating_back_to_before_units_skips_the_root(self):
        class Before:
            def get_model(self, app, model):
                raise LookupError(model)

        ensure_root(apps=Before())
        self.assertFalse(Unit.objects.exists())

    @override_settings(SALLSKAP_ROOT=None)
    def test_without_a_config_there_is_no_root(self):
        ensure_root()
        self.assertFalse(Unit.objects.exists())


class Shape(Tree):
    def test_teams_hold_teams(self):
        team("pr", self.development)

    def test_teams_do_not_hold_associations(self):
        with self.assertRaises(ValidationError):
            association("chemistry", self.development)

    def test_associations_hold_associations(self):
        association("chemistry", self.robotics)

    def test_a_unit_cannot_be_its_own_parent(self):
        self.uavs.parent = self.uavs
        with self.assertRaises(ValidationError):
            self.uavs.save()

    def test_no_cycles(self):
        self.development.parent = self.race
        with self.assertRaises(ValidationError):
            self.development.save()
        self.refresh(self.development)
        self.assertEqual(self.development.parent, self.robotics)

    def test_moving_carries_everything_under_it(self):
        operations = team("operations", self.robotics)
        self.uavs.parent = operations
        self.uavs.save()
        self.refresh(self.race)
        self.assertEqual(list(self.race.ancestors()), [self.root, self.robotics, operations, self.uavs])
        self.assertEqual(set(operations.descendants()), {self.uavs, self.race})

    def test_an_association_cannot_move_under_a_team(self):
        chemistry = association("chemistry", self.root)
        chemistry.parent = self.development
        with self.assertRaises(ValidationError):
            chemistry.save()

    def test_an_association_holding_associations_cannot_become_a_team(self):
        association("chemistry", self.robotics)
        self.robotics.kind = TEAM
        with self.assertRaises(ValidationError):
            self.robotics.save()

    def test_a_team_under_a_team_cannot_become_an_association(self):
        self.uavs.kind = ASSOCIATION
        with self.assertRaises(ValidationError):
            self.uavs.save()

    def test_a_team_directly_under_an_association_can_become_one(self):
        self.development.kind = ASSOCIATION  # the teams under it can stay
        self.development.save()
        self.refresh(self.development)
        self.assertEqual(self.development.kind, ASSOCIATION)

    def test_an_archived_team_or_a_project_cannot_become_an_association(self):
        operations = team("operations", self.robotics, end_date=date(2027, 1, 1))
        operations.kind = ASSOCIATION
        with self.assertRaises(ValidationError):
            operations.save()

    def test_only_teams_have_an_end_date(self):
        with self.assertRaises(ValidationError):
            association("chemistry", self.root, end_date=date(2027, 1, 1))

    def test_colours_are_hex(self):
        team("pr", self.robotics, primary_colour="#1a5fb4", accent_colour="#FFAA00")
        with self.assertRaises(ValidationError):
            team("web", self.robotics, primary_colour="blue")


class Archiving(Tree):
    def test_archiving_a_project_archives_everything_under_it(self):
        sub = team("timing", self.race)
        subsub = team("timing-hardware", sub)
        self.race.archive()
        self.refresh(self.race, sub, subsub, self.uavs)
        self.assertTrue(all(unit.archived for unit in (self.race, sub, subsub)))
        self.assertFalse(self.uavs.archived)

    def test_archiving_a_service_archives_its_projects_too(self):
        self.development.archive()
        self.refresh(self.uavs, self.race)
        self.assertTrue(self.uavs.archived and self.race.archived)

    def test_archiving_needs_no_end_date_to_have_passed(self):
        self.race.archive()  # ends 2027, and archiving is done by hand when a project is over
        self.refresh(self.race)
        self.assertTrue(self.race.archived)

    def test_associations_cannot_be_archived(self):
        with self.assertRaises(ValidationError):
            self.robotics.archive()

    def test_nothing_new_under_an_archived_unit(self):
        self.race.archive()
        with self.assertRaises(ValidationError):
            team("late", self.race)

    def test_nothing_moves_under_an_archived_unit(self):
        self.race.archive()
        operations = team("operations", self.robotics)
        operations.parent = self.race
        with self.assertRaises(ValidationError):
            operations.save()

    def test_taking_out_of_the_archive_brings_back_that_team_alone(self):
        self.uavs.archive()
        self.uavs.unarchive()
        self.refresh(self.uavs, self.race)
        self.assertFalse(self.uavs.archived)
        self.assertTrue(self.race.archived)

    def test_a_team_under_an_archived_one_stays_archived(self):
        self.uavs.archive()
        with self.assertRaises(ValidationError):
            self.race.unarchive()
        self.refresh(self.race)
        self.assertTrue(self.race.archived)

    def test_archiving_twice_keeps_the_first_date(self):
        self.race.archive()
        self.refresh(self.race)
        first = self.race.archived_at
        self.uavs.archive()
        self.refresh(self.race)
        self.assertEqual(self.race.archived_at, first)


class Deleting(Tree):
    def test_a_unit_with_nothing_under_it_can_be_deleted(self):
        self.race.delete()
        self.assertFalse(Unit.objects.filter(key="drone-race-2027").exists())

    def test_a_unit_with_units_under_it_cannot(self):
        with self.assertRaises(ValidationError):
            self.uavs.delete()
        self.assertTrue(Unit.objects.filter(pk=self.uavs.pk).exists())

    def test_nor_can_a_bulk_delete_take_it(self):
        with self.assertRaises(ValidationError):
            Unit.objects.filter(pk=self.uavs.pk).delete()
        self.assertTrue(Unit.objects.filter(pk=self.uavs.pk).exists())

    def test_an_association_taking_part_somewhere_cannot(self):
        chemistry = association("chemistry", self.root)
        self.development.taking_part.add(chemistry)
        with self.assertRaises(ValidationError):
            chemistry.delete()

    def test_an_association_a_service_is_run_for_cannot(self):
        chemistry = association("chemistry", self.root)
        team("chemistry-website", self.robotics, run_for=chemistry)
        with self.assertRaises(ValidationError):
            chemistry.delete()


class TakingPart(Tree):
    def setUp(self):
        super().setUp()
        self.chemistry = association("chemistry", self.root)

    def test_a_team_can_involve_other_associations(self):
        self.development.taking_part.add(self.chemistry)
        self.assertEqual(list(self.chemistry.teams_taking_part.all()), [self.development])

    def test_only_associations_take_part(self):
        with self.assertRaises(ValidationError):
            self.development.taking_part.add(team("web", self.chemistry))

    def test_the_owner_is_not_listed_as_taking_part(self):
        for owner in (self.robotics, self.root):
            with self.subTest(owner=owner.key), self.assertRaises(ValidationError), transaction.atomic():
                self.uavs.taking_part.add(owner)

    def test_only_teams_involve_associations(self):
        with self.assertRaises(ValidationError):
            self.robotics.taking_part.add(self.chemistry)


class RunFor(Tree):
    def setUp(self):
        super().setUp()
        self.chemistry = association("chemistry", self.root)

    def test_a_service_can_be_run_for_another_association(self):
        website = team("chemistry-website", self.robotics, run_for=self.chemistry)
        self.assertEqual(list(self.chemistry.run_by.all()), [website])

    def test_it_is_run_for_an_association(self):
        with self.assertRaises(ValidationError):
            team("web", self.robotics, run_for=team("lab", self.chemistry))

    def test_not_for_its_own_association_or_one_above(self):
        for target in (self.robotics, self.root):
            with self.subTest(target=target.key), self.assertRaises(ValidationError):
                team(f"web-{target.key}", self.development, run_for=target)

    def test_only_teams_are_run_for_others(self):
        with self.assertRaises(ValidationError):
            association("physics", self.root, run_for=self.chemistry)

    def test_moving_cannot_put_a_service_inside_the_association_it_is_run_for(self):
        team("chemistry-website", self.robotics, run_for=self.chemistry)
        self.robotics.parent = self.chemistry
        with self.assertRaises(ValidationError):
            self.robotics.save()

    def test_moving_cannot_put_a_team_inside_an_association_taking_part(self):
        self.development.taking_part.add(self.chemistry)
        operations = team("operations", self.chemistry)
        self.development.parent = operations
        with self.assertRaises(ValidationError):
            self.development.save()

    def test_ending_the_arrangement(self):
        website = team("chemistry-website", self.robotics, run_for=self.chemistry)
        website.run_for = None
        website.save()
        self.assertFalse(self.chemistry.run_by.exists())


class Examples(TestCase):
    """The two made-up installations, as every later segment uses them."""

    def build(self, name):
        spec = examples.EXAMPLES[name]
        root = Unit.objects.create(kind=ASSOCIATION, key=spec.root_key, name=spec.root_name)
        examples.build(name, root)
        return root

    def test_both_build_and_follow_every_rule(self):
        for name in examples.EXAMPLES:
            with self.subTest(name=name):
                with transaction.atomic():
                    root = self.build(name)
                    for unit in Unit.objects.all():
                        unit.full_clean()
                    self.assertEqual(Unit.objects.root(), root)
                    transaction.set_rollback(True)

    def test_example_society_is_one_association(self):
        self.build("society")
        self.assertEqual(Unit.objects.filter(kind=ASSOCIATION).count(), 1)
        self.assertTrue(Unit.objects.filter(end_date__isnull=False, archived_at__isnull=True).exists())  # a project
        self.assertTrue(Unit.objects.filter(end_date__isnull=False, archived_at__isnull=False).exists())  # last year's
        self.assertTrue(Unit.objects.filter(kind=TEAM, parent__kind=TEAM).exists())  # a team in a team
        self.assertFalse(Unit.objects.filter(run_for__isnull=False).exists())
        self.assertFalse(Unit.objects.filter(taking_part__isnull=False).exists())

    def test_example_federation_is_the_tree_in_structure_md(self):
        root = self.build("federation")
        self.assertEqual(Unit.objects.filter(parent=root, kind=ASSOCIATION).count(), 2)
        race = Unit.objects.get(key="drone-race-2027")
        self.assertEqual([u.key for u in race.ancestors()], [root.key, "robotics", "development", "uavs"])
        self.assertTrue(Unit.objects.filter(run_for__isnull=False).exists())
        self.assertTrue(Unit.objects.filter(taking_part__isnull=False).exists())

    def test_building_under_an_existing_root(self):
        root = Unit.objects.create(kind=ASSOCIATION, key="testing", name="Test server")
        examples.build("federation", root)
        self.assertEqual(Unit.objects.root(), root)
        self.assertTrue(Unit.objects.filter(parent=root, key="robotics").exists())

    def test_the_root_key_cannot_clash_with_an_example_key(self):
        root = Unit.objects.create(kind=ASSOCIATION, key="robotics", name="Clash")
        with self.assertRaises(ValidationError):
            examples.build("federation", root)

    def test_replace_removes_everything_under_the_root(self):
        root = self.build("federation")
        examples.build("society", root, replace=True)
        self.assertFalse(Unit.objects.filter(key="robotics").exists())
        self.assertEqual(Unit.objects.root(), root)
        self.assertEqual(Unit.objects.filter(kind=ASSOCIATION).count(), 1)

    def test_without_replace_it_refuses_a_tree_that_is_already_there(self):
        root = self.build("society")
        with self.assertRaises(ValidationError):
            examples.build("society", root)


class AdminPage(TestCase):
    client_class = CaddyClient

    def setUp(self):
        self.root = association("example")
        self.website = team("website", self.root)

    def enter(self, role):
        from accounts.tests import make_user

        user, _ = make_user(f"{role}@example.org", [role], two_factor=True)
        self.client.force_login(user)
        return user

    def form(self, **fields):
        data = {"kind": TEAM, "key": "", "name": "", "description": "", "parent": self.root.pk, "primary_colour": "", "accent_colour": "", "end_date": "", "run_for": "", "taking_part": []}
        data.update(fields)
        return {k: v for k, v in data.items() if v is not None}

    def test_an_operator_adds_a_unit(self):
        self.enter("operator")
        response = self.client.post(reverse("admin:structure_unit_add"), self.form(key="events", name="Events"))
        self.assertEqual(response.status_code, 302, response.content.decode()[:2000])
        self.assertTrue(Unit.objects.filter(key="events", parent=self.root).exists())

    def test_the_form_shows_a_broken_rule(self):
        self.enter("operator")
        response = self.client.post(reverse("admin:structure_unit_add"), self.form(key="Bad Key", name="Bad"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Unit.objects.filter(name="Bad").exists())

    def test_the_key_is_fixed_after_creation(self):
        self.enter("operator")
        url = reverse("admin:structure_unit_change", args=[self.website.pk])
        self.client.post(url, self.form(key="site", name="Site"))
        self.website.refresh_from_db()
        self.assertEqual((self.website.key, self.website.name), ("website", "Site"))

    def test_the_roots_name_comes_from_the_config(self):
        self.enter("operator")
        url = reverse("admin:structure_unit_change", args=[self.root.pk])
        self.client.post(url, self.form(kind=ASSOCIATION, name="Other", parent=""))
        self.root.refresh_from_db()
        self.assertEqual(self.root.name, "Example")

    def test_the_root_has_no_delete_button(self):
        self.enter("operator")
        response = self.client.post(reverse("admin:structure_unit_delete", args=[self.root.pk]), {"post": "yes"})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Unit.objects.filter(pk=self.root.pk).exists())

    def test_an_operator_archives_and_brings_back(self):
        self.enter("operator")
        project = team("hackathon", self.root, end_date=date(2027, 4, 1))
        changelist = reverse("admin:structure_unit_changelist")
        self.client.post(changelist, {"action": "archive", "_selected_action": [project.pk]})
        project.refresh_from_db()
        self.assertTrue(project.archived)
        self.client.post(changelist, {"action": "unarchive", "_selected_action": [project.pk]})
        project.refresh_from_db()
        self.assertFalse(project.archived)

    def test_a_team_inside_an_archived_one_can_still_be_edited(self):
        self.enter("operator")
        project = team("hackathon", self.root, end_date=date(2027, 4, 1))
        catering = team("catering", project)
        project.archive()
        url = reverse("admin:structure_unit_change", args=[catering.pk])
        response = self.client.post(url, self.form(key="catering", name="Food", parent=project.pk))
        self.assertEqual(response.status_code, 302)
        catering.refresh_from_db()
        self.assertEqual(catering.name, "Food")

    def test_bringing_back_a_team_and_the_one_above_it_together(self):
        self.enter("operator")
        zeta = team("zeta", self.root)
        alpha = team("alpha", zeta)
        zeta.archive()
        changelist = reverse("admin:structure_unit_changelist")
        self.client.post(changelist + "?o=2", {"action": "unarchive", "_selected_action": [alpha.pk, zeta.pk]})
        alpha.refresh_from_db()
        zeta.refresh_from_db()
        self.assertFalse(alpha.archived or zeta.archived)

    def test_the_list_is_a_tree(self):
        self.enter("operator")
        team("design", self.website)
        response = self.client.get(reverse("admin:structure_unit_changelist"))
        page = response.content.decode()
        self.assertLess(page.index("Website"), page.index("Design"))

    def test_board_sees_units_but_cannot_change_them(self):
        self.enter("board")
        self.assertEqual(self.client.get(reverse("admin:structure_unit_changelist")).status_code, 200)
        self.assertEqual(self.client.post(reverse("admin:structure_unit_add"), self.form(key="events", name="Events")).status_code, 403)
        self.assertFalse(Unit.objects.filter(key="events").exists())

    def test_a_member_sees_nothing(self):
        user, _ = get_user_model().objects.create_user("member@example.org"), None
        user.groups.set(Group.objects.filter(role__key="member"))
        self.client.force_login(user)
        response = self.client.get(reverse("admin:structure_unit_changelist"))
        self.assertNotEqual(response.status_code, 200)
