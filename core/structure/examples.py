"""The two made-up installations, for core's tests, the test server and later the
public demo. They grow with each segment: units, people, grants and seat rules so
far. Nothing calls this on a real installation. The test server loads
one with tests/load_example.py.

They're built as an installation would be: the first chair is recorded as `kit chair`
does, and everything else is an action (structure/actions.py) done by the made-up person
who did it, so loading one goes through the permission checks and fills the audit log.
Each tie can say who recorded it; otherwise it's the root's chair at that point. Last,
made-up people with nothing left are deleted, as core would that night (deleting.py).

Reserved names only (RFC 2606), so nothing can be mistaken for a real association."""

from dataclasses import dataclass
from datetime import date, datetime, time

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from . import actions
from .deleting import delete_people
from .models import AuditEntry, EmailAddress, Grant, Membership, Permission, Person, Position, Reason, SeatRule, Unit, _erase
from .server import record_root_chair

TEAM, ASSOCIATION = Unit.Kind.TEAM, Unit.Kind.ASSOCIATION
SERVER = AuditEntry.Via.SERVER


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
    by: str = ""  # the address of whoever records it. Empty: the root's chair at that point


def member(unit, start, end=None, kind=Membership.Kind.ORDINARY, by=""):
    return Tie(unit, kind, start, end, by=by)


def holds(unit, role, start, end=None, title="", by=""):
    return Tie(unit, role, start, end, title, by=by)


def alumnus(unit, start, expires, by=""):
    return Tie(unit, Reason.Kind.ALUMNUS, start, expires, basis=Reason.Basis.CONSENT, source="The alumni offer, when their membership ended", by=by)


@dataclass(frozen=True)
class Who:
    name: str
    email: str
    account: bool = True
    memberships: tuple[Tie, ...] = ()
    positions: tuple[Tie, ...] = ()
    reasons: tuple[Tie, ...] = ()


@dataclass(frozen=True)
class Given:
    """A grant: a permission to a role held in a unit, over a unit, given by someone."""

    permission: str
    role: str
    held_in: str  # a key, or ROOT
    unit: str  # a key, or ROOT
    start: date
    by: str  # the giver's address
    include_below: bool = False


@dataclass(frozen=True)
class Seats:
    """A seat rule: a role in a unit for whoever holds one of some roles held in a unit,
    or in each association directly under it. Core gives the seats."""

    unit: str  # a key, or ROOT
    role: str
    from_roles: tuple[str, ...]
    held_in: str  # a key, or ROOT
    start: date
    by: str  # the maker's address
    each_association_under: bool = False


@dataclass(frozen=True)
class Example:
    root_key: str  # used when the tests make the root. On a server, it comes from the config
    root_name: str
    units: tuple[Spec, ...]
    people: tuple[Who, ...] = ()  # in the order they're recorded. The first is the first chair
    grants: tuple[Given, ...] = ()  # none in Example Society: they're optional
    seat_rules: tuple[Seats, ...] = ()  # none in Example Society either


