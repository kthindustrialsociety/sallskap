"""The backup commands against the real restic, with only Docker replaced.

The tests in test_kit.py replace restic, so they check what kit asks for but not
whether restic agrees: a renamed option would pass there and fail at three in the
morning. These take a real backup of a made-up installation, export it, delete the
lot and put it back. Skipped where restic isn't installed.

Run from the repo root with: python3 -m unittest discover -s tests
"""

import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("kit", ROOT / "kit" / "kit.py")
kit = importlib.util.module_from_spec(spec)
sys.modules["kit"] = kit
spec.loader.exec_module(kit)

SECRET = "the-contents-of-a-restored-file-91c4"


@unittest.skipUnless(shutil.which("restic"), "restic isn't installed")
class RealRestic(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.base = base

        self.root = base / "root"
        module = self.root / "modules" / "demo"
        module.mkdir(parents=True)
        self.demo_data = base / "srv" / "demo"
        (module / "module.toml").write_text(f'name = "demo"\ndata = ["{self.demo_data}"]\n')
        (module / "compose.yml").write_text("services:\n  demo:\n    image: example.org/demo:9.9.9\n")

        self.paths = kit.Paths(
            config_dir=base / "etc",
            website_dir=base / "srv" / "website",
            deploy_home=base / "home",
            core_dir=base / "srv" / "core",
            caddy_dir=base / "srv" / "caddy",
            backup_dir=base / "srv" / "backup",
        )
        self.paths.config_dir.mkdir(parents=True)
        self.paths.config.write_text('[association]\ndomain = "example.org"\n[modules]\ndemo = true\n')
        for folder in (self.paths.core_dir, self.paths.caddy_dir, self.paths.deploy_home, self.demo_data):
            folder.mkdir(parents=True)
        (self.paths.core_dir / "db.sqlite3").write_text(SECRET)
        (self.demo_data / "repo.git").write_text("a repository")
        (self.paths.deploy_home / "authorized_keys").write_text("ssh-ed25519 AAAA a-publishing-key")

        self.docker_calls = []
        real_run = subprocess.run

        def fake_run(command, *args, **kwargs):
            """Only Docker: restic, git, tar and a module's own script run for real."""
            if command[0] == "docker":
                self.docker_calls.append(command)
                return subprocess.CompletedProcess(command, 0, "", "")
            return real_run(command, *args, **kwargs)

        for patcher in (
            mock.patch.object(kit, "ROOT", self.root),
            mock.patch.object(kit.subprocess, "run", fake_run),
            mock.patch.object(kit, "Paths", return_value=self.paths),
            mock.patch.object(kit, "status", return_value=0),
            mock.patch.object(kit, "apply", lambda paths, build=False: None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_kit(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = kit.main(list(args))
        return code, out.getvalue() + err.getvalue()

    def restic(self, *args):
        """Ask restic itself, rather than trusting what kit thinks it did."""
        environment = {**kit.os.environ, **kit._read_env(self.paths.backup_env)}
        result = subprocess.run(
            ["restic", "--repo", str(self.paths.repo), *args], env=environment, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def snapshots(self):
        return json.loads(self.restic("snapshots", "--json"))

    def test_a_backup_is_taken_and_restic_can_read_it_back(self):
        code, out = self.run_kit("backup")
        self.assertEqual(code, 0, out)
        snapshots = self.snapshots()
        self.assertEqual(len(snapshots), 1)
        self.assertIn(str(self.paths.core_dir), snapshots[0]["paths"])
        self.assertIn("sallskap", snapshots[0]["tags"])
        self.assertIn("image:demo=9.9.9", snapshots[0]["tags"])
        self.assertIn("module:demo", snapshots[0]["tags"])

    def test_a_backup_passes_restics_own_integrity_check(self):
        self.run_kit("backup")
        self.assertIn("no errors were found", self.restic("check", "--read-data"))

    def test_damage_to_a_backup_is_noticed(self):
        """What `restic check` is for. Done after the backup, as a backup prunes packs
        no kept snapshot needs and can carry the damage away with them."""
        self.run_kit("backup")
        packs = [p for p in (self.paths.repo / "data").rglob("*") if p.is_file()]
        self.assertTrue(packs)
        damaged = bytearray(packs[-1].read_bytes())
        damaged[len(damaged) // 2] ^= 0xFF
        packs[-1].chmod(0o644)  # restic stores them read-only
        packs[-1].write_bytes(bytes(damaged))
        environment = {**kit.os.environ, **kit._read_env(self.paths.backup_env)}
        result = subprocess.run(
            ["restic", "--repo", str(self.paths.repo), "check", "--read-data"],
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("repository contains errors", result.stdout + result.stderr)

    def test_the_backup_is_unreadable_without_the_password(self):
        self.run_kit("backup")
        result = subprocess.run(
            ["restic", "--repo", str(self.paths.repo), "snapshots"],
            env={**kit.os.environ, "RESTIC_PASSWORD": "not-the-password"},
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("wrong password", result.stderr + result.stdout)

    def test_a_second_backup_only_stores_what_changed(self):
        self.run_kit("backup")
        (self.demo_data / "second.git").write_text("another repository")
        self.run_kit("backup")
        self.assertEqual(len(self.snapshots()), 2)
        latest = json.loads(self.restic("snapshots", "--latest", "1", "--json"))
        self.assertEqual(latest[-1]["summary"]["files_new"], 1)

    def test_a_modules_own_step_runs_and_what_it_writes_is_backed_up(self):
        script = self.root / "modules" / "demo" / "backup.sh"
        script.write_text(f'#!/bin/sh\nprintf dumped > "{self.demo_data}/dump.sql"\n')
        self.run_kit("backup")
        listed = self.restic("ls", "latest")
        self.assertIn("dump.sql", listed)

    def test_an_export_can_be_unpacked_and_read_on_another_machine(self):
        self.run_kit("backup")
        exported = self.base / "away.tar"
        self.run_kit("backup", "export", str(exported))
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        subprocess.run(["tar", "-xf", str(exported), "-C", str(elsewhere)], check=True)
        password = kit._read_env(self.paths.backup_env)["RESTIC_PASSWORD"]
        result = subprocess.run(
            ["restic", "--repo", str(elsewhere / "repo"), "snapshots", "--json"],
            env={**kit.os.environ, "RESTIC_PASSWORD": password},
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)), 1)

    def test_a_restore_brings_every_file_back_as_it_was(self):
        self.run_kit("backup")
        shutil.rmtree(self.paths.core_dir)
        shutil.rmtree(self.demo_data)
        (self.paths.deploy_home / "authorized_keys").unlink()
        code, out = self.run_kit("restore", "--force")
        self.assertEqual(code, 0, out)
        self.assertEqual((self.paths.core_dir / "db.sqlite3").read_text(), SECRET)
        self.assertEqual((self.demo_data / "repo.git").read_text(), "a repository")
        self.assertIn("a-publishing-key", (self.paths.deploy_home / "authorized_keys").read_text())

    def test_a_restore_from_an_exported_file_alone_brings_everything_back(self):
        """What a fresh server does: nothing of the installation is left to help it."""
        self.run_kit("backup")
        exported = self.base / "away.tar"
        self.run_kit("backup", "export", str(exported))
        password = kit._read_env(self.paths.backup_env)["RESTIC_PASSWORD"]

        shutil.rmtree(self.paths.backup_dir)
        shutil.rmtree(self.paths.core_dir)
        shutil.rmtree(self.demo_data)
        shutil.rmtree(self.paths.config_dir)  # the password file goes with it

        with mock.patch.dict(kit.os.environ, {"RESTIC_PASSWORD": password}):
            code, out = self.run_kit("restore", str(exported))
        self.assertEqual(code, 0, out)
        self.assertEqual((self.paths.core_dir / "db.sqlite3").read_text(), SECRET)
        self.assertTrue(self.paths.config.exists())
        # And the restored installation can go on backing up with the same password.
        self.assertEqual(kit._read_env(self.paths.backup_env)["RESTIC_PASSWORD"], password)

    def test_a_restore_with_the_wrong_password_says_so_and_changes_nothing(self):
        self.run_kit("backup")
        exported = self.base / "away.tar"
        self.run_kit("backup", "export", str(exported))
        shutil.rmtree(self.paths.backup_dir)
        shutil.rmtree(self.paths.core_dir)
        with mock.patch.dict(kit.os.environ, {"RESTIC_PASSWORD": "not-the-password"}):
            code, out = self.run_kit("restore", str(exported))
        self.assertEqual(code, 1)
        self.assertIn("password may be wrong", out)
        self.assertFalse(self.paths.core_dir.exists())

    def test_repeated_backups_dont_fill_the_disk(self):
        """Each run forgets and prunes, so a nightly backup settles at what the policy
        keeps rather than growing for ever. How many that is, is restic's business:
        what matters is that running it again doesn't add another."""
        for _ in range(3):
            self.run_kit("backup")
        settled = len(self.snapshots())
        for _ in range(3):
            self.run_kit("backup")
        self.assertEqual(len(self.snapshots()), settled)
        self.assertIn(str(self.paths.core_dir), self.snapshots()[-1]["paths"])

    def test_a_copy_to_an_off_server_target_can_be_restored_from_on_its_own(self):
        """The target is a plain restic repository, so a folder stands in for the real
        thing here: sftp: and s3: differ only in how restic reaches them."""
        target = self.base / "off-server"
        self.paths.config.write_text(
            f'[association]\ndomain = "example.org"\n[backup]\ntarget = "{target}"\n[modules]\ndemo = true\n'
        )
        code, out = self.run_kit("backup")
        self.assertEqual(code, 0, out)
        password = kit._read_env(self.paths.backup_env)["RESTIC_PASSWORD"]
        result = subprocess.run(
            ["restic", "--repo", str(target), "snapshots", "--json"],
            env={**kit.os.environ, "RESTIC_PASSWORD": password},
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)), 1)

    def test_a_restore_can_read_an_off_server_target_on_its_own(self):
        """What an association with a push target and a dead server has to do. Without
        this they would need restic by hand, which is the thing the kit is meant to spare
        them. A folder stands in for sftp: here; restic reaches both the same way."""
        target = self.base / "off-server"
        self.paths.config.write_text(
            f'[association]\ndomain = "example.org"\n[backup]\ntarget = "{target}"\n[modules]\ndemo = true\n'
        )
        self.run_kit("backup")
        password = kit._read_env(self.paths.backup_env)["RESTIC_PASSWORD"]

        # Nothing of the installation is left: no local repository, no config, no data.
        shutil.rmtree(self.paths.backup_dir)
        shutil.rmtree(self.paths.core_dir)
        shutil.rmtree(self.demo_data)
        shutil.rmtree(self.paths.config_dir)

        with mock.patch.dict(kit.os.environ, {"RESTIC_PASSWORD": password}):
            code, out = self.run_kit("restore", "--from", str(target))
        self.assertEqual(code, 0, out)
        self.assertEqual((self.paths.core_dir / "db.sqlite3").read_text(), SECRET)
        self.assertEqual((self.demo_data / "repo.git").read_text(), "a repository")

    def test_a_restore_with_nothing_local_says_the_two_ways_in(self):
        (self.paths.core_dir / "db.sqlite3").unlink()  # past the guard about replacing one
        code, out = self.run_kit("restore")
        self.assertEqual(code, 1)
        self.assertIn("kit restore <file>", out)
        self.assertIn("--from", out)

    def test_a_second_run_copies_only_what_the_target_lacks(self):
        target = self.base / "off-server"
        self.paths.config.write_text(
            f'[association]\ndomain = "example.org"\n[backup]\ntarget = "{target}"\n[modules]\ndemo = true\n'
        )
        self.run_kit("backup")
        _, out = self.run_kit("backup")
        self.assertNotIn("Setting up", out)  # it's only set up once
        self.assertIn("Copying off the server", out)


if __name__ == "__main__":
    unittest.main()
