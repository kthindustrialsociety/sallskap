#!/bin/sh
# Safe to run on every start: migrations and the cache table only change what's missing.
set -e
python manage.py migrate --noinput
python manage.py createcachetable
python manage.py clearsessions  # Django keeps expired sessions until told otherwise

# One process, so memory stays predictable: each thread can hold one Argon2
# password check of about 100 MB, and the container's limit has to cover them all.
exec gunicorn sallskap.wsgi:application --bind 0.0.0.0:8000 --workers 1 --threads 4 --timeout 30
