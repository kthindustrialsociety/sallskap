"""The two made-up installations, for core's tests, the test server and later the
public demo. They grow with each segment: units now, then people, positions, grants,
seat rules and words. Nothing calls this on a real installation. The test server loads
one with tests/load_example.py.

Reserved names only (RFC 2606), so nothing can be mistaken for a real association."""

from dataclasses import dataclass
from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import Unit

TEAM, ASSOCIATION = Unit.Kind.TEAM, Unit.Kind.ASSOCIATION


@dataclass(frozen=True)
class Spec:
    key: str
    name: str
    kind: str = TEAM
    description: str = ""
    end_date: date | None = None
    archived: bool = False
    run_for: str | None = None  # a key
    taking_part: tuple[str, ...] = ()  # keys
    children: tuple["Spec", ...] = ()


@dataclass(frozen=True)
class Example:
    root_key: str  # used when the tests make the root. On a server, it comes from the config
    root_name: str
    units: tuple[Spec, ...]


# The common case: one association, with teams and projects, and nothing of federations.
SOCIETY = Example(
    "example-society",
    "Example Society",
    (
        Spec("events", "Events", description="Pub nights, lectures and the spring party.", children=(
            Spec("photography", "Photography", description="Takes the pictures at our events."),
        )),
        Spec("website", "Website", description="Keeps example.org running."),
        Spec("spring-hackathon-2027", "Spring hackathon 2027", end_date=date(2027, 4, 25)),
        Spec("autumn-hackathon-2025", "Autumn hackathon 2025", end_date=date(2025, 11, 15), archived=True),
    ),
)

# The hard case: the tree in structure.md §1, with a second association under the root.
FEDERATION = Example(
    "example-federation",
    "Example Federation",
    (
        Spec("extended-board", "Extended board", description="The board and the chairs of the associations inside."),
        Spec("spring-hackathon-2027", "Spring hackathon 2027", end_date=date(2027, 4, 25), taking_part=("robotics", "chemistry")),
        Spec("robotics", "Example Robotics", ASSOCIATION, children=(
            Spec("operations", "Operations"),
            Spec("development", "Development", children=(
                Spec("uavs", "UAVs", children=(
                    Spec("drone-race-2027", "Drone race 2027", end_date=date(2027, 6, 5)),
                )),
            )),
            Spec("chemistry-website", "Example Chemistry's website", run_for="chemistry"),
        )),
        Spec("chemistry", "Example Chemistry", ASSOCIATION, children=(
            Spec("lab-nights", "Lab nights"),
        )),
    ),
)

EXAMPLES = {"society": SOCIETY, "federation": FEDERATION}


def _all(specs):
    for spec in specs:
        yield spec
        yield from _all(spec.children)


@transaction.atomic
def build(name, root, replace=False):
    """Build an example's units under `root`, which keeps its own name and key. With
    `replace`, everything under the root goes first, which only an example should have."""
    below = root.descendants()
    if below.exists():
        if not replace:
            raise ValidationError(f"{root} already has units under it. Use --replace to remove them first.")
        Unit.taking_part.through.objects.filter(from_unit__in=below).delete()
        below.update(run_for=None)
        for unit in below.order_by("-path"):  # the deepest first, as a unit with units under it can't go
            unit.delete()

    units = {}

    def create(specs, parent):
        for spec in specs:
            unit = Unit(kind=spec.kind, key=spec.key, name=spec.name, description=spec.description, parent=parent, end_date=spec.end_date)
            unit.save()
            units[spec.key] = unit
            create(spec.children, unit)

    example = EXAMPLES[name]
    create(example.units, root)
    for spec in _all(example.units):
        unit = units[spec.key]
        if spec.run_for:
            unit.run_for = units[spec.run_for]
            unit.save()
        if spec.taking_part:
            unit.taking_part.add(*(units[key] for key in spec.taking_part))
        if spec.archived:
            unit.archive()
    return units
