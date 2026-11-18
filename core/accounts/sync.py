"""Tells other tools about changes to someone's access straight away, rather than at
their next sign-in, which for someone who has left may never come.

A change to someone's account or groups marks them as waiting (AccessSync), as part of
the same save. `manage.py sync_tools` runs in its own container and works through who's
waiting: it's the only process holding the tools' admin tokens, so core's web process,
which faces the internet, never has them, and a tool being down never holds up a page.

Each module that can be told about changes ships a `connector.py`, which core loads from
its folder, so a module that's off just isn't asked. A connector has no Django in it: it
has a `tool`, its name in the login service, and `apply(person, note)`, which makes the
tool match what's decided for that person and has to be safe to repeat. The person
carries their keys (structure.md §5) and the tree of units, for a tool that maps it. `note(key, text)`
records something only a person can do, and it should be called before the change that
makes it needed, so a crash partway can't lose it."""

import importlib.util
import logging
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from allauth.idp.oidc.models import Client
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext as _
from structure import models as structure
from structure.access import for_tools, keys
from structure.deleting import delete_people

from .login_service import may_use
from .models import AccessSync, FollowUp, User

logger = logging.getLogger(__name__)

STUCK_AFTER = timedelta(minutes=1)  # when the admin page starts saying so
LONGEST_WAIT = timedelta(minutes=5)
BATCH = 50


@dataclass(frozen=True)
class Unit:
    """A unit of the tree, as a connector sees it."""

    key: str
    name: str
    team: bool
    association: str  # its own key for an association, or the key of the one owning the team
    root: bool = False


@dataclass(frozen=True)
class Person:
    """What a connector gets told about someone."""

    sub: str
    email: str
    allowed: bool  # may they use this tool at all, right now?
    groups: tuple[str, ...] = ()  # their keys, as the login response sends them
    units: tuple[Unit, ...] = ()  # the whole tree, archived units too, for a tool that maps it


def tree():
    """Every unit, with the association each one is or belongs to: one query."""
    rows = list(structure.Unit.objects.values_list("key", "name", "kind", "path", "parent_id"))
    associations = {path: key for key, _name, kind, path, _parent in rows if kind == structure.Unit.Kind.ASSOCIATION}
    return tuple(
        Unit(key, name, kind == structure.Unit.Kind.TEAM, next(associations[above] for above in reversed(structure.path_and_above(path)) if above in associations), parent is None)
        for key, name, kind, path, parent in rows
    )


def sub_of(user):
    # What the login service told the tool. Importing it here, as it needs the apps loaded.
    from allauth.idp.oidc.adapter import get_adapter

    return get_adapter().get_user_sub(None, user)


def _ask(sub, user, label):
    AccessSync.objects.update_or_create(
        sub=sub,
        defaults={"user": user, "label": label, "requested_at": timezone.now(), "pending": True, "attempts": 0, "retry_at": None},
    )


def request_for(user, wait=None):
    """`wait` asks for it to be done no sooner than that, such as once a tool has made
    someone's account. If they're already waiting, they keep their place, and their
    attempts if a tool is down: it's only moved to the later of the two times."""
    sub = sub_of(user)
    if wait is None:
        return _ask(sub, user, user.email)
    now = timezone.now()
    row = AccessSync.objects.filter(sub=sub, pending=True).first()
    if row is None:
        AccessSync.objects.update_or_create(
            sub=sub, defaults={"user": user, "label": user.email, "requested_at": now, "pending": True, "attempts": 0, "retry_at": now + wait}
        )
    else:
        AccessSync.objects.filter(pk=row.pk).update(requested_at=now, retry_at=max(row.retry_at or now + wait, now + wait))


def request_for_deleted(user):
    """Its row has already lost the link, so the address has to come from what's left."""
    _ask(sub_of(user), None, user.email)


def request_for_everyone():
    """After anything that could change who may use a tool, and when the worker starts:
    it also catches a tool restored from a backup, and a change made some other way.
    That includes people whose accounts are gone, as a backup can bring theirs back."""
    now = timezone.now()
    rows = [AccessSync(sub=sub_of(user), user=user, label=user.email, requested_at=now) for user in User.objects.all()]
    AccessSync.objects.bulk_create(
        rows,
        update_conflicts=True,
        unique_fields=["sub"],
        update_fields=["user", "label", "requested_at", "pending", "attempts", "retry_at"],
    )
    AccessSync.objects.filter(user__isnull=True).update(requested_at=now, pending=True, attempts=0, retry_at=None)


def request_for_new_days(since, today):
    """Ties that start or end on a day with no save, such as a board taking office on
    1 July, change keys all the same. So the worker asks about everyone whose ties started
    or ended after `since`, up to and including `today`, once the day has changed."""
    days = Q(start__gt=since, start__lte=today) | Q(end__gt=since, end__lte=today)
    ties = Q(person__memberships__in=structure.Membership.objects.filter(days)) | Q(person__positions__in=structure.Position.objects.filter(days))
    for user in User.objects.filter(ties).distinct():
        request_for(user)


def new_day(day):
    """Returns today, having asked about the days since `day` if it has changed, and
    deleted the people with nothing left (structure.md §3). That's never done as the worker
    starts, so an update never deletes anyone by itself. If it fails, it's logged and tried
    again the next night, and the worker goes on telling the tools."""
    today = timezone.localdate()
    if today > day:
        request_for_new_days(day, today)
        try:
            delete_people(today)
        except Exception:
            logger.exception("Couldn't delete the people with nothing left today. Trying again tomorrow.")
    return today


class Unavailable:
    """A module that's on, whose connector can't run. Skipping it would look like nothing
    had to be done, so people stay waiting and the admin page says so."""

    def __init__(self, tool, reason):
        self.tool = tool
        self.reason = reason


