"""Telling tools about changes to someone's access, with made-up connectors in place of
the tools. tests/test_connectors.py and tests/live_check.py cover the real ones."""

import io
import os
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

from allauth.idp.oidc.adapter import get_adapter
from allauth.idp.oidc.models import Client
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from . import sync
from .models import AccessSync, FollowUp, ToolAccess
from .test_login_service import make_tool
from .tests import PASSWORD, Base, make_user, totp_code


class FakeConnector:
    def __init__(self, tool="tool"):
        self.tool = tool
        self.seen = []
        self.broken = False
        self.notes = []

    def apply(self, person, note):
        if self.broken:
            raise RuntimeError("the tool is down")
        self.seen.append(person)
        for key, text in self.notes:
            note(key, text)


def waiting(user):
    row = AccessSync.objects.filter(user=user).first()
    return bool(row and row.pending)


class Asking(Base):
    """Every way someone's access can change has to mark them as waiting."""

    def setUp(self):
        self.user, _ = make_user(groups=["developer"])
        AccessSync.objects.all().delete()

    def test_turning_off_an_account(self):
        self.user.is_active = False
        self.user.save()
        self.assertTrue(waiting(self.user))

    def test_logging_in_doesnt_count(self):
        """It would otherwise ask, for everyone, at every login."""
        self.user.last_login = timezone.now()
        self.user.save(update_fields=["last_login"])
        self.assertFalse(waiting(self.user))

    def test_a_new_account_doesnt_count_as_it_has_reached_nothing(self):
        make_user("new@example.org")
        self.assertFalse(AccessSync.objects.exists())

    def test_adding_them_to_a_group(self):
        self.user.groups.add(Group.objects.get(role__key="board"))
        self.assertTrue(waiting(self.user))

    def test_removing_them_from_a_group(self):
        self.user.groups.remove(Group.objects.get(role__key="developer"))
        self.assertTrue(waiting(self.user))

    def test_clearing_their_groups(self):
        self.user.groups.clear()
        self.assertTrue(waiting(self.user))

    def test_setting_their_groups(self):
        self.user.groups.set([])
        self.assertTrue(waiting(self.user))

    def test_changing_a_groups_members_from_the_other_side(self):
        group = Group.objects.get(role__key="developer")
        group.user_set.remove(self.user)
        self.assertTrue(waiting(self.user))
        AccessSync.objects.all().delete()
        group.user_set.add(self.user)
        self.assertTrue(waiting(self.user))

    def test_clearing_a_groups_members(self):
        Group.objects.get(role__key="developer").user_set.clear()
        self.assertTrue(waiting(self.user))

    def test_deleting_a_group(self):
        group = Group.objects.create(name="Temporary")
        self.user.groups.add(group)
        AccessSync.objects.all().delete()
        group.delete()
        self.assertTrue(waiting(self.user))

    def test_renaming_a_group(self):
        """A tool's access list holds custom:<name>, so this can take someone's access away."""
        group = Group.objects.create(name="Temporary")
        self.user.groups.add(group)
        AccessSync.objects.all().delete()
        group.name = "Renamed"
        group.save()
        self.assertTrue(waiting(self.user))

    def test_changing_who_may_use_a_tool_asks_for_everyone(self):
        tool = make_tool()
        AccessSync.objects.all().delete()
        ToolAccess.objects.create(tool=tool, group_keys=["board"])
        self.assertTrue(waiting(self.user))

    def test_deleting_an_account_keeps_what_tools_know_them_by(self):
        sub, email = sync.sub_of(self.user), self.user.email
        self.user.delete()
        row = AccessSync.objects.get(sub=sub)
        self.assertIsNone(row.user)
        self.assertEqual(row.label, email)
        self.assertTrue(row.pending)

    def test_asking_again_moves_it_along_and_clears_any_wait(self):
        self.user.groups.clear()
        row = AccessSync.objects.get(user=self.user)
        AccessSync.objects.filter(pk=row.pk).update(pending=False, attempts=4, retry_at=timezone.now() + timedelta(hours=1))
        self.user.groups.add(Group.objects.get(role__key="board"))
        again = AccessSync.objects.get(user=self.user)
        self.assertGreater(again.requested_at, row.requested_at)
        self.assertTrue(again.pending)
        self.assertEqual(again.attempts, 0)
        self.assertIsNone(again.retry_at)

    def test_asking_for_everyone_includes_new_accounts_and_keeps_one_row_each(self):
        make_user("new@example.org")
        self.user.groups.clear()
        sync.request_for_everyone()
        sync.request_for_everyone()
        self.assertEqual(AccessSync.objects.filter(pending=True).count(), 2)
        self.assertEqual(AccessSync.objects.count(), 2)

    def test_the_change_and_the_asking_happen_together(self):
        """Otherwise a crash between the two would leave someone cut off in core and not in the tools."""
        with self.assertRaises(ZeroDivisionError):
            with transaction.atomic():
                self.user.is_active = False
                self.user.save()
                1 / 0
        self.assertFalse(AccessSync.objects.exists())

    def test_the_sub_is_what_the_login_service_tells_tools(self):
        self.assertEqual(sync.sub_of(self.user), get_adapter().get_user_sub(make_tool(), self.user))


