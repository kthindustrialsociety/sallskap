"""The grep test (structure.md §7): nothing in core writes a unit, a tie, a grant, a seat
rule or the audit log but the actions and the few places named below, each for a reason.

It reads core's code with Python's own parser rather than a pattern, so it sees each call
and the function it's in. A write is a call that saves, deletes, creates or updates,
changes who takes part in a team, or one of core's own methods that write the tree. A
call counts if it names one of those models, or if it's a save or delete in a file that
imports them, as the parser can't tell what it's called on. Migrations and tests aren't
read: they write rows to set things up.

Django's admin page saves through its own code, which no scan of ours can see, so
test_actions.py checks that page separately: it refuses an operator every change."""

import ast
import re
from pathlib import Path

from django.test import SimpleTestCase

from .actions import ACTIONS

CORE = Path(__file__).resolve().parent.parent
MODELS = ("Unit", "Membership", "Position", "Reason", "Grant", "SeatRule")
TREE = (*MODELS, "AuditEntry", "taking_part", "teams_taking_part")
WRITES = {"save", "delete", "create", "get_or_create", "update_or_create", "update", "bulk_create", "bulk_update", "add", "remove", "set", "clear"}
CORES_OWN = {"end_on", "archive", "unarchive", "merge", "_erase", "link_account", "verify"}
UNSEEN = {"save", "delete"}

# Every function in these may write: they're the actions, and the models and seats they use.
WRITERS = {"structure/actions.py", "structure/models.py", "structure/seats.py"}

# And these functions elsewhere, each for its reason.
ALLOWED = {
    ("structure/server.py", "record_root_chair"): "kit chair, which records the root's chair on the server (structure.md §4)",
    ("structure/apps.py", "_account_saved"): "an account's person, which merges people on a verified address (structure.md §2)",
    ("structure/admin.py", "PersonAdmin.merge"): "an operator merging two people by hand, which is logged",
    ("structure/examples.py", "_remove"): "removing a made-up installation with --replace, on a test server only",
}


def _imports_the_tree(module):
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom) and node.module:
            names = {alias.name for alias in node.names}
            if node.module.endswith("models") and names & set(MODELS):  # the audit log alone gives nothing to save
                return True
            if node.module == "structure" and "models" in names:
                return True
    return False


def _is_write(call, imports):
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in CORES_OWN
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr in CORES_OWN:
        return True
    if func.attr in WRITES and re.search(rf"\b({'|'.join(TREE)})\b", ast.unparse(func.value)):
        return True
    return func.attr in UNSEEN and imports


def writes(source):
    """(the function it's in, its line, the call) for each call that could write the tree."""
    module = ast.parse(source)
    imports = _imports_the_tree(module)
    found = []

    def visit(node, where):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, f"{where}.{child.name}" if where else child.name)
                continue
            if isinstance(child, ast.Call) and _is_write(child, imports):
                found.append((where or "<module>", child.lineno, ast.unparse(child)[:120]))
            visit(child, where)

    visit(module, "")
    return found


def scanned():
    """Core's own code: everything but migrations and tests."""
    for path in sorted(CORE.rglob("*.py")):
        name = path.relative_to(CORE).as_posix()
        if "/migrations/" in f"/{name}" or path.name == "tests.py" or path.name.startswith("test_"):
            continue
        yield name, path.read_text("utf-8")


def _allowed(name, where):
    return any(name == path and (where == function or where.startswith(function + ".")) for path, function in ALLOWED)


class Writes(SimpleTestCase):
    def test_nothing_else_writes_the_tree(self):
        wrong = [
            f"{name}:{line} in {where}: {call}"
            for name, source in scanned()
            if name not in WRITERS
            for where, line, call in writes(source)
            if not _allowed(name, where)
        ]
        self.assertEqual(wrong, [], "Only an action may write this. Call one from structure/actions.py:\n" + "\n".join(wrong))

    def test_it_reads_every_part_of_core(self):
        names = {name for name, _ in scanned()}
        for expected in ("structure/admin.py", "structure/examples.py", "accounts/admin.py", "accounts/sync.py", "accounts/management/commands/create_admin.py", "sallskap/admin.py"):
            self.assertIn(expected, names)
        self.assertFalse([name for name in names if "migrations/" in name or "test" in Path(name).name])

    def test_it_sees_a_write_in_every_action(self):
        """So a scan that had stopped seeing anything would fail here, not pass."""
        found = {where.split(".")[0] for where, _, _ in writes((CORE / "structure/actions.py").read_text("utf-8"))}
        self.assertEqual(set(ACTIONS) - found, set())

    def test_every_exception_is_still_needed(self):
        sources = dict(scanned())
        unused = [key for key in ALLOWED if not any(_allowed(key[0], where) and where for where, _, _ in writes(sources.get(key[0], "")))]
        self.assertEqual(unused, [], "Take these out of ALLOWED: nothing there writes the tree any more.")

    def test_it_catches_writes_however_they_are_spelled(self):
        planted = '''
from structure.models import Position, Unit

def sneaky(position, unit, chosen):
    Unit.objects.filter(pk=1).update(name="x")
    position.end_on()
    unit.save()
    unit.taking_part.add(chosen)
    Position.objects.create(unit=unit)

class Form:
    def save(self, team):
        team.delete()
'''
        self.assertEqual([where for where, _, _ in writes(planted)], ["sneaky"] * 5 + ["Form.save"])

    def test_and_leaves_what_is_not_the_tree_alone(self):
        harmless = '''
from accounts.models import AccessSync

def fine(user, found):
    user.save()
    found.add("member")
    AccessSync.objects.filter(pk=1).update(pending=False)
'''
        self.assertEqual(writes(harmless), [])
