"""The connectors that tell tools about changes, against made-up tools that record what
they were asked. tests/live_check.py runs them against the real ones."""

import importlib.util
import io
import json
import sys
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


def load(name):
    spec = importlib.util.spec_from_file_location(f"connector_{name}", ROOT / "modules" / name / "connector.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


git = load("git")
vault = load("vault")


class Answer(io.BytesIO):
    def __init__(self, status, body=b"", cookies=()):
        super().__init__(body)
        self.status = status
        self.headers = Message()
        for cookie in cookies:
            self.headers["Set-Cookie"] = cookie


def person(sub="7", allowed=True, groups=(), email="someone@example.org"):
    return SimpleNamespace(sub=sub, allowed=allowed, groups=tuple(groups), email=email)


class FakeForgejo:
    """Just enough of its API, holding what the real one would."""

    def __init__(self):
        self.account = {"id": 42, "login": "someone", "login_name": "7", "source_id": 1, "prohibit_login": False, "is_admin": False}
        self.teams = {"Owners": 1, "developers": 2, "board": 3, "a-project": 4}
        self.members = {2}
        self.repos = [("example", "made-by-them"), ("example", "someone-elses"), ("someone", "their-own")]
        self.collaborators = {"made-by-them"}
        self.calls = []
        self.broken = None

    def writes(self):
        return [(method, path) for method, path, _ in self.calls if method != "GET"]

    def __call__(self, request, timeout=None):
        path = request.full_url.removeprefix(git.API)
        method = request.get_method()
        body = json.loads(request.data) if request.data else None
        self.calls.append((method, path, body))
        if self.broken and self.broken in path:
            raise urllib.error.HTTPError(request.full_url, 500, "broken", Message(), None)
        status, content = self.answer(method, path, body)
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "no", Message(), None)
        return Answer(status, json.dumps(content).encode() if content is not None else b"")

    def answer(self, method, path, body):
        if path.startswith("/admin/users?login_name="):
            sub = path.split("=")[1].split("&")[0]
            return 200, [self.account] if self.account and self.account["login_name"] == sub else []
        if path.startswith("/admin/users/someone") and method == "PATCH":
            self.account = {**self.account, **{("is_admin" if k == "admin" else k): v for k, v in body.items()}}
            return 200, self.account
        if path.startswith("/orgs/example/teams"):
            return 200, [{"id": number, "name": name} for name, number in self.teams.items()]
        if path.startswith("/teams/"):
            team = int(path.split("/")[2])
            if method == "GET":
                return (200, {}) if team in self.members else (404, None)
            (self.members.add if method == "PUT" else self.members.discard)(team)
            return 204, None
        if path.startswith("/repos/search"):
            return 200, {"data": [{"owner": {"login": owner}, "name": name} for owner, name in self.repos]}
        if "/collaborators/" in path:
            repo = path.split("/")[3]
            if method == "GET":
                return (204, None) if repo in self.collaborators else (404, None)
            self.collaborators.discard(repo)
            return 204, None
        return 404, None


