"""Tells Forgejo what core has decided about someone, straight away. Without this, it
only hears at their next sign-in, so someone who has left would keep their tokens and any
repo they created. See core/accounts/sync.py for how this gets called.

How the tree maps (structure.md §5, and "Who gets what" in the README):
- The root association is the installation's organisation, named after the domain. Every
  other association gets an organisation named by its key, made when someone first needs it.
- A team becomes a Forgejo team named by its key, in the organisation of the association
  that owns it, whose leads and participants write to the repos it's given. Its leads are
  also in `<key>.lead`, which administers those repos and can create new ones.
- Each organisation has `members`, which reads the repos someone opens to members.
- An association's chair and vice chair are in its organisation's Owners, and so are
  operators in the installation's organisation.

Core only changes what it made, which says so in its description, along with the
installation's organisation and the teams module.toml names. One made by hand with the
same name is left alone, and the change waits, with the reason on the admin page.

Standard library only. This has to be safe to repeat, so it only changes what differs."""

import json
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "http://forgejo:3000/api/v1"  # over Compose's own network: it publishes no ports
TIMEOUT = 20
PAGE = 50  # the most Forgejo sends at once
MARK = "Kept in step with core."  # starts the description of everything core makes here
OWNERS = "Owners"
MEMBERS = "members"
LEAD = ".lead"  # unit keys have no dots, so this can't be another team's name
HEADS = ("chair", "vice_chair")
NO_LIMIT = 1_000_000  # repos an organisation may have: 0 would fall back on the limit for people
UNITS = ["repo.code", "repo.issues", "repo.pulls", "repo.releases", "repo.wiki", "repo.projects"]


def _quoted(value):
    return urllib.parse.quote(str(value), safe="")


