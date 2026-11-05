"""The login service for other tools, tested with a fake tool and fake users."""

import base64
import io
import json
from datetime import timedelta
from urllib.parse import parse_qs, urlencode, urlparse

import allauth.idp.urls
import jwt
from allauth.idp.oidc.internal.oauthlib.server import get_server
from allauth.idp.oidc.models import Client, Token
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import Group
from django.core.management import CommandError, call_command
from django.test import override_settings
from django.urls import resolve, reverse
from django.utils import timezone
from sallskap.urls import not_found

from .login_service import SCOPES
from .models import ToolAccess
from .tests import PASSWORD, Base, CaddyClient, User, make_user, totp_code

SIGNING_KEY = (
    rsa.generate_private_key(public_exponent=65537, key_size=2048)
    .private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    .decode()
)
CALLBACK = "https://tool.example.org/callback"
TOKEN_URL = "/identity/o/api/token"
USERINFO_URL = "/identity/o/api/userinfo"
AUTHORIZE_URL = "/identity/o/authorize"
TOOL_SECRET = "the-fake-tools-secret"


def make_tool(name="tool", callback=CALLBACK, secret=TOOL_SECRET):
    tool = Client(name=name, skip_consent=True)
    tool.set_redirect_uris([callback])
    tool.set_scopes(SCOPES)
    tool.secret = make_password(secret, hasher="sallskap_sha256")
    tool.save()
    return tool


