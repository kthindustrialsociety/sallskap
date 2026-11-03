"""Step 1's tests, using fake users. The README shows how to run them in Docker."""

import io
import logging
import time

from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import SECRET_SESSION_KEY, TOTP, format_hotp_value, generate_totp_secret, hotp_value
from allauth.urls import urlpatterns as allauth_urlpatterns
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import resolve, reverse
from sallskap.urls import not_found

from .links import password_link

logging.disable(logging.CRITICAL)  # these tests refuse people on purpose, which would fill the output

User = get_user_model()
PASSWORD = "correct-horse-battery-7"
NEW_PASSWORD = "a-different-long-phrase-9"


class CaddyClient(Client):
    """Sends what Caddy sends: HTTPS was used, and which address the visitor has."""

    def __init__(self, ip="203.0.113.9", **kwargs):
        super().__init__(headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": ip}, **kwargs)


def totp_code(secret):
    return format_hotp_value(hotp_value(secret, int(time.time()) // 30))


def make_user(email="person@example.org", groups=(), password=None, two_factor=False):
    """Returns the user, and their 2FA secret if they have one."""
    user = User.objects.create_user(email, password)
    user.groups.set(Group.objects.filter(role__key__in=groups))
    secret = None
    if two_factor:
        secret = generate_totp_secret()
        TOTP.activate(user, secret)
    return user, secret


def set_password_url(link):
    return link.removeprefix(f"https://{settings.APP_HOST}")


class Base(TestCase):
    client_class = CaddyClient

    def log_in(self, email, password=PASSWORD, client=None):
        return (client or self.client).post(reverse("account_login"), {"login": email, "password": password})

    def assert_logged_in(self, client=None):
        response = (client or self.client).get(reverse("home"))
        self.assertEqual(response.status_code, 200)

    def assert_logged_out(self, client=None):
        response = (client or self.client).get(reverse("home"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("account_login"), response.url)

    def enter_code(self, code, client=None):
        return (client or self.client).post(reverse("mfa_authenticate"), {"code": code})


class Login(Base):
    def test_right_password_logs_in(self):
        make_user(password=PASSWORD)
        self.log_in("person@example.org")
        self.assert_logged_in()

    def test_wrong_password_is_refused(self):
        make_user(password=PASSWORD)
        self.log_in("person@example.org", "not-the-password-at-all")
        self.assert_logged_out()

    def test_unknown_email_is_refused_with_the_same_message(self):
        make_user(password=PASSWORD)
        unknown = self.log_in("nobody@example.org", PASSWORD)
        wrong = self.log_in("person@example.org", "not-the-password-at-all")
        self.assertEqual(unknown.context["form"].errors, wrong.context["form"].errors)
        self.assert_logged_out()

    def test_email_ignores_case(self):
        make_user(password=PASSWORD)
        self.log_in("PERSON@Example.ORG")
        self.assert_logged_in()

    def test_account_with_no_password_cannot_log_in(self):
        make_user()
        self.log_in("person@example.org", "")
        self.assert_logged_out()

    def test_a_turned_off_account_is_refused(self):
        user, _ = make_user(password=PASSWORD)
        user.is_active = False
        user.save()
        self.log_in("person@example.org")
        self.assert_logged_out()

    def test_password_alone_is_not_enough_with_2fa(self):
        make_user(password=PASSWORD, two_factor=True)
        response = self.log_in("person@example.org")
        self.assertRedirects(response, reverse("mfa_authenticate"), fetch_redirect_response=False)
        self.assert_logged_out()

    def test_password_and_right_code_log_in(self):
        _, secret = make_user(password=PASSWORD, two_factor=True)
        self.log_in("person@example.org")
        self.enter_code(totp_code(secret))
        self.assert_logged_in()

    def test_wrong_code_is_refused(self):
        make_user(password=PASSWORD, two_factor=True)
        self.log_in("person@example.org")
        self.enter_code("000000")
        self.assert_logged_out()

    def test_a_code_cannot_be_used_twice(self):
        _, secret = make_user(password=PASSWORD, two_factor=True)
        code = totp_code(secret)
        self.log_in("person@example.org")
        self.enter_code(code)
        self.assert_logged_in()

        second = CaddyClient(ip="203.0.113.10")
        self.log_in("person@example.org", client=second)
        self.enter_code(code, client=second)
        self.assert_logged_out(second)

    def test_a_recovery_code_works_once(self):
        user, _ = make_user(password=PASSWORD, two_factor=True)
        code = RecoveryCodes.activate(user).get_unused_codes()[0]
        self.log_in("person@example.org")
        self.enter_code(code)
        self.assert_logged_in()

        second = CaddyClient(ip="203.0.113.10")
        self.log_in("person@example.org", client=second)
        self.enter_code(code, client=second)
        self.assert_logged_out(second)


class Lockout(Base):
    def test_locks_after_repeated_wrong_passwords(self):
        make_user(password=PASSWORD)
        for _ in range(5):
            self.log_in("person@example.org", "not-the-password-at-all")
        response = self.log_in("person@example.org")  # the right password, too late
        self.assertContains(response, "Too many failed login attempts")
        self.assert_logged_out()

    def test_a_lock_only_holds_at_the_address_it_came_from(self):
        """Otherwise anyone who knows an email address could keep that person out."""
        make_user(password=PASSWORD)
        for _ in range(5):
            self.log_in("person@example.org", "not-the-password-at-all")
        other = CaddyClient(ip="198.51.100.7")
        self.log_in("person@example.org", client=other)
        self.assert_logged_in(other)

    def test_a_lock_holds_across_browsers_at_the_same_address(self):
        make_user(password=PASSWORD)
        for _ in range(5):
            self.log_in("person@example.org", "not-the-password-at-all", client=CaddyClient(ip="198.51.100.7"))
        response = self.log_in("person@example.org", client=CaddyClient(ip="198.51.100.7"))
        self.assertContains(response, "Too many failed login attempts")

    def test_locks_after_repeated_wrong_codes(self):
        _, secret = make_user(password=PASSWORD, two_factor=True)
        self.log_in("person@example.org")
        for _ in range(5):
            self.enter_code("000000")
        self.enter_code(totp_code(secret))  # the right code, too late
        self.assert_logged_out()

    def test_one_address_is_limited_across_accounts(self):
        for attempt in range(10):
            self.log_in(f"guess{attempt}@example.org", "not-the-password-at-all")
        make_user(password=PASSWORD)
        response = self.log_in("person@example.org")
        self.assertContains(response, "Too many failed login attempts")
        self.assert_logged_out()

    def test_a_faked_forwarded_header_does_not_escape_the_limit(self):
        """Only the last address counts, as that's the one Caddy adds."""
        for attempt in range(10):
            client = CaddyClient(ip=f"10.9.8.{attempt}, 203.0.113.9")
            self.log_in(f"guess{attempt}@example.org", "not-the-password-at-all", client=client)
        make_user(password=PASSWORD)
        response = self.log_in("person@example.org", client=CaddyClient(ip="10.9.8.99, 203.0.113.9"))
        self.assertContains(response, "Too many failed login attempts")

    def test_visitors_at_other_addresses_are_not_affected(self):
        """If every visitor looked like Caddy's address, one person could lock everyone out."""
        for attempt in range(10):
            self.log_in(f"guess{attempt}@example.org", "not-the-password-at-all")
        make_user(password=PASSWORD)
        other = CaddyClient(ip="198.51.100.7")
        self.log_in("person@example.org", client=other)
        self.assert_logged_in(other)


class ClosedRoutes(Base):
    def test_routes_that_need_signup_or_email_do_not_exist(self):
        for path in (
            "/accounts/signup/",
            "/accounts/email/",
            "/accounts/confirm-email/",
            "/accounts/confirm-email/abc/",
            "/accounts/password/reset/",
            "/accounts/password/reset/done/",
            "/accounts/password/reset/key/1-abc/",
            "/accounts/password/set/",
            "/accounts/login/code/confirm/",
            "/accounts/some-future-route/",
        ):
            with self.subTest(path):
                self.assertEqual(self.client.get(path).status_code, 404)
                self.assertEqual(self.client.post(path, {"email": "a@example.org"}).status_code, 404)
        self.assertEqual(User.objects.count(), 0)

    def test_only_the_routes_we_use_are_open(self):
        """If an allauth upgrade adds a route, this fails instead of the route going live."""

        def routes(patterns, prefix=""):
            for pattern in patterns:
                if hasattr(pattern, "url_patterns"):
                    yield from routes(pattern.url_patterns, prefix + str(pattern.pattern))
                else:
                    yield prefix + str(pattern.pattern)

        open_routes = {route for route in routes(allauth_urlpatterns) if resolve(f"/accounts/{route}").func is not not_found}
        self.assertEqual(
            open_routes,
            {
                "login/",
                "logout/",
                "inactive/",
                "reauthenticate/",
                "password/change/",
                "2fa/",
                "2fa/authenticate/",
                "2fa/reauthenticate/",
                "2fa/totp/activate/",
                "2fa/totp/deactivate/",
                "2fa/recovery-codes/",
                "2fa/recovery-codes/generate/",
                "2fa/recovery-codes/download/",
            },
        )

    def test_the_routes_we_use_still_work(self):
        self.assertEqual(self.client.get(reverse("account_login")).status_code, 200)
        self.assertEqual(self.client.get(reverse("mfa_index")).status_code, 302)  # to login
        self.assertEqual(self.client.get("/accounts/login").status_code, 301)  # adds the slash

    def test_the_login_page_offers_neither_signup_nor_reset(self):
        response = self.client.get(reverse("account_login"))
        self.assertNotContains(response, "signup")
        self.assertNotContains(response, "password/reset")

    def test_django_admin_login_cannot_log_anyone_in(self):
        make_user(groups=["operator"], password=PASSWORD)
        response = self.client.post(reverse("admin:login"), {"username": "person@example.org", "password": PASSWORD})
        self.assertRedirects(
            response,
            f"{reverse('account_login')}?next={reverse('admin:index')}",
            fetch_redirect_response=False,
        )
        self.assert_logged_out()

    def test_no_email_is_sent(self):
        make_user(password=PASSWORD)
        self.log_in("person@example.org", "not-the-password-at-all")
        self.log_in("person@example.org")
        self.assertEqual(mail.outbox, [])


class Groups(Base):
    def snapshot(self):
        return sorted(
            (group.pk, group.name, group.role.key, sorted(group.permissions.values_list("pk", flat=True)))
            for group in Group.objects.all()
        )

    def test_all_six_exist_automatically(self):
        self.assertEqual(
            sorted(Group.objects.values_list("name", flat=True)),
            ["Board", "Developer", "Events", "Member", "Operator", "Treasurer"],
        )

    def test_running_setup_again_changes_nothing(self):
        before = self.snapshot()
        call_command("migrate", verbosity=0)
        self.assertEqual(self.snapshot(), before)

    def test_setup_repairs_changed_permissions(self):
        before = self.snapshot()
        Group.objects.get(role__key="board").permissions.clear()
        call_command("migrate", verbosity=0)
        self.assertEqual(self.snapshot(), before)

    def test_renaming_a_group_keeps_its_rules_and_its_name(self):
        Group.objects.filter(role__key="board").update(name="Styrelse")
        call_command("migrate", verbosity=0)
        self.assertEqual(Group.objects.get(role__key="board").name, "Styrelse")
        self.assertFalse(Group.objects.filter(name="Board").exists())

        user, _ = make_user(groups=["board"])
        self.assertTrue(user.requires_2fa)
        self.assertTrue(user.is_staff)


class Admin(Base):
    def enter(self, groups, two_factor=True, email="person@example.org"):
        user, _ = make_user(email, groups, two_factor=two_factor)
        self.client.force_login(user)
        return user

    def get_admin(self):
        return self.client.get(reverse("admin:index"), follow=True)

    def assert_sent_to_2fa_setup(self, url):
        response = self.client.get(url, follow=True)
        self.assertEqual(response.redirect_chain[-1][0], reverse("mfa_index"))
        self.assertContains(response, "Set up two-factor authentication")

    def user_form(self, user, **changes):
        data = {
            "email": user.email,
            "name": user.name,
            "is_active": "on",
            "groups": [group.pk for group in user.groups.all()],
        }
        data.update(changes)
        return data

    def test_visitors_are_sent_to_the_real_login(self):
        target = reverse("admin:accounts_user_changelist")
        response = self.client.get(target, follow=True)
        self.assertEqual(response.redirect_chain[-1][0], f"{reverse('account_login')}?next={target}")

    def test_login_returns_to_the_page_asked_for(self):
        _, secret = make_user(groups=["operator"], password=PASSWORD, two_factor=True)
        target = reverse("admin:accounts_user_changelist")
        login_page = self.client.get(target, follow=True).redirect_chain[-1][0]
        self.client.post(login_page, {"login": "person@example.org", "password": PASSWORD})
        response = self.enter_code(totp_code(secret))
        self.assertRedirects(response, target, fetch_redirect_response=False)

    def test_login_never_redirects_to_another_site(self):
        make_user(password=PASSWORD)
        login_page = self.client.get(f"{reverse('admin:login')}?next=https://evil.example.org/", follow=True).redirect_chain[-1][0]
        response = self.client.post(login_page, {"login": "person@example.org", "password": PASSWORD})
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("evil", response.url)

    def test_groups_needing_2fa_are_refused_without_it(self):
        for group in ("board", "treasurer", "operator"):
            with self.subTest(group):
                self.enter([group], two_factor=False, email=f"{group}@example.org")
                self.assert_sent_to_2fa_setup(reverse("admin:index"))
                self.assert_sent_to_2fa_setup(reverse("admin:accounts_user_changelist"))

    def test_a_superuser_is_refused_without_2fa_too(self):
        self.client.force_login(User.objects.create_superuser("root@example.org"))
        self.assert_sent_to_2fa_setup(reverse("admin:index"))

    def test_the_same_groups_get_in_with_2fa(self):
        for group in ("board", "treasurer", "operator"):
            with self.subTest(group):
                self.enter([group], email=f"{group}@example.org")
                self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)

    def test_events_gets_in_without_2fa_and_sees_nothing_yet(self):
        self.enter(["events"], two_factor=False)
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["app_list"], [])

    def test_developer_and_member_are_refused(self):
        for group in ("developer", "member"):
            with self.subTest(group):
                self.enter([group], email=f"{group}@example.org")
                response = self.get_admin()
                self.assertEqual(response.status_code, 403)

    def test_someone_with_no_group_is_refused(self):
        self.enter([])
        self.assertEqual(self.get_admin().status_code, 403)

    def test_treasurer_cannot_see_what_is_meant_for_operators(self):
        self.enter(["treasurer"])
        self.assertEqual(self.client.get(reverse("admin:index")).context["app_list"], [])
        for name in ("accounts_user_changelist", "accounts_user_add", "auth_group_changelist", "mfa_authenticator_changelist"):
            with self.subTest(name):
                self.assertEqual(self.client.get(reverse(f"admin:{name}")).status_code, 403)

    def test_board_can_view_users_but_change_nothing(self):
        other, _ = make_user("other@example.org", ["member"])
        self.enter(["board"])
        self.assertContains(self.client.get(reverse("admin:accounts_user_changelist")), "other@example.org")

        change = reverse("admin:accounts_user_change", args=[other.pk])
        self.assertEqual(self.client.get(change).status_code, 200)
        operators = Group.objects.get(role__key="operator")
        self.assertEqual(self.client.post(change, self.user_form(other, groups=[operators.pk])).status_code, 403)
        self.assertFalse(other.has_role("operator"))

        self.assertEqual(self.client.get(reverse("admin:accounts_user_add")).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:auth_group_changelist")).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:mfa_authenticator_changelist")).status_code, 403)

    def test_board_cannot_put_itself_in_a_group(self):
        me = self.enter(["board"])
        operators = Group.objects.get(role__key="operator")
        change = reverse("admin:accounts_user_change", args=[me.pk])
        self.assertEqual(self.client.post(change, self.user_form(me, groups=[operators.pk])).status_code, 403)
        self.assertFalse(me.has_role("operator"))

    def test_operator_adds_a_person_to_a_group_with_no_password(self):
        self.enter(["operator"])
        board = Group.objects.get(role__key="board")
        response = self.client.post(
            reverse("admin:accounts_user_add"),
            {"email": "New.Person@Example.org", "name": "New Person", "is_active": "on", "groups": [board.pk]},
        )
        self.assertEqual(response.status_code, 302)
        added = User.objects.get(email="new.person@example.org")
        self.assertTrue(added.has_role("board"))
        self.assertFalse(added.has_usable_password())

    def test_two_accounts_cannot_differ_only_by_case(self):
        make_user("taken@example.org")
        self.enter(["operator"])
        response = self.client.post(
            reverse("admin:accounts_user_add"), {"email": "TAKEN@example.org", "is_active": "on"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already exists")
        self.assertEqual(User.objects.filter(email="taken@example.org").count(), 1)

    def test_the_database_refuses_an_uppercase_email(self):
        user, _ = make_user()
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.filter(pk=user.pk).update(email="Person@example.org")

    def test_operator_cannot_make_anyone_a_superuser(self):
        me = self.enter(["operator"])
        change = reverse("admin:accounts_user_change", args=[me.pk])
        self.assertNotContains(self.client.get(change), "is_superuser")
        self.client.post(change, self.user_form(me, is_superuser="on"))
        me.refresh_from_db()
        self.assertFalse(me.is_superuser)

    def test_operator_cannot_change_a_superuser(self):
        boss = User.objects.create_superuser("boss@example.org")
        self.enter(["operator"])
        change = reverse("admin:accounts_user_change", args=[boss.pk])
        response = self.client.post(change, self.user_form(boss, is_active=""))
        self.assertEqual(response.status_code, 403)
        boss.refresh_from_db()
        self.assertTrue(boss.is_active)

    def test_operator_can_rename_a_group_but_not_change_its_permissions(self):
        self.enter(["operator"])
        treasurer = Group.objects.get(role__key="treasurer")
        change = reverse("admin:auth_group_change", args=[treasurer.pk])
        any_permission = Group.objects.get(role__key="operator").permissions.first()
        self.client.post(change, {"name": "Kassör", "permissions": [any_permission.pk]})
        treasurer.refresh_from_db()
        self.assertEqual(treasurer.name, "Kassör")
        self.assertEqual(treasurer.permissions.count(), 0)

    def test_nobody_can_delete_a_built_in_group(self):
        self.client.force_login(User.objects.create_superuser("root@example.org"))
        TOTP.activate(User.objects.get(email="root@example.org"), generate_totp_secret())
        board = Group.objects.get(role__key="board")
        response = self.client.post(reverse("admin:auth_group_delete", args=[board.pk]), {"post": "yes"})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Group.objects.filter(pk=board.pk).exists())

    def test_operator_cannot_see_a_2fa_secret(self):
        other, secret = make_user("other@example.org", two_factor=True)
        recovery = RecoveryCodes.activate(other).get_unused_codes()[0]
        self.enter(["operator"])
        for authenticator in Authenticator.objects.filter(user=other):
            response = self.client.get(reverse("admin:mfa_authenticator_change", args=[authenticator.pk]))
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, secret)
            self.assertNotContains(response, recovery)
            self.assertNotContains(response, "seed")

    def test_operator_can_remove_2fa_for_someone_who_lost_their_phone(self):
        other, _ = make_user("other@example.org", ["board"], two_factor=True)
        RecoveryCodes.activate(other)
        self.assertFalse(other.needs_2fa_setup)
        totp = Authenticator.objects.get(user=other, type=Authenticator.Type.TOTP)
        self.enter(["operator"])
        response = self.client.post(reverse("admin:mfa_authenticator_delete", args=[totp.pk]), {"post": "yes"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(other.needs_2fa_setup)
        self.assertFalse(Authenticator.objects.filter(user=other).exists())

    def test_removing_2fa_in_bulk_removes_all_of_it(self):
        other, _ = make_user("other@example.org", ["board"], two_factor=True)
        RecoveryCodes.activate(other)
        totp = Authenticator.objects.get(user=other, type=Authenticator.Type.TOTP)
        self.enter(["operator"])
        self.client.post(
            reverse("admin:mfa_authenticator_changelist"),
            {"action": "delete_selected", "_selected_action": [totp.pk], "post": "yes"},
        )
        self.assertFalse(Authenticator.objects.filter(user=other).exists())

    def test_removing_2fa_leaves_other_people_alone(self):
        other, _ = make_user("other@example.org", two_factor=True)
        bystander, _ = make_user("bystander@example.org", two_factor=True)
        totp = Authenticator.objects.get(user=other, type=Authenticator.Type.TOTP)
        self.enter(["operator"])
        self.client.post(reverse("admin:mfa_authenticator_delete", args=[totp.pk]), {"post": "yes"})
        self.assertTrue(Authenticator.objects.filter(user=bystander).exists())

    def test_old_recovery_codes_stop_working_after_2fa_is_removed(self):
        """Otherwise a stolen password and paper code still log in after someone starts over."""
        other, _ = make_user("lost@example.org", ["board"], password=PASSWORD, two_factor=True)
        old_code = RecoveryCodes.activate(other).get_unused_codes()[0]
        totp = Authenticator.objects.get(user=other, type=Authenticator.Type.TOTP)
        self.enter(["operator"])
        self.client.post(reverse("admin:mfa_authenticator_delete", args=[totp.pk]), {"post": "yes"})

        phone = CaddyClient(ip="198.51.100.7")
        self.log_in("lost@example.org", client=phone)
        phone.post(reverse("account_reauthenticate"), {"password": PASSWORD})
        phone.get(reverse("mfa_activate_totp"))
        phone.post(reverse("mfa_activate_totp"), {"code": totp_code(phone.session[SECRET_SESSION_KEY])})
        self.assertFalse(User.objects.get(pk=other.pk).needs_2fa_setup)

        thief = CaddyClient(ip="198.51.100.66")
        self.log_in("lost@example.org", client=thief)
        self.enter_code(old_code, client=thief)
        self.assert_logged_out(thief)

    def test_operator_cannot_remove_a_superusers_2fa(self):
        boss, _ = make_user("boss@example.org", two_factor=True)
        boss.is_superuser = True
        boss.save()
        record = Authenticator.objects.get(user=boss)
        self.enter(["operator"])
        response = self.client.post(reverse("admin:mfa_authenticator_delete", args=[record.pk]), {"post": "yes"})
        self.assertEqual(response.status_code, 403)
        self.client.post(
            reverse("admin:mfa_authenticator_changelist"),
            {"action": "delete_selected", "_selected_action": [record.pk], "post": "yes"},
        )
        self.assertTrue(Authenticator.objects.filter(user=boss).exists())

    def test_nobody_can_copy_or_change_2fa_records_in_the_admin(self):
        other, _ = make_user("other@example.org", two_factor=True)
        authenticator = Authenticator.objects.get(user=other)
        self.enter(["operator"])
        self.assertEqual(self.client.get(reverse("admin:mfa_authenticator_add")).status_code, 403)
        change = reverse("admin:mfa_authenticator_change", args=[authenticator.pk])
        self.assertEqual(self.client.post(change, {"data": "{}"}).status_code, 403)

    def test_leaving_a_group_ends_admin_access_at_the_next_click(self):
        me = self.enter(["operator"])
        self.assertEqual(self.client.get(reverse("admin:accounts_user_changelist")).status_code, 200)
        me.groups.clear()
        self.assertEqual(self.client.get(reverse("admin:accounts_user_changelist"), follow=True).status_code, 403)

    def test_joining_a_group_requires_2fa_at_the_next_click(self):
        me = self.enter(["events"], two_factor=False)
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)
        me.groups.add(Group.objects.get(role__key="board"))
        self.assert_sent_to_2fa_setup(reverse("admin:index"))

    def test_turning_off_2fa_ends_admin_access_at_the_next_click(self):
        me = self.enter(["board"])
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)
        Authenticator.objects.filter(user=me).delete()
        self.assert_sent_to_2fa_setup(reverse("admin:index"))

    def test_turning_off_an_account_ends_its_session(self):
        me = self.enter(["operator"])
        self.assert_logged_in()
        me.is_active = False
        me.save()
        self.assert_logged_out()

    def test_admin_password_page_sends_people_to_the_limited_one(self):
        self.enter(["operator"])
        response = self.client.get(reverse("admin:password_change"))
        self.assertRedirects(response, reverse("account_change_password"), fetch_redirect_response=False)


