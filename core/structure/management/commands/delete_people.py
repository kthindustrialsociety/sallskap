from django.core.management.base import BaseCommand

from structure.deleting import delete_people, due
from structure.models import AuditEntry


class Command(BaseCommand):
    help = "Delete the people with nothing left (structure.md §3), as core does just after midnight. With --dry-run, only say who."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="say who would be deleted today, and change nothing")

    def handle(self, *args, dry_run=False, **options):
        if dry_run:
            found = list(due())
            for person in found:
                self.stdout.write(f"{person.pk}\t{person}")
            self.stdout.write(f"{len(found)} with nothing left for 30 days would be deleted today. Nothing was changed.")
            return
        counts = delete_people(via=AuditEntry.Via.SERVER)
        self.stdout.write(
            f"Deleted {counts['people']} with nothing left, with {counts['addresses']} addresses, {counts['memberships']} memberships "
            f"and {counts['reasons']} reasons. Kept {counts['positions_kept']} of their positions, under their names."
        )
        if counts["failed"]:
            self.stderr.write(f"{counts['failed']} couldn't be deleted: see the messages above. They're tried again tonight.")
