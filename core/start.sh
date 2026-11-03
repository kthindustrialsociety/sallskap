#!/bin/sh
# Safe to run on every start: migrations and the cache table only change what's missing.
set -e

# Without this, core would start and look fine, and the first login to a tool would fail.
: "${OIDC_SIGNING_KEY:?The signing key is missing. Run kit apply on the server to create it.}"

python manage.py migrate --noinput
python manage.py createcachetable
python manage.py clearsessions  # Django keeps expired sessions until told otherwise
python manage.py oidc_cleartokens

# One process, so memory stays predictable: each thread can hold one Argon2
# password check of about 100 MB, and the container's limit has to cover them all.
exec gunicorn sallskap.wsgi:application --bind 0.0.0.0:8000 --workers 1 --threads 4 --timeout 30
