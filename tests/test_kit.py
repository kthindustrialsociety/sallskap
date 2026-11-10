"""Run from the repo root with: python3 -m unittest discover -s tests"""

import base64
import contextlib
import importlib.util
import io
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("kit", ROOT / "kit" / "kit.py")
kit = importlib.util.module_from_spec(spec)
import sys  # noqa: E402

sys.modules["kit"] = kit
spec.loader.exec_module(kit)

CONFIG = """\
# keep this comment
[association]
name = "Example Society"
domain = "example.org"

[mail]
from = "no-reply@example.org"

[[website.sites]]
domain  = "example.org"
aliases = ["example.se", "old-name.se"]

[[website.sites]]
domain = "links.example.org"

[modules]
website = true
"""


def make_tar(files: dict[str, bytes], links: dict[str, str] | None = None) -> io.BytesIO:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tar.addfile(info)
    buffer.seek(0)
    return buffer


class ParseConfig(unittest.TestCase):
    def test_redirects_follow_the_www_rule(self):
        main, links = kit.parse_config(CONFIG).sites
        self.assertEqual(
            main.redirects,
            ["www.example.org", "example.se", "old-name.se", "www.example.se", "www.old-name.se"],
        )
        self.assertEqual(links.redirects, [])

    def test_www_can_be_set_explicitly(self):
        self.assertIn("www.example.co.uk", kit.Site("example.co.uk", www=True).redirects)
        self.assertNotIn("www.example.org", kit.Site("example.org", www=False).redirects)

    def assert_refused(self, site_block: str, message: str):
        text = '[association]\ndomain = "example.org"\n' + site_block
        with self.assertRaisesRegex(kit.KitError, message):
            kit.parse_config(text)

    def test_refuses_a_duplicate_hostname(self):
        self.assert_refused(
            '[[website.sites]]\ndomain = "a.org"\n[[website.sites]]\ndomain = "b.org"\naliases = ["a.org"]\n',
            "more than once",
        )

    def test_refuses_a_www_hostname_implied_twice(self):
        self.assert_refused(
            '[[website.sites]]\ndomain = "a.org"\naliases = ["www.a.org"]\n',
            "more than once",
        )

    def test_refuses_service_hostnames(self):
        self.assert_refused('[[website.sites]]\ndomain = "app.example.org"\n', "reserved")
        self.assert_refused('[[website.sites]]\ndomain = "git.example.org"\n', "reserved")

    def test_refuses_a_www_site_domain(self):
        self.assert_refused('[[website.sites]]\ndomain = "www.a.org"\n', "bare domain")

    def test_refuses_unknown_settings(self):
        self.assert_refused('[[website.sites]]\ndomain = "a.org"\nalias = ["b.org"]\n', "Unknown")

    def test_refuses_text_that_could_inject_web_server_config(self):
        for bad in ("a.org {", "a.org\\nb.org", "A.ORG", "a.org/x", "*.a.org", "{uri}.org"):
            self.assert_refused(f'[[website.sites]]\ndomain = "{bad}"\n', "valid lowercase")

    def test_refuses_a_mail_address_that_could_inject_config(self):
        text = '[association]\ndomain = "a.org"\n[mail]\nfrom = "x@a.org\\n}"\n'
        with self.assertRaisesRegex(kit.KitError, "mail address"):
            kit.parse_config(text)

    def test_needs_a_primary_domain(self):
        with self.assertRaisesRegex(kit.KitError, "needs a domain"):
            kit.parse_config('[association]\nname = "x"\n')

    def test_reports_toml_errors_plainly(self):
        with self.assertRaisesRegex(kit.KitError, "syntax error"):
            kit.parse_config("[association\n")


class RenderCaddyfile(unittest.TestCase):
    def setUp(self):
        self.text = kit.render_caddyfile(kit.parse_config(CONFIG))

    def test_routes_the_app_hostname_to_core(self):
        block = self.text.split("app.example.org {\n")[1].split("\n}")[0]
        self.assertIn("reverse_proxy core:8000", block)

    def test_logged_in_pages_do_not_get_the_public_site_caching(self):
        block = self.text.split("app.example.org {\n")[1].split("\n}")[0]
        self.assertNotIn("website_defaults", block)
        self.assertNotIn("Cache-Control", block)

    def test_serves_each_site_from_its_current_release(self):
        self.assertIn("root * /srv/website/example.org/current", self.text)
        self.assertIn("root * /srv/website/links.example.org/current", self.text)

    def test_aliases_redirect_permanently_and_keep_the_path(self):
        self.assertIn(
            "www.example.org, example.se, old-name.se, www.example.se, www.old-name.se {\n"
            "\tredir https://example.org{uri} permanent\n}",
            self.text,
        )

    def test_spa_fallback_is_on_by_default_and_can_be_turned_off(self):
        self.assertIn("/index.html\n", self.text)
        config = kit.parse_config('[association]\ndomain = "a.org"\n[[website.sites]]\ndomain = "a.org"\nspa = false\n')
        self.assertNotIn("try_files", kit.render_caddyfile(config))

    def test_every_site_gets_the_defaults(self):
        self.assertEqual(self.text.count("import website_defaults"), 2)
        self.assertIn("(website_defaults) {", self.text)
        self.assertIn("encode zstd gzip", self.text)

    def test_renders_with_no_sites(self):
        text = kit.render_caddyfile(kit.parse_config('[association]\ndomain = "a.org"\n'))
        self.assertIn("(website_defaults)", text)
        self.assertNotIn("file_server", text.split("(website_defaults)")[0])


class EditConfig(unittest.TestCase):
    def test_add_then_remove_restores_the_config(self):
        site = kit.Site("new.org", ("alias.org",), spa=False, www=False)
        added = kit.add_site_block(CONFIG, site)
        self.assertEqual(kit.parse_config(added).sites[-1], site)
        self.assertEqual(tomllib.loads(kit.remove_site_block(added, "new.org")), tomllib.loads(CONFIG))

    def test_remove_keeps_other_sites_tables_and_comments(self):
        text = kit.remove_site_block(CONFIG, "example.org")
        self.assertIn("# keep this comment", text)
        self.assertEqual([s.domain for s in kit.parse_config(text).sites], ["links.example.org"])
        self.assertTrue(tomllib.loads(text)["modules"]["website"])

    def test_remove_does_not_match_on_a_longer_domain(self):
        with self.assertRaisesRegex(kit.KitError, "No site"):
            kit.remove_site_block(CONFIG, "links.example")

    def test_remove_of_an_unknown_site_is_an_error(self):
        with self.assertRaisesRegex(kit.KitError, "No site"):
            kit.remove_site_block(CONFIG, "nope.org")


class Sites(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.paths = kit.Paths(base / "etc", base / "srv", base / "home")
        self.paths.config_dir.mkdir()
        self.paths.config.write_text(CONFIG)
        self.site_dir = self.paths.site_dir("example.org")
        self.site_dir.mkdir(parents=True)

    def publish(self, body: bytes = b"<h1>hi</h1>"):
        return kit.receive(self.paths, "example.org", make_tar({"index.html": body, "assets/a-12345678.js": b"x"}))

    def served(self) -> bytes:
        return (self.site_dir / "current" / "index.html").read_bytes()

    def test_publish_goes_live(self):
        self.publish(b"one")
        self.assertEqual(self.served(), b"one")
        self.assertTrue((self.site_dir / "current" / "assets" / "a-12345678.js").is_file())

    def test_published_files_are_readable_by_the_web_server(self):
        release = self.publish()
        self.assertEqual((release / "index.html").stat().st_mode & 0o777, 0o644)
        self.assertEqual((release / "assets").stat().st_mode & 0o777, 0o755)

    def test_current_is_a_relative_link(self):
        # Caddy sees the same tree at another path inside its container.
        self.publish()
        self.assertFalse((self.site_dir / "current").readlink().is_absolute())

    def test_tar_of_a_dot_directory_works(self):
        buffer = io.BytesIO()
        with tempfile.TemporaryDirectory() as source:
            Path(source, "index.html").write_bytes(b"dot")
            with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
                tar.add(source, arcname=".")
        buffer.seek(0)
        kit.receive(self.paths, "example.org", buffer)
        self.assertEqual(self.served(), b"dot")

    def assert_refused_and_unchanged(self, stream, message):
        self.publish(b"good")
        before = sorted(p.name for p in (self.site_dir / "releases").iterdir())
        with self.assertRaisesRegex(kit.KitError, message):
            kit.receive(self.paths, "example.org", stream)
        self.assertEqual(self.served(), b"good")
        self.assertEqual(sorted(p.name for p in (self.site_dir / "releases").iterdir()), before)

    def test_refuses_paths_that_escape_the_release(self):
        self.assert_refused_and_unchanged(make_tar({"index.html": b"x", "../../evil": b"x"}), "unsafe")
        self.assertFalse(self.site_dir.parent.joinpath("evil").exists())

    def test_refuses_absolute_paths(self):
        self.assert_refused_and_unchanged(make_tar({"/tmp/sallskap-evil": b"x"}), "unsafe")

    def test_refuses_symlinks(self):
        self.assert_refused_and_unchanged(make_tar({"index.html": b"x"}, {"leak": "/etc/passwd"}), "link")

    def test_refuses_an_upload_without_index_html(self):
        self.assert_refused_and_unchanged(make_tar({"other.html": b"x"}), "index.html")

    def test_refuses_something_that_is_not_a_tar(self):
        self.assert_refused_and_unchanged(io.BytesIO(b"not a tar at all"), "valid tar")

    def test_refuses_an_upload_over_the_size_limit(self):
        original = kit.MAX_UPLOAD_BYTES
        kit.MAX_UPLOAD_BYTES = 5
        self.addCleanup(setattr, kit, "MAX_UPLOAD_BYTES", original)
        with self.assertRaisesRegex(kit.KitError, "too large"):
            kit.receive(self.paths, "example.org", make_tar({"index.html": b"far too long"}))

    def test_refuses_an_unknown_site(self):
        with self.assertRaisesRegex(kit.KitError, "No site"):
            kit.receive(self.paths, "nope.org", make_tar({"index.html": b"x"}))

    def test_refuses_a_domain_that_walks_out_of_the_web_folder(self):
        with self.assertRaisesRegex(kit.KitError, "valid lowercase"):
            kit.receive(self.paths, "../../etc", make_tar({"index.html": b"x"}))

    def test_keeps_only_recent_releases(self):
        for i in range(kit.RELEASES_KEPT + 3):
            self.publish(str(i).encode())
        self.assertEqual(len(kit._releases(self.site_dir)), kit.RELEASES_KEPT)
        self.assertEqual(self.served(), str(kit.RELEASES_KEPT + 2).encode())

    def test_rollback_goes_back_one_release_at_a_time(self):
        for body in (b"one", b"two", b"three"):
            self.publish(body)
        kit.rollback(self.paths, "example.org")
        self.assertEqual(self.served(), b"two")
        kit.rollback(self.paths, "example.org")
        self.assertEqual(self.served(), b"one")
        with self.assertRaisesRegex(kit.KitError, "no earlier release"):
            kit.rollback(self.paths, "example.org")

    def test_publishing_after_a_rollback_goes_live(self):
        self.publish(b"one")
        self.publish(b"two")
        kit.rollback(self.paths, "example.org")
        self.publish(b"three")
        self.assertEqual(self.served(), b"three")

    def test_init_creates_a_private_config_once(self):
        self.paths.config.unlink()
        kit.init_config(self.paths, "example.org")
        self.assertEqual(kit.load_config(self.paths).domain, "example.org")
        self.assertEqual(self.paths.config.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(kit.KitError, "already exists"):
            kit.init_config(self.paths, "example.org")

    def test_add_site_writes_config_and_folder(self):
        kit.add_site(self.paths, kit.Site("new.org", ("alias.org",)))
        self.assertTrue(self.paths.site_dir("new.org").is_dir())
        self.assertEqual(kit.load_config(self.paths).sites[-1].aliases, ("alias.org",))
        self.assertEqual(self.paths.config.stat().st_mode & 0o777, 0o600)

    def test_add_site_refuses_a_collision_and_changes_nothing(self):
        with self.assertRaisesRegex(kit.KitError, "more than once"):
            kit.add_site(self.paths, kit.Site("example.se"))
        self.assertEqual(self.paths.config.read_text(), CONFIG)
        self.assertFalse(self.paths.site_dir("example.se").exists())

    def test_deploy_key_is_limited_to_one_site_and_replaceable(self):
        first = kit.create_deploy_key(self.paths, "example.org")
        self.assertIn("PRIVATE KEY", first)
        kit.create_deploy_key(self.paths, "links.example.org")
        lines = self.paths.authorized_keys.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith('restrict,command="/usr/local/bin/kit site receive example.org" ssh-ed25519'))

        second = kit.create_deploy_key(self.paths, "example.org")
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.paths.authorized_keys.read_text().splitlines()), 2)
        self.assertEqual(self.paths.authorized_keys.stat().st_mode & 0o777, 0o600)

    def test_remove_site_revokes_only_its_key_and_keeps_files(self):
        kit.create_deploy_key(self.paths, "example.org")
        kit.create_deploy_key(self.paths, "links.example.org")
        self.publish()
        kit.remove_site(self.paths, "example.org")
        keys = self.paths.authorized_keys.read_text()
        self.assertNotIn("receive example.org", keys)
        self.assertIn("receive links.example.org", keys)
        self.assertTrue((self.site_dir / "current" / "index.html").is_file())
        self.assertEqual([s.domain for s in kit.load_config(self.paths).sites], ["links.example.org"])


