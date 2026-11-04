#!/usr/bin/env python3
"""Checks a real, running installation from the outside: through Caddy, over HTTPS,
with a real certificate. Run on the server as root. It creates fake users at
@example.org, so don't run it on an installation with real members.

    python3 tests/live_check.py prepare   create fake users and run every check
    python3 tests/live_check.py verify    after a rebuild, reboot or second setup:
                                          did logins and data survive?
    python3 tests/live_check.py burst     many logins at once: does the memory limit hold?
    python3 tests/live_check.py cleanup   remove the fake users

Standard library only, as the rest of the kit's tooling is.
"""

import base64
import hashlib
import hmac
import http.client
import json
import re
import secrets
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = Path("/root/.sallskap-live-check.json")
CONFIG = Path("/etc/sallskap/config.toml")
CORE_ENV = Path("/etc/sallskap/core.env")
GIT_ENV = Path("/etc/sallskap/git.env")
FORGEJO = "sallskap-forgejo-1"
FORGEJO_SERVICE_USER = "sallskap-admin"
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


def set_groups(email, roles):
    in_core(
        f"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
get_user_model().objects.get(email={email!r}).groups.set(Group.objects.filter(role__key__in={roles!r}))
"""
    )


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
    check("It can't gain privileges", "no-new-privileges:true" in config["SecurityOpt"])
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
    check("Opening the sign-in page goes straight to the login service", first.status == 302 and first.location.endswith(f"/user/oauth2/{org_of(host)}"), f"{first.status} {first.location}")
    second = looping.get("/user/login")
    check("Coming straight back shows the page, so a login error can't loop", second.status == 200, f"{second.status} {second.location}")

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
    """Someone can set a Forgejo password on their account page. It must not open anything."""
    section("Forgejo: a password set in its own settings")
    chosen = "fake-forgejo-password-42"
    answer = person.git.send("POST", "/user/settings/account", {"password": chosen, "retype": chosen})
    print(f"  info  Setting one answered {answer.status} {answer.location}")
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
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    host = app_host()
    token = state.get("git", {}).get("operator_token")
    if token:
        print("Fake repo:", forgejo_api(host, token, "DELETE", f"/repos/{org_of(host)}/fake-repo")[0])
    listed = in_forgejo("forgejo", "admin", "user", "list").stdout
    for name in re.findall(r"^\d+\s+(fake-\S+)", listed, re.M):
        in_forgejo("forgejo", "admin", "user", "delete", "--username", name, "--purge")
    left = re.findall(r"^\d+\s+(fake-\S+)", in_forgejo("forgejo", "admin", "user", "list").stdout, re.M)
    print(f"Fake accounts left in Forgejo: {len(left)}")


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
    if state.get("git"):
        burst_forgejo(host, state["git"])


def _oom_kills(cgroup):
    events = cgroup / "memory.events"
    if not events.exists():
        return None
    return int(re.search(r"^oom_kill (\d+)$", events.read_text(), re.M).group(1))


def cleanup():
    if git_enabled():
        cleanup_forgejo()
    in_core(
        f"""
from django.contrib.auth import get_user_model
from django.core.cache import cache
print(get_user_model().objects.filter(email__startswith="fake-", email__endswith="@example.org").delete())
print(get_user_model().objects.filter(email__startswith="nobody", email__endswith="@example.org").delete())
print(get_user_model().objects.filter(email__in={FAKE_OTHER!r}).delete())
cache.clear()
"""
    )
    STATE.unlink(missing_ok=True)
    kit("tool", "remove", FAKE_TOOL)
    print(f"Fake tool left: {FAKE_TOOL in kit('tool', 'list').stdout}")
    remaining = in_core(
        'from django.contrib.auth import get_user_model; print(get_user_model().objects.filter(email__endswith="@example.org").count())'
    )
    print(f"Fake users left: {remaining}")


def main():
    stages = {"prepare": prepare, "verify": verify, "burst": burst, "cleanup": cleanup}
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