R = Position.Role
HONORARY = Membership.Kind.HONORARY
ALVA, CLEO, GUN, INES, HAMPUS, JON = (f"{name}@example.org" for name in ("alva", "cleo", "gun", "ines", "hampus", "jon"))


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
        # The board, elected at the annual meeting. The treasurer records the members.
        Who("Alva Example", ALVA, memberships=(member(ROOT, date(2022, 9, 1)),), positions=(holds(ROOT, R.CHAIR, date(2026, 3, 1)),)),
        Who("Bruno Example", "bruno@example.org", memberships=(member(ROOT, date(2023, 9, 1)),), positions=(holds(ROOT, R.VICE_CHAIR, date(2026, 3, 1)),)),
        Who("Cleo Example", CLEO, memberships=(member(ROOT, date(2023, 9, 1)),), positions=(holds(ROOT, R.TREASURER, date(2025, 3, 1)),)),
        Who("Dag Example", "dag@example.org", memberships=(member(ROOT, date(2024, 9, 1), by=CLEO),), positions=(holds(ROOT, R.SECRETARY, date(2026, 3, 1)),)),
        Who("Ebba Example", "ebba@example.org", memberships=(member(ROOT, date(2024, 9, 1), by=CLEO),), positions=(holds(ROOT, R.BOARD_MEMBER, date(2026, 3, 1)),)),
        Who("Frans Example", "frans@example.org", memberships=(member(ROOT, date(2025, 9, 1), by=CLEO),), positions=(holds(ROOT, R.BOARD_MEMBER, date(2026, 3, 1)),)),
        # A lead for each team, and last year's project with its history. Events' lead
        # appoints the lead of Photography, a team inside it.
        Who("Gun Example", GUN, memberships=(member(ROOT, date(2024, 9, 1), by=CLEO),), positions=(holds("events", R.LEAD, date(2025, 9, 1)),)),
        Who("Hampus Example", HAMPUS, memberships=(member(ROOT, date(2025, 9, 1), by=CLEO),), positions=(holds("photography", R.LEAD, date(2026, 1, 15), by=GUN),)),
        Who("Ines Example", INES, memberships=(member(ROOT, date(2023, 9, 1), by=CLEO),), positions=(holds("website", R.LEAD, date(2024, 9, 1)),)),
        Who("Jon Example", JON, memberships=(member(ROOT, date(2025, 9, 1), by=CLEO),), positions=(holds("spring-hackathon-2027", R.LEAD, date(2026, 10, 1)),)),
        Who("Kaj Example", "kaj@example.org", memberships=(member(ROOT, date(2024, 9, 1), by=CLEO),), positions=(holds("autumn-hackathon-2025", R.LEAD, date(2025, 8, 1), date(2025, 11, 20)),)),
        # Participants, added by their leads, one of them from a partner company, who isn't a member.
        Who("Lo Example", "lo@example.org", memberships=(member(ROOT, date(2025, 9, 1), by=CLEO),), positions=(
            holds("events", R.PARTICIPANT, date(2025, 9, 15), by=GUN),
            holds("autumn-hackathon-2025", R.PARTICIPANT, date(2025, 9, 1), date(2025, 11, 20)),
        )),
        Who("Mio Example", "mio@example.org", memberships=(member(ROOT, date(2025, 9, 1), by=CLEO),), positions=(
            holds("website", R.PARTICIPANT, date(2025, 10, 1), by=INES),
            holds("photography", R.PARTICIPANT, date(2026, 2, 1), by=HAMPUS),
        )),
        Who("Nora Partner", "nora@example.net", positions=(holds("spring-hackathon-2027", R.PARTICIPANT, date(2026, 10, 1), title="Mentor from a partner company", by=JON),)),
        # Members: honorary, lapsed, an alumnus, and one with no account.
        Who("Olof Example", "olof@example.org", memberships=(member(ROOT, date(2015, 5, 1), kind=HONORARY, by=CLEO),)),
        Who("Petra Example", "petra@example.org", memberships=(member(ROOT, date(2023, 9, 1), date(2026, 1, 1), by=CLEO),)),
        Who("Rut Example", "rut@example.org", account=False, memberships=(member(ROOT, date(2020, 9, 1), date(2025, 7, 1), by=CLEO),), reasons=(alumnus(ROOT, date(2025, 7, 1), date(2030, 7, 1), by=CLEO),)),
        Who("Sixten Example", "sixten@example.org", account=False, memberships=(member(ROOT, date(2026, 9, 1), by=CLEO),)),
    ),
)

