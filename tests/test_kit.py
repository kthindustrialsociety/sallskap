"""Run from the repo root with: python3 -m unittest discover -s tests"""

import base64
import contextlib
import importlib.util
import io
import json
import re
import subprocess
import tarfile
import tempfile
import tomllib
import unittest
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
        for name in ("core.env", "git.env"):
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

    def test_apply_starts_caddy_then_waits_for_core(self):
        self.run_kit("apply")
        self.assertEqual(self.starts(), [["up", "-d", "caddy"], ["up", "-d", "--wait", "core"]])

    def test_apply_only_rebuilds_when_asked(self):
        self.run_kit("apply", "--build")
        self.assertEqual(self.starts()[-1], ["up", "-d", "--wait", "--build", "core"])

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
        self.assertEqual(starts, ["up -d caddy", "up -d --wait core", "up -d --wait"])
        self.assertEqual(self.setup_calls, ["prepare", "setup"])
        self.assertEqual(len(self.demo_commands()), 1)

    def test_a_module_joins_the_same_project_as_core(self):
        self.run_kit("enable", "demo")
        self.assertEqual(self.demo_commands()[0][:5], ["docker", "compose", "--project-name", "sallskap", "--project-directory"])
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
        self.assertEqual(self.demo_commands()[-1], self.calls[-1])
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

    def test_disable_takes_the_route_away_stops_it_and_keeps_its_data(self):
        self.run_kit("enable", "demo")
        self.calls.clear()
        code, _ = self.run_kit("disable", "demo")
        self.assertEqual(code, 0)
        self.assertEqual(self.config().modules, ())
        self.assertNotIn("demo.example.org", self.paths.caddyfile.read_text())
        removal = self.demo_commands()[-1]
        self.assertEqual(removal[-3:], ["rm", "--stop", "--force"])
        self.assertNotIn("--volumes", removal)
        self.assertEqual(self.calls[-1], removal)

    def test_disabling_something_that_is_off_does_nothing(self):
        code, out = self.run_kit("disable", "demo")
        self.assertEqual(code, 0)
        self.assertIn("isn't enabled", out)
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
        self.calls = []
        self.sources = ""
        self.tools = ""
        self.users = ""
        self.org_exists = False
        self.teams = ["Owners"]
        self.refuse = None  # part of a command to answer with a failure

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
        return [json.loads(body) for command, body in self.calls if text in " ".join(command) and body]

    def setup(self):
        kit.setup_git(None, self.config)

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

    def test_the_token_can_only_do_what_the_setup_needs(self):
        self.setup()
        token = self.find("generate-access-token")[0]
        self.assertIn("--scopes write:admin,write:organization ", token)

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
        patcher = mock.patch.object(kit.urllib.request, "urlopen", return_value=response)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_status(self, addresses):
        out = io.StringIO()
        with mock.patch.object(kit, "_resolve_ipv4", return_value=addresses), contextlib.redirect_stdout(out):
            code = kit.status(self.paths)
        return code, out.getvalue()

    def test_a_missing_record_for_the_app_hostname_is_reported(self):
        code, out = self.run_status(set())
        self.assertEqual(code, 1)
        self.assertIn("Add this record: app.example.org A 203.0.113.1", out)

    def test_the_app_hostname_pointing_here_is_fine(self):
        code, out = self.run_status({"203.0.113.1"})
        self.assertEqual(code, 0)
        self.assertIn("ok   app.example.org", out)

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
        self.assertIn("Add this record: demo.example.org A 203.0.113.1", out)

    def test_a_module_that_is_off_needs_no_record(self):
        self.fake_module()
        _, out = self.run_status(set())
        self.assertNotIn("demo.example.org", out)


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


if __name__ == "__main__":
    unittest.main()
