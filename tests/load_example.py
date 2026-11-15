#!/usr/bin/env python3
"""Loads one of the two made-up installations onto a test server, under its own
association, which keeps its name and key, with made-up people and accounts. Run on
the server as root. Never on an installation with real members: with --replace, it
deletes everything under the root, and every tie to it.

    python3 tests/load_example.py society test.example.org
    python3 tests/load_example.py federation test.example.org --replace

The domain has to be the server's own primary domain, so it's never run on the wrong
one by mistake. The definitions are in core/structure/examples.py. They're the same
ones core's tests use, and they grow with each segment.

Standard library only, as the rest of the kit's tooling is.
"""

import argparse
import json
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = Path("/etc/sallskap/config.toml")
EXAMPLES = ("society", "federation")

LOAD = """
import json, sys
from django.core.exceptions import ValidationError
from structure.examples import EXAMPLES, build
from structure.models import Unit
try:
    units = build({name}, Unit.objects.root(), replace={replace})
except (ValidationError, Unit.DoesNotExist) as error:
    print(" ".join(getattr(error, "messages", [str(error)])), file=sys.stderr)
    sys.exit(1)
print(json.dumps({{"units": len(units), "people": len(EXAMPLES[{name}].people), "grants": len(EXAMPLES[{name}].grants)}}))
"""


def check_domain(domain, config_text):
    """The config's primary domain, if it's the one given. Otherwise it says why not."""
    primary = tomllib.loads(config_text).get("association", {}).get("domain")
    if primary != domain:
        sys.exit(f"This server's primary domain is {primary}, not {domain}. Nothing was changed.")
    return primary


def main(argv=None):
    parser = argparse.ArgumentParser(description="Load a made-up installation onto a test server.")
    parser.add_argument("example", choices=EXAMPLES)
    parser.add_argument("domain", help="this server's primary domain, to confirm it's the right one")
    parser.add_argument("--replace", action="store_true", help="first delete every unit under the association, and the made-up people")
    args = parser.parse_args(argv)

    check_domain(args.domain, CONFIG.read_text("utf-8"))
    code = LOAD.format(name=json.dumps(args.example), replace=args.replace)
    result = subprocess.run(
        ["docker", "compose", "--project-directory", str(ROOT), "exec", "-T", "core", "python", "manage.py", "shell", "-v", "0", "-c", code],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        sys.exit(f"Nothing was loaded: {result.stderr.strip() or result.stdout.strip()}")
    loaded = json.loads(result.stdout.strip().splitlines()[-1])
    parts = [f"{loaded['units']} units under the association", f"{loaded['people']} people"]
    pages = ["Units", "People"]
    if loaded["grants"]:  # none in Example Society, where grants stay out of sight
        parts.append(f"{loaded['grants']} grants")
        pages.append("Grants")
    print(f"Loaded Example {args.example.title()}: {', '.join(parts[:-1])} and {parts[-1]}. See them in the admin page, under {', '.join(pages[:-1])} and {pages[-1]}.")


if __name__ == "__main__":
    main()