QUINN, PIA, WILMA, NILS, KIM, JANA, IVAR = (f"{name}@example.org" for name in ("quinn", "pia", "wilma", "nils", "kim", "jana", "ivar"))

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
        # The root's board: an ended chair and their successor, and a member of the root
        # directly. Viktor, the first chair, gives a grant and makes a seat rule (below),
        # then records Una as his successor, which ends his own position.
        Who("Viktor Example", "viktor@example.org", memberships=(member("robotics", date(2021, 9, 1)),), positions=(holds(ROOT, R.CHAIR, date(2025, 7, 1), date(2026, 7, 1)),)),
        Who("Una Example", "una@example.org", memberships=(member("chemistry", date(2022, 9, 1)),), positions=(holds(ROOT, R.CHAIR, date(2026, 7, 1)),)),
        Who("Tor Example", "tor@example.org", memberships=(member("robotics", date(2022, 9, 1)),), positions=(holds(ROOT, R.VICE_CHAIR, date(2026, 7, 1)),)),
        Who("Selma Example", "selma@example.org", memberships=(member(ROOT, date(2024, 9, 1)),), positions=(holds(ROOT, R.TREASURER, date(2026, 7, 1)),)),
        Who("Rikard Example", "rikard@example.org", memberships=(member("chemistry", date(2023, 9, 1)),), positions=(holds(ROOT, R.SECRETARY, date(2026, 7, 1)),)),
        Who("Ylva Example", "ylva@example.org", memberships=(member("chemistry", date(2023, 9, 1)),), positions=(holds(ROOT, R.BOARD_MEMBER, date(2026, 7, 1)),)),
        # Robotics, and one person with positions in both associations. Astrid was chair
        # before Quinn, and her seat on the root's board ended with it (below). Quinn
        # records Robotics' board, and Wilma, its treasurer, its members.
        Who("Astrid Example", "astrid@example.org", memberships=(member("robotics", date(2020, 9, 1)),), positions=(holds("robotics", R.CHAIR, date(2024, 5, 1), date(2026, 5, 1)),)),
        Who("Quinn Example", QUINN, memberships=(member("robotics", date(2023, 9, 1)),), positions=(holds("robotics", R.CHAIR, date(2026, 5, 1)),)),
        Who("Pia Example", PIA, memberships=(member("robotics", date(2023, 9, 1), by=QUINN),), positions=(holds("robotics", R.VICE_CHAIR, date(2026, 5, 1), by=QUINN),)),
        Who("Wilma Example", WILMA, memberships=(member("robotics", date(2022, 9, 1), by=QUINN), member("chemistry", date(2024, 1, 15))), positions=(
            holds("robotics", R.TREASURER, date(2026, 5, 1), by=QUINN),
            holds("chemistry", R.BOARD_MEMBER, date(2026, 4, 1)),
        )),
        # Operations changed lead, and the grant to its lead passed on (below).
        Who("Otto Example", "otto@example.org", memberships=(member("robotics", date(2024, 9, 1), by=WILMA),), positions=(holds("operations", R.LEAD, date(2025, 9, 1), date(2026, 10, 1), by=QUINN),)),
        Who("Zara Example", "zara@example.org", memberships=(member("robotics", date(2024, 9, 1), by=WILMA),), positions=(holds("operations", R.LEAD, date(2026, 10, 1), by=QUINN),)),
        # Pia leads Development by a seat, and so appoints UAVs' lead, who appoints Drone race's.
        Who("Nils Example", NILS, memberships=(member("robotics", date(2023, 9, 1), by=WILMA),), positions=(holds("uavs", R.LEAD, date(2025, 1, 10), by=PIA),)),
        Who("Mira Example", "mira@example.org", memberships=(member("robotics", date(2024, 9, 1), by=WILMA),), positions=(
            holds("drone-race-2027", R.LEAD, date(2026, 9, 1), by=NILS),
            holds("development", R.PARTICIPANT, date(2025, 2, 1), by=PIA),
        )),
        Who("Leo Example", "leo@example.org", memberships=(member("robotics", date(2025, 9, 1), by=WILMA),), positions=(holds("chemistry-website", R.LEAD, date(2026, 2, 1), by=QUINN),)),
        Who("Elsa Example", "elsa@example.org", memberships=(member("robotics", date(2025, 9, 1), by=WILMA),), positions=(holds("uavs", R.PARTICIPANT, date(2025, 10, 1), by=NILS),)),
        # Bodil flew with UAVs before Elsa. She had no account, and nothing left once she'd
        # left, so core has deleted her: her position stays under her name, and nothing else
        # of hers (structure.md §3).
        Who("Bodil Example", "bodil@example.org", account=False, memberships=(member("robotics", date(2021, 9, 1), date(2025, 6, 1), by=WILMA),), positions=(holds("uavs", R.PARTICIPANT, date(2024, 1, 15), date(2025, 6, 1), by=NILS),)),
        # Chemistry, whose chair records its members and lead.
        Who("Kim Example", KIM, memberships=(member("chemistry", date(2023, 9, 1)),), positions=(holds("chemistry", R.CHAIR, date(2026, 4, 1)),)),
        Who("Jana Example", JANA, memberships=(member("chemistry", date(2024, 9, 1), by=KIM),), positions=(holds("lab-nights", R.LEAD, date(2025, 9, 1), by=KIM),)),
        Who("Disa Example", "disa@example.org", account=False, memberships=(member("chemistry", date(2026, 9, 1), by=KIM),), positions=(holds("lab-nights", R.PARTICIPANT, date(2026, 9, 15), by=JANA),)),
        # The hackathon, with Robotics and Chemistry taking part, and someone from outside.
        Who("Ivar Example", IVAR, memberships=(member("chemistry", date(2025, 9, 1), by=KIM),), positions=(holds("spring-hackathon-2027", R.LEAD, date(2026, 9, 1)),)),
        Who("Hedda Partner", "hedda@example.net", positions=(holds("spring-hackathon-2027", R.PARTICIPANT, date(2026, 10, 1), title="Judge from a partner company", by=IVAR),)),
        # The advisory board: an alumnus, who is no longer a member, and someone from outside.
        Who("Greta Example", "greta@example.org", memberships=(member("robotics", date(2015, 9, 1), date(2020, 7, 1), by=WILMA),), positions=(holds("advisory-board", R.PARTICIPANT, date(2024, 1, 1), title="Advisor"),), reasons=(alumnus("robotics", date(2020, 7, 1), date(2030, 7, 1), by=WILMA),)),
        Who("Folke Advisor", "folke@example.net", positions=(holds("advisory-board", R.PARTICIPANT, date(2024, 1, 1), title="Advisor"),)),
    ),
    (
        # structure.md §4's three. The first outlived Viktor, the chair who gave it.
        Given(Permission.MANAGE_FINANCES, R.TREASURER, ROOT, ROOT, date(2025, 9, 1), "viktor@example.org", include_below=True),
        Given(Permission.SEE_MEMBERS, R.BOARD_MEMBER, ROOT, ROOT, date(2026, 7, 1), "una@example.org", include_below=True),
        Given(Permission.SEND_MAILINGS, R.LEAD, "operations", "robotics", date(2026, 5, 15), QUINN),
    ),
    (
        # structure.md §4's three, the extended board as two rules. The grant to the
        # root's board members above reaches Quinn and Kim, seated on it.
        Seats(ROOT, R.BOARD_MEMBER, (R.CHAIR,), ROOT, date(2025, 9, 1), "viktor@example.org", each_association_under=True),
        Seats("extended-board", R.PARTICIPANT, Position.BOARD_ROLES, ROOT, date(2026, 7, 1), "una@example.org"),
        Seats("extended-board", R.PARTICIPANT, (R.CHAIR, R.VICE_CHAIR), ROOT, date(2026, 7, 1), "una@example.org", each_association_under=True),
        Seats("development", R.LEAD, (R.VICE_CHAIR,), "robotics", date(2026, 5, 1), QUINN),
    ),
)

