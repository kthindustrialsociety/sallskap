from django.contrib.auth import get_user_model
from allauth.mfa.models import Authenticator
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import transaction
from structure.models import AuditEntry

from accounts.links import password_link
from accounts.models import normalize_email
from accounts.roles import NAME


class Command(BaseCommand):
    help = "Create the first admin, or restore someone's Operator access and clear any lockouts. Safe to repeat."

    def add_arguments(self, parser):
        parser.add_argument("email")
        parser.add_argument("--reset-2fa", action="store_true", help="Also remove their 2FA and recovery codes.")

    def handle(self, *args, email, reset_2fa=False, **options):
        email = normalize_email(email)
        try:
            validate_email(email)
        except ValidationError as error:
            raise CommandError(f"{email!r} isn't a valid email address.") from error

        User = get_user_model()
        with transaction.atomic():
            found = User.objects.filter(email=email).first()
            user = found or User.objects.create_user(email)
            # The audit log's row: what changed, the address recorded as changed, not shown.
            changes = {} if found else {"email": None}
            if not (found and found.is_operator):
                changes["operator"] = [False, True]
            user.groups.add(Group.objects.get(name=NAME))
            if not user.is_active:
                changes["is_active"] = [False, True]
                user.is_active = True
                user.save(update_fields=["is_active"])
            if reset_2fa and Authenticator.objects.filter(user=user).exists():
                changes["two_factor"] = [True, False]
                Authenticator.objects.filter(user=user).delete()
            AuditEntry.record("make_operator", AuditEntry.Via.SERVER, obj=user, changes=changes)
        cache.clear()  # the login limits: this is the way back in for someone locked out

        self.stdout.write(f"{email} is an Operator, and login lockouts are cleared.")
        if reset_2fa:
            self.stdout.write("Their 2FA and recovery codes are removed. They'll set up 2FA again before using the admin page.")
        if not user.has_usable_password():
            self.stdout.write(f"This link lets them choose a password, once, within 24 hours:\n{password_link(user)}")
            self.stdout.write("Treat it like a password: whoever opens it first chooses the password. Send it to them privately, and don't paste it anywhere else.")
