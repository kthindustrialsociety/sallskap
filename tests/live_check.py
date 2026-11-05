#!/usr/bin/env python3
"""Checks a real, running installation from the outside: through Caddy, over HTTPS,
with a real certificate. Run on the server as root. It creates fake users at
@example.org, so don't run it on an installation with real members.

    python3 tests/live_check.py prepare   create fake users and run every check
    python3 tests/live_check.py verify    after a rebuild, reboot or second setup:
                                          did logins and data survive?
    python3 tests/live_check.py burst     many logins at once: does the memory limit hold?
    python3 tests/live_check.py backups   take a real backup and read it back. This one
                                          makes no fake users, so it's safe on a real
                                          installation, though logins pause for it
    python3 tests/live_check.py cleanup   remove the fake users

Standard library only, as the rest of the kit's tooling is.
"""

import base64
import fcntl
import hashlib
import hmac
import html
import http.client
import json
import re
import secrets
import shutil
import socket
import sqlite3
import ssl
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
import urllib.parse
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = Path("/root/.sallskap-live-check.json")
CONFIG = Path("/etc/sallskap/config.toml")
CORE_ENV = Path("/etc/sallskap/core.env")
GIT_ENV = Path("/etc/sallskap/git.env")
FORGEJO = "sallskap-forgejo-1"
FORGEJO_SERVICE_USER = "sallskap-admin"
VAULT = "sallskap-vaultwarden-1"
VAULT_DIR = Path("/srv/vault")
VAULT_ENV = Path("/etc/sallskap/vault.env")
VAULT_EMERGENCY = "sallskap-vault-emergency"
SYNC_ENV = Path("/etc/sallskap/sync.env")
GIT_SECRET = "the-contents-of-a-private-file-7f3a"
VAULT_UID = "10002"
VAULT_HASH = "bm90LWEtcmVhbC1tYXN0ZXItcGFzc3dvcmQtaGFzaA=="
VAULT_KEY = "2.bm90LXJlYWw=|bm90LXJlYWw=|bm90LXJlYWw="
VAULT_IDENTIFIER = "00000000-01DC-01DC-01DC-000000000000"  # the one Vaultwarden uses for itself
VAULT_SESSION_SECONDS = 12 * 3600
PASSWORD = "fake-user-long-password-42"
LOCKED_TEXT = "Too many failed login attempts"
FAKE = {
    "fake-member": (["member"], False),
    "fake-board-no2fa": (["board"], False),
    "fake-treasurer": (["treasurer"], True),
    "fake-operator": (["operator"], True),
    "fake-wrongcode": (["operator"], True),
    "fake-lockme": (["member"], False),
    "fake-link": (["member"], False),
    "fake-link-2fa": (["board"], True),
    "fake-tool-2fa": (["treasurer"], True),
    "fake-tool-member": (["member"], False),
    "fake-tool-off": (["member"], False),
    "fake-git-dev": (["developer"], False),
    "fake-git-keeper": (["developer"], False),
    "fake-git-board": (["board"], True),
    "fake-git-operator": (["operator"], True),
    "fake-git-member": (["member"], False),
    "fake-git-moving": (["member"], True),
    "fake-git-leaving": (["developer"], False),
    "fake-git-clash": (["member"], False),
    "fake-vault-board": (["board"], True),
    "fake-vault-operator": (["operator"], True),
    "fake-vault-board-no2fa": (["board"], False),
    "fake-vault-member": (["member"], False),
    "fake-vault-dev": (["developer"], False),
    "fake-vault-treasurer": (["treasurer"], True),
    "fake-vault-moving": (["member"], True),
    "fake-vault-newcomer": (["board"], True),
    "fake-vault-reused": (["operator"], True),
    "fake-vault-keeper": (["board"], True),
    "fake-vault-session": (["board"], True),
    "fake-vault-renamed": (["board"], True),
    "fake-vault-rules": (["board"], True),
    "fake-vault-offon": (["board"], True),
    "fake-sync-2fa": (["developer"], False),
    "fake-sync-joiner": (["member"], True),
    "fake-sync-creator": (["developer"], False),
    "fake-sync-colleague": (["developer"], False),
    "fake-sync-moved": (["developer"], True),
    "fake-sync-admin": (["operator"], True),
    "fake-sync-leaver": (["developer"], False),
    "fake-sync-deleted": (["developer"], False),
    "fake-sync-bypass": (["developer"], False),
    "fake-sync-outage": (["developer"], False),
    "fake-sync-nowork": (["developer"], False),
    "fake-sync-down": (["developer"], False),
    "fake-sync-blocked": (["developer"], False),
    "fake-sync-rescuer": (["operator"], True),
    "fake-sync-no2fa": (["developer"], False),
    "fake-vsync-owner": (["board"], True),
    "fake-vsync-removed": (["board"], True),
    "fake-vsync-off": (["board"], True),
    "fake-vsync-solo": (["board"], True),
    "fake-vsync-co": (["board"], True),
    "fake-vsync-deleted": (["board"], True),
    "fake-vsync-renamed": (["board"], True),
    "fake-vsync-outage": (["board"], True),
    "fake-vsync-down": (["board"], True),
    "fake-vsync-off-down": (["board"], True),
    "fake-vsync-member": (["member"], False),
}
# These can't be named fake-something, as what's being tested is the name itself.
FAKE_OTHER = ["fake-git-clash@example.net", f"{FORGEJO_SERVICE_USER}@example.org", "admin@example.org"]
FAKE_TOOL = "fake-tool"
FAKE_CALLBACK = "https://tool.example.org/callback"

failures = 0


