import signal
import sys
import time
from datetime import timedelta
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from accounts import sync

POLL_SECONDS = 2
EVERYONE_EVERY = timedelta(days=1)
ALIVE = Path("/tmp/alive")  # what the container's health check looks at


class Command(BaseCommand):
    help = "Tell the tools about changes to people's access. Runs in its own container."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="work through who's waiting, then stop")

    def handle(self, *args, once=False, **options):
        # Python ignores SIGTERM as a container's first process. Stopping partway loses
        # nothing, as someone only stops waiting once they've been dealt with.
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        connectors = sync.load_connectors()
        self.stdout.write(f"Telling these tools about changes: {', '.join(c.tool for c in connectors) or 'none yet'}")
        sync.request_for_everyone()
        next_pass = timezone.now() + EVERYONE_EVERY
        while True:
            close_old_connections()
            worked = sync.run_once(connectors)
            if once and not worked:
                return
            ALIVE.touch()
            if timezone.now() >= next_pass:
                sync.request_for_everyone()
                next_pass = timezone.now() + EVERYONE_EVERY
            if not worked:
                time.sleep(POLL_SECONDS)
