"""The two made-up installations, for core's tests, the test server and later the
public demo. They grow with each segment: units and people now, then grants, seat
rules and words. Nothing calls this on a real installation. The test server loads
one with tests/load_example.py.

Reserved names only (RFC 2606), so nothing can be mistaken for a real association."""

from dataclasses import dataclass
from datetime import date

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from .models import EmailAddress, Membership, Person, Position, Reason, Unit, _erase

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


ROOT = ""  # in a tie, the root, whatever its key


@dataclass(frozen=True)
class Tie:
    unit: str  # a key, or ROOT
    what: str  # a membership's kind, a position's role or a reason's kind
    start: date
    end: date | None = None  # a reason's expiry
    title: str = ""
    basis: str = ""  # a reason's legal basis
    source: str = ""  # a reason's


def member(unit, start, end=None, kind=Membership.Kind.ORDINARY):
    return Tie(unit, kind, start, end)


def holds(unit, role, start, end=None, title=""):
    return Tie(unit, role, start, end, title)


def alumnus(unit, start, expires):
    return Tie(unit, Reason.Kind.ALUMNUS, start, expires, basis=Reason.Basis.CONSENT, source="The alumni offer, when their membership ended")


@dataclass(frozen=True)
class Who:
    name: str
    email: str
    account: bool = True
    memberships: tuple[Tie, ...] = ()
    positions: tuple[Tie, ...] = ()
    reasons: tuple[Tie, ...] = ()


@dataclass(frozen=True)
class Example:
    root_key: str  # used when the tests make the root. On a server, it comes from the config
    root_name: str
    units: tuple[Spec, ...]
    people: tuple[Who, ...] = ()


R = Position.Role
HONORARY = Membership.Kind.HONORARY


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
    (
        # The board, elected at the annual meeting.
        Who("Alva Example", "alva@example.org", memberships=(member(ROOT, date(2022, 9, 1)),), positions=(holds(ROOT, R.CHAIR, date(2026, 3, 1)),)),
        Who("Bruno Example", "bruno@example.org", memberships=(member(ROOT, date(2023, 9, 1)),), positions=(holds(ROOT, R.VICE_CHAIR, date(2026, 3, 1)),)),
        Who("Cleo Example", "cleo@example.org", memberships=(member(ROOT, date(2023, 9, 1)),), positions=(holds(ROOT, R.TREASURER, date(2025, 3, 1)),)),
        Who("Dag Example", "dag@example.org", memberships=(member(ROOT, date(2024, 9, 1)),), positions=(holds(ROOT, R.SECRETARY, date(2026, 3, 1)),)),
        Who("Ebba Example", "ebba@example.org", memberships=(member(ROOT, date(2024, 9, 1)),), positions=(holds(ROOT, R.BOARD_MEMBER, date(2026, 3, 1)),)),
        Who("Frans Example", "frans@example.org", memberships=(member(ROOT, date(2025, 9, 1)),), positions=(holds(ROOT, R.BOARD_MEMBER, date(2026, 3, 1)),)),
        # A lead for each team, and last year's project with its history.
        Who("Gun Example", "gun@example.org", memberships=(member(ROOT, date(2024, 9, 1)),), positions=(holds("events", R.LEAD, date(2025, 9, 1)),)),
        Who("Hampus Example", "hampus@example.org", memberships=(member(ROOT, date(2025, 9, 1)),), positions=(holds("photography", R.LEAD, date(2026, 1, 15)),)),
        Who("Ines Example", "ines@example.org", memberships=(member(ROOT, date(2023, 9, 1)),), positions=(holds("website", R.LEAD, date(2024, 9, 1)),)),
        Who("Jon Example", "jon@example.org", memberships=(member(ROOT, date(2025, 9, 1)),), positions=(holds("spring-hackathon-2027", R.LEAD, date(2026, 10, 1)),)),
        Who("Kaj Example", "kaj@example.org", memberships=(member(ROOT, date(2024, 9, 1)),), positions=(holds("autumn-hackathon-2025", R.LEAD, date(2025, 8, 1), date(2025, 11, 20)),)),
        # Participants, one of them from a partner company, who isn't a member.
        Who("Lo Example", "lo@example.org", memberships=(member(ROOT, date(2025, 9, 1)),), positions=(
            holds("events", R.PARTICIPANT, date(2025, 9, 15)),
            holds("autumn-hackathon-2025", R.PARTICIPANT, date(2025, 9, 1), date(2025, 11, 20)),
        )),
        Who("Mio Example", "mio@example.org", memberships=(member(ROOT, date(2025, 9, 1)),), positions=(
            holds("website", R.PARTICIPANT, date(2025, 10, 1)),
            holds("photography", R.PARTICIPANT, date(2026, 2, 1)),
        )),
        Who("Nora Partner", "nora@example.net", positions=(holds("spring-hackathon-2027", R.PARTICIPANT, date(2026, 10, 1), title="Mentor from a partner company"),)),
        # Members: honorary, lapsed, an alumnus, and one with no account.
        Who("Olof Example", "olof@example.org", memberships=(member(ROOT, date(2015, 5, 1), kind=HONORARY),)),
        Who("Petra Example", "petra@example.org", memberships=(member(ROOT, date(2023, 9, 1), date(2026, 1, 1)),)),
        Who("Rut Example", "rut@example.org", account=False, memberships=(member(ROOT, date(2020, 9, 1), date(2025, 7, 1)),), reasons=(alumnus(ROOT, date(2025, 7, 1), date(2030, 7, 1)),)),
        Who("Sixten Example", "sixten@example.org", account=False, memberships=(member(ROOT, date(2026, 9, 1)),)),
    ),
)

