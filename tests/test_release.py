"""VERSION and CHANGELOG.md, and how kit reads them. See docs/releasing.md.

Run from the repo root with: python3 -m unittest discover -s tests"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if "kit" not in sys.modules:
    spec = importlib.util.spec_from_file_location("kit", ROOT / "kit" / "kit.py")
    sys.modules["kit"] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sys.modules["kit"])
kit = sys.modules["kit"]

SECTION = """\
## {number} ({date})

### Before you update

{before}

### Changes

- feat: something
"""


def changelog(*sections: tuple[str, str, str]) -> str:
    return "# Changelog\n\nIntro.\n\n" + "\n".join(SECTION.format(number=n, date=d, before=b) for n, d, b in sections)


class TheRealChangelog(unittest.TestCase):
    """What CI checks on every pull request, inside the existing Kit job."""

    def test_it_has_no_problems(self):
        text = (ROOT / "CHANGELOG.md").read_text("utf-8")
        self.assertEqual(kit.changelog_problems(text, kit.release_number(ROOT)), [])

    def test_version_is_a_bare_release_number(self):
        self.assertRegex((ROOT / "VERSION").read_text("utf-8"), r"^\d+\.\d+\.\d+\n$")


class ReleaseNumbers(unittest.TestCase):
    def test_they_sort_as_numbers(self):
        self.assertLess(kit.parse_release("0.9.0"), kit.parse_release("0.10.0"))
        self.assertEqual(kit.parse_release("v1.2.3"), (1, 2, 3))

    def test_anything_else_is_refused(self):
        for text in ("main", "1.2", "1.2.3-rc1", "1.2.3+abc", "v", ""):
            with self.subTest(text=text), self.assertRaises(kit.KitError):
                kit.parse_release(text)


class ReadingTheChangelog(unittest.TestCase):
    def test_sections_come_newest_first_with_what_to_read_first(self):
        releases = kit.parse_changelog(changelog(("0.7.0", "2026-11-20", "- Read this."), ("0.6.0", "2026-11-06", "- Nothing.")))
        self.assertEqual([(r.number, r.date) for r in releases], [("0.7.0", "2026-11-20"), ("0.6.0", "2026-11-06")])
        self.assertEqual(releases[0].before_updating, "- Read this.")
        self.assertNotIn("0.6.0", releases[0].text)

    def test_a_missing_section_for_version_is_a_problem(self):
        problems = kit.changelog_problems(changelog(("0.6.0", "2026-11-06", "Nothing.")), "0.7.0")
        self.assertEqual(len(problems), 1)
        self.assertIn("0.7.0", problems[0])

    def test_an_empty_before_you_update_is_a_problem(self):
        text = changelog(("0.7.0", "2026-11-20", ""), ("0.6.0", "2026-11-06", "Nothing."))
        self.assertIn("Write 'Nothing.'", " ".join(kit.changelog_problems(text, "0.7.0")))

    def test_a_missing_before_you_update_is_a_problem(self):
        text = "## 0.7.0 (2026-11-20)\n\n### Changes\n\n- fix: x\n"
        self.assertTrue(kit.changelog_problems(text, "0.7.0"))

    def test_sections_out_of_order_or_twice_are_a_problem(self):
        for order in ((("0.6.0", "Nothing."), ("0.7.0", "Nothing.")), (("0.7.0", "Nothing."), ("0.7.0", "Nothing."))):
            text = changelog(*((number, "2026-11-06", before) for number, before in order))
            with self.subTest(order=order):
                self.assertIn("newest first", " ".join(kit.changelog_problems(text, "0.7.0")))

    def test_a_heading_in_another_shape_isnt_a_release(self):
        self.assertEqual(kit.parse_changelog("## 0.7.0\n\n## Unreleased (2026-11-06)\n"), [])


class TheRunningVersion(unittest.TestCase):
    """Against a real git repository, shallow-cloned the way the installer does it."""

    def git(self, *args: str, cwd: Path) -> str:
        environment = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        identity = ["-c", "user.name=Test", "-c", "user.email=test@example.org", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false"]
        return subprocess.run(["git", *identity, *args], cwd=cwd, env=environment, check=True, capture_output=True, text=True).stdout

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.origin = Path(self._tmp.name) / "origin"
        self.origin.mkdir()
        self.git("init", "-q", "-b", "main", cwd=self.origin)
        (self.origin / "VERSION").write_text("0.7.0\n")
        self.git("add", "VERSION", cwd=self.origin)
        self.git("commit", "-q", "-m", "release", cwd=self.origin)
        self.git("tag", "-a", "v0.7.0", "-m", "notes", cwd=self.origin)

    def clone(self, ref: str) -> Path:
        target = Path(self._tmp.name) / f"clone-{ref}"
        self.git("clone", "-q", "--depth", "1", "--branch", ref, f"file://{self.origin}", str(target), cwd=self.origin)
        return target

    def test_a_checkout_of_the_tag_is_that_release(self):
        self.assertEqual(kit.running_version(self.clone("v0.7.0")), "0.7.0")

    def test_a_later_commit_is_the_release_plus_the_commit(self):
        (self.origin / "other").write_text("x")
        self.git("add", "other", cwd=self.origin)
        self.git("commit", "-q", "-m", "later", cwd=self.origin)
        clone = self.clone("main")
        commit = self.git("rev-parse", "--short", "HEAD", cwd=clone).strip()
        self.assertEqual(kit.running_version(clone), f"0.7.0+{commit}")


if __name__ == "__main__":
    unittest.main()
