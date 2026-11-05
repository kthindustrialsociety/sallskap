from urllib.parse import urlparse

from allauth.idp.oidc.adapter import get_adapter
from allauth.idp.oidc.models import Client
from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import URLValidator
from django.db import transaction

from accounts.hashers import ToolSecretHasher
from accounts.login_service import SCOPES
from accounts.models import GroupRole, ToolAccess


class Command(BaseCommand):
    help = "Register the tools that can use the login service."

    def add_arguments(self, parser):
        actions = parser.add_subparsers(dest="action", required=True)
        add = actions.add_parser("add")
        add.add_argument("name")
        add.add_argument("callback", help="the exact address the tool wants people sent back to")
        add.add_argument("--group", action="append", default=[], dest="groups", help="only this group may log in (repeatable)")
        add.add_argument("--session-hours", type=int, help="for a tool whose sessions last as long as its access token (1-24)")
        actions.add_parser("list")
        actions.add_parser("remove").add_argument("name")

    def handle(self, *args, action, **options):
        if action == "add":
            self.add(options["name"], options["callback"], options["groups"], options["session_hours"])
        elif action == "list":
            self.list()
        else:
            self.remove(options["name"])

    def add(self, name, callback, groups=(), session_hours=None):
        if Client.objects.filter(name=name).exists():
            raise CommandError(f"{name} is already registered. To get a new secret, remove it and add it again.")
        self.check_callback(callback)
        self.check_groups(groups)
        if session_hours is not None:
            if not groups:
                raise CommandError("A longer session is only for a tool limited to certain groups.")
            if not 1 <= session_hours <= 24:
                raise CommandError("A session can last from 1 to 24 hours.")

        secret = get_adapter().generate_client_secret()
        tool = Client(name=name, type=Client.Type.CONFIDENTIAL, skip_consent=True)  # our own tools: no "allow?" page
        tool.set_redirect_uris([callback])
        tool.set_scopes(SCOPES)
        tool.set_default_scopes(SCOPES)
        tool.set_grant_types([Client.GrantType.AUTHORIZATION_CODE])  # no refresh tokens, and no other way in
        tool.set_response_types([Client.ResponseType.CODE])
        tool.secret = make_password(secret, hasher=ToolSecretHasher.algorithm)
        with transaction.atomic():  # so it's never open to everyone, even briefly
            tool.save()
            if groups:
                ToolAccess.objects.create(tool=tool, group_keys=list(groups), session_hours=session_hours)

        self.stdout.write(f"Registered {name}. Give the tool these settings. The secret is shown once and isn't kept here.")
        self.stdout.write(f"  Issuer:    https://{settings.APP_HOST}")
        self.stdout.write(f"  Client ID: {tool.id}")
        self.stdout.write(f"  Secret:    {secret}")
        self.stdout.write(f"  Callback:  {callback}")

    def check_callback(self, callback):
        """Registered addresses are matched exactly, so a fragment or wildcard here
        could only be a mistake, plain HTTP would expose the login code, and a
        password in the address would end up stored in the database."""
        problem = CommandError(f"{callback!r} must be a full https address, with no wildcard, # or password.")
        try:
            URLValidator(schemes=["https"])(callback)
        except ValidationError as error:
            raise problem from error
        if "*" in callback or "#" in callback or urlparse(callback).username:
            raise problem

    def check_groups(self, groups):
        """A misspelt group would quietly keep everyone out."""
        known = set(GroupRole.objects.values_list("key", flat=True))
        known |= {f"custom:{name}" for name in Group.objects.filter(role__isnull=True).values_list("name", flat=True)}
        unknown = sorted(set(groups) - known)
        if unknown:
            raise CommandError(f"No such group: {', '.join(unknown)}. These exist: {', '.join(sorted(known))}.")

    def list(self):
        tools = Client.objects.select_related("access").order_by("name")
        if not tools:
            self.stdout.write("No tools are registered.")
        for tool in tools:
            who = ", ".join(tool.access.group_keys) if hasattr(tool, "access") else "any account"
            hours = f", {tool.access.session_hours} hour sessions" if hasattr(tool, "access") and tool.access.session_hours else ""
            self.stdout.write(f"{tool.name}  {' '.join(tool.get_redirect_uris())}  ({who}{hours})")

    def remove(self, name):
        deleted, _ = Client.objects.filter(name=name).delete()
        if not deleted:
            raise CommandError(f"No tool called {name}.")
        self.stdout.write(f"Removed {name}. Its tokens no longer work, and nobody can log in to it through here.")