@override_settings(IDP_OIDC_PRIVATE_KEY=SIGNING_KEY)
class Service(Base):
    @classmethod
    def setUpTestData(cls):
        cls.tool = make_tool()

    def authorize_url(self, client_id=None, **params):
        defaults = {
            "client_id": client_id or self.tool.id,
            "redirect_uri": CALLBACK,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "state": "state-from-the-tool",
            "nonce": "nonce-from-the-tool",
        }
        return f"{AUTHORIZE_URL}?{urlencode({**defaults, **params})}"

    def sent_back(self, response):
        """Where the person was sent back to the tool with, if they were."""
        if response.status_code != 302 or not response.headers["Location"].startswith(CALLBACK):
            return None
        return {key: values[0] for key, values in parse_qs(urlparse(response.headers["Location"]).query).items()}

    def get_code(self, client=None, **params):
        response = (client or self.client).get(self.authorize_url(**params))
        back = self.sent_back(response)
        self.assertIsNotNone(back, f"not sent back to the tool: {response.status_code} {response.headers.get('Location')}")
        self.assertEqual(back["state"], "state-from-the-tool")
        return back["code"]

    def exchange(self, code, secret=TOOL_SECRET, client_id=None, redirect_uri=CALLBACK):
        """What the tool's server does with the code, straight to Django."""
        credentials = base64.b64encode(f"{client_id or self.tool.id}:{secret}".encode()).decode()
        return self.client.post(
            TOKEN_URL,
            {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
            headers={"Authorization": f"Basic {credentials}"},
        )

    def verified_claims(self, id_token, audience=None):
        """Check the signature the way a tool would: against the published key."""
        published = self.client.get("/.well-known/jwks.json").json()["keys"]
        kid = jwt.get_unverified_header(id_token)["kid"]
        key = next(jwt.PyJWK(k).key for k in published if k["kid"] == kid)
        return jwt.decode(
            id_token,
            key,
            algorithms=["RS256"],
            audience=audience or self.tool.id,
            issuer=f"https://{settings.APP_HOST}",
        )

    def login_claims(self, user, **params):
        self.client.force_login(user)
        response = self.exchange(self.get_code(**params))
        self.assertEqual(response.status_code, 200, response.content)
        return self.verified_claims(response.json()["id_token"])


class Discovery(Service):
    def test_tools_can_find_the_service(self):
        data = self.client.get("/.well-known/openid-configuration").json()
        self.assertEqual(data["issuer"], f"https://{settings.APP_HOST}")
        for name, path in (
            ("authorization_endpoint", AUTHORIZE_URL),
            ("token_endpoint", TOKEN_URL),
            ("userinfo_endpoint", USERINFO_URL),
            ("jwks_uri", "/.well-known/jwks.json"),
        ):
            self.assertTrue(data[name].endswith(path), name)
        self.assertIn("groups", data["scopes_supported"])
        self.assertIn("groups", data["claims_supported"])

    def test_it_only_advertises_what_is_open(self):
        data = self.client.get("/.well-known/openid-configuration").json()
        self.assertNotIn("device_authorization_endpoint", data)
        self.assertNotIn("revocation_endpoint", data)
        self.assertNotIn("registration_endpoint", data)
        self.assertNotIn("introspection_endpoint", data)
        self.assertEqual(data["grant_types_supported"], ["authorization_code"])
        self.assertEqual(data["response_types_supported"], ["code"])
        self.assertNotIn("none", data["token_endpoint_auth_methods_supported"])

    def test_the_public_key_is_published_and_the_private_key_is_not(self):
        keys = self.client.get("/.well-known/jwks.json").json()["keys"]
        self.assertEqual(len(keys), 1)
        self.assertEqual(keys[0]["kty"], "RSA")
        self.assertNotIn("d", keys[0])

    def test_only_the_routes_tools_use_are_open(self):
        """If an allauth upgrade adds a route, this fails instead of the route going live."""

        def routes(patterns, prefix=""):
            for pattern in patterns:
                if hasattr(pattern, "url_patterns"):
                    yield from routes(pattern.url_patterns, prefix + str(pattern.pattern))
                else:
                    yield prefix + str(pattern.pattern)

        open_routes = {route for route in routes(allauth.idp.urls.urlpatterns) if resolve(f"/{route}").func is not not_found}
        self.assertEqual(
            open_routes,
            {
                ".well-known/openid-configuration",
                ".well-known/jwks.json",
                "identity/o/authorize",
                "identity/o/logout",
                "identity/o/api/token",
                "identity/o/api/userinfo",
            },
        )

    def test_closed_routes_are_gone(self):
        for path in (
            "/identity/o/device",
            "/identity/o/api/device/code",
            "/identity/o/api/revoke",
            "/identity/o/api/introspect",
            "/identity/o/api/clients",
            "/identity/o/some-future-route",
        ):
            with self.subTest(path):
                self.assertEqual(self.client.post(path).status_code, 404)


class Login(Service):
    def test_a_visitor_is_asked_to_log_in_then_returned_to_the_tool(self):
        make_user(password=PASSWORD)
        response = self.client.get(self.authorize_url())
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("account_login"), response.url)
        self.assertIsNone(self.sent_back(response))

    def test_after_login_the_person_goes_on_to_the_tool(self):
        make_user(password=PASSWORD)
        login_page = self.client.get(self.authorize_url()).url
        after_login = self.client.post(login_page, {"login": "person@example.org", "password": PASSWORD})
        self.assertIn(AUTHORIZE_URL, after_login.url)
        self.assertIsNotNone(self.sent_back(self.client.get(after_login.url)))

    def test_someone_already_logged_in_sees_no_prompt(self):
        """One login for everything. No login page, and no 'allow this tool?' page."""
        user, _ = make_user()
        self.client.force_login(user)
        self.assertIsNotNone(self.sent_back(self.client.get(self.authorize_url())))

    def test_with_2fa_a_password_alone_does_not_reach_the_tool(self):
        make_user(password=PASSWORD, two_factor=True)
        login_page = self.client.get(self.authorize_url()).url
        after_login = self.client.post(login_page, {"login": "person@example.org", "password": PASSWORD})
        self.assertIn(reverse("mfa_authenticate"), after_login.url)
        self.assertIsNone(self.sent_back(self.client.get(self.authorize_url())))

    def test_with_2fa_the_code_leads_on_to_the_tool(self):
        _, secret = make_user(password=PASSWORD, two_factor=True)
        login_page = self.client.get(self.authorize_url()).url
        self.client.post(login_page, {"login": "person@example.org", "password": PASSWORD})
        after_code = self.enter_code(totp_code(secret))
        self.assertIn(AUTHORIZE_URL, after_code.url)
        self.assertIsNotNone(self.sent_back(self.client.get(after_code.url)))

    def test_asking_for_a_fresh_login_asks_for_the_password_again(self):
        user, _ = make_user(password=PASSWORD)
        self.client.force_login(user)
        response = self.client.get(self.authorize_url(prompt="login"))
        self.assertIn(reverse("account_reauthenticate"), response.url)

    def test_a_silent_check_for_a_visitor_tells_the_tool_no(self):
        back = self.sent_back(self.client.get(self.authorize_url(prompt="none")))
        self.assertEqual(back["error"], "login_required")
        self.assertNotIn("code", back)