class TwoFactorSetup(Base):
    def test_a_new_board_member_can_set_up_2fa_and_get_in(self):
        """The whole first visit, with no email in the system at any point."""
        user, _ = make_user(groups=["board"], password=PASSWORD)
        self.log_in("person@example.org")
        self.assertContains(self.client.get(reverse("home")), reverse("mfa_index"))

        # allauth asks for the password again before changing 2FA, so nobody can
        # do it from a session someone else left open.
        next_page = self.client.get(reverse("mfa_activate_totp"))
        self.assertIn(reverse("account_reauthenticate"), next_page.url)
        self.client.post(reverse("account_reauthenticate"), {"password": PASSWORD})

        response = self.client.get(reverse("mfa_activate_totp"))
        self.assertEqual(response.status_code, 200)
        secret = self.client.session[SECRET_SESSION_KEY]
        self.client.post(reverse("mfa_activate_totp"), {"code": totp_code(secret)})

        self.assertFalse(User.objects.get(pk=user.pk).needs_2fa_setup)
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)
        self.assertTrue(Authenticator.objects.filter(user=user, type=Authenticator.Type.RECOVERY_CODES).exists())
        self.assertEqual(EmailAddress.objects.count(), 0)

    def test_setup_needs_the_right_code(self):
        user, _ = make_user(groups=["board"], password=PASSWORD)
        self.log_in("person@example.org")
        self.client.post(reverse("account_reauthenticate"), {"password": PASSWORD})
        self.client.get(reverse("mfa_activate_totp"))
        self.client.post(reverse("mfa_activate_totp"), {"code": "000000"})
        self.assertTrue(User.objects.get(pk=user.pk).needs_2fa_setup)

    def test_setup_needs_the_password_again(self):
        user, _ = make_user(groups=["board"], password=PASSWORD)
        self.log_in("person@example.org")
        response = self.client.post(reverse("mfa_activate_totp"), {"code": "000000"})
        self.assertIn(reverse("account_reauthenticate"), response.url)
        self.assertTrue(User.objects.get(pk=user.pk).needs_2fa_setup)


