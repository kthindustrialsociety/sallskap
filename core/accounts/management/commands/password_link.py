from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from accounts.links import password_link
from accounts.models import normalize_email


class Command(BaseCommand):
    help = "Print a one-time link that lets someone choose a password."

    def add_arguments(self, parser):
        parser.add_argument("email")

    def handle(self, *args, email, **options):
        email = normalize_email(email)
        user = get_user_model().objects.filter(email=email).first()
        if user is None:
            raise CommandError(f"No account for {email}. An Operator can add it on the admin page.")
        if not user.is_active:
            raise CommandError(f"The account for {email} is turned off.")
        self.stdout.write(f"This link lets {email} choose a password, once, within 24 hours:\n{password_link(user)}")
        self.stdout.write("If they use 2FA, they'll still need it to log in.")
