"""The connectors that tell tools about changes, against made-up tools that record what
they were asked. tests/live_check.py runs them against the real ones."""

import importlib.util
import io
import json
import sys
import unittest
import urllib.error
import urllib.parse
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


def unit(key, association, team=True, root=False, name=None):
    return SimpleNamespace(key=key, name=name or key.replace("-", " ").title(), team=team, association=association, root=root)


# Example Federation as the worker describes it to connectors: the root, whose key is test
# here, with Robotics and Chemistry inside. Chemistry has a team whose key Forgejo already
# uses for something else.
FEDERATION = (
    unit("test", "test", team=False, root=True, name="Test Association"),
    unit("extended-board", "test"),
    unit("spring-hackathon-2027", "test"),
    unit("robotics", "robotics", team=False, name="Example Robotics"),
    unit("operations", "robotics"),
    unit("development", "robotics"),
    unit("uavs", "robotics"),
    unit("drone-race-2027", "robotics"),
    unit("chemistry-website", "robotics"),  # run for Chemistry, by Robotics
    unit("chemistry", "chemistry", team=False, name="Example Chemistry"),
    unit("lab-nights", "chemistry"),
    unit("members", "chemistry"),
)

# Example Society: one association, and each person's keys today (core/structure/examples.py).
SOCIETY = (
    unit("test", "test", team=False, root=True, name="Test Association"),
    unit("events", "test"),
    unit("photography", "test"),
    unit("website", "test"),
    unit("spring-hackathon-2027", "test"),
    unit("autumn-hackathon-2025", "test"),
)
SOCIETY_KEYS = {
    "alva": ["board", "member", "test:chair", "test:member"],
    "bruno": ["board", "member", "test:member", "test:vice_chair"],
    "cleo": ["board", "member", "test:member", "test:treasurer", "treasurer"],
    "gun": ["events:lead", "member", "test:member"],
    "hampus": ["member", "photography:lead", "test:member"],
    "ines": ["member", "test:member", "website:lead"],
    "jon": ["member", "spring-hackathon-2027:lead", "test:member"],
    "kaj": ["member", "test:member"],
    "lo": ["events:participant", "member", "test:member"],
    "mio": ["member", "photography:participant", "test:member", "website:participant"],
    "nora": ["spring-hackathon-2027:participant"],
    "olof": ["member", "test:member"],
    "petra": [],
}


def person(sub="7", allowed=True, groups=(), email="someone@example.org", units=FEDERATION):
    return SimpleNamespace(sub=sub, allowed=allowed, groups=tuple(groups), email=email, units=units)


