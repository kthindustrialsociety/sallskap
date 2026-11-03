"""Run from the repo root with: python3 -m unittest discover -s tests"""

import importlib.util
import io
import re
import subprocess
import tarfile
import tempfile
import tomllib
import unittest
from pathlib import Path

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


class Packaging(unittest.TestCase):
    def test_only_caddy_publishes_ports(self):
        """docs/spec.md §7.2 rule 7: Docker's published ports bypass the firewall."""
        for compose in [ROOT / "compose.yml", *ROOT.glob("modules/*/compose.yml")]:
            current_service, in_services = None, False
            for line in compose.read_text().splitlines():
                if line.startswith("services:"):
                    in_services = True
                elif in_services and line.startswith("  ") and not line.startswith("   ") and line.strip().endswith(":"):
                    current_service = line.strip().rstrip(":")
                elif line.strip() == "ports:":
                    self.assertEqual(current_service, "caddy", f"{compose}: {current_service} publishes ports")

    def test_installer_variables_do_not_clash_with_os_release(self):
        """Sourcing /etc/os-release over a readonly variable kills the installer."""
        fields = {"NAME", "VERSION", "ID", "ID_LIKE", "PRETTY_NAME", "VERSION_ID", "VERSION_CODENAME", "HOME_URL"}
        names = set(re.findall(r"^readonly (\w+)=", (ROOT / "install.sh").read_text(), re.M))
        self.assertTrue(names)
        self.assertFalse(names & fields)

    def test_every_container_has_a_memory_limit(self):
        """docs/spec.md §7.2 rule 4."""
        for compose in [ROOT / "compose.yml", *ROOT.glob("modules/*/compose.yml")]:
            text = compose.read_text()
            self.assertEqual(text.count("image:"), text.count("mem_limit:"), compose)


if __name__ == "__main__":
    unittest.main()