class PasswordLinks(Base):
    def use(self, link, password=NEW_PASSWORD, client=None):
        client = client or self.client
        form_page = client.get(set_password_url(link), follow=True)
        if not form_page.context["validlink"]:
            return form_page
        return client.post(form_page.redirect_chain[-1][0], {"new_password1": password, "new_password2": password})

    def test_a_link_sets_a_password_without_logging_in(self):
        user, _ = make_user()
        response = self.use(password_link(user))
        self.assertRedirects(response, reverse("account_login"), fetch_redirect_response=False)
        self.assert_logged_out()
        self.log_in("person@example.org", NEW_PASSWORD)
        self.assert_logged_in()

    def test_a_link_works_only_once(self):
        user, _ = make_user()
        link = password_link(user)
        self.use(link)
        again = CaddyClient().get(set_password_url(link), follow=True)
        self.assertFalse(again.context["validlink"])
        self.assertContains(again, "expired or has already been used")

    def test_a_link_stops_working_once_the_person_has_logged_in(self):
        user, _ = make_user(password=PASSWORD)
        link = password_link(user)
        self.log_in("person@example.org")
        self.assertFalse(CaddyClient().get(set_password_url(link), follow=True).context["validlink"])

    @override_settings(PASSWORD_RESET_TIMEOUT=-1)
    def test_a_link_expires(self):
        user, _ = make_user()
        self.assertFalse(self.client.get(set_password_url(password_link(user)), follow=True).context["validlink"])

    def test_a_link_stops_working_when_the_account_is_turned_off(self):
        user, _ = make_user()
        link = password_link(user)
        user.is_active = False
        user.save()
        self.assertFalse(self.client.get(set_password_url(link), follow=True).context["validlink"])

    def test_a_made_up_link_is_refused(self):
        user, _ = make_user()
        link = password_link(user)
        self.assertFalse(self.client.get(set_password_url(link[:-3] + "xx/"), follow=True).context["validlink"])

    def test_a_link_does_not_get_around_2fa(self):
        user, secret = make_user(groups=["board"], two_factor=True)
        self.use(password_link(user))
        self.assert_logged_out()

        response = self.log_in("person@example.org", NEW_PASSWORD)
        self.assertRedirects(response, reverse("mfa_authenticate"), fetch_redirect_response=False)
        self.assert_logged_out()
        self.enter_code("000000")
        self.assert_logged_out()
        self.enter_code(totp_code(secret))
        self.assert_logged_in()

    def test_a_new_password_ends_open_sessions(self):
        user, _ = make_user(password=PASSWORD)
        open_session = CaddyClient(ip="198.51.100.7")
        self.log_in("person@example.org", client=open_session)
        self.assert_logged_in(open_session)
        self.use(password_link(User.objects.get(pk=user.pk)))
        self.assert_logged_out(open_session)

    def test_a_weak_password_is_refused(self):
        user, _ = make_user()
        response = self.use(password_link(user), password="password")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.assertFalse(User.objects.get(pk=user.pk).has_usable_password())