class FakeForgejo:
    """Just enough of its API, holding what the real one would: what the trial on the test
    server found it does, in A5's notes."""

    def __init__(self):
        self.accounts = {}
        self.orgs = {}
        self.teams = {}  # id: team, with the organisation's name in "org"
        self.members = {}  # team id: logins
        self.add_account("7", "someone")
        self.add_org("example", "")  # the installation's, made by kit: no mark
        self.add_team("example", "board", "", "read")
        self.repos = [("example", "made-by-them"), ("example", "someone-elses"), ("robotics", "for-operations"), ("someone", "their-own")]
        self.collaborators = {("example", "made-by-them"), ("robotics", "for-operations")}
        self.calls = []
        self.broken = None

    @property
    def account(self):
        return self.accounts["7"]

    def add_account(self, sub, login):
        self.accounts[sub] = {"id": 40 + len(self.accounts), "login": login, "login_name": sub, "source_id": 1, "prohibit_login": False, "is_admin": False}

    def add_org(self, name, description):
        self.orgs[name] = {"username": name, "description": description, "visibility": "public"}
        self.add_team(name, "Owners", "", "owner")

    def add_team(self, org, name, description, permission, members=()):
        number = len(self.teams) + 1
        self.teams[number] = {"id": number, "name": name, "description": description, "permission": permission, "org": org}
        self.members[number] = set(members)
        return number

    def team(self, org, name):
        return next((number for number, team in self.teams.items() if team["org"] == org and team["name"].lower() == name.lower()), None)

    def teams_of(self, login="someone"):
        return {(self.teams[number]["org"], self.teams[number]["name"]) for number, logins in self.members.items() if login in logins}

    def writes(self):
        return [(method, path) for method, path, _ in self.calls if method != "GET"]

    def __call__(self, request, timeout=None):
        path = request.full_url.removeprefix(git.API)
        method = request.get_method()
        body = json.loads(request.data) if request.data else None
        self.calls.append((method, path, body))
        if self.broken and self.broken in path:
            raise urllib.error.HTTPError(request.full_url, 500, "broken", Message(), None)
        status, content = self.answer(method, path, body, request.get_header("Sudo"))
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "no", Message(), None)
        return Answer(status, json.dumps(content).encode() if content is not None else b"")

    def answer(self, method, path, body, sudo=None):
        route, _, query = path.partition("?")
        query = dict(part.split("=", 1) for part in query.split("&") if part)
        if query.get("page", "1") != "1":
            return 200, []
        parts = route.strip("/").split("/")
        if route == "/admin/users" and "login_name" in query:
            account = self.accounts.get(urllib.parse.unquote(query["login_name"]))
            return 200, [account] if account else []
        if route == "/admin/users":
            return 200, list(self.accounts.values())[:1]
        if parts[:2] == ["admin", "users"] and method == "PATCH":
            login = urllib.parse.unquote(parts[2])
            if login in self.orgs:
                self.orgs[login]["max_repo_creation"] = body["max_repo_creation"]
                return 200, {}
            account = next(account for account in self.accounts.values() if account["login"] == login)
            account.update({("is_admin" if k == "admin" else k): v for k, v in body.items()})
            return 200, account
        if route == "/orgs" and method == "POST":
            taken = {account["login"] for account in self.accounts.values()} | set(self.orgs) | {"explore", "api"}
            if body["username"] in taken:
                return 422, None
            self.add_org(body["username"], body["description"])
            self.orgs[body["username"]].update(full_name=body["full_name"], visibility=body["visibility"])
            return 201, self.orgs[body["username"]]
        if parts[0] == "orgs" and len(parts) == 2:
            return (200, self.orgs[parts[1]]) if parts[1] in self.orgs else (404, None)
        if parts[0] == "orgs" and parts[2] == "teams" and method == "GET":
            return 200, [self.public(number) for number, team in self.teams.items() if team["org"] == parts[1]]
        if parts[0] == "orgs" and parts[2] == "teams" and method == "POST":
            if self.team(parts[1], body["name"]):
                return 422, None
            number = self.add_team(parts[1], body["name"], body["description"], body["permission"])
            self.teams[number].update({k: body[k] for k in ("can_create_org_repo", "includes_all_repositories", "units")})
            return 201, self.public(number)
        if route == "/user/teams":
            if sudo not in {account["login"] for account in self.accounts.values()}:
                return 404, None
            return 200, [self.public(number) for number, logins in self.members.items() if sudo in logins]
        if parts[0] == "teams" and parts[2] == "members":
            number, login = int(parts[1]), urllib.parse.unquote(parts[3])
            (self.members[number].add if method == "PUT" else self.members[number].discard)(login)
            return 204, None
        if route == "/repos/search":
            return 200, {"data": [{"owner": {"login": owner}, "name": name} for owner, name in self.repos]}
        if "collaborators" in parts:
            repo = (parts[1], parts[2])
            if method == "GET":
                return (204, None) if repo in self.collaborators else (404, None)
            self.collaborators.discard(repo)
            return 204, None
        return 404, None

    def public(self, number):
        team = self.teams[number]
        return {**{k: v for k, v in team.items() if k != "org"}, "organization": self.orgs[team["org"]]}