class Core(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.paths = kit.Paths(base / "etc", base / "srv", base / "home", base / "core")
        self.config = kit.parse_config(CONFIG)

    def test_secrets_are_private_and_use_the_primary_domain(self):
        kit.prepare_core(self.paths, self.config)
        text = self.paths.core_env.read_text()
        self.assertEqual(self.paths.core_env.stat().st_mode & 0o777, 0o600)
        self.assertIn("SALLSKAP_DOMAIN=example.org\n", text)
        self.assertRegex(text, r"DJANGO_SECRET_KEY=[\w-]{60,}\n")

    def test_it_makes_a_real_signing_key_on_one_line(self):
        kit.prepare_core(self.paths, self.config)
        encoded = re.search(r"OIDC_SIGNING_KEY=(.+)\n", self.paths.core_env.read_text()).group(1)
        self.assertIn(b"BEGIN PRIVATE KEY", base64.b64decode(encoded))

    def test_core_data_folder_is_private(self):
        kit.prepare_core(self.paths, self.config)
        self.assertEqual(self.paths.core_dir.stat().st_mode & 0o777, 0o700)

    def test_each_installation_gets_its_own_secret_key(self):
        other = kit.Paths(self.paths.config_dir / "other", self.paths.website_dir, self.paths.deploy_home, self.paths.core_dir)
        kit.prepare_core(self.paths, self.config)
        kit.prepare_core(other, self.config)
        self.assertNotEqual(self.paths.core_env.read_text(), other.core_env.read_text())

    def test_running_setup_again_changes_nothing(self):
        kit.prepare_core(self.paths, self.config)
        before = self.paths.core_env.read_text()
        modified = self.paths.core_env.stat().st_mtime_ns
        kit.prepare_core(self.paths, self.config)
        self.assertEqual(self.paths.core_env.read_text(), before)
        self.assertEqual(self.paths.core_env.stat().st_mtime_ns, modified)

    def test_changing_the_domain_keeps_the_secret_key(self):
        kit.prepare_core(self.paths, self.config)
        key = re.search(r"DJANGO_SECRET_KEY=(.+)", self.paths.core_env.read_text()).group(1)
        signing_key = re.search(r"OIDC_SIGNING_KEY=(.+)", self.paths.core_env.read_text()).group(1)
        kit.prepare_core(self.paths, kit.parse_config('[association]\ndomain = "new.org"\n'))
        text = self.paths.core_env.read_text()
        self.assertIn("SALLSKAP_DOMAIN=new.org\n", text)
        self.assertIn(f"DJANGO_SECRET_KEY={key}\n", text)
        self.assertIn(f"OIDC_SIGNING_KEY={signing_key}\n", text)

    def test_an_installation_from_before_the_login_service_gets_a_signing_key(self):
        self.paths.config_dir.mkdir()
        self.paths.core_env.write_text("SALLSKAP_DOMAIN=example.org\nDJANGO_SECRET_KEY=keep-this\n")
        kit.prepare_core(self.paths, self.config)
        text = self.paths.core_env.read_text()
        self.assertIn("DJANGO_SECRET_KEY=keep-this\n", text)
        self.assertIn("OIDC_SIGNING_KEY=", text)

    def test_secrets_stay_out_of_git(self):
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True).stdout
        for name in ("core.env", "git.env", "vault.env"):
            self.assertNotIn(name, tracked)
            self.assertFalse(list(ROOT.glob(f"**/{name}")))

    def test_email_addresses_are_checked_before_reaching_the_container(self):
        self.assertEqual(kit._check_email(" First.Admin@Example.org "), "first.admin@example.org")
        for bad in ("nope", "--settings=x@example.org", ""):
            with self.assertRaisesRegex(kit.KitError, "valid email"):
                kit._check_email(bad)

    def test_kit_leaves_the_real_address_check_to_django(self):
        """Otherwise someone an Operator added on the admin page couldn't get a link."""
        self.assertEqual(kit._check_email("o'brien@kth.se"), "o'brien@kth.se")

    def test_the_uid_given_the_data_folder_is_the_one_core_runs_as(self):
        dockerfile = (ROOT / "core" / "Dockerfile").read_text()
        self.assertIn(f"USER {kit.CORE_UID}:{kit.CORE_UID}", dockerfile)
        self.assertIn(f"--uid {kit.CORE_UID} ", dockerfile)


class Docker(unittest.TestCase):
    """Runs kit's commands with Docker replaced, recording what it was asked to do."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.paths = kit.Paths(base / "etc", base / "srv", base / "home", base / "core")
        self.paths.config_dir.mkdir()
        self.paths.config.write_text('[association]\ndomain = "example.org"\n')
        self.calls = []
        self.broken = ()  # services that fail to start

        real_run = subprocess.run

        def fake_run(command, *args, **kwargs):
            if command[0] != "docker":
                return real_run(command, *args, **kwargs)
            self.calls.append(command)
            if "up" in command and any(service in command for service in self.broken):
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0, "", "")

        for patcher in (
            mock.patch.object(kit.subprocess, "run", fake_run),
            mock.patch.object(kit, "Paths", return_value=self.paths),
            mock.patch.object(kit, "status", return_value=0),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_kit(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = kit.main(list(args))
        return code, out.getvalue()

    def starts(self):
        return [call[call.index("up") :] for call in self.calls if "up" in call]

    def test_website_commands_only_touch_caddy(self):
        self.run_kit("site", "add", "x.org")
        self.run_kit("site", "remove", "x.org")
        self.assertTrue(self.starts())
        for start in self.starts():
            self.assertIn("caddy", start)
            self.assertNotIn("core", start)
            self.assertNotIn("--wait", start)

    def test_a_broken_core_cannot_stop_a_site_being_added(self):
        self.broken = ("core",)
        code, out = self.run_kit("site", "add", "x.org")
        self.assertEqual(code, 0)
        self.assertIn("PRIVATE KEY", out)

    def test_a_new_deploy_key_is_shown_even_if_the_web_server_fails(self):
        self.broken = ("caddy",)
        code, out = self.run_kit("site", "add", "x.org")
        self.assertEqual(code, 1)
        self.assertIn("PRIVATE KEY", out)

    def test_a_deploy_key_written_to_a_file_is_the_key_alone(self):
        """So `ssh root@server 'kit site key x' > file` saves a key that works. Measured on
        the first real install: one copied out of a terminal lost its BEGIN and END lines."""
        self.run_kit("site", "add", "x.org")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(kit.main(["site", "key", "x.org"]), 0)
        lines = out.getvalue().strip().splitlines()
        self.assertEqual(lines[0], "-----BEGIN OPENSSH PRIVATE KEY-----")
        self.assertEqual(lines[-1], "-----END OPENSSH PRIVATE KEY-----")
        self.assertIn("ssh-keygen -y -f", err.getvalue())

    def test_on_a_screen_the_key_comes_with_what_it_is_for(self):
        out = io.StringIO()
        out.isatty = lambda: True
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            kit.main(["site", "add", "x.org"])
        text = out.getvalue()
        self.assertIn("-----BEGIN OPENSSH PRIVATE KEY-----", text)
        self.assertIn("can publish this one site, and nothing else", text)
        self.assertIn("'kit site key x.org' > x.org-deploy-key", text)

    def test_apply_starts_caddy_then_waits_for_core(self):
        self.run_kit("apply")
        self.assertEqual(self.starts(), [["up", "-d", "caddy"], ["up", "-d", "--wait", "core"], ["up", "-d", "--wait", "sync"]])

    def test_apply_only_rebuilds_when_asked(self):
        builds = lambda: [call for call in self.calls if call[-2:] == ["build", "core"]]  # noqa: E731
        self.run_kit("apply")
        self.assertEqual(builds(), [])
        self.run_kit("apply", "--build")
        self.assertEqual(len(builds()), 1)
        self.assertLess(self.calls.index(builds()[0]), next(i for i, c in enumerate(self.calls) if "core" in c and "up" in c and i > self.calls.index(builds()[0])))

    def test_compose_is_quiet_and_never_asks_a_registry_for_core(self):
        """Measured on the first real install: Compose's build log was a page long, and it
        asked Docker Hub for core's image, which only exists on the server."""
        self.run_kit("apply")
        self.assertTrue(all(call[2:4] == ["--progress", "quiet"] for call in self.calls if call[1] == "compose"))
        core = compose_services()["core"]
        self.assertIn("pull_policy: never", core)

    def test_a_broken_core_cannot_hold_up_the_web_server(self):
        self.run_kit("apply")
        self.calls.clear()
        self.broken = ("core",)
        code, _ = self.run_kit("apply")
        self.assertEqual(code, 1)
        self.assertTrue(any("reload" in call for call in self.calls))

    def test_init_shows_what_dns_is_needed(self):
        self.paths.config.unlink()
        self.run_kit("init", "example.org")
        kit.status.assert_called_once()

    def test_admin_passes_on_only_what_was_asked_for(self):
        self.run_kit("admin", "o'brien@kth.se")
        self.assertEqual(self.calls[-1][-2:], ["create_admin", "o'brien@kth.se"])
        self.run_kit("admin", "o'brien@kth.se", "--reset-2fa")
        self.assertEqual(self.calls[-1][-3:], ["create_admin", "o'brien@kth.se", "--reset-2fa"])


    def test_tool_commands_reach_core(self):
        self.run_kit("tool", "add", "git", "https://git.example.org/callback")
        self.assertEqual(self.calls[-1][-5:], ["manage.py", "tool", "add", "git", "https://git.example.org/callback"])
        self.run_kit("tool", "list")
        self.assertEqual(self.calls[-1][-2:], ["tool", "list"])
        self.run_kit("tool", "remove", "git")
        self.assertEqual(self.calls[-1][-3:], ["tool", "remove", "git"])

    def test_a_tool_can_be_limited_to_groups(self):
        self.run_kit("tool", "add", "vault", "https://vault.example.org/callback", "--group", "board", "--group", "operator")
        self.assertEqual(self.calls[-1][-5:], ["add", "vault", "https://vault.example.org/callback", "--group=board", "--group=operator"])

    def test_a_tool_can_have_longer_sessions(self):
        self.run_kit("tool", "add", "vault", "https://vault.example.org/callback", "--group", "board", "--session-hours", "12")
        self.assertEqual(self.calls[-1][-2:], ["--group=board", "--session-hours=12"])

    def test_tool_commands_refuse_what_could_be_read_as_an_option(self):
        for args in (
            ("tool", "add", "--help=x", "https://git.example.org/callback"),
            ("tool", "add", "git", "--settings=x"),
            ("tool", "add", "Git Tool", "https://git.example.org/callback"),
            ("tool", "add", "git", "http://git.example.org/callback"),
            ("tool", "remove", "--force"),
        ):
            with self.subTest(args), contextlib.redirect_stderr(io.StringIO()):
                try:
                    code, _ = self.run_kit(*args)
                except SystemExit as stopped:  # argparse refuses some of these itself
                    code = stopped.code
                self.assertNotEqual(code, 0)
        self.assertEqual(self.calls, [])