class Commands(Base):
    def run_command(self, name, *args):
        output = io.StringIO()
        call_command(name, *args, stdout=output)
        return output.getvalue()

    def test_create_admin_makes_an_operator_who_must_choose_a_password(self):
        output = self.run_command("create_admin", "First.Admin@Example.org")
        user = User.objects.get(email="first.admin@example.org")
        self.assertTrue(user.has_role("operator"))
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.has_usable_password())
        self.assertIn(f"https://{settings.APP_HOST}/set-password/", output)

    def test_create_admin_again_changes_nothing(self):
        self.run_command("create_admin", "admin@example.org")
        user = User.objects.get(email="admin@example.org")
        user.set_password(PASSWORD)
        user.save()
        before = user.password

        output = self.run_command("create_admin", "admin@example.org")
        self.assertNotIn("set-password", output)
        self.assertEqual(User.objects.count(), 1)
        user.refresh_from_db()
        self.assertEqual(user.password, before)
        self.assertEqual(user.groups.count(), 1)

    def test_create_admin_restores_a_locked_out_operator(self):
        user, _ = make_user("admin@example.org", ["member"], password=PASSWORD)
        user.is_active = False
        user.save()
        self.run_command("create_admin", "admin@example.org")
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertTrue(user.has_role("operator"))

    def test_create_admin_leaves_2fa_alone_unless_asked(self):
        user, _ = make_user("admin@example.org", ["operator"], two_factor=True)
        self.run_command("create_admin", "admin@example.org")
        self.assertFalse(user.needs_2fa_setup)

    def test_reset_2fa_gives_a_sole_operator_a_way_back_in(self):
        user, _ = make_user("admin@example.org", ["operator"], password=PASSWORD, two_factor=True)
        RecoveryCodes.activate(user)
        output = self.run_command("create_admin", "admin@example.org", "--reset-2fa")
        self.assertIn("2FA", output)
        self.assertFalse(Authenticator.objects.filter(user=user).exists())
        self.assertEqual(User.objects.get(pk=user.pk).password, user.password)
        self.log_in("admin@example.org")
        self.assert_logged_in()
        self.assertContains(self.client.get(reverse("home")), reverse("mfa_index"))

    def test_create_admin_clears_a_lockout(self):
        make_user("admin@example.org", ["operator"], password=PASSWORD)
        for _ in range(5):
            self.log_in("admin@example.org", "not-the-password-at-all")
        self.log_in("admin@example.org")
        self.assert_logged_out()
        self.run_command("create_admin", "admin@example.org")
        self.log_in("admin@example.org")
        self.assert_logged_in()

    def test_create_admin_accepts_every_address_the_admin_page_does(self):
        self.run_command("create_admin", "o'brien@kth.se")
        self.assertTrue(User.objects.filter(email="o'brien@kth.se").exists())

    def test_create_admin_refuses_a_bad_address(self):
        with self.assertRaises(CommandError):
            self.run_command("create_admin", "not an address")
        self.assertEqual(User.objects.count(), 0)

    def test_password_link_works_for_an_existing_person(self):
        make_user()
        output = self.run_command("password_link", "PERSON@example.org")
        link = next(line for line in output.splitlines() if line.startswith("https://"))
        self.assertTrue(self.client.get(set_password_url(link), follow=True).context["validlink"])

    def test_password_link_refuses_unknown_and_turned_off_accounts(self):
        with self.assertRaisesRegex(CommandError, "No account"):
            self.run_command("password_link", "nobody@example.org")
        user, _ = make_user()
        user.is_active = False
        user.save()
        with self.assertRaisesRegex(CommandError, "turned off"):
            self.run_command("password_link", "person@example.org")