class TwoFactorRule(Service):
    def test_groups_needing_2fa_cannot_reach_a_tool_without_it(self):
        for group in ("board", "treasurer", "operator"):
            with self.subTest(group):
                user, _ = make_user(f"{group}@example.org", [group])
                self.client.force_login(user)
                response = self.client.get(self.authorize_url())
                self.assertRedirects(response, reverse("mfa_index"), fetch_redirect_response=False)
                self.assertIsNone(self.sent_back(response))

    def test_a_superuser_cannot_reach_a_tool_without_it(self):
        self.client.force_login(User.objects.create_superuser("root@example.org"))
        self.assertRedirects(self.client.get(self.authorize_url()), reverse("mfa_index"), fetch_redirect_response=False)

    def test_a_silent_check_cannot_get_around_the_rule(self):
        user, _ = make_user(groups=["board"])
        self.client.force_login(user)
        back = self.sent_back(self.client.get(self.authorize_url(prompt="none")))
        self.assertEqual(back["error"], "login_required")
        self.assertNotIn("code", back)

    def test_a_form_post_cannot_get_around_the_rule(self):
        user, _ = make_user(groups=["board"])
        self.client.force_login(user)
        response = self.client.post(self.authorize_url())
        self.assertIsNone(self.sent_back(response))
        self.assertFalse(Token.objects.exists())

    def test_the_same_groups_get_through_with_2fa(self):
        user, _ = make_user(groups=["board"], two_factor=True)
        self.client.force_login(user)
        self.assertIsNotNone(self.sent_back(self.client.get(self.authorize_url())))

    def test_groups_without_the_rule_get_through_without_2fa(self):
        for group in ("member", "developer", "events"):
            with self.subTest(group):
                user, _ = make_user(f"{group}@example.org", [group])
                self.client.force_login(user)
                self.assertIsNotNone(self.sent_back(self.client.get(self.authorize_url())))

    def test_joining_a_group_applies_the_rule_at_the_next_login(self):
        user, _ = make_user(groups=["member"])
        self.client.force_login(user)
        self.assertIsNotNone(self.sent_back(self.client.get(self.authorize_url())))
        user.groups.add(Group.objects.get(role__key="board"))
        self.assertIsNone(self.sent_back(self.client.get(self.authorize_url())))


class Claims(Service):
    def test_the_signed_statement_says_who_it_is(self):
        user, _ = make_user("Anna.Lind@Example.org", ["member"])
        user.name = "Anna Lind"
        user.save()
        claims = self.login_claims(user)
        self.assertEqual(claims["sub"], str(user.pk))
        self.assertEqual(claims["email"], "anna.lind@example.org")
        self.assertIs(claims["email_verified"], False)
        self.assertEqual(claims["name"], "Anna Lind")
        self.assertEqual(claims["preferred_username"], "anna.lind")
        self.assertEqual(claims["nonce"], "nonce-from-the-tool")

    def test_two_people_with_the_same_name_get_different_usernames(self):
        """Forgejo allows one account per name, so the second person would get an error page."""
        first, _ = make_user("anna@example.org")
        second, _ = make_user("anna@example.net")
        self.assertEqual(self.login_claims(first)["preferred_username"], "anna")
        self.assertEqual(self.login_claims(second)["preferred_username"], f"anna-{second.pk}")

    def test_a_longer_address_does_not_count_as_the_same_name(self):
        make_user("anna.lind@example.org")
        make_user("xanna@example.org")
        user, _ = make_user("anna@example.org")
        self.assertEqual(self.login_claims(user)["preferred_username"], "anna")

    def test_it_passes_on_each_persons_groups(self):
        user, _ = make_user(groups=["board", "treasurer"], two_factor=True)
        self.assertEqual(self.login_claims(user)["groups"], ["board", "treasurer"])

    def test_someone_with_no_groups_gets_an_empty_list(self):
        user, _ = make_user()
        self.assertEqual(self.login_claims(user)["groups"], [])

    def test_renaming_a_group_cannot_break_a_tools_rules(self):
        Group.objects.filter(role__key="board").update(name="Styrelse")
        user, _ = make_user(groups=["board"], two_factor=True)
        self.assertEqual(self.login_claims(user)["groups"], ["board"])

    def test_a_group_an_operator_added_goes_by_its_name(self):
        user, _ = make_user(groups=["member"])
        user.groups.add(Group.objects.create(name="Robotics"))
        self.assertEqual(self.login_claims(user)["groups"], ["custom:Robotics", "member"])

    def test_a_group_an_operator_added_cannot_pass_for_a_built_in_one(self):
        """It wouldn't carry the 2FA rule, but a tool would treat its members as Operators."""
        user, _ = make_user()
        user.groups.add(Group.objects.create(name="operator"))
        self.assertFalse(user.requires_2fa)
        self.assertEqual(self.login_claims(user)["groups"], ["custom:operator"])

    def test_a_tool_only_gets_what_it_asked_for(self):
        user, _ = make_user(groups=["member"])
        claims = self.login_claims(user, scope="openid")
        self.assertEqual(claims["sub"], str(user.pk))
        for name in ("email", "name", "preferred_username", "groups"):
            self.assertNotIn(name, claims)

    def test_group_changes_show_at_the_next_login(self):
        user, _ = make_user(groups=["member"])
        self.assertEqual(self.login_claims(user)["groups"], ["member"])
        user.groups.set(Group.objects.filter(role__key="developer"))
        self.assertEqual(self.login_claims(user)["groups"], ["developer"])
        user.groups.clear()
        self.assertEqual(self.login_claims(user)["groups"], [])

    def test_a_statement_altered_in_transit_is_refused(self):
        user, _ = make_user(groups=["member"])
        self.client.force_login(user)
        id_token = self.exchange(self.get_code()).json()["id_token"]
        header, payload, signature = id_token.split(".")
        forged = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        forged["groups"] = ["operator"]
        forged_payload = base64.urlsafe_b64encode(json.dumps(forged).encode()).rstrip(b"=").decode()
        with self.assertRaises(jwt.InvalidSignatureError):
            self.verified_claims(f"{header}.{forged_payload}.{signature}")