# The hard case: the tree in structure.md §1, with a second association under the root.
FEDERATION = Example(
    "example-federation",
    "Example Federation",
    (
        Spec("extended-board", "Extended board", description="The board and the chairs of the associations inside."),
        Spec("advisory-board", "Advisory board", description="Alumni and friends who advise the board."),
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
    (
        # The root's board: an ended chair and their successor, and a member of the root directly.
        Who("Viktor Example", "viktor@example.org", memberships=(member("robotics", date(2021, 9, 1)),), positions=(holds(ROOT, R.CHAIR, date(2025, 7, 1), date(2026, 7, 1)),)),
        Who("Una Example", "una@example.org", memberships=(member("chemistry", date(2022, 9, 1)),), positions=(holds(ROOT, R.CHAIR, date(2026, 7, 1)),)),
        Who("Tor Example", "tor@example.org", memberships=(member("robotics", date(2022, 9, 1)),), positions=(holds(ROOT, R.VICE_CHAIR, date(2026, 7, 1)),)),
        Who("Selma Example", "selma@example.org", memberships=(member(ROOT, date(2024, 9, 1)),), positions=(holds(ROOT, R.TREASURER, date(2026, 7, 1)),)),
        Who("Rikard Example", "rikard@example.org", memberships=(member("chemistry", date(2023, 9, 1)),), positions=(holds(ROOT, R.SECRETARY, date(2026, 7, 1)),)),
        # Robotics, and one person with positions in both associations.
        Who("Quinn Example", "quinn@example.org", memberships=(member("robotics", date(2023, 9, 1)),), positions=(holds("robotics", R.CHAIR, date(2026, 5, 1)),)),
        Who("Pia Example", "pia@example.org", memberships=(member("robotics", date(2023, 9, 1)),), positions=(holds("robotics", R.VICE_CHAIR, date(2026, 5, 1)),)),
        Who("Wilma Example", "wilma@example.org", memberships=(member("robotics", date(2022, 9, 1)), member("chemistry", date(2024, 1, 15))), positions=(
            holds("robotics", R.TREASURER, date(2026, 5, 1)),
            holds("chemistry", R.BOARD_MEMBER, date(2026, 4, 1)),
        )),
        Who("Otto Example", "otto@example.org", memberships=(member("robotics", date(2024, 9, 1)),), positions=(holds("operations", R.LEAD, date(2025, 9, 1)),)),
        Who("Nils Example", "nils@example.org", memberships=(member("robotics", date(2023, 9, 1)),), positions=(holds("uavs", R.LEAD, date(2025, 1, 10)),)),
        Who("Mira Example", "mira@example.org", memberships=(member("robotics", date(2024, 9, 1)),), positions=(
            holds("drone-race-2027", R.LEAD, date(2026, 9, 1)),
            holds("development", R.PARTICIPANT, date(2025, 2, 1)),
        )),
        Who("Leo Example", "leo@example.org", memberships=(member("robotics", date(2025, 9, 1)),), positions=(holds("chemistry-website", R.LEAD, date(2026, 2, 1)),)),
        Who("Elsa Example", "elsa@example.org", memberships=(member("robotics", date(2025, 9, 1)),), positions=(holds("uavs", R.PARTICIPANT, date(2025, 10, 1)),)),
        # Chemistry.
        Who("Kim Example", "kim@example.org", memberships=(member("chemistry", date(2023, 9, 1)),), positions=(holds("chemistry", R.CHAIR, date(2026, 4, 1)),)),
        Who("Jana Example", "jana@example.org", memberships=(member("chemistry", date(2024, 9, 1)),), positions=(holds("lab-nights", R.LEAD, date(2025, 9, 1)),)),
        Who("Disa Example", "disa@example.org", account=False, memberships=(member("chemistry", date(2026, 9, 1)),), positions=(holds("lab-nights", R.PARTICIPANT, date(2026, 9, 15)),)),
        # The hackathon, with Robotics and Chemistry taking part, and someone from outside.
        Who("Ivar Example", "ivar@example.org", memberships=(member("chemistry", date(2025, 9, 1)),), positions=(holds("spring-hackathon-2027", R.LEAD, date(2026, 9, 1)),)),
        Who("Hedda Partner", "hedda@example.net", positions=(holds("spring-hackathon-2027", R.PARTICIPANT, date(2026, 10, 1), title="Judge from a partner company"),)),
        # The advisory board: an alumnus, who is no longer a member, and someone from outside.
        Who("Greta Example", "greta@example.org", memberships=(member("robotics", date(2015, 9, 1), date(2020, 7, 1)),), positions=(holds("advisory-board", R.PARTICIPANT, date(2024, 1, 1), title="Advisor"),), reasons=(alumnus("robotics", date(2020, 7, 1), date(2030, 7, 1)),)),
        Who("Folke Advisor", "folke@example.net", positions=(holds("advisory-board", R.PARTICIPANT, date(2024, 1, 1), title="Advisor"),)),
    ),
)

EXAMPLES = {"society": SOCIETY, "federation": FEDERATION}


def _all(specs):
    for spec in specs:
        yield spec
        yield from _all(spec.children)


def example_addresses():
    return {who.email for example in EXAMPLES.values() for who in example.people}


def _remove(root, below):
    """Everything an example made: the units under the root, every tie to them, and
    the made-up people with their accounts. Ties and people are otherwise never
    deleted like this, so it's only for a test server."""
    addresses = example_addresses()
    people = Person.objects.filter(emails__address__in=addresses)
    for model, unit in ((Membership, "association"), (Position, "unit"), (Reason, "unit")):
        _erase(model.objects.filter(models.Q(**{f"{unit}__in": below}) | models.Q(person__in=people)))
    get_user_model().objects.filter(email__in=addresses).delete()
    _erase(Person.objects.filter(pk__in=list(people.values_list("pk", flat=True))))


def _add_people(people, units, root):
    User = get_user_model()

    def where(key):
        return root if key == ROOT else units[key]

    for who in people:
        if who.account:
            person = User.objects.create_user(who.email, name=who.name).person
        else:
            person = Person.objects.create(name=who.name)
            EmailAddress.objects.create(person=person, address=who.email, verified_at=timezone.now())
        for tie in who.memberships:
            Membership.objects.create(person=person, association=where(tie.unit), kind=tie.what, start=tie.start, end=tie.end)
        for tie in who.positions:
            Position.objects.create(person=person, unit=where(tie.unit), role=tie.what, title=tie.title, start=tie.start, end=tie.end)
        for tie in who.reasons:
            Reason.objects.create(person=person, unit=where(tie.unit), kind=tie.what, legal_basis=tie.basis, source=tie.source, start=tie.start, expires=tie.end)


@transaction.atomic
def build(name, root, replace=False):
    """Build an example's units and people under `root`, which keeps its own name and
    key. With `replace`, everything under the root goes first, which only an example
    should have, and so do the made-up people of both examples."""
    below = root.descendants()
    if below.exists() or Person.objects.filter(emails__address__in=example_addresses()).exists():
        if not replace:
            raise ValidationError(f"{root} already has units or made-up people. Use --replace to remove them first.")
        _remove(root, below)
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
    _add_people(example.people, units, root)
    for spec in _all(example.units):  # last, as nobody is added to an archived team
        if spec.archived:
            units[spec.key].archive()
    return units
