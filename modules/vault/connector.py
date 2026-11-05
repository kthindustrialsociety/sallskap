"""Tells Vaultwarden what core has decided about someone, straight away. Without this, a
token used by hand keeps working for up to 12 hours, as it only asks core when refreshing.
See core/accounts/sync.py for how this gets called.

Standard library only. This has to be safe to repeat, so it only changes what differs."""

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request

ADMIN = "http://vaultwarden:8080/admin"  # over Compose's own network. Caddy keeps this off the internet
TIMEOUT = 20
OWNER = 0  # Vaultwarden's number for an organisation's owner

ROW = re.compile(r"<tr>(.*?)</tr>", re.S)
ACCOUNT = re.compile(r'data-vw-user-uuid="([^"]+)"')
IDENTIFIER = re.compile(r'<span class="d-block text-break text-wrap">(.*?)</span>', re.S)


class Connector:
    tool = "vaultwarden"

    def __init__(self, environ):
        self.token = environ["VAULT_ADMIN_TOKEN"]
        self.issuer = f"https://app.{environ['SALLSKAP_DOMAIN']}"
        self.cookie = None

    def _log_in(self):
        request = urllib.request.Request(
            ADMIN,
            method="POST",
            data=urllib.parse.urlencode({"token": self.token}).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                cookies = response.headers.get_all("Set-Cookie") or []
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"Vaultwarden answered {error.code} to its admin token") from None
        found = [cookie.split(";")[0] for cookie in cookies if cookie.startswith("VW_ADMIN=")]
        if not found:
            raise RuntimeError("Vaultwarden didn't accept its admin token")
        self.cookie = found[0]

    def _call(self, method, path):
        """Logging in is limited to a few tries, so its session is kept until it runs out."""
        for attempt in (1, 2):
            if not self.cookie:
                self._log_in()
            request = urllib.request.Request(
                ADMIN + path,
                method=method,
                headers={"Cookie": self.cookie, "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                    return response.read().decode()
            except urllib.error.HTTPError as error:
                if error.code == 401 and attempt == 1:
                    self.cookie = None
                    continue
                raise RuntimeError(f"Vaultwarden answered {error.code} to {method} {path}") from None
        return None  # not reached: the second try either returns or raises

    def _account_ids(self):
        """Who's who, by the same issuer and `sub` it finds people by when they sign in. Its
        list as data doesn't say, and addresses don't do: it keeps someone's old address, so
        another person could later have it in core. Only this page has them. If it's changed
        in an upgrade, this stops rather than acting on the wrong person."""
        page = self._call("GET", "/users/overview")
        if 'id="users-table"' not in page:
            raise RuntimeError("Vaultwarden's list of people isn't what this expects")
        accounts = {}
        for row in ROW.findall(page):
            if "<th" in row:
                continue
            account, identifier = ACCOUNT.search(row), IDENTIFIER.search(row)
            if not account or not identifier:
                raise RuntimeError("Vaultwarden's list of people isn't what this expects")
            accounts[html.unescape(identifier.group(1)).strip()] = account.group(1)
        return accounts

    def apply(self, person, note):
        account_id = self._account_ids().get(f"{self.issuer}/{person.sub}")
        if account_id is None:
            return  # they've never signed in, so there's nothing to change
        everyone = json.loads(self._call("GET", "/users"))
        account = next(entry for entry in everyone if entry["id"] == account_id)
        if person.allowed:
            if not account["userEnabled"]:
                self._call("POST", f"/users/{account_id}/enable")
        elif account["userEnabled"]:
            self._note_shared_passwords(person, account, everyone, note)
            # Ends every token at once, and refuses a master password too, unlike deauth.
            self._call("POST", f"/users/{account_id}/disable")

    def _note_shared_passwords(self, person, account, everyone, note):
        """Its server can't read the entries, and they're passwords to other sites, so only a
        person can change them. This says which organisations, and warns of one left with
        no owner, as nobody could then manage it."""
        for organisation in account["organizations"]:
            owned_by_others = any(
                other["userEnabled"] and any(entry["id"] == organisation["id"] and entry["type"] == OWNER for entry in other["organizations"])
                for other in everyone
                if other["id"] != account["id"]
            )
            name = organisation["name"]
            if organisation["type"] == OWNER and not owned_by_others:
                note(
                    organisation["id"],
                    f"Nobody can manage {name} now. {person.email} was its only owner, and has lost access to the vault. "
                    f"Add them back to a group for long enough to make someone else an owner. "
                    f"Then remove them from {name}, and change every password they could see there.",
                )
            else:
                note(
                    organisation["id"],
                    f"Change the shared passwords in {name}. {person.email} could see them, and has lost access to the vault. "
                    f"An owner of {name} should remove them from it, then change every password they could see, "
                    f"at the site each one belongs to.",
                )