class Modules(unittest.TestCase):
    """Runs against a made-up module, with Docker replaced."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.root = base / "root"
        (self.root / "modules" / "website").mkdir(parents=True)
        (self.root / "modules" / "website" / "module.toml").write_text('name = "website"\n')
        (self.root / "VERSION").write_text("1.2.3\n")
        (self.root / "modules" / "website" / "site.caddy").write_text("(website_defaults) {\n}\n")
        demo = self.root / "modules" / "demo"
        demo.mkdir()
        (demo / "module.toml").write_text('name = "demo"\ndescription = "A demo"\nsubdomain = "demo"\n')
        (demo / "compose.yml").write_text("services:\n")
        (demo / "site.caddy").write_text("__HOST__ {\n\treverse_proxy demo:80\n}\n")

        self.paths = kit.Paths(base / "etc", base / "srv", base / "home", base / "core")
        self.paths.config_dir.mkdir()
        self.paths.config.write_text('# keep this\n[association]\ndomain = "example.org"\n')
        self.calls = []
        self.broken = ()

        real_run = subprocess.run

        def fake_run(command, *args, **kwargs):
            if command[0] != "docker":
                return real_run(command, *args, **kwargs)
            self.calls.append(command)
            if "up" in command and any(service in " ".join(command) for service in self.broken):
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0, "", "")

        self.setup_calls = []
        for patcher in (
            mock.patch.object(kit, "ROOT", self.root),
            mock.patch.object(kit.subprocess, "run", fake_run),
            mock.patch.object(kit, "Paths", return_value=self.paths),
            mock.patch.object(kit, "status", return_value=0),
            mock.patch.dict(kit.PREPARE, {"demo": lambda paths, config: self.setup_calls.append("prepare")}),
            mock.patch.dict(kit.SETUP, {"demo": lambda paths, config: self.setup_calls.append("setup")}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_kit(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = kit.main(list(args))
        return code, out.getvalue() + err.getvalue()

    def config(self):
        return kit.load_config(self.paths)

    def caddyfile(self):
        return kit.render_caddyfile(self.config())

    def demo_commands(self):
        return [call for call in self.calls if any(part.endswith("demo/compose.yml") for part in call)]

    def test_config_lists_only_what_is_switched_on(self):
        text = '[association]\ndomain = "a.org"\n[modules]\ndemo = true\nchat = false\n'
        self.assertEqual(kit.parse_config(text).modules, ("demo",))

    def test_config_refuses_a_module_that_does_not_exist(self):
        with self.assertRaisesRegex(kit.KitError, "no module called nope"):
            kit.parse_config('[association]\ndomain = "a.org"\n[modules]\nnope = true\n')

    def test_config_refuses_something_other_than_true_or_false(self):
        with self.assertRaisesRegex(kit.KitError, "true or false"):
            kit.parse_config('[association]\ndomain = "a.org"\n[modules]\ndemo = "yes"\n')

    def test_a_module_name_cannot_walk_out_of_the_modules_folder(self):
        code, out = self.run_kit("enable", "../../etc")
        self.assertEqual(code, 1)
        self.assertIn("isn't a valid module name", out)

    def test_setting_a_module_adds_the_table_and_keeps_comments(self):
        text = kit.set_module("# keep\n[association]\ndomain = \"a.org\"\n", "demo", True)
        self.assertIn("# keep", text)
        self.assertTrue(tomllib.loads(text)["modules"]["demo"])

    def test_setting_a_module_changes_only_its_own_line(self):
        text = '[modules]\n# keep\nwebsite = true\ndemo = false\n\n[[website.sites]]\ndomain = "a.org"\n'
        changed = kit.set_module(text, "demo", True)
        self.assertEqual(changed, text.replace("demo = false", "demo = true"))

    def test_setting_a_module_stays_inside_its_table(self):
        text = '[modules]\nwebsite = true\n\n[other]\ndemo = "not this one"\n'
        raw = tomllib.loads(kit.set_module(text, "demo", True))
        self.assertTrue(raw["modules"]["demo"])
        self.assertEqual(raw["other"]["demo"], "not this one")

    def test_a_module_is_only_routed_when_enabled(self):
        self.assertNotIn("demo.example.org", self.caddyfile())
        self.paths.config.write_text(kit.set_module(self.paths.config.read_text(), "demo", True))
        self.assertIn("demo.example.org {\n\treverse_proxy demo:80\n}", self.caddyfile())
        self.assertNotIn("__HOST__", self.caddyfile())

    def test_a_module_hostname_is_reserved_even_when_it_is_off(self):
        with self.assertRaisesRegex(kit.KitError, "reserved"):
            kit.parse_config('[association]\ndomain = "example.org"\n[[website.sites]]\ndomain = "demo.example.org"\n')

    def test_enable_starts_everything_in_order_then_runs_setup(self):
        code, _ = self.run_kit("enable", "demo")
        self.assertEqual(code, 0)
        self.assertEqual(self.config().modules, ("demo",))
        self.assertEqual(self.paths.config.stat().st_mode & 0o777, 0o600)
        self.assertIn("# keep this", self.paths.config.read_text())
        starts = [" ".join(call[call.index("up") :]) for call in self.calls if "up" in call]
        self.assertEqual(starts, ["up -d caddy", "up -d --wait core", "up -d --wait", "up -d --wait sync"])
        self.assertEqual(self.setup_calls, ["prepare", "setup"])
        self.assertEqual(len(self.demo_commands()), 1)

    def test_a_module_joins_the_same_project_as_core(self):
        self.run_kit("enable", "demo")
        self.assertEqual(self.demo_commands()[0][:7], ["docker", "compose", "--progress", "quiet", "--project-name", "sallskap", "--project-directory"])
        self.assertIn("name: sallskap", (ROOT / "compose.yml").read_text())

    def test_enabling_twice_leaves_the_config_alone(self):
        self.run_kit("enable", "demo")
        before = self.paths.config.stat().st_mtime_ns
        self.run_kit("enable", "demo")
        self.assertEqual(self.paths.config.stat().st_mtime_ns, before)

    def test_a_broken_module_cannot_hold_up_core_or_the_sites(self):
        self.broken = ("demo",)
        code, out = self.run_kit("enable", "demo")
        self.assertEqual(code, 1)
        self.assertIn("a system command failed", out)
        starts = [" ".join(call[call.index("up") :]) for call in self.calls if "up" in call]
        self.assertEqual(starts[:2], ["up -d caddy", "up -d --wait core"])
        self.assertEqual(self.setup_calls, ["prepare"])

    def test_a_broken_module_doesnt_stop_the_worker_starting(self):
        """The other tools still have to hear about changes, and it may have a new token."""
        self.broken = ("demo",)
        code, _ = self.run_kit("enable", "demo")
        self.assertEqual(code, 1)
        starts = [" ".join(call[call.index("up") :]) for call in self.calls if "up" in call]
        self.assertEqual(starts[-1], "up -d --wait sync")

    def test_apply_does_what_a_module_needs_once_its_up(self):
        done = []
        with mock.patch.dict(kit.AFTER_START, {"demo": lambda paths, config: done.append(len(self.calls))}):
            self.run_kit("enable", "demo")
        started = next(i for i, call in enumerate(self.calls, 1) if any(part.endswith("demo/compose.yml") for part in call))
        self.assertEqual(done, [started])

    def test_enabling_again_finishes_after_a_failure(self):
        self.broken = ("demo",)
        self.run_kit("enable", "demo")
        self.broken = ()
        code, _ = self.run_kit("enable", "demo")
        self.assertEqual(code, 0)
        self.assertEqual(self.setup_calls, ["prepare", "prepare", "setup"])

    def test_apply_starts_enabled_modules_last_and_skips_setup(self):
        self.run_kit("enable", "demo")
        self.calls.clear()
        self.setup_calls.clear()
        self.run_kit("apply")
        self.assertEqual(self.demo_commands()[-1], self.calls[-2])
        self.assertEqual(self.calls[-1][-4:], ["up", "-d", "--wait", "sync"])  # after them, as preparing one can give it a token
        self.assertEqual(self.setup_calls, ["prepare"])

    def test_apply_ignores_modules_that_are_off(self):
        self.run_kit("apply")
        self.assertEqual(self.demo_commands(), [])
        self.assertEqual(self.setup_calls, [])

    def test_enable_refuses_a_module_with_nothing_to_start(self):
        code, out = self.run_kit("enable", "website")
        self.assertEqual(code, 1)
        self.assertIn("nothing to start", out)
        self.assertEqual(self.calls, [])

    def test_enable_refuses_an_unknown_module_and_changes_nothing(self):
        before = self.paths.config.read_text()
        code, out = self.run_kit("enable", "nope")
        self.assertEqual(code, 1)
        self.assertIn("no module called nope", out)
        self.assertEqual(self.paths.config.read_text(), before)

    def test_disable_stops_it_takes_the_route_away_and_keeps_its_data(self):
        self.run_kit("enable", "demo")
        self.calls.clear()
        code, _ = self.run_kit("disable", "demo")
        self.assertEqual(code, 0)
        self.assertEqual(self.config().modules, ())
        self.assertNotIn("demo.example.org", self.paths.caddyfile.read_text())
        removal = self.demo_commands()[-1]
        self.assertEqual(removal[-3:], ["rm", "--stop", "--force"])
        self.assertNotIn("--volumes", removal)

    def test_disabling_something_that_is_off_does_not_touch_it(self):
        code, out = self.run_kit("disable", "demo")
        self.assertEqual(code, 0)
        self.assertIn("isn't enabled", out)
        self.assertEqual(self.demo_commands(), [])

    def test_disable_tells_the_worker_to_stop_asking_it(self):
        self.run_kit("enable", "demo")
        self.calls.clear()
        self.run_kit("disable", "demo")
        self.assertEqual(self.calls[-1][-3:], ["up", "-d", "sync"])

    def test_a_failed_disable_can_be_finished_by_running_it_again(self):
        """It's still marked as on, so the next run reaches the container."""
        self.run_kit("enable", "demo")
        real_run = kit.subprocess.run

        def broken(command, *args, **kwargs):
            if "rm" in command:
                raise subprocess.CalledProcessError(1, command)
            return real_run(command, *args, **kwargs)

        with mock.patch.object(kit.subprocess, "run", broken):
            code, _ = self.run_kit("disable", "demo")
        self.assertEqual(code, 1)
        self.assertEqual(self.config().modules, ("demo",))
        self.calls.clear()
        self.assertEqual(self.run_kit("disable", "demo")[0], 0)
        self.assertEqual(self.demo_commands()[-1][-3:], ["rm", "--stop", "--force"])

    def test_a_web_server_problem_while_disabling_can_be_finished_too(self):
        """It's already marked as off, so running it again has to get the route away."""
        self.run_kit("enable", "demo")
        real_apply = kit.apply_web
        with mock.patch.object(kit, "apply_web", side_effect=kit.KitError("no")):
            self.assertEqual(self.run_kit("disable", "demo")[0], 1)
        self.assertIn("demo.example.org", self.paths.caddyfile.read_text())
        self.assertEqual(kit.apply_web, real_apply)
        self.run_kit("disable", "demo")
        self.assertNotIn("demo.example.org", self.paths.caddyfile.read_text())

    def test_disable_refuses_a_module_with_nothing_to_start(self):
        self.paths.config.write_text('[association]\ndomain = "example.org"\n[modules]\nwebsite = true\n')
        code, out = self.run_kit("disable", "website")
        self.assertEqual(code, 1)
        self.assertIn("nothing to start", out)
        self.assertEqual(self.config().modules, ("website",))
        self.assertEqual(self.calls, [])

    def test_leftover_warnings_are_turned_off(self):
        """Each module has its own Compose file, so the others look like leftovers."""
        self.run_kit("apply")
        self.assertEqual(kit.os.environ.get("COMPOSE_IGNORE_ORPHANS"), "true")