class Forgejo(unittest.TestCase):
    def setUp(self):
        self.forgejo = FakeForgejo()
        patcher = mock.patch.object(git.urllib.request, "urlopen", self.forgejo)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.connector = git.Connector({"FORGEJO_ADMIN_TOKEN": "the-token", "SALLSKAP_DOMAIN": "example.org"})

    def apply(self, **kwargs):
        self.connector.apply(person(**kwargs), lambda key, text: self.fail("Forgejo has nothing for a person to do"))

    def made_team(self, org, name, members=("someone",), description=None):
        return self.forgejo.add_team(org, name, git.MARK + " " + name if description is None else description, "write", members)

    def made_org(self, org):
        self.forgejo.add_org(org, git.MARK + " " + org)

    # ---- As before A5

    def test_nothing_happens_for_someone_who_has_never_signed_in(self):
        self.apply(sub="999", groups=["test:member"])
        self.assertEqual(self.forgejo.writes(), [])

    def test_the_check_after_an_update_reads_and_changes_nothing(self):
        self.connector.check()
        self.assertEqual(self.forgejo.writes(), [])
        self.assertEqual([path for _, path, _ in self.forgejo.calls], ["/admin/users?limit=1", "/orgs/example"])

    def test_the_check_after_an_update_fails_when_the_api_has_changed(self):
        self.forgejo.broken = "/orgs/example"
        with self.assertRaisesRegex(RuntimeError, "500"):
            self.connector.check()

    def test_it_changes_nothing_for_someone_already_right(self):
        self.apply(groups=["board", "test:member", "uavs:participant"])
        self.forgejo.calls = []
        self.apply(groups=["board", "test:member", "uavs:participant"])
        self.assertEqual(self.forgejo.writes(), [])

    def test_leaving_blocks_them_first_of_all(self):
        self.apply(allowed=False, groups=["board"])
        self.assertEqual(self.forgejo.writes()[0], ("PATCH", "/admin/users/someone"))
        self.assertTrue(self.forgejo.account["prohibit_login"])

    def test_every_edit_sends_what_forgejo_requires(self):
        self.apply(allowed=False)
        body = next(body for method, _, body in self.forgejo.calls if method == "PATCH")
        self.assertEqual((body["login_name"], body["source_id"]), ("7", 1))

    def test_leaving_takes_them_off_every_team_in_cores_organisations_even_ones_made_by_hand(self):
        self.made_org("robotics")
        self.made_team("robotics", "uavs")
        by_hand = self.made_team("example", "made-by-hand", description="ours")
        self.forgejo.members[self.forgejo.team("example", "board")].add("someone")
        self.forgejo.members[self.forgejo.team("example", "Owners")].add("someone")
        self.forgejo.add_org("hand-made", "not core's")
        elsewhere = self.made_team("hand-made", "theirs", description="theirs")
        self.apply(allowed=False)
        self.assertEqual(self.forgejo.teams_of(), {("hand-made", "theirs")})
        self.assertNotIn("someone", self.forgejo.members[by_hand])
        self.assertIn("someone", self.forgejo.members[elsewhere])

    def test_leaving_removes_collaborations(self):
        self.apply(allowed=False)
        self.assertEqual(self.forgejo.collaborators, set())

    def test_leaving_ends_admin_rights(self):
        self.forgejo.account["is_admin"] = True
        self.apply(allowed=False)
        self.assertFalse(self.forgejo.account["is_admin"])

    def test_someone_coming_back_is_unblocked_and_gets_their_teams(self):
        self.forgejo.account["prohibit_login"] = True
        self.apply(groups=["board", "uavs:participant"])
        self.assertFalse(self.forgejo.account["prohibit_login"])
        self.assertEqual(self.forgejo.teams_of(), {("example", "board"), ("robotics", "uavs")})

    def test_losing_a_key_takes_its_team_and_leaves_the_account_on(self):
        self.apply(groups=["board", "uavs:participant"])
        self.apply(groups=["test:member"])
        self.assertEqual(self.forgejo.teams_of(), {("example", "members")})
        self.assertFalse(self.forgejo.account["prohibit_login"])

    def test_teams_made_by_hand_are_left_alone(self):
        by_hand = self.made_team("example", "a-project", description="made in Forgejo")
        self.apply(groups=["test:member"])
        self.assertIn("someone", self.forgejo.members[by_hand])

    def test_operators_are_admins_and_owners_and_others_arent(self):
        self.apply(groups=["operator"])
        self.assertTrue(self.forgejo.account["is_admin"])
        self.assertEqual(self.forgejo.teams_of(), {("example", "Owners")})
        self.apply(groups=["board"])
        self.assertFalse(self.forgejo.account["is_admin"])
        self.assertEqual(self.forgejo.teams_of(), {("example", "board")})

    def test_operators_stay_collaborators_everywhere(self):
        self.apply(groups=["operator"])
        self.assertEqual(self.forgejo.collaborators, {("example", "made-by-them"), ("robotics", "for-operations")})

    def test_someone_with_no_team_left_in_an_organisation_stops_being_a_collaborator_there(self):
        self.apply(groups=["board", "test:member", "robotics:member"])
        self.assertEqual(self.forgejo.collaborators, set())

    def test_someone_on_a_team_there_stays_a_collaborator_there_only(self):
        self.apply(groups=["operations:participant", "test:member"])
        self.assertEqual(self.forgejo.collaborators, {("robotics", "for-operations")})

    def test_it_only_removes_them_as_a_collaborator_where_they_are_one(self):
        self.apply(groups=["test:member"])
        removed = [path for method, path in self.forgejo.writes() if "collaborators" in path]
        self.assertEqual(removed, ["/repos/example/made-by-them/collaborators/someone", "/repos/robotics/for-operations/collaborators/someone"])

    def test_a_repo_in_their_own_name_stays_theirs(self):
        self.forgejo.collaborators = {("someone", "their-own")}
        self.apply(groups=["test:member"])
        self.assertEqual(self.forgejo.collaborators, {("someone", "their-own")})

    def test_it_reads_every_page(self):
        self.forgejo.repos = [("example", f"repo-{n}") for n in range(50)]
        answer = self.forgejo.answer

        def paged(method, path, body, sudo=None):
            if path.startswith("/repos/search") and "page=2" in path:
                return 200, {"data": [{"owner": {"login": "example"}, "name": "on-the-second-page"}]}
            return answer(method, path, body, sudo)

        self.forgejo.answer = paged
        self.forgejo.collaborators = {("example", "on-the-second-page")}
        self.apply(groups=["test:member"])
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
        self.apply(allowed=False)
        self.assertIn(("PATCH", "/admin/users/some%2F..%2Fone"), self.forgejo.writes())

    def test_it_asks_forgejo_which_teams_they_are_on_as_them(self):
        self.apply(groups=["test:member"])
        self.assertIn(("GET", "/user/teams?limit=50&page=1", None), self.forgejo.calls)

    # ---- The tree: structure.md §5

    def test_the_roots_members_read_in_the_installations_organisation(self):
        self.apply(groups=["member", "test:member"])
        self.assertEqual(self.forgejo.teams_of(), {("example", "members")})
        team = self.forgejo.teams[self.forgejo.team("example", "members")]
        self.assertEqual((team["permission"], team["can_create_org_repo"], team["includes_all_repositories"]), ("read", False, False))
        self.assertTrue(team["description"].startswith(git.MARK))

    def test_membership_counts_upwards_into_each_organisation(self):
        self.apply(groups=["member", "robotics:member", "test:member"])
        self.assertEqual(self.forgejo.teams_of(), {("example", "members"), ("robotics", "members")})

    def test_an_association_gets_its_organisation_when_someone_first_needs_it(self):
        self.assertNotIn("robotics", self.forgejo.orgs)
        self.apply(groups=["robotics:member"])
        org = self.forgejo.orgs["robotics"]
        self.assertEqual((org["full_name"], org["visibility"]), ("Example Robotics", "public"))
        self.assertTrue(org["description"].startswith(git.MARK))
        self.assertEqual(org["max_repo_creation"], git.NO_LIMIT)
        self.assertNotIn("chemistry", self.forgejo.orgs)

    def test_a_team_writes_and_its_leads_administer_and_create_repos(self):
        self.apply(groups=["operations:lead", "robotics:member"], sub="7")
        self.forgejo.add_account("8", "other")
        self.apply(groups=["operations:participant"], sub="8")
        self.assertEqual(self.forgejo.teams_of("someone"), {("robotics", "operations"), ("robotics", "operations.lead"), ("robotics", "members")})
        self.assertEqual(self.forgejo.teams_of("other"), {("robotics", "operations")})
        team = self.forgejo.teams[self.forgejo.team("robotics", "operations")]
        lead = self.forgejo.teams[self.forgejo.team("robotics", "operations.lead")]
        self.assertEqual((team["permission"], team["can_create_org_repo"], team["includes_all_repositories"]), ("write", False, False))
        self.assertEqual((lead["permission"], lead["can_create_org_repo"], lead["includes_all_repositories"]), ("admin", True, False))

    def test_a_team_inside_a_team_is_in_its_associations_organisation(self):
        self.apply(groups=["drone-race-2027:lead", "uavs:participant", "robotics:member"])
        self.assertEqual({org for org, _ in self.forgejo.teams_of()}, {"robotics"})

    def test_a_team_run_for_another_association_belongs_to_the_one_running_it(self):
        self.apply(groups=["chemistry-website:participant", "chemistry:member"])
        self.assertEqual(self.forgejo.teams_of(), {("robotics", "chemistry-website"), ("chemistry", "members")})

    def test_a_team_others_take_part_in_belongs_to_its_owner(self):
        self.apply(groups=["spring-hackathon-2027:participant", "chemistry:member"])
        self.assertEqual(self.forgejo.teams_of(), {("example", "spring-hackathon-2027"), ("chemistry", "members")})

    def test_a_new_chair_gets_their_organisations_admin_and_the_last_one_loses_it(self):
        self.forgejo.add_account("8", "last-chair")
        self.apply(sub="8", groups=["robotics:chair", "robotics:member"])
        self.assertIn(("robotics", "Owners"), self.forgejo.teams_of("last-chair"))
        self.apply(sub="7", groups=["robotics:chair", "robotics:member"])
        self.apply(sub="8", groups=["robotics:member"])
        self.assertIn(("robotics", "Owners"), self.forgejo.teams_of("someone"))
        self.assertNotIn(("robotics", "Owners"), self.forgejo.teams_of("last-chair"))

    def test_the_roots_chair_and_vice_chair_own_the_installations_organisation(self):
        for role in ("chair", "vice_chair"):
            self.apply(groups=["board", f"test:{role}", "test:member"])
            self.assertIn(("example", "Owners"), self.forgejo.teams_of())
        self.apply(groups=["board", "test:treasurer", "test:member"])
        self.assertNotIn(("example", "Owners"), self.forgejo.teams_of())

    def test_only_board_roles_of_the_root_give_the_board_team(self):
        self.apply(groups=["robotics:chair", "robotics:member", "test:member"])
        self.assertNotIn(("example", "board"), self.forgejo.teams_of())

    def test_an_archived_project_takes_its_people_off_its_teams_and_keeps_them(self):
        self.apply(groups=["drone-race-2027:lead", "robotics:member"])
        self.apply(groups=["robotics:member"])  # archived: its key is gone
        self.assertEqual(self.forgejo.teams_of(), {("robotics", "members")})
        self.assertIsNotNone(self.forgejo.team("robotics", "drone-race-2027"))
        self.assertIsNotNone(self.forgejo.team("robotics", "drone-race-2027.lead"))

    def test_a_moved_team_leaves_its_old_organisation(self):
        self.apply(groups=["uavs:participant"])
        moved = tuple(unit("uavs", "chemistry") if u.key == "uavs" else u for u in FEDERATION)
        self.apply(groups=["uavs:participant"], units=moved)
        self.assertEqual(self.forgejo.teams_of(), {("chemistry", "uavs")})

    def test_a_key_forgejo_already_uses_gets_a_team_of_its_own(self):
        self.apply(groups=["members:lead", "chemistry:member"])
        self.assertEqual(self.forgejo.teams_of(), {("chemistry", "members.team"), ("chemistry", "members.team.lead"), ("chemistry", "members")})

    def test_an_organisation_made_by_hand_is_left_alone_and_says_why(self):
        self.forgejo.add_org("robotics", "made in Forgejo")
        with self.assertRaisesRegex(RuntimeError, "already has an organisation called robotics that core didn't make"):
            self.apply(groups=["robotics:member"])
        self.assertEqual(self.forgejo.teams_of(), set())

    def test_a_team_made_by_hand_with_a_units_name_is_left_alone_and_says_why(self):
        self.made_org("robotics")
        self.made_team("robotics", "uavs", members=(), description="made in Forgejo")
        with self.assertRaisesRegex(RuntimeError, "already has a team called uavs in robotics that core didn't make"):
            self.apply(groups=["uavs:participant"])
        self.assertEqual(self.forgejo.teams_of(), set())

    def test_a_name_forgejo_wont_give_an_organisation_says_why(self):
        self.forgejo.add_account("8", "robotics")
        with self.assertRaisesRegex(RuntimeError, "won't make an organisation called robotics"):
            self.apply(groups=["robotics:member"])

    def test_the_installations_organisation_is_never_made_here(self):
        del self.forgejo.orgs["example"]
        with self.assertRaisesRegex(RuntimeError, "kit enable git"):
            self.apply(groups=["test:member"])

    def test_example_society_gets_one_organisation_and_no_federation_teams(self):
        for number, (name, keys) in enumerate(sorted(SOCIETY_KEYS.items())):
            self.forgejo.add_account(str(100 + number), name)
            self.connector.apply(person(sub=str(100 + number), groups=keys, units=SOCIETY), lambda key, text: None)
        self.assertEqual(set(self.forgejo.orgs), {"example"})
        made = {team["name"] for team in self.forgejo.teams.values() if team["description"].startswith(git.MARK)}
        self.assertEqual(made, {
            "members", "events", "events.lead", "photography", "photography.lead", "website", "website.lead",
            "spring-hackathon-2027", "spring-hackathon-2027.lead",
        })
        self.assertEqual(self.forgejo.teams_of("alva"), {("example", "Owners"), ("example", "board"), ("example", "members")})
        self.assertEqual(self.forgejo.teams_of("nora"), {("example", "spring-hackathon-2027")})
        self.assertEqual(self.forgejo.teams_of("petra"), set())


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

    def test_the_check_after_an_update_reads_the_page_and_changes_nothing(self):
        self.vaultwarden.add("u1", "7", "someone@example.org")
        self.connector.check()
        self.assertEqual(self.vaultwarden.writes(), [])

    def test_the_check_after_an_update_fails_when_the_page_has_changed(self):
        real = self.vaultwarden.__call__

        def changed(request, timeout=None):
            answer = real(request, timeout)
            return Answer(200, b"<html>Something else</html>") if request.full_url.endswith("/users/overview") else answer

        with mock.patch.object(vault.urllib.request, "urlopen", changed), self.assertRaisesRegex(RuntimeError, "isn't what this expects"):
            self.connector.check()

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
