from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email

from accounts.links import password_link
from accounts.models import normalize_email


class Command(BaseCommand):
    help = "Create the first admin, or give an existing account Operator access again. Safe to repeat."

    def add_arguments(self, parser):
        parser.add_argument("email")

    def handle(self, *args, email, **options):
        email = normalize_email(email)
        try:
            validate_email(email)
        except ValidationError as error:
            raise CommandError(f"{email!r} isn't a valid email address.") from error

        User = get_user_model()
        user = User.objects.filter(email=email).first() or User.objects.create_user(email)
        user.groups.add(Group.objects.get(role__key="operator"))
        if not user.is_active:
            user.is_active = True
            user.save(update_fields=["is_active"])

        self.stdout.write(f"{email} is an Operator.")
        if not user.has_usable_password():
            self.stdout.write(f"This link lets them choose a password, once, within 24 hours:\n{password_link(user)}")
