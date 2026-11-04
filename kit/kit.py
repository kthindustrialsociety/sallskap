#!/usr/bin/env python3
"""kit: operate a Sällskap installation. See docs/spec.md §7.5 and §10.3.

Standard library only, so it runs on a fresh server before anything else is
installed. Needs Python 3.11 or newer.
"""

import argparse
import base64
import fcntl
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
PROJECT = "sallskap"  # the name in compose.yml: every module joins this Compose project
DEPLOY_USER = "sallskap-deploy"
KIT_COMMAND = "/usr/local/bin/kit"
CADDY_ROOT = "/srv/website"
RELEASES_KEPT = 5
MAX_UPLOAD_BYTES = 2 * 1024**3
MAX_UPLOAD_FILES = 100_000
CORE_UID = 10001  # the user core runs as: see core/Dockerfile
FORGEJO_UID = 1000  # the user Forgejo's rootless image runs as

SITE_HEADER = "[[website.sites]]"
SITE_FIELDS = {"domain", "aliases", "spa", "www"}
HOSTNAME = re.compile(
    r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]"
)


EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[a-z0-9.-]+")


class KitError(Exception):
    """A problem to explain to the operator, without a traceback."""


@dataclass(frozen=True)
class Paths:
    config_dir: Path = Path(os.environ.get("SALLSKAP_CONFIG_DIR", "/etc/sallskap"))
    website_dir: Path = Path(os.environ.get("SALLSKAP_WEBSITE_DIR", CADDY_ROOT))
    deploy_home: Path = Path(os.environ.get("SALLSKAP_DEPLOY_HOME", "/var/lib/sallskap-deploy"))
    core_dir: Path = Path(os.environ.get("SALLSKAP_CORE_DIR", "/srv/core"))
    git_dir: Path = Path(os.environ.get("SALLSKAP_GIT_DIR", "/srv/git"))

    @property
    def config(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def core_env(self) -> Path:
        return self.config_dir / "core.env"

    @property
    def git_env(self) -> Path:
        return self.config_dir / "git.env"

    @property
    def caddyfile(self) -> Path:
        return self.config_dir / "generated" / "Caddyfile"

    @property
    def authorized_keys(self) -> Path:
        return self.deploy_home / ".ssh" / "authorized_keys"

    def site_dir(self, domain: str) -> Path:
        return self.website_dir / domain


@dataclass(frozen=True)
class Site:
    domain: str
    aliases: tuple[str, ...] = ()
    spa: bool = True
    www: bool | None = None

    @property
    def redirects(self) -> list[str]:
        """Every hostname that redirects to this site's domain."""
        names = list(self.aliases)
        use_www = _is_bare_domain(self.domain) if self.www is None else self.www
        if use_www:
            names.insert(0, f"www.{self.domain}")
        names += [f"www.{alias}" for alias in self.aliases if _is_bare_domain(alias)]
        return names


@dataclass(frozen=True)
class Config:
    domain: str
    mail_from: str | None = None
    sites: tuple[Site, ...] = field(default_factory=tuple)
    modules: tuple[str, ...] = ()

    def hostnames(self) -> list[str]:
        return [n for s in self.sites for n in (s.domain, *s.redirects)]

    @property
    def short_name(self) -> str:
        return self.domain.split(".")[0]


def _is_bare_domain(name: str) -> bool:
    # A guess: example.co.uk needs an explicit `www = true`.
    return name.count(".") == 1


def _check_hostname(name: object, what: str) -> str:
    if not isinstance(name, str) or not HOSTNAME.fullmatch(name):
        raise KitError(f"{what} {name!r} isn't a valid lowercase hostname.")
    return name


# ---- Config ------------------------------------------------------------------


def module_manifest(name: str) -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise KitError(f"{name!r} isn't a valid module name.")
    try:
        return tomllib.loads((ROOT / "modules" / name / "module.toml").read_text("utf-8"))
    except FileNotFoundError:
        raise KitError(f"There's no module called {name}.") from None


def _has_containers(name: str) -> bool:
    return (ROOT / "modules" / name / "compose.yml").exists()


def service_hostnames(primary: str) -> set[str]:
    names = {f"app.{primary}"}
    for path in (ROOT / "modules").glob("*/module.toml"):
        subdomain = tomllib.loads(path.read_text("utf-8")).get("subdomain")
        if subdomain:
            names.add(f"{subdomain}.{primary}")
    return names


def parse_config(text: str) -> Config:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise KitError(f"The config file has a syntax error: {error}") from error

    primary = raw.get("association", {}).get("domain")
    if primary is None:
        raise KitError("The config file needs a domain under [association].")
    primary = _check_hostname(primary, "Primary domain")

    sites = []
    for entry in raw.get("website", {}).get("sites", []):
        unknown = set(entry) - SITE_FIELDS
        if unknown:
            raise KitError(f"Unknown site settings: {', '.join(sorted(unknown))}.")
        domain = _check_hostname(entry.get("domain"), "Site domain")
        if domain.startswith("www."):
            raise KitError(f"Use the bare domain, not {domain}: www is handled automatically.")
        aliases = tuple(_check_hostname(a, "Alias") for a in entry.get("aliases", []))
        spa, www = entry.get("spa", True), entry.get("www")
        if not isinstance(spa, bool) or not isinstance(www, (bool, type(None))):
            raise KitError(f"On {domain}, spa and www must be true or false.")
        sites.append(Site(domain, aliases, spa, www))

    mail_from = raw.get("mail", {}).get("from")
    if mail_from is not None and not EMAIL.fullmatch(str(mail_from)):
        raise KitError(f"The mail address {mail_from!r} isn't valid.")

    modules = raw.get("modules", {})
    for name, enabled in modules.items():
        if not isinstance(enabled, bool):
            raise KitError(f"Under [modules], {name} must be true or false.")
        if enabled:
            module_manifest(name)  # refuses one that doesn't exist

    config = Config(primary, mail_from, tuple(sites), tuple(n for n, enabled in modules.items() if enabled))

    seen = set()
    services = service_hostnames(primary)
    for name in config.hostnames():
        if name in seen:
            raise KitError(f"{name} is used more than once.")
        if name in services:
            raise KitError(f"{name} is reserved for a service.")
        seen.add(name)
    return config


def load_config(paths: Paths) -> Config:
    try:
        return parse_config(paths.config.read_text("utf-8"))
    except FileNotFoundError:
        raise KitError(f"No config file at {paths.config}. Run the setup first.") from None


def _write_atomic(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        tmp.write(text)
    os.chmod(tmp.name, mode)
    os.replace(tmp.name, path)


def add_site_block(text: str, site: Site) -> str:
    lines = [SITE_HEADER, f'domain  = "{site.domain}"']
    if site.aliases:
        quoted = ", ".join(f'"{alias}"' for alias in site.aliases)
        lines.append(f"aliases = [{quoted}]")
    if not site.spa:
        lines.append("spa     = false")
    if site.www is not None:
        lines.append(f"www     = {str(site.www).lower()}")
    return text.rstrip("\n") + "\n\n" + "\n".join(lines) + "\n"


def remove_site_block(text: str, domain: str) -> str:
    """Remove one site's block as text, so comments elsewhere survive."""
    lines = text.splitlines(keepends=True)
    domain_line = re.compile(rf'\s*domain\s*=\s*"{re.escape(domain)}"\s*(#.*)?$')
    i = 0
    while i < len(lines):
        if lines[i].strip() == SITE_HEADER:
            end = i + 1
            while end < len(lines) and not lines[end].lstrip().startswith("["):
                end += 1
            if any(domain_line.match(line.rstrip("\n")) for line in lines[i:end]):
                result = "".join(lines[:i] + lines[end:])
                return result.rstrip("\n") + "\n"
            i = end
        else:
            i += 1
    raise KitError(f"No site called {domain}.")


def set_module(text: str, name: str, enabled: bool) -> str:
    """Switch a module as text, so comments elsewhere survive."""
    line = f"{name} = {str(enabled).lower()}\n"
    lines = text.splitlines(keepends=True)
    start = next((i for i, current in enumerate(lines) if current.strip() == "[modules]"), None)
    if start is None:
        return text.rstrip("\n") + f"\n\n[modules]\n{line}"
    for i in range(start + 1, len(lines)):
        if lines[i].lstrip().startswith("["):
            break
        if re.match(rf"\s*{re.escape(name)}\s*=", lines[i]):
            lines[i] = line
            return "".join(lines)
    lines.insert(start + 1, line)
    return "".join(lines)


def init_config(paths: Paths, domain: str) -> None:
    """Create the first config. The setup wizard will do this through a browser."""
    if paths.config.exists():
        raise KitError(f"{paths.config} already exists.")
    text = f'[association]\ndomain = "{_check_hostname(domain, "Primary domain")}"\n'
    parse_config(text)
    _write_atomic(paths.config, text, 0o600)


# ---- Caddy -------------------------------------------------------------------


def render_caddyfile(config: Config) -> str:
    parts = []
    if config.mail_from:
        parts.append(f"{{\n\temail {config.mail_from}\n}}")
    parts.append((ROOT / "modules" / "website" / "site.caddy").read_text("utf-8").strip())
    # Not website_defaults: its caching headers are for public pages, and these
    # are logged-in ones. Django sets its own security headers.
    parts.append(
        f"app.{config.domain} {{\n"
        "\tencode zstd gzip\n"
        "\theader -Server\n"
        "\theader -Via\n"
        "\treverse_proxy core:8000\n"
        "}"
    )
    for name in config.modules:
        subdomain = module_manifest(name).get("subdomain")
        if subdomain:
            routing = (ROOT / "modules" / name / "site.caddy").read_text("utf-8").strip()
            parts.append(routing.replace("__HOST__", f"{subdomain}.{config.domain}").replace("__SHORT_NAME__", config.short_name))
    for site in config.sites:
        fallback = "\ttry_files {path} {path}/index.html /index.html\n" if site.spa else ""
        parts.append(
            f"{site.domain} {{\n"
            "\timport website_defaults\n"
            f"\troot * {CADDY_ROOT}/{site.domain}/current\n"
            f"{fallback}"
            "\tfile_server\n"
            "}"
        )
        if site.redirects:
            parts.append(
                f"{', '.join(site.redirects)} {{\n"
                f"\tredir https://{site.domain}{{uri}} permanent\n"
                "}"
            )
    return "\n\n".join(parts) + "\n"


def _compose(*args: str) -> list[str]:
    return ["docker", "compose", "--project-directory", str(ROOT), *args]


def _module_compose(name: str, *args: str) -> list[str]:
    """Each module runs from its own file, in the same Compose project as core, so
    Caddy can reach it over the project's network. Only the running modules' files
    are ever needed, and nothing else has to know which those are."""
    file = ROOT / "modules" / name / "compose.yml"
    return ["docker", "compose", "--project-name", PROJECT, "--project-directory", str(ROOT), "--file", str(file), *args]


# What a module needs beyond starting its containers. Everything here has to be safe
# to repeat, as running `kit enable` again is how someone finishes after a failure.
PREPARE: dict = {}  # folders and secrets, before its containers start
SETUP: dict = {}  # connecting it to core, once its containers are up


def _new_signing_key() -> str:
    """The login service signs what it tells other tools with this."""
    command = ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048"]
    pem = subprocess.run(command, check=True, capture_output=True).stdout
    return base64.b64encode(pem).decode()  # on one line, for the settings file


def _read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return dict(line.split("=", 1) for line in path.read_text("utf-8").splitlines() if "=" in line)


def prepare_core(paths: Paths, config: Config) -> None:
    """Give core its folder and settings. The secrets are made once and then kept:
    a new secret key would log everyone out and cancel every password link, and
    a new signing key would make every tool stop trusting the login service."""
    current = _read_env(paths.core_env)
    key = current.get("DJANGO_SECRET_KEY") or secrets.token_urlsafe(50)
    signing_key = current.get("OIDC_SIGNING_KEY") or _new_signing_key()
    text = f"SALLSKAP_DOMAIN={config.domain}\nDJANGO_SECRET_KEY={key}\nOIDC_SIGNING_KEY={signing_key}\n"
    if not paths.core_env.exists() or paths.core_env.read_text("utf-8") != text:
        _write_atomic(paths.core_env, text, 0o600)

    paths.core_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.geteuid() == 0:
        os.chown(paths.core_dir, CORE_UID, CORE_UID)


def apply_web(paths: Paths) -> None:
    """Start Caddy and switch it to the current config. The config is validated
    inside its container first, so a bad one changes nothing. Core isn't involved,
    so the public sites don't depend on it."""
    config = load_config(paths)
    prepare_core(paths, config)  # Compose reads core's settings file for every command
    text = render_caddyfile(config)
    start = _compose("up", "-d", "caddy")
    if not paths.caddyfile.exists():
        _write_atomic(paths.caddyfile, text)
        subprocess.run(start, check=True)
        return

    subprocess.run(start, check=True)
    candidate = paths.caddyfile.with_name("Caddyfile.new")
    _write_atomic(candidate, text)
    in_container = _compose("exec", "-T", "caddy", "caddy")
    check = subprocess.run(
        [*in_container, "validate", "--adapter", "caddyfile", "--config", "/etc/caddy/Caddyfile.new"],
        capture_output=True,
        text=True,
    )
    if check.returncode != 0:
        candidate.unlink()
        raise KitError(f"Caddy rejected the new config, so nothing changed:\n{check.stderr}")
    os.replace(candidate, paths.caddyfile)
    subprocess.run(
        [*in_container, "reload", "--adapter", "caddyfile", "--config", "/etc/caddy/Caddyfile"],
        check=True,
    )


def apply(paths: Paths, build: bool = False) -> None:
    """Start everything. Caddy goes first and modules last, so a problem with one can't
    hold up the public sites or logins. With --wait, core has finished migrating and
    each module is healthy when this returns."""
    apply_web(paths)
    subprocess.run(_compose("up", "-d", "--wait", *(["--build"] if build else []), "core"), check=True)
    config = load_config(paths)
    for name in config.modules:
        if _has_containers(name):
            if name in PREPARE:
                PREPARE[name](paths, config)
            subprocess.run(_module_compose(name, "up", "-d", "--wait"), check=True)


def enable_module(paths: Paths, name: str) -> None:
    """Its route goes live before its setup has finished, so a module has to be
    safe to reach from its first start."""
    module_manifest(name)
    if not _has_containers(name):
        raise KitError(f"{name} has nothing to start, so it can't be switched on or off.")
    text = paths.config.read_text("utf-8")
    new_text = set_module(text, name, True)
    parse_config(new_text)
    if new_text != text:
        _write_atomic(paths.config, new_text, 0o600)
    apply(paths)
    if name in SETUP:
        SETUP[name](paths, load_config(paths))


def disable_module(paths: Paths, name: str) -> None:
    """Stop it and keep its data: only an explicit `kit remove` may delete that. It's
    only marked as off once it has stopped, so running this again can always finish."""
    module_manifest(name)
    if not _has_containers(name):
        raise KitError(f"{name} has nothing to start, so it can't be switched on or off.")
    if name in load_config(paths).modules:
        subprocess.run(_module_compose(name, "rm", "--stop", "--force"), check=True)
        text = set_module(paths.config.read_text("utf-8"), name, False)
        parse_config(text)
        _write_atomic(paths.config, text, 0o600)
    else:
        print(f"{name} isn't enabled.")
    apply_web(paths)


# ---- Core: accounts ----------------------------------------------------------


def _manage(*args: str) -> int:
    """Run a Django command in the running core container. It prints for itself."""
    return subprocess.run(_compose("exec", "-T", "core", "python", "manage.py", *args)).returncode


def _check_email(address: str) -> str:
    """Django checks the address properly. This only stops it being read as an option."""
    address = address.strip().lower()
    if address.startswith("-") or "@" not in address:
        raise KitError(f"{address!r} isn't a valid email address.")
    return address


def _check_tool_name(name: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise KitError(f"{name!r} isn't a valid tool name. Use lowercase letters, numbers and dashes.")
    return name


def _check_callback(address: str) -> str:
    """Django checks the address properly. This only stops it being read as an option."""
    if not address.startswith("https://"):
        raise KitError(f"{address!r} must be a full https address.")
    return address


# ---- The git module ----------------------------------------------------------


FORGEJO_TOOL = "forgejo"  # its name in core's login service
FORGEJO_SERVICE_USER = "sallskap-admin"  # owns the organisation. Nobody can log in as it
FORGEJO_UNITS = ["repo.code", "repo.issues", "repo.pulls", "repo.releases", "repo.wiki", "repo.projects"]
FORGEJO_TEAMS = (
    {"name": "developers", "permission": "write", "can_create_org_repo": True},
    {"name": "board", "permission": "read", "can_create_org_repo": False},
)
NO_LIMIT = 1_000_000  # repos the organisation may have: 0 would fall back on the limit for people


def prepare_git(paths: Paths, config: Config) -> None:
    """Forgejo's secrets are made once and then kept: a new key would make its stored
    login secret and 2FA records unreadable."""
    current = _read_env(paths.git_env)
    settings = {
        "FORGEJO__server__DOMAIN": f"git.{config.domain}",
        "FORGEJO__server__ROOT_URL": f"https://git.{config.domain}/",
        "FORGEJO__security__SECRET_KEY": current.get("FORGEJO__security__SECRET_KEY") or secrets.token_urlsafe(48),
        "FORGEJO__security__INTERNAL_TOKEN": current.get("FORGEJO__security__INTERNAL_TOKEN") or secrets.token_urlsafe(48),
    }
    text = "".join(f"{name}={value}\n" for name, value in settings.items())
    if not paths.git_env.exists() or paths.git_env.read_text("utf-8") != text:
        _write_atomic(paths.git_env, text, 0o600)
    paths.git_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.geteuid() == 0:
        os.chown(paths.git_dir, FORGEJO_UID, FORGEJO_UID)


def _core_output(*args: str) -> str:
    """For commands whose output kit needs. A failure doesn't show the command line,
    as it can carry a secret."""
    result = subprocess.run(_compose("exec", "-T", "core", "python", "manage.py", *args), capture_output=True, text=True)
    if result.returncode:
        raise KitError(f"Core refused `{' '.join(args[:2])}`: {result.stderr.strip()}")
    return result.stdout


def _forgejo_output(*args: str) -> str:
    command = _module_compose("git", "exec", "-T", "forgejo", "forgejo", *args)
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise KitError(f"Forgejo refused `{' '.join(args[:3])}`: {(result.stderr or result.stdout).strip()}")
    return result.stdout


def _forgejo_api(token: str, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    """Forgejo publishes no ports, so this goes through curl inside its container. The
    token reaches curl on stdin: a command line can be read by anyone on the server."""
    settings = [f'header = "Authorization: token {token}"', 'header = "Content-Type: application/json"']
    if body is not None:
        data = json.dumps(body).replace("\\", "\\\\").replace('"', '\\"')
        settings.append(f'data-binary = "{data}"')
    command = _module_compose(
        "git", "exec", "-T", "forgejo", "curl", "-sS", "--request", method, "--write-out", "\n%{http_code}",
        "--config", "-", f"http://127.0.0.1:3000/api/v1{path}",
    )  # fmt: skip
    result = subprocess.run(command, capture_output=True, text=True, input="\n".join(settings) + "\n")
    if result.returncode:
        raise KitError(f"Couldn't reach Forgejo's API: {result.stderr.strip()}")
    content, _, status = result.stdout.rpartition("\n")
    try:
        return int(status), json.loads(content) if content else None
    except ValueError:
        raise KitError(f"Forgejo's API gave an answer kit can't read: {result.stdout[:200]}") from None


def _expect(answer: tuple[int, object], *statuses: int) -> object:
    if answer[0] not in statuses:
        raise KitError(f"Forgejo's API answered {answer[0]}: {answer[1]}")
    return answer[1]


def _create_org_and_teams(config: Config, org: str) -> None:
    """Forgejo silently skips a team in its map if it doesn't exist, and there's no
    command for making one, so this uses its API. Forgejo 15 has no way of deleting the
    token afterwards with password login off, so it stays in its database, where only
    a hash is stored, and its value is thrown away when this returns. So this only
    runs until the login is connected."""
    if not re.search(rf"^\d+\s+{FORGEJO_SERVICE_USER}\s", _forgejo_output("admin", "user", "list", "--admin"), re.M):
        _forgejo_output(
            "admin", "user", "create", "--username", FORGEJO_SERVICE_USER, "--email", f"{FORGEJO_SERVICE_USER}@git.{config.domain}",
            "--admin", "--random-password", "--must-change-password=false",
        )  # fmt: skip
    token = _forgejo_output(
        "admin", "user", "generate-access-token", "--username", FORGEJO_SERVICE_USER,
        "--token-name", f"kit-{secrets.token_hex(4)}", "--scopes", "write:admin,write:organization", "--raw",
    ).strip()  # fmt: skip
    if _forgejo_api(token, "GET", f"/orgs/{org}")[0] == 404:
        _expect(_forgejo_api(token, "POST", "/orgs", {"username": org, "visibility": "public"}), 201)
    # The limit on new repos is for people, but organisations are held to it too.
    _expect(
        _forgejo_api(token, "PATCH", f"/admin/users/{org}", {"login_name": org, "source_id": 0, "max_repo_creation": NO_LIMIT}),
        200,
    )
    existing = {team["name"] for team in _expect(_forgejo_api(token, "GET", f"/orgs/{org}/teams"), 200)}
    for team in FORGEJO_TEAMS:
        if team["name"] not in existing:
            body = {**team, "includes_all_repositories": True, "units": FORGEJO_UNITS}
            _expect(_forgejo_api(token, "POST", f"/orgs/{org}/teams", body), 201)


def _connect_login(config: Config, org: str, source, registered) -> None:
    """A tool's secret is only shown when it's made, so if one side is missing, both
    get a new one."""
    if registered:
        _core_output("tool", "remove", FORGEJO_TOOL)
    output = _core_output("tool", "add", FORGEJO_TOOL, f"https://git.{config.domain}/user/oauth2/{org}/callback")
    fields = dict(re.findall(r"^\s+(Client ID|Secret):\s+(\S+)$", output, re.M))
    if len(fields) != 2:
        raise KitError("Core registered Forgejo, but kit couldn't read its settings. Run this again.")
    team_map = {
        "operator": {org: ["Owners"]},
        "developer": {org: ["developers"]},
        "board": {org: ["board"]},
    }
    settings = [
        "--name", org,
        "--provider", "openidConnect",
        "--key", fields["Client ID"],
        "--secret", fields["Secret"],
        "--auto-discover-url", f"https://app.{config.domain}/.well-known/openid-configuration",
        "--scopes", "email", "--scopes", "profile", "--scopes", "groups",
        "--group-claim-name", "groups",
        "--admin-group", "operator",
        "--group-team-map", json.dumps(team_map),
        "--group-team-map-removal",
        "--skip-local-2fa",  # core has already asked for it
    ]  # fmt: skip
    try:
        if source:
            _forgejo_output("admin", "auth", "update-oauth", "--id", source.group(1), *settings)
        else:
            _forgejo_output("admin", "auth", "add-oauth", *settings)
        # It reads its login sources when it starts.
        subprocess.run(_module_compose("git", "restart", "forgejo"), check=True, capture_output=True)
        subprocess.run(_module_compose("git", "up", "-d", "--wait"), check=True, capture_output=True)
    except (KitError, subprocess.CalledProcessError):
        # Core has a secret that Forgejo may not. Without this, the next run would find
        # both sides present and call it finished, with every sign-in failing.
        _core_output("tool", "remove", FORGEJO_TOOL)
        raise


def setup_git(paths: Paths, config: Config) -> None:
    """The login is connected last, so finding it means everything before it finished,
    and running this again doesn't make another token."""
    org = config.short_name
    source = re.search(rf"^(\d+)\s+{re.escape(org)}\s", _forgejo_output("admin", "auth", "list"), re.M)
    registered = re.search(rf"^{FORGEJO_TOOL}\s", _core_output("tool", "list"), re.M)
    if source and registered:
        return
    if not source:
        _create_org_and_teams(config, org)
    _connect_login(config, org, source, registered)


PREPARE["git"] = prepare_git
SETUP["git"] = setup_git


# ---- Sites: config and credentials -------------------------------------------


def _key_marker(domain: str) -> str:
    return f"sallskap-site:{domain}"


def _chown_to_deploy(path: Path) -> None:
    if os.geteuid() == 0:
        shutil.chown(path, DEPLOY_USER, DEPLOY_USER)


def add_site(paths: Paths, site: Site) -> None:
    text = paths.config.read_text("utf-8")
    new_text = add_site_block(text, site)
    parse_config(new_text)  # refuses collisions before anything is written
    site_dir = paths.site_dir(site.domain)
    site_dir.mkdir(parents=True, exist_ok=True)
    _chown_to_deploy(site_dir)
    _write_atomic(paths.config, new_text, 0o600)


def remove_site(paths: Paths, domain: str) -> None:
    text = remove_site_block(paths.config.read_text("utf-8"), domain)
    parse_config(text)
    _write_atomic(paths.config, text, 0o600)
    revoke_deploy_key(paths, domain)


def create_deploy_key(paths: Paths, domain: str) -> str:
    """Add a key that can only publish to this site. Returns the private key,
    which is shown once and never stored on the server."""
    _check_hostname(domain, "Site domain")
    with tempfile.TemporaryDirectory() as tmp:
        key = Path(tmp) / "key"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", _key_marker(domain), "-f", str(key)],
            check=True,
        )
        private, public = key.read_text(), key.with_suffix(".pub").read_text().strip()

    revoke_deploy_key(paths, domain)
    keys = paths.authorized_keys
    current = keys.read_text() if keys.exists() else ""
    line = f'restrict,command="{KIT_COMMAND} site receive {domain}" {public}\n'
    keys.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_atomic(keys, current + line, 0o600)
    _chown_to_deploy(keys.parent)
    _chown_to_deploy(keys)
    return private


def revoke_deploy_key(paths: Paths, domain: str) -> None:
    keys = paths.authorized_keys
    if not keys.exists():
        return
    marker = _key_marker(domain)
    kept = [line for line in keys.read_text().splitlines(keepends=True) if not line.rstrip().endswith(marker)]
    _write_atomic(keys, "".join(kept), 0o600)
    _chown_to_deploy(keys)


# ---- Sites: releases ---------------------------------------------------------


def _releases(site_dir: Path) -> list[Path]:
    root = site_dir / "releases"
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and not p.name.endswith(".tmp"))


def _switch(site_dir: Path, release: Path) -> None:
    link = site_dir / "current.new"
    link.unlink(missing_ok=True)
    link.symlink_to(release.relative_to(site_dir))
    os.replace(link, site_dir / "current")  # atomic: visitors see old or new, never neither


def _extract_upload(stream, target: Path) -> None:
    """Unpack a tar stream, allowing only plain files and folders inside target.
    Uploads come from CI, so a stolen deploy key must not reach outside the site."""
    total = files = 0
    with tarfile.open(fileobj=stream, mode="r|*") as tar:
        for member in tar:
            parts = PurePosixPath(member.name).parts
            if member.name.startswith("/") or ".." in parts:
                raise KitError(f"Refused unsafe path in upload: {member.name}")
            if not (member.isfile() or member.isdir()):
                raise KitError(f"Refused link or special file in upload: {member.name}")
            files += 1
            total += member.size
            if files > MAX_UPLOAD_FILES or total > MAX_UPLOAD_BYTES:
                raise KitError("The upload is too large.")
            destination = target.joinpath(*parts)  # the archive root "." has no parts
            if member.isdir():
                destination.mkdir(mode=0o755, parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            with tar.extractfile(member) as source, destination.open("wb") as out:
                shutil.copyfileobj(source, out)
            destination.chmod(0o644)


def receive(paths: Paths, domain: str, stream) -> Path:
    """Publish a tar stream as a new release. `current` only moves on success."""
    _check_hostname(domain, "Site domain")
    site_dir = paths.site_dir(domain)
    if not site_dir.is_dir():
        raise KitError(f"No site called {domain}.")

    with (site_dir / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        releases = site_dir / "releases"
        releases.mkdir(mode=0o755, exist_ok=True)
        seconds, nanos = divmod(time.time_ns(), 10**9)
        name = f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime(seconds))}-{nanos:09d}"
        temporary = releases / f"{name}.tmp"
        temporary.mkdir(mode=0o755)
        try:
            try:
                _extract_upload(stream, temporary)
            except tarfile.TarError as error:
                raise KitError(f"The upload isn't a valid tar archive: {error}") from error
            if not (temporary / "index.html").is_file():
                raise KitError("The upload has no index.html at its top level.")
            release = releases / name
            temporary.rename(release)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        _switch(site_dir, release)
        _prune(site_dir)
    return release


def _prune(site_dir: Path) -> None:
    current = (site_dir / "current").resolve()
    for old in _releases(site_dir)[:-RELEASES_KEPT]:
        if old.resolve() != current:
            shutil.rmtree(old)


def rollback(paths: Paths, domain: str) -> Path:
    _check_hostname(domain, "Site domain")
    site_dir = paths.site_dir(domain)
    with (site_dir / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        releases = _releases(site_dir)
        current = (site_dir / "current").resolve()
        earlier = [r for r in releases if r.resolve() != current and r.name < current.name]
        if not earlier:
            raise KitError(f"{domain} has no earlier release to go back to.")
        _switch(site_dir, earlier[-1])
        return earlier[-1]


# ---- Status ------------------------------------------------------------------


def _resolve_ipv4(name: str) -> set[str]:
    try:
        return {info[4][0] for info in socket.getaddrinfo(name, 443, socket.AF_INET)}
    except socket.gaierror:
        return set()


def _check_dns(name: str, server_ip: str) -> int:
    """Print whether a hostname points here, and return the number of problems."""
    addresses = _resolve_ipv4(name)
    if server_ip in addresses and len(addresses) == 1:
        print(f"  ok   {name}")
        return 0
    found = ", ".join(sorted(addresses)) or "nothing"
    print(f"  DNS  {name} points at {found}. Add this record: {name} A {server_ip}")
    return 1


def status(paths: Paths) -> int:
    config = load_config(paths)
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=10) as response:
            server_ip = response.read().decode().strip()
    except OSError as error:
        raise KitError(f"Couldn't look up this server's public address: {error}") from error

    print(f"Primary domain: {config.domain}\nServer address: {server_ip}\n")
    print("Login and admin page:")
    problems = _check_dns(f"app.{config.domain}", server_ip)
    for name in config.modules:
        manifest = module_manifest(name)
        if manifest.get("subdomain"):
            print(f"{manifest['description']}:")
            problems += _check_dns(f"{manifest['subdomain']}.{config.domain}", server_ip)
    for site in config.sites:
        current = paths.site_dir(site.domain) / "current"
        published = current.resolve().name if current.exists() else "nothing published yet"
        print(f"{site.domain}: {published}")
        for name in (site.domain, *site.redirects):
            problems += _check_dns(name, server_ip)
    return 1 if problems else 0


# ---- Command line ------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kit", description="Operate a Sällskap installation.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create the first config").add_argument("domain", help="the primary domain")
    apply_command = commands.add_parser("apply", help="start the containers and switch to the current web server config")
    apply_command.add_argument("--build", action="store_true", help="rebuild core first, after its code has changed")
    admin = commands.add_parser("admin", help="create the first admin, or restore someone's Operator access")
    admin.add_argument("email")
    admin.add_argument("--reset-2fa", action="store_true", help="also remove their 2FA, if they've lost their phone and codes")
    commands.add_parser("link", help="create a one-time link for someone to choose a password").add_argument("email")
    commands.add_parser("status", help="show sites, published releases and DNS problems")
    commands.add_parser("enable", help="switch on a module, or finish switching it on").add_argument("module")
    commands.add_parser("disable", help="stop a module and keep its data").add_argument("module")

    tool = commands.add_parser("tool", help="manage the tools that can use the login service")
    tool = tool.add_subparsers(dest="tool_action", required=True)
    tool_add = tool.add_parser("add", help="register a tool, and show its settings and secret once")
    tool_add.add_argument("name")
    tool_add.add_argument("callback", help="the exact https address the tool wants people sent back to")
    tool.add_parser("list", help="show the registered tools")
    tool.add_parser("remove", help="remove a tool and end its access").add_argument("name")

    site = commands.add_parser("site", help="manage public sites").add_subparsers(dest="action", required=True)
    add = site.add_parser("add", help="add a site and create its deploy key")
    add.add_argument("domain")
    add.add_argument("--alias", action="append", default=[], help="a hostname that redirects here (repeatable)")
    add.add_argument("--no-spa", action="store_true", help="don't send unknown paths to index.html")
    add.add_argument("--www", action=argparse.BooleanOptionalAction, default=None, help="redirect www.<domain> here")
    site.add_parser("remove", help="remove a site; its files stay on disk").add_argument("domain")
    site.add_parser("rollback", help="switch a site back to its previous release").add_argument("domain")
    site.add_parser("key", help="replace a site's deploy key").add_argument("domain")
    site.add_parser("receive", help="used by deploy keys: publish a tar from stdin").add_argument("domain")
    return parser


def _print_deploy_key(domain: str, private: str) -> None:
    print(f"\nDeploy key for {domain}. Store it as a CI secret now; it isn't kept on this server.\n")
    print(private)
    print(f"Publish from CI with:\n  tar -C dist -czf - . | ssh -o IdentitiesOnly=yes -i <key> {DEPLOY_USER}@<server>")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = Paths()
    # Each module has its own Compose file, so the others always look like leftovers.
    os.environ["COMPOSE_IGNORE_ORPHANS"] = "true"
    try:
        if args.command == "init":
            init_config(paths, args.domain)
            apply(paths)
            print(f"Created {paths.config}.")
            print("Create the first admin with: kit admin you@example.org")
            print(f"Add a public site with: kit site add {args.domain}\n")
            status(paths)
        elif args.command == "apply":
            apply(paths, build=args.build)
        elif args.command == "admin":
            return _manage("create_admin", _check_email(args.email), *(["--reset-2fa"] if args.reset_2fa else []))
        elif args.command == "link":
            return _manage("password_link", _check_email(args.email))
        elif args.command == "tool":
            if args.tool_action == "add":
                return _manage("tool", "add", _check_tool_name(args.name), _check_callback(args.callback))
            if args.tool_action == "remove":
                return _manage("tool", "remove", _check_tool_name(args.name))
            return _manage("tool", "list")
        elif args.command == "status":
            return status(paths)
        elif args.command == "enable":
            enable_module(paths, args.module)
            print(f"Enabled {args.module}.\n")
            status(paths)
        elif args.command == "disable":
            disable_module(paths, args.module)
        elif args.action == "add":
            site = Site(args.domain, tuple(args.alias), not args.no_spa, args.www)
            add_site(paths, site)
            # Shown first: it's kept nowhere else, so a later failure mustn't lose it.
            _print_deploy_key(site.domain, create_deploy_key(paths, site.domain))
            apply_web(paths)
            status(paths)
        elif args.action == "remove":
            remove_site(paths, args.domain)
            apply_web(paths)
            print(f"Removed {args.domain}. Its files remain in {paths.site_dir(args.domain)}.")
        elif args.action == "rollback":
            print(f"{args.domain} now serves {rollback(paths, args.domain).name}.")
        elif args.action == "key":
            _print_deploy_key(args.domain, create_deploy_key(paths, args.domain))
        elif args.action == "receive":
            print(f"Published {receive(paths, args.domain, sys.stdin.buffer).name}.")
    except KitError as error:
        print(f"kit: {error}", file=sys.stderr)
        return 1
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"kit: a system command failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