class Connector:
    tool = "forgejo"

    def __init__(self, environ):
        self.token = environ["FORGEJO_ADMIN_TOKEN"]
        self.org = environ["SALLSKAP_DOMAIN"].split(".")[0]  # kit names it the same way
        manifest = tomllib.loads((Path(__file__).parent / "module.toml").read_text("utf-8"))
        self.teams = manifest["teams"]  # the same map Forgejo reads at sign-in
        self.admin_group = manifest["admin_group"]
        # A unit with one of these keys gets <key>.team, as the name is already taken.
        self.taken = {OWNERS.lower(), MEMBERS, *(team.lower() for team in self.teams.values())}

    def _call(self, method, path, body=None, sudo=None):
        headers = {"Authorization": f"token {self.token}", "Content-Type": "application/json"}
        if sudo:
            headers["Sudo"] = sudo  # as that person, which the admin token can do
        request = urllib.request.Request(
            API + path, method=method, data=json.dumps(body).encode() if body is not None else None, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                content = response.read()
                return response.status, json.loads(content) if content else None
        except urllib.error.HTTPError as error:
            return error.code, None

    def _expect(self, method, path, *statuses, body=None, sudo=None):
        status, content = self._call(method, path, body, sudo)
        if status not in statuses:
            raise RuntimeError(f"Forgejo answered {status} to {method} {path}")
        return content

    def _pages(self, path, sudo=None):
        """Everything a list has, as Forgejo only sends part of it at a time."""
        found, page = [], 1
        while True:
            content = self._expect("GET", f"{path}{'&' if '?' in path else '?'}limit={PAGE}&page={page}", 200, sudo=sudo)
            items = content["data"] if isinstance(content, dict) else content
            found += items
            if len(items) < PAGE:
                return found
            page += 1

    def _find(self, sub):
        """Someone's name can change. Their `sub` can't, so that's how Forgejo finds them too."""
        for account in self._pages(f"/admin/users?login_name={_quoted(sub)}"):
            if account["login_name"] == sub and account["source_id"] > 0:
                return account
        return None

    def check(self):
        """For `kit update`: its admin API still takes the token, and the organisation is there."""
        self._expect("GET", "/admin/users?limit=1", 200)
        self._expect("GET", f"/orgs/{_quoted(self.org)}", 200)

    def apply(self, person, note):
        account = self._find(person.sub)
        if account is None:
            return  # they've never signed in, so there's nothing to change
        login = account["login"]
        keys = set(person.groups) if person.allowed else set()
        # Blocking goes first, as it's what ends their tokens and open sessions.
        self._set_flags(account, blocked=not person.allowed, admin=self.admin_group in keys)
        wanted = self._wanted(keys, {unit.key: unit for unit in person.units})
        self._set_teams(login, wanted, {unit.key: unit for unit in person.units}, leaving=not person.allowed)
        # Forgejo makes someone who creates a repo a collaborator, and leaving a team
        # doesn't change that. They stay one while they hold a team in its organisation.
        if self.admin_group not in keys:
            kept = {org for org, team in wanted if team.lower() not in self.taken}
            self._remove_collaborations(account, login, kept)

    def _set_flags(self, account, blocked, admin):
        changes = {}
        if account["prohibit_login"] != blocked:
            changes["prohibit_login"] = blocked
        if account["is_admin"] != admin:
            changes["admin"] = admin
        if changes:
            # Forgejo requires these two on every edit, even though they aren't changing.
            body = {"login_name": account["login_name"], "source_id": account["source_id"], **changes}
            self._expect("PATCH", f"/admin/users/{_quoted(account['login'])}", 200, body=body)

    # ---- Which teams

    def _org_of(self, association):
        return self.org if association.root else association.key

    def _team_of(self, unit):
        return f"{unit.key}.team" if unit.key in self.taken else unit.key

    def _wanted(self, keys, units):
        """(organisation, team) for each team their keys give them."""
        wanted = {(self.org, self.teams[key]) for key in keys if key in self.teams}
        for key in keys:
            unit_key, _, role = key.partition(":")
            unit = units.get(unit_key)
            if unit is None:
                continue
            org = self._org_of(units[unit.association])
            if role == "member":
                wanted.add((org, MEMBERS))
            elif role in HEADS and not unit.team:
                wanted.add((org, OWNERS))
            elif role in ("lead", "participant") and unit.team:
                wanted.add((org, self._team_of(unit)))
                if role == "lead":
                    wanted.add((org, self._team_of(unit) + LEAD))
        return wanted

    def _our_org(self, org):
        """Whether core or kit made this organisation: the installation's, or one whose
        description says so."""
        return org["username"] == self.org or (org.get("description") or "").startswith(MARK)

    def _our_team(self, org, team):
        """In one of those: Owners, the teams module.toml names in the installation's, and
        any whose description says core made it."""
        if team["name"] == OWNERS or (org == self.org and team["name"] in self.teams.values()):
            return True
        return (team.get("description") or "").startswith(MARK)

    def _set_teams(self, login, wanted, units, leaving):
        """Leaving takes someone off every team in core's organisations, made by hand or not."""
        current = {(team["organization"]["username"], team["name"]): team for team in self._pages("/user/teams", sudo=login)}
        for (org, name), team in current.items():
            if (org, name) not in wanted and self._our_org(team["organization"]) and (leaving or self._our_team(org, team)):
                self._expect("DELETE", f"/teams/{team['id']}/members/{_quoted(login)}", 204)
        teams = {}  # each organisation's, once
        for org, name in sorted(wanted - set(current)):
            if org not in teams:
                teams[org] = self._teams(org, units)
            team = teams[org].get(name.lower()) or self._make_team(org, name, units)
            teams[org][name.lower()] = team
            if not self._our_team(org, team):
                raise RuntimeError(f"Forgejo already has a team called {team['name']} in {org} that core didn't make, so core won't use it. Rename it there")
            self._expect("PUT", f"/teams/{team['id']}/members/{_quoted(login)}", 204)

    def _teams(self, org, units):
        """An organisation's teams, by name. It's made first if it isn't there."""
        status, found = self._call("GET", f"/orgs/{_quoted(org)}")
        if status == 404 and org == self.org:
            raise RuntimeError(f"Forgejo has no organisation called {org}. Run `kit enable git` again")
        if status == 404:
            self._make_org(org, units)
        elif status != 200:
            raise RuntimeError(f"Forgejo answered {status} to GET /orgs/{org}")
        elif not self._our_org(found):
            raise RuntimeError(f"Forgejo already has an organisation called {org} that core didn't make, so core won't use it. Rename it there")
        return {team["name"].lower(): team for team in self._pages(f"/orgs/{_quoted(org)}/teams")}

    def _make_org(self, org, units):
        name = units[org].name if org in units else org
        status, _ = self._call("POST", "/orgs", {"username": org, "full_name": name, "description": f"{MARK} {name}", "visibility": "public"})
        if status != 201:
            raise RuntimeError(f"Forgejo won't make an organisation called {org} ({status}): someone's account has that name, or Forgejo keeps it for itself")
        # The limit on new repos is for people, but organisations are held to it too.
        self._expect("PATCH", f"/admin/users/{_quoted(org)}", 200, body={"login_name": org, "source_id": 0, "max_repo_creation": NO_LIMIT})

    def _make_team(self, org, name, units):
        if name == OWNERS or (org == self.org and name in self.teams.values()):
            raise RuntimeError(f"Forgejo has no team called {name} in {org}. Run `kit enable git` again")
        lead = name.endswith(LEAD)
        unit = None if name == MEMBERS else units.get(name.removesuffix(LEAD).removesuffix(".team"))
        what = unit.name if unit else "Members"
        body = {
            "name": name,
            "description": f"{MARK} {what}" + (", its leads" if lead else ""),
            "permission": "admin" if lead else "read" if name == MEMBERS else "write",
            "can_create_org_repo": lead,
            "includes_all_repositories": False,  # a repo joins a team when someone gives it one
            "units": UNITS,
        }
        return self._expect("POST", f"/orgs/{_quoted(org)}/teams", 201, body=body)

    def _remove_collaborations(self, account, login, kept):
        """Every repo they can reach comes up here, including through a team, so each one
        is checked first. Repos in their own name stay theirs, and so do their
        collaborations in an organisation where they still hold a team."""
        for repo in self._pages(f"/repos/search?uid={account['id']}"):
            owner = repo["owner"]["login"]
            if owner == account["login"] or owner in kept:
                continue
            path = f"/repos/{_quoted(owner)}/{_quoted(repo['name'])}/collaborators/{_quoted(login)}"
            status, _ = self._call("GET", path)
            if status == 204:
                self._expect("DELETE", path, 204)
            elif status != 404:
                raise RuntimeError(f"Forgejo answered {status} to GET {path}")
