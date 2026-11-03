FROM python:3.14-slim-trixie

ENV PYTHONUNBUFFERED=1

# kit gives /srv/core to this uid: keep CORE_UID in kit/kit.py the same.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin sallskap

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes --root-user-action=ignore -r requirements.txt
COPY . .
RUN SALLSKAP_DOMAIN=build.invalid DJANGO_SECRET_KEY=build-only python manage.py collectstatic --noinput

USER 10001:10001
CMD ["./start.sh"]