class Forgejo(unittest.TestCase):
    def setUp(self):
        self.forgejo = FakeForgejo()
        patcher = mock.patch.object(git.urllib.request, "urlopen", self.forgejo)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.connector = git.Connector({"FORGEJO_ADMIN_TOKEN": "the-token", "SALLSKAP_DOMAIN": "example.org"})

    def apply(self, **kwargs):
        self.connector.apply(person(**kwargs), lambda key, text: self.fail("Forgejo has nothing for a person to do"))

    def test_nothing_happens_for_someone_who_has_never_signed_in(self):
        self.apply(sub="999", groups=["developer"])
        self.assertEqual(self.forgejo.writes(), [])

    def test_it_changes_nothing_for_someone_already_right(self):
        self.apply(groups=["developer"])
        self.assertEqual(self.forgejo.writes(), [])

    def test_leaving_blocks_them_first_of_all(self):
        self.apply(allowed=False, groups=["developer"])
        self.assertEqual(self.forgejo.writes()[0], ("PATCH", "/admin/users/someone"))
        self.assertTrue(self.forgejo.account["prohibit_login"])

    def test_every_edit_sends_what_forgejo_requires(self):
        self.apply(allowed=False)
        body = next(body for method, _, body in self.forgejo.calls if method == "PATCH")
        self.assertEqual((body["login_name"], body["source_id"]), ("7", 1))

    def test_leaving_takes_them_off_every_team_including_ones_not_in_the_map(self):
        self.forgejo.members = {1, 2, 3, 4}
        self.apply(allowed=False)
        self.assertEqual(self.forgejo.members, set())

    def test_leaving_removes_collaborations(self):
        self.apply(allowed=False)
        self.assertEqual(self.forgejo.collaborators, set())

    def test_leaving_ends_admin_rights(self):
        self.forgejo.account["is_admin"] = True
        self.apply(allowed=False)
        self.assertFalse(self.forgejo.account["is_admin"])

    def test_someone_coming_back_is_unblocked_and_gets_their_team(self):
        self.forgejo.account["prohibit_login"] = True
        self.forgejo.members = set()
        self.apply(groups=["developer"])
        self.assertFalse(self.forgejo.account["prohibit_login"])
        self.assertEqual(self.forgejo.members, {2})

    def test_leaving_a_group_takes_its_team_and_leaves_the_account_on(self):
        self.apply(groups=["member"])
        self.assertEqual(self.forgejo.members, set())
        self.assertFalse(self.forgejo.account["prohibit_login"])

    def test_leaving_a_group_leaves_teams_not_in_the_map_alone(self):
        """They'll be for projects, which core will manage some other way."""
        self.forgejo.members = {2, 4}
        self.apply(groups=["member"])
        self.assertEqual(self.forgejo.members, {4})

    def test_leaving_a_group_removes_collaborations_on_repos_they_created(self):
        self.apply(groups=["member"])
        self.assertEqual(self.forgejo.collaborators, set())

    def test_moving_to_a_group_that_only_reads_removes_them_too(self):
        self.apply(groups=["board"])
        self.assertEqual(self.forgejo.collaborators, set())
        self.assertEqual(self.forgejo.members, {3})

    def test_developers_and_operators_stay_collaborators(self):
        for groups in (["developer"], ["operator"]):
            self.forgejo.collaborators = {"made-by-them"}
            self.apply(groups=groups)
            self.assertEqual(self.forgejo.collaborators, {"made-by-them"})

    def test_operators_are_admins_and_others_arent(self):
        self.apply(groups=["operator"])
        self.assertTrue(self.forgejo.account["is_admin"])
        self.assertIn(1, self.forgejo.members)
        self.apply(groups=["developer"])
        self.assertFalse(self.forgejo.account["is_admin"])
        self.assertNotIn(1, self.forgejo.members)

    def test_it_only_removes_them_as_a_collaborator_where_they_are_one(self):
        self.apply(groups=["member"])
        removed = [path for method, path in self.forgejo.writes() if "collaborators" in path]
        self.assertEqual(removed, ["/repos/example/made-by-them/collaborators/someone"])

    def test_a_repo_in_their_own_name_stays_theirs(self):
        self.forgejo.collaborators = {"their-own"}
        self.apply(groups=["member"])
        self.assertEqual(self.forgejo.collaborators, {"their-own"})

    def test_it_reads_every_page(self):
        page_one = [("example", f"repo-{n}") for n in range(50)]
        self.forgejo.repos = page_one
        answer = self.forgejo.answer

        def paged(method, path, body):
            if path.startswith("/repos/search") and "page=2" in path:
                return 200, {"data": [{"owner": {"login": "example"}, "name": "on-the-second-page"}]}
            return answer(method, path, body)

        self.forgejo.answer = paged
        self.forgejo.collaborators = {"on-the-second-page"}
        self.apply(groups=["member"])
        self.assertEqual(self.forgejo.collaborators, set())

    def test_a_token_that_doesnt_work_is_an_error_and_not_nothing_to_do(self):
        self.forgejo.broken = "/admin/users"
        with self.assertRaisesRegex(RuntimeError, "500"):
            self.apply(allowed=False)

    def test_an_error_partway_is_an_error_so_it_gets_tried_again(self):
        self.forgejo.broken = "/collaborators/"
        with self.assertRaisesRegex(RuntimeError, "500"):
            self.apply(allowed=False)

    def test_an_error_never_shows_the_token(self):
        self.forgejo.broken = "/admin/users"
        with self.assertRaises(RuntimeError) as caught:
            self.apply(allowed=False)
        self.assertNotIn("the-token", str(caught.exception))

    def test_the_account_has_to_come_from_core(self):
        """The organisation has a login_name too. It can't be mistaken for a person."""
        self.forgejo.account["source_id"] = 0
        self.apply(allowed=False)
        self.assertEqual(self.forgejo.writes(), [])

    def test_a_name_with_odd_characters_stays_in_its_place_in_the_address(self):
        self.forgejo.account["login"] = "some/../one"
        self.forgejo.answer = lambda method, path, body: (200, [self.forgejo.account]) if "login_name" in path else (200, self.forgejo.account) if method == "PATCH" else (200, [])
        self.apply(allowed=False)
        self.assertIn(("PATCH", "/admin/users/some%2F..%2Fone"), self.forgejo.writes())


