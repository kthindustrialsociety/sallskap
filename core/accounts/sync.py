"""Tells other tools about changes to someone's access straight away, rather than at
their next sign-in, which for someone who has left may never come.

A change to someone's account or groups marks them as waiting (AccessSync), as part of
the same save. `manage.py sync_tools` runs in its own container and works through who's
waiting: it's the only process holding the tools' admin tokens, so core's web process,
which faces the internet, never has them, and a tool being down never holds up a page.

Each module that can be told about changes ships a `connector.py`, which core loads from
its folder, so a module that's off just isn't asked. A connector has no Django in it: it
has a `tool`, its name in the login service, and `apply(person, note)`, which makes the
tool match what's decided for that person and has to be safe to repeat. `note(key, text)`
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

from .login_service import may_use
from .models import AccessSync, FollowUp, User

logger = logging.getLogger(__name__)

STUCK_AFTER = timedelta(minutes=1)  # when the admin page starts saying so
LONGEST_WAIT = timedelta(minutes=5)
BATCH = 50


@dataclass(frozen=True)
class Person:
    """What a connector gets told about someone."""

    sub: str
    email: str
    allowed: bool  # may they use this tool at all, right now?
    groups: tuple[str, ...] = ()


def sub_of(user):
    # What the login service told the tool. Importing it here, as it needs the apps loaded.
    from allauth.idp.oidc.adapter import get_adapter

    return get_adapter().get_user_sub(None, user)


def _ask(sub, user, label):
    AccessSync.objects.update_or_create(
        sub=sub,
        defaults={"user": user, "label": label, "requested_at": timezone.now(), "pending": True, "attempts": 0, "retry_at": None},
    )


def request_for(user):
    _ask(sub_of(user), user, user.email)


def request_for_deleted(user):
    """Its row has already lost the link, so the address has to come from what's left."""
    _ask(sub_of(user), None, user.email)


def request_for_everyone():
    """After anything that could change who may use a tool, and when the worker starts:
    it also catches a tool restored from a backup, and a change made some other way."""
    now = timezone.now()
    rows = [AccessSync(sub=sub_of(user), user=user, label=user.email, requested_at=now) for user in User.objects.all()]
    AccessSync.objects.bulk_create(
        rows,
        update_conflicts=True,
        unique_fields=["sub"],
        update_fields=["user", "label", "requested_at", "pending", "attempts", "retry_at"],
    )


def load_connectors(environ=os.environ):
    """A module isn't ready until `kit` has given the worker its token."""
    folder = Path(environ.get("SYNC_MODULES_DIR", "/modules"))
    connectors = []
    for name in filter(None, environ.get("SYNC_MODULES", "").split(",")):
        spec = importlib.util.spec_from_file_location(f"connector_{name}", folder / name / "connector.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        try:
            connectors.append(module.Connector(environ))
        except KeyError as missing:
            logger.warning("Not telling %s about changes: it has no %s yet.", name, missing)
    return connectors


def _person(row, tool):
    user = row.user
    return Person(
        sub=row.sub,
        email=row.label,
        allowed=bool(user and user.is_active and may_use(user, tool)),
        groups=tuple(user.group_keys()) if user else (),
    )


def _note(row, tool_name):
    def note(key, text):
        FollowUp.objects.get_or_create(
            sub=row.sub,
            tool=tool_name,
            key=key,
            done_at__isnull=True,
            defaults={"about": row.label, "text": text},
        )

    return note


def _wait(attempts):
    return min(timedelta(seconds=5 * 2 ** min(attempts, 8)), LONGEST_WAIT)  # a tool can be down for days


def process(row, connectors):
    """Each tool is told once for each change, and one being down doesn't hold up the
    others. A change arriving partway only moves requested_at, so it's picked up next time."""
    version = row.requested_at.isoformat()
    tools = dict(row.tools)
    failed = False
    for connector in connectors:
        if tools.get(connector.tool, {}).get("version") == version:
            continue
        tool = Client.objects.filter(name=connector.tool).first()
        if tool is None:
            continue  # not registered, so nobody can use it. Registering it asks for everyone again
        now = timezone.now().isoformat()
        try:
            connector.apply(_person(row, tool), _note(row, connector.tool))
        except Exception as error:  # whatever one tool does, the others still get told
            failed = True
            tools[connector.tool] = {"error": str(error)[:300], "at": now}
            logger.warning("Couldn't tell %s about %s: %s", connector.tool, row.label, error)
        else:
            tools[connector.tool] = {"version": version, "at": now}

    current = AccessSync.objects.filter(pk=row.pk, requested_at=row.requested_at)
    if failed:
        retry_at = timezone.now() + _wait(row.attempts)
        finished = current.update(tools=tools, attempts=row.attempts + 1, retry_at=retry_at)
    else:
        finished = current.update(tools=tools, pending=False, attempts=0, retry_at=None)
    if not finished:
        AccessSync.objects.filter(pk=row.pk).update(tools=tools)
    elif not failed and row.user_id is None:
        current.delete()  # their account is gone, and every tool knows


def run_once(connectors):
    due = AccessSync.objects.filter(Q(retry_at__isnull=True) | Q(retry_at__lte=timezone.now()), pending=True)
    rows = list(due.select_related("user").order_by("requested_at")[:BATCH])
    for row in rows:
        process(row, connectors)
    return len(rows)


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