EXAMPLES = {"society": SOCIETY, "federation": FEDERATION}


def _all(specs):
    for spec in specs:
        yield spec
        yield from _all(spec.children)


def example_addresses():
    return {who.email for example in EXAMPLES.values() for who in example.people}


def example_names():
    return {who.name for example in EXAMPLES.values() for who in example.people}


def _remove(root):
    """Everything an example made: the units under the root, every tie to them, and
    the made-up people with their accounts. Ties and people are otherwise never
    deleted like this, so it's only for a test server. The audit log keeps its rows."""
    below = root.descendants()
    addresses = example_addresses()
    people = Person.objects.filter(emails__address__in=addresses)
    _erase(Grant.objects.filter(models.Q(unit__in=below) | models.Q(held_in__in=below) | models.Q(given_by__in=people)))
    # Seats first, as each points at its rule and at the position it comes from.
    rules = models.Q(unit__in=below) | models.Q(held_in__in=below) | models.Q(made_by__in=people)
    deleted = models.Q(person__isnull=True, kept_name__in=example_names())  # the made-up people since deleted
    positions = models.Q(unit__in=below) | models.Q(person__in=people) | deleted
    _erase(Position.objects.filter(models.Q(seat_rule__in=SeatRule.objects.filter(rules)) | models.Q(seat_from__in=Position.objects.filter(positions)) | positions & models.Q(seat_rule__isnull=False)))
    _erase(SeatRule.objects.filter(rules))
    for model, unit in ((Membership, "association"), (Position, "unit"), (Reason, "unit")):
        _erase(model.objects.filter(models.Q(**{f"{unit}__in": below}) | models.Q(person__in=people)))
    _erase(Position.objects.filter(deleted))
    get_user_model().objects.filter(email__in=addresses).delete()
    _erase(Person.objects.filter(pk__in=list(people.values_list("pk", flat=True))))
    Unit.taking_part.through.objects.filter(from_unit__in=below).delete()
    below.update(run_for=None)
    for unit in below.order_by("-path"):  # the deepest first, as a unit with units under it can't go
        unit.delete()