OVERVIEW = """
<table id="users-table"><thead><tr><th class="vw-account-details">User</th></tr></thead>
<tbody>{rows}</tbody></table>
"""
ROW = """<tr><td><span class="d-block">{email}</span></td>
<td><span class="d-block text-break text-wrap">{identifier}</span></td>
<td><span data-vw-user-uuid="{uuid}" data-vw-user-email="{email}"></span></td></tr>"""


class FakeVaultwarden:
    def __init__(self):
        self.people = {}
        self.calls = []
        self.logins = 0
        self.valid_cookie = "VW_ADMIN=good"
        self.status_for = {}

    def add(self, uuid, sub, email, enabled=True, organisations=()):
        self.people[uuid] = {"id": uuid, "sub": sub, "email": email, "userEnabled": enabled, "organizations": list(organisations)}

    def __call__(self, request, timeout=None):
        path = request.full_url.removeprefix(vault.ADMIN)
        method = request.get_method()
        if path == "":
            self.logins += 1
            if request.data != b"token=the-token":
                raise urllib.error.HTTPError(request.full_url, 401, "no", Message(), None)
            return Answer(200, b"page", [f"{self.valid_cookie}; Path=/admin; HttpOnly"])
        self.calls.append((method, path))
        if request.get_header("Cookie") != self.valid_cookie:
            raise urllib.error.HTTPError(request.full_url, 401, "no", Message(), None)
        if path in self.status_for:
            raise urllib.error.HTTPError(request.full_url, self.status_for[path], "no", Message(), None)
        if path == "/users/overview":
            rows = "".join(
                ROW.format(email=p["email"], uuid=uuid, identifier=f"https:&#x2F;&#x2F;app.example.org&#x2F;{p['sub']}" if p["sub"] else "")
                for uuid, p in self.people.items()
            )
            return Answer(200, OVERVIEW.format(rows=rows).encode())
        if path == "/users":
            return Answer(200, json.dumps(list(self.people.values())).encode())
        action = path.rsplit("/", 1)[1]
        uuid = path.split("/")[2]
        self.people[uuid]["userEnabled"] = action == "enable"
        return Answer(200)

    def writes(self):
        return [(method, path) for method, path in self.calls if method != "GET"]


def org(identifier="org-1", name="Shared passwords", kind=2):
    return {"id": identifier, "name": name, "type": kind}