def check(name, passed, detail=""):
    global failures
    print(f"  {'PASS' if passed else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not passed else ""))
    failures += 0 if passed else 1


def section(title):
    print(f"\n{title}")


def compose(*args):
    return subprocess.run(
        ["docker", "compose", "--project-directory", str(ROOT), *args], capture_output=True, text=True
    )


def in_core(code):
    """Run Python inside the core container and return what it prints."""
    result = compose("exec", "-T", "core", "python", "manage.py", "shell", "-v", "0", "-c", code)
    if result.returncode:
        raise SystemExit(f"Couldn't run code in core:\n{result.stderr}")
    return result.stdout.strip()


def docker_inspect(service):
    container = compose("ps", "-q", service).stdout.strip()
    return json.loads(subprocess.run(["docker", "inspect", container], capture_output=True, text=True).stdout)[0]


def kit(*args):
    return subprocess.run(["/usr/local/bin/kit", *args], capture_output=True, text=True)


def app_host():
    return f"app.{tomllib.loads(CONFIG.read_text())['association']['domain']}"


def totp(secret, offset=0):
    counter = int(time.time()) // 30 + offset
    digest = hmac.new(base64.b32decode(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    start = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[start : start + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


class Response:
    def __init__(self, raw):
        self.status = raw.status
        self.location = raw.getheader("Location", "")
        self.headers = raw.msg
        self.set_cookies = raw.msg.get_all("Set-Cookie") or []
        self.text = raw.read().decode("utf-8", "replace")

    def cookie(self, name):
        return next((c for c in self.set_cookies if c.startswith(f"{name}=")), "")


class Browser:
    """Just enough of a browser: keeps cookies, sends CSRF tokens, checks certificates."""

    def __init__(self, host, cookies=None, headers=None):
        self.host = host
        self.cookies = dict(cookies or {})
        self.headers = headers or {}
        self.context = ssl.create_default_context()

    def send(self, method, path, form=None):
        connection = http.client.HTTPSConnection(self.host, context=self.context, timeout=30)
        headers = dict(self.headers)
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        headers["Referer"] = f"https://{self.host}{path}"
        body = None
        if form is not None:
            body = urllib.parse.urlencode(form)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        connection.request(method, path, body, headers)
        response = Response(connection.getresponse())
        connection.close()
        for raw in response.set_cookies:
            name, _, value = raw.split(";")[0].partition("=")
            if value and "Max-Age=0" not in raw:
                self.cookies[name] = value
            else:
                self.cookies.pop(name, None)
        return response

    def get(self, path):
        return self.send("GET", path)

    def post_form(self, path, fields):
        """Open the page first for its CSRF token, as a person's browser would."""
        page = self.get(path)
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text)
        return self.send("POST", path, {"csrfmiddlewaretoken": token.group(1) if token else "", **fields})

    def follow(self, path):
        """Follow redirects, and return the final response and every URL visited."""
        visited = [path]
        response = self.get(path)
        for _ in range(8):
            if response.status not in (301, 302, 303, 307):
                break
            parsed = urllib.parse.urlparse(response.location)
            next_path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
            visited.append(next_path)
            response = self.get(next_path)
        return response, visited

    def log_in(self, email, password=PASSWORD):
        return self.post_form("/accounts/login/", {"login": email, "password": password})

    def enter_code(self, code):
        return self.post_form("/accounts/2fa/authenticate/", {"code": code})

    def logged_in(self):
        return self.get("/").status == 200


def email_of(name):
    return f"{name}@example.org"


def authorize_path(client_id, **params):
    query = {
        "client_id": client_id,
        "redirect_uri": FAKE_CALLBACK,
        "response_type": "code",
        "scope": "openid email profile groups",
        "state": "state-from-the-tool",
        "nonce": "nonce-from-the-tool",
        **params,
    }
    return f"/identity/o/authorize?{urllib.parse.urlencode(query)}"


def sent_back(response):
    """What the person was sent back to the tool with, if they were."""
    if response.status != 302 or not response.location.startswith(FAKE_CALLBACK):
        return None
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(response.location).query))


def tool_request(host, path, *, secret=None, client_id=None, bearer=None, form=None):
    """What a tool's own server sends, which comes with no browser or cookies."""
    connection = http.client.HTTPSConnection(host, context=ssl.create_default_context(), timeout=30)
    headers = {}
    if secret is not None:
        pair = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
        headers["Authorization"] = f"Basic {pair}"
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    body = None
    if form is not None:
        body = urllib.parse.urlencode(form)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    connection.request("POST" if form is not None else "GET", path, body, headers)
    response = Response(connection.getresponse())
    connection.close()
    return response


def exchange(host, tool, code, secret=None):
    return tool_request(
        host,
        "/identity/o/api/token",
        client_id=tool["client_id"],
        secret=tool["secret"] if secret is None else secret,
        form={"grant_type": "authorization_code", "code": code, "redirect_uri": FAKE_CALLBACK},
    )


def verified_claims(host, tool, id_token):
    """Check the signature against the key published over HTTPS, as a tool would.
    PyJWT is inside core, and this script sticks to the standard library."""
    keys = json.loads(Browser(host).get("/.well-known/jwks.json").text)["keys"]
    code = f"""
import json, jwt
token, keys = {id_token!r}, {keys!r}
try:
    kid = jwt.get_unverified_header(token)["kid"]
    key = next(jwt.PyJWK(k).key for k in keys if k["kid"] == kid)
    print(json.dumps(jwt.decode(token, key, algorithms=["RS256"], audience={tool["client_id"]!r}, issuer={f"https://{host}"!r})))
except Exception as error:
    print(json.dumps({{"refused": type(error).__name__}}))
"""
    return json.loads(in_core(code).splitlines()[-1])


def create_fake_users():
    code = f"""
import json
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
FAKE = {FAKE!r}
User = get_user_model()
secrets = {{}}
for name, (roles, two_factor) in FAKE.items():
    user = User.objects.create_user(name + "@example.org", {PASSWORD!r})
    user.groups.set(Group.objects.filter(role__key__in=roles))
    if two_factor:
        secrets[name] = generate_totp_secret()
        TOTP.activate(user, secrets[name])
for email in {FAKE_OTHER!r}:
    User.objects.create_user(email, {PASSWORD!r})
cache.clear()
print(json.dumps(secrets))
"""
    return json.loads(in_core(code).splitlines()[-1])


def clear_limits():
    in_core("from django.core.cache import cache; cache.clear()")


def data_snapshot():
    code = """
import json
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
print(json.dumps({
    "users": sorted(get_user_model().objects.values_list("email", "password")),
    "groups": sorted((g.pk, g.name, sorted(g.permissions.values_list("pk", flat=True))) for g in Group.objects.all()),
}))
"""
    return json.loads(in_core(code).splitlines()[-1])


def ssh_port():
    settings = subprocess.run(["sshd", "-T"], capture_output=True, text=True).stdout
    found = re.search(r"^port (\d+)$", settings, re.M)
    return found.group(1) if found else "22"


def env_fingerprint():
    return hashlib.sha256(CORE_ENV.read_bytes()).hexdigest()


# ---- Checks ---------------------------------------------------------------------


def check_exposure(host):
    section("Exposure")
    lines = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}|{{.Ports}}"], capture_output=True, text=True
    ).stdout.splitlines()
    publishing = sorted(line.split("|")[0] for line in lines if "->" in line)
    check("Only Caddy publishes ports", publishing == ["sallskap-caddy-1"], str(publishing))

    core = docker_inspect("core")
    check("Core has no published ports", not core["HostConfig"]["PortBindings"], str(core["HostConfig"]["PortBindings"]))
    check("Core has a memory limit", core["HostConfig"]["Memory"] > 0)
    check("Core's filesystem is read-only", core["HostConfig"]["ReadonlyRootfs"])
    check("Core drops all capabilities", core["HostConfig"]["CapDrop"] == ["ALL"], str(core["HostConfig"]["CapDrop"]))
    check("Core can't gain privileges", "no-new-privileges:true" in core["HostConfig"]["SecurityOpt"])
    check("Core restarts by itself", core["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped")
    uid = compose("exec", "-T", "core", "id", "-u").stdout.strip()
    check("Core runs as non-root", uid == "10001", uid)

    listening = subprocess.run(["ss", "-ltnH"], capture_output=True, text=True).stdout.splitlines()
    open_ports = set()
    for line in listening:
        address = line.split()[3]
        if address.startswith(("127.", "[::1]")):
            continue
        open_ports.add(address.rsplit(":", 1)[1])
    check("The server only listens on SSH, HTTP and HTTPS", open_ports <= {ssh_port(), "80", "443"}, str(sorted(open_ports)))

    public_ip = socket.gethostbyname(host)
    try:
        socket.create_connection((public_ip, 8000), 3).close()
        reachable = True
    except OSError:
        reachable = False
    check("Core's port can't be reached from the public address", not reachable)
    firewall = subprocess.run(["ufw", "status"], capture_output=True, text=True).stdout
    check("The firewall is on", "Status: active" in firewall)
    allowed = {line.split()[0] for line in firewall.splitlines() if " ALLOW " in line}
    expected = {f"{ssh_port()}/tcp", "80/tcp", "443/tcp", "443/udp"}
    check("The firewall only allows SSH, HTTP and HTTPS", allowed <= expected, str(sorted(allowed)))

    data = subprocess.run(["stat", "-c", "%u %a", "/srv/core"], capture_output=True, text=True).stdout.strip()
    check("Core's data folder is private to it", data == "10001 700", data)
    secrets_mode = subprocess.run(["stat", "-c", "%u %a", str(CORE_ENV)], capture_output=True, text=True).stdout.strip()
    check("The secrets file is readable by root only", secrets_mode == "0 600", secrets_mode)


def check_https(host):
    section("HTTPS and cookies")
    browser = Browser(host)
    page = browser.get("/accounts/login/")
    check("The login page loads over HTTPS with a valid certificate", page.status == 200, str(page.status))

    plain = http.client.HTTPConnection(host, 80, timeout=10)
    plain.request("GET", "/accounts/login/")
    answer = plain.getresponse()
    check(
        "Plain HTTP redirects to HTTPS",
        answer.status in (301, 308) and answer.getheader("Location", "").startswith("https://"),
        str(answer.status),
    )
    plain.close()

    check("HSTS is on", "max-age=" in (page.headers.get("Strict-Transport-Security") or ""))
    check("The site can't be framed", page.headers.get("X-Frame-Options") == "DENY")
    check("No server software is advertised", page.headers.get("Server") is None, str(page.headers.get("Server")))
    check("No proxy software is advertised", page.headers.get("Via") is None, str(page.headers.get("Via")))
    check("Logged-in pages aren't stored in shared caches", "public" not in (page.headers.get("Cache-Control") or ""))

    csrf = page.cookie("__Host-csrftoken")
    check("CSRF cookie is Secure", "Secure" in csrf, csrf)
    check("CSRF cookie has no Domain, so it stays on this hostname", "domain=" not in csrf.lower(), csrf)

    forged = Browser(host)
    forged.get("/accounts/login/")
    refused = forged.send("POST", "/accounts/login/", {"login": email_of("fake-member"), "password": PASSWORD})
    check("A login with no CSRF token is refused", refused.status == 403, str(refused.status))

    member = Browser(host)
    response = member.log_in(email_of("fake-member"))
    session = response.cookie("__Host-sessionid")
    check("Session cookie is set at login", bool(session), response.status)
    check("Session cookie is Secure", "Secure" in session, session)
    check("Session cookie is HttpOnly", "HttpOnly" in session, session)
    check("Session cookie is SameSite=Lax", "SameSite=Lax" in session, session)
    check("Session cookie has no Domain, so it stays on this hostname", "domain=" not in session.lower(), session)
    check("Session cookie uses path /", "Path=/" in session, session)

    other = Browser(host)
    other.context.check_hostname = False
    other.context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection(socket.gethostbyname(host), context=other.context, timeout=10)
    try:
        connection.request("GET", "/", headers={"Host": host.replace("app.", "evil.")})
        status = connection.getresponse().status
    except (ssl.SSLError, OSError):
        status = "refused"
    check("A wrong hostname doesn't reach the app", status != 200, str(status))


def check_login(host, secrets_by_name):
    section("Login")
    good = Browser(host)
    good.log_in(email_of("fake-member"))
    check("Right password logs in", good.logged_in())

    bad = Browser(host)
    bad.log_in(email_of("fake-member"), "not-the-password-at-all")
    check("Wrong password is refused", not bad.logged_in())

    case = Browser(host)
    case.log_in(email_of("fake-member").upper())
    check("Email ignores case", case.logged_in())

    partial = Browser(host)
    response = partial.log_in(email_of("fake-wrongcode"))
    check("With 2FA, a password alone goes to the code page", "/accounts/2fa/authenticate" in response.location, response.location)
    check("With 2FA, a password alone doesn't log in", not partial.logged_in())
    partial.enter_code("000000")
    check("A wrong code is refused", not partial.logged_in())

    operator = Browser(host)
    operator.log_in(email_of("fake-operator"))
    operator.enter_code(totp(secrets_by_name["fake-operator"]))
    check("Password and the right code log in", operator.logged_in())
    return operator


def check_lockout(host):
    section("Lockout")
    clear_limits()
    attacker = Browser(host)
    for _ in range(5):
        attacker.log_in(email_of("fake-lockme"), "not-the-password-at-all")
    response = attacker.log_in(email_of("fake-lockme"))
    check("After 5 wrong passwords, the right one is refused", LOCKED_TEXT in response.text and not attacker.logged_in())

    clear_limits()
    for attempt in range(10):
        faked = Browser(host, headers={"X-Forwarded-For": f"10.9.8.{attempt}"})
        faked.log_in(f"nobody{attempt}@example.org", "not-the-password-at-all")
    faked = Browser(host, headers={"X-Forwarded-For": "10.9.8.99"})
    response = faked.log_in(email_of("fake-member"))
    check("A faked X-Forwarded-For doesn't escape the limit per address", LOCKED_TEXT in response.text)

    keys = in_core(
        """
from django.db import connection
with connection.cursor() as c:
    c.execute("select cache_key from django_cache where cache_key like '%login_failed%/ip:%'")
    print("\\n".join(r[0] for r in c.fetchall()))
"""
    )
    addresses = {key.rsplit(":", 1)[1] if ":ip:" not in key else key.split(":ip:", 1)[1] for key in keys.splitlines()}
    caddy_ip = next(iter(docker_inspect("caddy")["NetworkSettings"]["Networks"].values()))["IPAddress"]
    check("Limits are counted per visitor address, not Caddy's", bool(addresses) and caddy_ip not in addresses, str(addresses))
    check("A faked address isn't what gets counted", not any(a.startswith("10.9.8.") for a in addresses), str(addresses))
    clear_limits()


def check_access(host, operator, secrets_by_name):
    section("Access")
    board = Browser(host)
    board.log_in(email_of("fake-board-no2fa"))
    final, visited = board.follow("/admin/")
    check("A Board member without 2FA is sent to set it up", "/accounts/2fa/" in visited[-1], str(visited))
    final, visited = board.follow("/admin/accounts/user/")
    check("...and can't reach the user list either", "/accounts/2fa/" in visited[-1], str(visited))

    treasurer = Browser(host)
    treasurer.log_in(email_of("fake-treasurer"))
    treasurer.enter_code(totp(secrets_by_name["fake-treasurer"]))
    check("A Treasurer with 2FA can open the admin page", treasurer.get("/admin/").status == 200)
    for path in ("/admin/accounts/user/", "/admin/auth/group/", "/admin/mfa/authenticator/"):
        status = treasurer.get(path).status
        check(f"A Treasurer can't see {path}", status == 403, str(status))

    check("An Operator with 2FA can see the user list", operator.get("/admin/accounts/user/").status == 200)
    check("An Operator can see groups", operator.get("/admin/auth/group/").status == 200)
    check("An Operator can see 2FA records", operator.get("/admin/mfa/authenticator/").status == 200)
    page = operator.get("/admin/mfa/authenticator/")
    match = re.search(r"/admin/mfa/authenticator/(\d+)/change/", page.text)
    if match:
        record = operator.get(match.group(0)).text
        leaked = any(secret in record for secret in secrets_by_name.values())
        check("An Operator can't read a 2FA secret", not leaked)

    member = Browser(host)
    member.log_in(email_of("fake-member"))
    final, _ = member.follow("/admin/")
    check("A Member is refused the admin page", final.status == 403, str(final.status))

    visitor = Browser(host)
    final, visited = visitor.follow("/admin/accounts/user/")
    check("A visitor is sent to the real login page", any(v.startswith("/accounts/login/") for v in visited), str(visited))

    direct = Browser(host)
    direct.get("/accounts/login/")  # for a valid CSRF token, so only the login itself is being tested
    token = direct.cookies.get("__Host-csrftoken", "")
    response = direct.send(
        "POST",
        "/admin/login/",
        {"csrfmiddlewaretoken": token, "username": email_of("fake-operator"), "password": PASSWORD},
    )
    check("Django's own admin login can't log anyone in", not direct.logged_in(), str(response.status))

    for path in ("/accounts/signup/", "/accounts/password/reset/", "/accounts/password/set/", "/accounts/email/"):
        status = Browser(host).get(path).status
        check(f"{path} doesn't exist", status == 404, str(status))


def check_links(host, secrets_by_name):
    section("One-time links")
    clear_limits()
    output = kit("link", email_of("fake-link"))
    match = re.search(rf"https://{re.escape(host)}(/set-password/\S+)", output.stdout)
    check("kit link prints a link", bool(match), output.stdout + output.stderr)
    if not match:
        return
    path = match.group(1)
    person = Browser(host)
    response = person.get(path)
    form_path = response.location
    response = person.post_form(form_path, {"new_password1": PASSWORD + "-new", "new_password2": PASSWORD + "-new"})
    check("The link accepts a new password", response.status == 302, str(response.status))
    check("The link doesn't log anyone in", not person.logged_in())
    person.log_in(email_of("fake-link"), PASSWORD + "-new")
    check("The new password works", person.logged_in())
    final, _ = Browser(host).follow(path)
    check("The link works only once", "expired or has already been used" in final.text)

    output = kit("link", email_of("fake-link-2fa"))
    path = re.search(rf"https://{re.escape(host)}(/set-password/\S+)", output.stdout).group(1)
    someone = Browser(host)
    response = someone.get(path)
    someone.post_form(response.location, {"new_password1": PASSWORD + "-new", "new_password2": PASSWORD + "-new"})
    someone.log_in(email_of("fake-link-2fa"), PASSWORD + "-new")
    check("A link doesn't get around 2FA", not someone.logged_in())
    someone.enter_code(totp(secrets_by_name["fake-link-2fa"]))
    check("...and the person's own code still works", someone.logged_in())

    refused = kit("link", "nobody@example.org")
    check("kit link refuses an unknown person", refused.returncode != 0, refused.stdout)


def check_recovery(host):
    section("Getting back in")
    clear_limits()
    locked = Browser(host)
    for _ in range(5):
        locked.log_in(email_of("fake-lockme"), "not-the-password-at-all")
    locked.log_in(email_of("fake-lockme"))
    check("Someone can be locked out at their address", not locked.logged_in())
    kit("admin", email_of("fake-lockme"))
    locked.log_in(email_of("fake-lockme"))
    check("kit admin clears a lockout", locked.logged_in())

    before = Browser(host)
    before.log_in(email_of("fake-wrongcode"))
    check("Before a 2FA reset, a password alone doesn't log in", not before.logged_in())
    output = kit("admin", email_of("fake-wrongcode"), "--reset-2fa")
    after = Browser(host)
    after.log_in(email_of("fake-wrongcode"))
    check("kit admin --reset-2fa removes 2FA", after.logged_in(), output.stdout + output.stderr)
    _, visited = after.follow("/admin/")
    check("...and they have to set it up again", "/accounts/2fa/" in visited[-1], str(visited))
    clear_limits()


def check_login_service(host, secrets_by_name):
    section("Login service for other tools")
    clear_limits()
    output = kit("tool", "add", FAKE_TOOL, FAKE_CALLBACK)
    fields = dict(re.findall(r"^\s+(Issuer|Client ID|Secret):\s+(\S+)$", output.stdout, re.M))
    check("kit tool add prints the settings a tool needs", len(fields) == 3, output.stdout + output.stderr)
    if len(fields) != 3:
        return None
    tool = {"client_id": fields["Client ID"], "secret": fields["Secret"]}
    check("kit tool add gives the right issuer", fields["Issuer"] == f"https://{host}", fields["Issuer"])
    again = kit("tool", "add", FAKE_TOOL, FAKE_CALLBACK)
    check("Adding the same tool twice is refused", again.returncode != 0, again.stdout)
    check("kit tool list shows it, with no secret", FAKE_TOOL in (listed := kit("tool", "list").stdout) and tool["secret"] not in listed, listed)

    data = json.loads(Browser(host).get("/.well-known/openid-configuration").text)
    check("Tools can find the service", data.get("issuer") == f"https://{host}", str(data.get("issuer")))
    check(
        "Every published address uses HTTPS and this hostname",
        all(data[k].startswith(f"https://{host}/") for k in ("authorization_endpoint", "token_endpoint", "userinfo_endpoint", "jwks_uri")),
    )
    check("The device flow isn't advertised", "device_authorization_endpoint" not in data)
    check("Only the code flow is advertised", data.get("grant_types_supported") == ["authorization_code"], str(data.get("grant_types_supported")))
    keys = json.loads(Browser(host).get("/.well-known/jwks.json").text)["keys"]
    check("One public signing key is published, and no private part", len(keys) == 1 and "d" not in keys[0])
    for path in ("/identity/o/device", "/identity/o/api/device/code", "/identity/o/api/revoke", "/identity/o/api/introspect", "/identity/o/api/clients"):
        status = Browser(host).get(path).status
        posted = Browser(host).send("POST", path, {}).status  # 403: Django's CSRF check comes first
        check(f"{path} doesn't exist", status == 404 and posted in (403, 404), f"{status} {posted}")

    member = Browser(host)
    member.log_in(email_of("fake-tool-member"))
    back = sent_back(member.get(authorize_path(tool["client_id"])))
    check("Someone already logged in goes straight back to the tool", bool(back and back.get("code")), str(back))
    if not back or "code" not in back:
        return tool
    check("The tool gets its state back", back.get("state") == "state-from-the-tool")
    answer = exchange(host, tool, back["code"])
    check("The tool can exchange the code", answer.status == 200, f"{answer.status} {answer.text[:200]}")
    tokens = json.loads(answer.text)
    claims = verified_claims(host, tool, tokens["id_token"])
    check("The signature checks out against the published key", "refused" not in claims, str(claims))
    check("It says who it is", claims.get("email") == email_of("fake-tool-member"), str(claims.get("email")))
    check("It passes on the person's groups", claims.get("groups") == ["member"], str(claims.get("groups")))
    check("It includes the tool's nonce", claims.get("nonce") == "nonce-from-the-tool")

    header, payload, signature = tokens["id_token"].split(".")
    forged = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    forged["groups"] = ["operator"]
    forged_payload = base64.urlsafe_b64encode(json.dumps(forged).encode()).rstrip(b"=").decode()
    refused = verified_claims(host, tool, f"{header}.{forged_payload}.{signature}")
    check("An altered statement fails the signature check", "refused" in refused, str(refused))

    info = tool_request(host, "/identity/o/api/userinfo", bearer=tokens["access_token"])
    check("The tool can ask who someone is", info.status == 200 and json.loads(info.text).get("groups") == ["member"], info.text[:200])
    in_url = tool_request(host, f"/identity/o/api/userinfo?access_token={tokens['access_token']}")
    check("A token in the address is refused", in_url.status == 401, str(in_url.status))
    used = exchange(host, tool, back["code"])
    check("A code works only once", used.status == 400, str(used.status))
    check("The tool gets no refresh token", "refresh_token" not in tokens, str(sorted(tokens)))
    fresh = sent_back(member.get(authorize_path(tool["client_id"])))
    wrong = exchange(host, tool, fresh["code"], secret="not-the-secret")
    check("A wrong secret is refused", wrong.status == 401, str(wrong.status))

    for label, path in (
        ("An unregistered return address", authorize_path(tool["client_id"], redirect_uri="https://evil.example.org/callback")),
        ("A return address with extra text", authorize_path(tool["client_id"], redirect_uri=f"{FAKE_CALLBACK}?next=https://evil.example.org/")),
        ("An unknown tool", authorize_path("no-such-tool")),
    ):
        response = member.get(path)
        check(f"{label} is refused with no redirect", response.status == 200 and not response.location, f"{response.status} {response.location}")

    board = Browser(host)
    board.log_in(email_of("fake-board-no2fa"))
    response = board.get(authorize_path(tool["client_id"]))
    check("A Board member without 2FA can't reach a tool", sent_back(response) is None and "/accounts/2fa/" in response.location, response.location)
    silent = sent_back(board.get(authorize_path(tool["client_id"], prompt="none")))
    check("...not even with a silent check", bool(silent) and silent.get("error") == "login_required" and "code" not in silent, str(silent))

    visitor = Browser(host)
    response = visitor.get(authorize_path(tool["client_id"]))
    check("A visitor is sent to log in first", response.location.startswith("/accounts/login/"), response.location)
    after_password = visitor.post_form(response.location, {"login": email_of("fake-tool-2fa"), "password": PASSWORD})
    check("With 2FA, a password alone doesn't reach the tool", "/accounts/2fa/authenticate" in after_password.location, after_password.location)
    after_code = visitor.enter_code(totp(secrets_by_name["fake-tool-2fa"]))
    back = sent_back(visitor.get(after_code.location)) if after_code.location else None
    check("With the code too, they go on to the tool", bool(back and back.get("code")), f"{after_code.location} {back}")
    if back and back.get("code"):
        answer = exchange(host, tool, back["code"])
        claims = verified_claims(host, tool, json.loads(answer.text)["id_token"])
        check("Their own groups come through", claims.get("groups") == ["treasurer"], str(claims.get("groups")))

    in_core(
        f"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
get_user_model().objects.get(email={email_of("fake-tool-member")!r}).groups.add(Group.objects.get(role__key="developer"))
"""
    )
    back = sent_back(member.get(authorize_path(tool["client_id"])))
    claims = verified_claims(host, tool, json.loads(exchange(host, tool, back["code"]).text)["id_token"])
    check("A group change shows at the next login", claims.get("groups") == ["developer", "member"], str(claims.get("groups")))

    leaving = Browser(host)
    leaving.log_in(email_of("fake-tool-off"))
    back = sent_back(leaving.get(authorize_path(tool["client_id"])))
    access_token = json.loads(exchange(host, tool, back["code"]).text)["access_token"]
    unused = sent_back(leaving.get(authorize_path(tool["client_id"])))
    in_core(f"from django.contrib.auth import get_user_model; get_user_model().objects.filter(email={email_of('fake-tool-off')!r}).update(is_active=False)")
    status = tool_request(host, "/identity/o/api/userinfo", bearer=access_token).status
    check("A turned-off account's token stops working", status == 401, str(status))
    status = exchange(host, tool, unused["code"]).status
    check("A turned-off account's code stops working", status == 400, str(status))
    response = leaving.get(authorize_path(tool["client_id"]))
    check("A turned-off account can't reach a tool", sent_back(response) is None, response.location)

    started = time.time()
    statuses = {
        tool_request(
            host,
            "/identity/o/api/token",
            client_id=tool["client_id"],
            secret="a-guess",
            form={"grant_type": "authorization_code", "code": "x", "redirect_uri": FAKE_CALLBACK},
        ).status
        for _ in range(20)
    }
    took = time.time() - started
    check("20 guesses at a tool's secret are refused, and cost little", statuses == {401} and took < 4, f"{statuses} in {took:.1f}s")
    clear_limits()
    return tool


# ---- Forgejo --------------------------------------------------------------------


def git_enabled():
    return tomllib.loads(CONFIG.read_text()).get("modules", {}).get("git", False)


def git_host(host):
    return host.replace("app.", "git.", 1)


def org_of(host):
    return host.split(".")[1]


def in_forgejo(*args):
    return subprocess.run(["docker", "exec", FORGEJO, *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)


class Person:
    """One browser, which keeps separate cookies for core and for Forgejo."""

    def __init__(self, host):
        self.host = host
        self.app = Browser(host)
        self.git = Browser(git_host(host))

    def log_in_to_core(self, email, code_secret=None):
        self.app.log_in(email)
        if code_secret:
            self.app.enter_code(totp(code_secret))

    def sign_in_to_git(self):
        return self.open_git(f"/user/oauth2/{org_of(self.host)}")

    def open_git(self, path):
        """Open a page and follow where it goes, across both hostnames. Returns the
        last place reached, as a hostname and path, and the answer from there."""
        browser = self.git
        last = f"{browser.host}{path}"
        response = browser.get(path)
        for _ in range(8):
            if response.status not in (301, 302, 303, 307):
                return last, response
            target = urllib.parse.urlparse(response.location)
            if target.netloc:  # an address with no hostname stays where it is
                browser = self.git if target.netloc == self.git.host else self.app
            path = target.path + (f"?{target.query}" if target.query else "")
            last = f"{browser.host}{path}"
            response = browser.get(path)
        return "too many redirects", response

    def in_git(self):
        """The real settings form, not just a 200: Forgejo answers someone it has
        blocked with a 200 page that says so."""
        page = self.git.get("/user/settings")
        return page.status == 200 and 'name="full_name"' in page.text

    def new_git_session(self):
        self.git = Browser(git_host(self.host))

    def make_token(self, name="fake-token"):
        """The way a person gets one: on the settings page, where no password is asked for."""
        scopes = ["write:repository", "write:organization", "write:user"]
        answer = self.git.send(
            "POST",
            "/user/settings/applications/tokens/new",
            [("name", name), ("resource", "all"), *[("scope", scope) for scope in scopes]],
        )
        found = re.search(r"\b[0-9a-f]{40}\b", self.git.get(answer.location or "/user/settings/applications").text)
        return found.group(0) if found else None


def forgejo_api(host, token, method, path, body=None, basic=None):
    connection = http.client.HTTPSConnection(git_host(host), context=ssl.create_default_context(), timeout=30)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"token {token}"
    if basic:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{basic[0]}:{basic[1]}".encode()).decode()
    connection.request(method, f"/api/v1{path}", json.dumps(body) if body is not None else None, headers)
    response = Response(connection.getresponse())
    connection.close()
    try:
        return response.status, json.loads(response.text)
    except ValueError:
        return response.status, None


def button_page(host):
    """Forgejo's sign-in page, which people only see just after a login error or signing out."""
    return Browser(git_host(host), cookies={"sallskap_stay": "1"}).get("/user/login")


def team_names(host, token):
    status, teams = forgejo_api(host, token, "GET", "/user/teams")
    return sorted(team["name"] for team in teams) if status == 200 else f"status {status}"


def run_git(*args, cwd=None):
    environment = {"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin", "HOME": "/tmp", "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=environment, timeout=120)


def repo_url(host, username, secret, repo):
    return f"https://{username}:{secret}@{git_host(host)}/{org_of(host)}/{repo}.git"


def set_groups(email, roles, wait=True):
    in_core(
        f"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
get_user_model().objects.get(email={email!r}).groups.set(Group.objects.filter(role__key__in={roles!r}))
"""
    )
    if wait:
        wait_for_sync()


def forgejo_tokens():
    """How many tokens exist, which only the database can say: it's read here, never changed."""
    code = "import sqlite3; print(sqlite3.connect('file:/srv/git/data/gitea.db?mode=ro', uri=True).execute('select count(*) from access_token').fetchone()[0])"
    return subprocess.run(["python3", "-c", code], capture_output=True, text=True).stdout.strip()


def forgejo_fingerprint():
    """What must not change when the setup runs again or the server restarts."""
    return {
        "secrets": hashlib.sha256(GIT_ENV.read_bytes()).hexdigest(),
        "login source": in_forgejo("forgejo", "admin", "auth", "list").stdout,
        "tool in core": kit("tool", "list").stdout,
        "number of tokens": forgejo_tokens(),
    }


def check_forgejo_exposure(host):
    section("Forgejo: exposure")
    info = json.loads(subprocess.run(["docker", "inspect", FORGEJO], capture_output=True, text=True).stdout)[0]
    config = info["HostConfig"]
    check("It has no published ports", not config["PortBindings"], str(config["PortBindings"]))
    check("It has a memory limit", config["Memory"] > 0)
    check("Its filesystem is read-only", config["ReadonlyRootfs"])
    check("It drops all capabilities", config["CapDrop"] == ["ALL"], str(config["CapDrop"]))
    check("It can't gain privileges", "no-new-privileges:true" in (config["SecurityOpt"] or []))
    check("It restarts by itself", config["RestartPolicy"]["Name"] == "unless-stopped")
    uid = in_forgejo("id", "-u").stdout.strip()
    check("It runs as non-root", uid == "1000", uid)
    # Both files: Go's dual-stack listeners only show up in the IPv6 one.
    listening = in_forgejo("cat", "/proc/net/tcp", "/proc/net/tcp6").stdout
    check("It listens on its web port, so this can see what's listening", ":0BB8 " in listening)  # port 3000
    check("Its SSH server isn't running", ":08AE " not in listening)  # port 2222
    data = subprocess.run(["stat", "-c", "%u %a", "/srv/git"], capture_output=True, text=True).stdout.strip()
    check("Its data folder is private to it", data == "1000 700", data)
    secrets_mode = subprocess.run(["stat", "-c", "%u %a", str(GIT_ENV)], capture_output=True, text=True).stdout.strip()
    check("Its secrets file is readable by root only", secrets_mode == "0 600", secrets_mode)
    try:
        socket.create_connection((socket.gethostbyname(git_host(host)), 2222), 3).close()
        reachable = True
    except OSError:
        reachable = False
    check("Port 2222 can't be reached from the public address", not reachable)


def check_forgejo_https(host):
    section("Forgejo: HTTPS and cookies")
    page = button_page(host)
    check("The sign-in page loads over HTTPS with a valid certificate", page.status == 200, str(page.status))
    plain = http.client.HTTPConnection(git_host(host), 80, timeout=10)
    plain.request("GET", "/")
    answer = plain.getresponse()
    check("Plain HTTP redirects to HTTPS", answer.status in (301, 308) and answer.getheader("Location", "").startswith("https://"), str(answer.status))
    plain.close()
    check("HSTS is on", "max-age=" in (page.headers.get("Strict-Transport-Security") or ""))
    check("The site can't be framed by others", page.headers.get("X-Frame-Options") in ("SAMEORIGIN", "DENY"))
    check("No server software is advertised", page.headers.get("Server") is None, str(page.headers.get("Server")))
    check("No proxy software is advertised", page.headers.get("Via") is None, str(page.headers.get("Via")))
    landing = Browser(git_host(host)).get("/")
    check("The first page leads to signing in", landing.location.endswith("/user/login"), landing.location)

    person = Person(host)
    person.log_in_to_core(email_of("fake-git-dev"))
    start = person.git.get(f"/user/oauth2/{org_of(host)}")
    cookies = start.set_cookies
    person.sign_in_to_git()
    session = next((c for c in person.git.get("/user/settings").set_cookies + cookies if c.startswith("session=")), "")
    check("Session cookie is Secure", "Secure" in session, session)
    check("Session cookie is HttpOnly", "HttpOnly" in session, session)
    check("Session cookie has no Domain, so it stays on this hostname", "domain=" not in session.lower(), session)
    check("Signing in leaves no long-lived cookie", "persistent" not in person.git.cookies, str(sorted(person.git.cookies)))


def check_forgejo_single_login(host, secrets_by_name):
    section("Forgejo: one login")
    page = button_page(host)
    check("The sign-in page offers the login service", f"/user/oauth2/{org_of(host)}" in page.text)
    check("The sign-in page has no password box", 'type="password"' not in page.text)

    person = Person(host)
    person.log_in_to_core(email_of("fake-git-dev"))
    last, final = person.open_git("/")
    check("Someone logged in to core who opens Forgejo gets in, with nothing to click", person.in_git(), f"{last} {final.status}")
    home = person.git.get("/")
    check("Once in, the first page is their dashboard, not the sign-in page", home.status == 200 and "/user/login" not in home.location and 'name="full_name"' not in home.text and "dashboard" in home.text.lower(), f"{home.status} {home.location}")
    check("...which the logo leads to", 'id="navbar-logo"' in home.text and re.search(r'id="navbar-logo"[^>]*href="/"|href="/"[^>]*id="navbar-logo"', home.text) is not None)

    visitor = Person(host)
    last, _ = visitor.open_git("/")
    check("A visitor who opens Forgejo is sent to core's login page", "/accounts/login/" in last, last)
    check("...and isn't in yet", not visitor.in_git())

    looping = Browser(git_host(host))
    first = looping.get("/user/login")
    check("Opening the sign-in page goes straight to the login service", first.status == 302 and urllib.parse.urlparse(first.location).path == f"/user/oauth2/{org_of(host)}", f"{first.status} {first.location}")
    second = looping.get("/user/login")
    check("Coming straight back shows the page, so a login error can't loop", second.status == 200, f"{second.status} {second.location}")

    returning = Person(host)
    returning.log_in_to_core(email_of("fake-git-dev"))
    last, final = returning.open_git("/user/login?redirect_to=%2Fexplore%2Frepos")
    check("Signing in from a page returns to that page", last.endswith("/explore/repos") and final.status == 200, f"{last} {final.status}")

    board = Person(host)
    response = board.git.get(f"/user/oauth2/{org_of(host)}")
    target = urllib.parse.urlparse(response.location)
    login = board.app.get(f"{target.path}?{target.query}")
    after_password = board.app.post_form(login.location, {"login": email_of("fake-git-board"), "password": PASSWORD})
    check("With 2FA, a password alone goes to the code page", "/accounts/2fa/authenticate" in after_password.location, after_password.location)
    check("...and doesn't get into Forgejo", not board.in_git())
    after_code = board.app.enter_code(totp(secrets_by_name["fake-git-board"]))
    authorize = board.app.get(after_code.location)
    back = urllib.parse.urlparse(authorize.location)
    board.git.get(f"{back.path}?{back.query}")
    check("With the code too, they're in, with nothing more to enter", board.in_git(), authorize.location)

    no_2fa = Person(host)
    no_2fa.log_in_to_core(email_of("fake-board-no2fa"))
    last, _ = no_2fa.sign_in_to_git()
    check("A Board member without 2FA is sent to set it up", "/accounts/2fa/" in last, last)
    check("...and doesn't get in", not no_2fa.in_git())

    forger = Person(host)
    forger.git.get(f"/user/oauth2/{org_of(host)}/callback?code=made-up&state=made-up")
    check("A made-up login code doesn't get in", not forger.in_git())

    # This browser never expires cookies, so the one from signing in has to go first,
    # or it would hide whether signing out sets its own.
    person.git.cookies.pop("sallskap_stay", None)
    person.git.send("POST", "/user/logout", {})
    check("Signing out of Forgejo works", not person.in_git())
    check("...and leaves them logged in to core", person.app.logged_in())
    last, _ = person.open_git("/")
    check("Opening Forgejo right after doesn't sign them straight back in", not person.in_git(), last)
    person.git.cookies.pop("sallskap_stay", None)  # as if the few minutes had passed
    person.open_git("/")
    check("...but after a few minutes it does, as they're still logged in to core", person.in_git())


def check_forgejo_closed_doors(host):
    section("Forgejo: no way around core")
    browser = Browser(git_host(host))
    posted = browser.send("POST", "/user/login", {"user_name": FORGEJO_SERVICE_USER, "password": "not-a-real-password"})
    check("Local password login is refused", posted.status == 403, str(posted.status))
    posted = browser.send("POST", "/user/sign_up", {"user_name": "fake-signup", "email": "fake-signup@example.org", "password": PASSWORD, "retype": PASSWORD})
    check("Signing up directly is refused", posted.status == 403, str(posted.status))
    check("Signing up made no account", "fake-signup" not in in_forgejo("forgejo", "admin", "user", "list").stdout)
    # A redirect to the sign-in page wouldn't show this: that's also what the route
    # does when it's on, for someone who isn't signed in.
    status = Browser(git_host(host)).get("/.well-known/openid-configuration").status
    check("Forgejo's own login service isn't available", status == 404, str(status))
    for path, label in (
        ("/user/login/openid", "OpenID sign-in"),
        ("/user/forgot_password", "Password reset"),
    ):
        answer = Browser(git_host(host)).get(path)
        check(f"{label} isn't available", answer.status in (403, 404) or "/user/login" in answer.location or "disabled" in answer.text.lower(), f"{answer.status} {answer.location}")
    install = Browser(git_host(host)).get("/install")
    check("The first-run setup page isn't there", install.status == 404 or "/user/login" in install.location, f"{install.status} {install.location}")
    faked = Browser(git_host(host), headers={"X-WEBAUTH-USER": FORGEJO_SERVICE_USER})
    check("A faked reverse-proxy login header gets nowhere", faked.get("/user/settings").status != 200)
    status = faked.get("/api/v1/user").status
    check("...on the API either", status == 401, str(status))

    section("Forgejo: personal data, to a visitor")
    people = Browser(git_host(host))
    explore = people.get("/explore/users")
    check("The list of people isn't public", explore.status in (303, 403, 404) and "/explore/users" not in explore.location, f"{explore.status} {explore.location}")
    final, _ = people.follow("/explore/users")
    check("...and no one's name is where it leads", "fake-git" not in final.text)
    status, _ = forgejo_api(host, None, "GET", "/users/search?q=fake")
    check("Searching for people isn't public", status in (401, 403, 404), str(status))
    check("Someone's page isn't public", Browser(git_host(host)).get("/fake-git-dev").status == 404)
    check("The organisation's page names no one", "fake-git" not in Browser(git_host(host)).get(f"/{org_of(host)}").text)
    status, members = forgejo_api(host, None, "GET", f"/orgs/{org_of(host)}/public_members")
    check("Nor does its public list of members", not members, str(members))


def check_forgejo_password_door(host, person, username, token):
    """A Forgejo password must not open anything, however someone came to have one."""
    section("Forgejo: a password on someone's account")
    page = person.git.get("/user/settings/account")
    check("Their account page doesn't offer to set one", 'name="retype"' not in page.text)
    check("...or to delete their account", "/user/settings/account/delete" not in page.text)
    chosen = "fake-forgejo-password-42"
    set_it = in_forgejo("forgejo", "admin", "user", "change-password", "--username", username, "--password", chosen, "--must-change-password=false")
    check("One can be set from the server, for this test", set_it.returncode == 0, set_it.stderr[:200])
    fresh = Browser(git_host(host))
    posted = fresh.send("POST", "/user/login", {"user_name": username, "password": chosen})
    check("It doesn't log in on the web", posted.status == 403, str(posted.status))
    status, _ = forgejo_api(host, None, "GET", "/user", basic=(username, chosen))
    check("It doesn't work on the API", status == 401, str(status))
    result = run_git("ls-remote", repo_url(host, username, chosen, "fake-repo"))
    check("It doesn't work with Git", result.returncode != 0, result.stdout)
    result = run_git("ls-remote", repo_url(host, username, token, "fake-repo"))
    check("A token still does", result.returncode == 0, result.stderr)


def check_forgejo_groups(host, secrets_by_name):
    section("Forgejo: groups decide teams")
    people = {}

    def signed_in(name, code=False):
        person = Person(host)
        person.log_in_to_core(email_of(name), secrets_by_name[name] if code else None)
        person.sign_in_to_git()
        token = person.make_token()
        check(f"{name} can make a token on the settings page", bool(token))
        people[name] = (person, token)
        return person, token

    _, token = signed_in("fake-git-dev")
    check("Developer: on the developers team", team_names(host, token) == ["developers"], str(team_names(host, token)))
    check("Developer: not a Forgejo admin", forgejo_api(host, token, "GET", "/user")[1].get("is_admin") is False)

    _, token = signed_in("fake-git-board", code=True)
    check("Board: on the board team", team_names(host, token) == ["board"], str(team_names(host, token)))
    check("Board: not a Forgejo admin", forgejo_api(host, token, "GET", "/user")[1].get("is_admin") is False)

    _, token = signed_in("fake-git-operator", code=True)
    check("Operator: an owner of the organisation", team_names(host, token) == ["Owners"], str(team_names(host, token)))
    check("Operator: a Forgejo admin", forgejo_api(host, token, "GET", "/user")[1].get("is_admin") is True)

    _, token = signed_in("fake-git-member")
    check("Member: on no team", team_names(host, token) == [], str(team_names(host, token)))
    check("Member: not a Forgejo admin", forgejo_api(host, token, "GET", "/user")[1].get("is_admin") is False)

    section("Forgejo: changing someone's groups")
    moving, token = signed_in("fake-git-moving", code=True)
    check("They start on no team", team_names(host, token) == [], str(team_names(host, token)))

    def next_login(roles):
        set_groups(email_of("fake-git-moving"), roles)
        moving.new_git_session()
        moving.sign_in_to_git()

    next_login(["developer"])
    check("Adding them to Developer puts them on the team at next login", team_names(host, token) == ["developers"], str(team_names(host, token)))
    next_login(["member"])
    check("Removing them takes them off the team at next login", team_names(host, token) == [], str(team_names(host, token)))
    next_login(["operator"])
    check("Adding them to Operator makes them an admin and owner", forgejo_api(host, token, "GET", "/user")[1].get("is_admin") is True and team_names(host, token) == ["Owners"], str(team_names(host, token)))
    next_login(["member"])
    check("Removing them takes both away", forgejo_api(host, token, "GET", "/user")[1].get("is_admin") is False and team_names(host, token) == [], str(team_names(host, token)))
    return people


def check_forgejo_git(host, people):
    section("Forgejo: what each team can do with Git")
    org = org_of(host)
    developer = people["fake-git-dev"][1]
    status, _ = forgejo_api(host, developer, "POST", f"/orgs/{org}/repos", {"name": "fake-repo", "private": True})
    check("A Developer can create a repo in the organisation", status == 201, str(status))
    form = people["fake-git-dev"][0].git.get("/repo/create").text
    check("The new repo form starts on private", bool(re.search(r'<input[^>]*name="private"[^>]*checked', form)))

    # Forgejo's API answers this refusal with a 500, so what counts is that no repo appears.
    status, _ = forgejo_api(host, developer, "POST", "/user/repos", {"name": "fake-personal"})
    exists = forgejo_api(host, developer, "GET", "/repos/fake-git-dev/fake-personal")[0]
    check("A Developer can't create a personal repo", status != 201 and exists == 404, f"{status} {exists}")
    owners = re.findall(r'data-value="\d+"[^>]*>.*?<span[^>]*>\s*([^<\s]+)', form, re.S)
    check("The new repo form only offers the organisation", org in owners and "fake-git-dev" not in owners, str(owners))
    member = people["fake-git-member"][1]
    status, _ = forgejo_api(host, member, "POST", "/user/repos", {"name": "fake-personal"})
    exists = forgejo_api(host, member, "GET", "/repos/fake-git-member/fake-personal")[0]
    check("A Member can't create a personal repo", status != 201 and exists == 404, f"{status} {exists}")
    status, _ = forgejo_api(host, member, "POST", f"/orgs/{org}/repos", {"name": "fake-members-repo"})
    check("A Member can't create a repo in the organisation", status == 403, str(status))
    status, _ = forgejo_api(host, member, "POST", "/orgs", {"username": "fake-org"})
    check("A Member can't create an organisation", status in (403, 422), str(status))

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp, "work")
        work.mkdir()
        (work / "file.txt").write_text("from the live check\n")
        for args in (("init", "-q", "-b", "main"), ("add", "."), ("-c", "user.name=Fake", "-c", "user.email=fake@example.org", "commit", "-q", "-m", "first")):
            run_git(*args, cwd=work)
        pushed = run_git("push", "-q", repo_url(host, "fake-git-dev", developer, "fake-repo"), "main", cwd=work)
        check("A Developer can push with a token", pushed.returncode == 0, pushed.stderr[:300])
        cloned = run_git("clone", "-q", repo_url(host, "fake-git-dev", developer, "fake-repo"), str(Path(tmp, "dev")))
        check("A Developer can clone it", cloned.returncode == 0 and Path(tmp, "dev", "file.txt").exists(), cloned.stderr[:300])

        board = people["fake-git-board"][1]
        cloned = run_git("clone", "-q", repo_url(host, "fake-git-board", board, "fake-repo"), str(Path(tmp, "board")))
        check("Board can clone it", cloned.returncode == 0, cloned.stderr[:300])
        pushed = run_git("push", "-q", repo_url(host, "fake-git-board", board, "fake-repo"), "main:board-branch", cwd=work)
        check("Board can't push to it", pushed.returncode != 0, pushed.stdout)
        # After the push, as an empty repo might be refused for being empty.
        status, _ = forgejo_api(host, board, "POST", f"/repos/{org}/fake-repo/forks", {})
        exists = forgejo_api(host, board, "GET", "/repos/fake-git-board/fake-repo")[0]
        check("Board can't fork it into a copy of their own", status != 202 and exists == 404, f"{status} {exists}")

        cloned = run_git("clone", "-q", repo_url(host, "fake-git-member", member, "fake-repo"), str(Path(tmp, "member")))
        check("A Member can't clone it", cloned.returncode != 0, cloned.stdout)
        cloned = run_git("ls-remote", f"https://{git_host(host)}/{org}/fake-repo.git")
        check("Someone not logged in can't reach it", cloned.returncode != 0, cloned.stdout)

        leaving = Person(host)
        leaving.log_in_to_core(email_of("fake-git-leaving"))
        leaving.sign_in_to_git()
        token = leaving.make_token()
        check("Before leaving the team, they can push", run_git("push", "-q", repo_url(host, "fake-git-leaving", token, "fake-repo"), "main:leaving-branch", cwd=work).returncode == 0)
        set_groups(email_of("fake-git-leaving"), ["member"])
        leaving.new_git_session()
        leaving.sign_in_to_git()
        pushed = run_git("push", "-q", repo_url(host, "fake-git-leaving", token, "fake-repo"), "main:leaving-branch-2", cwd=work)
        check("After leaving, at next login, their token can't push", pushed.returncode != 0, pushed.stdout)


def check_forgejo_names(host):
    section("Forgejo: usernames can't take over an account")
    first = Person(host)
    first.log_in_to_core(email_of("fake-git-clash"))
    first.sign_in_to_git()
    first_token = first.make_token()
    first_name = forgejo_api(host, first_token, "GET", "/user")[1].get("login")
    check("The first person gets the plain name", first_name == "fake-git-clash", str(first_name))

    second = Person(host)
    second.log_in_to_core("fake-git-clash@example.net")
    second.sign_in_to_git()
    second_token = second.make_token()
    second_name = (forgejo_api(host, second_token, "GET", "/user")[1] or {}).get("login", "") if second_token else ""
    check("Someone with the same name gets their own account", second_name.startswith("fake-git-clash-"), str(second_name))

    for email, label in ((f"{FORGEJO_SERVICE_USER}@example.org", "the account that owns the organisation"), ("admin@example.org", "a name Forgejo reserves")):
        person = Person(host)
        person.log_in_to_core(email)
        _, final = person.sign_in_to_git()
        check(f"An address matching {label} gets an error, not the account", not person.in_git(), str(final.status))
        print(f"  info  It answered {final.status}.")

    section("Forgejo: addresses come from core")
    someone = "fake-git-clash@example.net"
    answer = first.git.send("POST", "/user/settings/account/email", {"email": someone})
    check("The form won't add someone else's address", answer.status == 403, f"{answer.status} {answer.location}")
    for path in ("/user//settings/account/email", "/user/settings/account/email/", "/user/./settings/account/email"):
        answer = first.git.send("POST", path, {"email": someone})
        check(f"...not through {path} either", answer.status != 303 or "/user/settings/account" not in answer.location, f"{answer.status} {answer.location}")
    status, _ = forgejo_api(host, first_token, "POST", "/user/emails", {"emails": [someone]})
    check("Nor will the API", status == 403, str(status))
    addresses = [item["email"] for item in forgejo_api(host, first_token, "GET", "/user/emails")[1]]
    check("...and it hasn't been added", someone not in addresses, str(addresses))
    check("Their own address is still there", email_of("fake-git-clash") in addresses, str(addresses))

    other = (forgejo_api(host, first_token, "GET", f"/users/{second_name}")[1] or {}).get("email", "")
    check("A logged-in person can't see someone else's address", "example.net" not in other, other)


def check_forgejo_setup_repeats(host):
    section("Forgejo: running the setup again")
    before = forgejo_fingerprint()
    output = kit("enable", "git")
    check("kit enable git runs again", output.returncode == 0, output.stdout[-300:] + output.stderr[-300:])
    after = forgejo_fingerprint()
    for label in before:
        check(f"Its {label} didn't change", before[label] == after[label], f"{before[label]} {after[label]}")


def check_forgejo(host, secrets_by_name):
    clear_limits()
    check_forgejo_exposure(host)
    check_forgejo_https(host)
    check_forgejo_closed_doors(host)
    check_forgejo_single_login(host, secrets_by_name)
    time.sleep(31)  # each code works once, and fake-git-board logs in again below
    people = check_forgejo_groups(host, secrets_by_name)
    check_forgejo_git(host, people)
    check_forgejo_password_door(host, people["fake-git-dev"][0], "fake-git-dev", people["fake-git-dev"][1])
    check_forgejo_names(host)
    check_forgejo_setup_repeats(host)
    clear_limits()

    keeper = Person(host)
    keeper.log_in_to_core(email_of("fake-git-keeper"))
    keeper.sign_in_to_git()
    token = keeper.make_token()
    fingerprint = forgejo_fingerprint()  # after that token, which is the last one made
    return {
        "cookies": keeper.git.cookies,
        "core_cookies": keeper.app.cookies,
        "token": token,
        "operator_token": people["fake-git-operator"][1],
        "fingerprint": fingerprint,
    }


def verify_forgejo(host, state):
    section("Forgejo: did logins and data survive?")
    keeper = Person(host)
    keeper.git.cookies = dict(state["cookies"])
    check("A session is still signed in", keeper.in_git())
    check("A token still works", team_names(host, state["token"]) == ["developers"], str(team_names(host, state["token"])))
    fresh = Person(host)
    fresh.app.cookies = dict(state["core_cookies"])
    fresh.sign_in_to_git()
    check("A new sign-in still works", fresh.in_git())
    with tempfile.TemporaryDirectory() as tmp:
        cloned = run_git("clone", "-q", repo_url(host, "fake-git-keeper", state["token"], "fake-repo"), str(Path(tmp, "check")))
        check("A repo is still there, with its contents", cloned.returncode == 0 and Path(tmp, "check", "file.txt").exists(), cloned.stderr[:300])
    now = forgejo_fingerprint()
    for label, value in now.items():
        check(f"Its {label} didn't change", value == state["fingerprint"][label], f"{value} {state['fingerprint'][label]}")
    health = json.loads(subprocess.run(["docker", "inspect", FORGEJO], capture_output=True, text=True).stdout)[0]["State"].get("Health", {})
    check("It's healthy", health.get("Status") == "healthy", str(health.get("Status")))
    check_forgejo_exposure(host)


def burst_forgejo(host, state):
    section("Many clones at once")
    info = json.loads(subprocess.run(["docker", "inspect", FORGEJO], capture_output=True, text=True).stdout)[0]
    cgroup = Path(f"/sys/fs/cgroup/system.slice/docker-{info['Id']}.scope")
    oom_before = _oom_kills(cgroup)
    results = []

    def one_clone(target):
        results.append(run_git("clone", "-q", repo_url(host, "fake-git-keeper", state["token"], "fake-repo"), target).returncode == 0)

    with tempfile.TemporaryDirectory() as tmp:
        threads = [threading.Thread(target=one_clone, args=(str(Path(tmp, str(n))),)) for n in range(12)]
        started = time.time()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    limit_mb = info["HostConfig"]["Memory"] / 1024**2
    peak_file = cgroup / "memory.peak"
    peak_mb = int(peak_file.read_text()) / 1024**2 if peak_file.exists() else None
    print(f"  info  12 clones took {time.time() - started:.1f}s. Peak memory {peak_mb and round(peak_mb)} MB of a {limit_mb:.0f} MB limit.")
    check("All 12 clones succeeded", all(results) and len(results) == 12, f"{sum(results)} of {len(results)}")
    check("Nothing was killed for using too much memory", _oom_kills(cgroup) == oom_before)


def cleanup_forgejo():
    host = app_host()
    token = sync_env().get("FORGEJO_ADMIN_TOKEN")
    org = org_of(host)
    if token:
        repos = forgejo_api(host, token, "GET", f"/orgs/{org}/repos?limit=50")[1] or []
        for repo in repos:
            if repo["name"].startswith("fake-"):
                print(f"Fake repo {repo['name']}:", forgejo_api(host, token, "DELETE", f"/repos/{org}/{repo['name']}")[0])
    listed = in_forgejo("forgejo", "admin", "user", "list").stdout
    for name in re.findall(r"^\d+\s+(fake-\S+)", listed, re.M):
        in_forgejo("forgejo", "admin", "user", "delete", "--username", name, "--purge")
    left = re.findall(r"^\d+\s+(fake-\S+)", in_forgejo("forgejo", "admin", "user", "list").stdout, re.M)
    print(f"Fake accounts left in Forgejo: {len(left)}")


# ---- Vaultwarden ---------------------------------------------------------------


def vault_enabled():
    return tomllib.loads(CONFIG.read_text()).get("modules", {}).get("vault", False)


def vault_host(host):
    return host.replace("app.", "vault.", 1)


def in_vault(*args):
    return subprocess.run(["docker", "exec", VAULT, *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)


def vault_db(query, *values):
    """What only its database can say: it's read here, never changed."""
    connection = sqlite3.connect(f"file:{VAULT_DIR}/db.sqlite3?mode=ro", uri=True)
    try:
        return connection.execute(query, values).fetchall()
    finally:
        connection.close()


def vault_account(email):
    found = vault_db("select uuid from users where email = ?", email)
    return found[0][0] if found else None


def clear_vault_limits():
    """Its login limits are kept in memory, and every check here comes from one address."""
    subprocess.run(["docker", "restart", VAULT], capture_output=True)
    wait_for_vault()


def vault_health():
    return json.loads(subprocess.run(["docker", "inspect", VAULT], capture_output=True, text=True).stdout)[0]["State"].get("Health", {}).get("Status")


def wait_for_vault():
    for _ in range(40):
        if vault_health() == "healthy":
            return
        time.sleep(1)
    raise SystemExit("Vaultwarden didn't come back.")


class VaultPerson:
    """One browser, doing what Vaultwarden's web page does with its API. Keys here are
    placeholders, as the server only stores them, so the real web page can't open these
    accounts. Real encryption only happens in a real browser, so it's checked in one."""

    def __init__(self, host):
        self.host = host
        self.app = Browser(host)
        self.vault = Browser(vault_host(host))
        self.device = str(uuid.uuid4())
        self.tokens = {}
        self.code = self.verifier = None

    def log_in_to_core(self, email, code_secret=None):
        self.app.log_in(email)
        if code_secret:
            self.app.enter_code(totp(code_secret))

    def start_path(self):
        """Where the web page sends someone who clicks its sign-in button."""
        self.code = None
        self.verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).rstrip(b"=").decode()
        query = {
            "client_id": "web",
            "redirect_uri": f"https://{self.vault.host}/sso-connector.html",
            "response_type": "code",
            "scope": "api offline_access",
            "state": secrets.token_urlsafe(16),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "response_mode": "query",
        }
        return f"/identity/connect/authorize?{urllib.parse.urlencode(query)}"

    def click_sign_in(self):
        """Follow the redirects across both hostnames. Returns the last place reached,
        as a hostname and path, and the answer from there."""
        browser, path = self.vault, self.start_path()
        last, response = f"{browser.host}{path}", None
        for _ in range(8):
            response = browser.get(path)
            if response.status not in (301, 302, 303, 307):
                return last, response
            target = urllib.parse.urlparse(response.location)
            if target.path == "/sso-connector.html":  # back on the web page, which takes it from here
                self.code = dict(urllib.parse.parse_qsl(target.query)).get("code")
                return f"{self.vault.host}{target.path}", response
            if target.netloc:
                browser = self.vault if target.netloc == self.vault.host else self.app
            path = target.path + (f"?{target.query}" if target.query else "")
            last = f"{browser.host}{path}"
        return "too many redirects", response

    def identity(self, **form):
        answer = self.vault.send("POST", "/identity/connect/token", {"client_id": "web", **form})
        try:
            return answer.status, json.loads(answer.text)
        except ValueError:
            return answer.status, {}

    def redeem(self, code=None, verifier=None):
        status, data = self.identity(
            grant_type="authorization_code",
            code=code or self.code or "",
            code_verifier=verifier or self.verifier or "",
            redirect_uri=f"https://{self.vault.host}/sso-connector.html",
            scope="api offline_access",
            deviceType="9",
            deviceIdentifier=self.device,
            deviceName="chrome",
        )
        if status == 200:
            self.tokens = data
        return status, data

    def sign_in(self):
        self.click_sign_in()
        return self.redeem()

    def refresh(self):
        status, data = self.identity(grant_type="refresh_token", refresh_token=self.tokens.get("refresh_token", ""))
        if status == 200:
            self.tokens = {**self.tokens, **data}
        return status, data

    def api(self, method, path, body=None):
        connection = http.client.HTTPSConnection(self.vault.host, context=self.vault.context, timeout=30)
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.tokens.get('access_token', '')}"}
        connection.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = Response(connection.getresponse())
        connection.close()
        try:
            return response.status, json.loads(response.text)
        except ValueError:
            return response.status, response.text

    def set_master_password(self, hint=None):
        return self.api(
            "POST",
            "/api/accounts/set-password",
            {
                "masterPasswordHash": VAULT_HASH,
                "masterPasswordHint": hint,
                "key": VAULT_KEY,
                "kdf": 0,
                "kdfIterations": 600000,
                "keys": {"encryptedPrivateKey": VAULT_KEY, "publicKey": "not-a-real-public-key"},
            },
        )

    def in_vault(self):
        status, data = self.api("GET", "/api/sync")
        return status == 200 and isinstance(data, dict) and "profile" in data

    def sync(self):
        return self.api("GET", "/api/sync")[1]


def vault_person(host, name, secrets_by_name, master_password=True):
    """Someone signed in to core and to Vaultwarden, ready to use it."""
    person = VaultPerson(host)
    person.log_in_to_core(email_of(name), secrets_by_name.get(name))
    person.sign_in()
    if master_password:
        person.set_master_password()
    return person


def check_vault_exposure(host):
    section("Vaultwarden: exposure")
    info = json.loads(subprocess.run(["docker", "inspect", VAULT], capture_output=True, text=True).stdout)[0]
    config = info["HostConfig"]
    check("It has no published ports", not config["PortBindings"], str(config["PortBindings"]))
    check("It has a memory limit", config["Memory"] > 0)
    check("Its filesystem is read-only", config["ReadonlyRootfs"])
    check("It drops all capabilities", config["CapDrop"] == ["ALL"], str(config["CapDrop"]))
    check("It can't gain privileges", "no-new-privileges:true" in (config["SecurityOpt"] or []))
    check("It restarts by itself", config["RestartPolicy"]["Name"] == "unless-stopped")
    uid = in_vault("id", "-u").stdout.strip()
    check("It runs as non-root", uid == VAULT_UID, uid)
    check("Its version is pinned", re.fullmatch(r".+:\d+\.\d+\.\d+", info["Config"]["Image"]) is not None, info["Config"]["Image"])
    data = subprocess.run(["stat", "-c", "%u %a", str(VAULT_DIR)], capture_output=True, text=True).stdout.strip()
    check("Its data folder is private to it", data == f"{VAULT_UID} 700", data)
    mode = subprocess.run(["stat", "-c", "%u %a", str(VAULT_ENV)], capture_output=True, text=True).stdout.strip()
    check("Its secrets file is readable by root only", mode == "0 600", mode)
    try:
        socket.create_connection((socket.gethostbyname(vault_host(host)), 8080), 3).close()
        reachable = True
    except OSError:
        reachable = False
    check("Port 8080 can't be reached from the public address", not reachable)


def check_vault_https(host):
    section("Vaultwarden: HTTPS and cookies")
    first = Browser(vault_host(host)).get("/")
    check("Opening its address starts signing in, with no address to type", first.status == 302 and first.location == f"/#/sso?identifier={VAULT_IDENTIFIER}", f"{first.status} {first.location}")
    stay = first.cookie("sallskap_stay")
    check("...with a cookie that stops it repeating", "Secure" in stay and "HttpOnly" in stay and "domain=" not in stay.lower(), stay)
    check("...for as long as a session lasts", f"Max-Age={VAULT_SESSION_SECONDS}" in stay, stay)
    page = Browser(vault_host(host), cookies={"sallskap_stay": "1"}).get("/")
    check("Its page loads over HTTPS with a valid certificate", page.status == 200, str(page.status))
    plain = http.client.HTTPConnection(vault_host(host), 80, timeout=10)
    plain.request("GET", "/")
    answer = plain.getresponse()
    check("Plain HTTP redirects to HTTPS", answer.status in (301, 308) and answer.getheader("Location", "").startswith("https://"), str(answer.status))
    plain.close()
    check("HSTS is on", "max-age=" in (page.headers.get("Strict-Transport-Security") or ""))
    check("The site can't be framed by others", "frame-ancestors" in (page.headers.get("Content-Security-Policy") or "") or page.headers.get("X-Frame-Options") in ("SAMEORIGIN", "DENY"))
    check("No server software is advertised", page.headers.get("Server") is None, str(page.headers.get("Server")))
    check("No proxy software is advertised", page.headers.get("Via") is None, str(page.headers.get("Via")))

    person = VaultPerson(host)
    started = person.vault.get(person.start_path())
    binding = next((c for c in started.set_cookies if "binding" in c.lower()), "")
    check("Starting a sign-in sends someone to core", started.location.startswith(f"https://{host}/identity/o/authorize"), started.location)
    check("It ties the sign-in to this browser, with a Secure cookie", "Secure" in binding and "HttpOnly" in binding, binding)
    check("...that stays on this hostname", "domain=" not in binding.lower(), binding)


def check_vault_single_login(host, secrets_by_name):
    section("Vaultwarden: one login")
    visitor = VaultPerson(host)
    reached, _ = visitor.click_sign_in()
    check("A visitor is sent to core to log in", reached.startswith(f"{host}/accounts/login/"), reached)
    check("...and gets no code", visitor.code is None)

    person = VaultPerson(host)
    person.log_in_to_core(email_of("fake-vault-board"), secrets_by_name["fake-vault-board"])
    reached, _ = person.click_sign_in()
    check("Someone logged in to core is sent straight back, with nothing to enter", reached.endswith("/sso-connector.html") and bool(person.code), reached)
    status, data = person.redeem()
    check("The code signs them in", status == 200 and "access_token" in data, f"{status} {str(data)[:200]}")
    check("Their account comes from core", vault_account(email_of("fake-vault-board")) is not None)
    options = data.get("UserDecryptionOptions") or {}
    check("A first login asks them to choose a master password", options.get("HasMasterPassword") is False, str(options))
    status, _ = person.set_master_password(hint="the name of my dog")
    check("A password hint can't be stored with it", status != 200, str(status))
    status, _ = person.set_master_password()
    check("They can choose one", status == 200, str(status))
    check("They're in", person.in_vault())
    check("It's their own address", person.sync().get("profile", {}).get("email") == email_of("fake-vault-board"))
    known = person.api("GET", f"/api/organizations/{VAULT_IDENTIFIER}/policies/master-password")[0]
    unknown = person.api("GET", "/api/organizations/00000000-0000-0000-0000-000000000000/policies/master-password")[0]
    check("The identifier its address leads to is the one Vaultwarden uses for itself", known == 200 and unknown != 200, f"{known} {unknown}")

    first_account = vault_account(email_of("fake-vault-board"))
    again = VaultPerson(host)
    again.app.cookies = dict(person.app.cookies)
    status, data = again.sign_in()
    check("Signing in again works, from another device", status == 200, str(status))
    check("...and it's the same account", vault_account(email_of("fake-vault-board")) == first_account)
    check("...which asks for the master password, not a new one", (data.get("UserDecryptionOptions") or {}).get("HasMasterPassword") is True)
    check("...and hands over only the encrypted key", data.get("Key") == VAULT_KEY, str(data.get("Key"))[:40])

    wrong = VaultPerson(host)
    wrong.app.cookies = dict(person.app.cookies)
    wrong.click_sign_in()
    check("A code needs the secret from the browser that started it", wrong.redeem(verifier="not-the-right-one")[0] == 400)
    check("A made-up code is refused", VaultPerson(host).redeem(code="made-up", verifier="made-up")[0] == 400)

    # Someone starts a sign-in and tricks a logged-in person into finishing it.
    tricking = VaultPerson(host)
    to_core = urllib.parse.urlparse(tricking.vault.get(tricking.start_path()).location)
    victim = VaultPerson(host)
    victim.app.cookies = dict(person.app.cookies)
    back = urllib.parse.urlparse(victim.app.get(f"{to_core.path}?{to_core.query}").location)
    check("...which core does send back to Vaultwarden, for this next check to mean anything", back.path == "/identity/connect/oidc-signin", back.path)
    answer = victim.vault.get(f"{back.path}?{back.query}")
    check("A sign-in started in another browser is refused", answer.status >= 400 and "sso-connector" not in answer.location, f"{answer.status} {answer.location}")
    return person


def check_vault_groups(host, secrets_by_name):
    section("Vaultwarden: groups decide who gets in")
    for name in ("fake-vault-member", "fake-vault-dev", "fake-vault-treasurer"):
        person = VaultPerson(host)
        person.log_in_to_core(email_of(name), secrets_by_name.get(name))
        reached, answer = person.click_sign_in()
        check(f"{name} is told they have no access", answer.status == 403 and "have access" in answer.text, f"{reached} {answer.status}")
        check("...and gets no code", person.code is None)
        check("...and no account", vault_account(email_of(name)) is None)

    person = VaultPerson(host)
    person.log_in_to_core(email_of("fake-vault-board-no2fa"))
    reached, _ = person.click_sign_in()
    check("A Board member without 2FA is sent to set it up", "/accounts/2fa" in reached, reached)
    check("...and gets no code", person.code is None)
    check("...and no account", vault_account(email_of("fake-vault-board-no2fa")) is None)

    operator = vault_person(host, "fake-vault-operator", secrets_by_name)
    check("An Operator gets in", operator.in_vault())

    moving = VaultPerson(host)
    moving.log_in_to_core(email_of("fake-vault-moving"), secrets_by_name["fake-vault-moving"])
    reached, answer = moving.click_sign_in()
    check("Someone outside the groups is refused", answer.status == 403, f"{reached} {answer.status}")
    set_groups(email_of("fake-vault-moving"), ["board"])
    status, _ = moving.sign_in()
    check("Joining Board gets them in at the next sign-in", status == 200, str(status))
    moving.set_master_password()
    account = vault_account(email_of("fake-vault-moving"))
    set_groups(email_of("fake-vault-moving"), ["member"])
    moving.click_sign_in()
    check("Leaving Board stops the next sign-in", moving.code is None)
    set_groups(email_of("fake-vault-moving"), ["operator"])
    status, _ = moving.sign_in()
    check("Coming back in another allowed group works", status == 200, str(status))
    check("...with the same account", vault_account(email_of("fake-vault-moving")) == account)
    return operator


def check_closed(person, route, body):
    """Caddy has to answer for the address itself, as a refusal from Vaultwarden could just
    be down to what was sent. None of the ways of writing it that Vaultwarden would treat
    as the same address may get through either."""
    name = route.rsplit("/", 1)[1]
    variations = [
        f"{route}/",
        f"{route}//",
        f"{route}%2F",
        f"{route}?x=1",
        f"{route}/?x=1",
        f"/{route}",
        route.upper(),
        route.replace(f"/{name}", f"//{name}"),
        route.replace(name, f"%{ord(name[0]):02x}{name[1:]}"),
        route.replace(f"/{name}", f"/./{name}"),
        route.replace(f"/{name}", f"/x/../{name}"),
    ]
    itself = person.api("POST", route, body)[0]
    answers = [person.api("POST", variation, body) for variation in variations]
    leaked = [a for a in answers if a[0] == 200]
    check(f"{route} is closed, however it's written", itself == 404 and not leaked, f"{itself} {[a[0] for a in answers]} {str(leaked)[:200]}")


def check_vault_closed_doors(host, person):
    section("Vaultwarden: closed doors")
    host_name = vault_host(host)
    email = email_of("fake-vault-board")
    status, data = person.identity(
        grant_type="password", username=email, password=VAULT_HASH, scope="api offline_access",
        deviceType="9", deviceIdentifier=person.device, deviceName="chrome",
    )  # fmt: skip
    check("The right master password doesn't log anyone in without core", status != 200 and "access_token" not in data, f"{status} {str(data)[:200]}")

    admin = Browser(host_name).get("/admin")
    check("The admin page isn't on the internet", admin.status == 404, str(admin.status))
    check("...nor anything under it", Browser(host_name).get("/admin/users/overview").status == 404)

    organization = person.api(
        "POST",
        "/api/organizations",
        {"name": "Fake organisation", "billingEmail": email, "collectionName": "Fake collection", "key": VAULT_KEY, "planType": 0, "keys": {"encryptedPrivateKey": VAULT_KEY, "publicKey": "not-a-real-public-key"}},
    )[1]
    org = organization.get("id") if isinstance(organization, dict) else None
    body = {"masterPasswordHash": VAULT_HASH, "email": email, "name": "x"}
    for route in ("/api/accounts/api-key", "/api/accounts/rotate-api-key", f"/api/organizations/{org}/api-key", f"/api/organizations/{org}/rotate-api-key"):
        check_closed(person, route, body)

    # Without these, the checks above could pass because the routes had moved.
    inside = in_vault(
        "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-X", "POST", "-H", "Content-Type: application/json",
        "--data", json.dumps({"email": "nobody-at-all@example.org", "name": "x"}),
        "http://127.0.0.1:8080/identity/accounts/register/send-verification-email",
    ).stdout  # fmt: skip
    check("The registration route is there behind Caddy, as it answers on its own", inside == "400", inside)
    inside = in_vault(
        "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-X", "POST", "-H", "Content-Type: application/json",
        "-H", f"Authorization: Bearer {person.tokens['access_token']}", "--data", json.dumps({"masterPasswordHash": "wrong"}),
        "http://127.0.0.1:8080/api/accounts/api-key",
    ).stdout  # fmt: skip
    check("The API key route is there behind Caddy, as it answers on its own", inside == "400", inside)

    emergency = person.api("POST", "/api/emergency-access/invite", {"email": email_of("fake-vault-operator"), "type": 0, "waitTimeDays": 1})
    check("Emergency access is off", emergency[0] != 200, str(emergency)[:200])
    in_a_week = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 7 * 86400))
    send = person.api("POST", "/api/sends", {"type": 0, "name": VAULT_KEY, "key": VAULT_KEY, "deletionDate": in_a_week, "disabled": False, "text": {"text": VAULT_KEY, "hidden": False}})
    check("Public share links are off", send[0] != 200 and "only able to delete" in str(send), str(send)[:200])
    icons = [Browser(host_name).get(f"/icons/{site}/icon.png") for site in ("github.com", "no-such-site.example.org")]
    check("Its server doesn't fetch icons from saved sites", icons[0].text == icons[1].text and icons[0].status == 200, str([i.status for i in icons]))
    return org