class Deployment(Base):
    def test_passwords_are_hashed_with_argon2(self):
        self.assertEqual(settings.PASSWORD_HASHERS[0], "django.contrib.auth.hashers.Argon2PasswordHasher")
        self.assertTrue(make_password("anything").startswith("argon2"))
        user, _ = make_user(password=PASSWORD)
        self.assertTrue(user.password.startswith("argon2"))

    def test_passwords_cannot_resemble_the_persons_name_or_email(self):
        user = User(email="someone@example.org", name="Anna Lindqvist")
        for password in ("annalindqvist1", "someone@example"):
            with self.subTest(password), self.assertRaises(ValidationError):
                validate_password(password, user)

    def test_session_cookie_is_secure_and_limited_to_this_host(self):
        make_user(password=PASSWORD)
        response = self.log_in("person@example.org")
        cookie = response.cookies["__Host-sessionid"]
        self.assertTrue(cookie["secure"])
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertEqual(cookie["domain"], "")
        self.assertEqual(cookie["path"], "/")

    def test_csrf_cookie_is_secure_and_limited_to_this_host(self):
        response = self.client.get(reverse("account_login"))
        cookie = response.cookies["__Host-csrftoken"]
        self.assertTrue(cookie["secure"])
        self.assertEqual(cookie["domain"], "")
        self.assertEqual(cookie["path"], "/")

    def test_login_needs_a_csrf_token(self):
        make_user(password=PASSWORD)
        client = CaddyClient(enforce_csrf_checks=True)
        response = self.log_in("person@example.org", client=client)
        self.assertEqual(response.status_code, 403)

    def test_plain_http_is_redirected(self):
        response = Client().get(reverse("account_login"))
        self.assertEqual(response.status_code, 301)
        self.assertTrue(response.url.startswith("https://"))

    def test_other_hostnames_are_refused(self):
        response = self.client.get(reverse("account_login"), headers={"Host": "evil.example.org"})
        self.assertEqual(response.status_code, 400)

    def test_only_the_app_hostname_is_allowed(self):
        self.assertEqual(settings.CSRF_TRUSTED_ORIGINS, [f"https://{settings.APP_HOST}"])

    def test_security_headers_are_sent(self):
        response = self.client.get(reverse("account_login"))
        self.assertIn("max-age=", response.headers["Strict-Transport-Security"])
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    def test_there_is_no_2fa_bypass_code(self):
        self.assertFalse(hasattr(settings, "MFA_TOTP_INSECURE_BYPASS_CODE"))

    def test_login_limits_survive_a_restart(self):
        self.assertEqual(settings.CACHES["default"]["BACKEND"], "django.core.cache.backends.db.DatabaseCache")

    def test_django_deployment_checks_pass(self):
        call_command("check", "--deploy", "--fail-level", "WARNING", verbosity=0)

    def test_every_migration_is_committed(self):
        call_command("makemigrations", "--check", "--dry-run", verbosity=0)