class GitSecrets(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.paths = kit.Paths(base / "etc", base / "srv", base / "home", base / "core", base / "git")
        self.config = kit.parse_config('[association]\ndomain = "example.org"\n')

    def settings(self):
        return kit._read_env(self.paths.git_env)

    def test_the_settings_use_the_primary_domain(self):
        kit.prepare_git(self.paths, self.config)
        self.assertEqual(self.settings()["FORGEJO__server__DOMAIN"], "git.example.org")
        self.assertEqual(self.settings()["FORGEJO__server__ROOT_URL"], "https://git.example.org/")

    def test_secrets_are_private_and_long(self):
        kit.prepare_git(self.paths, self.config)
        self.assertEqual(self.paths.git_env.stat().st_mode & 0o777, 0o600)
        self.assertGreater(len(self.settings()["FORGEJO__security__SECRET_KEY"]), 50)
        self.assertGreater(len(self.settings()["FORGEJO__security__INTERNAL_TOKEN"]), 50)

    def test_the_folders_forgejos_own_check_requires_exist_from_the_start(self):
        """Production's first update failed Forgejo's check: a fresh install has no repos,
        so no repositories folder, which Forgejo only makes with the first one."""
        kit.prepare_git(self.paths, self.config)
        self.assertTrue((self.paths.git_dir / "git" / "repositories").is_dir())
        self.assertTrue((self.paths.git_dir / "data" / "log").is_dir())

    def test_the_data_folder_is_private(self):
        kit.prepare_git(self.paths, self.config)
        self.assertEqual(self.paths.git_dir.stat().st_mode & 0o777, 0o700)

    def test_running_it_again_changes_nothing(self):
        kit.prepare_git(self.paths, self.config)
        before, modified = self.paths.git_env.read_text(), self.paths.git_env.stat().st_mtime_ns
        kit.prepare_git(self.paths, self.config)
        self.assertEqual(self.paths.git_env.read_text(), before)
        self.assertEqual(self.paths.git_env.stat().st_mtime_ns, modified)

    def test_changing_the_domain_keeps_the_secrets(self):
        """A new key would make Forgejo's stored login secret and 2FA records unreadable."""
        kit.prepare_git(self.paths, self.config)
        before = self.settings()
        kit.prepare_git(self.paths, kit.parse_config('[association]\ndomain = "new.org"\n'))
        after = self.settings()
        self.assertEqual(after["FORGEJO__server__DOMAIN"], "git.new.org")
        self.assertEqual(after["FORGEJO__security__SECRET_KEY"], before["FORGEJO__security__SECRET_KEY"])
        self.assertEqual(after["FORGEJO__security__INTERNAL_TOKEN"], before["FORGEJO__security__INTERNAL_TOKEN"])


class GitSetup(unittest.TestCase):
    """Runs the setup with a made-up Forgejo and core, recording what they were asked."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.paths = kit.Paths(base / "etc", base / "srv", base / "home", base / "core", base / "git", base / "vault")
        self.calls = []
        self.sources = ""
        self.tools = ""
        self.users = ""
        self.org_exists = False
        self.teams = ["Owners"]
        self.refuse = None  # part of a command to answer with a failure
        self.dead_token = None  # one Forgejo no longer knows

        def fake_run(command, *args, **kwargs):
            self.calls.append((command, kwargs.get("input")))
            return self.answer(command, kwargs.get("input"))

        patcher = mock.patch.object(kit.subprocess, "run", fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = kit.parse_config('[association]\ndomain = "kthis.example"\n')

    def answer(self, command, body):
        line = " ".join(command)
        done = lambda out="": subprocess.CompletedProcess(command, 0, out, "")
        if self.refuse and self.refuse in line:
            return subprocess.CompletedProcess(command, 1, "", "refused by the fake")
        if "curl" in command:
            if self.dead_token and self.dead_token in (body or ""):
                return done("{}\n401")
            url = command[-1].removeprefix("http://127.0.0.1:3000/api/v1")
            method = command[command.index("--request") + 1]
            if method == "GET" and url == "/orgs/kthis":
                return done(f"{{}}\n{200 if self.org_exists else 404}")
            if method == "GET" and url == "/orgs/kthis/teams":
                return done(json.dumps([{"name": name} for name in self.teams]) + "\n200")
            if method == "POST":
                return done("{}\n201")
            return done("{}\n200")
        if "auth" in command and "list" in command:
            return done(self.sources)
        if "tool" in command and "list" in command:
            return done(self.tools)
        if "tool" in command and "add" in command:
            return done("Registered.\n  Client ID: the-client-id\n  Secret:    the-secret\n")
        if "user" in command and "list" in command:
            return done(self.users)
        if "generate-access-token" in command:
            return done("the-token\n")
        return done()

    def lines(self):
        return [" ".join(command) for command, _ in self.calls]

    def find(self, text):
        return [line for line in self.lines() if text in line]

    def index(self, text):
        return next(i for i, line in enumerate(self.lines()) if text in line)

    def bodies(self, text):
        """What was sent to Forgejo's API, which reaches curl as a setting on stdin."""
        found = []
        for command, settings in self.calls:
            match = re.search(r'^data-binary = "(.*)"$', settings or "", re.M)
            if match and text in " ".join(command):
                found.append(json.loads(match.group(1).replace('\\"', '"').replace("\\\\", "\\")))
        return found

    def setup(self):
        kit.setup_git(self.paths, self.config)

    def keep_token(self):
        kit._write_sync_env(self.paths, self.config, FORGEJO_ADMIN_TOKEN="kept-token")

    def test_the_token_never_goes_on_a_command_line(self):
        """Anyone on the server can read those, and this token can't be deleted."""
        self.setup()
        self.assertFalse(self.find("the-token"))
        self.assertTrue(any("Authorization: token the-token" in (settings or "") for _, settings in self.calls))

    def test_text_sent_to_the_api_survives_curls_quoting(self):
        kit._forgejo_api("t", "POST", "/x", {"name": 'a"b\\c'})
        self.assertEqual(self.bodies("curl"), [{"name": 'a"b\\c'}])

    def test_the_organisation_is_named_after_the_domain(self):
        self.setup()
        self.assertEqual(self.bodies("/api/v1/orgs")[0]["username"], "kthis")

    def test_it_creates_a_service_account_that_nobody_gets_a_password_for(self):
        self.setup()
        create = self.find("admin user create")[0]
        self.assertIn("--admin", create)
        self.assertIn("--random-password", create)
        self.assertNotIn("--password ", create)

    def test_it_keeps_a_service_account_that_exists(self):
        self.users = "ID Username\n1 sallskap-admin a@b true true false\n"
        self.setup()
        self.assertEqual(self.find("admin user create"), [])

    def test_the_token_can_only_do_what_this_and_the_worker_need(self):
        self.setup()
        token = self.find("generate-access-token")[0]
        self.assertIn("--scopes write:admin,write:organization,write:repository,write:user ", token)

    def test_the_token_is_kept_for_the_worker_and_nowhere_else(self):
        self.setup()
        self.assertEqual(kit._read_env(self.paths.sync_env)["FORGEJO_ADMIN_TOKEN"], "the-token")
        self.assertEqual(self.paths.sync_env.stat().st_mode & 0o777, 0o600)

    def test_the_worker_is_restarted_last_so_it_has_the_token(self):
        self.setup()
        self.assertTrue(self.lines()[-1].endswith("up -d --wait sync"), self.lines()[-1])
        self.assertLess(self.index("add-oauth"), len(self.lines()) - 1)

    def test_a_kept_token_is_used_rather_than_making_another(self):
        """Forgejo 15 can't delete one with password login off, so another would leave the old one working."""
        self.keep_token()
        self.setup()
        self.assertEqual(self.find("generate-access-token"), [])
        self.assertTrue(any("Authorization: token kept-token" in (settings or "") for _, settings in self.calls))

    def test_a_token_forgejo_no_longer_knows_is_replaced(self):
        """After a restore from before it was made. Otherwise running this again can't finish."""
        self.keep_token()
        self.dead_token = "kept-token"
        self.setup()
        self.assertEqual(len(self.find("generate-access-token")), 1)
        self.assertEqual(kit._read_env(self.paths.sync_env)["FORGEJO_ADMIN_TOKEN"], "the-token")

    def test_forgejo_being_down_doesnt_cost_a_token(self):
        self.keep_token()
        real = self.answer
        self.answer = lambda command, body: subprocess.CompletedProcess(command, 0, "{}\n502", "") if "curl" in command else real(command, body)
        self.assertEqual(kit._forgejo_token(self.paths, self.config), "kept-token")
        self.assertEqual(self.find("generate-access-token"), [])

    def test_kit_apply_gives_an_upgrade_its_token(self):
        """It doesn't run the setup, and the worker can't tell Forgejo anything without one."""
        self.assertIs(kit.AFTER_START["git"], kit._forgejo_token)

    def test_an_upgrade_gets_a_token_and_leaves_its_login_alone(self):
        self.sources = "ID Name Type Enabled\n1\tkthis\tOAuth2\ttrue\n"
        self.tools = "forgejo  https://git.kthis.example/user/oauth2/kthis/callback\n"
        self.users = "ID Username\n1 sallskap-admin a@b true true false\n"
        self.setup()
        self.assertEqual(len(self.find("generate-access-token")), 1)
        self.assertEqual(self.find("admin user create"), [])
        self.assertEqual(self.find("tool add"), [])
        self.assertEqual(self.find("add-oauth"), [])
        self.assertEqual(self.find("update-oauth"), [])
        self.assertEqual(self.find(" restart "), [])

    def test_it_creates_the_teams_a_group_can_lead_to(self):
        self.setup()
        teams = {team["name"]: team for team in self.bodies("/orgs/kthis/teams")}
        self.assertEqual(teams["developers"]["permission"], "write")
        self.assertTrue(teams["developers"]["can_create_org_repo"])
        self.assertEqual(teams["board"]["permission"], "read")
        self.assertFalse(teams["board"]["can_create_org_repo"])
        for team in teams.values():
            self.assertTrue(team["includes_all_repositories"])

    def test_it_only_creates_what_is_missing(self):
        self.org_exists = True
        self.teams = ["Owners", "developers"]
        self.setup()
        self.assertEqual([team["name"] for team in self.bodies("/orgs/kthis/teams")], ["board"])
        self.assertFalse([b for b in self.bodies("/api/v1/orgs") if "username" in b])

    def test_the_organisation_may_make_repos_when_people_may_not(self):
        self.setup()
        limit = self.bodies("/admin/users/kthis")[0]["max_repo_creation"]
        self.assertGreater(limit, 1000)
        self.assertIn('FORGEJO__repository__MAX_CREATION_LIMIT: "0"', compose_services()["forgejo"])

    def test_it_registers_the_tool_with_forgejos_exact_return_address(self):
        self.setup()
        self.assertIn("tool add forgejo https://git.kthis.example/user/oauth2/kthis/callback", self.find("tool add")[0])

    def source(self):
        command = next(command for command, _ in self.calls if "add-oauth" in command or "update-oauth" in command)
        settings, key = {}, None
        for part in command[command.index("admin") + 3 :]:
            if part.startswith("--"):
                key = part
                settings.setdefault(key, [])
            else:
                settings[key].append(part)
        return settings

    def test_forgejo_is_given_the_tools_settings(self):
        self.setup()
        source = self.source()
        self.assertEqual(source["--name"], ["kthis"])
        self.assertEqual(source["--provider"], ["openidConnect"])
        self.assertEqual(source["--key"], ["the-client-id"])
        self.assertEqual(source["--secret"], ["the-secret"])
        self.assertEqual(source["--auto-discover-url"], ["https://app.kthis.example/.well-known/openid-configuration"])
        self.assertEqual(source["--scopes"], ["email", "profile", "groups"])

    def test_groups_decide_teams_and_taking_one_away_counts(self):
        self.setup()
        source = self.source()
        self.assertEqual(source["--group-claim-name"], ["groups"])
        self.assertEqual(
            json.loads(source["--group-team-map"][0]),
            {"operator": {"kthis": ["Owners"]}, "developer": {"kthis": ["developers"]}, "board": {"kthis": ["board"]}},
        )
        self.assertIn("--group-team-map-removal", source)

    def test_only_operators_are_forgejo_admins(self):
        self.setup()
        self.assertEqual(self.source()["--admin-group"], ["operator"])

    def test_nobody_is_asked_for_a_second_code(self):
        self.setup()
        self.assertIn("--skip-local-2fa", self.source())

    def test_the_login_is_connected_last(self):
        """Finding it is how a later run knows everything before it finished."""
        self.setup()
        self.assertLess(self.index("/orgs/kthis/teams"), self.index("add-oauth"))

    def test_forgejo_restarts_to_read_its_new_login(self):
        self.setup()
        self.assertLess(self.index("add-oauth"), self.index(" restart forgejo"))
        self.assertLess(self.index(" restart forgejo"), self.index(" up -d --wait"))

    def test_a_finished_setup_does_nothing_and_makes_no_token(self):
        self.keep_token()
        self.sources = "ID Name Type Enabled\n1\tkthis\tOAuth2\ttrue\n"
        self.tools = "forgejo  https://git.kthis.example/user/oauth2/kthis/callback\n"
        self.setup()
        self.assertEqual(self.find("generate-access-token"), [])
        self.assertEqual(self.find("tool add"), [])
        self.assertEqual(self.find("add-oauth"), [])
        self.assertEqual(self.find(" restart "), [])

    def test_a_login_source_for_something_else_does_not_count(self):
        self.sources = "1\tkthis-old\tOAuth2\ttrue\n"
        self.tools = "forgejo  https://x/callback\n"
        self.setup()
        self.assertTrue(self.find("add-oauth"))

    def test_a_tool_with_no_login_source_gets_a_new_secret(self):
        """Its secret was only shown once, so it can't be given to Forgejo any more."""
        self.tools = "forgejo  https://git.kthis.example/user/oauth2/kthis/callback\n"
        self.setup()
        self.assertLess(self.index("tool remove forgejo"), self.index("tool add"))
        self.assertTrue(self.find("add-oauth"))

    def test_a_login_source_with_no_tool_makes_no_new_token(self):
        """The login is connected last, so the organisation and teams are already there."""
        self.keep_token()
        self.sources = "ID Name Type Enabled\n3\tkthis\tOAuth2\ttrue\n"
        self.setup()
        self.assertEqual(self.find("generate-access-token"), [])

    def test_a_failed_login_setup_can_be_finished_by_running_it_again(self):
        """Core has a new secret by then. If the tool stayed, the next run would find both
        sides present and call it finished, with every sign-in failing."""
        self.sources = "ID Name Type Enabled\n3\tkthis\tOAuth2\ttrue\n"
        self.refuse = "update-oauth"
        with self.assertRaises(kit.KitError):
            self.setup()
        self.assertTrue(self.find("tool remove forgejo"))

    def test_a_failed_restart_can_be_finished_by_running_it_again(self):
        self.refuse = " restart forgejo"
        real_run = kit.subprocess.run

        def strict(command, *args, **kwargs):
            result = real_run(command, *args, **kwargs)
            if kwargs.get("check") and result.returncode:
                raise subprocess.CalledProcessError(result.returncode, command)
            return result

        with mock.patch.object(kit.subprocess, "run", strict), self.assertRaises(subprocess.CalledProcessError):
            self.setup()
        self.assertTrue(self.find("tool remove forgejo"))

    def test_a_login_source_with_no_tool_is_updated_not_duplicated(self):
        self.sources = "ID Name Type Enabled\n3\tkthis\tOAuth2\ttrue\n"
        self.setup()
        self.assertEqual(self.find("add-oauth"), [])
        self.assertIn("update-oauth --id 3 ", self.find("update-oauth")[0])

    def test_a_refused_step_stops_before_the_login_is_connected(self):
        self.refuse = "/orgs/kthis/teams"
        with self.assertRaises(kit.KitError):
            self.setup()
        self.assertEqual(self.find("tool add"), [])
        self.assertEqual(self.find("add-oauth"), [])

    def test_an_api_answer_that_is_not_success_is_an_error(self):
        with mock.patch.object(kit, "_forgejo_api", return_value=(500, {"message": "broken"})):
            with self.assertRaisesRegex(kit.KitError, "answered 500"):
                self.setup()

    def test_an_api_answer_it_cannot_read_is_explained(self):
        real_run = kit.subprocess.run

        def broken(command, *args, **kwargs):
            if "curl" in command:
                return subprocess.CompletedProcess(command, 0, "<html>Bad gateway</html>\n502", "")
            return real_run(command, *args, **kwargs)

        with mock.patch.object(kit.subprocess, "run", broken), self.assertRaisesRegex(kit.KitError, "can't read"):
            self.setup()

    def test_settings_it_cannot_read_are_explained(self):
        real_answer = self.answer
        self.answer = lambda command, body: (
            subprocess.CompletedProcess(command, 0, "something new\n", "") if "add" in command and "tool" in command else real_answer(command, body)
        )
        with self.assertRaisesRegex(kit.KitError, "couldn't read its settings"):
            self.setup()

    def test_a_failure_never_shows_the_secret(self):
        self.refuse = "add-oauth"
        with self.assertRaises(kit.KitError) as caught:
            self.setup()
        self.assertNotIn("the-secret", str(caught.exception))
        self.assertIn("refused by the fake", str(caught.exception))


class VaultSetup(unittest.TestCase):
    """Runs the setup with a made-up core, recording what it was asked."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.paths = kit.Paths(base / "etc", base / "srv", base / "home", base / "core", base / "git", base / "vault")
        self.config = kit.parse_config('[association]\ndomain = "example.org"\n')
        self.calls = []
        self.tools = ""
        self.refuse = None
        self.new_secrets = iter(("first-secret", "second-secret"))

        def fake_run(command, *args, **kwargs):
            line = " ".join(command)
            self.calls.append(line)
            if self.refuse and self.refuse in line:
                return subprocess.CompletedProcess(command, 1, "", "refused by the fake")
            if "tool list" in line:
                return subprocess.CompletedProcess(command, 0, self.tools, "")
            if "tool add" in line:
                self.tools = "vaultwarden  https://x  (board, operator)\n"
                return subprocess.CompletedProcess(command, 0, f"Registered.\n  Client ID: the-id\n  Secret:    {next(self.new_secrets)}\n", "")
            if "tool remove" in line:
                self.tools = ""
            if "argon2" in line:
                self.hashed.append(kwargs.get("input"))
                return subprocess.CompletedProcess(command, 0, f"$argon2id$v=19$fake{len(self.hashed)}\n", "")
            return subprocess.CompletedProcess(command, 0, "", "")

        self.hashed = []
        patcher = mock.patch.object(kit.subprocess, "run", fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def settings(self):
        return kit._read_env(self.paths.vault_env)

    def worker_settings(self):
        return kit._read_env(self.paths.sync_env)

    def find(self, text):
        return [line for line in self.calls if text in line]

    def test_it_registers_with_core_using_the_modules_groups_and_callback(self):
        kit.prepare_vault(self.paths, self.config)
        added = self.find("tool add")[0]
        self.assertIn("vaultwarden https://vault.example.org/identity/connect/oidc-signin", added)
        self.assertIn("--group=board --group=operator", added)
        self.assertIn("--session-hours=12", added)

    def test_it_writes_what_vaultwarden_needs(self):
        kit.prepare_vault(self.paths, self.config)
        self.assertEqual(
            self.settings(),
            {
                "DOMAIN": "https://vault.example.org",
                "SSO_AUTHORITY": "https://app.example.org",
                "SSO_CLIENT_ID": "the-id",
                "SSO_CLIENT_SECRET": "first-secret",
                "ADMIN_TOKEN": "'$argon2id$v=19$fake1'",
            },
        )

    def test_only_the_worker_has_the_admin_token(self):
        kit.prepare_vault(self.paths, self.config)
        token = self.worker_settings()["VAULT_ADMIN_TOKEN"]
        self.assertGreater(len(token), 40)
        self.assertEqual(self.hashed, [token])
        self.assertNotIn(token, self.paths.vault_env.read_text())
        self.assertFalse(self.paths.core_env.exists() and token in self.paths.core_env.read_text())
        self.assertEqual(self.paths.sync_env.stat().st_mode & 0o777, 0o600)

    def test_the_admin_token_never_goes_on_a_command_line(self):
        kit.prepare_vault(self.paths, self.config)
        token = self.worker_settings()["VAULT_ADMIN_TOKEN"]
        self.assertFalse([line for line in self.calls if token in line])

    def test_the_hash_is_quoted_or_compose_would_read_the_dollars_as_variables(self):
        kit.prepare_vault(self.paths, self.config)
        self.assertIn("ADMIN_TOKEN='$argon2id$", self.paths.vault_env.read_text())

    def test_running_it_again_keeps_the_admin_token(self):
        kit.prepare_vault(self.paths, self.config)
        token, hashed = self.worker_settings()["VAULT_ADMIN_TOKEN"], self.settings()["ADMIN_TOKEN"]
        kit.prepare_vault(self.paths, self.config)
        self.assertEqual(self.worker_settings()["VAULT_ADMIN_TOKEN"], token)
        self.assertEqual(self.settings()["ADMIN_TOKEN"], hashed)
        self.assertEqual(len(self.hashed), 1)

    def test_a_lost_hash_gets_a_new_token_on_both_sides(self):
        kit.prepare_vault(self.paths, self.config)
        token = self.worker_settings()["VAULT_ADMIN_TOKEN"]
        self.paths.vault_env.unlink()
        kit.prepare_vault(self.paths, self.config)
        self.assertNotEqual(self.worker_settings()["VAULT_ADMIN_TOKEN"], token)
        self.assertEqual(self.settings()["ADMIN_TOKEN"], "'$argon2id$v=19$fake2'")

    def test_a_lost_token_gets_a_new_hash_too(self):
        kit.prepare_vault(self.paths, self.config)
        text = self.paths.sync_env.read_text()
        self.paths.sync_env.write_text("".join(line for line in text.splitlines(keepends=True) if "VAULT_ADMIN_TOKEN" not in line))
        kit.prepare_vault(self.paths, self.config)
        self.assertEqual(self.settings()["ADMIN_TOKEN"], "'$argon2id$v=19$fake2'")

    def test_the_authority_has_no_trailing_slash_as_it_must_match_the_issuer_exactly(self):
        kit.prepare_vault(self.paths, self.config)
        self.assertFalse(self.settings()["SSO_AUTHORITY"].endswith("/"))

    def test_its_secrets_and_data_are_private(self):
        kit.prepare_vault(self.paths, self.config)
        self.assertEqual(self.paths.vault_env.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.paths.vault_dir.stat().st_mode & 0o777, 0o700)

    def test_running_it_again_changes_nothing(self):
        kit.prepare_vault(self.paths, self.config)
        before = self.settings()
        kit.prepare_vault(self.paths, self.config)
        self.assertEqual(self.settings(), before)
        self.assertEqual(len(self.find("tool add")), 1)
        self.assertEqual(self.find("tool remove"), [])

    def test_a_tool_removed_from_core_gets_a_new_secret_on_both_sides(self):
        kit.prepare_vault(self.paths, self.config)
        self.tools = ""
        kit.prepare_vault(self.paths, self.config)
        self.assertEqual(self.settings()["SSO_CLIENT_SECRET"], "second-secret")

    def test_a_lost_secret_gets_a_new_one_on_both_sides(self):
        kit.prepare_vault(self.paths, self.config)
        self.paths.vault_env.unlink()
        kit.prepare_vault(self.paths, self.config)
        self.assertEqual(len(self.find("tool remove")), 1)
        self.assertEqual(self.settings()["SSO_CLIENT_SECRET"], "second-secret")

    def test_a_failed_registration_leaves_no_secret_so_the_next_run_starts_over(self):
        kit.prepare_vault(self.paths, self.config)
        self.tools, self.refuse = "", "tool add"
        with self.assertRaises(kit.KitError):
            kit.prepare_vault(self.paths, self.config)
        self.assertEqual(self.settings()["SSO_CLIENT_SECRET"], "")
        self.refuse = None
        kit.prepare_vault(self.paths, self.config)
        self.assertEqual(self.settings()["SSO_CLIENT_SECRET"], "second-secret")

    def test_unreadable_settings_from_core_are_an_error_not_an_empty_secret(self):
        with mock.patch.object(kit, "_core_output", side_effect=["", "Registered, oddly.\n"]), self.assertRaises(kit.KitError):
            kit.prepare_vault(self.paths, self.config)

    def test_a_new_hostname_keeps_the_secret(self):
        kit.prepare_vault(self.paths, self.config)
        kit.prepare_vault(self.paths, kit.parse_config('[association]\ndomain = "new.org"\n'))
        self.assertEqual(self.settings()["DOMAIN"], "https://vault.new.org")
        self.assertEqual(self.settings()["SSO_CLIENT_SECRET"], "first-secret")

    def test_vaultwardens_address_is_a_service_so_no_site_can_take_it(self):
        with self.assertRaisesRegex(kit.KitError, "reserved"):
            kit.parse_config('[association]\ndomain = "example.org"\n[[website.sites]]\ndomain = "vault.example.org"\n')


class Status(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.paths = kit.Paths(base / "etc", base / "srv", base / "home", base / "core")
        self.paths.config_dir.mkdir()
        self.paths.config.write_text('[association]\ndomain = "example.org"\n')
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"203.0.113.1"
        for patcher in (
            mock.patch.object(kit.urllib.request, "urlopen", return_value=response),
            # Whether the machine running the tests has restic isn't what's being tested.
            mock.patch.object(kit.shutil, "which", return_value="/usr/bin/restic"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_status(self, addresses):
        out = io.StringIO()
        with (
            mock.patch.object(kit, "_resolve_ipv4", return_value=addresses),
            mock.patch.object(kit, "_update_status", return_value=0),  # tested on its own, in test_update.py
            mock.patch.object(kit, "_public_dns", return_value=None),  # tested on its own, below
            contextlib.redirect_stdout(out),
        ):
            code = kit.status(self.paths)
        return code, out.getvalue()

    def test_a_missing_record_for_the_app_hostname_is_reported(self):
        code, out = self.run_status(set())
        self.assertEqual(code, 1)
        self.assertIn("app.example.org has no record yet. Add this one: app.example.org A 203.0.113.1", out)

    def test_the_app_hostname_pointing_here_is_fine(self):
        code, out = self.run_status({"203.0.113.1"})
        self.assertIn("ok   app.example.org", out)
        self.assertNotIn("DNS", out)
        # The only thing left to report is that nothing has been backed up yet.
        self.assertEqual(code, 1)
        self.assertIn("no backups have been taken yet", out)

    def test_a_public_answer_is_used_with_its_ttl_for_a_record_just_changed(self):
        """Measured on production: this server's own resolver kept the old answer."""
        out = io.StringIO()
        with (
            mock.patch.object(kit, "_public_dns", return_value=({"198.51.100.7"}, 3600)),
            mock.patch.object(kit, "_resolve_ipv4", return_value={"203.0.113.1"}),
            contextlib.redirect_stdout(out),
        ):
            problems = kit._check_dns("app.example.org", "203.0.113.1")
        self.assertEqual(problems, 1)
        self.assertIn("still points at 198.51.100.7", out.getvalue())
        self.assertIn("can last up to 1 hour", out.getvalue())
        self.assertIn("change it to: app.example.org A 203.0.113.1", out.getvalue())

    def test_a_public_answer_pointing_here_is_ok(self):
        out = io.StringIO()
        with mock.patch.object(kit, "_public_dns", return_value=({"203.0.113.1"}, 60)), contextlib.redirect_stdout(out):
            self.assertEqual(kit._check_dns("app.example.org", "203.0.113.1"), 0)
        self.assertIn("ok   app.example.org", out.getvalue())

    def test_ttls_are_said_the_way_a_person_would(self):
        self.assertEqual([kit._spoken_seconds(n) for n in (1, 60, 300, 3600, 86400)], ["1 second", "60 seconds", "5 minutes", "1 hour", "24 hours"])

    def test_without_a_public_answer_the_local_one_is_used(self):
        out = io.StringIO()
        with (
            mock.patch.object(kit, "_public_dns", return_value=None),
            mock.patch.object(kit, "_resolve_ipv4", return_value={"198.51.100.7"}),
            contextlib.redirect_stdout(out),
        ):
            kit._check_dns("app.example.org", "203.0.113.1")
        self.assertIn("the record's TTL, often an hour", out.getvalue())

    def test_the_public_answer_reads_a_records_and_the_lowest_ttl(self):
        answer = {"Answer": [
            {"name": "app.example.org", "type": 5, "TTL": 300, "data": "elsewhere.example.net."},
            {"name": "elsewhere.example.net", "type": 1, "TTL": 120, "data": "198.51.100.7"},
            {"name": "elsewhere.example.net", "type": 1, "TTL": 60, "data": "198.51.100.8"},
        ]}
        response = io.BytesIO(json.dumps(answer).encode())
        with mock.patch.object(kit.urllib.request, "urlopen", return_value=contextlib.nullcontext(response)):
            self.assertEqual(kit._public_dns("app.example.org"), ({"198.51.100.7", "198.51.100.8"}, 60))

    def test_no_public_answer_when_it_cant_be_reached(self):
        with mock.patch.object(kit.urllib.request, "urlopen", side_effect=OSError("no network")):
            self.assertIsNone(kit._public_dns("app.example.org"))

    def fake_module(self):
        root = Path(self._tmp.name) / "root"
        (root / "modules" / "demo").mkdir(parents=True)
        (root / "modules" / "demo" / "module.toml").write_text('description = "A demo"\nsubdomain = "demo"\n')
        patcher = mock.patch.object(kit, "ROOT", root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_enabled_modules_hostname_is_checked(self):
        self.fake_module()
        self.paths.config.write_text('[association]\ndomain = "example.org"\n[modules]\ndemo = true\n')
        code, out = self.run_status(set())
        self.assertEqual(code, 1)
        self.assertIn("Add this one: demo.example.org A 203.0.113.1", out)

    def test_a_module_that_is_off_needs_no_record(self):
        self.fake_module()
        _, out = self.run_status(set())
        self.assertNotIn("demo.example.org", out)


class Backups(unittest.TestCase):
    """Runs the backup commands against a made-up module, with Docker and restic replaced."""

    SNAPSHOT_TIME = "2026-11-05T03:00:11.123456789+01:00"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.base = base
        self.root = base / "root"
        (self.root / "modules" / "website").mkdir(parents=True)
        (self.root / "modules" / "website" / "module.toml").write_text(
            f'name = "website"\ndata = ["{base / "srv" / "website"}"]\n'
        )
        (self.root / "modules" / "website" / "site.caddy").write_text("(website_defaults) {\n}\n")
        (self.root / "VERSION").write_text("1.2.3\n")
        self.demo = self.root / "modules" / "demo"
        self.demo.mkdir()
        self.demo_data = base / "srv" / "demo"
        (self.demo / "module.toml").write_text(
            f'name = "demo"\ndescription = "A demo"\nsubdomain = "demo"\ndata = ["{self.demo_data}"]\n'
        )
        (self.demo / "compose.yml").write_text("services:\n  demo:\n    image: example.org/demo/demo:9.9.9\n")
        (self.demo / "site.caddy").write_text("__HOST__ {\n\treverse_proxy demo:80\n}\n")

        self.paths = kit.Paths(
            config_dir=base / "etc",
            website_dir=base / "srv" / "website",
            deploy_home=base / "home",
            core_dir=base / "srv" / "core",
            caddy_dir=base / "srv" / "caddy",
            backup_dir=base / "srv" / "backup",
        )
        self.paths.config_dir.mkdir()
        self.paths.config.write_text('[association]\ndomain = "example.org"\n[modules]\ndemo = true\n')
        for folder in (self.paths.core_dir, self.paths.caddy_dir, self.paths.deploy_home, self.demo_data):
            folder.mkdir(parents=True)

        self.calls = []
        self.restic_fails = ()  # restic commands that come back with an error
        self.stopped_extras = []
        real_run = subprocess.run

        def fake_run(command, *args, **kwargs):
            if command[0] == "restic":
                self.calls.append(command)
                return self.fake_restic(command, **kwargs)
            if command[0] == "docker":
                self.calls.append(command)
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[0] == "sh":  # a module's own backup.sh
                self.calls.append(command)
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[0] == "git":
                return subprocess.CompletedProcess(command, 0, "abc1234\n", "")
            return real_run(command, *args, **kwargs)

        for patcher in (
            mock.patch.object(kit, "ROOT", self.root),
            mock.patch.object(kit.subprocess, "run", fake_run),
            mock.patch.object(kit, "Paths", return_value=self.paths),
            mock.patch.object(kit, "status", return_value=0),
            mock.patch.object(kit.shutil, "which", return_value="/usr/bin/restic"),
            mock.patch.object(kit, "apply", lambda paths, build=False: self.calls.append(["kit", "apply"])),
            mock.patch.dict(kit.STOP, {"demo": lambda paths: self.stopped_extras.append("demo")}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_restic(self, command, **kwargs):
        action = command[3]
        repo = Path(command[2])
        if action in self.restic_fails:
            return subprocess.CompletedProcess(command, 1, "", "restic said no")
        if action == "init":
            (repo / "config").parent.mkdir(parents=True, exist_ok=True)
            (repo / "config").write_text("a repository")
            return subprocess.CompletedProcess(command, 0, "", "")
        if action == "cat":
            return subprocess.CompletedProcess(command, 1 if not (repo / "config").exists() else 0, "", "")
        if action == "snapshots":
            snapshot = {
                "id": "deadbeef",
                "time": self.SNAPSHOT_TIME,
                "hostname": "old-server",
                "paths": [str(self.paths.core_dir)],
                "tags": ["sallskap", "version:abc1234", "module:demo", "image:demo=9.9.9"],
            }
            return subprocess.CompletedProcess(command, 0, json.dumps([snapshot]), "")
        return subprocess.CompletedProcess(command, 0, "", "")

    def run_kit(self, *args, a_screen=False):
        out, err = io.StringIO(), io.StringIO()
        out.isatty = lambda: a_screen  # some output only belongs on someone's screen
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = kit.main(list(args))
        return code, out.getvalue() + err.getvalue()

    def restic_calls(self, action=None):
        return [c for c in self.calls if c[0] == "restic" and (action is None or c[3] == action)]

    def docker_calls(self):
        return [c for c in self.calls if c[0] == "docker"]

    def config(self):
        return kit.load_config(self.paths)

    # ---- What goes in --------------------------------------------------------

    def test_what_goes_in_comes_from_the_module_manifests(self):
        wanted = kit.backup_paths(self.paths, self.config())
        self.assertIn(self.demo_data, wanted)
        self.assertIn(self.paths.config_dir, wanted)
        self.assertIn(self.paths.core_dir, wanted)

    def test_caddys_certificates_and_the_publishing_keys_go_in(self):
        wanted = kit.backup_paths(self.paths, self.config())
        self.assertIn(self.paths.caddy_dir, wanted)
        self.assertIn(self.paths.deploy_home, wanted)

    def test_a_module_that_is_switched_off_still_gets_backed_up(self):
        """Disabling a module keeps its data, so a backup has to keep it too."""
        self.paths.config.write_text('[association]\ndomain = "example.org"\n')
        self.assertEqual(self.config().modules, ())
        self.assertIn(self.demo_data, kit.backup_paths(self.paths, self.config()))

    def test_a_folder_that_isnt_there_is_left_out(self):
        shutil.rmtree(self.demo_data)
        self.assertNotIn(self.demo_data, kit.backup_paths(self.paths, self.config()))

    def test_the_backups_own_folder_is_never_backed_up(self):
        (self.demo / "module.toml").write_text(f'name = "demo"\ndata = ["{self.paths.backup_dir}"]\n')
        self.paths.backup_dir.mkdir(parents=True)
        with self.assertRaisesRegex(kit.KitError, "holds the backups"):
            kit.backup_paths(self.paths, self.config())

    def test_a_mistake_in_a_manifest_costs_an_error_and_not_an_outage(self):
        (self.demo / "module.toml").write_text(f'name = "demo"\ndata = ["{self.paths.backup_dir}"]\n')
        self.paths.backup_dir.mkdir(parents=True)
        code, out = self.run_kit("backup")
        self.assertEqual(code, 1)
        self.assertIn("holds the backups", out)
        self.assertFalse([c for c in self.docker_calls() if "stop" in c])

    # ---- The password --------------------------------------------------------

    def test_the_password_is_made_once_and_then_kept(self):
        first, made = kit.backup_password(self.paths)
        self.assertTrue(made)
        again, made_again = kit.backup_password(self.paths)
        self.assertEqual(first, again)
        self.assertFalse(made_again)
        self.assertEqual(self.paths.backup_env.stat().st_mode & 0o777, 0o600)

    def test_the_password_is_shown_once_and_only_when_it_is_made(self):
        code, out = self.run_kit("backup", a_screen=True)
        self.assertEqual(code, 0)
        password = kit._read_env(self.paths.backup_env)["RESTIC_PASSWORD"]
        self.assertIn(password, out)
        self.assertIn("can't be recovered", out)
        _, again = self.run_kit("backup", a_screen=True)
        self.assertNotIn(password, again)

    def test_the_password_never_goes_into_the_logs(self):
        """The nightly run is the first backup on a server nobody has touched, and its
        output is the system journal."""
        code, out = self.run_kit("backup")
        self.assertEqual(code, 0)
        password = kit._read_env(self.paths.backup_env)["RESTIC_PASSWORD"]
        self.assertNotIn(password, out)
        self.assertIn("can't be recovered", out)
        self.assertIn("kit backup password", out)

    def test_the_password_is_shown_on_a_screen_and_noted(self):
        self.run_kit("backup")
        password = kit._read_env(self.paths.backup_env)["RESTIC_PASSWORD"]
        code, out = self.run_kit("backup", "password", a_screen=True)
        self.assertEqual(code, 0, out)
        self.assertIn(password, out)
        self.assertIn("shared organisation", out)
        self.assertTrue((self.paths.backup_dir / "password-shown").exists())

    def test_the_password_isnt_shown_where_it_would_go_into_a_log(self):
        self.run_kit("backup")
        password = kit._read_env(self.paths.backup_env)["RESTIC_PASSWORD"]
        code, out = self.run_kit("backup", "password")
        self.assertEqual(code, 1)
        self.assertNotIn(password, out)
        self.assertIn("ssh -t", out)
        self.assertFalse((self.paths.backup_dir / "password-shown").exists())

    def test_status_warns_until_someone_has_seen_the_password(self):
        """Measured on production: the nightly backup made it before anyone looked."""
        self.run_kit("backup")
        self.note_export(days_ago=1)
        with mock.patch.object(kit.time, "time", return_value=self.snapshot_timestamp() + 3600):
            problems, out = self.status_lines()
        self.assertEqual(problems, 1)
        self.assertIn("kit backup password", out)
        self.run_kit("backup", "password", a_screen=True)
        with mock.patch.object(kit.time, "time", return_value=self.snapshot_timestamp() + 3600):
            problems, out = self.status_lines()
        self.assertEqual(problems, 0, out)

    def test_a_backup_without_a_password_refuses_rather_than_making_one_silently(self):
        """Only `kit backup` may make the password, as only it prints it."""
        with self.assertRaisesRegex(kit.KitError, "No backup password"):
            kit._restic_environment(self.paths)

    # ---- Taking one ----------------------------------------------------------

    def test_a_first_backup_sets_up_the_repository_then_copies(self):
        code, _ = self.run_kit("backup")
        self.assertEqual(code, 0)
        self.assertEqual([c[3] for c in self.restic_calls()], ["init", "backup", "forget"])

    def test_a_later_backup_doesnt_set_the_repository_up_again(self):
        self.run_kit("backup")
        self.calls.clear()
        self.run_kit("backup")
        self.assertEqual([c[3] for c in self.restic_calls()], ["backup", "forget"])

    def test_everything_but_caddy_stops_before_the_copy_and_starts_after(self):
        self.run_kit("backup")
        order = []
        for call in self.calls:
            if call[0] == "restic" and call[3] == "backup":
                order.append("copy")
                continue
            if call[0] != "docker":
                continue
            # A module runs from its own Compose file; core and the worker are named.
            module = next((Path(part).parent.name for part in call if part.endswith("compose.yml")), None)
            if "stop" in call:
                order.append(f"stop {module or ' '.join(call[call.index('stop') + 1 :])}")
            elif "up" in call:
                order.append(f"start {module or call[-1]}")
        self.assertEqual(order, ["stop sync core", "stop demo", "copy", "start demo", "start core", "start sync"])
        self.assertFalse([c for c in self.docker_calls() if "caddy" in c])

    def test_everything_is_waited_for_so_a_backup_cant_leave_it_down(self):
        self.run_kit("backup")
        for call in self.docker_calls():
            if "up" in call:
                self.assertIn("--wait", call)

    def test_the_vaults_emergency_copy_is_stopped_too(self):
        """It writes to the same data, and it's meant to be short-lived anyway."""
        self.run_kit("backup")
        self.assertEqual(self.stopped_extras, ["demo"])

    def test_the_containers_come_back_even_when_the_copy_fails(self):
        self.restic_fails = ("backup",)
        code, out = self.run_kit("backup")
        self.assertEqual(code, 1)
        self.assertIn("restic backup failed", out)
        self.assertEqual(len([c for c in self.docker_calls() if "up" in c]), 3)

    def test_something_that_doesnt_come_back_is_reported(self):
        def fails_to_start(command, *args, **kwargs):
            self.calls.append(command)
            if command[0] == "docker" and "up" in command:
                raise subprocess.CalledProcessError(1, command)
            return self.fake_restic(command, **kwargs) if command[0] == "restic" else subprocess.CompletedProcess(command, 0, "", "")

        self.run_kit("backup")  # so the repository exists
        with mock.patch.object(kit.subprocess, "run", fails_to_start):
            code, out = self.run_kit("backup")
        self.assertEqual(code, 1)
        self.assertIn("didn't come back: demo, core, sync", out)
        self.assertIn("Run: kit apply", out)

    def test_a_modules_own_step_runs_while_it_is_still_up(self):
        (self.demo / "backup.sh").write_text("#!/bin/sh\nexit 0\n")
        self.run_kit("backup")
        names = [c[0] if c[0] != "sh" else "backup.sh" for c in self.calls]
        self.assertLess(names.index("backup.sh"), names.index("docker"))

    def test_a_modules_own_step_failing_stops_the_backup(self):
        (self.demo / "backup.sh").write_text("#!/bin/sh\nexit 1\n")
        fails = lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", "")  # noqa: E731
        with mock.patch.object(kit.subprocess, "run", fails), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(kit.KitError, "backup.sh failed"):
                kit._run_backup_scripts(self.config())

    def test_the_snapshot_records_the_version_and_each_pinned_image(self):
        self.run_kit("backup")
        tags = self.restic_calls("backup")[0]
        self.assertIn("version:abc1234", tags)
        self.assertIn("release:1.2.3+abc1234", tags)  # the fake git says HEAD carries no tag
        self.assertIn("image:demo=9.9.9", tags)
        self.assertIn("module:demo", tags)

    def test_old_snapshots_are_pruned_so_the_disk_cant_fill(self):
        self.run_kit("backup")
        forget = self.restic_calls("forget")[0]
        self.assertIn("--prune", forget)
        self.assertIn("--keep-daily", forget)
        self.assertIn("--tag", forget)

    def test_the_pause_is_reported_so_an_operator_knows_what_it_costs(self):
        _, out = self.run_kit("backup")
        self.assertRegex(out, r"paused for \d+ seconds")
        self.assertIn("public sites weren't", out)

    def test_restic_missing_says_how_to_install_it(self):
        with mock.patch.object(kit.shutil, "which", return_value=None):
            code, out = self.run_kit("backup")
        self.assertEqual(code, 1)
        self.assertIn("apt-get install restic", out)
        self.assertEqual(self.calls, [])

    def test_two_backups_at_once_are_refused_rather_than_interleaved(self):
        with kit._backup_lock(self.paths):
            code, out = self.run_kit("backup")
        self.assertEqual(code, 1)
        self.assertIn("A backup or an update is already running", out)

    # ---- The off-server target ----------------------------------------------

    def test_nothing_leaves_the_server_unless_a_target_is_set(self):
        _, out = self.run_kit("backup")
        self.assertEqual(self.restic_calls("copy"), [])
        self.assertIn("Nothing has left this server", out)

    def test_a_target_is_set_up_once_and_then_copied_to(self):
        self.paths.config.write_text(
            f'[association]\ndomain = "example.org"\n[backup]\ntarget = "{self.base / "away"}"\n[modules]\ndemo = true\n'
        )
        self.run_kit("backup")
        init_targets = [c for c in self.restic_calls("init") if str(self.base / "away") in c]
        self.assertEqual(len(init_targets), 1)
        self.assertIn("--copy-chunker-params", init_targets[0])
        self.assertEqual(len(self.restic_calls("copy")), 1)
        self.calls.clear()
        self.run_kit("backup")
        self.assertEqual([c for c in self.restic_calls("init") if str(self.base / "away") in c], [])
        self.assertEqual(len(self.restic_calls("copy")), 1)

    def test_a_target_is_read_as_a_restic_repository_and_nothing_else(self):
        for target in ("", 42, True):
            with self.subTest(target=target):
                text = f'[association]\ndomain = "a.org"\n[backup]\ntarget = {json.dumps(target)}\n'
                with self.assertRaisesRegex(kit.KitError, "must be a restic repository"):
                    kit.parse_config(text)

    # ---- Taking a copy off the server ---------------------------------------

    def test_an_export_holds_the_whole_repository_as_one_file(self):
        self.run_kit("backup")
        out_file = self.base / "away.tar"
        code, out = self.run_kit("backup", "export", str(out_file))
        self.assertEqual(code, 0)
        self.assertIn("It's encrypted", out)
        with tarfile.open(out_file) as tar:
            self.assertIn("repo/config", tar.getnames())
        self.assertEqual(out_file.stat().st_mode & 0o777, 0o600)

    def test_an_export_leaves_out_the_lock_and_its_own_note(self):
        """Or unpacking it would bring another server's lock and dates along."""
        self.run_kit("backup")
        out_file = self.base / "away.tar"
        self.run_kit("backup", "export", str(out_file))
        with tarfile.open(out_file) as tar:
            self.assertEqual([n for n in tar.getnames() if "kit.lock" in n or "last-export" in n], [])

    def test_an_export_to_a_folder_is_named_by_its_date(self):
        self.run_kit("backup")
        self.run_kit("backup", "export", str(self.base))
        self.assertTrue(list(self.base.glob("sallskap-backup-*.tar")))

    def test_an_export_records_when_a_copy_last_left_the_server(self):
        self.run_kit("backup")
        self.assertFalse((self.paths.backup_dir / "last-export").exists())
        self.run_kit("backup", "export", str(self.base / "away.tar"))
        self.assertTrue((self.paths.backup_dir / "last-export").exists())

    def test_an_export_with_nothing_to_export_says_so(self):
        code, out = self.run_kit("backup", "export", str(self.base / "away.tar"))
        self.assertEqual(code, 1)
        self.assertIn("no backup to export yet", out)

    def fake_stdout(self, a_screen):
        stream = io.BytesIO()
        pretend = mock.MagicMock()
        pretend.buffer = stream
        pretend.isatty.return_value = a_screen
        return stream, mock.patch.object(kit.sys, "stdout", pretend)

    def test_an_export_to_a_screen_is_refused_rather_than_printed(self):
        self.run_kit("backup")
        _, as_stdout = self.fake_stdout(a_screen=True)
        with as_stdout, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(kit.KitError, "print the backup to your screen"):
                kit.export_backup(self.paths, "-")

    def test_an_export_streams_to_standard_output_by_default(self):
        """So a copy off the server is one line over SSH, with nothing left behind."""
        self.run_kit("backup")
        stream, as_stdout = self.fake_stdout(a_screen=False)
        with as_stdout, contextlib.redirect_stderr(io.StringIO()):
            kit.export_backup(self.paths, "-")
        stream.seek(0)
        with tarfile.open(fileobj=stream) as tar:
            self.assertIn("repo/config", tar.getnames())

    # ---- Status --------------------------------------------------------------

    def status_lines(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            problems = kit._backup_status(self.paths)
        return problems, out.getvalue()

    def test_status_says_when_nothing_has_been_backed_up(self):
        problems, out = self.status_lines()
        self.assertEqual(problems, 1)
        self.assertIn("no backups have been taken yet", out)

    def test_status_counts_a_missing_restic_as_a_problem(self):
        with mock.patch.object(kit.shutil, "which", return_value=None):
            problems, out = self.status_lines()
        self.assertEqual(problems, 1)
        self.assertIn("no backups are being taken", out)

    def test_status_reports_a_fresh_backup_and_nags_about_the_copy(self):
        self.run_kit("backup")
        self.note_password_shown()
        with mock.patch.object(kit.time, "time", return_value=self.snapshot_timestamp() + 3600):
            problems, out = self.status_lines()
        self.assertIn("ok   last backup 1 hour ago", out)
        self.assertEqual(problems, 1)
        self.assertIn("no copy has ever left this server", out)
        # A copy written on the server doesn't leave it, so it says how to pull one.
        self.assertIn("ssh root@<this server> 'kit backup export -' > sallskap-backup-", out)

    def test_status_reports_a_backup_that_has_stopped_running(self):
        self.run_kit("backup")
        self.note_password_shown()
        self.note_export(days_ago=1)
        with mock.patch.object(kit.time, "time", return_value=self.snapshot_timestamp() + 5 * 86400):
            problems, out = self.status_lines()
        self.assertIn("the last backup was 5 days ago", out)
        self.assertIn("sallskap-backup.timer", out)
        self.assertEqual(problems, 1)

    def test_status_reports_a_copy_off_the_server_that_has_got_old(self):
        self.run_kit("backup")
        self.note_password_shown()
        self.note_export(days_ago=40)
        with mock.patch.object(kit.time, "time", return_value=self.snapshot_timestamp() + 3600):
            problems, out = self.status_lines()
        self.assertIn("ok   last backup", out)
        self.assertIn("the last copy off this server was 40 days ago", out)
        self.assertEqual(problems, 1)

    def note_password_shown(self):
        (self.paths.backup_dir / "password-shown").write_text("1\n")

    def note_export(self, days_ago):
        when = self.snapshot_timestamp() - days_ago * 86400 + 3600
        (self.paths.backup_dir / "last-export").write_text(f"{int(when)}\n")

    def snapshot_timestamp(self):
        return datetime.fromisoformat(self.SNAPSHOT_TIME[:26] + self.SNAPSHOT_TIME[-6:]).timestamp()

    # ---- Restoring -----------------------------------------------------------

    def test_a_restore_refuses_to_replace_a_working_installation(self):
        (self.paths.core_dir / "db.sqlite3").write_text("a database")
        code, out = self.run_kit("restore")
        self.assertEqual(code, 1)
        self.assertIn("already has an installation", out)
        self.assertEqual(self.restic_calls(), [])

    def test_a_restore_over_a_working_installation_needs_force(self):
        self.run_kit("backup")
        (self.paths.core_dir / "db.sqlite3").write_text("a database")
        code, out = self.run_kit("restore", "--force")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.restic_calls("restore")), 1)

    def test_a_restore_over_an_installation_moves_it_aside_and_restarts_caddy(self):
        """Measured on the test server: a running Caddy follows a moved folder, not its name."""
        self.run_kit("backup")
        (self.paths.core_dir / "db.sqlite3").write_text("a database")
        code, out = self.run_kit("restore", "--force")
        self.assertEqual(code, 0, out)
        aside = list(self.paths.core_dir.parent.glob("core.before-restore-*"))
        self.assertEqual(len(aside), 1)
        self.assertEqual((aside[0] / "db.sqlite3").read_text(), "a database")
        self.assertIn(str(aside[0]), out)
        restore = self.calls.index(self.restic_calls("restore")[0])
        restart = next(i for i, c in enumerate(self.calls) if c[0] == "docker" and c[-2:] == ["restart", "caddy"])
        self.assertLess(restore, restart)

    def test_a_restore_onto_a_fresh_server_moves_nothing_and_leaves_caddy(self):
        self.run_kit("backup")
        self.run_kit("restore")
        self.assertFalse(list(self.paths.core_dir.parent.glob("*.before-restore-*")))
        self.assertFalse([c for c in self.calls if c[0] == "docker" and "restart" in c])

    def test_a_restore_puts_everything_back_then_starts_it(self):
        self.run_kit("backup")
        code, out = self.run_kit("restore")
        self.assertEqual(code, 0, out)
        restore = self.restic_calls("restore")[0]
        self.assertIn("deadbeef", restore)
        self.assertEqual(restore[-2:], ["--target", "/"])
        self.assertIn(["kit", "apply"], self.calls)

    def test_a_restore_says_to_remove_whoever_has_left(self):
        """Measured on a real restore: a person deleted in core after the backup came
        back as an active member, and core no longer knew they had left. Nothing takes
        their access away, so the only thing that can is the person reading this."""
        self.run_kit("backup")
        _, out = self.run_kit("restore")
        self.assertIn("REMOVE ANYONE WHO HAS LEFT", out)
        self.assertIn("no longer knows they ever left", out)
        self.assertIn("master password", out)

    def test_a_restore_says_how_old_the_backup_is(self):
        """So the operator knows how far back to look for who has left."""
        self.run_kit("backup")
        _, out = self.run_kit("restore")
        self.assertRegex(out, r"Restoring the backup taken .* (minutes|hours|days) ago\.")

    def test_a_backup_taken_moments_ago_isnt_called_zero_hours(self):
        self.assertEqual(kit._spoken_age(0), "1 minute ago")
        self.assertEqual(kit._spoken_age(20 / 1440), "20 minutes ago")
        self.assertEqual(kit._spoken_age(5 / 24), "5 hours ago")
        self.assertEqual(kit._spoken_age(3), "3 days ago")

    def test_a_restore_stops_what_is_running_before_putting_files_back(self):
        self.run_kit("backup")
        self.calls.clear()
        self.run_kit("restore")
        stops = [i for i, c in enumerate(self.calls) if "stop" in c]
        restores = [i for i, c in enumerate(self.calls) if c[0] == "restic" and c[3] == "restore"]
        self.assertTrue(stops and restores and max(stops) < min(restores))

    def test_a_restore_refuses_a_backup_from_a_different_tool_version(self):
        """An older tool refuses a database written by a newer one."""
        self.run_kit("backup")
        (self.demo / "compose.yml").write_text("services:\n  demo:\n    image: example.org/demo/demo:8.0.0\n")
        code, out = self.run_kit("restore")
        self.assertEqual(code, 1)
        self.assertIn("the backup was taken with 9.9.9, this code pins 8.0.0", out)
        self.assertEqual(self.restic_calls("restore"), [])

    def test_a_version_difference_can_be_overridden_once_it_has_been_seen(self):
        self.run_kit("backup")
        (self.demo / "compose.yml").write_text("services:\n  demo:\n    image: example.org/demo/demo:8.0.0\n")
        code, out = self.run_kit("restore", "--across-versions")
        self.assertEqual(code, 0, out)
        self.assertIn("Going ahead with a version difference", out)

    def test_replacing_an_installation_doesnt_also_waive_the_version_check(self):
        """Rolling back a bad upgrade is the usual reason to restore over a working
        server, and the usual way to end up with an older tool and a newer database.
        One flag for both risks would skip the check exactly then."""
        self.run_kit("backup")
        (self.paths.core_dir / "db.sqlite3").write_text("a database")
        (self.demo / "compose.yml").write_text("services:\n  demo:\n    image: example.org/demo/demo:8.0.0\n")
        code, out = self.run_kit("restore", "--force")
        self.assertEqual(code, 1)
        self.assertIn("The versions don't match", out)
        self.assertIn("--across-versions", out)
        self.assertEqual(self.restic_calls("restore"), [])
        code, out = self.run_kit("restore", "--force", "--across-versions")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.restic_calls("restore")), 1)

    def test_a_restore_from_an_exported_file_unpacks_it_first(self):
        self.run_kit("backup")
        out_file = self.base / "away.tar"
        self.run_kit("backup", "export", str(out_file))
        shutil.rmtree(self.paths.backup_dir)
        code, out = self.run_kit("restore", str(out_file))
        self.assertEqual(code, 0, out)
        self.assertTrue((self.paths.repo / "config").exists())

    def test_a_restore_wont_unpack_over_backups_that_are_already_there(self):
        self.run_kit("backup")
        out_file = self.base / "away.tar"
        self.run_kit("backup", "export", str(out_file))
        code, out = self.run_kit("restore", str(out_file))
        self.assertEqual(code, 1)
        self.assertIn("already holds backups", out)

    def test_a_restore_refuses_a_file_that_isnt_an_export(self):
        not_a_backup = self.base / "notes.txt"
        not_a_backup.write_text("hello")
        code, out = self.run_kit("restore", str(not_a_backup))
        self.assertEqual(code, 1)
        self.assertIn("isn't a backup exported by kit", out)

    def test_a_restore_with_nothing_to_restore_from_says_what_to_give_it(self):
        code, out = self.run_kit("restore")
        self.assertEqual(code, 1)
        self.assertIn("kit restore <file>", out)

    def test_an_exported_file_cannot_write_outside_the_backup_folder(self):
        """It's unpacked as root, so a crafted file mustn't be able to place one anywhere."""
        shutil.rmtree(self.paths.backup_dir, ignore_errors=True)
        crafted = self.base / "crafted.tar"
        with tarfile.open(crafted, "w") as tar:
            info = tarfile.TarInfo("../../escaped")
            info.size = 0
            tar.addfile(info, io.BytesIO(b""))
        code, _ = self.run_kit("restore", str(crafted))
        self.assertEqual(code, 1)
        self.assertFalse((self.base.parent / "escaped").exists())

    def test_the_password_for_a_restore_is_taken_from_the_offline_copy(self):
        with mock.patch.dict(kit.os.environ, {}, clear=False):
            kit.os.environ.pop("RESTIC_PASSWORD", None)
            with mock.patch.object(kit.getpass, "getpass", return_value="typed-in") as asked:
                self.assertEqual(kit._restore_password(self.paths), "typed-in")
        asked.assert_called_once()

    def test_a_restore_puts_the_publishing_keys_back_in_the_deploy_accounts_name(self):
        """restic puts back the numbers it saved, and this server's may differ."""
        self.run_kit("backup")
        self.paths.config.write_text(
            '[association]\ndomain = "example.org"\n\n[[website.sites]]\ndomain = "example.org"\n'
        )
        site = self.paths.site_dir("example.org")
        site.mkdir(parents=True)
        (site / "current").write_text("x")
        with mock.patch.object(kit.os, "geteuid", return_value=0), mock.patch.object(kit.shutil, "chown") as chown:
            kit._fix_ownership(self.paths, self.config())
        changed = {str(call.args[0]) for call in chown.call_args_list}
        self.assertIn(str(self.paths.deploy_home), changed)
        self.assertIn(str(site / "current"), changed)


class WorkerSettings(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.paths = kit.Paths(base / "etc", base / "srv", base / "home", base / "core", base / "git", base / "vault")
        self.config = kit.parse_config('[association]\ndomain = "example.org"\n[modules]\ngit = true\nvault = true\nwebsite = true\n')

    def settings(self):
        return kit._read_env(self.paths.sync_env)

    def test_it_only_asks_modules_that_are_on_and_have_a_connector(self):
        kit._write_sync_env(self.paths, self.config)
        self.assertEqual(self.settings()["SYNC_MODULES"], "git,vault")
        kit._write_sync_env(self.paths, kit.parse_config('[association]\ndomain = "example.org"\n[modules]\nvault = true\n'))
        self.assertEqual(self.settings()["SYNC_MODULES"], "vault")

    def test_it_is_private(self):
        kit._write_sync_env(self.paths, self.config)
        self.assertEqual(self.paths.sync_env.stat().st_mode & 0o777, 0o600)

    def test_it_doesnt_get_core_secrets(self):
        kit.prepare_core(self.paths, self.config)
        core = kit._read_env(self.paths.core_env)
        worker = kit._read_env(self.paths.sync_env)
        self.assertNotEqual(worker["DJANGO_SECRET_KEY"], core["DJANGO_SECRET_KEY"])
        self.assertNotIn("OIDC_SIGNING_KEY", worker)

    def test_core_is_never_given_the_tools_tokens(self):
        kit.prepare_core(self.paths, self.config)
        kit._write_sync_env(self.paths, self.config, FORGEJO_ADMIN_TOKEN="the-token")
        kit.prepare_core(self.paths, self.config)
        self.assertNotIn("the-token", self.paths.core_env.read_text())
        self.assertNotIn("sync.env", compose_services()["core"])

    def test_preparing_core_keeps_the_tokens_and_changes_nothing_else(self):
        kit.prepare_core(self.paths, self.config)
        kit._write_sync_env(self.paths, self.config, FORGEJO_ADMIN_TOKEN="the-token")
        before = self.paths.sync_env.read_text()
        modified = self.paths.sync_env.stat().st_mtime_ns
        kit.prepare_core(self.paths, self.config)
        self.assertEqual(self.paths.sync_env.read_text(), before)
        self.assertEqual(self.paths.sync_env.stat().st_mtime_ns, modified)

    def test_turning_a_module_off_stops_asking_it_and_keeps_its_token(self):
        kit._write_sync_env(self.paths, self.config, FORGEJO_ADMIN_TOKEN="the-token")
        kit._write_sync_env(self.paths, kit.parse_config('[association]\ndomain = "example.org"\n'))
        self.assertEqual(self.settings()["SYNC_MODULES"], "")
        self.assertEqual(self.settings()["FORGEJO_ADMIN_TOKEN"], "the-token")

    def test_the_team_map_has_one_home(self):
        manifest = kit.module_manifest("git")
        self.assertEqual(manifest["teams"], {"operator": "Owners", "developer": "developers", "board": "board"})
        self.assertEqual(manifest["admin_group"], "operator")
        for team in kit.FORGEJO_TEAMS:
            self.assertIn(team["name"], manifest["teams"].values())
        # Forgejo silently skips a team in its map that doesn't exist. It makes Owners itself.
        self.assertEqual(set(manifest["teams"].values()) - {"Owners"}, {team["name"] for team in kit.FORGEJO_TEAMS})

    def test_someone_who_can_only_read_never_stays_a_collaborator(self):
        self.assertNotIn("board", kit.module_manifest("git")["collaborator_groups"])


class Emergency(unittest.TestCase):
    """Runs the commands with Docker replaced, recording what it was asked to do."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.paths = kit.Paths(base / "etc", base / "srv", base / "home", base / "core", base / "git", base / "vault")
        self.paths.config_dir.mkdir()
        self.paths.config.write_text('[association]\ndomain = "example.org"\n[modules]\nvault = true\ngit = true\n')
        self.calls = []
        self.running = False

        def fake_run(command, *args, **kwargs):
            self.calls.append(command)
            line = " ".join(command)
            if command[:2] == ["docker", "inspect"]:
                return subprocess.CompletedProcess(command, 0 if self.running else 1, "172.18.0.9\n" if self.running else "", "")
            if command[:2] == ["docker", "rm"]:  # succeeds whether or not there's anything to remove
                self.running = False
                return subprocess.CompletedProcess(command, 0, "", "")
            if " run " in line:
                self.running = True
            if "generate-access-token" in line:
                return subprocess.CompletedProcess(command, 0, "0123456789abcdef0123456789abcdef01234567\n", "")
            return subprocess.CompletedProcess(command, 0, "", "")

        for patcher in (mock.patch.object(kit.subprocess, "run", fake_run), mock.patch.object(kit, "Paths", return_value=self.paths)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_kit(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = kit.main(list(args))
        return code, out.getvalue() + err.getvalue()

    def started(self):
        return next(call for call in self.calls if "run" in call)

    def test_it_turns_on_password_login_only_in_the_copy(self):
        self.run_kit("emergency", "vault")
        command = " ".join(self.started())
        self.assertIn("--env SSO_ENABLED=false", command)
        self.assertIn("--env SSO_ONLY=false", command)
        self.assertIn("--env DOMAIN=https://localhost:8222", command)
        self.assertEqual(self.started()[-1], "vaultwarden")
        self.assertIn('SSO_ONLY: "true"', compose_services()["vaultwarden"])

    def test_nothing_about_it_can_be_reached_except_over_ssh(self):
        self.run_kit("emergency", "vault")
        command = self.started()
        for flag in ("--publish", "-p", "--service-ports", "--use-aliases"):
            self.assertNotIn(flag, command)

    def test_it_serves_https_as_its_web_page_refuses_anything_else(self):
        self.run_kit("emergency", "vault")
        command = " ".join(self.started())
        self.assertIn('ROCKET_TLS={certs="/data/emergency-tls/cert.pem",key="/data/emergency-tls/key.pem"}', command)

    def test_its_certificate_is_only_for_localhost_and_goes_when_it_stops(self):
        self.run_kit("emergency", "vault")
        made = next(call for call in self.calls if call[0] == "openssl")
        self.assertIn("subjectAltName=DNS:localhost", made)
        self.assertIn("-nodes", made)
        self.assertEqual(made[made.index("-days") + 1], "7")
        folder = self.paths.vault_dir / "emergency-tls"
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        self.run_kit("emergency", "vault", "--off")
        self.assertFalse(folder.exists())

    def test_it_turns_off_its_admin_page(self):
        self.run_kit("emergency", "vault")
        self.assertIn("ADMIN_TOKEN=", self.started())

    def test_it_doesnt_depend_on_core(self):
        self.run_kit("emergency", "vault")
        self.assertIn("--no-deps", self.started())
        self.assertFalse([call for call in self.calls if "manage.py" in call])

    def test_it_says_how_to_reach_it(self):
        code, out = self.run_kit("emergency", "vault")
        self.assertEqual(code, 0)
        self.assertIn("ssh -L 8222:172.18.0.9:8080", out)
        self.assertIn("https://localhost:8222", out)
        self.assertIn("warn about the certificate", out)
        self.assertIn("kit emergency vault --off", out)

    def test_asking_twice_starts_one(self):
        self.run_kit("emergency", "vault")
        self.run_kit("emergency", "vault")
        self.assertEqual(len([call for call in self.calls if "run" in call]), 1)

    def test_it_can_be_stopped(self):
        self.run_kit("emergency", "vault")
        code, out = self.run_kit("emergency", "vault", "--off")
        self.assertEqual(code, 0)
        self.assertIn("Stopped", out)
        self.assertFalse(self.running)
        self.assertIn("wasn't running", self.run_kit("emergency", "vault", "--off")[1])

    def test_a_git_token_goes_on_their_own_account(self):
        code, out = self.run_kit("emergency", "git", "someone")
        self.assertEqual(code, 0)
        command = " ".join(next(call for call in self.calls if "generate-access-token" in call))
        self.assertIn("--username someone ", command)
        self.assertNotIn(kit.FORGEJO_SERVICE_USER, command)
        self.assertIn("0123456789abcdef0123456789abcdef01234567", out)
        self.assertIn("Settings, Applications", out)

    def test_a_git_token_doesnt_depend_on_core(self):
        self.run_kit("emergency", "git", "someone")
        self.assertFalse([call for call in self.calls if "manage.py" in call])

    def test_disabling_the_vault_stops_the_copy_too(self):
        self.run_kit("emergency", "vault")
        with mock.patch.object(kit, "apply_web"):
            code, _ = self.run_kit("disable", "vault")
        self.assertEqual(code, 0)
        self.assertFalse(self.running)
        self.assertFalse((self.paths.vault_dir / "emergency-tls").exists())

    def test_disabling_git_leaves_the_copy_alone(self):
        self.run_kit("emergency", "vault")
        with mock.patch.object(kit, "apply_web"):
            self.run_kit("disable", "git")
        self.assertTrue(self.running)

    def test_status_says_so_even_with_the_module_off(self):
        self.run_kit("emergency", "vault")
        self.paths.config.write_text('[association]\ndomain = "example.org"\n')
        address = lambda *args, **kwargs: io.BytesIO(b"203.0.113.1")
        with mock.patch.object(kit, "_resolve_ipv4", return_value={"203.0.113.1"}), mock.patch.object(kit.urllib.request, "urlopen", address):
            _, out = self.run_kit("status")
        self.assertIn("emergency copy is running", out)

    def test_no_docker_means_nothing_is_running(self):
        with mock.patch.object(kit.subprocess, "run", side_effect=FileNotFoundError("docker")):
            self.assertIsNone(kit._vault_emergency_address())

    def test_it_refuses_a_module_thats_off(self):
        self.paths.config.write_text('[association]\ndomain = "example.org"\n')
        for args in (("emergency", "vault"), ("emergency", "git", "someone")):
            with self.subTest(args):
                code, out = self.run_kit(*args)
                self.assertEqual(code, 1)
                self.assertIn("isn't enabled", out)
        self.assertEqual(self.calls, [])

    def test_status_says_if_the_copy_is_still_running(self):
        address = lambda *args, **kwargs: io.BytesIO(b"203.0.113.1")
        with mock.patch.object(kit, "_resolve_ipv4", return_value={"203.0.113.1"}), mock.patch.object(kit.urllib.request, "urlopen", address):
            self.run_kit("emergency", "vault")
            _, running = self.run_kit("status")
            self.run_kit("emergency", "vault", "--off")
            _, stopped = self.run_kit("status")
        self.assertIn("emergency copy is running", running)
        self.assertNotIn("emergency copy is running", stopped)

    def test_a_name_that_could_be_read_as_an_option_is_refused(self):
        with contextlib.redirect_stderr(io.StringIO()):
            for name in ("-x", "--admin", "a b", "a/b"):
                with self.subTest(name):
                    try:
                        code, _ = self.run_kit("emergency", "git", name)
                    except SystemExit as stopped:  # argparse refuses some of these itself
                        code = stopped.code
                    self.assertNotEqual(code, 0)
        self.assertEqual(self.calls, [])


class CommandLine(unittest.TestCase):
    def test_a_problem_gives_a_message_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"SALLSKAP_CONFIG_DIR": tmp, "PATH": "/usr/bin:/bin"}
            result = subprocess.run(
                [sys.executable, str(ROOT / "kit" / "kit.py"), "status"], capture_output=True, text=True, env=env
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("No config file", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


def compose_services() -> dict[str, str]:
    """The text of each service in every Compose file. Standard library only,
    so this reads the layout rather than parsing YAML."""
    services = {}
    for compose in [ROOT / "compose.yml", *ROOT.glob("modules/*/compose.yml")]:
        name, in_services = None, False
        for line in compose.read_text().splitlines():
            if line.startswith("services:"):
                in_services = True
            elif line and not line.startswith((" ", "#")):
                in_services, name = False, None
            elif in_services and re.fullmatch(r"  [\w-]+:\s*", line):
                name = line.strip().rstrip(":")
                services[name] = ""
            elif name:
                services[name] += line + "\n"
    return services


class Packaging(unittest.TestCase):
    def test_compose_files_are_read_correctly(self):
        self.assertLessEqual({"caddy", "core"}, set(compose_services()))

    def test_the_worker_has_the_same_limits_as_core_and_no_ports(self):
        worker = compose_services()["sync"]
        for setting in ("mem_limit:", "pids_limit:", "read_only: true", "cap_drop: [ALL]", '"no-new-privileges:true"', "restart: unless-stopped"):
            self.assertIn(setting, worker)
        self.assertNotIn("ports:", worker)

    def test_the_worker_runs_cores_image_and_cant_change_module_code(self):
        worker = compose_services()["sync"]
        self.assertIn("image: sallskap-core:${SALLSKAP_CORE_TAG:-local}", worker)
        self.assertIn("./modules:/modules:ro", worker)
        self.assertIn("condition: service_healthy", worker)

    def test_restarting_the_worker_cant_lock_it_out_of_vaultwarden(self):
        vault = compose_services()["vaultwarden"]
        self.assertIn('ADMIN_RATELIMIT_MAX_BURST: "30"', vault)
        self.assertIn('ADMIN_RATELIMIT_SECONDS: "10"', vault)

    def test_vaultwardens_admin_page_is_closed_at_caddy(self):
        """Its admin token is on now, so this is what keeps the page off the internet."""
        self.assertIn("path /admin /admin/*", (ROOT / "modules" / "vault" / "site.caddy").read_text())

    def test_core_is_the_only_way_into_vaultwarden(self):
        vault = compose_services()["vaultwarden"]
        for setting in ('SSO_ENABLED: "true"', 'SSO_ONLY: "true"', 'SIGNUPS_ALLOWED: "false"'):
            self.assertIn(setting, vault)

    def test_vaultwarden_never_hands_someone_an_existing_account(self):
        self.assertIn('SSO_SIGNUPS_MATCH_EMAIL: "false"', compose_services()["vaultwarden"])

    def test_vaultwarden_accepts_core_saying_nothing_about_addresses_only_with_matching_off(self):
        """Its docs warn that the two together open account takeover."""
        vault = compose_services()["vaultwarden"]
        self.assertIn('SSO_ALLOW_UNKNOWN_EMAIL_VERIFICATION: "true"', vault)
        self.assertIn('SSO_SIGNUPS_MATCH_EMAIL: "false"', vault)

    def test_opening_vaultwarden_starts_signing_in_once_per_session(self):
        routing = (ROOT / "modules" / "vault" / "site.caddy").read_text()
        self.assertIn("redir @first_visit /#/sso?identifier=00000000-01DC-01DC-01DC-000000000000 302", routing)
        hours = kit.module_manifest("vault")["session_hours"]
        self.assertIn(f"Max-Age={hours * 3600};", routing)

    def test_vaultwarden_makes_no_empty_account_for_an_invited_address(self):
        self.assertIn('INVITATIONS_ALLOWED: "false"', compose_services()["vaultwarden"])

    def test_nobody_gets_a_vaultwarden_key_that_gets_around_core(self):
        routing = (ROOT / "modules" / "vault" / "site.caddy").read_text()
        for route in (
            "/admin ",
            "/admin/*",
            "/identity/accounts/register*",
            "/api/accounts/register*",
            "/api/accounts/api-key*",
            "/api/accounts/rotate-api-key*",
            "/api/organizations/*/api-key*",
            "/api/organizations/*/rotate-api-key*",
            "/api/organizations/*/api-key/*",  # a * in the middle stops at the next /
            "/api/organizations/*/rotate-api-key/*",
        ):
            self.assertIn(route, routing)
        self.assertIn("respond @closed 404", routing)

    def test_vaultwardens_version_is_pinned_and_it_is_not_root(self):
        vault = compose_services()["vaultwarden"]
        self.assertRegex(vault, r"image: \S+:\d+\.\d+\.\d+\n")
        self.assertIn(f'user: "{kit.VAULT_UID}:{kit.VAULT_UID}"', vault)

    def test_vaultwardens_groups_are_the_ones_that_require_2fa(self):
        """So every login to the vault has passed 2FA."""
        self.assertEqual(kit.module_manifest("vault")["access"], ["board", "operator"])

    def test_only_caddy_publishes_ports(self):
        """docs/spec.md §7.2 rule 7: Docker's published ports bypass the firewall."""
        for name, text in compose_services().items():
            if name != "caddy":
                self.assertNotIn("ports:", text, f"{name} publishes ports")

    def test_installer_variables_do_not_clash_with_os_release(self):
        """Sourcing /etc/os-release over a readonly variable kills the installer."""
        fields = {"NAME", "VERSION", "ID", "ID_LIKE", "PRETTY_NAME", "VERSION_ID", "VERSION_CODENAME", "HOME_URL"}
        names = set(re.findall(r"^readonly (\w+)=", (ROOT / "install.sh").read_text(), re.M))
        self.assertTrue(names)
        self.assertFalse(names & fields)

    def test_every_container_has_a_memory_limit(self):
        """docs/spec.md §7.2 rule 4."""
        for name, text in compose_services().items():
            self.assertIn("mem_limit:", text, f"{name} has no memory limit")

    def test_core_refuses_to_start_without_its_signing_key(self):
        """Otherwise it looks fine until the first login to a tool fails."""
        self.assertRegex((ROOT / "core" / "start.sh").read_text(), r"(?m)^: \"\$\{OIDC_SIGNING_KEY:\?")

    def test_forgejo_runs_locked_down(self):
        text = compose_services()["forgejo"]
        self.assertIn("read_only: true", text)
        self.assertIn("cap_drop: [ALL]", text)
        self.assertIn("no-new-privileges:true", text)

    def test_forgejos_version_is_pinned(self):
        """So an upgrade only arrives in a version of the kit that it was tested with."""
        self.assertRegex(compose_services()["forgejo"], r"image: codeberg\.org/forgejo/forgejo:\d+\.\d+\.\d+-rootless\n")

    def test_forgejo_can_only_be_entered_through_core(self):
        """Each of these would be a way around 2FA."""
        text = compose_services()["forgejo"]
        for setting in (
            'FORGEJO__security__INSTALL_LOCK: "true"',
            'FORGEJO__service__ENABLE_INTERNAL_SIGNIN: "false"',
            'FORGEJO__service__ENABLE_BASIC_AUTHENTICATION: "false"',
            'FORGEJO__service__ALLOW_ONLY_EXTERNAL_REGISTRATION: "true"',
            'FORGEJO__openid__ENABLE_OPENID_SIGNIN: "false"',
            'FORGEJO__openid__ENABLE_OPENID_SIGNUP: "false"',
            'FORGEJO__oauth2__ENABLED: "false"',
        ):
            self.assertIn(setting, text)

    def test_a_fork_cannot_get_around_the_limit_on_personal_repos(self):
        self.assertIn('FORGEJO__repository__ALLOW_FORK_WITHOUT_MAXIMUM_LIMIT: "false"', compose_services()["forgejo"])

    def test_people_cannot_manage_what_comes_from_core(self):
        self.assertIn("FORGEJO__admin__EXTERNAL_USER_DISABLE_FEATURES: deletion,manage_password,manage_ssh_keys", compose_services()["forgejo"])

    def test_addresses_cannot_be_added_in_forgejo(self):
        """With no mail to confirm it, someone else's address would stop them signing in."""
        routing = (ROOT / "modules" / "git" / "site.caddy").read_text()
        self.assertIn("path /user/settings/account/email* /api/v1/user/emails*", routing)
        self.assertIn('respond @email "Addresses are managed in core." 403', routing)

    def test_signing_in_keeps_the_page_someone_came_from(self):
        self.assertIn("/user/oauth2/__SHORT_NAME__?{query} 302", (ROOT / "modules" / "git" / "site.caddy").read_text())

    def test_forgejo_never_hands_someone_an_existing_account(self):
        """`auto` would give an account to anyone who arrives with the same name."""
        self.assertIn("FORGEJO__oauth2_client__ACCOUNT_LINKING: disabled", compose_services()["forgejo"])

    def test_forgejo_sessions_survive_a_restart(self):
        self.assertIn("FORGEJO__session__PROVIDER: db", compose_services()["forgejo"])

    def test_forgejo_runs_no_ssh_server(self):
        """The rootless image's own template turns it on, and only Caddy may publish ports."""
        text = compose_services()["forgejo"]
        self.assertIn('FORGEJO__server__DISABLE_SSH: "true"', text)
        self.assertIn('FORGEJO__server__START_SSH_SERVER: "false"', text)

    def test_forgejo_keeps_people_private(self):
        text = compose_services()["forgejo"]
        for setting in (
            'FORGEJO__service__DEFAULT_KEEP_EMAIL_PRIVATE: "true"',
            "FORGEJO__service__DEFAULT_USER_VISIBILITY: limited",
            'FORGEJO__service_0X2E_explore__DISABLE_USERS_PAGE: "true"',
        ):
            self.assertIn(setting, text)

    def test_the_git_module_is_routed_to_forgejo(self):
        config = kit.parse_config('[association]\ndomain = "example.org"\n[modules]\ngit = true\n')
        block = kit.render_caddyfile(config).split("git.example.org {\n")[1].split("\n}")[0]
        self.assertIn("reverse_proxy forgejo:3000", block)
        self.assertIn("Strict-Transport-Security", block)
        self.assertIn("-Server", block)

    def test_opening_forgejo_goes_straight_to_the_login_service(self):
        config = kit.parse_config('[association]\ndomain = "example.org"\n[modules]\ngit = true\n')
        block = kit.render_caddyfile(config).split("git.example.org {\n")[1].split("\n}\n")[0]
        self.assertIn("redir @sign_in /user/oauth2/example?{query} 302", block)
        self.assertNotIn("__SHORT_NAME__", block)

    def test_the_login_it_goes_to_is_the_one_setup_creates(self):
        """The address in Caddy and the login source's name have to be the same."""
        config = kit.parse_config('[association]\ndomain = "example.org"\n')
        self.assertEqual(config.short_name, "example")
        self.assertIn("org = config.short_name", (ROOT / "kit" / "kit.py").read_text())

    def test_only_opening_the_page_is_redirected(self):
        """Forgejo answers a posted login itself, which is what refuses local passwords."""
        routing = (ROOT / "modules" / "git" / "site.caddy").read_text()
        self.assertIn("method GET", routing)
        self.assertIn("not header Cookie *sallskap_stay=1*", routing)

    def test_the_uid_given_forgejos_data_folder_is_the_images(self):
        """The image's documented default: nothing in the Compose file changes it."""
        self.assertEqual(kit.FORGEJO_UID, 1000)
        self.assertNotIn("user:", compose_services()["forgejo"])

    def test_core_runs_locked_down(self):
        text = compose_services()["core"]
        self.assertIn("read_only: true", text)
        self.assertIn("cap_drop: [ALL]", text)
        self.assertIn("no-new-privileges:true", text)
        self.assertRegex((ROOT / "core" / "Dockerfile").read_text(), r"(?m)^USER 10001")

    def test_backups_run_every_night_without_anyone_remembering(self):
        timer = (ROOT / "systemd" / "sallskap-backup.timer").read_text()
        self.assertIn("OnCalendar=*-*-* 03:00:00", timer)
        self.assertIn("Persistent=true", timer)  # a server that was off catches up
        self.assertIn("WantedBy=timers.target", timer)

    def test_the_nightly_run_is_the_same_command_an_operator_would_type(self):
        service = (ROOT / "systemd" / "sallskap-backup.service").read_text()
        self.assertIn(f"ExecStart={kit.KIT_COMMAND} backup", service)
        self.assertIn("Type=oneshot", service)

    def test_the_nightly_run_is_skipped_rather_than_failing_before_the_setup(self):
        service = (ROOT / "systemd" / "sallskap-backup.service").read_text()
        self.assertIn("ExecCondition=/usr/bin/test -f /etc/sallskap/config.toml", service)

    def test_a_nightly_backup_cant_make_the_server_feel_slow(self):
        service = (ROOT / "systemd" / "sallskap-backup.service").read_text()
        self.assertIn("IOSchedulingClass=idle", service)
        self.assertIn("Nice=", service)

    def test_the_installer_puts_restic_and_the_timer_in_place(self):
        installer = (ROOT / "install.sh").read_text()
        self.assertRegex(installer, r"apt-get install .*\brestic\b")
        self.assertIn("systemd/sallskap-backup.service", installer)
        self.assertIn("systemctl enable --now sallskap-backup.timer", installer)
        self.assertIn("schedule_backups", installer.split("main() {")[1])


if __name__ == "__main__":
    unittest.main()