def check_vault_sharing(host, owner, org, operator, secrets_by_name):
    """With no mail, the steps an invitation would have gone through by email."""
    section("Vaultwarden: sharing passwords without mail")
    check("Someone can create an organisation", org is not None)
    status, _ = owner.api("POST", f"/api/organizations/{org}/users/invite", {"emails": [email_of("fake-vault-operator")], "groups": [], "type": 2, "collections": [], "permissions": {}})
    check("They can add someone who has signed in", status == 200, str(status))
    members = owner.api("GET", f"/api/organizations/{org}/users")[1].get("data", [])
    member = next((m for m in members if m["email"] == email_of("fake-vault-operator")), {})
    check("With no email to answer, they're accepted right away", member.get("status") == 1, str(member.get("status")))
    status, _ = owner.api("POST", f"/api/organizations/{org}/users/{member.get('id')}/confirm", {"key": VAULT_KEY})
    check("The owner can confirm them, which gives them the organisation's key", status == 200, str(status))
    joined = [o["id"] for o in operator.sync().get("profile", {}).get("organizations", [])]
    check("They now see the organisation", org in joined, str(joined))
    status, _ = owner.api("POST", f"/api/organizations/{org}/users/invite", {"emails": [email_of("fake-vault-never")], "groups": [], "type": 2, "collections": [], "permissions": {}})
    check("Someone who has never signed in can't be added", status != 200, str(status))
    check("...and no empty account is made for them", vault_account(email_of("fake-vault-never")) is None)

    newcomer = VaultPerson(host)
    newcomer.log_in_to_core(email_of("fake-vault-newcomer"), secrets_by_name["fake-vault-newcomer"])
    newcomer.sign_in()  # and no master password yet
    status, _ = owner.api("POST", f"/api/organizations/{org}/users/invite", {"emails": [email_of("fake-vault-newcomer")], "groups": [], "type": 2, "collections": [], "permissions": {}})
    pending = vault_db("select count(*) from invitations where email = ?", email_of("fake-vault-newcomer"))[0][0]
    check("Adding someone with no master password yet leaves an invitation, as this next check needs", status == 200 and pending == 1, f"{status} {pending}")
    stranger = VaultPerson(host)
    claim = {"email": email_of("fake-vault-newcomer"), "name": "x"}
    check_closed(stranger, "/identity/accounts/register/send-verification-email", claim)
    check_closed(stranger, "/identity/accounts/register/finish", claim)
    hash_after = vault_db("select password_hash from users where email = ?", email_of("fake-vault-newcomer"))[0][0]
    check("A stranger hasn't claimed that account: it still has no master password", not hash_after)


