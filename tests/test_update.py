"""`kit update`, against real git repositories with made-up releases, and fake Docker,
restic and curl. Run from the repo root with: python3 -m unittest discover -s tests"""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if "kit" not in sys.modules:
    spec = importlib.util.spec_from_file_location("kit", ROOT / "kit" / "kit.py")
    sys.modules["kit"] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sys.modules["kit"])
kit = sys.modules["kit"]

GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
IDENTITY = ["-c", "user.name=Test", "-c", "user.email=test@example.org", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false"]
Disk = namedtuple("Disk", "total used free")


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *IDENTITY, *args], cwd=cwd, env=GIT_ENV, check=True, capture_output=True, text=True).stdout


def changelog(*releases: tuple[str, str]) -> str:
    sections = [f"## {number} (2026-11-06)\n\n### Before you update\n\n{before}\n\n### Changes\n\n- feat: x\n" for number, before in releases]
    return "# Changelog\n\n" + "\n".join(sections)


class UpdateBase(unittest.TestCase):
    """An origin with releases, and an installation cloned from it the way the installer does."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.origin = self.base / "origin"
        (self.origin / "core").mkdir(parents=True)
        (self.origin / "modules" / "git").mkdir(parents=True)
        (self.origin / "core" / "Dockerfile").write_text("FROM scratch\n")
        git("init", "-q", "-b", "main", cwd=self.origin)
        self.history = []
        self.release("0.7.0", forgejo="15.0.11", before="Nothing.")

        self.paths = kit.Paths(
            config_dir=self.base / "etc",
            website_dir=self.base / "srv" / "website",
            deploy_home=self.base / "home",
            core_dir=self.base / "srv" / "core",
            caddy_dir=self.base / "srv" / "caddy",
            backup_dir=self.base / "srv" / "backup",
        )
        self.paths.config_dir.mkdir()
        self.paths.config.write_text('[association]\ndomain = "example.org"\n[modules]\ngit = true\n')
        self.paths.backup_env.write_text("RESTIC_PASSWORD=secret\n")
        (self.paths.repo).mkdir(parents=True)
        (self.paths.repo / "config").write_text("")
        self.paths.core_dir.mkdir(parents=True)

        self.calls = []
        self.unhealthy = False
        self.answers = {}  # the first words of a command → its exit code
        self.images = ["sallskap-core:0.6.0", "sallskap-core:0.7.0", "caddy:2.10-alpine", "codeberg.org/forgejo/forgejo:15.0.11-rootless", "playwright:latest"]
        self.forgejo = []
        real_run = subprocess.run

        def fake_run(command, *args, **kwargs):
            if command[0] == "git":
                return real_run(command, *args, **kwargs)
            self.calls.append(list(command))
            if kwargs.get("stdin") is not None:
                kwargs["stdin"].read()
            for start, code in self.answers.items():
                if command[: len(start)] == list(start):
                    return subprocess.CompletedProcess(command, code, "", "it failed")
            if command[:2] == ["docker", "ps"]:
                state = "running\tUp 1 minute (unhealthy)" if self.unhealthy else "running\tUp 1 minute (healthy)"
                return subprocess.CompletedProcess(command, 0, f"sallskap-core-1\tsallskap-core:{self.running}\t{state}\n", "")
            if command[:3] == ["docker", "image", "ls"]:
                return subprocess.CompletedProcess(command, 0, "\n".join(self.images) + "\n", "")
            if command[0] == "restic" and "snapshots" in command:
                return subprocess.CompletedProcess(command, 0, json.dumps([{"id": "snap1", "time": "2026-11-06T20:00:00Z", "tags": []}]), "")
            if command[0] == "curl":
                return subprocess.CompletedProcess(command, 0, "200", "")
            return subprocess.CompletedProcess(command, 0, "", "")

        self.doctor = "[1] Check paths\n - [W] Found 2 Orphaned TwoFactor without existing User\n"

        def forgejo_output(*args):
            self.forgejo.append(args)
            self.calls.append(["forgejo", *args])
            if args == ("doctor", "check", "--list"):
                return "Default\tName\tTitle\n\tgc-lfs\t\tGarbage\n*\tpaths\t\tPaths\n*\tauthorized-keys\tKeys\n\tcheck-db-consistency\tDB\n"
            if args[:2] == ("doctor", "check"):
                return self.doctor
            return ""

        self.execs = []
        for patcher in (
            mock.patch.object(kit.subprocess, "run", fake_run),
            mock.patch.object(kit, "_forgejo_output", forgejo_output),
            mock.patch.object(kit.os, "geteuid", return_value=0),
            mock.patch.object(kit.os, "execv", lambda *args: self.execs.append(args)),
            mock.patch.object(kit.shutil, "which", return_value="/usr/bin/restic"),
            mock.patch.object(kit.shutil, "disk_usage", return_value=Disk(100 * 1024**3, 0, 50 * 1024**3)),
            mock.patch.dict(kit.STOP, {}, clear=True),
            mock.patch.object(kit, "Paths", return_value=self.paths),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.install("v0.7.0")

    @property
    def running(self) -> str:
        return kit.running_version().replace("+", "-")

    def release(self, number: str, forgejo: str, before: str, tag: bool = True, version: str | None = None) -> None:
        self.history.insert(0, (number, before))
        (self.origin / "VERSION").write_text(f"{version or number}\n")
        (self.origin / "CHANGELOG.md").write_text(changelog(*self.history))
        (self.origin / "compose.yml").write_text(
            "services:\n  core:\n    image: sallskap-core:${SALLSKAP_CORE_TAG:-local}\n  caddy:\n    image: caddy:2.10-alpine\n"
        )
        (self.origin / "modules" / "git" / "compose.yml").write_text(
            f"services:\n  forgejo:\n    image: codeberg.org/forgejo/forgejo:{forgejo}-rootless\n"
        )
        (self.origin / "modules" / "git" / "module.toml").write_text(f'name = "git"\ndata = ["{self.base / "srv" / "git"}"]\n')
        git("add", "-A", cwd=self.origin)
        git("commit", "-q", "-m", f"release {number}", cwd=self.origin)
        if tag:
            git("tag", "-a", f"v{number}", "-m", "notes", cwd=self.origin)

    def install(self, ref: str) -> None:
        self.root = self.base / "opt"
        git("clone", "-q", "--depth", "1", "--branch", ref, f"file://{self.origin}", str(self.root), cwd=self.base)
        patcher = mock.patch.object(kit, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def kit(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = kit.main(list(args))
        return code, out.getvalue()

    def head(self) -> str:
        return git("rev-parse", "HEAD", cwd=self.root).strip()

    def fetched(self, number: str) -> str:
        """The way `kit update` gets it: fetched, not checked out."""
        return kit._choose_target(number, None, "0.7.0")[0]

    def tag_commit(self, number: str) -> str:
        return git("rev-parse", f"v{number}^{{commit}}", cwd=self.origin).strip()

    def state(self) -> dict:
        return json.loads((self.paths.backup_dir / "update.json").read_text())

    def index(self, first_words: list[str]) -> int:
        return next(i for i, call in enumerate(self.calls) if call[: len(first_words)] == first_words)


class ChoosingWhereToGo(UpdateBase):
    def test_releases_sort_as_numbers_and_test_tags_are_left_out(self):
        self.release("0.10.0", forgejo="15.0.11", before="Nothing.")
        self.release("0.9.0", forgejo="15.0.11", before="Nothing.")
        git("tag", "v0.11.0-rc1", cwd=self.origin)
        self.assertEqual(kit.available_releases(), ["0.7.0", "0.9.0", "0.10.0"])

    def test_the_newest_release_is_the_default(self):
        self.release("0.7.1", forgejo="15.0.12", before="Nothing.")
        self.release("0.8.0", forgejo="15.0.12", before="Nothing.")
        self.assertEqual(kit._choose_target(None, None, "0.7.0"), (self.tag_commit("0.8.0"), "0.8.0"))

    def test_the_same_release_is_nothing_to_do(self):
        with self.assertRaisesRegex(kit.KitError, "already runs 0.7.0"):
            kit._choose_target("0.7.0", None, "0.7.0")

    def test_an_older_release_is_refused(self):
        with self.assertRaisesRegex(kit.KitError, "only goes forwards"):
            kit._choose_target("0.7.0", None, "0.8.0")

    def test_a_release_the_server_is_already_past_is_refused(self):
        with self.assertRaisesRegex(kit.KitError, "only goes forwards"):
            kit._choose_target("0.7.0", None, "0.7.0+abc1234")

    def test_a_release_that_doesnt_exist_says_which_is_newest(self):
        with self.assertRaisesRegex(kit.KitError, "no release 0.9.0. The newest is 0.7.0"):
            kit._choose_target("0.9.0", None, "0.6.0")

    def test_a_branch_is_refused_as_a_release(self):
        with self.assertRaisesRegex(kit.KitError, "isn't a release number"):
            kit._choose_target("main", None, "0.7.0")

    def test_a_tag_whose_version_file_disagrees_is_refused(self):
        self.release("0.7.1", forgejo="15.0.11", before="Nothing.", version="0.7.0")
        with self.assertRaisesRegex(kit.KitError, "doesn't hold release 0.7.1"):
            kit._choose_target("0.7.1", None, "0.7.0")

    def test_a_release_with_an_incomplete_changelog_is_refused(self):
        self.release("0.7.1", forgejo="15.0.11", before="")
        with self.assertRaisesRegex(kit.KitError, "changelog isn't complete"):
            kit._choose_target("0.7.1", None, "0.7.0")

    def test_a_branch_is_taken_with_unreleased(self):
        self.release("0.7.0", forgejo="15.0.12", before="Nothing.", tag=False)
        commit, version = kit._choose_target(None, "main", "0.7.0")
        self.assertEqual(commit, git("rev-parse", "main", cwd=self.origin).strip())
        self.assertRegex(version, r"^0\.7\.0\+[0-9a-f]{7,}$")

    def test_an_older_branch_is_refused_too(self):
        with self.assertRaisesRegex(kit.KitError, "older than 0.8.0"):
            kit._choose_target(None, "main", "0.8.0")


class WhatItSaysFirst(UpdateBase):
    def test_each_release_says_what_to_do_first_oldest_first(self):
        self.release("0.7.1", forgejo="15.0.11", before="- Read the first thing.")
        self.release("0.8.0", forgejo="15.0.11", before="- Then the second.")
        text = "\n".join(kit.describe_update("0.7.0", "0.8.0", self.fetched("0.8.0")))
        self.assertLess(text.index("Read the first thing"), text.index("Then the second"))
        self.assertNotIn("Nothing.", text)  # 0.7.0's own section is already installed

    def test_a_new_forgejo_says_to_read_its_notes_and_check_custom_css(self):
        self.release("0.7.1", forgejo="16.0.0", before="Nothing.")
        text = "\n".join(kit.describe_update("0.7.0", "0.7.1", self.fetched("0.7.1")))
        self.assertIn("Forgejo: 15.0.11-rootless → 16.0.0-rootless", text)
        self.assertIn(kit.FORGEJO_RELEASES, text)
        self.assertIn("CSS", text)

    def test_nothing_about_forgejo_when_it_stays_the_same(self):
        self.release("0.7.1", forgejo="15.0.11", before="Nothing.")
        text = "\n".join(kit.describe_update("0.7.0", "0.7.1", self.fetched("0.7.1")))
        self.assertNotIn("Forgejo", text)


class Updating(UpdateBase):
    def setUp(self):
        super().setUp()
        self.release("0.7.1", forgejo="15.0.12", before="- Something to know.")

    def test_it_goes_in_order_and_hands_over_to_the_new_code(self):
        code, out = self.kit("update", "--yes")
        self.assertEqual(code, 0, out)
        pull = self.index(["docker", "pull"])
        build = self.index(["docker", "build"])
        flush = self.index(["forgejo", "manager", "flush-queues"])
        backup = next(i for i, call in enumerate(self.calls) if call[0] == "restic" and "backup" in call)
        self.assertLess(pull, flush)
        self.assertLess(build, flush)
        self.assertLess(flush, backup)
        self.assertIn("codeberg.org/forgejo/forgejo:15.0.12-rootless", self.calls[pull])
        self.assertIn("sallskap-core:0.7.1", self.calls[build])
        self.assertEqual(self.head(), self.tag_commit("0.7.1"))
        self.assertEqual(self.execs[0][1][-2:], ["update", "--finish"])
        self.assertIn("Something to know", out)

    def test_nothing_starts_between_the_backup_and_the_switch(self):
        """Anything written then would be lost if the update had to be rolled back."""
        self.kit("update", "--yes")
        backup = next(i for i, call in enumerate(self.calls) if call[0] == "restic" and "backup" in call)
        self.assertFalse([call for call in self.calls[backup:] if "up" in call])

    def test_it_records_how_to_get_back(self):
        before = self.head()
        self.kit("update", "--yes")
        state = self.state()
        self.assertEqual((state["from"], state["from_commit"], state["to"], state["snapshot"]), ("0.7.0", before, "0.7.1", "snap1"))
        self.assertIn("sallskap-core:0.7.0", state["images_before"])
        self.assertIsNone(state.get("result"))
        self.assertEqual(git("rev-parse", "refs/sallskap/before-update", cwd=self.root).strip(), before)

    def test_without_a_screen_it_asks_for_yes_and_changes_nothing(self):
        before = self.head()
        with mock.patch.object(kit.sys.stdin, "isatty", return_value=False):
            code, out = self.kit("update")
        self.assertEqual(code, 1)
        self.assertIn("add --yes", out)
        self.assertEqual(self.head(), before)
        self.assertFalse([call for call in self.calls if call[:2] in (["docker", "pull"], ["docker", "build"])])

    def test_answering_anything_but_yes_stops(self):
        with mock.patch.object(kit.sys.stdin, "isatty", return_value=True), mock.patch("builtins.input", return_value="y"):
            code, out = self.kit("update")
        self.assertEqual(code, 1)
        self.assertIn("Stopped, as asked", out)

    def test_something_already_unhealthy_stops_it_before_anything_changes(self):
        self.unhealthy = True
        code, out = self.kit("update", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("Not everything is running properly", out)
        self.assertIn("Nothing has changed", out)
        self.assertEqual(self.calls[-1][:2], ["docker", "ps"])

    def test_too_little_disk_stops_it(self):
        with mock.patch.object(kit.shutil, "disk_usage", return_value=Disk(10 * 1024**3, 9 * 1024**3, 1024**3)):
            code, out = self.kit("update", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("GB free", out)

    def test_a_failed_download_changes_nothing(self):
        self.answers[("docker", "pull")] = 1
        before = self.head()
        code, out = self.kit("update", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("Couldn't download", out)
        self.assertIn("Nothing has changed", out)
        self.assertEqual(self.head(), before)
        self.assertFalse([call for call in self.calls if call[0] == "restic"])

    def test_a_failed_build_changes_nothing(self):
        self.answers[("docker", "build")] = 1
        code, out = self.kit("update", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("didn't build, so nothing has changed", out)

    def test_a_failed_backup_leaves_the_old_version_running(self):
        self.answers[("restic", "--repo", str(self.paths.repo), "backup")] = 1
        before = self.head()
        code, out = self.kit("update", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("The backup failed, so nothing was updated", out)
        self.assertEqual(self.head(), before)
        self.assertTrue([call for call in self.calls if "up" in call and "--wait" in call])
        self.assertFalse((self.paths.backup_dir / "update.json").exists())

    def test_an_unfinished_update_has_to_be_finished_or_rolled_back_first(self):
        self.kit("update", "--yes")
        code, out = self.kit("update", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("kit update --finish", out)
        self.assertIn("kit update --rollback", out)


class Finishing(UpdateBase):
    def setUp(self):
        super().setUp()
        self.release("0.7.1", forgejo="15.0.12", before="Nothing.")
        self.kit("update", "--yes")
        self.calls.clear()
        patcher = mock.patch.object(kit, "apply")
        self.apply = patcher.start()
        self.addCleanup(patcher.stop)

    def test_when_every_check_passes_old_images_go_and_the_previous_ones_stay(self):
        code, out = self.kit("update", "--finish")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.state()["result"], "done")
        removed = [call[-1] for call in self.calls if call[:3] == ["docker", "image", "rm"]]
        self.assertEqual(removed, ["sallskap-core:0.6.0"])  # 0.7.0 was running before, and others' images are left alone
        self.assertIn(["docker", "builder", "prune", "--force"], self.calls)
        self.assertIn("Updated to 0.7.1", out)

    def test_every_check_runs(self):
        self.kit("update", "--finish")
        self.assertIn(("doctor", "check", "--run", "paths", "--run", "check-db-consistency"), self.forgejo)
        self.assertTrue([call for call in self.calls if call[-3:] == ["manage.py", "sync_tools", "--check"]])
        curl = next(call for call in self.calls if call[0] == "curl")
        self.assertIn("app.example.org:443:127.0.0.1", curl)

    def test_a_failed_check_stops_says_what_to_run_and_cleans_nothing(self):
        self.answers[("docker", "compose", "--project-directory", str(self.root), "exec", "-T", "sync")] = 1
        code, out = self.kit("update", "--finish")
        self.assertEqual(code, 1)
        self.assertIn("FAILED ITS CHECKS", out)
        self.assertIn("The worker can't reach every tool", out)
        self.assertIn("kit update --rollback", out)
        self.assertEqual(self.state()["result"], "failed")
        self.assertFalse([call for call in self.calls if call[:3] == ["docker", "image", "rm"]])

    def test_a_warning_from_forgejos_check_doesnt_fail(self):
        """It exits 0 either way, so its output is what says."""
        code, out = self.kit("update", "--finish")
        self.assertEqual(code, 0, out)
        self.assertIn("warns: Found 2 Orphaned TwoFactor", out)

    def test_an_error_from_forgejos_check_fails_the_update(self):
        self.doctor = "[1] Check Database Version\n - [E] the database is newer than this Forgejo\n"
        code, out = self.kit("update", "--finish")
        self.assertEqual(code, 1)
        self.assertIn("Forgejo's own check found: the database is newer than this Forgejo", out)

    def test_failing_to_start_is_a_failed_check_too(self):
        self.apply.side_effect = subprocess.CalledProcessError(1, ["docker", "compose", "up"])
        code, out = self.kit("update", "--finish")
        self.assertEqual(code, 1)
        self.assertIn("It didn't start", out)

    def test_there_has_to_be_an_update_to_finish(self):
        self.kit("update", "--finish")
        code, out = self.kit("update", "--finish")
        self.assertEqual(code, 1)
        self.assertIn("no update waiting", out)


class RollingBack(UpdateBase):
    def setUp(self):
        super().setUp()
        self.before = self.head()
        self.release("0.7.1", forgejo="15.0.12", before="Nothing.")
        self.kit("update", "--yes")

    def test_it_goes_back_to_the_old_code_which_restores_the_backup_from_before(self):
        code, out = self.kit("update", "--rollback", "--yes")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.head(), self.before)
        self.assertEqual(self.execs[-1][1][-4:], ["restore", "--force", "--snapshot", "snap1"])
        self.assertEqual(self.state()["result"], "rolled back")

    def test_it_says_what_is_lost(self):
        _, out = self.kit("update", "--rollback", "--yes")
        self.assertIn("Anything written since the backup is lost", out)

    def test_twice_is_refused(self):
        self.kit("update", "--rollback", "--yes")
        code, out = self.kit("update", "--rollback", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("no update to roll back", out)


class Status(UpdateBase):
    def test_a_newer_release_is_mentioned(self):
        self.release("0.7.1", forgejo="15.0.11", before="Nothing.")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(kit._update_status(self.paths), 0)
        self.assertIn("Sällskap 0.7.0. 0.7.1 is out", out.getvalue())

    def test_the_newest_says_so(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            kit._update_status(self.paths)
        self.assertIn("Sällskap 0.7.0, the newest release", out.getvalue())

    def test_a_failed_update_is_a_problem(self):
        self.release("0.7.1", forgejo="15.0.11", before="Nothing.")
        self.kit("update", "--yes")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(kit._update_status(self.paths), 1)
        self.assertIn("kit update --rollback", out.getvalue())


class RestoringOverAnInstallation(unittest.TestCase):
    def test_what_was_there_is_moved_aside_rather_than_mixed_in(self):
        """An old database with a newer `-wal` file beside it would be read as one."""
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            paths = kit.Paths(config_dir=base / "etc", core_dir=base / "core")
            (base / "core").mkdir()
            (base / "core" / "db.sqlite3-wal").write_text("newer")
            paths.config_dir.mkdir()
            moved = kit._move_aside({"paths": [str(base / "core"), str(paths.config_dir), str(base / "missing")]}, paths)
            self.assertEqual(len(moved), 1)
            self.assertFalse((base / "core").exists())
            self.assertTrue((moved[0] / "db.sqlite3-wal").exists())
            self.assertTrue(paths.config_dir.exists())


if __name__ == "__main__":
    unittest.main()