class Tokens(Service):
    def test_a_tool_can_ask_who_someone_is_with_its_access_token(self):
        user, _ = make_user(groups=["member"])
        self.client.force_login(user)
        access_token = self.exchange(self.get_code()).json()["access_token"]
        response = CaddyClient().get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["groups"], ["member"])
        self.assertEqual(response.json()["email"], "person@example.org")

    def test_asking_with_no_token_is_refused(self):
        self.assertEqual(self.client.get(USERINFO_URL).status_code, 401)

    def test_tokens_in_the_address_are_refused_as_they_end_up_in_logs(self):
        user, _ = make_user()
        self.client.force_login(user)
        access_token = self.exchange(self.get_code()).json()["access_token"]
        self.assertEqual(CaddyClient().get(f"{USERINFO_URL}?access_token={access_token}").status_code, 401)

    def test_a_tool_gets_no_refresh_token(self):
        """Otherwise a tool could keep getting new tokens for someone long after
        they'd left, without them ever logging in."""
        user, _ = make_user()
        self.client.force_login(user)
        data = self.exchange(self.get_code()).json()
        self.assertNotIn("refresh_token", data)
        self.assertIn("access_token", data)
        self.assertIn("id_token", data)
        self.assertFalse(Token.objects.filter(type=Token.Type.REFRESH_TOKEN).exists())

    def test_a_refresh_token_would_be_refused_anyway(self):
        """A second line of defence: a real one, made here, still can't be used."""
        user, _ = make_user()
        token_value = "a-real-refresh-token"
        refresh = Token(client=self.tool, user=user, type=Token.Type.REFRESH_TOKEN)
        refresh.set_value(token_value)
        refresh.set_scopes(SCOPES)
        refresh.save()
        credentials = base64.b64encode(f"{self.tool.id}:{TOOL_SECRET}".encode()).decode()
        response = self.client.post(
            TOKEN_URL,
            {"grant_type": "refresh_token", "refresh_token": token_value},
            headers={"Authorization": f"Basic {credentials}"},
        )
        self.assertIn(response.status_code, (400, 401))
        self.assertNotIn("access_token", response.json())

    def test_the_library_still_issues_a_refresh_token_or_this_view_can_go(self):
        self.assertTrue(get_server().auth_grant.refresh_token)

    def test_the_access_token_is_short_lived(self):
        user, _ = make_user()
        self.client.force_login(user)
        self.exchange(self.get_code())
        token = Token.objects.get(type=Token.Type.ACCESS_TOKEN)
        self.assertLess(token.expires_at, timezone.now() + timedelta(minutes=11))

    def test_tokens_are_stored_hashed(self):
        user, _ = make_user()
        self.client.force_login(user)
        access_token = self.exchange(self.get_code()).json()["access_token"]
        self.assertFalse(Token.objects.filter(hash=access_token).exists())

    def test_a_code_works_only_once(self):
        user, _ = make_user()
        self.client.force_login(user)
        code = self.get_code()
        self.assertEqual(self.exchange(code).status_code, 200)
        self.assertEqual(self.exchange(code).status_code, 400)

    def test_a_turned_off_account_cannot_get_tokens_or_use_them(self):
        user, _ = make_user()
        self.client.force_login(user)
        code, second_code = self.get_code(), self.get_code()
        access_token = self.exchange(code).json()["access_token"]
        user.is_active = False
        user.save()
        self.assertEqual(self.exchange(second_code).status_code, 400)
        response = CaddyClient().get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
        self.assertEqual(response.status_code, 401)
        self.assertIsNone(self.sent_back(self.client.get(self.authorize_url())))