class _Builder:
    def __init__(self, example, root):
        self.example, self.root = example, root
        self.units = {}
        self.people = {}  # by address

    def where(self, key):
        return self.root if key == ROOT else self.units[key]

    def actor(self, address=""):
        """Who records something: the person named, or the root's chair at that point."""
        if address:
            return self.people[address]
        return Position.objects.current().get(unit=self.root, role=R.CHAIR).person

    def add_person(self, who):
        if who.account:  # as an operator adds an account: not an action
            person = get_user_model().objects.create_user(who.email, name=who.name).person
        else:
            person = Person.objects.create(name=who.name)
            EmailAddress.objects.create(person=person, address=who.email, verified_at=timezone.now())
        # As if added when their first tie started, as they would have been, so that the 30
        # days after someone is added don't keep anyone with nothing left (deleting.py).
        first = min((tie.start for tie in (*who.memberships, *who.positions, *who.reasons)), default=None)
        if first is not None:  # someone with no ties is added today
            Person.objects.filter(pk=person.pk).update(created_at=timezone.make_aware(datetime.combine(first, time(12))))
        self.people[who.email] = person
        return person

    def first_chair(self):
        """The first in the list, as `kit chair` records them, with a membership of the root
        from the one they have, or from the day they become chair. Returns the ties that
        recorded, so they aren't recorded again."""
        first = self.example.people[0]
        chair = next(tie for tie in first.positions if tie.unit == ROOT and tie.what == R.CHAIR)
        since = next((tie for tie in first.memberships if tie.unit == ROOT), None)
        record_root_chair(self.add_person(first), chair.start, since.start if since else None)
        return [chair, since]

    def add_units(self, specs, parent):
        for spec in specs:
            unit = actions.create_unit(
                self.actor(), parent=parent, kind=spec.kind, key=spec.key, name=spec.name, description=spec.description, end_date=spec.end_date, via=SERVER,
            )
            self.units[spec.key] = unit
            self.add_units(spec.children, unit)

    def add_ties(self, who, done):
        person = self.people.get(who.email) or self.add_person(who)
        for tie in who.memberships:
            if not any(tie is other for other in done):
                actions.add_membership(
                    self.actor(tie.by), person=person, association=self.where(tie.unit), kind=tie.what, start=tie.start, end=tie.end, via=SERVER,
                )
        for tie in who.positions:
            if not any(tie is other for other in done):
                actions.appoint(
                    self.actor(tie.by), person=person, unit=self.where(tie.unit), role=tie.what, start=tie.start, end=tie.end, title=tie.title, via=SERVER,
                )
        for tie in who.reasons:
            actions.add_reason(
                self.actor(tie.by), person=person, unit=self.where(tie.unit), kind=tie.what, legal_basis=tie.basis, source=tie.source,
                start=tie.start, expires=tie.end, via=SERVER,
            )

    def add_rules_by(self, address):
        """What this person gave and made, right after they're recorded, while it's theirs to give."""
        for given in self.example.grants:
            if given.by == address:
                actions.give_grant(
                    self.people[address], permission=given.permission, role=given.role, held_in=self.where(given.held_in), unit=self.where(given.unit),
                    include_below=given.include_below, start=given.start, via=SERVER,
                )
        for seats in self.example.seat_rules:
            if seats.by == address:
                actions.make_seat_rule(
                    self.people[address], unit=self.where(seats.unit), role=seats.role, from_roles=list(seats.from_roles), held_in=self.where(seats.held_in),
                    each_association_under=seats.each_association_under, start=seats.start, via=SERVER,
                )

    def build(self):
        recorded = self.first_chair()
        self.add_units(self.example.units, self.root)
        for spec in _all(self.example.units):
            unit = self.units[spec.key]
            if spec.run_for:
                actions.run_for(self.actor(), team=unit, association=self.units[spec.run_for], via=SERVER)
            for key in spec.taking_part:
                actions.add_taking_part(self.actor(), team=unit, association=self.units[key], via=SERVER)
        for number, who in enumerate(self.example.people):
            self.add_ties(who, recorded if number == 0 else [])
            self.add_rules_by(who.email)
        for spec in _all(self.example.units):  # last, as nothing new goes into an archived team
            if spec.archived:
                actions.archive_unit(self.actor(), unit=self.units[spec.key], via=SERVER)
        self.delete_departed()
        return self.units

    def delete_departed(self):
        """Made-up people with nothing left, as core would delete them that night, and nobody else."""
        delete_people(people=[person.pk for person in self.people.values()], via=SERVER)


@transaction.atomic
def build(name, root, replace=False):
    """Build an example's units and people under `root`, which keeps its own name and
    key. With `replace`, everything under the root goes first, which only an example
    should have, and so do the made-up people of both examples."""
    if root.descendants().exists() or Person.objects.filter(emails__address__in=example_addresses()).exists():
        if not replace:
            raise ValidationError(f"{root} already has units or made-up people. Use --replace to remove them first.")
        _remove(root)
    made_up = Person.objects.filter(emails__address__in=example_addresses())
    real = Position.objects.filter(unit=root, role=R.CHAIR).exclude(person__in=made_up).exclude(person__isnull=True, kept_name__in=example_names())
    if real.exists():
        raise ValidationError(
            f"{root} has a chair who isn't made up, {real.first().holder()}. A made-up installation records its own chairs, "
            "so it's only loaded where nobody real has ever held that seat. Nothing was changed."
        )
    return _Builder(EXAMPLES[name], root).build()