def check_vault_addresses(host, secrets_by_name):
    section("Vaultwarden: accounts follow core's id, not the address")
    person = vault_person(host, "fake-vault-reused", secrets_by_name)
    first = vault_account(email_of("fake-vault-reused"))
    check("Someone signs in", person.in_vault() and first is not None)
    in_core(
        f"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from allauth.mfa.totp.internal.auth import TOTP
User = get_user_model()
User.objects.get(email={email_of("fake-vault-reused")!r}).delete()
user = User.objects.create_user({email_of("fake-vault-reused")!r}, {PASSWORD!r})
user.groups.set(Group.objects.filter(role__key__in=["operator"]))
TOTP.activate(user, {secrets_by_name["fake-vault-reused"]!r})
"""
    )
    time.sleep(31)  # each code works once
    newcomer = VaultPerson(host)
    newcomer.log_in_to_core(email_of("fake-vault-reused"), secrets_by_name["fake-vault-reused"])
    status, data = newcomer.sign_in()
    check("A new core account with an old address doesn't get the old vault", status != 200, f"{status} {str(data)[:200]}")

    # With no link to core, it's an account that only has a matching address. Nothing
    # should make one, so this is what the setting protects if something ever does.
    connection = sqlite3.connect(VAULT_DIR / "db.sqlite3")
    with connection:
        connection.execute("delete from sso_users where user_uuid = ?", (first,))
    connection.close()
    status, data = newcomer.sign_in()
    check("...nor an account with a matching address and no link to core", status != 200, f"{status} {str(data)[:200]}")


def check_vault_renamed(host, secrets_by_name):
    section("Vaultwarden: a new address in core")
    person = vault_person(host, "fake-vault-renamed", secrets_by_name)
    account = vault_account(email_of("fake-vault-renamed"))
    in_core(f"from django.contrib.auth import get_user_model as g; g().objects.filter(email={email_of('fake-vault-renamed')!r}).update(email={email_of('fake-vault-renamed-new')!r})")
    status, _ = person.sign_in()
    check("They can still sign in", status == 200, str(status))
    check("...to the same account, which keeps the old address", vault_account(email_of("fake-vault-renamed")) == account and vault_account(email_of("fake-vault-renamed-new")) is None)


def check_vault_sessions(host, secrets_by_name):
    section("Vaultwarden: how long a session lasts")
    email = email_of("fake-vault-session")
    person = vault_person(host, "fake-vault-session", secrets_by_name)
    lasts = int(person.tokens.get("expires_in", 0))
    check("A session lasts 12 hours", abs(lasts - VAULT_SESSION_SECONDS) < 60, str(lasts))

    def expires():
        return json.loads(base64.urlsafe_b64decode(person.tokens["access_token"].split(".")[1] + "=="))["exp"]

    ending = expires()
    status, _ = person.refresh()
    check("It can be refreshed", status == 200, str(status))
    check("...which doesn't extend it", expires() == ending, f"{expires()} {ending}")

    # Core refuses them too, which is what a browser meets if the worker is ever down. The
    # worker would otherwise hide this, and end the session for good before it's reached.
    worker("stop")
    try:
        set_groups(email, ["member"], wait=False)
        status, _ = person.refresh()
        check("With the worker stopped, leaving the groups ends it at the next refresh, which is every page load", status != 200, str(status))
        check("...but a token used by hand keeps working, which is what the worker is for", person.api("GET", "/api/sync")[0] == 200)
        set_groups(email, ["board"], wait=False)
        check("...and only core was stopping it", person.refresh()[0] == 200)

        in_core(f"from django.contrib.auth import get_user_model as g; g().objects.filter(email={email!r}).update(is_active=False)")
        status, _ = person.refresh()
        check("Turning off the account ends it at the next refresh", status != 200, str(status))
        in_core(f"from django.contrib.auth import get_user_model as g; g().objects.filter(email={email!r}).update(is_active=True)")
    finally:
        worker("start")


def check_vault_limits(host):
    section("Vaultwarden: login limits count real addresses")
    faking = VaultPerson(host)
    statuses = []
    for number in range(12):
        faking.vault.headers = {"X-Forwarded-For": f"203.0.113.{number}", "X-Real-IP": f"203.0.113.{number}"}
        statuses.append(faking.redeem(code="made-up", verifier="made-up")[0])
    check("Too many tries are stopped, even with a faked address", 429 in statuses, str(statuses))
    clear_vault_limits()
    faking.identity(grant_type="refresh_token", refresh_token="not-a-token")
    answer = subprocess.run(["docker", "logs", "--tail", "20", VAULT], capture_output=True, text=True)
    found = re.findall(r"refresh_token from (\S+?):", answer.stdout + answer.stderr)
    public = socket.gethostbyname(vault_host(host))
    check("It logs the real address, not Caddy's or a faked one", found and found[-1] == public, str(found[-1:]))


def vault_fingerprint():
    """What must not change when the setup runs again or the server restarts."""
    return {
        "secrets": hashlib.sha256(VAULT_ENV.read_bytes()).hexdigest(),
        "signing key": hashlib.sha256((VAULT_DIR / "rsa_key.pem").read_bytes()).hexdigest(),
        "tool in core": kit("tool", "list").stdout,
    }


def check_vault_changing_rules(host, secrets_by_name):
    """Changing its groups or session length means registering it again, as its README says."""
    section("Vaultwarden: registering it again")
    person = vault_person(host, "fake-vault-rules", secrets_by_name)
    before = hashlib.sha256(VAULT_ENV.read_bytes()).hexdigest()
    kit("tool", "remove", "vaultwarden")
    done = kit("enable", "vault")
    check("It works", done.returncode == 0, done.stderr[-300:])
    check("...with a new secret on both sides", hashlib.sha256(VAULT_ENV.read_bytes()).hexdigest() != before)
    check("An open session ends at its next refresh", person.refresh()[0] != 200)
    status, _ = person.sign_in()
    check("Signing in again works", status == 200, str(status))
    check("...and they're in", person.in_vault())


def check_vault_off_and_on(host, secrets_by_name):
    section("Vaultwarden: switching it off and on")
    person = vault_person(host, "fake-vault-offon", secrets_by_name)
    account, secrets_file = vault_account(email_of("fake-vault-offon")), VAULT_ENV.read_bytes()
    done = kit("disable", "vault")
    check("`kit disable` works", done.returncode == 0, done.stderr[-300:])
    running = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True).stdout
    check("It's stopped", VAULT not in running, running)
    check("Its data stays", (VAULT_DIR / "db.sqlite3").exists() and VAULT_ENV.exists())
    try:
        Browser(vault_host(host), cookies={"sallskap_stay": "1"}).get("/")
        reachable = True
    except (ssl.SSLError, OSError):
        reachable = False
    check("Its address no longer answers", not reachable)
    done = kit("enable", "vault")
    check("`kit enable` brings it back", done.returncode == 0, done.stderr[-300:])
    check("...with the same secret", VAULT_ENV.read_bytes() == secrets_file)
    wait_for_vault()
    check("...and the same account", vault_account(email_of("fake-vault-offon")) == account)
    check("...and the session still works", person.in_vault())


def check_vault_setup_repeats():
    section("Vaultwarden: running the setup again")
    before = vault_fingerprint()
    done = kit("enable", "vault")
    check("It works", done.returncode == 0, done.stderr[-300:])
    check("...and changes nothing", vault_fingerprint() == before)
    check("Core lists it, for its groups only", "(board, operator, 12 hour sessions)" in before["tool in core"], before["tool in core"])


def check_vault(host, secrets_by_name):
    clear_limits()
    clear_vault_limits()
    check_vault_exposure(host)
    check_vault_https(host)
    owner = check_vault_single_login(host, secrets_by_name)
    clear_vault_limits()
    operator = check_vault_groups(host, secrets_by_name)
    clear_vault_limits()
    org = check_vault_closed_doors(host, owner)
    clear_vault_limits()
    check_vault_sharing(host, owner, org, operator, secrets_by_name)
    clear_vault_limits()
    check_vault_addresses(host, secrets_by_name)
    clear_vault_limits()
    check_vault_renamed(host, secrets_by_name)
    check_vault_sessions(host, secrets_by_name)
    clear_vault_limits()
    check_vault_limits(host)
    clear_vault_limits()
    check_vault_changing_rules(host, secrets_by_name)
    clear_vault_limits()
    check_vault_off_and_on(host, secrets_by_name)
    clear_vault_limits()
    check_vault_setup_repeats()
    clear_limits()

    keeper = vault_person(host, "fake-vault-keeper", secrets_by_name)
    return {
        "core_cookies": keeper.app.cookies,
        "tokens": keeper.tokens,
        "org": org,
        "fingerprint": vault_fingerprint(),
    }


def verify_vault(host, state):
    section("Vaultwarden: did logins and data survive?")
    clear_vault_limits()
    keeper = VaultPerson(host)
    keeper.tokens = state["tokens"]
    check("A session is still signed in", keeper.in_vault())
    person = VaultPerson(host)
    person.app.cookies = dict(state["core_cookies"])
    status, _ = person.sign_in()
    check("A new sign-in still works", status == 200, str(status))
    check("...into the same account, with its master password", (person.tokens.get("UserDecryptionOptions") or {}).get("HasMasterPassword") is True)
    orgs = vault_db("select count(*) from organizations where uuid = ?", state["org"])[0][0]
    check("Its data is still there", orgs == 1, str(orgs))
    now = vault_fingerprint()
    for label, value in now.items():
        check(f"Its {label} didn't change", value == state["fingerprint"][label], f"{value} {state['fingerprint'][label]}")
    check("It's healthy", vault_health() == "healthy", str(vault_health()))
    check_vault_exposure(host)


def burst_vault(host, state):
    section("Many sign-ins to Vaultwarden at once")
    clear_vault_limits()
    info = json.loads(subprocess.run(["docker", "inspect", VAULT], capture_output=True, text=True).stdout)[0]
    cgroup = Path(f"/sys/fs/cgroup/system.slice/docker-{info['Id']}.scope")
    oom_before = _oom_kills(cgroup)
    results = []

    def one_sign_in():
        person = VaultPerson(host)
        person.app.cookies = dict(state["core_cookies"])
        results.append(person.sign_in()[0] == 200 and person.in_vault())

    # Its limit lets 10 through at once from one address, so more would only test that.
    threads = [threading.Thread(target=one_sign_in) for _ in range(10)]
    started = time.time()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    peak_file = cgroup / "memory.peak"
    peak_mb = int(peak_file.read_text()) / 1024**2 if peak_file.exists() else None
    print(f"  info  10 sign-ins took {time.time() - started:.1f}s. Peak memory {peak_mb and round(peak_mb)} MB of a {info['HostConfig']['Memory'] / 1024**2:.0f} MB limit.")
    check("All 10 sign-ins succeeded", all(results) and len(results) == 10, f"{sum(results)} of {len(results)}")
    check("Nothing was killed for using too much memory", _oom_kills(cgroup) == oom_before)


def cleanup_vault():
    """Through its database with it stopped, as nothing else can remove someone else's
    account: the admin page is off. Only what belongs to fake accounts goes."""
    subprocess.run(["docker", "stop", VAULT], capture_output=True)
    connection = sqlite3.connect(VAULT_DIR / "db.sqlite3")
    try:
        people = [row[0] for row in connection.execute("select uuid from users where email like 'fake-%@example.org'")]
        marks = ",".join("?" * len(people))
        orgs = [row[0] for row in connection.execute(f"select distinct org_uuid from users_organizations where atype = 0 and user_uuid in ({marks})", people)]
        org_marks = ",".join("?" * len(orgs))
        tables = [row[0] for row in connection.execute("select name from sqlite_master where type = 'table' and name not like 'sqlite_%' and name != '__diesel_schema_migrations'")]
        for table in tables:
            columns = {row[1] for row in connection.execute(f"pragma table_info({table})")}
            if "user_uuid" in columns:
                connection.execute(f"delete from {table} where user_uuid in ({marks})", people)
            for column in ("org_uuid", "organization_uuid"):
                if column in columns:
                    connection.execute(f"delete from {table} where {column} in ({org_marks})", orgs)
        connection.execute(f"delete from organizations where uuid in ({org_marks})", orgs)
        connection.execute(f"delete from users where uuid in ({marks})", people)
        connection.execute("delete from invitations where email like 'fake-%@example.org'")
        connection.commit()
        left = connection.execute("select count(*) from users where email like 'fake-%@example.org'").fetchone()[0]
    finally:
        connection.close()
    subprocess.run(["docker", "start", VAULT], capture_output=True)
    print(f"Fake accounts left in Vaultwarden: {left}, removed {len(people)} and {len(orgs)} organisations")


# ---- Access: the table in step 5 of the plan ------------------------------------
#
# Every check here has a control just before it, so nothing passes because a thing
# never worked. Fake accounts only: the real ones are compared separately.


def wait_for(condition, seconds=20):
    """How long it took, or None if it never happened."""
    started = time.time()
    while time.time() - started < seconds:
        if condition():
            return round(time.time() - started, 1)
        time.sleep(0.25)
    return None


def core_db(query, *values):
    """What only its database can say: it's read here, never changed."""
    connection = sqlite3.connect("file:/srv/core/db.sqlite3?mode=ro", uri=True)
    try:
        return connection.execute(query, values).fetchall()
    finally:
        connection.close()


def wait_for_sync(seconds=30):
    """Until the worker has told every tool about every change."""
    wait_for(lambda: core_db("select count(*) from accounts_accesssync where pending = 1")[0][0] == 0, seconds)


def sync_env():
    return dict(line.split("=", 1) for line in SYNC_ENV.read_text().splitlines() if "=" in line)


def worker(action):
    """Stop or start the worker, for what it would otherwise hide."""
    compose(action, "sync")
    if action == "start":
        wait_for(lambda: docker_inspect("sync")["State"].get("Health", {}).get("Status") == "healthy", 60)


def set_active(email, active):
    in_core(
        f"""
from django.contrib.auth import get_user_model
user = get_user_model().objects.get(email={email!r})
user.is_active = {active!r}
user.save()
"""
    )


def admin_page_set_active(operator, email, active):
    """The way an Operator does it: the real form on the admin page."""
    pk = core_db("select id from accounts_user where email = ?", email)[0][0]
    path = f"/admin/accounts/user/{pk}/change/"
    page = operator.get(path)
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text)
    groups = [("groups", str(row[0])) for row in core_db("select group_id from accounts_user_groups where user_id = ?", pk)]
    form = [("csrfmiddlewaretoken", token.group(1) if token else ""), ("email", email), ("name", ""), ("_save", "Save"), *groups]
    if active:
        form.append(("is_active", "on"))
    return operator.send("POST", path, form).status


