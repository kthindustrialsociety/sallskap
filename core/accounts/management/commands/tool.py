from urllib.parse import urlparse

from allauth.idp.oidc.adapter import get_adapter
from allauth.idp.oidc.models import Client
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import URLValidator

from accounts.login_service import SCOPES


class Command(BaseCommand):
    help = "Register the tools that can use the login service."

    def add_arguments(self, parser):
        actions = parser.add_subparsers(dest="action", required=True)
        add = actions.add_parser("add")
        add.add_argument("name")
        add.add_argument("callback", help="the exact address the tool wants people sent back to")
        actions.add_parser("list")
        actions.add_parser("remove").add_argument("name")

    def handle(self, *args, action, **options):
        if action == "add":
            self.add(options["name"], options["callback"])
        elif action == "list":
            self.list()
        else:
            self.remove(options["name"])

    def add(self, name, callback):
        if Client.objects.filter(name=name).exists():
            raise CommandError(f"{name} is already registered. To get a new secret, remove it and add it again.")
        self.check_callback(callback)

        secret = get_adapter().generate_client_secret()
        tool = Client(name=name, type=Client.Type.CONFIDENTIAL, skip_consent=True)  # our own tools: no "allow?" page
        tool.set_redirect_uris([callback])
        tool.set_scopes(SCOPES)
        tool.set_default_scopes(SCOPES)
        tool.set_grant_types([Client.GrantType.AUTHORIZATION_CODE])  # no refresh tokens, and no other way in
        tool.set_response_types([Client.ResponseType.CODE])
        tool.set_secret(secret)
        tool.save()

        self.stdout.write(f"Registered {name}. Give the tool these settings. The secret is shown once and isn't kept here.")
        self.stdout.write(f"  Issuer:    https://{settings.APP_HOST}")
        self.stdout.write(f"  Client ID: {tool.id}")
        self.stdout.write(f"  Secret:    {secret}")
        self.stdout.write(f"  Callback:  {callback}")

    def check_callback(self, callback):
        """Registered addresses are matched exactly, so a fragment or wildcard here
        could only be a mistake, and plain HTTP would expose the login code."""
        problem = CommandError(f"{callback!r} must be a full https address, with no wildcard or #.")
        try:
            URLValidator(schemes=["https"])(callback)
        except ValidationError as error:
            raise problem from error
        if "*" in callback or urlparse(callback).fragment or "#" in callback:
            raise problem

    def list(self):
        tools = Client.objects.order_by("name")
        if not tools:
            self.stdout.write("No tools are registered.")
        for tool in tools:
            self.stdout.write(f"{tool.name}  {' '.join(tool.get_redirect_uris())}")

    def remove(self, name):
        deleted, _ = Client.objects.filter(name=name).delete()
        if not deleted:
            raise CommandError(f"No tool called {name}.")
        self.stdout.write(f"Removed {name}. Its tokens no longer work, and nobody can log in to it through here.")