class Vaultwarden(unittest.TestCase):
    def setUp(self):
        self.vaultwarden = FakeVaultwarden()
        patcher = mock.patch.object(vault.urllib.request, "urlopen", self.vaultwarden)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.connector = vault.Connector({"VAULT_ADMIN_TOKEN": "the-token", "SALLSKAP_DOMAIN": "example.org"})
        self.notes = []

    def apply(self, **kwargs):
        self.connector.apply(person(**kwargs), lambda key, text: self.notes.append((key, text)))

    def test_nothing_happens_for_someone_who_has_never_signed_in(self):
        self.vaultwarden.add("u1", "8", "other@example.org")
        self.apply(allowed=False)
        self.assertEqual(self.vaultwarden.writes(), [])

    def test_someone_allowed_is_left_alone(self):
        self.vaultwarden.add("u1", "7", "someone@example.org")
        self.apply()
        self.assertEqual(self.vaultwarden.writes(), [])

    def test_someone_who_has_left_is_turned_off(self):
        self.vaultwarden.add("u1", "7", "someone@example.org")
        self.apply(allowed=False)
        self.assertEqual(self.vaultwarden.writes(), [("POST", "/users/u1/disable")])

    def test_turning_someone_off_twice_doesnt_do_it_twice(self):
        """It resets their security stamp, and someone may have just come back."""
        self.vaultwarden.add("u1", "7", "someone@example.org", enabled=False)
        self.apply(allowed=False)
        self.assertEqual(self.vaultwarden.writes(), [])

    def test_someone_coming_back_is_turned_on(self):
        self.vaultwarden.add("u1", "7", "someone@example.org", enabled=False)
        self.apply()
        self.assertEqual(self.vaultwarden.writes(), [("POST", "/users/u1/enable")])

    def test_it_finds_people_by_core_id_and_not_by_address(self):
        """Vaultwarden keeps an old address, so someone else could have it in core by now."""
        self.vaultwarden.add("old", "1", "someone@example.org")
        self.vaultwarden.add("new", "7", "changed@example.org")
        self.apply(allowed=False, email="someone@example.org")
        self.assertEqual(self.vaultwarden.writes(), [("POST", "/users/new/disable")])

    def test_it_doesnt_mix_up_ids_that_start_the_same(self):
        self.vaultwarden.add("u1", "70", "other@example.org")
        self.apply(allowed=False)
        self.assertEqual(self.vaultwarden.writes(), [])

    def test_an_account_with_no_link_to_core_is_left_alone(self):
        self.vaultwarden.add("u1", "", "someone@example.org")
        self.apply(allowed=False)
        self.assertEqual(self.vaultwarden.writes(), [])

    def test_a_changed_page_is_an_error_rather_than_nothing_to_do(self):
        """Otherwise an upgrade could quietly stop anyone being cut off."""
        self.vaultwarden.add("u1", "7", "someone@example.org")
        real = self.vaultwarden.__call__

        def changed(request, timeout=None):
            answer = real(request, timeout)
            if request.full_url.endswith("/users/overview"):
                return Answer(200, answer.read().replace(b"data-vw-user-uuid", b"data-other"))
            return answer

        with mock.patch.object(vault.urllib.request, "urlopen", changed), self.assertRaisesRegex(RuntimeError, "isn't what this expects"):
            self.apply(allowed=False)

    def test_a_page_with_no_table_is_an_error_too(self):
        real = self.vaultwarden.__call__

        def changed(request, timeout=None):
            answer = real(request, timeout)
            return Answer(200, b"<html>Something else</html>") if request.full_url.endswith("/users/overview") else answer

        with mock.patch.object(vault.urllib.request, "urlopen", changed), self.assertRaisesRegex(RuntimeError, "isn't what this expects"):
            self.apply(allowed=False)

    def test_it_says_which_organisations_need_their_passwords_changed(self):
        self.vaultwarden.add("u1", "7", "someone@example.org", organisations=[org()])
        self.apply(allowed=False)
        self.assertEqual(len(self.notes), 1)
        key, text = self.notes[0]
        self.assertEqual(key, "org-1")
        self.assertIn("Shared passwords", text)
        self.assertIn("someone@example.org", text)
        self.assertIn("change every password", text)

    def test_it_writes_that_down_before_turning_them_off(self):
        """A crash in between would otherwise lose it: next time they're already off."""
        self.vaultwarden.add("u1", "7", "someone@example.org", organisations=[org()])
        order = []
        real = self.vaultwarden.__call__

        def recording(request, timeout=None):
            if request.full_url.endswith("/disable"):
                order.append("disable")
            return real(request, timeout)

        with mock.patch.object(vault.urllib.request, "urlopen", recording):
            self.connector.apply(person(allowed=False), lambda key, text: order.append("note"))
        self.assertEqual(order, ["note", "disable"])

    def test_someone_with_no_organisation_leaves_nothing_to_do(self):
        self.vaultwarden.add("u1", "7", "someone@example.org")
        self.apply(allowed=False)
        self.assertEqual(self.notes, [])

    def test_someone_already_off_leaves_nothing_new_to_do(self):
        self.vaultwarden.add("u1", "7", "someone@example.org", enabled=False, organisations=[org()])
        self.apply(allowed=False)
        self.assertEqual(self.notes, [])

    def test_an_organisation_left_with_no_owner_says_so(self):
        self.vaultwarden.add("u1", "7", "someone@example.org", organisations=[org(kind=vault.OWNER)])
        self.apply(allowed=False)
        self.assertIn("only owner", self.notes[0][1])

    def test_a_second_owner_means_it_doesnt(self):
        self.vaultwarden.add("u1", "7", "someone@example.org", organisations=[org(kind=vault.OWNER)])
        self.vaultwarden.add("u2", "8", "other@example.org", organisations=[org(kind=vault.OWNER)])
        self.apply(allowed=False)
        self.assertNotIn("only owner", self.notes[0][1])

    def test_a_second_owner_who_is_turned_off_doesnt_count(self):
        self.vaultwarden.add("u1", "7", "someone@example.org", organisations=[org(kind=vault.OWNER)])
        self.vaultwarden.add("u2", "8", "other@example.org", enabled=False, organisations=[org(kind=vault.OWNER)])
        self.apply(allowed=False)
        self.assertIn("only owner", self.notes[0][1])

    def test_an_owner_of_some_other_organisation_doesnt_count(self):
        self.vaultwarden.add("u1", "7", "someone@example.org", organisations=[org(kind=vault.OWNER)])
        self.vaultwarden.add("u2", "8", "other@example.org", organisations=[org("org-2", kind=vault.OWNER)])
        self.apply(allowed=False)
        self.assertIn("only owner", self.notes[0][1])

    def test_it_logs_in_once_and_keeps_the_session(self):
        """Logging in is limited to a few tries."""
        self.vaultwarden.add("u1", "7", "someone@example.org")
        self.apply()
        self.apply()
        self.assertEqual(self.vaultwarden.logins, 1)

    def test_it_logs_in_again_when_the_session_runs_out(self):
        self.vaultwarden.add("u1", "7", "someone@example.org")
        self.apply()
        self.connector.cookie = "VW_ADMIN=expired"
        self.apply()
        self.assertEqual(self.vaultwarden.logins, 2)

    def test_a_wrong_token_is_an_error_and_not_nothing_to_do(self):
        self.vaultwarden.add("u1", "7", "someone@example.org")
        self.connector.token = "wrong"
        with self.assertRaisesRegex(RuntimeError, "401"):
            self.apply(allowed=False)

    def test_a_refused_change_is_an_error_so_it_gets_tried_again(self):
        self.vaultwarden.add("u1", "7", "someone@example.org")
        self.vaultwarden.status_for["/users/u1/disable"] = 500
        with self.assertRaisesRegex(RuntimeError, "500"):
            self.apply(allowed=False)

    def test_an_error_never_shows_the_token(self):
        self.connector.token = "the-wrong-token"
        with self.assertRaises(RuntimeError) as caught:
            self.apply(allowed=False)
        self.assertNotIn("the-wrong-token", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
