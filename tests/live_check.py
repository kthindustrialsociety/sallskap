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
import threading
import time
import tomllib
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = Path("/root/.sallskap-live-check.json")
CONFIG = Path("/etc/sallskap/config.toml")
CORE_ENV = Path("/etc/sallskap/core.env")
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
}

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
            if response.status not in (301, 302):
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
    "groups": sorted((g.pk, g.name, g.role.key, sorted(g.permissions.values_list("pk", flat=True))) for g in Group.objects.all()),
}))
"""
    return json.loads(in_core(code).splitlines()[-1])


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
    check("The server only listens on SSH, HTTP and HTTPS", open_ports <= {"22", "80", "443"}, str(sorted(open_ports)))

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
    expected = {"22/tcp", "80/tcp", "443/tcp", "443/udp"}
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

    for path in ("/accounts/signup/", "/accounts/password/reset/", "/accounts/email/"):
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

    keeper = Browser(host)
    keeper.log_in(email_of("fake-member"))
    STATE.write_text(
        json.dumps(
            {
                "operator_cookies": operator.cookies,
                "member_cookies": keeper.cookies,
                "env": env_fingerprint(),
                "data": data_snapshot(),
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
    status = docker_inspect("core")["State"]
    check("Core is healthy", status.get("Health", {}).get("Status") == "healthy", str(status.get("Health", {}).get("Status")))
    check_exposure(host)


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


def _oom_kills(cgroup):
    events = cgroup / "memory.events"
    if not events.exists():
        return None
    return int(re.search(r"^oom_kill (\d+)$", events.read_text(), re.M).group(1))


def cleanup():
    in_core(
        """
from django.contrib.auth import get_user_model
from django.core.cache import cache
print(get_user_model().objects.filter(email__startswith="fake-", email__endswith="@example.org").delete())
print(get_user_model().objects.filter(email__startswith="nobody", email__endswith="@example.org").delete())
cache.clear()
"""
    )
    STATE.unlink(missing_ok=True)
    remaining = in_core(
        'from django.contrib.auth import get_user_model; print(get_user_model().objects.filter(email__endswith="@example.org").count())'
    )
    print(f"Fake users left: {remaining}")


def main():
    stages = {"prepare": prepare, "verify": verify, "burst": burst, "cleanup": cleanup}
    if len(sys.argv) != 2 or sys.argv[1] not in stages:
        raise SystemExit(__doc__)
    stages[sys.argv[1]]()
    if sys.argv[1] != "cleanup":
        print(f"\n{'All checks passed.' if not failures else f'{failures} checks FAILED.'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
