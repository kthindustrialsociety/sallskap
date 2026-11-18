from datetime import date

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from structure.models import EmailAddress, normalize_address
from structure.server import record_root_chair


def day(text):
    return date.fromisoformat(text)


class Command(BaseCommand):
    help = (
        "Record the association's chair, ending the current one's position that day. For a new "
        "installation, this is how the first chair is recorded. The audit log records it."
    )

    def add_arguments(self, parser):
        parser.add_argument("email", help="one of their verified addresses, such as their account's")
        parser.add_argument("--from", dest="start", type=day, help="their first day as chair, such as 2027-07-01. Today if not given")
        parser.add_argument("--member-since", type=day, help="if they aren't a member yet: when their membership began. Their first day as chair if not given")

    def handle(self, *args, email, start=None, member_since=None, **options):
        address = normalize_address(email)
        found = EmailAddress.objects.filter(address=address, verified_at__isnull=False).select_related("person").first()
        if found is None:
            raise CommandError(f"Nobody has {address} as a verified address. Add their account on the admin page first.")
        try:
            position, membership = record_root_chair(found.person, start, member_since)
        except ValidationError as error:
            raise CommandError(" ".join(error.messages)) from None
        person, root = found.person, position.unit
        if membership is not None:
            self.stdout.write(f"{person} is a member of {root} from {membership.start}, as a chair has to be.")
        self.stdout.write(f"{person} is the chair of {root} from {position.start}.")
        if person.user_id:
            self.stdout.write("The role needs two-factor authentication, which they'll be asked to set up when they next log in.")
        else:
            self.stdout.write(f"They have no account yet. Add one for {address} on the admin page, so they can log in.")