def load_connectors(environ=os.environ):
    """A module isn't ready until `kit` has given the worker its token. One that's broken
    mustn't stop the worker telling the others."""
    folder = Path(environ.get("SYNC_MODULES_DIR", "/modules"))
    connectors = []
    for name in filter(None, environ.get("SYNC_MODULES", "").split(",")):
        try:
            spec = importlib.util.spec_from_file_location(f"connector_{name}", folder / name / "connector.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            connectors.append(module.Connector(environ))
        except KeyError as missing:
            connectors.append(Unavailable(module.Connector.tool, f"it has no {missing} yet"))
        except Exception as error:
            connectors.append(Unavailable(name, f"its connector didn't load: {error}"))
        if isinstance(connectors[-1], Unavailable):
            logger.warning("Can't tell %s about changes: %s", connectors[-1].tool, connectors[-1].reason)
    return connectors


def check(connectors):
    """For `kit update`, after a new version starts: whether each tool still answers the way
    its connector expects. A connector says how with `check()`, which raises if not. One
    without it only has to have loaded."""
    problems = []
    for connector in connectors:
        if isinstance(connector, Unavailable):
            problems.append(f"{connector.tool}: {connector.reason}")
            continue
        try:
            if hasattr(connector, "check"):
                connector.check()
        except Exception as error:
            problems.append(f"{connector.tool}: {error}")
    return problems


def label(row):
    return row.label or _("A deleted account (%(sub)s)") % {"sub": row.sub}


def _person(row, tool, units=()):
    user = row.user
    held = keys(user) if user else []
    return Person(
        sub=row.sub,
        email=label(row),
        allowed=bool(user and user.is_active and may_use(user, tool, held)),
        groups=tuple(for_tools(user, held)) if user else (),  # the same keys as the login response
        units=units,
    )


def _note(row, tool_name):
    def note(key, text):
        FollowUp.objects.get_or_create(
            sub=row.sub,
            tool=tool_name,
            key=key,
            done_at__isnull=True,
            defaults={"about": label(row), "text": text},
        )

    return note


def _wait(attempts):
    return min(timedelta(seconds=5 * 2 ** min(attempts, 8)), LONGEST_WAIT)  # a tool can be down for days


def process(row, connectors, down=None, units=None):
    """Each tool is told once for each change, and one being down doesn't hold up the
    others. A change arriving partway only moves requested_at, so it's picked up next time.
    `down` holds tools that aren't answering, so a hung one costs one timeout and not one
    for everyone who's waiting."""
    down = {} if down is None else down
    units = tree() if units is None else units
    version = row.requested_at.isoformat()
    tools = dict(row.tools)
    failed = False
    asked = set()
    for connector in connectors:
        now = timezone.now().isoformat()
        asked.add(connector.tool)
        if tools.get(connector.tool, {}).get("version") == version:
            continue
        reason = connector.reason if isinstance(connector, Unavailable) else down.get(connector.tool)
        if reason:
            failed = True
            tools[connector.tool] = {"error": reason, "at": now}
            continue
        tool = Client.objects.filter(name=connector.tool).first()
        if tool is None:
            asked.discard(connector.tool)
            continue  # not registered, so nobody can use it. Registering it asks for everyone again
        try:
            connector.apply(_person(row, tool, units), _note(row, connector.tool))
        except Exception as error:  # whatever one tool does, the others still get told
            failed = True
            tools[connector.tool] = {"error": str(error)[:300], "at": now}
            logger.warning("Couldn't tell %s about %s: %s", connector.tool, row.label, error)
            if isinstance(error, OSError):  # it isn't answering, as opposed to refusing this one person
                down[connector.tool] = tools[connector.tool]["error"]
        else:
            tools[connector.tool] = {"version": version, "at": now}
    tools = {name: state for name, state in tools.items() if name in asked}  # or a tool that's gone shows an old error forever

    current = AccessSync.objects.filter(pk=row.pk, requested_at=row.requested_at)
    if failed:
        retry_at = timezone.now() + _wait(row.attempts)
        finished = current.update(tools=tools, attempts=row.attempts + 1, retry_at=retry_at)
    else:
        finished = current.update(tools=tools, pending=False, attempts=0, retry_at=None)
    if not finished:
        AccessSync.objects.filter(pk=row.pk).update(tools=tools)
    elif not failed and row.user_id is None:
        # Their account is gone, so their address goes too. The row stays, with only the
        # id tools know them by, so that each look at everyone covers them as well.
        current.update(label="")


def run_once(connectors, tick=lambda: None):
    """The newest first, each time, so someone just removed doesn't wait behind a look at
    everyone, or behind others being retried, even one already under way. Each is told
    once per run, and with the tree as it is then. `tick` says the worker is still alive,
    each time."""
    down, done = {}, []
    while len(done) < BATCH:
        due = AccessSync.objects.filter(Q(retry_at__isnull=True) | Q(retry_at__lte=timezone.now()), pending=True).exclude(pk__in=done)
        row = due.select_related("user").order_by("-requested_at").first()
        if row is None:
            break
        done.append(row.pk)
        process(row, connectors, down)
        tick()
    return len(done)


def stuck():
    """Changes that should have reached the tools by now."""
    return AccessSync.objects.filter(pending=True, requested_at__lt=timezone.now() - STUCK_AFTER)


def describe(row):
    """In words, for the admin page."""
    errors = [f"{tool}: {state['error']}" for tool, state in sorted(row.tools.items()) if state.get("error")]
    if errors:
        return "; ".join(errors)
    if row.pending:
        return _("Waiting to be sent")
    return _("Sent to %(tools)s") % {"tools": ", ".join(sorted(row.tools))} if row.tools else _("Nothing to send")