class Refusals(Service):
    def setUp(self):
        user, _ = make_user()
        self.client.force_login(user)

    def test_a_wrong_secret_is_refused(self):
        self.assertEqual(self.exchange(self.get_code(), secret="not-the-secret").status_code, 401)

    def test_the_wrong_tool_cannot_use_a_code(self):
        other = make_tool("other", "https://other.example.org/callback", "the-other-secret")
        response = self.exchange(self.get_code(), secret="the-other-secret", client_id=other.id)
        self.assertEqual(response.status_code, 400)

    def test_a_code_needs_the_address_it_was_issued_for(self):
        response = self.exchange(self.get_code(), redirect_uri="https://tool.example.org/other")
        self.assertEqual(response.status_code, 400)

    def test_an_unknown_tool_is_refused_with_no_redirect(self):
        response = self.client.get(self.authorize_url(client_id="no-such-tool"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(CALLBACK, response.headers.get("Location", ""))
        self.assertContains(response, "Error")

    def test_an_unregistered_return_address_is_refused_with_no_redirect(self):
        for address in (
            "https://evil.example.org/callback",
            f"{CALLBACK}/extra",
            "https://tool.example.org/",
            f"{CALLBACK}?next=https://evil.example.org/",
            f"{CALLBACK}?",
            f"{CALLBACK}#fragment",
        ):
            with self.subTest(address):
                response = self.client.get(self.authorize_url(redirect_uri=address))
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("Location", response.headers)

    def test_a_wrong_return_address_is_refused_before_anyone_logs_in(self):
        visitor = CaddyClient(ip="198.51.100.7")
        response = visitor.get(self.authorize_url(redirect_uri=f"{CALLBACK}?next=https://evil.example.org/"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Location", response.headers)

    def test_a_scope_the_tool_is_not_allowed_is_refused(self):
        back = self.sent_back(self.client.get(self.authorize_url(scope="openid admin")))
        self.assertEqual(back["error"], "invalid_scope")

    def test_the_older_less_safe_flows_are_refused(self):
        for response_type in ("token", "id_token", "id_token token", "code token"):
            with self.subTest(response_type):
                back = self.sent_back(self.client.get(self.authorize_url(response_type=response_type)))
                if back is not None:
                    self.assertIn("error", back)
                self.assertFalse(Token.objects.filter(type=Token.Type.ACCESS_TOKEN).exists())

    def test_logging_in_with_a_password_at_the_token_address_is_refused(self):
        """This would skip 2FA altogether."""
        credentials = base64.b64encode(f"{self.tool.id}:{TOOL_SECRET}".encode()).decode()
        for grant in (
            {"grant_type": "password", "username": "person@example.org", "password": PASSWORD},
            {"grant_type": "client_credentials"},
            {"grant_type": "refresh_token", "refresh_token": "anything"},
        ):
            with self.subTest(grant["grant_type"]):
                response = self.client.post(TOKEN_URL, grant, headers={"Authorization": f"Basic {credentials}"})
                self.assertIn(response.status_code, (400, 401))
        self.assertFalse(Token.objects.exists())

    def test_a_tool_with_no_secret_is_refused(self):
        response = self.client.post(
            TOKEN_URL,
            {"grant_type": "authorization_code", "code": self.get_code(), "redirect_uri": CALLBACK, "client_id": self.tool.id},
        )
        self.assertEqual(response.status_code, 401)

    def test_a_tool_secret_can_be_checked_without_argon2(self):
        """Anyone can try the token address, so a guess mustn't cost 100 MB."""
        encoded = make_password("a-secret", hasher="sallskap_sha256")
        self.assertTrue(encoded.startswith("sallskap_sha256$"))
        self.assertNotIn("a-secret", encoded)
        self.assertTrue(check_password("a-secret", encoded))
        self.assertFalse(check_password("a-guess", encoded))

    def test_the_cheap_check_is_never_used_for_a_persons_password(self):
        user, _ = make_user("someone@example.org", password=PASSWORD)
        self.assertTrue(user.password.startswith("argon2"))


class Logout(Service):
    def test_a_tool_can_send_someone_to_log_out_and_they_are_asked_first(self):
        user, _ = make_user()
        self.client.force_login(user)
        response = self.client.get("/identity/o/logout")
        self.assertEqual(response.status_code, 200)
        self.assert_logged_in()


class ToolCommand(Service):
    """The commands behind `kit tool`."""

    def run_command(self, *args):
        output = io.StringIO()
        call_command("tool", *args, stdout=output)
        return output.getvalue()

    def read(self, output, label):
        return next(line.split(":", 1)[1].strip() for line in output.splitlines() if line.strip().startswith(label))


class Tools(ToolCommand):
    def test_adding_a_tool_prints_its_settings_and_the_secret_works(self):
        output = self.run_command("add", "git", "https://git.example.org/callback")
        self.assertEqual(self.read(output, "Issuer"), f"https://{settings.APP_HOST}")
        client_id, secret = self.read(output, "Client ID"), self.read(output, "Secret")
        tool = Client.objects.get(id=client_id)
        self.assertEqual(tool.name, "git")
        self.assertTrue(tool.check_secret(secret))

    def test_the_secret_is_not_stored_as_written(self):
        secret = self.read(self.run_command("add", "git", "https://git.example.org/callback"), "Secret")
        self.assertNotIn(secret, Client.objects.get(name="git").secret)

    def test_the_secret_is_long_and_checked_without_argon2(self):
        """Anyone can try the token address, so a guess mustn't cost 100 MB."""
        secret = self.read(self.run_command("add", "git", "https://git.example.org/callback"), "Secret")
        self.assertGreaterEqual(len(secret), 60)
        self.assertTrue(Client.objects.get(name="git").secret.startswith("sallskap_sha256$"))

    def test_a_tool_added_this_way_can_log_someone_in(self):
        output = self.run_command("add", "git", CALLBACK)
        client_id, secret = self.read(output, "Client ID"), self.read(output, "Secret")
        user, _ = make_user(groups=["member"])
        self.client.force_login(user)
        response = self.exchange(self.get_code(client_id=client_id), secret=secret, client_id=client_id)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.verified_claims(response.json()["id_token"], audience=client_id)["groups"], ["member"])

    def test_a_new_tool_is_limited_to_the_safe_flow(self):
        self.run_command("add", "git", "https://git.example.org/callback")
        tool = Client.objects.get(name="git")
        self.assertEqual(tool.get_grant_types(), ["authorization_code"])
        self.assertEqual(tool.get_response_types(), ["code"])
        self.assertEqual(tool.type, Client.Type.CONFIDENTIAL)
        self.assertEqual(tool.get_redirect_uris(), ["https://git.example.org/callback"])
        self.assertFalse(tool.allow_uri_wildcards)
        self.assertTrue(tool.skip_consent)
        self.assertEqual(set(tool.get_scopes()), set(SCOPES))

    def test_adding_a_tool_twice_changes_nothing(self):
        self.run_command("add", "git", "https://git.example.org/callback")
        before = Client.objects.get(name="git")
        with self.assertRaisesRegex(CommandError, "already"):
            self.run_command("add", "git", "https://other.example.org/callback")
        after = Client.objects.get(name="git")
        self.assertEqual((after.id, after.secret, after.redirect_uris), (before.id, before.secret, before.redirect_uris))

    def test_a_return_address_must_be_a_full_https_address(self):
        for address in (
            "http://git.example.org/callback",
            "git.example.org",
            "https://*.example.org/x",
            "https://git.example.org/#x",
            "https://user:password@git.example.org/callback",
        ):
            with self.subTest(address), self.assertRaisesRegex(CommandError, "https"):
                self.run_command("add", "git", address)
        self.assertFalse(Client.objects.filter(name="git").exists())

    def test_listing_shows_tools_and_no_secrets(self):
        secret = self.read(self.run_command("add", "git", "https://git.example.org/callback"), "Secret")
        output = self.run_command("list")
        self.assertIn("git", output)
        self.assertIn("https://git.example.org/callback", output)
        self.assertNotIn(secret, output)

    def test_removing_a_tool_ends_its_access(self):
        user, _ = make_user()
        self.client.force_login(user)
        access_token = self.exchange(self.get_code()).json()["access_token"]
        self.run_command("remove", "tool")
        self.assertFalse(Client.objects.filter(name="tool").exists())
        response = CaddyClient().get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
        self.assertEqual(response.status_code, 401)

    def test_removing_an_unknown_tool_is_refused(self):
        with self.assertRaisesRegex(CommandError, "No tool"):
            self.run_command("remove", "nope")


class ToolGroups(Service):
    """A tool limited to certain groups, as Vaultwarden is. It can't check for itself."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        ToolAccess.objects.create(tool=cls.tool, group_keys=["board", "operator"])

    def test_someone_in_a_listed_group_gets_through(self):
        for group in ("board", "operator"):
            with self.subTest(group):
                user, _ = make_user(f"{group}@example.org", [group], two_factor=True)
                self.assertEqual(self.login_claims(user)["sub"], str(user.pk))

    def test_someone_with_another_group_is_refused_with_no_code(self):
        for groups in (["member"], ["developer"], ["treasurer"], []):
            with self.subTest(groups):
                user, _ = make_user(f"{'-'.join(groups) or 'nobody'}@example.org", groups, two_factor=True)
                self.client.force_login(user)
                response = self.client.get(self.authorize_url())
                self.assertEqual(response.status_code, 403)
                self.assertContains(response, "access to tool", status_code=403)
                self.assertIsNone(self.sent_back(response))

    def test_a_silent_check_tells_the_tool_no(self):
        user, _ = make_user(groups=["member"])
        self.client.force_login(user)
        back = self.sent_back(self.client.get(self.authorize_url(prompt="none")))
        self.assertEqual(back["error"], "login_required")
        self.assertNotIn("code", back)

    def test_a_form_post_cannot_get_around_the_check(self):
        user, _ = make_user(groups=["member"])
        self.client.force_login(user)
        response = self.client.post(self.authorize_url(), follow=True)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Token.objects.exists())

    def test_a_visitor_logs_in_first_and_is_refused_after(self):
        user, _ = make_user(groups=["member"])
        response = self.client.get(self.authorize_url())
        self.assertIn(reverse("account_login"), response.headers["Location"])
        self.client.force_login(user)
        self.assertEqual(self.client.get(self.authorize_url()).status_code, 403)

    def test_being_refused_comes_before_being_asked_to_set_up_2fa(self):
        user, _ = make_user(groups=["treasurer"])
        self.client.force_login(user)
        self.assertEqual(self.client.get(self.authorize_url()).status_code, 403)

    def test_a_listed_group_still_needs_2fa(self):
        user, _ = make_user(groups=["board"])
        self.client.force_login(user)
        self.assertRedirects(self.client.get(self.authorize_url()), reverse("mfa_index"), fetch_redirect_response=False)

    def test_a_superuser_outside_the_groups_is_refused(self):
        user, _ = make_user(two_factor=True)
        user.is_superuser = True
        user.save()
        self.client.force_login(user)
        self.assertEqual(self.client.get(self.authorize_url()).status_code, 403)

    def test_a_custom_group_named_after_a_listed_one_is_refused(self):
        """It goes by custom:board, so this is the rule that protects the group list."""
        user, _ = make_user()
        user.groups.add(Group.objects.create(name="board"))
        self.client.force_login(user)
        self.assertEqual(self.client.get(self.authorize_url()).status_code, 403)

    def test_renaming_a_listed_group_keeps_its_people_in(self):
        user, _ = make_user(groups=["board"], two_factor=True)
        Group.objects.filter(role__key="board").update(name="Styrelsen")
        self.assertEqual(self.login_claims(user)["sub"], str(user.pk))

    def test_joining_a_listed_group_gives_access_at_the_next_login(self):
        user, _ = make_user(groups=["member"], two_factor=True)
        self.client.force_login(user)
        self.assertEqual(self.client.get(self.authorize_url()).status_code, 403)
        user.groups.add(Group.objects.get(role__key="board"))
        self.assertIsNotNone(self.sent_back(self.client.get(self.authorize_url())))

    def test_leaving_the_groups_stops_a_code_already_issued(self):
        user, _ = make_user(groups=["board"], two_factor=True)
        self.client.force_login(user)
        code = self.get_code()
        user.groups.clear()
        response = self.exchange(code)
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("access_token", response.json())

    def test_leaving_the_groups_stops_a_token_already_issued(self):
        user, _ = make_user(groups=["board"], two_factor=True)
        self.client.force_login(user)
        access_token = self.exchange(self.get_code()).json()["access_token"]
        asking = CaddyClient()
        self.assertEqual(asking.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}).status_code, 200)
        user.groups.clear()
        self.assertEqual(asking.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}).status_code, 401)

    def test_a_tool_with_no_list_stays_open_to_every_account(self):
        other = make_tool("other", "https://other.example.org/callback")
        user, _ = make_user(groups=["member"])
        self.client.force_login(user)
        response = self.client.get(self.authorize_url(client_id=other.id, redirect_uri="https://other.example.org/callback"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].startswith("https://other.example.org/callback"))

    def test_an_empty_list_lets_nobody_in(self):
        ToolAccess.objects.filter(tool=self.tool).update(group_keys=[])
        user, _ = make_user(groups=["board"], two_factor=True)
        self.client.force_login(user)
        self.assertEqual(self.client.get(self.authorize_url()).status_code, 403)

    def test_removing_a_tool_removes_its_list(self):
        self.tool.delete()
        self.assertFalse(ToolAccess.objects.exists())

    def seconds_until_expiry(self, data):
        token = Token.objects.by_value(data["access_token"]).get()
        return (token.expires_at - timezone.now()).total_seconds()

    def test_a_token_lasts_the_tools_session_length(self):
        ToolAccess.objects.filter(tool=self.tool).update(session_hours=12)
        user, _ = make_user(groups=["board"], two_factor=True)
        self.client.force_login(user)
        data = self.exchange(self.get_code()).json()
        self.assertEqual(data["expires_in"], 12 * 3600)
        self.assertAlmostEqual(self.seconds_until_expiry(data), 12 * 3600, delta=30)

    def test_with_no_session_length_a_token_is_short_lived(self):
        user, _ = make_user(groups=["board"], two_factor=True)
        self.client.force_login(user)
        data = self.exchange(self.get_code()).json()
        self.assertEqual(data["expires_in"], 600)
        self.assertAlmostEqual(self.seconds_until_expiry(data), 600, delta=30)

    def test_another_tools_session_length_does_not_change_this_ones(self):
        ToolAccess.objects.filter(tool=self.tool).update(session_hours=12)
        other = make_tool("other", "https://other.example.org/callback")
        user, _ = make_user(groups=["member"])
        self.client.force_login(user)
        response = self.client.get(self.authorize_url(client_id=other.id, redirect_uri="https://other.example.org/callback"))
        code = parse_qs(urlparse(response.headers["Location"]).query)["code"][0]
        data = self.exchange(code, client_id=other.id, redirect_uri="https://other.example.org/callback").json()
        self.assertEqual(data["expires_in"], 600)


class ToolGroupCommands(ToolCommand):
    def access_of(self, name):
        return Client.objects.get(name=name).access.group_keys

    def test_adding_a_tool_with_groups_limits_it(self):
        self.run_command("add", "vault", "https://vault.example.org/callback", "--group=board", "--group=operator")
        self.assertEqual(self.access_of("vault"), ["board", "operator"])

    def test_adding_a_tool_with_no_groups_leaves_it_open(self):
        self.run_command("add", "git", "https://git.example.org/callback")
        self.assertFalse(ToolAccess.objects.filter(tool__name="git").exists())

    def test_a_custom_group_goes_by_its_marked_name(self):
        Group.objects.create(name="Alumni")
        self.run_command("add", "vault", "https://vault.example.org/callback", "--group=custom:Alumni")
        self.assertEqual(self.access_of("vault"), ["custom:Alumni"])

    def test_a_group_that_does_not_exist_is_refused_with_no_tool_left_behind(self):
        for group in ("bord", "custom:Nobody", "custom:board", ""):
            with self.subTest(group), self.assertRaisesRegex(CommandError, "No such group"):
                self.run_command("add", "vault", "https://vault.example.org/callback", f"--group={group}")
        self.assertFalse(Client.objects.filter(name="vault").exists())

    def test_listing_shows_who_can_use_each_tool(self):
        self.run_command("add", "vault", "https://vault.example.org/callback", "--group=board", "--group=operator")
        self.run_command("add", "git", "https://git.example.org/callback")
        output = self.run_command("list")
        self.assertIn("https://vault.example.org/callback  (board, operator)", output)
        self.assertIn("https://git.example.org/callback  (any account)", output)

    def test_a_limited_tool_can_have_longer_sessions(self):
        self.run_command("add", "vault", "https://vault.example.org/callback", "--group=board", "--session-hours=12")
        self.assertEqual(Client.objects.get(name="vault").access.session_hours, 12)
        self.assertIn("(board, 12 hour sessions)", self.run_command("list"))

    def test_only_a_limited_tool_can_have_longer_sessions(self):
        with self.assertRaisesRegex(CommandError, "limited"):
            self.run_command("add", "vault", "https://vault.example.org/callback", "--session-hours=12")
        self.assertFalse(Client.objects.filter(name="vault").exists())

    def test_a_session_can_last_at_most_a_day(self):
        for hours in (0, 25, -1):
            with self.subTest(hours), self.assertRaisesRegex(CommandError, "1 to 24"):
                self.run_command("add", "vault", "https://vault.example.org/callback", "--group=board", f"--session-hours={hours}")
        self.assertFalse(Client.objects.filter(name="vault").exists())


class OperatorsAndTools(Service):
    def test_only_the_kit_manages_tools(self):
        """Adding a tool decides who gets people's details, so it stays with root."""
        user, _ = make_user(groups=["operator"], two_factor=True)
        self.client.force_login(user)
        self.assertEqual(self.client.get("/admin/allauth_idp_oidc/client/").status_code, 403)
        self.assertEqual(self.client.get("/admin/allauth_idp_oidc/token/").status_code, 403)