class Working(Base):
    def setUp(self):
        self.tool = make_tool()
        self.user, _ = make_user(groups=["developer"])
        AccessSync.objects.all().delete()
        self.connector = FakeConnector()

    def ask(self):
        sync.request_for(self.user)
        return AccessSync.objects.get(user=self.user)

    def test_a_tool_is_told_what_is_decided(self):
        self.ask()
        self.assertEqual(sync.run_once([self.connector]), 1)
        person = self.connector.seen[0]
        self.assertEqual((person.sub, person.email), (str(self.user.pk), self.user.email))
        self.assertTrue(person.allowed)
        self.assertEqual(person.groups, ("developer",))

    def test_someone_turned_off_isnt_allowed(self):
        self.user.is_active = False
        self.user.save()
        sync.run_once([self.connector])
        self.assertFalse(self.connector.seen[0].allowed)

    def test_someone_outside_a_tools_groups_isnt_allowed(self):
        ToolAccess.objects.create(tool=self.tool, group_keys=["board"])
        sync.run_once([self.connector])
        self.assertFalse(self.connector.seen[0].allowed)
        self.assertEqual(self.connector.seen[0].groups, ("developer",))

    def test_someone_in_a_tools_groups_is(self):
        ToolAccess.objects.create(tool=self.tool, group_keys=["developer"])
        sync.run_once([self.connector])
        self.assertTrue(self.connector.seen[0].allowed)

    def test_each_tool_decides_separately(self):
        other = make_tool("other", "https://other.example.org/callback")
        ToolAccess.objects.create(tool=other, group_keys=["board"])
        second = FakeConnector("other")
        sync.request_for(self.user)
        sync.run_once([self.connector, second])
        self.assertTrue(self.connector.seen[-1].allowed)
        self.assertFalse(second.seen[-1].allowed)

    def test_someone_deleted_isnt_allowed(self):
        sync.request_for(self.user)
        self.user.delete()
        sync.run_once([self.connector])
        self.assertFalse(self.connector.seen[0].allowed)
        self.assertEqual(self.connector.seen[0].groups, ())

    def test_once_everyone_knows_about_someone_deleted_the_row_goes(self):
        sub = sync.sub_of(self.user)
        self.user.delete()
        sync.run_once([self.connector])
        self.assertFalse(AccessSync.objects.filter(sub=sub).exists())

    def test_a_row_for_someone_deleted_stays_until_every_tool_knows(self):
        sub = sync.sub_of(self.user)
        self.user.delete()
        self.connector.broken = True
        sync.run_once([self.connector])
        self.assertTrue(AccessSync.objects.get(sub=sub).pending)

    def test_finished_means_finished(self):
        self.ask()
        sync.run_once([self.connector])
        self.assertFalse(waiting(self.user))
        self.assertEqual(sync.run_once([self.connector]), 0)
        self.assertEqual(len(self.connector.seen), 1)

    def test_a_tool_thats_down_is_tried_again_later_not_straight_away(self):
        self.ask()
        self.connector.broken = True
        sync.run_once([self.connector])
        row = AccessSync.objects.get(user=self.user)
        self.assertTrue(row.pending)
        self.assertEqual(row.attempts, 1)
        self.assertGreater(row.retry_at, timezone.now())
        self.assertIn("the tool is down", row.tools["tool"]["error"])
        self.assertEqual(sync.run_once([self.connector]), 0)

    def test_it_gets_there_when_the_tool_comes_back(self):
        self.ask()
        self.connector.broken = True
        sync.run_once([self.connector])
        self.connector.broken = False
        AccessSync.objects.filter(user=self.user).update(retry_at=timezone.now() - timedelta(seconds=1))
        sync.run_once([self.connector])
        row = AccessSync.objects.get(user=self.user)
        self.assertFalse(row.pending)
        self.assertEqual(row.attempts, 0)
        self.assertNotIn("error", row.tools["tool"])
        self.assertEqual(len(self.connector.seen), 1)

    def test_one_tool_down_doesnt_hold_up_the_other(self):
        make_tool("other", "https://other.example.org/callback")
        second = FakeConnector("other")
        self.ask()
        self.connector.broken = True
        sync.run_once([self.connector, second])
        self.assertEqual(len(second.seen), 1)
        self.assertTrue(waiting(self.user))

    def test_the_tool_that_got_it_isnt_told_twice(self):
        make_tool("other", "https://other.example.org/callback")
        second = FakeConnector("other")
        self.ask()
        self.connector.broken = True
        sync.run_once([self.connector, second])
        self.connector.broken = False
        AccessSync.objects.filter(user=self.user).update(retry_at=timezone.now() - timedelta(seconds=1))
        sync.run_once([self.connector, second])
        self.assertEqual(len(second.seen), 1)
        self.assertEqual(len(self.connector.seen), 1)
        self.assertFalse(waiting(self.user))

    def test_the_wait_grows_and_has_a_limit(self):
        self.assertEqual(sync._wait(0), timedelta(seconds=5))
        self.assertEqual(sync._wait(1), timedelta(seconds=10))
        self.assertEqual(sync._wait(100), sync.LONGEST_WAIT)
        self.assertEqual(sync._wait(100_000), sync.LONGEST_WAIT)  # a tool can be down for days

    def test_a_change_arriving_partway_isnt_lost(self):
        """It's read before the tool is told, and could change before this finishes."""
        self.ask()
        user = self.user

        class Changing(FakeConnector):
            def apply(self, person, note):
                super().apply(person, note)
                if len(self.seen) == 1:
                    user.groups.clear()

        connector = Changing()
        sync.run_once([connector])
        self.assertTrue(waiting(self.user))
        sync.run_once([connector])
        self.assertEqual(len(connector.seen), 2)
        self.assertEqual(connector.seen[1].groups, ())
        self.assertFalse(waiting(self.user))

    def test_a_tool_that_isnt_registered_is_skipped_and_nothing_is_decided_for_it(self):
        """`kit tool remove` then `kit enable` is how a tool's groups are changed, and nobody
        should lose access in between. Registering it asks for everyone again."""
        self.ask()
        ghost = FakeConnector("ghost")
        sync.run_once([ghost])
        self.assertEqual(ghost.seen, [])
        self.assertFalse(waiting(self.user))
        make_tool("ghost", "https://ghost.example.org/callback")
        self.assertTrue(waiting(self.user))

    def test_with_no_connectors_nothing_waits(self):
        """A module that's off just isn't asked. The worker asks for everyone when it starts."""
        self.ask()
        sync.run_once([])
        self.assertFalse(waiting(self.user))

    def test_stopping_partway_loses_nothing(self):
        """Someone only stops waiting after every tool has been dealt with."""
        self.ask()

        class Crashing(FakeConnector):
            def apply(self, person, note):
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            sync.run_once([Crashing()])
        self.assertTrue(waiting(self.user))