def git_person(host, name, secrets_by_name):
    person = Person(host)
    person.log_in_to_core(email_of(name), secrets_by_name.get(name))
    person.sign_in_to_git()
    return person, person.make_token()


def git_account(host, email):
    """What Forgejo holds for someone, asked the way the worker asks."""
    sub = core_db("select id from accounts_user where email = ?", email)[0][0]
    found = forgejo_api(host, sync_env()["FORGEJO_ADMIN_TOKEN"], "GET", f"/admin/users?login_name={sub}")[1]
    return found[0] if found else None


def collaborators(host, repo):
    token = sync_env()["FORGEJO_ADMIN_TOKEN"]
    return [user["login"] for user in forgejo_api(host, token, "GET", f"/repos/{org_of(host)}/{repo}/collaborators")[1]]


def make_repo(host, token, repo):
    """A private repo, with something in it to look for."""
    org = org_of(host)
    status, _ = forgejo_api(host, token, "POST", f"/orgs/{org}/repos", {"name": repo, "private": True, "auto_init": True, "default_branch": "main"})
    content = base64.b64encode(GIT_SECRET.encode()).decode()
    added, _ = forgejo_api(host, token, "POST", f"/repos/{org}/{repo}/contents/secret.txt", {"content": content, "branch": "main", "message": "x"})
    return status == 201 and added == 201


def can_push(host, username, token, repo, branch):
    with tempfile.TemporaryDirectory() as tmp:
        cloned = run_git("clone", "-q", repo_url(host, username, token, repo), tmp)
        if cloned.returncode:
            return False
        for args in (("checkout", "-q", "-b", branch), ("-c", "user.name=Fake", "-c", "user.email=fake@example.org", "commit", "-q", "--allow-empty", "-m", "x")):
            run_git(*args, cwd=tmp)
        return run_git("push", "-q", "origin", branch, cwd=tmp).returncode == 0


def git_refs(host, username, token, repo):
    """What Git asks for first. Forgejo answers someone it has blocked with a 200 page."""
    connection = http.client.HTTPSConnection(git_host(host), context=ssl.create_default_context(), timeout=30)
    authorization = base64.b64encode(f"{username}:{token}".encode()).decode()
    connection.request("GET", f"/{org_of(host)}/{repo}.git/info/refs?service=git-upload-pack", headers={"Authorization": f"Basic {authorization}"})
    text = connection.getresponse().read().decode(errors="replace")
    connection.close()
    return text


def can_clone(host, username, token, repo):
    with tempfile.TemporaryDirectory() as tmp:
        cloned = run_git("clone", "-q", repo_url(host, username, token, repo), str(Path(tmp, "x")))
        return cloned.returncode == 0 and Path(tmp, "x", "secret.txt").exists()


def check_sync_exposure(host):
    section("The worker: what it can reach, and what holds its tokens")
    info = docker_inspect("sync")
    config = info["HostConfig"]
    check("It publishes no ports", not config["PortBindings"], str(config["PortBindings"]))
    check("It has a memory limit", config["Memory"] > 0)
    check("Its filesystem is read-only", config["ReadonlyRootfs"])
    check("It drops all capabilities", config["CapDrop"] == ["ALL"], str(config["CapDrop"]))
    check("It can't gain privileges", "no-new-privileges:true" in config["SecurityOpt"])
    check("It restarts by itself", config["RestartPolicy"]["Name"] == "unless-stopped")
    check("It's healthy", info["State"].get("Health", {}).get("Status") == "healthy", str(info["State"].get("Health")))
    uid = compose("exec", "-T", "sync", "id", "-u").stdout.strip()
    check("It runs as non-root", uid == "10001", uid)
    modules = next((mount for mount in info["Mounts"] if mount["Destination"] == "/modules"), {})
    check("The modules' code is read-only to it", modules.get("RW") is False, str(modules))

    env = sync_env()
    mode = subprocess.run(["stat", "-c", "%u %a", str(SYNC_ENV)], capture_output=True, text=True).stdout.strip()
    check("Its settings file is readable by root only", mode == "0 600", mode)
    has = lambda service, value: value in " ".join(docker_inspect(service)["Config"]["Env"])
    for name in ("FORGEJO_ADMIN_TOKEN", "VAULT_ADMIN_TOKEN"):
        check(f"It has {name}", has("sync", env[name]))
        check("...and core's web process doesn't", not has("core", env[name]) and env[name] not in CORE_ENV.read_text())
    core_settings = dict(line.split("=", 1) for line in CORE_ENV.read_text().splitlines() if "=" in line)
    check("It doesn't have core's signing key", not has("sync", core_settings["OIDC_SIGNING_KEY"]))
    check("...or its secret key", not has("sync", core_settings["DJANGO_SECRET_KEY"]))
    in_vault_env = dict(line.split("=", 1) for line in in_vault("env").stdout.splitlines() if "=" in line)
    check("Vaultwarden only gets a hash of its admin token", in_vault_env.get("ADMIN_TOKEN", "").startswith("$argon2id$"), in_vault_env.get("ADMIN_TOKEN", "")[:12])
    check("...and not the token", env["VAULT_ADMIN_TOKEN"] not in in_vault("env").stdout and env["VAULT_ADMIN_TOKEN"] not in VAULT_ENV.read_text())
    wrong = compose("exec", "-T", "core", "python", "-c", "import urllib.request, urllib.error\ntry:\n    urllib.request.urlopen(urllib.request.Request('http://vaultwarden:8080/admin', data=b'token=wrong', headers={'Content-Type': 'application/x-www-form-urlencoded'}))\nexcept urllib.error.HTTPError as e:\n    print(e.code)")
    check("A wrong admin token is refused, even from inside", wrong.stdout.strip() == "401", wrong.stdout + wrong.stderr[-200:])
    check("With its token on, the admin page still isn't on the internet", Browser(vault_host(host)).get("/admin").status == 404)
    check("...nor anything under it", Browser(vault_host(host)).get("/admin/users/overview").status == 404)
    check("Vaultwarden hasn't warned about a plain token", "plain text" not in subprocess.run(["docker", "logs", VAULT], capture_output=True, text=True).stdout)


