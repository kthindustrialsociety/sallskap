"""Tells Forgejo what core has decided about someone, straight away. Without this, it
only hears at their next sign-in, so someone who has left would keep their tokens and any
repo they created. See core/accounts/sync.py for how this gets called.

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
        self.collaborator_groups = set(manifest["collaborator_groups"])

    def _call(self, method, path, body=None):
        request = urllib.request.Request(
            API + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"token {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                content = response.read()
                return response.status, json.loads(content) if content else None
        except urllib.error.HTTPError as error:
            return error.code, None

    def _expect(self, method, path, *statuses, body=None):
        status, content = self._call(method, path, body)
        if status not in statuses:
            raise RuntimeError(f"Forgejo answered {status} to {method} {path}")
        return content

    def _pages(self, path):
        """Everything a list has, as Forgejo only sends part of it at a time."""
        found, page = [], 1
        while True:
            content = self._expect("GET", f"{path}{'&' if '?' in path else '?'}limit={PAGE}&page={page}", 200)
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

    def apply(self, person, note):
        account = self._find(person.sub)
        if account is None:
            return  # they've never signed in, so there's nothing to change
        name = _quoted(account["login"])
        groups = set(person.groups) if person.allowed else set()
        # Blocking goes first, as it's what ends their tokens and open sessions.
        self._set_flags(account, blocked=not person.allowed, admin=self.admin_group in groups)
        self._set_teams(name, groups, leaving=not person.allowed)
        # Forgejo makes someone who creates a repo a collaborator, when their team only
        # has write access, and leaving the team doesn't change that.
        if not groups & self.collaborator_groups:
            self._remove_collaborations(account, name)

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

    def _set_teams(self, name, groups, leaving):
        """Teams not in the map aren't core's to manage, unless someone has left altogether."""
        group_of = {team: group for group, team in self.teams.items()}
        for team in self._pages(f"/orgs/{_quoted(self.org)}/teams"):
            if leaving:
                wanted = False
            elif team["name"] in group_of:
                wanted = group_of[team["name"]] in groups
            else:
                continue
            path = f"/teams/{team['id']}/members/{name}"
            status, _ = self._call("GET", path)
            if status not in (200, 404):
                raise RuntimeError(f"Forgejo answered {status} to GET {path}")
            if (status == 200) != wanted:
                self._expect("PUT" if wanted else "DELETE", path, 204)

    def _remove_collaborations(self, account, name):
        """Every repo they can reach comes up here, including through a team, so each one
        is checked first. Repos in their own name stay theirs."""
        for repo in self._pages(f"/repos/search?uid={account['id']}"):
            owner = repo["owner"]["login"]
            if owner == account["login"]:
                continue
            path = f"/repos/{_quoted(owner)}/{_quoted(repo['name'])}/collaborators/{name}"
            status, _ = self._call("GET", path)
            if status == 204:
                self._expect("DELETE", path, 204)
            elif status != 404:
                raise RuntimeError(f"Forgejo answered {status} to GET {path}")