class Loading(Base):
    def test_a_module_isnt_loaded_until_it_has_its_token(self):
        with tempfile.TemporaryDirectory() as folder:
            demo = Path(folder, "demo")
            demo.mkdir()
            (demo / "connector.py").write_text(
                "class Connector:\n    tool = 'demo'\n    def __init__(self, environ):\n        self.token = environ['DEMO_TOKEN']\n"
            )
            env = {"SYNC_MODULES_DIR": folder, "SYNC_MODULES": "demo"}
            self.assertEqual(sync.load_connectors(env), [])
            found = sync.load_connectors({**env, "DEMO_TOKEN": "t"})
            self.assertEqual([connector.tool for connector in found], ["demo"])

    def test_no_modules_means_no_connectors(self):
        self.assertEqual(sync.load_connectors({"SYNC_MODULES": ""}), [])


class Command(Base):
    def test_starting_asks_for_everyone(self):
        user, _ = make_user()
        AccessSync.objects.all().delete()
        with mock.patch.dict(os.environ, {"SYNC_MODULES": ""}):
            call_command("sync_tools", "--once", stdout=io.StringIO())
        self.assertEqual(AccessSync.objects.filter(user=user).count(), 1)
        self.assertFalse(AccessSync.objects.get(user=user).pending)


class FollowUps(Base):
    def setUp(self):
        self.tool = make_tool()
        self.user, _ = make_user(groups=["board"])
        AccessSync.objects.all().delete()

    def run_with_note(self):
        connector = FakeConnector()
        connector.notes = [("org-1", "Change the passwords in Shared.")]
        sync.request_for(self.user)
        sync.run_once([connector])

    def open_ones(self):
        return FollowUp.objects.filter(done_at__isnull=True)

    def test_a_note_opens_one(self):
        self.run_with_note()
        item = self.open_ones().get()
        self.assertEqual(item.about, self.user.email)
        self.assertEqual(item.tool, "tool")
        self.assertEqual(item.text, "Change the passwords in Shared.")

    def test_repeating_it_doesnt_add_another(self):
        self.run_with_note()
        self.run_with_note()
        self.assertEqual(self.open_ones().count(), 1)

    def test_one_thats_done_doesnt_stop_a_new_one_next_time(self):
        self.run_with_note()
        self.open_ones().update(done_at=timezone.now())
        self.run_with_note()
        self.assertEqual(self.open_ones().count(), 1)
        self.assertEqual(FollowUp.objects.count(), 2)

    def test_a_note_survives_the_tool_failing_right_after(self):
        """It's written before the change that makes it needed, so a retry can't miss it."""

        class FailsAfterNoting(FakeConnector):
            def apply(self, person, note):
                note("org-1", "Change the passwords in Shared.")
                raise RuntimeError("the tool is down")

        sync.request_for(self.user)
        sync.run_once([FailsAfterNoting()])
        self.assertEqual(self.open_ones().count(), 1)

    def enter_admin(self, groups, email="admin@example.org"):
        user, secret = make_user(email, groups, password=PASSWORD, two_factor=True)
        self.log_in(email)
        self.enter_code(totp_code(secret))
        return user

    def list_url(self):
        return reverse("admin:accounts_followup_changelist")

    def test_board_and_operators_see_them_and_nobody_else(self):
        self.run_with_note()
        for groups, status in ((["board"], 200), (["operator"], 200), (["treasurer"], 403), (["events"], 403)):
            self.client.logout()
            user = self.enter_admin(groups, f"{groups[0]}@example.org")
            with self.subTest(groups):
                self.assertEqual(self.client.get(self.list_url()).status_code, status)
            user.delete()

    def test_a_member_cant_reach_them(self):
        make_user("member@example.org", ["member"], password=PASSWORD)
        self.log_in("member@example.org")
        self.assertNotEqual(self.client.get(self.list_url()).status_code, 200)

    def test_marking_one_done_says_who_and_when(self):
        self.run_with_note()
        board = self.enter_admin(["board"])
        item = self.open_ones().get()
        response = self.client.post(self.list_url(), {"action": "mark_done", "_selected_action": [item.pk]})
        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        self.assertIsNotNone(item.done_at)
        self.assertEqual(item.done_by, board)

    def test_marking_one_done_twice_keeps_the_first(self):
        self.run_with_note()
        board = self.enter_admin(["board"])
        item = self.open_ones().get()
        self.client.post(self.list_url(), {"action": "mark_done", "_selected_action": [item.pk]})
        item.refresh_from_db()
        first = item.done_at
        self.client.post(self.list_url(), {"action": "mark_done", "_selected_action": [item.pk]})
        item.refresh_from_db()
        self.assertEqual(item.done_at, first)
        self.assertEqual(item.done_by, board)

    def test_nobody_can_add_or_delete_one(self):
        self.run_with_note()
        self.enter_admin(["operator"])
        item = self.open_ones().get()
        self.assertEqual(self.client.get(reverse("admin:accounts_followup_add")).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:accounts_followup_delete", args=[item.pk])).status_code, 403)

    def test_nobody_can_edit_one(self):
        self.run_with_note()
        self.enter_admin(["operator"])
        item = self.open_ones().get()
        url = reverse("admin:accounts_followup_change", args=[item.pk])
        self.client.post(url, {"text": "Nothing to do", "done_at_0": "2020-01-01", "done_at_1": "00:00:00"})
        item.refresh_from_db()
        self.assertEqual(item.text, "Change the passwords in Shared.")
        self.assertIsNone(item.done_at)

    def test_open_ones_are_on_the_admin_page_until_done(self):
        self.run_with_note()
        self.enter_admin(["board"])
        self.assertContains(self.client.get(reverse("admin:index")), "Change the passwords in Shared.")
        self.open_ones().update(done_at=timezone.now())
        self.assertNotContains(self.client.get(reverse("admin:index")), "Change the passwords in Shared.")

    def test_a_treasurer_isnt_told(self):
        self.run_with_note()
        self.enter_admin(["treasurer"])
        self.assertNotContains(self.client.get(reverse("admin:index")), "Change the passwords in Shared.")

    def test_text_from_a_tool_is_escaped(self):
        connector = FakeConnector()
        connector.notes = [("org-1", "Change the passwords in <script>alert(1)</script>.")]
        sync.request_for(self.user)
        sync.run_once([connector])
        self.enter_admin(["board"])
        for url in (reverse("admin:index"), self.list_url()):
            self.assertNotContains(self.client.get(url), "<script>alert(1)</script>")


class AdminPage(Base):
    def enter_as_operator(self):
        user, secret = make_user("operator@example.org", ["operator"], password=PASSWORD, two_factor=True)
        self.log_in("operator@example.org")
        self.enter_code(totp_code(secret))
        return user

    def setUp(self):
        self.tool = make_tool()
        self.person, _ = make_user(groups=["developer"])
        self.enter_as_operator()
        AccessSync.objects.all().delete()

    def change_url(self):
        return reverse("admin:accounts_user_change", args=[self.person.pk])

    def test_turning_off_an_account_on_the_page_asks(self):
        self.client.post(self.change_url(), {"email": self.person.email, "name": "", "groups": []})
        self.person.refresh_from_db()
        self.assertFalse(self.person.is_active)
        self.assertTrue(waiting(self.person))

    def test_their_page_says_what_happened(self):
        self.assertContains(self.client.get(self.change_url()), "Nothing to send")
        connector = FakeConnector()
        sync.request_for(self.person)
        self.assertContains(self.client.get(self.change_url()), "Waiting to be sent")
        connector.broken = True
        sync.run_once([connector])
        self.assertContains(self.client.get(self.change_url()), "the tool is down")
        connector.broken = False
        AccessSync.objects.update(retry_at=timezone.now() - timedelta(seconds=1))
        sync.run_once([connector])
        page = self.client.get(self.change_url())
        self.assertContains(page, "Sent to tool")
        self.assertNotContains(page, "the tool is down")

    def test_nothing_is_said_on_the_admin_page_at_first(self):
        sync.request_for(self.person)
        self.assertNotContains(self.client.get(reverse("admin:index")), "reached every tool")

    def test_a_change_stuck_for_a_minute_is_on_the_admin_page(self):
        sync.request_for(self.person)
        AccessSync.objects.update(requested_at=timezone.now() - timedelta(minutes=2))
        self.assertContains(self.client.get(reverse("admin:index")), "reached every tool")

    def test_a_board_member_is_told_too(self):
        self.client.logout()
        _, secret = make_user("board@example.org", ["board"], password=PASSWORD, two_factor=True)
        self.log_in("board@example.org")
        self.enter_code(totp_code(secret))
        sync.request_for(self.person)
        AccessSync.objects.update(requested_at=timezone.now() - timedelta(minutes=2))
        self.assertContains(self.client.get(reverse("admin:index")), "reached every tool")

    def test_tools_are_not_a_condition_for_saving(self):
        """The page only writes down what's wanted. It never waits on a tool."""
        self.assertFalse(Client.objects.filter(name="not-there").exists())
        response = self.client.post(self.change_url(), {"email": self.person.email, "name": "", "groups": [], "is_active": "on"})
        self.assertEqual(response.status_code, 302)


class Roles(Base):
    def test_follow_ups_are_for_board_and_operators(self):
        for key, expected in (("board", True), ("operator", True), ("treasurer", False), ("events", False), ("developer", False), ("member", False)):
            with self.subTest(key):
                user, _ = make_user(f"{key}@example.org", [key])
                self.assertEqual(user.has_perm("accounts.change_followup"), expected)

    def test_nobody_gets_to_change_the_queue(self):
        for key in ("board", "operator"):
            user, _ = make_user(f"{key}@example.org", [key])
            self.assertFalse(user.has_perm("accounts.change_accesssync"))
            self.assertFalse(user.has_perm("accounts.add_followup"))
            self.assertFalse(user.has_perm("accounts.delete_followup"))