def check_git_own_2fa(host, secrets_by_name):
    """Someone with Forgejo's own 2FA turned on isn't asked again, as core has already asked."""
    section("Forgejo: its own 2FA doesn't add a second login")
    person, _ = git_person(host, "fake-sync-2fa", secrets_by_name)
    page = person.git.get("/user/settings/security/two_factor/enroll")
    found = re.search(r"\b[A-Z2-7]{32,}\b", page.text)
    check("Someone can turn on Forgejo's own 2FA", bool(found), str(page.status))
    if found:
        answer = person.git.send("POST", "/user/settings/security/two_factor/enroll", [("passcode", totp(found.group(0)))])
        check("...which it takes", answer.status in (200, 303), str(answer.status))
        enrolled = core_db_git("select count(*) from two_factor where uid = (select id from user where lower_name = 'fake-sync-2fa')")
        check("...and it's really on", enrolled == 1, str(enrolled))
    person.new_git_session()
    reached, _ = person.sign_in_to_git()
    check("Signing in again asks for nothing more", person.in_git() and "two_factor" not in reached, reached)


def core_db_git(query):
    connection = sqlite3.connect("file:/srv/git/data/gitea.db?mode=ro", uri=True)
    try:
        return connection.execute(query).fetchone()[0]
    finally:
        connection.close()


def check_git_groups_now(host, operator, secrets_by_name):
    section("Forgejo: joining a group")
    joiner, token = git_person(host, "fake-sync-joiner", secrets_by_name)
    check("Control: a Member is on no team", team_names(host, token) == [], str(team_names(host, token)))
    email = email_of("fake-sync-joiner")
    set_groups(email, ["board"], wait=False)
    took = wait_for(lambda: team_names(host, token) == ["board"])
    check("Joining Board puts them on the team, with no new sign-in", took is not None)
    print(f"  info  It took {took} seconds.")

    section("Forgejo: a group that requires 2FA waits for it")
    email = email_of("fake-sync-no2fa")
    _, token = git_person(host, "fake-sync-no2fa", secrets_by_name)
    check("Control: a Developer is on their team and isn't an admin", team_names(host, token) == ["developers"] and forgejo_api(host, token, "GET", "/user")[1].get("is_admin") is False)
    set_groups(email, ["developer", "operator"])
    check("Joining Operator with no 2FA doesn't make them a Forgejo admin", forgejo_api(host, token, "GET", "/user")[1].get("is_admin") is False)
    check("...or an owner", team_names(host, token) == ["developers"], str(team_names(host, token)))
    in_core(
        f"""
from django.contrib.auth import get_user_model
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
TOTP.activate(get_user_model().objects.get(email={email!r}), generate_totp_secret())
"""
    )
    took = wait_for(lambda: forgejo_api(host, token, "GET", "/user")[1].get("is_admin") is True)
    check("Setting up 2FA does both, with no new sign-in", took is not None and "Owners" in team_names(host, token), str(team_names(host, token)))

    section("Forgejo: leaving a group, including repos they created")
    org = org_of(host)
    colleague, colleague_token = git_person(host, "fake-sync-colleague", secrets_by_name)
    creator, token = git_person(host, "fake-sync-creator", secrets_by_name)
    repo = "fake-sync-repo"
    check("A Developer creates a repo", make_repo(host, token, repo))
    check("Control: Forgejo made them a collaborator", "fake-sync-creator" in collaborators(host, repo), str(collaborators(host, repo)))
    check("Control: they can push to it", can_push(host, "fake-sync-creator", token, repo, "before"))
    check("Control: so can a colleague", can_push(host, "fake-sync-colleague", colleague_token, repo, "colleague-before"))

    set_groups(email_of("fake-sync-creator"), ["member"], wait=False)
    took = wait_for(lambda: forgejo_api(host, token, "GET", f"/repos/{org}/{repo}")[0] == 404)
    check("Leaving the group takes the repo they created away, with no new sign-in", took is not None)
    print(f"  info  It took {took} seconds.")
    wait_for_sync()
    check("They're on no team", team_names(host, token) == [], str(team_names(host, token)))
    check("They aren't a collaborator on it", "fake-sync-creator" not in collaborators(host, repo), str(collaborators(host, repo)))
    check("They can't push", not can_push(host, "fake-sync-creator", token, repo, "after"))
    check("They can't clone", not can_clone(host, "fake-sync-creator", token, repo))
    check("Their account still works, for public repos", forgejo_api(host, token, "GET", "/user")[0] == 200)
    check("A colleague on the team isn't affected", can_push(host, "fake-sync-colleague", colleague_token, repo, "colleague-after"))

    mover, token = git_person(host, "fake-sync-moved", secrets_by_name)
    moved_repo = "fake-sync-moved-repo"
    check("A Developer who's about to join Board creates a repo", make_repo(host, token, moved_repo))
    set_groups(email_of("fake-sync-moved"), ["board"])
    check("On joining Board instead, they can read it", can_clone(host, "fake-sync-moved", token, moved_repo))
    check("...but no longer write to it", not can_push(host, "fake-sync-moved", token, moved_repo, "after"))
    check("...as they aren't a collaborator", "fake-sync-moved" not in collaborators(host, moved_repo), str(collaborators(host, moved_repo)))

    admin, token = git_person(host, "fake-sync-admin", secrets_by_name)
    check("Control: an Operator is a Forgejo admin", forgejo_api(host, token, "GET", "/user")[1].get("is_admin") is True)
    set_groups(email_of("fake-sync-admin"), ["member"])
    check("Leaving Operator ends that, with no new sign-in", forgejo_api(host, token, "GET", "/user")[1].get("is_admin") is False)
    check("...and takes them off the owners", team_names(host, token) == [], str(team_names(host, token)))


def check_git_offboarding(host, operator, secrets_by_name):
    section("Forgejo: offboarding")
    org = org_of(host)
    email = email_of("fake-sync-leaver")
    leaver, token = git_person(host, "fake-sync-leaver", secrets_by_name)
    repo = "fake-sync-leaver-repo"
    raw = f"/{org}/{repo}/raw/branch/main/secret.txt"
    check("They create a repo", make_repo(host, token, repo))
    check("Control: their token reaches the API", forgejo_api(host, token, "GET", "/user")[0] == 200)
    check("Control: their token works with Git", can_clone(host, "fake-sync-leaver", token, repo))
    check("Control: their open session shows the settings page", leaver.in_git())
    check("Control: their open session shows the file", GIT_SECRET in leaver.git.get(raw).text)
    check("Control: their open session shows the repo's file list", "secret.txt" in leaver.git.get(f"/{org}/{repo}").text)
    check("Control: Git hands out the repo's refs", "refs/heads" in git_refs(host, "fake-sync-leaver", token, repo))
    check("Control: their core session works", leaver.app.logged_in())

    status = admin_page_set_active(operator, email, False)
    check("An Operator turns off their account on the admin page", status == 302, str(status))
    took = wait_for(lambda: forgejo_api(host, token, "GET", "/user")[0] != 200)
    check("Their token stops working on the API, with nothing more to do", took is not None)
    print(f"  info  It took {took} seconds.")
    wait_for_sync()
    check("...and with Git", not can_clone(host, "fake-sync-leaver", token, repo))
    check("...which hands out nothing about the repo, whatever the status", "refs/heads" not in git_refs(host, "fake-sync-leaver", token, repo))
    # Forgejo answers a blocked session with a 200 page, so status alone proves nothing.
    check("Their open session no longer shows the settings page", not leaver.in_git())
    check("...or the file", GIT_SECRET not in leaver.git.get(raw).text)
    check("...or the repo's file list", "secret.txt" not in leaver.git.get(f"/{org}/{repo}").text)
    before = forgejo_tokens()
    leaver.git.send("POST", "/user/settings/applications/tokens/new", [("name", "while-blocked"), ("resource", "all"), ("scope", "write:repository")])
    check("...and can't make a new token", forgejo_tokens() == before, f"{before} {forgejo_tokens()}")
    check("Core has ended their session too", not leaver.app.logged_in())
    fresh = Person(host)
    fresh.app.log_in(email)
    check("They can't log in to core", not fresh.app.logged_in())
    account = git_account(host, email)
    check("Forgejo has them blocked", account["prohibit_login"] is True, str(account["prohibit_login"]))
    developers = next(t for t in forgejo_api(host, sync_env()["FORGEJO_ADMIN_TOKEN"], "GET", f"/orgs/{org}/teams")[1] if t["name"] == "developers")
    check("They're off the team", forgejo_api(host, sync_env()["FORGEJO_ADMIN_TOKEN"], "GET", f"/teams/{developers['id']}/members/fake-sync-leaver")[0] == 404)
    check("They aren't a collaborator on what they created", "fake-sync-leaver" not in collaborators(host, repo), str(collaborators(host, repo)))

    status = admin_page_set_active(operator, email, True)
    check("Turning the account back on works", status == 302, str(status))
    took = wait_for(lambda: forgejo_api(host, token, "GET", "/user")[0] == 200)
    check("...and so does their token", took is not None)
    wait_for_sync()
    check("...and they're back on the team", team_names(host, token) == ["developers"], str(team_names(host, token)))
    check("...but not back as a collaborator, as the team is what gives them access", "fake-sync-leaver" not in collaborators(host, repo), str(collaborators(host, repo)))
    fresh = Person(host)
    fresh.log_in_to_core(email)
    fresh.sign_in_to_git()
    check("...and they can sign in again", fresh.in_git())

    section("Forgejo: other ways of leaving")
    _, token = git_person(host, "fake-sync-deleted", secrets_by_name)
    deleted = git_account(host, email_of("fake-sync-deleted"))
    in_core(f"from django.contrib.auth import get_user_model as g; g().objects.get(email={email_of('fake-sync-deleted')!r}).delete()")
    check("Deleting the account in core stops their token", wait_for(lambda: forgejo_api(host, token, "GET", "/user")[0] != 200) is not None)
    wait_for_sync()
    forgejo_api(host, sync_env()["FORGEJO_ADMIN_TOKEN"], "PATCH", "/admin/users/fake-sync-deleted", {"login_name": deleted["login_name"], "source_id": deleted["source_id"], "prohibit_login": False})
    check("Control: restoring Forgejo from an older backup would bring their account back", forgejo_api(host, token, "GET", "/user")[0] == 200)
    compose("restart", "sync")
    check("...until the worker's regular look at everyone, though they're gone from core", wait_for(lambda: forgejo_api(host, token, "GET", "/user")[0] != 200, 60) is not None)

    _, token = git_person(host, "fake-sync-bypass", secrets_by_name)
    in_core(f"from django.contrib.auth import get_user_model as g; g().objects.filter(email={email_of('fake-sync-bypass')!r}).update(is_active=False)")
    time.sleep(5)
    check("Control: a change that gets past the signals isn't noticed yet", forgejo_api(host, token, "GET", "/user")[0] == 200)
    compose("restart", "sync")
    check("...until the worker's regular look at everyone", wait_for(lambda: forgejo_api(host, token, "GET", "/user")[0] != 200, 60) is not None)

    section("Forgejo: when it's down at the time")
    _, token = git_person(host, "fake-sync-outage", secrets_by_name)
    email = email_of("fake-sync-outage")
    subprocess.run(["docker", "stop", FORGEJO], capture_output=True)
    try:
        status = admin_page_set_active(operator, email, False)
        check("Turning off an account still works, and the page doesn't wait for it", status == 302, str(status))
        pk = core_db("select id from accounts_user where email = ?", email)[0][0]
        wait_for(lambda: "error" in core_db("select tools from accounts_accesssync where sub = ?", str(pk))[0][0])
        page = operator.get(f"/admin/accounts/user/{pk}/change/").text
        check("Their page on the admin site says it hasn't reached Forgejo", "forgejo:" in page)
    finally:
        subprocess.run(["docker", "start", FORGEJO], capture_output=True)
    wait_for(lambda: json.loads(subprocess.run(["docker", "inspect", FORGEJO], capture_output=True, text=True).stdout)[0]["State"].get("Health", {}).get("Status") == "healthy", 90)
    took = wait_for(lambda: forgejo_api(host, token, "GET", "/user")[0] == 403, 330)
    check("When it's back, the worker tries again and their token stops", took is not None)
    print(f"  info  It took {took} seconds after Forgejo was healthy.")
    wait_for_sync()  # blocking comes first, so their token stops before it has finished
    page = operator.get(f"/admin/accounts/user/{pk}/change/").text
    check("Their page then says it's done", "forgejo:" not in page and "Sent to" in page)


def check_worker_down(host, operator, secrets_by_name):
    section("The worker: when it's down")
    _, token = git_person(host, "fake-sync-nowork", secrets_by_name)
    email = email_of("fake-sync-nowork")
    worker("stop")
    try:
        status = admin_page_set_active(operator, email, False)
        check("Turning off an account still works", status == 302, str(status))
        time.sleep(3)
        check("Control: their token isn't stopped yet", forgejo_api(host, token, "GET", "/user")[0] == 200)
        time.sleep(61)
        index = operator.get("/admin/").text
        check("After a minute, the admin page says so", f"{email} hasn't reached every tool" in html.unescape(index))
    finally:
        worker("start")
    check("When it's back, it catches up", wait_for(lambda: forgejo_api(host, token, "GET", "/user")[0] == 403, 60) is not None)
    wait_for_sync()
    check("...and the admin page stops saying so", f"{email} hasn" not in html.unescape(operator.get("/admin/").text))

    section("The worker: restarting it a few times")
    clear_vault_limits()
    for _ in range(6):  # it logs in to Vaultwarden's admin page each time
        compose("restart", "sync")
        time.sleep(4)
        wait_for_sync()
    limited = core_db("select count(*) from accounts_accesssync where tools like '%answered 429%'")[0][0]
    check("Doesn't get it locked out of Vaultwarden, which would hold up someone's removal", limited == 0, f"{limited} people waiting")


def vault_member(owner, org, person_email, kind=2, collection=None):
    """Add someone to an organisation, as the steps with no mail go."""
    collections = [{"id": collection, "readOnly": False, "hidePasswords": False, "manage": False}] if collection else []
    owner.api("POST", f"/api/organizations/{org}/users/invite", {"emails": [person_email], "groups": [], "type": kind, "collections": collections, "permissions": {}})
    members = owner.api("GET", f"/api/organizations/{org}/users")[1].get("data", [])
    member = next((m for m in members if m["email"] == person_email), {})
    owner.api("POST", f"/api/organizations/{org}/users/{member.get('id')}/confirm", {"key": VAULT_KEY})
    return member.get("id")


def vault_org(person, email, name):
    answer = person.api(
        "POST",
        "/api/organizations",
        {"name": name, "billingEmail": email, "collectionName": VAULT_KEY, "key": VAULT_KEY, "planType": 0, "keys": {"encryptedPrivateKey": VAULT_KEY, "publicKey": "not-a-real-public-key"}},
    )[1]
    return answer["id"]


def vault_enabled_flag(email):
    found = vault_db("select enabled from users where email = ?", email)
    return bool(found[0][0]) if found else None


def follow_ups(email):
    return core_db("select text, done_at, done_by_id from accounts_followup where about = ? order by id", email)


