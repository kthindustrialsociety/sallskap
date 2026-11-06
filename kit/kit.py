#!/usr/bin/env python3
"""kit: operate a Sällskap installation. See docs/spec.md §7.5 and §10.3.

Standard library only, so it runs on a fresh server before anything else is
installed. Needs Python 3.11 or newer.
"""

import argparse
import base64
import contextlib
import fcntl
import getpass
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
from datetime import datetime, timezone
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
VAULT_UID = 10002  # the user Vaultwarden runs as: see modules/vault/compose.yml

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
    vault_dir: Path = Path(os.environ.get("SALLSKAP_VAULT_DIR", "/srv/vault"))
    caddy_dir: Path = Path(os.environ.get("SALLSKAP_CADDY_DIR", "/srv/caddy"))
    backup_dir: Path = Path(os.environ.get("SALLSKAP_BACKUP_DIR", "/srv/backup"))

    @property
    def config(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def core_env(self) -> Path:
        return self.config_dir / "core.env"

    @property
    def sync_env(self) -> Path:
        return self.config_dir / "sync.env"

    @property
    def git_env(self) -> Path:
        return self.config_dir / "git.env"

    @property
    def vault_env(self) -> Path:
        return self.config_dir / "vault.env"

    @property
    def backup_env(self) -> Path:
        return self.config_dir / "backup.env"

    @property
    def repo(self) -> Path:
        """The restic repository. Its folder also holds kit's own lock and the note of
        when a copy was last taken off the server, which an export mustn't include."""
        return self.backup_dir / "repo"

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
    backup_target: str | None = None

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

    target = raw.get("backup", {}).get("target")
    if target is not None and (not isinstance(target, str) or not target.strip()):
        raise KitError('Under [backup], target must be a restic repository, such as "sftp:backups@host:/srv/sallskap".')

    config = Config(
        primary,
        mail_from,
        tuple(sites),
        tuple(n for n, enabled in modules.items() if enabled),
        target.strip() if isinstance(target, str) else None,
    )

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


CORE_IMAGE = "sallskap-core"


def core_image(version: str | None = None) -> str:
    """Core's image is named after the version it was built from, so the one before an
    update is still there to go back to. A tag can't hold a +, so 0.7.0+abc1234 becomes
    0.7.0-abc1234."""
    return f"{CORE_IMAGE}:{(version or running_version()).replace('+', '-')}"


def _compose(*args: str) -> list[str]:
    # compose.yml names core's image from this. Set each time, as the code under ROOT
    # changes during an update, and the process that finishes it inherits this one's.
    os.environ["SALLSKAP_CORE_TAG"] = core_image().split(":", 1)[1]
    return ["docker", "compose", "--project-directory", str(ROOT), *args]


def _have_image(name: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", name], capture_output=True).returncode == 0


def _module_compose(name: str, *args: str) -> list[str]:
    """Each module runs from its own file, in the same Compose project as core, so
    Caddy can reach it over the project's network. Only the running modules' files
    are ever needed, and nothing else has to know which those are."""
    file = ROOT / "modules" / name / "compose.yml"
    return ["docker", "compose", "--project-name", PROJECT, "--project-directory", str(ROOT), "--file", str(file), *args]


# What a module needs beyond starting its containers. Everything here has to be safe
# to repeat, as running `kit enable` again is how someone finishes after a failure.
PREPARE: dict = {}  # folders and secrets, before its containers start
AFTER_START: dict = {}  # what an upgrade needs too, as `kit apply` doesn't run SETUP
SETUP: dict = {}  # connecting it to core, once its containers are up
STOP: dict = {}  # anything else of its that's running, which stopping it wouldn't reach


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
    _write_sync_env(paths, config)


def _write_sync_env(paths: Paths, config: Config, **tokens: str) -> None:
    """For the worker that tells tools about changes to people's access: see
    core/accounts/sync.py. Its tokens are only here, so core's web process, which faces
    the internet, never has them. Nor does it get core's secrets, as it signs nothing."""
    current = _read_env(paths.sync_env)
    settings = {
        **current,
        "SALLSKAP_DOMAIN": config.domain,
        "DJANGO_SECRET_KEY": current.get("DJANGO_SECRET_KEY") or secrets.token_urlsafe(50),  # Django insists on one
        "SYNC_MODULES": ",".join(name for name in config.modules if (ROOT / "modules" / name / "connector.py").exists()),
        **tokens,
    }
    text = "".join(f"{name}={value}\n" for name, value in settings.items())
    if not paths.sync_env.exists() or paths.sync_env.read_text("utf-8") != text:
        _write_atomic(paths.sync_env, text, 0o600)


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
    each module is healthy when this returns. The worker goes after the modules, as
    preparing one can give it a new token, and it starts even if one fails, as the others
    still have to hear about changes."""
    apply_web(paths)
    subprocess.run(_compose("up", "-d", "--wait", *(["--build"] if build else []), "core"), check=True)
    config = load_config(paths)
    try:
        for name in config.modules:
            if _has_containers(name):
                if name in PREPARE:
                    PREPARE[name](paths, config)
                subprocess.run(_module_compose(name, "up", "-d", "--wait"), check=True)
                if name in AFTER_START:
                    AFTER_START[name](paths, config)
    finally:
        subprocess.run(_compose("up", "-d", "--wait", "sync"), check=True)


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
        if name in STOP:
            STOP[name](paths)
        subprocess.run(_module_compose(name, "rm", "--stop", "--force"), check=True)
        text = set_module(paths.config.read_text("utf-8"), name, False)
        parse_config(text)
        _write_atomic(paths.config, text, 0o600)
    else:
        print(f"{name} isn't enabled.")
    apply_web(paths)
    subprocess.run(_compose("up", "-d", "sync"), check=True)  # so it stops asking that module


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
    # Its logs go to the console, but its own check fails if the folder its config
    # names is missing, which would make every `kit update` look broken.
    log_dir = paths.git_dir / "data" / "log"
    log_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    if os.geteuid() == 0:
        for folder in (paths.git_dir, paths.git_dir / "data", log_dir):
            os.chown(folder, FORGEJO_UID, FORGEJO_UID)


def _core_output(*args: str) -> str:
    """For commands whose output kit needs. A failure doesn't show the command line,
    as it can carry a secret."""
    result = subprocess.run(_compose("exec", "-T", "core", "python", "manage.py", *args), capture_output=True, text=True)
    if result.returncode:
        raise KitError(f"Core refused `{' '.join(args[:2])}`: {result.stderr.strip()}")
    return result.stdout


def _core_python(code: str, stdin: str) -> str:
    """Python inside core, for what kit's standard library can't do. What it's given goes
    on stdin, as a command line can be read by anyone on the server."""
    command = _compose("exec", "-T", "core", "python", "-c", code)
    result = subprocess.run(command, capture_output=True, text=True, input=stdin)
    if result.returncode:
        raise KitError(f"Core couldn't do that: {result.stderr.strip()}")
    return result.stdout.strip()


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


def _forgejo_token(paths: Paths, config: Config) -> str:
    """The admin token the worker uses, which this setup uses too. It's made once and then
    kept, as Forgejo 15 has no way of deleting one with password login off. So a new one
    would leave the old one working, unless Forgejo no longer knows it, as after a restore
    from before it was made."""
    kept = _read_env(paths.sync_env).get("FORGEJO_ADMIN_TOKEN")
    if kept and _forgejo_api(kept, "GET", "/user")[0] != 401:
        return kept
    if not re.search(rf"^\d+\s+{FORGEJO_SERVICE_USER}\s", _forgejo_output("admin", "user", "list", "--admin"), re.M):
        _forgejo_output(
            "admin", "user", "create", "--username", FORGEJO_SERVICE_USER, "--email", f"{FORGEJO_SERVICE_USER}@git.{config.domain}",
            "--admin", "--random-password", "--must-change-password=false",
        )  # fmt: skip
    token = _forgejo_output(
        "admin", "user", "generate-access-token", "--username", FORGEJO_SERVICE_USER, "--token-name", f"kit-{secrets.token_hex(4)}",
        "--scopes", "write:admin,write:organization,write:repository,write:user", "--raw",
    ).strip()  # fmt: skip
    _write_sync_env(paths, config, FORGEJO_ADMIN_TOKEN=token)
    return token


def _create_org_and_teams(token: str, org: str) -> None:
    """Forgejo silently skips a team in its map if it doesn't exist, and there's no
    command for making one, so this uses its API."""
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
    manifest = module_manifest("git")
    team_map = {group: {org: [team]} for group, team in manifest["teams"].items()}
    settings = [
        "--name", org,
        "--provider", "openidConnect",
        "--key", fields["Client ID"],
        "--secret", fields["Secret"],
        "--auto-discover-url", f"https://app.{config.domain}/.well-known/openid-configuration",
        "--scopes", "email", "--scopes", "profile", "--scopes", "groups",
        "--group-claim-name", "groups",
        "--admin-group", manifest["admin_group"],
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
    """The login is connected last, so finding it means everything before it finished."""
    org = config.short_name
    source = re.search(rf"^(\d+)\s+{re.escape(org)}\s", _forgejo_output("admin", "auth", "list"), re.M)
    registered = re.search(rf"^{FORGEJO_TOOL}\s", _core_output("tool", "list"), re.M)
    token = _forgejo_token(paths, config)
    if not source:
        _create_org_and_teams(token, org)
    if not (source and registered):
        _connect_login(config, org, source, registered)
    subprocess.run(_compose("up", "-d", "--wait", "sync"), check=True)  # now it has the token


PREPARE["git"] = prepare_git
AFTER_START["git"] = _forgejo_token
SETUP["git"] = setup_git


# ---- The vault module --------------------------------------------------------


VAULT_TOOL = "vaultwarden"  # its name in core's login service


def _write_vault_env(paths: Paths, config: Config, admin_hash: str, client_id: str = "", secret: str = "") -> None:
    settings = {
        "DOMAIN": f"https://vault.{config.domain}",
        "SSO_AUTHORITY": f"https://app.{config.domain}",
        "SSO_CLIENT_ID": client_id,
        "SSO_CLIENT_SECRET": secret,
        "ADMIN_TOKEN": admin_hash,
    }
    _write_atomic(paths.vault_env, "".join(f"{name}={value}\n" for name, value in settings.items()), 0o600)


def _vault_admin_hash(paths: Paths, config: Config, current: dict[str, str]) -> str:
    """Vaultwarden only gets a hash of its admin token, and only the worker gets the token.
    The quotes are part of the setting, or Compose would read the $ in it as variables."""
    if current.get("ADMIN_TOKEN") and _read_env(paths.sync_env).get("VAULT_ADMIN_TOKEN"):
        return current["ADMIN_TOKEN"]
    token = secrets.token_urlsafe(48)
    hashed = _core_python("import sys; from argon2 import PasswordHasher; print(PasswordHasher().hash(sys.stdin.read()))", token)
    _write_sync_env(paths, config, VAULT_ADMIN_TOKEN=token)
    return f"'{hashed}'"


def prepare_vault(paths: Paths, config: Config) -> None:
    """Core is already up when this runs, so the login is connected before Vaultwarden
    first starts, and it's never reachable without one. A tool's secret is only shown
    when it's made, so if one side is missing, both get a new one. The old secret goes
    first, so stopping partway can only leave a side missing."""
    current = _read_env(paths.vault_env)
    admin_hash = _vault_admin_hash(paths, config, current)
    registered = re.search(rf"^{VAULT_TOOL}\s", _core_output("tool", "list"), re.M)
    if not (registered and current.get("SSO_CLIENT_SECRET")):
        _write_vault_env(paths, config, admin_hash)
        if registered:
            _core_output("tool", "remove", VAULT_TOOL)
        manifest = module_manifest("vault")
        rules = [f"--group={group}" for group in manifest["access"]] + [f"--session-hours={manifest['session_hours']}"]
        output = _core_output("tool", "add", VAULT_TOOL, f"https://vault.{config.domain}/identity/connect/oidc-signin", *rules)
        fields = dict(re.findall(r"^\s+(Client ID|Secret):\s+(\S+)$", output, re.M))
        if len(fields) != 2:
            raise KitError("Core registered Vaultwarden, but kit couldn't read its settings. Run this again.")
        current = {"SSO_CLIENT_ID": fields["Client ID"], "SSO_CLIENT_SECRET": fields["Secret"]}
    _write_vault_env(paths, config, admin_hash, current["SSO_CLIENT_ID"], current["SSO_CLIENT_SECRET"])
    paths.vault_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.geteuid() == 0:
        os.chown(paths.vault_dir, VAULT_UID, VAULT_UID)


PREPARE["vault"] = prepare_vault


# ---- Getting in while core is down -------------------------------------------


VAULT_EMERGENCY = "sallskap-vault-emergency"
VAULT_EMERGENCY_PORT = 8222  # on the operator's own computer, at the end of their tunnel
VAULT_EMERGENCY_TLS = "emergency-tls"


def _require_module(paths: Paths, name: str) -> None:
    if name not in load_config(paths).modules:
        raise KitError(f"The {name} module isn't enabled.")


def _vault_emergency_address() -> str | None:
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", VAULT_EMERGENCY],
            capture_output=True,
            text=True,
        )
    except OSError:  # no Docker yet, so nothing's running
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _make_emergency_certificate(paths: Paths) -> None:
    """In Vaultwarden's own folder, as that's what it can read. It's only good for localhost
    and for a week, and it goes when the copy stops."""
    folder = paths.vault_dir / VAULT_EMERGENCY_TLS
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(mode=0o700, parents=True)
    command = [
        "openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-days", "7",
        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
        "-keyout", str(folder / "key.pem"), "-out", str(folder / "cert.pem"),
    ]  # fmt: skip
    subprocess.run(command, check=True, capture_output=True)
    if os.geteuid() == 0:
        for path in (folder, *folder.iterdir()):
            os.chown(path, VAULT_UID, VAULT_UID)


def vault_emergency_on(paths: Paths) -> str:
    """A second Vaultwarden on the same data, with password login on and no route from
    Caddy or any published port, so only someone with SSH on the server can reach it. The
    public address stays core-only throughout, and an account turned off stays turned off.
    `compose run` gives it no alias on the network, so Caddy can't end up sending anyone
    here, and it isn't restarted. Nothing about it needs core.

    Its web page refuses any address that isn't HTTPS, even on localhost, so this has a
    certificate made on the spot. A browser warns about it, and the tunnel is what actually
    protects the traffic. Its health check shows as failing, as that uses plain HTTP."""
    _require_module(paths, "vault")
    if not _vault_emergency_address():
        _make_emergency_certificate(paths)
        settings = {
            "SSO_ENABLED": "false",
            "SSO_ONLY": "false",
            "DOMAIN": f"https://localhost:{VAULT_EMERGENCY_PORT}",
            "ROCKET_TLS": f'{{certs="/data/{VAULT_EMERGENCY_TLS}/cert.pem",key="/data/{VAULT_EMERGENCY_TLS}/key.pem"}}',
            "ADMIN_TOKEN": "",  # turns off its admin page, which nothing here needs
        }
        environment = [item for name, value in settings.items() for item in ("--env", f"{name}={value}")]
        run = _module_compose("vault", "run", "--detach", "--rm", "--no-deps", "--name", VAULT_EMERGENCY, *environment, "vaultwarden")
        subprocess.run(run, check=True, capture_output=True)
    address = _vault_emergency_address()
    if not address:
        raise KitError("Its emergency copy didn't start. See: docker logs " + VAULT_EMERGENCY)
    return address


def vault_emergency_off(paths: Paths) -> bool:
    """Whether there was one to stop: removing nothing still counts as success to Docker."""
    running = bool(_vault_emergency_address())
    if running:
        subprocess.run(["docker", "rm", "--force", VAULT_EMERGENCY], check=True, capture_output=True)
    shutil.rmtree(paths.vault_dir / VAULT_EMERGENCY_TLS, ignore_errors=True)
    return running


STOP["vault"] = vault_emergency_off


def git_emergency_token(paths: Paths, username: str) -> str:
    """On that person's own account, so it's in their name. Only a person's own settings
    page can delete one, once core is back, as Forgejo 15 has no other way with password
    login off."""
    _require_module(paths, "git")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", username):
        raise KitError(f"{username!r} isn't a valid Forgejo username.")
    return _forgejo_output(
        "admin", "user", "generate-access-token", "--username", username,
        "--token-name", f"emergency-{secrets.token_hex(4)}", "--scopes", "all", "--raw",
    ).strip()  # fmt: skip


# ---- Backups -----------------------------------------------------------------
#
# See docs/restore.md. One encrypted restic repository on the server, taken nightly
# by sallskap-backup.timer, plus `kit backup export` for a copy that leaves the
# server: by hand, or from a cron job on a machine the association trusts. The copy
# is pulled rather than pushed where possible, as then the server holds no
# credentials for it and can't be made to delete it.

BACKUP_TAG = "sallskap"  # so a shared repository can hold other things as well
BACKUP_KEEP = ("--keep-daily", "7", "--keep-weekly", "4", "--keep-monthly", "6")
BACKUP_STALE_DAYS = 2
EXPORT_STALE_DAYS = 35  # a copy taken monthly is a month and a bit old at worst


def _require_restic() -> None:
    if shutil.which("restic") is None:
        raise KitError("restic isn't installed. Add it with: apt-get install restic")


def _restic_environment(paths: Paths, password: str | None = None, **extra: str) -> dict[str, str]:
    """`backup.env` is read as the environment, so a target that needs credentials of
    its own, such as S3, only takes extra lines in that file. A password given here
    wins, which is how a restore works before that file has been put back."""
    environment = {**os.environ, **_read_env(paths.backup_env)}
    if password:
        environment["RESTIC_PASSWORD"] = password
    if not environment.get("RESTIC_PASSWORD"):
        raise KitError(f"No backup password in {paths.backup_env}. Take a backup first, or set RESTIC_PASSWORD.")
    return {**environment, **extra}


def _restic_run(paths: Paths, *args: str, password: str | None = None, repo: str | None = None, **extra: str) -> None:
    """Let restic print for itself: how much it added, and how long it took, is what
    an operator wants to see."""
    command = ["restic", "--repo", repo or str(paths.repo), *args]
    if subprocess.run(command, env=_restic_environment(paths, password, **extra)).returncode != 0:
        raise KitError(f"restic {args[0]} failed. Nothing else was run.")


def _restic_read(paths: Paths, *args: str, password: str | None = None, repo: str | None = None, **extra: str) -> str | None:
    """None when restic refuses, which is how a missing or unreadable repository is
    told apart from an empty one."""
    command = ["restic", "--repo", repo or str(paths.repo), *args]
    environment = _restic_environment(paths, password, **extra)
    result = subprocess.run(command, env=environment, capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def _repo_exists(paths: Paths) -> bool:
    return (paths.repo / "config").exists()


def backup_password(paths: Paths) -> tuple[str, bool]:
    """Made once and then kept, and whether it had to be made. Everything in a backup
    is unreadable without it, so it goes in the vault and on an offline copy. It lives
    in the backup too, so a restored server keeps backing up with the same one."""
    current = _read_env(paths.backup_env)
    if current.get("RESTIC_PASSWORD"):
        return current["RESTIC_PASSWORD"], False
    password = secrets.token_urlsafe(32)
    settings = {**current, "RESTIC_PASSWORD": password}
    _write_atomic(paths.backup_env, "".join(f"{n}={v}\n" for n, v in settings.items()), 0o600)
    return password, True


def _print_backup_password(paths: Paths, password: str) -> None:
    """Only to someone's screen. The nightly run is the first backup on a server nobody
    has touched, and printing it there would write the password into the system journal,
    where it would outlive the installation and travel in any copy of the logs."""
    print("\nThis installation now has a backup password, made just now.")
    print("Nothing in a backup can be read without it, and it can't be recovered.")
    print("Store it in the vault, and write down an offline copy kept away from the backups.\n")
    if sys.stdout.isatty():
        print(f"  {password}\n")
        print(f"It's also in {paths.backup_env}, which only root can read.\n")
    else:
        print(f"It isn't printed here, as this isn't a screen and it would go into the logs.")
        print(f"Read it on the server with:  sudo grep RESTIC_PASSWORD {paths.backup_env}\n")


def backup_paths(paths: Paths, config: Config) -> list[Path]:
    """The config, core's own folders, and every folder a module declares under `data`
    in its `module.toml`, whether or not it's switched on: disabling a module keeps its
    data, so a backup has to keep it too. Caddy's folder comes along for its
    certificates, so a restored server serves HTTPS without asking for new ones, and
    the deploy account's for the publishing keys, so CI keeps working after a restore."""
    wanted = [paths.config_dir, paths.core_dir, paths.caddy_dir, paths.deploy_home]
    for manifest in sorted((ROOT / "modules").glob("*/module.toml")):
        for declared in tomllib.loads(manifest.read_text("utf-8")).get("data", []):
            wanted.append(Path(declared))
    present = [path for path in dict.fromkeys(wanted) if path.exists()]
    if paths.backup_dir in present:  # a repository inside itself would grow without end
        raise KitError(f"{paths.backup_dir} holds the backups, so it can't be backed up.")
    return present


def _code_version(root: Path | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(root or ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


# ---- Releases: see docs/releasing.md -----------------------------------------

RELEASE_NUMBER = re.compile(r"(\d+)\.(\d+)\.(\d+)")
CHANGELOG_HEADING = re.compile(r"^## (\d+\.\d+\.\d+) \((\d{4}-\d{2}-\d{2})\)$", re.M)
BEFORE_UPDATING = "### Before you update"


def parse_release(text: str) -> tuple[int, int, int]:
    """A release number such as 0.7.0, as numbers, so 0.10.0 sorts after 0.9.0."""
    match = RELEASE_NUMBER.fullmatch(text.strip().removeprefix("v"))
    if not match:
        raise KitError(f"{text!r} isn't a release number such as 0.7.0.")
    return tuple(int(part) for part in match.groups())


def release_number(root: Path | None = None) -> str:
    """The newest release this code contains, from VERSION. It only changes in a release."""
    text = ((root or ROOT) / "VERSION").read_text("utf-8").strip()
    parse_release(text)
    return text


def running_version(root: Path | None = None) -> str:
    """The release, if this checkout is exactly its tag, as the installer and `kit update`
    leave it. Anything else is that release plus the commit, such as 0.6.0+abc1234."""
    root = root or ROOT
    number = release_number(root)
    tags = subprocess.run(
        ["git", "-C", str(root), "tag", "--points-at", "HEAD"], capture_output=True, text=True
    ).stdout.split()
    return number if f"v{number}" in tags else f"{number}+{_code_version(root)}"


@dataclass(frozen=True)
class Release:
    number: str
    date: str
    text: str  # everything under its heading

    @property
    def before_updating(self) -> str:
        """The part someone updating has to read first, without its heading."""
        start = self.text.find(BEFORE_UPDATING)
        if start < 0:
            return ""
        body = self.text[start + len(BEFORE_UPDATING) :]
        end = body.find("\n### ")
        return (body if end < 0 else body[:end]).strip()


def parse_changelog(text: str) -> list[Release]:
    """Each release in CHANGELOG.md, newest first, as the file has them."""
    headings = list(CHANGELOG_HEADING.finditer(text))
    releases = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        releases.append(Release(heading.group(1), heading.group(2), text[heading.end() : end].strip()))
    return releases


def changelog_problems(text: str, version: str) -> list[str]:
    """What's wrong with a changelog, for CI and for `kit update` to refuse a release by."""
    releases = parse_changelog(text)
    problems = []
    if version not in [release.number for release in releases]:
        problems.append(f"There's no section for {version}, the number in VERSION.")
    for release in releases:
        if not release.before_updating:
            problems.append(f"{release.number} has no '{BEFORE_UPDATING}' part, or it's empty. Write 'Nothing.' if there's nothing.")
    numbers = [parse_release(release.number) for release in releases]
    if numbers != sorted(numbers, reverse=True) or len(set(numbers)) != len(numbers):
        problems.append("The sections aren't newest first, or one appears twice.")
    return problems


def _pinned_images() -> dict[str, str]:
    """Each module's pinned image version, read from its Compose file, keyed by the
    image's own name so a module with several containers is covered. A restore records
    these: an older Forgejo refuses to start on a database written by a newer one, so
    which versions took a backup decides whether it can be restored here."""
    images = {}
    for compose in sorted((ROOT / "modules").glob("*/compose.yml")):
        for match in re.finditer(r"^\s*image:\s*(\S+?):(\S+)\s*$", compose.read_text("utf-8"), re.M):
            images[match.group(1).rsplit("/", 1)[-1]] = match.group(2)
    return images


def _backup_tags(config: Config) -> list[str]:
    """What this snapshot was taken by, so a restore can say whether it belongs here.
    restic keeps tags with the snapshot and needs no file of our own inside it."""
    tags = [BACKUP_TAG, f"version:{_code_version()}", f"release:{running_version()}"]
    tags += [f"module:{name}" for name in config.modules]
    tags += [f"image:{name}={version}" for name, version in sorted(_pinned_images().items())]
    return [item for tag in tags for item in ("--tag", tag)]


@contextlib.contextmanager
def _backup_lock(paths: Paths):
    """One at a time: the nightly run and an operator can otherwise collide, and an
    export must not read a repository that's being written."""
    paths.backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(paths.backup_dir / "kit.lock", "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise KitError("A backup or an update is already running. Try again in a few minutes.") from None
        yield handle


def _module_containers(config: Config) -> list[str]:
    return [name for name in config.modules if _has_containers(name)]


def _run_backup_scripts(config: Config) -> None:
    """Each module's own step before the copy, from §7.1 of the spec, while its
    containers are still running, as a database dump needs its database up. It writes
    inside the module's own data folder, which the copy then picks up."""
    for name in config.modules:
        script = ROOT / "modules" / name / "backup.sh"
        if script.exists():
            print(f"Running {name}'s own backup step.")
            if subprocess.run(["sh", str(script)], cwd=script.parent).returncode != 0:
                raise KitError(f"{name}'s backup.sh failed, so no backup was taken.")


def _stop_for_backup(paths: Paths, config: Config) -> None:
    """Everything but Caddy, so core's copy agrees with each tool's copy. The public
    sites stay up throughout, as Caddy depends on none of this. STOP also reaches the
    vault's emergency copy, which would otherwise be writing to the data being copied,
    and which is meant to be short-lived anyway."""
    subprocess.run(_compose("stop", "sync", "core"), check=True)
    for name in _module_containers(config):
        if name in STOP:
            STOP[name](paths)
        subprocess.run(_module_compose(name, "stop"), check=True)


def _start_after_backup(paths: Paths, config: Config) -> None:
    """In the same order as `kit apply`, waiting for each to be healthy as that does,
    so a backup can't quietly leave something down. Everything is tried even if one
    fails, as the rest still has to come back."""
    problems = []
    for name in _module_containers(config):
        try:
            subprocess.run(_module_compose(name, "up", "-d", "--wait"), check=True)
        except subprocess.CalledProcessError:
            problems.append(name)
    for service in ("core", "sync"):
        try:
            subprocess.run(_compose("up", "-d", "--wait", service), check=True)
        except subprocess.CalledProcessError:
            problems.append(service)
    if problems:
        raise KitError(f"The backup finished, but these didn't come back: {', '.join(problems)}. Run: kit apply")


def _copy_off_server(paths: Paths, config: Config, password: str) -> None:
    """Push to the target in the config, with the same password, so the copy there and
    an exported file are interchangeable. Its chunker parameters are copied from this
    repository, or everything would be stored again from scratch on each run. This is
    for an association with somewhere to put backups but no machine to pull them: a
    pull needs no credentials here, and a server that can write to its own off-server
    copy can also be made to delete it."""
    target = config.backup_target
    source = {"RESTIC_FROM_PASSWORD": password}
    if _restic_read(paths, "cat", "config", password=password, repo=target, **source) is None:
        print(f"Setting up {target} for the first time.")
        _restic_run(paths, "init", "--copy-chunker-params", "--from-repo", str(paths.repo), repo=target, **source)
    print(f"Copying off the server to {target}.")
    _restic_run(paths, "copy", "--from-repo", str(paths.repo), repo=target, **source)


def run_backup(paths: Paths) -> None:
    """Take one backup. Safe to repeat, and safe to interrupt: restic either finishes a
    snapshot or leaves the repository as it was, and the containers come back either way."""
    _require_restic()
    with _backup_lock(paths):
        _take_backup(paths, load_config(paths))


def _take_backup(paths: Paths, config: Config, leave_stopped: bool = False) -> None:
    """The backup itself, with the lock already held. `kit update` leaves everything
    stopped afterwards, so nothing can be written between its backup and the switch,
    as that would be lost if the update had to be rolled back. If the copy fails,
    everything starts again either way."""
    password, is_new = backup_password(paths)
    if is_new:
        _print_backup_password(paths, password)
    if not _repo_exists(paths):
        _restic_run(paths, "init")
    # Worked out before anything stops, so a mistake in a module's manifest costs
    # an error rather than an outage.
    what = [str(path) for path in backup_paths(paths, config)]
    tags = _backup_tags(config)
    _run_backup_scripts(config)
    paused = time.monotonic()
    _stop_for_backup(paths, config)
    try:
        _restic_run(paths, "backup", *tags, "--", *what)
    except BaseException:
        _start_after_backup(paths, config)
        raise
    if leave_stopped:
        print("\nThe backup is taken. Logins and the tools stay stopped for the update. The public sites don't.")
    else:
        _start_after_backup(paths, config)
        print(f"\nLogins and the tools were paused for {time.monotonic() - paused:.0f} seconds. The public sites weren't.")
        _restic_run(paths, "forget", "--prune", "--tag", BACKUP_TAG, *BACKUP_KEEP)  # the nightly run does this during an update
    if config.backup_target:
        _copy_off_server(paths, config, password)
    elif not leave_stopped:
        print(f"\nNothing has left this server. Take a copy with: kit backup export {_export_name()}")


def _export_name() -> str:
    return f"sallskap-backup-{datetime.now(timezone.utc):%Y-%m-%d}.tar"


def _note_export(paths: Paths) -> None:
    _write_atomic(paths.backup_dir / "last-export", f"{int(time.time())}\n", 0o600)


def export_backup(paths: Paths, destination: str) -> None:
    """One file holding the whole repository. It's already encrypted, so the file needs
    no password of its own, and it carries every snapshot rather than one night.
    `-` streams it to stdout, which makes a copy off the server one line over SSH, and
    the same line in a cron job on a machine the association trusts."""
    with _backup_lock(paths):
        if not _repo_exists(paths):
            raise KitError("There's no backup to export yet. Take one with: kit backup")
        if destination == "-":
            if sys.stdout.isatty():
                raise KitError(
                    "That would print the backup to your screen. Send it somewhere:"
                    f"\n  ssh root@this-server 'kit backup export -' > {_export_name()}"
                    f"\n  kit backup export /root/{_export_name()}"
                )
            with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as tar:
                tar.add(paths.repo, arcname="repo")
            sys.stdout.buffer.flush()
            print(f"Exported {paths.repo} to standard output.", file=sys.stderr)
        else:
            path = Path(destination)
            if path.is_dir():
                path = path / _export_name()
            with tarfile.open(path, mode="w") as tar:
                tar.add(paths.repo, arcname="repo")
            os.chmod(path, 0o600)
            print(f"Exported to {path} ({path.stat().st_size / 1e6:.1f} MB).")
            print("It's encrypted. Keep it away from the password, or the password protects nothing.")
        _note_export(paths)


# ---- Restoring ---------------------------------------------------------------


def _restore_password(paths: Paths) -> str:
    """From the offline copy, typed in, as the vault holding the other copy is inside
    the backup. An existing `backup.env` is used when there is one, which is how a
    second restore onto the same server needs no typing."""
    for known in (os.environ.get("RESTIC_PASSWORD"), _read_env(paths.backup_env).get("RESTIC_PASSWORD")):
        if known:
            return known
    try:
        password = getpass.getpass("Backup password, from the offline copy: ")
    except (EOFError, OSError):
        raise KitError("No backup password. Set RESTIC_PASSWORD and run this again.") from None
    if not password:
        raise KitError("No backup password given.")
    return password


def _unpack_export(paths: Paths, source: str) -> None:
    """An exported file back into place, so the restore then runs against a local
    repository however the copy arrived."""
    if _repo_exists(paths):
        raise KitError(f"{paths.repo} already holds backups. Move that folder aside first.")
    paths.backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stream = sys.stdin.buffer if source == "-" else open(source, "rb")
    try:
        with tarfile.open(fileobj=stream, mode="r|*") as tar:
            tar.extractall(paths.backup_dir, filter="data")
    except (tarfile.TarError, EOFError) as error:
        raise KitError(f"That isn't a backup exported by kit: {error}") from error
    finally:
        if stream is not sys.stdin.buffer:
            stream.close()
    if not _repo_exists(paths):
        raise KitError("That file unpacked, but holds no backup repository.")


def _snapshot_to_restore(paths: Paths, password: str, repo: str | None = None, snapshot_id: str | None = None) -> dict:
    """The newest backup, or the one asked for, as `kit update --rollback` does."""
    which = [snapshot_id] if snapshot_id else ["--latest", "1"]
    output = _restic_read(paths, "snapshots", *which, "--tag", BACKUP_TAG, "--json", password=password, repo=repo)
    if output is None:
        raise KitError("Couldn't read the backups. The password may be wrong, or the copy may be damaged.")
    snapshots = json.loads(output)
    if not snapshots:
        raise KitError(f"That copy holds no backup {snapshot_id} taken by kit." if snapshot_id else "That copy holds no backups taken by kit.")
    return snapshots[-1]


def _check_versions(snapshot: dict, force: bool) -> None:
    """An older Forgejo refuses to start on a database written by a newer one, so code
    older than the backup can leave it unusable. Which way round can't be worked out
    from version numbers, so any difference stops and says what it found."""
    taken_with = dict(
        tag.removeprefix("image:").split("=", 1) for tag in snapshot.get("tags", []) if tag.startswith("image:")
    )
    here = _pinned_images()
    differences = [
        f"{name}: the backup was taken with {version}, this code pins {here[name]}"
        for name, version in sorted(taken_with.items())
        if name in here and here[name] != version
    ]
    if differences and not force:
        raise KitError(
            "The versions don't match:\n  "
            + "\n  ".join(differences)
            + "\nRestoring can only go forwards: an older tool refuses a newer database."
            "\nInstall the version the backup was taken with, or, if this one is newer,"
            "\nrun this again with --across-versions."
        )
    for line in differences:
        print(f"Going ahead with a version difference, as asked: {line}")


def _looks_installed(paths: Paths) -> bool:
    return (paths.core_dir / "db.sqlite3").exists()


def _fix_ownership(paths: Paths, config: Config) -> None:
    """restic puts back the numbers it saved. The containers' users are fixed numbers in
    the Compose files, so those are already right, but the deploy account's number is
    whatever this server gave it, so its folders are set again by name."""
    if os.geteuid() != 0:
        return
    folders = [paths.deploy_home, *(paths.site_dir(site.domain) for site in config.sites)]
    try:
        for folder in folders:
            if not folder.exists():
                continue
            _chown_to_deploy(folder)
            for child in folder.rglob("*"):
                _chown_to_deploy(child)
    except LookupError:
        raise KitError(
            f"There's no {DEPLOY_USER} account on this server, so the publishing folders"
            " can't be put in its name. Run the installer first, then run this again."
        ) from None


def _move_aside(snapshot: dict, paths: Paths) -> list[Path]:
    """Restoring over a working installation would leave whatever it has written since,
    and restic can only delete those files from version 0.17, newer than Debian 12 and
    Ubuntu 24.04 have. A database's `-wal` file left beside an older copy of it would
    be read as part of it. So the folders are moved aside first, which costs no space,
    and kept, in case something in them is wanted. The config folder stays, as the
    restore needs its backup password, and its files are rewritten by `kit apply`."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    moved = []
    for name in snapshot.get("paths", []):
        folder = Path(name)
        if folder == paths.config_dir or not folder.exists():
            continue
        aside = folder.with_name(f"{folder.name}.before-restore-{stamp}")
        folder.rename(aside)
        moved.append(aside)
    return moved


def restore(
    paths: Paths,
    source: str | None,
    force: bool,
    across_versions: bool = False,
    from_repo: str | None = None,
    snapshot_id: str | None = None,
) -> None:
    """A full restore onto a fresh server: the installer first, then this. It puts back
    the config and every folder in the backup, then starts everything. See docs/restore.md
    for what to check afterwards, which includes who has left since the backup was taken.

    The two guards have their own flags on purpose. Replacing a working installation and
    crossing tool versions are different risks, and one flag for both would waive the
    version check exactly when it matters most: rolling back a bad upgrade is the usual
    reason to restore over a working server, and it's also the usual way to end up
    running an older tool against a newer database."""
    _require_restic()
    if _looks_installed(paths) and not force:
        raise KitError(
            f"This server already has an installation in {paths.core_dir}, and a restore would replace it."
            "\nRestore onto a fresh server, or run this again with --force if you mean to replace this one."
        )
    if from_repo and source:
        raise KitError("Give either an exported file or --from, not both.")
    if source:
        _unpack_export(paths, source)
    if not from_repo and not _repo_exists(paths):
        raise KitError(
            f"No backups in {paths.repo}. Give the exported file: kit restore <file>"
            "\nOr read them straight from where they're kept: kit restore --from sftp:…"
        )

    password = _restore_password(paths)
    snapshot = _snapshot_to_restore(paths, password, repo=from_repo, snapshot_id=snapshot_id)
    _check_versions(snapshot, across_versions)
    taken = re.sub(r"(\.\d{6})\d+", r"\1", snapshot["time"])
    old = _age_in_days(datetime.fromisoformat(taken).timestamp())
    print(
        f"Restoring the backup taken {snapshot['time'][:19].replace('T', ' ')}"
        f" on {snapshot.get('hostname', '?')}, {_spoken_age(old)}."
    )
    for path in snapshot.get("paths", []):
        print(f"  {path}")

    if paths.config.exists():  # anything running has to let go of the files first
        with contextlib.suppress(KitError, subprocess.CalledProcessError):
            _stop_for_backup(paths, load_config(paths))
    moved = _move_aside(snapshot, paths) if _looks_installed(paths) else []
    _restic_run(paths, "restore", snapshot["id"], "--target", "/", password=password, repo=from_repo)

    config = load_config(paths)
    _fix_ownership(paths, config)
    # Core's image for this version is still here after a rolled-back update, and
    # building it again would need the internet and the build cache an update clears.
    apply(paths, build=not _have_image(core_image()))
    if moved:
        print("\nWhat was here before is kept, in case anything in it is wanted. Delete it once you're sure:")
        for folder in moved:
            print(f"  {folder}")
    print(f"\nRestored. Two things to do now, from docs/restore.md:")
    print(f"  - REMOVE ANYONE WHO HAS LEFT in the last {_spoken_age(old).replace(' ago', '')}.")
    print("    This put everyone back as they were when the backup was taken, including people")
    print("    who have left since. They are members again, with working access, and nothing")
    print("    here will take it away: this installation no longer knows they ever left.")
    print("    Removing them is by hand, and their shared passwords still need changing.")
    print("  - A forgotten master password is gone for good: a restore can't bring one back.")


# ---- Updates: see docs/spec.md §10.3 and docs/releasing.md -------------------

UPDATE_SPARE_BYTES = 3 * 1024**3  # new images and core's build, on top of the backup
IMAGE_LINE = re.compile(r"^\s*image:\s*([^\s$]+?):([^\s$]+?)\s*(?:#.*)?$", re.M)
FORGEJO_RELEASES = "https://codeberg.org/forgejo/forgejo/releases"
TOOL_NAMES = {"forgejo": "Forgejo", "vaultwarden": "Vaultwarden", "caddy": "Caddy"}


def _update_state_file(paths: Paths) -> Path:
    """Beside the backups, as a restore puts /etc/sallskap back as it was and would
    take this with it. Root only, like the backups."""
    return paths.backup_dir / "update.json"


def _read_update_state(paths: Paths) -> dict | None:
    state_file = _update_state_file(paths)
    return json.loads(state_file.read_text("utf-8")) if state_file.exists() else None


def _write_update_state(paths: Paths, state: dict) -> None:
    paths.backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_atomic(_update_state_file(paths), json.dumps(state, indent=2) + "\n", 0o600)


def _git(*args: str) -> str:
    result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
    if result.returncode:
        raise KitError(f"git {args[0]} failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout


def _git_from_origin(*args: str) -> str:
    """The same, for what needs the internet, with a message that says so."""
    try:
        return _git(*args)
    except KitError as error:
        origin = subprocess.run(["git", "-C", str(ROOT), "remote", "get-url", "origin"], capture_output=True, text=True)
        raise KitError(
            f"Couldn't get the kit from {origin.stdout.strip() or 'where it came from'}: {error}"
            "\nCheck this server can reach the internet, then run this again. Nothing has changed."
        ) from None


def available_releases() -> list[str]:
    """Every release tag where the kit comes from, oldest first. Anything that isn't a
    plain release number, such as a test tag, is left out."""
    output = _git_from_origin("ls-remote", "--tags", "--refs", "origin", "v*")
    found = []
    for line in output.splitlines():
        name = line.split("refs/tags/", 1)[-1]
        with contextlib.suppress(KitError):
            found.append((parse_release(name), name.removeprefix("v")))
    return [name for _, name in sorted(found)]


def _show(commit: str, path: str) -> str | None:
    result = subprocess.run(["git", "-C", str(ROOT), "show", f"{commit}:{path}"], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def images_at(commit: str) -> dict[str, str]:
    """Every pinned image in a commit's Compose files, by the image's own name. Core's
    isn't pinned, as it's built here."""
    files = _git("ls-tree", "-r", "--name-only", commit).split()
    images = {}
    for name in files:
        if name == "compose.yml" or re.fullmatch(r"modules/[^/]+/compose\.yml", name):
            for match in IMAGE_LINE.finditer(_show(commit, name) or ""):
                images[match.group(1).rsplit("/", 1)[-1]] = f"{match.group(1)}:{match.group(2)}"
    return images


def _base(version: str) -> tuple[int, int, int]:
    return parse_release(version.split("+", 1)[0])


def _choose_target(wanted: str | None, unreleased: str | None, current: str) -> tuple[str, str]:
    """The commit to move to and its version, fetched but not checked out. Only release
    tags are taken unless asked otherwise, and never an older one, as an older tool
    refuses a newer database and the backup is the only way back."""
    if unreleased:
        _git_from_origin("fetch", "-q", "--depth", "1", "origin", unreleased)
        commit = _git("rev-parse", "FETCH_HEAD").strip()
        number = (_show(commit, "VERSION") or "").strip()
        parse_release(number)
        if parse_release(number) < _base(current):
            raise KitError(f"{unreleased} holds {number}, older than {current}, which this runs. Nothing has changed.")
        return commit, f"{number}+{_git('rev-parse', '--short', commit).strip()}"

    releases = available_releases()
    if not releases:
        raise KitError("There are no releases to update to yet. Nothing has changed.")
    target = wanted.removeprefix("v") if wanted else releases[-1]
    parse_release(target)
    if target not in releases:
        raise KitError(f"There's no release {target}. The newest is {releases[-1]}. Nothing has changed.")
    if target == current:
        raise KitError(f"This already runs {current}, so there's nothing to do.")
    if parse_release(target) <= _base(current):
        raise KitError(
            f"{target} is older than {current}, which this runs, and an update only goes forwards:"
            "\nan older tool refuses a database a newer one has written. Nothing has changed."
            "\nTo go back after an update, use: kit update --rollback"
        )
    _git_from_origin("fetch", "-q", "--depth", "1", "origin", "tag", f"v{target}")
    commit = _git("rev-parse", f"v{target}^{{commit}}").strip()
    if (_show(commit, "VERSION") or "").strip() != target:
        raise KitError(f"The tag v{target} doesn't hold release {target} in its VERSION file, so it's refused. Nothing has changed.")
    problems = changelog_problems(_show(commit, "CHANGELOG.md") or "", target)
    if problems:
        raise KitError(f"Release {target}'s changelog isn't complete, so it's refused:\n  " + "\n  ".join(problems))
    return commit, target


def _tool_name(image: str) -> str:
    return TOOL_NAMES.get(image, image)


def describe_update(current: str, target: str, commit: str) -> list[str]:
    """What someone has to read before going ahead, as `apt-listchanges` shows Debian's
    NEWS files: each release's "Before you update", and every tool that changes."""
    lines = [f"This server runs Sällskap {current}. This update goes to {target}.", ""]
    releases = [
        release
        for release in parse_changelog(_show(commit, "CHANGELOG.md") or "")
        if _base(current) < parse_release(release.number) <= _base(target)
    ]
    if "+" in target:
        lines += ["This isn't a release, so it has no changelog of its own. Read its commits first.", ""]
    for release in reversed(releases):  # oldest first, the order to take them in
        lines.append(f"Before you update, from {release.number}'s changelog:")
        lines += [f"  {line}" for line in release.before_updating.splitlines()]
        lines.append("")
    before, after = images_at("HEAD"), images_at(commit)
    changed = {name: (before.get(name), image) for name, image in after.items() if before.get(name) != image}
    if changed:
        lines.append("Tool versions:")
        for name, (old, new) in sorted(changed.items()):
            lines.append(f"  {_tool_name(name)}: {old.rsplit(':', 1)[1] if old else 'new'} → {new.rsplit(':', 1)[1]}")
        lines.append("")
    if "forgejo" in changed:
        lines += [
            f"Forgejo changes version. Read its release notes for anything that breaks: {FORGEJO_RELEASES}",
            "If you've changed Forgejo's look with your own CSS, check it against them first, as its upgrade guide says.",
            "",
        ]
    lines += [
        "What happens: the new images are prepared while everything runs. Then a backup is",
        "taken, and logins and the tools stay down until the new version has started and",
        "passed its checks, usually a minute or two. The public sites stay up throughout.",
        "There's no going back to an older tool on the same data: the backup is the way back.",
    ]
    return lines


def _project_containers() -> list[tuple[str, str, str, str]]:
    """Name, image, state and status of each of the installation's containers. The vault's
    emergency copy is left out: its health check always fails, and a backup stops it."""
    command = [
        "docker", "ps", "--all", "--filter", f"label=com.docker.compose.project={PROJECT}",
        "--format", "{{.Names}}\t{{.Image}}\t{{.State}}\t{{.Status}}",
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise KitError(f"Couldn't ask Docker what's running: {result.stderr.strip()}")
    rows = [tuple(line.split("\t")) for line in result.stdout.splitlines() if line.count("\t") == 3]
    return [row for row in rows if row[0] != VAULT_EMERGENCY]


def unhealthy_containers() -> list[str]:
    return [
        f"{name} ({status})"
        for name, _, state, status in _project_containers()
        if state != "running" or "unhealthy" in status or "health: starting" in status
    ]


def _bytes_in(folders: list[Path]) -> int:
    total = 0
    for folder in folders:
        for parent, _, files in os.walk(folder):
            for name in files:
                with contextlib.suppress(OSError):
                    total += os.lstat(os.path.join(parent, name)).st_size
    return total


def _check_ready_to_update(paths: Paths, config: Config) -> None:
    """Everything that can stop an update, before anything changes. A problem that was
    already there mustn't be blamed on the update, or found halfway through it."""
    if os.geteuid() != 0:
        raise KitError("An update has to run as root. Run it again with sudo.")
    _require_restic()
    unfinished = _read_update_state(paths)
    if unfinished and not unfinished.get("result"):
        raise KitError(
            f"The update to {unfinished['to']} that started {unfinished['started']} didn't finish."
            "\nFinish it with:   kit update --finish"
            "\nOr go back with:  kit update --rollback"
        )
    unhealthy = unhealthy_containers()
    if unhealthy:
        raise KitError(
            "Not everything is running properly, so updating now could hide the cause:\n  "
            + "\n  ".join(unhealthy)
            + "\nRun: kit apply   Then run this again once that works. Nothing has changed."
        )
    needed = UPDATE_SPARE_BYTES + _bytes_in(backup_paths(paths, config))
    free = shutil.disk_usage("/").free
    if free < needed:
        raise KitError(
            f"There's {free / 1024**3:.1f} GB free on this server, and an update needs about {needed / 1024**3:.1f} GB"
            "\nfor the new images and the backup. Free some space, then run this again. Nothing has changed."
        )


def _prepare_images(commit: str, version: str) -> None:
    """The slow part, while everything still runs: pull the new tools, and build core's
    new image from the new code without checking it out."""
    for image in sorted(set(images_at(commit).values()) - set(images_at("HEAD").values())):
        print(f"Downloading {image}.")
        if subprocess.run(["docker", "pull", "--quiet", image], stdout=subprocess.DEVNULL).returncode:
            raise KitError(f"Couldn't download {image}. Check this server can reach the internet, then run this again. Nothing has changed.")
    print(f"Building core for {version}.")
    archive = subprocess.Popen(["git", "-C", str(ROOT), "archive", "--format=tar", f"{commit}:core"], stdout=subprocess.PIPE)
    build = subprocess.run(["docker", "build", "--quiet", "--tag", core_image(version), "-"], stdin=archive.stdout, stdout=subprocess.DEVNULL)
    archive.stdout.close()
    if archive.wait() or build.returncode:
        raise KitError(f"Core's new image didn't build, so nothing has changed. Run this again, or see: docker build {ROOT}/core")


def _confirm(question: str, yes: bool) -> None:
    if yes:
        return
    if not sys.stdin.isatty():
        raise KitError("This asks before going ahead. Run it on a screen, or add --yes. Nothing has changed.")
    if input(f"{question} Type yes to go ahead: ").strip().lower() != "yes":
        raise KitError("Stopped, as asked. Nothing has changed.")


def update(paths: Paths, wanted: str | None, unreleased: str | None, yes: bool) -> None:
    """Back up, then move to a newer release. Everything up to the backup changes
    nothing, so a failure there leaves the old version running. From the switch on, the
    new release's own code carries on, so it's the new code that starts itself."""
    config = load_config(paths)
    _check_ready_to_update(paths, config)
    current = running_version()
    commit, target = _choose_target(wanted, unreleased, current)
    print("\n".join(describe_update(current, target, commit)) + "\n")
    if unreleased:
        print(f"WARNING: {unreleased} isn't a release. Only do this on a test server.\n")
    _confirm(f"Update to {target}?", yes)

    _prepare_images(commit, target)
    with _backup_lock(paths) as lock:
        if "git" in config.modules:
            print("Telling Forgejo to finish its queued work.")
            try:
                _forgejo_output("manager", "flush-queues")
            except KitError as error:
                raise KitError(f"{error}\nNothing has changed, and everything is still running.") from None
        print("Taking a backup. This is the way back if anything goes wrong.")
        try:
            _take_backup(paths, config, leave_stopped=True)
        except (KitError, subprocess.CalledProcessError) as error:
            raise KitError(f"The backup failed, so nothing was updated, and everything is running as before:\n{error}") from None
        try:
            _switch_code(paths, current, commit, target)
        except BaseException as error:
            _start_after_backup(paths, config)
            _update_state_file(paths).unlink(missing_ok=True)
            raise KitError(f"Switching to the new code failed, so the old version has been started again:\n{error}") from None
        os.set_inheritable(lock.fileno(), True)  # the nightly backup waits until it's finished
        kit = str(ROOT / "kit" / "kit.py")
        os.execv(sys.executable, [sys.executable, kit, "update", "--finish"])


def _switch_code(paths: Paths, current: str, commit: str, target: str) -> None:
    """Record how to get back, then check out the new code. Nothing is running here."""
    snapshot = _snapshot_to_restore(paths, backup_password(paths)[0])
    state = {
        "from": current,
        "from_commit": _git("rev-parse", "HEAD").strip(),
        "to": target,
        "to_commit": commit,
        "snapshot": snapshot["id"],
        "started": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "image_ids_before": _image_ids_in_use(),
    }
    _write_update_state(paths, state)
    # Kept by name, so Git can't tidy the old version away while it's still the way back.
    _git("update-ref", "refs/sallskap/before-update", state["from_commit"])
    _git("checkout", "-q", "--detach", commit)
    print(f"Switched the code to {target}.")


# Left out of Forgejo's own check, which its upgrade guide runs with --all. Measured on
# a healthy 15.0.11: these two always report errors here, so they'd hide a real one.
FORGEJO_DOCTOR_SKIPPED = {
    "gc-lfs": "a clean-up rather than a check, and it fails with LFS unused",
    "authorized-keys": "for its SSH server, which is off",
}


def forgejo_doctor_problems() -> list[str]:
    """Every check but those above. It exits 0 even when a check reports an error, so
    its output is read for them. Warnings are shown and don't fail the update."""
    try:
        listed = _forgejo_output("doctor", "check", "--list")
        names = [line.lstrip("*").split()[0] for line in listed.splitlines()[1:] if line.strip()]
        wanted = [item for name in names if name not in FORGEJO_DOCTOR_SKIPPED for item in ("--run", name)]
        output = _forgejo_output("doctor", "check", *wanted)
    except KitError as error:
        return [f"Forgejo's own check failed: {error}"]
    for line in output.splitlines():
        if "[W]" in line:
            print(f"Forgejo's own check warns: {line.split('[W]', 1)[1].strip()}")
    errors = [line.split("[E]", 1)[1].strip() for line in output.splitlines() if "[E]" in line]
    return [f"Forgejo's own check found: {error}" for error in errors]


def after_update_problems(paths: Paths, config: Config) -> list[str]:
    """Whether the new version works, before anything is cleaned up."""
    problems = [f"Not running properly: {name}" for name in unhealthy_containers()]
    if "git" in config.modules:
        problems += forgejo_doctor_problems()
    check = subprocess.run(_compose("exec", "-T", "sync", "python", "manage.py", "sync_tools", "--check"), capture_output=True, text=True)
    if check.returncode:
        problems.append(f"The worker can't reach every tool the way it expects: {(check.stdout + check.stderr).strip()}")
    host = f"app.{config.domain}"
    # Straight to Caddy on this server, as DNS may point elsewhere, and without checking
    # the certificate, which `kit status` looks after. This is whether core answers.
    answer = subprocess.run(
        ["curl", "-ksS", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "20",
         "--resolve", f"{host}:443:127.0.0.1", f"https://{host}/accounts/login/"],
        capture_output=True, text=True,
    )
    if answer.stdout.strip() != "200":
        problems.append(f"The login page at {host} answered {answer.stdout.strip() or answer.stderr.strip()}, not 200")
    return problems


def _image_ids_in_use() -> list[str]:
    """By ID rather than name: Docker shows a container's image as a bare ID once its tag
    has moved on, and a name-matching clean-up would then delete the image it runs."""
    names = [name for name, _, _, _ in _project_containers()]
    if not names:
        return []
    result = subprocess.run(["docker", "inspect", "--format", "{{.Image}}", *names], capture_output=True, text=True)
    return sorted(set(result.stdout.split()))


def _clean_up_images(state: dict) -> None:
    """Old images of this installation's own tools, keeping the ones from before this
    update, which a rollback goes back to. Nothing else on the server is touched, apart
    from Docker's build cache, which is what building core leaves behind."""
    keep = set(state["image_ids_before"]) | set(_image_ids_in_use())
    listed = subprocess.run(
        ["docker", "image", "ls", "--no-trunc", "--format", "{{.Repository}}:{{.Tag}}\t{{.ID}}"], capture_output=True, text=True
    )
    images = [line.split("\t") for line in listed.stdout.splitlines() if "\t" in line]
    ours = {name.rsplit(":", 1)[0] for name, image_id in images if image_id in keep} | {CORE_IMAGE}
    for name, image_id in images:
        if name.rsplit(":", 1)[0] in ours and image_id not in keep and not name.endswith(":<none>"):
            subprocess.run(["docker", "image", "rm", name], capture_output=True)
    subprocess.run(["docker", "image", "prune", "--force"], capture_output=True)
    subprocess.run(["docker", "builder", "prune", "--force"], capture_output=True)


def _show_logs_of_unhealthy() -> None:
    """What a failed start most likely needs: the end of each failed container's own log."""
    for name, _, state, status in _project_containers():
        if state != "running" or "unhealthy" in status or "health: starting" in status:
            print(f"\nThe end of {name}'s log, which usually says why:", file=sys.stderr)
            subprocess.run(["docker", "logs", "--tail", "15", name], stdout=sys.stderr, stderr=sys.stderr)


def finish_update(paths: Paths) -> int:
    """Run by the new release's code, straight after the switch."""
    state = _read_update_state(paths)
    if not state or state.get("result"):
        raise KitError("There's no update waiting to be finished.")
    print("Starting the new version. Core updates its database as it starts.")
    config = load_config(paths)
    try:
        apply(paths)
        problems = after_update_problems(paths, config)
    except KitError as error:
        problems = [f"It didn't start: {error}"]
    except subprocess.CalledProcessError:
        problems = [f"It didn't start: {name}" for name in unhealthy_containers()] or ["It didn't start, and Docker's message above says why"]
        _show_logs_of_unhealthy()
    if problems:
        state["result"] = "failed"
        _write_update_state(paths, state)
        print(f"\nTHE UPDATE TO {state['to']} FAILED ITS CHECKS:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            f"\nNothing has been undone or cleaned up. The public sites are unaffected."
            f"\nTo go back to {state['from']}, as it was at {state['started']}, run:"
            f"\n\n  kit update --rollback\n"
            f"\nThat puts back the backup taken just before the update. Anything written since then is lost.",
            file=sys.stderr,
        )
        return 1
    free_before = shutil.disk_usage("/").free
    _clean_up_images(state)
    freed = (shutil.disk_usage("/").free - free_before) / 1024**3
    state["result"] = "done"
    _write_update_state(paths, state)
    print(f"\nUpdated to {state['to']}, and every check passed. Cleaning up freed {max(freed, 0):.1f} GB.")
    print(f"The images from {state['from']} are kept. If something turns out wrong later: kit update --rollback")
    print("That puts back the backup taken before the update, so anything written since would be lost.")
    return 0


def rollback_update(paths: Paths, yes: bool) -> None:
    """Back to the code and the backup from just before the last update. The old code
    does the restore, as it's the one that knows how its own backup goes back."""
    state = _read_update_state(paths)
    if not state or state.get("result") == "rolled back":
        raise KitError("There's no update to roll back.")
    print(f"This goes back to {state['from']}, with the backup taken {state['started']}, just before updating to {state['to']}.")
    if state.get("result") == "done":
        print("That update finished and passed its checks, so people may have used it since.")
    print("Anything written since the backup is lost.\n")
    _confirm("Go back?", yes)
    _git("checkout", "-q", "--detach", state["from_commit"])
    state["result"] = "rolled back"
    _write_update_state(paths, state)
    kit = str(ROOT / "kit" / "kit.py")
    os.execv(sys.executable, [sys.executable, kit, "restore", "--force", "--snapshot", state["snapshot"]])


def _update_status(paths: Paths) -> int:
    """For `kit status`: which release this is, and whether there's a newer one."""
    current = running_version()
    state = _read_update_state(paths)
    if state and state.get("result") in (None, "failed"):
        print(f"Sällskap {current}. THE UPDATE TO {state['to']} DIDN'T FINISH. Go back with: kit update --rollback")
        return 1
    try:
        newest = (available_releases() or [None])[-1]
    except KitError:
        print(f"Sällskap {current}. Couldn't check for a newer release.")
        return 0
    if newest and parse_release(newest) > _base(current):
        print(f"Sällskap {current}. {newest} is out: see what's in it with kit update, which asks before changing anything.")
    else:
        print(f"Sällskap {current}, the newest release.")
    return 0


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


def _age_in_days(when: float) -> float:
    return max(0.0, (time.time() - when) / 86400)


def _spoken_age(days: float) -> str:
    """Rounded to something a person would say. Minutes matter here: a backup taken a
    moment ago shouldn't read as "0 hours"."""
    def counted(number: int, unit: str) -> str:
        return f"{number} {unit}{'' if number == 1 else 's'} ago"

    if days * 24 < 1:
        return counted(max(1, round(days * 24 * 60)), "minute")
    if days < 1:
        return counted(round(days * 24), "hour")
    return counted(round(days), "day")


def _backup_status(paths: Paths) -> int:
    """When the last backup was taken, and when a copy last left the server. Both are
    problems when they get old: a backup nobody has ever taken off the server doesn't
    survive losing the server. See docs/restore.md."""
    print("Backups:")
    if shutil.which("restic") is None:
        print("  warn restic isn't installed, so no backups are being taken. Add it: apt-get install restic")
        return 1
    if not _repo_exists(paths) or not _read_env(paths.backup_env).get("RESTIC_PASSWORD"):
        print("  warn no backups have been taken yet. Take one: kit backup")
        return 1

    problems = 0
    output = _restic_read(paths, "snapshots", "--latest", "1", "--tag", BACKUP_TAG, "--json")
    snapshots = json.loads(output) if output else []
    if not snapshots:
        print("  warn the backups can't be read, or hold nothing. Take one: kit backup")
        return 1
    # restic reports nanoseconds, which datetime doesn't read.
    stamp = re.sub(r"(\.\d{6})\d+", r"\1", snapshots[-1]["time"])
    days = _age_in_days(datetime.fromisoformat(stamp).timestamp())
    if days > BACKUP_STALE_DAYS:
        print(f"  warn the last backup was {_spoken_age(days)}. Check: systemctl status sallskap-backup.timer")
        problems += 1
    else:
        print(f"  ok   last backup {_spoken_age(days)}")

    note = paths.backup_dir / "last-export"
    try:
        when = float(note.read_text("utf-8").strip())
    except (OSError, ValueError):
        print(f"  warn no copy has ever left this server. Take one: kit backup export {_export_name()}")
        return problems + 1
    days = _age_in_days(when)
    if days > EXPORT_STALE_DAYS:
        print(f"  warn the last copy off this server was {_spoken_age(days)}. Take one: kit backup export {_export_name()}")
        problems += 1
    else:
        print(f"  ok   last copy off this server {_spoken_age(days)}")
    return problems


def status(paths: Paths) -> int:
    config = load_config(paths)
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=10) as response:
            server_ip = response.read().decode().strip()
    except OSError as error:
        raise KitError(f"Couldn't look up this server's public address: {error}") from error

    print(f"Primary domain: {config.domain}\nServer address: {server_ip}\n")
    if _vault_emergency_address():  # even if the module is off
        print("The vault's emergency copy is running, which takes master passwords. Stop it when you're done: kit emergency vault --off\n")
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
    print()
    problems += _backup_status(paths)
    problems += _update_status(paths)
    return 1 if problems else 0


# ---- Command line ------------------------------------------------------------


class _ShowVersion(argparse.Action):
    """Worked out only when asked, so other commands don't run git first."""

    def __init__(self, option_strings, dest, **kwargs):
        super().__init__(option_strings, dest, nargs=0, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        print(f"Sällskap {running_version()}")
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kit", description="Operate a Sällskap installation.")
    parser.add_argument("--version", action=_ShowVersion, help="show which release this is")
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

    emergency = commands.add_parser("emergency", help="get into a tool while core is down")
    emergency = emergency.add_subparsers(dest="emergency_tool", required=True)
    vault = emergency.add_parser("vault", help="start a private copy of the vault that takes master passwords, reached over SSH")
    vault.add_argument("--off", action="store_true", help="stop it again")
    emergency.add_parser("git", help="make a token on someone's own Forgejo account").add_argument("username")

    backup = commands.add_parser("backup", help="take a backup, or export one to take off this server")
    backup_actions = backup.add_subparsers(dest="backup_action")
    export = backup_actions.add_parser("export", help="write the backups as one file, or - for standard output")
    export.add_argument("destination", nargs="?", default="-", help="a file, a folder, or - for standard output")
    restore_command = commands.add_parser("restore", help="put a backup back onto a fresh server")
    restore_command.add_argument(
        "source", nargs="?", help="an exported file, or - to read one from standard input. Left out: use the backups on this server"
    )
    restore_command.add_argument(
        "--from", dest="from_repo", metavar="REPO",
        help="read the backups straight from where they're kept, such as sftp:backups@host:/sallskap, instead of a file",
    )
    restore_command.add_argument("--force", action="store_true", help="replace an installation that's already on this server")
    restore_command.add_argument(
        "--across-versions", action="store_true", help="go ahead when the backup was taken with other tool versions"
    )
    restore_command.add_argument("--snapshot", metavar="ID", help="restore this backup rather than the newest")

    update_command = commands.add_parser("update", help="move to a newer release, after taking a backup")
    update_command.add_argument("version", nargs="?", help="the release to move to, such as 0.7.1. The newest if left out")
    update_command.add_argument("--yes", action="store_true", help="go ahead without asking first")
    update_command.add_argument("--unreleased", metavar="REF", help="move to a branch or commit instead of a release: test servers only")
    update_command.add_argument("--rollback", action="store_true", help="go back to before the last update, from the backup it took")
    update_command.add_argument("--finish", action="store_true", help=argparse.SUPPRESS)  # run by the new code after the switch

    tool = commands.add_parser("tool", help="manage the tools that can use the login service")
    tool = tool.add_subparsers(dest="tool_action", required=True)
    tool_add = tool.add_parser("add", help="register a tool, and show its settings and secret once")
    tool_add.add_argument("name")
    tool_add.add_argument("callback", help="the exact https address the tool wants people sent back to")
    tool_add.add_argument("--group", action="append", default=[], help="only this group may log in (repeatable). Without any, every account can")
    tool_add.add_argument("--session-hours", type=int, help="for a tool limited to groups whose sessions last as long as its access token (1-24)")
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
                rules = [f"--group={group}" for group in args.group]  # the = stops it being read as an option
                if args.session_hours is not None:
                    rules.append(f"--session-hours={args.session_hours}")
                return _manage("tool", "add", _check_tool_name(args.name), _check_callback(args.callback), *rules)
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
        elif args.command == "backup":
            if args.backup_action == "export":
                export_backup(paths, args.destination)
            else:
                run_backup(paths)
        elif args.command == "restore":
            restore(paths, args.source, args.force, args.across_versions, args.from_repo, args.snapshot)
        elif args.command == "update":
            if args.finish:
                return finish_update(paths)
            if args.rollback:
                rollback_update(paths, args.yes)
            else:
                if args.version and args.unreleased:
                    raise KitError("Give either a release or --unreleased, not both.")
                update(paths, args.version, args.unreleased, args.yes)
        elif args.command == "emergency" and args.emergency_tool == "git":
            print(f"Token for {args.username}, for the API and Git:\n\n{git_emergency_token(paths, args.username)}\n")
            print("Delete it under Settings, Applications once core is back. Nothing else can.")
        elif args.command == "emergency":
            if args.off:
                print("Stopped the vault's emergency copy." if vault_emergency_off(paths) else "It wasn't running.")
            else:
                address = vault_emergency_on(paths)
                print("The vault's emergency copy is running. Only someone with SSH on this server can reach it.\n")
                print("On your own computer, add this to how you normally connect to this server:\n")
                print(f"  ssh -L {VAULT_EMERGENCY_PORT}:{address}:8080 ...\n")
                print(f"Then open https://localhost:{VAULT_EMERGENCY_PORT} and log in with your address and master password.")
                print("Your browser will warn about the certificate, as it was made just now for this. Go ahead: the tunnel protects it.")
                print("Stop it when you're done: kit emergency vault --off")
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