def check_vault_offboarding(host, operator, secrets_by_name):
    section("Vaultwarden: leaving a group")
    clear_vault_limits()
    owner = vault_person(host, "fake-vsync-owner", secrets_by_name)
    owner_email = email_of("fake-vsync-owner")
    org_name = "Fake shared passwords"
    org = vault_org(owner, owner_email, org_name)
    check("An owner has an organisation", bool(org))

    email = email_of("fake-vsync-removed")
    removed = vault_person(host, "fake-vsync-removed", secrets_by_name)
    vault_member(owner, org, email)
    account = vault_account(email)
    joined = [entry["id"] for entry in removed.sync().get("profile", {}).get("organizations", [])]
    check("Control: they're in the organisation", org in joined, str(joined))
    check("Control: their token reaches the API", removed.in_vault())
    check("Control: it can be refreshed", removed.refresh()[0] == 200)
    set_groups(email, ["member"], wait=False)
    took = wait_for(lambda: not removed.in_vault())
    check("Leaving the groups ends their token, even used by hand", took is not None)
    print(f"  info  It took {took} seconds.")
    wait_for_sync()
    check("...and refreshing it", removed.refresh()[0] != 200)
    check("Vaultwarden has them turned off", vault_enabled_flag(email) is False)
    reached, answer = removed.click_sign_in()
    check("Signing in again shows them they have no access", answer.status == 403 and removed.code is None, f"{reached} {answer.status}")
    check("A follow-up asks for the shared passwords to change", len(follow_ups(email)) == 1 and org_name in follow_ups(email)[0][0], str(follow_ups(email)))
    set_groups(email, ["board"])
    check("Coming back turns them on again", vault_enabled_flag(email) is True)
    status, _ = removed.sign_in()
    check("...and they can sign in", status == 200, str(status))
    check("...to the same account", vault_account(email) == account)
    check("...and asking again doesn't repeat the follow-up", len(follow_ups(email)) == 1, str(follow_ups(email)))
    # With that one still open. A second note mustn't get in the way of turning them off.
    set_groups(email, ["member"], wait=False)
    took = wait_for(lambda: not removed.in_vault())
    check("Leaving again ends their token again", took is not None)
    wait_for_sync()
    check("...with Vaultwarden turning them off", vault_enabled_flag(email) is False)
    check("...and still the one follow-up", len(follow_ups(email)) == 1, str(follow_ups(email)))
    set_groups(email, ["board"])

    section("Vaultwarden: offboarding")
    clear_vault_limits()
    email = email_of("fake-vsync-off")
    leaver = vault_person(host, "fake-vsync-off", secrets_by_name)
    owner_id = owner.sync()["profile"]["id"]
    collection = owner.api("GET", f"/api/organizations/{org}/collections")[1]["data"][0]["id"]
    member_id = vault_member(owner, org, email, collection=collection)
    created = owner.api("POST", "/api/ciphers/create", {"cipher": {"type": 2, "name": VAULT_KEY, "organizationId": org, "encryptedFor": owner_id, "secureNote": {"type": 0}, "notes": VAULT_KEY}, "collectionIds": [collection]})
    entry = created[1]["id"] if created[0] == 200 else None
    check("A shared entry exists", bool(entry), str(created)[:200])
    check("Control: their token reaches the API", leaver.in_vault())
    check("Control: they can fetch the shared entry", leaver.api("GET", f"/api/ciphers/{entry}")[0] == 200)
    check("Control: they have their own entries to keep", leaver.api("POST", "/api/ciphers", {"type": 2, "name": VAULT_KEY, "encryptedFor": leaver.sync()["profile"]["id"], "secureNote": {"type": 0}, "notes": VAULT_KEY})[0] == 200)
    status = admin_page_set_active(operator, email, False)
    check("An Operator turns off their account on the admin page", status == 302, str(status))
    took = wait_for(lambda: not leaver.in_vault())
    check("Their token stops working, even used by hand", took is not None)
    print(f"  info  It took {took} seconds.")
    wait_for_sync()
    check("...and refreshing it", leaver.refresh()[0] != 200)
    check("...and fetching the shared entry", leaver.api("GET", f"/api/ciphers/{entry}")[0] != 200)
    check("Vaultwarden has them turned off", vault_enabled_flag(email) is False)
    fresh = VaultPerson(host)
    fresh.app.log_in(email)
    check("They can't log in to core", not fresh.app.logged_in())

    open_items = follow_ups(email)
    check("A follow-up names the organisation and the person", len(open_items) == 1 and org_name in open_items[0][0] and email in open_items[0][0], str(open_items))
    check("...and it's open", open_items and open_items[0][1] is None)
    compose("restart", "sync")
    time.sleep(5)
    wait_for_sync()
    check("Going over everyone again doesn't add another", len(follow_ups(email)) == 1, str(follow_ups(email)))

    board = Browser(host)
    board.log_in(owner_email)
    board.enter_code(totp(secrets_by_name["fake-vsync-owner"], 1))  # each code works once, and they've just used this one
    index = board.get("/admin/").text
    check("Board sees it on the admin page", f"{email} could see them" in html.unescape(index))
    check("...and in its list", org_name in board.get("/admin/accounts/followup/").text)
    member = Browser(host)
    member.log_in(email_of("fake-vsync-member"))
    check("A Member doesn't", member.get("/admin/accounts/followup/").status != 200)
    check("Nobody can add one by hand", board.get("/admin/accounts/followup/add/").status == 403)

    # What a person does: the owner changes the entry and removes them from the organisation.
    status, _ = owner.api("PUT", f"/api/ciphers/{entry}", {"type": 2, "name": VAULT_KEY, "organizationId": org, "encryptedFor": owner_id, "secureNote": {"type": 0}, "notes": VAULT_KEY.replace("bm90LXJlYWw=", "Y2hhbmdlZA==", 1)})
    check("An owner can change the shared entry", status == 200, str(status))
    status, _ = owner.api("DELETE", f"/api/organizations/{org}/users/{member_id}")
    check("...and remove them from the organisation, though their account is off", status == 200, str(status))
    pk = core_db("select id from accounts_followup where about = ?", email)[0][0]
    page = board.get("/admin/accounts/followup/")
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text)
    done = board.send("POST", "/admin/accounts/followup/", [("csrfmiddlewaretoken", token.group(1) if token else ""), ("action", "mark_done"), ("_selected_action", str(pk))])
    check("...and mark the follow-up done", done.status == 302, str(done.status))
    closed = follow_ups(email)[0]
    board_pk = core_db("select id from accounts_user where email = ?", owner_email)[0][0]
    check("It records when and who", closed[1] is not None and closed[2] == board_pk, str(closed))
    check("...and it's no longer shown as to do", f"{email} could see them" not in html.unescape(board.get("/admin/").text))
    check("...while another that's still open is", f"{email_of('fake-vsync-removed')} could see them" in html.unescape(board.get("/admin/").text))

    status = admin_page_set_active(operator, email, True)
    wait_for_sync()
    check("If they come back, their account is on again", vault_enabled_flag(email) is True, str(status))
    fresh = VaultPerson(host)
    fresh.log_in_to_core(email, None)
    fresh.app.enter_code(totp(secrets_by_name["fake-vsync-off"], 1))
    check("...and they can sign in", fresh.sign_in()[0] == 200)
    data = fresh.sync()
    check("...with their own entries", len(data.get("ciphers", [])) == 1, str(len(data.get("ciphers", []))))
    check("...and not the organisation's", org not in [entry["id"] for entry in data.get("profile", {}).get("organizations", [])])

    section("Vaultwarden: an organisation left with no owner")
    clear_vault_limits()
    email = email_of("fake-vsync-solo")
    solo = vault_person(host, "fake-vsync-solo", secrets_by_name)
    vault_org(solo, email, "Fake solo organisation")
    set_groups(email, ["member"])
    text = " ".join(item[0] for item in follow_ups(email))
    check("Losing an organisation's only owner says so", "only owner" in text and "Fake solo organisation" in text, text)
    email = email_of("fake-vsync-co")
    co = vault_person(host, "fake-vsync-co", secrets_by_name)
    vault_member(owner, org, email, kind=0)
    set_groups(email, ["member"])
    text = " ".join(item[0] for item in follow_ups(email))
    check("One owner of two leaving doesn't", text and "only owner" not in text, text)

    section("Vaultwarden: people are found by core's id, not their address")
    clear_vault_limits()
    old_address, new_address = email_of("fake-vsync-renamed"), email_of("fake-vsync-renamed-new")
    renamed = vault_person(host, "fake-vsync-renamed", secrets_by_name)
    in_core(
        f"""
from django.contrib.auth import get_user_model
user = get_user_model().objects.get(email={old_address!r})
user.email = {new_address!r}
user.save()
"""
    )
    wait_for_sync()
    check("Control: Vaultwarden keeps their old address, and they're still in", vault_account(old_address) is not None and renamed.in_vault())
    in_core(
        f"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
user = get_user_model().objects.create_user({old_address!r}, {PASSWORD!r})
user.groups.set(Group.objects.filter(role__key="board"))
user.is_active = False
user.save()
"""
    )
    wait_for_sync()
    check("Turning off someone else who now has that address leaves them alone", renamed.in_vault() and vault_enabled_flag(old_address) is True, str(vault_enabled_flag(old_address)))
    set_active(new_address, False)
    check("Turning off the person themselves, at their new address, ends their token", wait_for(lambda: not renamed.in_vault()) is not None)

    section("Vaultwarden: other ways of leaving")
    deleted = vault_person(host, "fake-vsync-deleted", secrets_by_name)
    in_core(f"from django.contrib.auth import get_user_model as g; g().objects.get(email={email_of('fake-vsync-deleted')!r}).delete()")
    check("Deleting the account in core ends their token", wait_for(lambda: not deleted.in_vault()) is not None)

    section("Vaultwarden: when it's down at the time")
    clear_vault_limits()
    email = email_of("fake-vsync-outage")
    person = vault_person(host, "fake-vsync-outage", secrets_by_name)
    subprocess.run(["docker", "stop", VAULT], capture_output=True)
    try:
        status = admin_page_set_active(operator, email, False)
        check("Turning off an account still works", status == 302, str(status))
        pk = core_db("select id from accounts_user where email = ?", email)[0][0]
        wait_for(lambda: "error" in core_db("select tools from accounts_accesssync where sub = ?", str(pk))[0][0])
        check("Their page on the admin site says it hasn't reached Vaultwarden", "vaultwarden:" in operator.get(f"/admin/accounts/user/{pk}/change/").text)
    finally:
        subprocess.run(["docker", "start", VAULT], capture_output=True)
    wait_for_vault()
    took = wait_for(lambda: not person.in_vault(), 330)
    check("When it's back, the worker tries again and their token stops", took is not None)
    print(f"  info  It took {took} seconds after Vaultwarden was healthy.")
    return {"blocked_vault_tokens": leaver.tokens}


def emergency_token(host, email, password=VAULT_HASH):
    """A password login at the emergency copy, which only someone on the server can reach."""
    connection = emergency_connection()
    form = urllib.parse.urlencode(
        {"grant_type": "password", "username": email, "password": password, "scope": "api offline_access", "client_id": "web", "deviceType": "9", "deviceIdentifier": str(uuid.uuid4()), "deviceName": "chrome"}
    )
    connection.request("POST", "/identity/connect/token", form, {"Content-Type": "application/x-www-form-urlencoded"})
    answer = connection.getresponse()
    text = answer.read().decode(errors="replace")
    connection.close()
    try:
        return answer.status, json.loads(text)
    except ValueError:
        return answer.status, {}


def kit_emergency_address():
    return subprocess.run(["docker", "inspect", "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", VAULT_EMERGENCY], capture_output=True, text=True).stdout.strip()


def emergency_connection():
    """It only speaks HTTPS, with a certificate made on the spot for localhost."""
    return http.client.HTTPSConnection(kit_emergency_address(), 8080, context=ssl._create_unverified_context(), timeout=30)


def emergency_get(path, token=None):
    connection = emergency_connection()
    connection.request("GET", path, headers={"Authorization": f"Bearer {token}"} if token else {})
    answer = connection.getresponse()
    text = answer.read().decode(errors="replace")
    connection.close()
    return answer.status, text


def check_core_down(host, secrets_by_name):
    org = org_of(host)
    clear_limits()
    clear_vault_limits()
    developer, token = git_person(host, "fake-sync-down", secrets_by_name)
    repo = "fake-sync-down-repo"
    make_repo(host, token, repo)
    vault = vault_person(host, "fake-vsync-down", secrets_by_name)
    off_email = email_of("fake-vsync-off-down")
    off = vault_person(host, "fake-vsync-off-down", secrets_by_name)
    set_active(off_email, False)
    wait_for_sync()
    check("Control: someone is turned off in Vaultwarden, for a check below", vault_enabled_flag(off_email) is False)
    sites = tomllib.loads(CONFIG.read_text()).get("website", {}).get("sites", [])
    git_person(host, "fake-sync-rescuer", secrets_by_name)
    check("Control: an Operator has a Forgejo account", "fake-sync-rescuer" in in_forgejo("forgejo", "admin", "user", "list").stdout)
    check("Control: signing in to core works", developer.app.logged_in())

    compose("stop", "core")
    try:
        section("Core is down: Forgejo")
        check("Control: core really is down", Browser(host).get("/").status >= 500)
        check("An open session keeps working", developer.in_git())
        check("...and shows repos", GIT_SECRET in developer.git.get(f"/{org}/{repo}/raw/branch/main/secret.txt").text)
        check("A token keeps working on the API", forgejo_api(host, token, "GET", "/user")[0] == 200)
        check("...and with Git", can_push(host, "fake-sync-down", token, repo, "while-core-is-down"))
        visitor = Person(host)
        visitor.app.cookies = dict(developer.app.cookies)  # logged in to core, so it's only core being down that stops this
        started = time.time()
        reached, answer = visitor.open_git(f"/user/oauth2/{org}")
        check("Someone new to Forgejo can't sign in", not visitor.in_git(), reached)
        check("...and is told straight away, rather than left waiting", time.time() - started < 20, str(time.time() - started))
        for site in sites[:1]:
            check("The public sites don't depend on core", Browser(site["domain"]).get("/").status == 200)

        section("Core is down: getting into Forgejo")
        answer = kit("emergency", "git", "fake-sync-rescuer")
        found = re.search(r"\b[0-9a-f]{40}\b", answer.stdout)
        check("`kit emergency git` makes a token on someone's own account", answer.returncode == 0 and bool(found), answer.stderr[-200:])
        emergency = found.group(0) if found else ""
        status, account = forgejo_api(host, emergency, "GET", "/user")
        check("It works on the API, in their name", status == 200 and account.get("login") == "fake-sync-rescuer", str(status))
        check("...and with Git, on a private repo", can_clone(host, "fake-sync-rescuer", emergency, repo))
        # After --, so it gets past kit's own command line and only the check itself can stop it.
        refused = kit("emergency", "git", "--", "--admin")
        check("It refuses a name that could be read as an option", refused.returncode != 0 and "isn't a valid" in refused.stderr, refused.stderr[-200:])
        check("It refuses someone with no account", kit("emergency", "git", "fake-nobody-at-all").returncode != 0)

        section("Core is down: Vaultwarden")
        check("Control: a token used by hand still reaches the API", vault.in_vault())
        check("...but can't refresh, so a browser's next page load signs it out", vault.refresh()[0] != 200)
        reached, answer = vault.click_sign_in()
        check("Someone logged in to core can't sign in", vault.code is None, reached)
        status, data = vault.identity(grant_type="password", username=email_of("fake-vsync-down"), password=VAULT_HASH, scope="api offline_access", deviceType="9", deviceIdentifier=vault.device, deviceName="chrome")
        check("The public address still refuses a master password", status != 200 and "access_token" not in data, str(status))

        section("Core is down: getting into Vaultwarden")
        answer = kit("emergency", "vault")
        check("`kit emergency vault` starts", answer.returncode == 0, answer.stderr[-300:])
        check("...and says how to reach it", "ssh -L 8222:" in answer.stdout and "https://localhost:8222" in answer.stdout, answer.stdout[:300])
        check("It's up", wait_for(lambda: emergency_is_up(), 60) is not None)
        lines = subprocess.run(["docker", "ps", "--format", "{{.Names}}|{{.Ports}}"], capture_output=True, text=True).stdout.splitlines()
        publishing = sorted(line.split("|")[0] for line in lines if "->" in line)
        check("Only Caddy publishes ports, still", publishing == ["sallskap-caddy-1"], str(publishing))
        listening = [line for line in subprocess.run(["ss", "-ltnH"], capture_output=True, text=True).stdout.splitlines() if ":8222 " in line or ":8080 " in line]
        check("Nothing on the server listens for it", not listening, str(listening))
        info = json.loads(subprocess.run(["docker", "inspect", VAULT_EMERGENCY], capture_output=True, text=True).stdout)[0]
        check("It won't restart by itself", info["HostConfig"]["RestartPolicy"]["Name"] in ("", "no"), str(info["HostConfig"]["RestartPolicy"]))
        check("It keeps Vaultwarden's limits", info["HostConfig"]["Memory"] > 0 and info["HostConfig"]["ReadonlyRootfs"] and info["HostConfig"]["CapDrop"] == ["ALL"])
        aliases = [alias for network in info["NetworkSettings"]["Networks"].values() for alias in network.get("Aliases") or []]
        check("Caddy can't find it under Vaultwarden's name", "vaultwarden" not in aliases, str(aliases))
        resolved = subprocess.run(["docker", "exec", "sallskap-caddy-1", "getent", "hosts", "vaultwarden"], capture_output=True, text=True).stdout.split()
        check("...as that name still means one address, and not this one", resolved and kit_emergency_address() not in resolved, str(resolved))

        status, data = emergency_token(host, email_of("fake-vsync-down"))
        check("The right master password gets in", status == 200 and "access_token" in data, f"{status} {str(data)[:200]}")
        status_sync, text = emergency_get("/api/sync", data.get("access_token"))
        check("...and reaches their vault", status_sync == 200 and email_of("fake-vsync-down") in text, str(status_sync))
        check("That token means nothing at the public address", public_vault_status(host, data.get("access_token")) == 401, str(public_vault_status(host, data.get("access_token"))))
        status, data = emergency_token(host, email_of("fake-vsync-down"), "bm90LXRoZS1yaWdodC1oYXNo")
        check("A wrong master password doesn't", status != 200 and "access_token" not in data, str(status))
        status, data = emergency_token(host, off_email)
        check("An account that's turned off doesn't, even with the right one", status != 200 and "access_token" not in data, f"{status} {str(data)[:200]}")
        check("Its admin page is off", "disabled" in emergency_get("/admin")[1].lower())
        refusals = [vault.identity(grant_type="password", username=email_of("fake-vsync-down"), password=VAULT_HASH, scope="api offline_access", deviceType="9", deviceIdentifier=vault.device, deviceName="chrome")[0] for _ in range(6)]
        check("The public address still refuses a master password, every time", all(status != 200 for status in refusals), str(refusals))
        check("`kit emergency vault` again changes nothing", kit("emergency", "vault").returncode == 0 and kit_emergency_address() != "")

        answer = kit("emergency", "vault", "--off")
        check("`--off` stops it", answer.returncode == 0 and "Stopped" in answer.stdout, answer.stdout + answer.stderr)
        check("...and it's gone", subprocess.run(["docker", "inspect", VAULT_EMERGENCY], capture_output=True).returncode != 0)
        check("...along with its certificate", not (VAULT_DIR / "emergency-tls").exists())
        check("Asking again says so", "wasn't" in kit("emergency", "vault", "--off").stdout)
    finally:
        subprocess.run(["docker", "rm", "--force", VAULT_EMERGENCY], capture_output=True)
        compose("up", "-d", "--wait", "core")

    section("Core is back")
    clear_limits()
    clear_vault_limits()
    check("An open Forgejo session is still fine", developer.in_git())
    returning = Person(host)
    returning.app.cookies = dict(developer.app.cookies)
    returning.sign_in_to_git()
    check("Signing in to Forgejo works again", returning.in_git())
    status, _ = vault.sign_in()
    check("Signing in to Vaultwarden works again", status == 200, str(status))
    check("...into the same account", vault.in_vault())

    section("Switching off the vault with its emergency copy running")
    kit("emergency", "vault")
    check("Control: the copy is running", wait_for(emergency_is_up, 60) is not None)
    check("`kit status` says so", "emergency copy is running" in kit("status").stdout)
    done = kit("disable", "vault")
    check("`kit disable vault` works", done.returncode == 0, done.stderr[-300:])
    check("...and stops it too", subprocess.run(["docker", "inspect", VAULT_EMERGENCY], capture_output=True).returncode != 0)
    check("...along with its certificate", not (VAULT_DIR / "emergency-tls").exists())
    done = kit("enable", "vault")
    check("`kit enable vault` brings Vaultwarden back", done.returncode == 0, done.stderr[-300:])
    wait_for_vault()


def emergency_is_up():
    try:
        return emergency_get("/alive")[0] == 200
    except (OSError, http.client.HTTPException):
        return False


def public_vault_status(host, token):
    person = VaultPerson(host)
    person.tokens = {"access_token": token or ""}
    return person.api("GET", "/api/sync")[0]


def check_access_table(host, operator, secrets_by_name):
    """Both tools together, as some of this takes core itself down."""
    check_sync_exposure(host)
    clear_limits()
    check_git_own_2fa(host, secrets_by_name)
    time.sleep(31)
    check_git_groups_now(host, operator, secrets_by_name)
    clear_limits()
    check_git_offboarding(host, operator, secrets_by_name)
    clear_limits()
    check_worker_down(host, operator, secrets_by_name)
    clear_limits()
    state = check_vault_offboarding(host, operator, secrets_by_name)
    clear_limits()
    check_core_down(host, secrets_by_name)
    clear_limits()
    clear_vault_limits()
    # Someone who has left, to check after a restart that they stay out.
    keeper_name = "fake-sync-blocked"
    _, token = git_person(host, keeper_name, secrets_by_name)
    admin_page_set_active(operator, email_of(keeper_name), False)
    wait_for_sync()
    return {**state, "blocked_git_token": token, "worker_token_hashes": hashlib.sha256(SYNC_ENV.read_bytes()).hexdigest()}


def verify_access(host, state):
    section("Access: do those who've left stay out?")
    check_sync_exposure(host)
    check("A blocked Forgejo token stays blocked", forgejo_api(host, state["blocked_git_token"], "GET", "/user")[0] == 403)
    blocked = VaultPerson(host)
    blocked.tokens = state["blocked_vault_tokens"]
    # That person came back during the checks, so this token ended when they left.
    check("A Vaultwarden token ended by leaving stays ended", not blocked.in_vault())
    check("The worker's settings didn't change", hashlib.sha256(SYNC_ENV.read_bytes()).hexdigest() == state["worker_token_hashes"])
    wait_for_sync()
    check("Nothing is waiting to be sent", core_db("select count(*) from accounts_accesssync where pending = 1")[0][0] == 0)


# ---- Backups --------------------------------------------------------------------


BACKUP_DIR = Path("/srv/backup")
BACKUP_ENV = Path("/etc/sallskap/backup.env")
SCRATCH = Path("/root/.sallskap-restore-check")


def site_domains():
    return [site["domain"] for site in tomllib.loads(CONFIG.read_text()).get("website", {}).get("sites", [])]


def backup_password():
    return dict(
        line.split("=", 1) for line in BACKUP_ENV.read_text().splitlines() if "=" in line
    )["RESTIC_PASSWORD"]


def restic(*args, password=None, repo=None):
    environment = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": "/root",
        "RESTIC_PASSWORD": password or backup_password(),
    }
    return subprocess.run(
        ["restic", "--repo", str(repo or BACKUP_DIR / "repo"), *args],
        env=environment,
        capture_output=True,
        text=True,
    )


def latest_snapshot():
    result = restic("snapshots", "--latest", "1", "--tag", "sallskap", "--json")
    return json.loads(result.stdout)[-1] if result.returncode == 0 and result.stdout.strip() else {}


def watch_while(hosts, stop):
    """Poll each host over HTTPS until told to stop, and report what answered. This is
    what shows that the public sites stay up while logins are paused for a backup."""
    answers = {host: [] for host in hosts}

    def poll():
        context = ssl.create_default_context()
        while not stop.is_set():
            for host in hosts:
                try:
                    connection = http.client.HTTPSConnection(host, context=context, timeout=4)
                    connection.request("GET", "/")
                    answers[host].append(connection.getresponse().status)
                    connection.close()
                except Exception:  # a refused or dropped connection is the answer here
                    answers[host].append(0)
            time.sleep(0.2)

    watcher = threading.Thread(target=poll, daemon=True)
    watcher.start()
    return answers, watcher


def check_backup_before_the_first_one(host):
    section("Backups: before the first one")
    check("restic is installed", bool(shutil.which("restic")))
    timer = subprocess.run(
        ["systemctl", "is-enabled", "sallskap-backup.timer"], capture_output=True, text=True
    ).stdout.strip()
    check("A backup is scheduled every night", timer == "enabled", timer)
    next_run = subprocess.run(
        ["systemctl", "show", "-p", "NextElapseUSecRealtime", "--value", "sallskap-backup.timer"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    check("...and its next run is in the future", bool(next_run) and next_run != "0", next_run)
    if not (BACKUP_DIR / "repo").exists():
        check("`kit status` says nothing has been backed up yet", "no backups have been taken yet" in kit("status").stdout)


CADDY = "sallskap-caddy-1"


def container_state(name):
    return json.loads(subprocess.run(["docker", "inspect", name], capture_output=True, text=True).stdout)[0]


def module_containers():
    """By name, as each module runs from its own Compose file and the project-wide
    `docker compose ps` only knows about core, the worker and Caddy."""
    containers = {"core": "sallskap-core-1", "the worker": "sallskap-sync-1"}
    if git_enabled():
        containers["Forgejo"] = FORGEJO
    if vault_enabled():
        containers["the vault"] = VAULT
    return containers


def check_backup_runs(host, sites):
    section("Backups: taking one")
    first_time = not (BACKUP_DIR / "repo").exists()
    caddy_started = container_state(CADDY)["State"]["StartedAt"]
    was_started = {name: container_state(c)["State"]["StartedAt"] for name, c in module_containers().items()}
    stop = threading.Event()
    answers, watcher = watch_while([host, *sites], stop)
    started = time.monotonic()
    result = kit("backup")
    took = time.monotonic() - started
    stop.set()
    watcher.join(timeout=10)

    check("`kit backup` finishes without an error", result.returncode == 0, result.stdout[-700:] + result.stderr[-700:])
    check("...and says how long logins were paused", "paused for" in result.stdout, result.stdout[-200:])
    print(f"  ----  the whole command took {took:.0f} s; it reported: {_paused_line(result.stdout)}")
    if first_time:
        check("...and prints the backup password the first time only", "can't be recovered" in result.stdout)

    for site in sites:
        seen = answers.get(site, [])
        check(f"The public site {site} stayed up throughout", bool(seen) and set(seen) == {200}, f"{len(seen)} requests: {sorted(set(seen))}")
    # Caddy stays up, so a login during the pause gets its 502 rather than nothing.
    logins = answers.get(host, [])
    check(
        "Logins were paused while the copy was taken",
        bool(logins) and (502 in logins or 0 in logins),
        f"{len(logins)} requests: {sorted(set(logins))}",
    )
    check("...and nothing in between: either a login page or a plain 502", set(logins) <= {0, 302, 502}, str(sorted(set(logins))))
    check("...and they answer again afterwards", Browser(host).get("/accounts/login/").status == 200)
    for name, container in module_containers().items():
        state = container_state(container)["State"]
        health = state.get("Health", {}).get("Status", state.get("Status"))
        check(f"{name} is healthy again", health in ("healthy", "running"), str(health))
        # The whole point of the pause: nothing was writing while its files were read.
        check(f"...and {name} was stopped for the copy, not read while running", state["StartedAt"] != was_started[name])
    check("Caddy never stopped, which is why the public sites didn't", container_state(CADDY)["State"]["StartedAt"] == caddy_started)
    wait_for_sync()
    check("Nothing is left waiting to be sent to a tool", core_db("select count(*) from accounts_accesssync where pending = 1")[0][0] == 0)
    return took


def _paused_line(output):
    return next((line.strip() for line in output.splitlines() if "paused for" in line), "nothing")


def check_what_is_in_a_backup(host):
    section("Backups: what's in one")
    snapshot = latest_snapshot()
    check("There's a snapshot tagged for this installation", bool(snapshot), str(snapshot)[:200])
    if not snapshot:
        return
    # What every module declares, switched on or not, as disabling one keeps its data.
    wanted = {"/etc/sallskap", "/srv/core", "/srv/caddy", "/var/lib/sallskap-deploy"}
    for manifest in (ROOT / "modules").glob("*/module.toml"):
        for declared in tomllib.loads(manifest.read_text()).get("data", []):
            if Path(declared).exists():
                wanted.add(declared)
    check("It covers the config, core, and every module's data", wanted <= set(snapshot["paths"]), str(sorted(wanted - set(snapshot["paths"]))))
    check("...and nothing else", not set(snapshot["paths"]) - wanted, str(sorted(set(snapshot["paths"]) - wanted)))
    check("It doesn't hold the backups themselves", "/srv/backup" not in snapshot["paths"])

    tags = set(snapshot["tags"])
    running = {}
    for container in (FORGEJO, VAULT):
        image = container_state(container)["Config"]["Image"]
        running[image.rsplit("/", 1)[-1].split(":")[0]] = image.rsplit(":", 1)[-1]
    for name, version in running.items():
        check(f"It records the {name} version it was taken with", f"image:{name}={version}" in tags, str(sorted(tags)))
    commit = subprocess.run(
        ["git", "-C", "/opt/sallskap", "rev-parse", "--short", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    check("...and which version of the kit took it", f"version:{commit}" in tags, str(sorted(tags)))

    listed = restic("ls", "latest").stdout
    for path in (
        "/srv/core/db.sqlite3",
        "/etc/sallskap/core.env",
        "/etc/sallskap/sync.env",
        "/etc/sallskap/backup.env",
        "/srv/caddy/data",
        "/var/lib/sallskap-deploy/.ssh/authorized_keys",
    ):
        check(f"{path} is in it", path in listed)
    if git_enabled():
        check("Forgejo's database is in it", "/srv/git/data/gitea.db" in listed)
        check("...and its repositories", "/srv/git/git/repositories" in listed)
    if vault_enabled():
        check("The vault's database is in it", "/srv/vault/db.sqlite3" in listed)


def check_a_backup_is_sound(host):
    section("Backups: is it readable, and only with the password?")
    integrity = restic("check", "--read-data")
    check("restic's own check passes, reading every byte", integrity.returncode == 0, integrity.stdout[-500:])
    wrong = restic("snapshots", password="not-the-password")
    check("The wrong password is refused", wrong.returncode != 0 and "wrong password" in (wrong.stdout + wrong.stderr))
    check(f"Only root can read {BACKUP_DIR}", oct(BACKUP_DIR.stat().st_mode & 0o777) == "0o700", oct(BACKUP_DIR.stat().st_mode & 0o777))
    check("Only root can read the password", oct(BACKUP_ENV.stat().st_mode & 0o777) == "0o600", oct(BACKUP_ENV.stat().st_mode & 0o777))
    second = kit("backup")
    check("Running it again works and doesn't print the password again", second.returncode == 0 and "can't be recovered" not in second.stdout)


def check_the_copy_can_be_read_back(host):
    """Restore into a folder of its own and read what comes out. A full restore onto a
    fresh server is a separate test; this one shows the copy is usable and consistent."""
    section("Backups: does what's in it still work?")
    shutil.rmtree(SCRATCH, ignore_errors=True)
    SCRATCH.mkdir(mode=0o700)
    restored = restic("restore", "latest", "--target", str(SCRATCH))
    check("The backup restores", restored.returncode == 0, restored.stderr[-500:])
    if restored.returncode != 0:
        return

    databases = [("core", "/srv/core/db.sqlite3", "accounts_user")]
    if git_enabled():
        databases.append(("Forgejo", "/srv/git/data/gitea.db", "user"))
    if vault_enabled():
        databases.append(("the vault", "/srv/vault/db.sqlite3", "users"))
    for name, path, table in databases:
        copy = SCRATCH / path.lstrip("/")
        check(f"{name}'s database came back", copy.exists(), str(copy))
        if not copy.exists():
            continue
        # These run in WAL mode, and stopping a container doesn't always leave the log
        # checkpointed: Forgejo's held 4 MB of writes its database file knew nothing
        # about, while core had folded its back in and removed it. Whole folders are
        # copied, so whichever it is, the pair travels together. The row count below is
        # what proves it: a backup of the database alone gives an old number here.
        log = copy.parent / (copy.name + "-wal")
        print(f"  ----  {name}: {'the log came with it' if log.exists() else 'no log; it had been folded in'}")
        # Opened read-write, on this copy only, so SQLite replays the log as it would
        # on a restored server.
        connection = sqlite3.connect(str(copy))
        try:
            sound = connection.execute("pragma integrity_check").fetchone()[0]
            rows = connection.execute(f'select count(*) from "{table}"').fetchone()[0]
        finally:
            connection.close()
        live = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            live_rows = live.execute(f'select count(*) from "{table}"').fetchone()[0]
        finally:
            live.close()
        check(f"...{name}'s copy is a whole database", sound == "ok", sound)
        check(f"...with every row the server has ({live_rows})", rows == live_rows, f"{rows} of {live_rows}")

    for path in ("/etc/sallskap/core.env", "/etc/sallskap/sync.env", "/etc/sallskap/vault.env", "/etc/sallskap/config.toml"):
        if not Path(path).exists():
            continue
        copy = SCRATCH / path.lstrip("/")
        same = copy.exists() and copy.read_bytes() == Path(path).read_bytes()
        check(f"{path} came back exactly as it is", same)
    if git_enabled():
        head = SCRATCH / "srv/git/git/repositories"
        check("Forgejo's repositories came back", head.exists() and any(head.rglob("HEAD")))
    for domain in site_domains():
        served = Path(f"/srv/website/{domain}/current")
        if not served.exists():
            continue
        page = served.resolve() / "index.html"
        copy = SCRATCH / str(page).lstrip("/")
        check(f"The published page for {domain} came back", copy.exists() and copy.read_bytes() == page.read_bytes(), str(copy))
        link = SCRATCH / f"srv/website/{domain}/current"
        check(f"...and {domain} still points at that release", link.is_symlink() and link.resolve().name == served.resolve().name)
    certificates = list((SCRATCH / "srv/caddy/data").rglob("*.crt"))
    check("Caddy's certificates came back, so a restore needs no new ones", bool(certificates), f"{len(certificates)} found")
    shutil.rmtree(SCRATCH, ignore_errors=True)


def check_a_copy_can_leave_the_server(host):
    section("Backups: taking a copy off the server")
    exported = Path("/root/.sallskap-export-check.tar")
    exported.unlink(missing_ok=True)
    before = time.time()
    result = kit("backup", "export", str(exported))
    check("`kit backup export` writes one file", result.returncode == 0 and exported.exists(), result.stderr[-300:])
    if not exported.exists():
        return
    check("...only root can read it", oct(exported.stat().st_mode & 0o777) == "0o600")
    with tarfile.open(exported) as tar:
        names = tar.getnames()
    check("...holding the whole repository", "repo/config" in names)
    check("...and not kit's lock or its own notes", not [n for n in names if "kit.lock" in n or "last-export" in n])

    elsewhere = Path("/root/.sallskap-elsewhere")
    shutil.rmtree(elsewhere, ignore_errors=True)
    elsewhere.mkdir(mode=0o700)
    subprocess.run(["tar", "-xf", str(exported), "-C", str(elsewhere)], check=True)
    read_back = restic("snapshots", "--json", repo=elsewhere / "repo")
    check("Unpacked somewhere else, it still reads with the password", read_back.returncode == 0, read_back.stderr[-300:])
    if read_back.returncode == 0:
        check("...and holds the same snapshots", len(json.loads(read_back.stdout)) == len(json.loads(restic("snapshots", "--json").stdout)))
    refused = restic("snapshots", password="not-the-password", repo=elsewhere / "repo")
    check("...and not without it", refused.returncode != 0)

    # Damage would be noticed: proved on this unpacked copy, so the real repository is
    # never touched. Flipping a byte in the live one wouldn't prove it, as the backup
    # that runs first prunes unreferenced packs and can drop the damage with them.
    packs = [p for p in (elsewhere / "repo" / "data").rglob("*") if p.is_file()]
    check("The unpacked copy has pack files to damage", bool(packs))
    if packs:
        damaged = bytearray(packs[-1].read_bytes())
        damaged[len(damaged) // 2] ^= 0xFF
        packs[-1].chmod(0o644)  # restic stores them read-only
        packs[-1].write_bytes(bytes(damaged))
        noticed = restic("check", "--read-data", repo=elsewhere / "repo")
        check(
            "A single flipped byte is noticed by restic's own check",
            "repository contains errors" in noticed.stdout + noticed.stderr,
            (noticed.stdout + noticed.stderr)[-300:],
        )

    # One line over SSH is the way a copy is meant to leave, so check the stream too.
    streamed = Path("/root/.sallskap-stream-check.tar")
    with streamed.open("wb") as target:
        piped = subprocess.run(["/usr/local/bin/kit", "backup", "export", "-"], stdout=target, stderr=subprocess.PIPE)
    check("Exporting to standard output works, for a copy over SSH", piped.returncode == 0, piped.stderr.decode()[-300:])
    with tarfile.open(streamed) as tar:
        check("...and gives the same thing", "repo/config" in tar.getnames())

    # Not just that a copy has left at some point: that this one was recorded. An older
    # note from an earlier run would otherwise hide an export that records nothing.
    note = BACKUP_DIR / "last-export"
    recorded = float(note.read_text().strip() or 0) if note.exists() else 0
    check("The export was recorded as having just happened", recorded >= before, f"{recorded} vs {before}")
    status = kit("status").stdout
    check("`kit status` reports the backup as fresh", "ok   last backup" in status, status[-400:])
    check("...and that a copy has left the server", "ok   last copy off this server" in status, status[-400:])
    shutil.rmtree(elsewhere, ignore_errors=True)
    exported.unlink(missing_ok=True)
    streamed.unlink(missing_ok=True)


def check_the_vaults_emergency_copy_is_stopped(host):
    if not vault_enabled():
        return
    section("Backups: the vault's emergency copy")
    started = kit("emergency", "vault")
    check("The emergency copy starts", started.returncode == 0, started.stderr[-300:])
    running = subprocess.run(["docker", "ps", "--filter", f"name={VAULT_EMERGENCY}", "-q"], capture_output=True, text=True)
    check("...and is running", bool(running.stdout.strip()))
    result = kit("backup")
    check("A backup finishes with it running", result.returncode == 0, result.stderr[-300:])
    after = subprocess.run(["docker", "ps", "-a", "--filter", f"name={VAULT_EMERGENCY}", "-q"], capture_output=True, text=True)
    check("...and stops it, as it writes to the data being copied", not after.stdout.strip())
    # Neither `compose stop` nor `compose up` reaches a `compose run` container, so this
    # is what shows the module's own stop step ran rather than something else.
    check("...and takes its certificate with it", not (VAULT_DIR / "emergency-tls").exists())
    check("`kit status` no longer mentions it", "emergency copy is running" not in kit("status").stdout)


def check_what_is_refused(host):
    section("Backups: what kit refuses")
    held = BACKUP_DIR / "kit.lock"
    with held.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = kit("backup")
    check("A second backup is refused rather than interleaved", result.returncode == 1 and "Another backup is already running" in result.stdout + result.stderr, result.stdout[-200:])

    # Someone could type this on the wrong server. It must stop before touching anything.
    live = Path("/srv/core/db.sqlite3")
    before = (live.stat().st_mtime, live.stat().st_size)
    attempt = kit("restore")
    check("A restore over a working installation is refused", attempt.returncode == 1 and "already has an installation" in attempt.stdout + attempt.stderr, attempt.stdout[-200:])
    check("...and it changed nothing", (live.stat().st_mtime, live.stat().st_size) == before)
    check("...and everything is still running", container_state("sallskap-core-1")["State"].get("Health", {}).get("Status") == "healthy")


def backups():
    host = app_host()
    sites = site_domains()
    print(f"Checking backups on https://{host}, with public sites: {', '.join(sites) or 'none'}")
    check_backup_before_the_first_one(host)
    check_backup_runs(host, sites)
    check_what_is_in_a_backup(host)
    check_a_backup_is_sound(host)
    check_the_copy_can_be_read_back(host)
    check_a_copy_can_leave_the_server(host)
    check_the_vaults_emergency_copy_is_stopped(host)
    check_what_is_refused(host)


def prepare():
    if STATE.exists():
        raise SystemExit(f"{STATE} exists. Run cleanup first.")
    host = app_host()
    print(f"Checking https://{host}")
    secrets_by_name = create_fake_users()
    check_exposure(host)
    check_https(host)
    clear_limits()
    operator = check_login(host, secrets_by_name)
    check_lockout(host)
    # Each code works once, so the next users log in with their own fresh ones.
    check_access(host, operator, secrets_by_name)
    check_links(host, secrets_by_name)
    check_recovery(host)
    tool = check_login_service(host, secrets_by_name)
    forgejo = check_forgejo(host, secrets_by_name) if git_enabled() else None
    vault = check_vault(host, secrets_by_name) if vault_enabled() else None
    access = check_access_table(host, operator, secrets_by_name) if git_enabled() and vault_enabled() else None
    if forgejo:
        forgejo["fingerprint"] = forgejo_fingerprint()  # after those, which make tokens too

    keeper = Browser(host)
    keeper.log_in(email_of("fake-member"))
    STATE.write_text(
        json.dumps(
            {
                "operator_cookies": operator.cookies,
                "member_cookies": keeper.cookies,
                "env": env_fingerprint(),
                "data": data_snapshot(),
                "tool": tool,
                "git": forgejo,
                "vault": vault,
                "access": access,
                "signing_key": json.loads(Browser(host).get("/.well-known/jwks.json").text)["keys"][0]["kid"],
            }
        )
    )
    STATE.chmod(0o600)


def verify():
    host = app_host()
    state = json.loads(STATE.read_text())
    section("Did logins and data survive?")
    operator = Browser(host, cookies=state["operator_cookies"])
    check("An Operator's session is still logged in", operator.logged_in())
    check("...and still reaches the admin page", operator.get("/admin/accounts/user/").status == 200)
    member = Browser(host, cookies=state["member_cookies"])
    check("A Member's session is still logged in", member.logged_in())
    clear_limits()
    fresh = Browser(host)
    fresh.log_in(email_of("fake-member"))
    check("A new login still works", fresh.logged_in())
    now = data_snapshot()
    check("Users and passwords are unchanged", now["users"] == state["data"]["users"])
    check("Groups and permissions are unchanged", now["groups"] == state["data"]["groups"])
    check("The secret key is unchanged", env_fingerprint() == state["env"])
    published = json.loads(Browser(host).get("/.well-known/jwks.json").text)["keys"]
    check("The signing key is unchanged", [key["kid"] for key in published] == [state["signing_key"]])
    tool = state["tool"]
    back = sent_back(member.get(authorize_path(tool["client_id"])))
    answer = exchange(host, tool, back["code"]) if back and back.get("code") else None
    check("A registered tool can still log someone in", bool(answer) and answer.status == 200, str(back))
    if answer:
        check("...with a signature that still checks out", "refused" not in verified_claims(host, tool, json.loads(answer.text)["id_token"]))
    status = docker_inspect("core")["State"]
    check("Core is healthy", status.get("Health", {}).get("Status") == "healthy", str(status.get("Health", {}).get("Status")))
    check_exposure(host)
    if state.get("git"):
        verify_forgejo(host, state["git"])
    if state.get("vault"):
        verify_vault(host, state["vault"])
    if state.get("access"):
        verify_access(host, state["access"])


def burst():
    host = app_host()
    clear_limits()
    core = docker_inspect("core")
    cgroup = Path(f"/sys/fs/cgroup/system.slice/docker-{core['Id']}.scope")
    oom_before = _oom_kills(cgroup)
    results = []

    def one_login():
        browser = Browser(host)
        browser.log_in(email_of("fake-member"))
        results.append(browser.logged_in())

    section("Many logins at once")
    threads = [threading.Thread(target=one_login) for _ in range(12)]
    started = time.time()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    took = time.time() - started
    limit_mb = core["HostConfig"]["Memory"] / 1024**2
    peak_file = cgroup / "memory.peak"
    peak_mb = int(peak_file.read_text()) / 1024**2 if peak_file.exists() else None
    print(f"  info  12 logins took {took:.1f}s. Peak memory {peak_mb and round(peak_mb)} MB of a {limit_mb:.0f} MB limit.")
    check("All 12 logins succeeded", all(results) and len(results) == 12, f"{sum(results)} of {len(results)}")
    check("Nothing was killed for using too much memory", _oom_kills(cgroup) == oom_before)
    clear_limits()
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    info = docker_inspect("sync")
    peak_file = Path(f"/sys/fs/cgroup/system.slice/docker-{info['Id']}.scope/memory.peak")
    if peak_file.exists():
        print(f"  info  The worker's peak memory since it started: {int(peak_file.read_text()) // 1024**2} MB of a {info['HostConfig']['Memory'] // 1024**2} MB limit.")
    if state.get("git"):
        burst_forgejo(host, state["git"])
    if state.get("vault"):
        burst_vault(host, state["vault"])


def _oom_kills(cgroup):
    events = cgroup / "memory.events"
    if not events.exists():
        return None
    return int(re.search(r"^oom_kill (\d+)$", events.read_text(), re.M).group(1))


def cleanup():
    if git_enabled():
        cleanup_forgejo()
    if vault_enabled():
        cleanup_vault()
    subprocess.run(["docker", "rm", "--force", VAULT_EMERGENCY], capture_output=True)
    in_core(
        f"""
from django.contrib.auth import get_user_model
from django.core.cache import cache
from accounts.models import FollowUp
print(FollowUp.objects.filter(about__startswith="fake-", about__endswith="@example.org").delete())
print(FollowUp.objects.filter(about__startswith="A deleted account").delete())
print(get_user_model().objects.filter(email__startswith="fake-", email__endswith="@example.org").delete())
print(get_user_model().objects.filter(email__startswith="nobody", email__endswith="@example.org").delete())
print(get_user_model().objects.filter(email__in={FAKE_OTHER!r}).delete())
cache.clear()
"""
    )
    # What's left of them, once every tool knows. This only runs on an installation with no real members.
    wait_for_sync()
    in_core("from accounts.models import AccessSync; AccessSync.objects.filter(user__isnull=True).delete()")
    STATE.unlink(missing_ok=True)
    kit("tool", "remove", FAKE_TOOL)
    print(f"Fake tool left: {FAKE_TOOL in kit('tool', 'list').stdout}")
    remaining = in_core(
        'from django.contrib.auth import get_user_model; print(get_user_model().objects.filter(email__endswith="@example.org").count())'
    )
    print(f"Fake users left: {remaining}")


def main():
    stages = {"prepare": prepare, "verify": verify, "burst": burst, "backups": backups, "cleanup": cleanup}
    if len(sys.argv) != 2 or sys.argv[1] not in stages:
        raise SystemExit(__doc__)
    try:
        stages[sys.argv[1]]()
    except Exception as error:  # keep the results so far, as they're what's needed most when something breaks
        check(f"The checks stopped early with {type(error).__name__}", False, str(error)[:300])
    if sys.argv[1] != "cleanup":
        print(f"\n{'All checks passed.' if not failures else f'{failures} checks FAILED.'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
