"""Settings for Sällskap core. Everything specific to one installation comes
from the environment, which `kit` writes to /etc/sallskap/core.env."""

import base64
import os
from pathlib import Path

from django.utils.translation import gettext_lazy as _

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("SALLSKAP_DATA_DIR", "/srv/core"))

APP_HOST = f"app.{os.environ['SALLSKAP_DOMAIN']}"
SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
DEBUG = False
ALLOWED_HOSTS = [APP_HOST]
CSRF_TRUSTED_ORIGINS = [f"https://{APP_HOST}"]

ROOT_URLCONF = "sallskap.urls"
WSGI_APPLICATION = "sallskap.wsgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"

INSTALLED_APPS = [
    "sallskap.apps.SallskapAdminConfig",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "allauth",
    "allauth.account",
    "allauth.mfa",
    "allauth.idp.oidc",
    "accounts",  # last: it assigns permissions, so every other app's must exist by then
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# ---- Data ---------------------------------------------------------------------

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATA_DIR / "db.sqlite3",
        "OPTIONS": {
            "transaction_mode": "IMMEDIATE",
            "timeout": 20,
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
        },
    }
}

# The login limits and TOTP replay protection are stored here, so they need a
# cache that's shared and survives a restart. Culling at a low MAX_ENTRIES would
# let someone erase the counters by spraying attempts.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "django_cache",
        "OPTIONS": {"MAX_ENTRIES": 100_000},
    }
}

# ---- Behind Caddy -------------------------------------------------------------

# Caddy is the only way in, and it sets these headers itself. Each one that
# arrives from a visitor is replaced before it gets here.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
ALLAUTH_TRUSTED_PROXY_COUNT = 1

SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31_536_000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SILENCED_SYSTEM_CHECKS = ["security.W021"]  # HSTS preload: a decision for the whole domain

# The __Host- prefix makes browsers refuse these cookies unless they're Secure,
# have no Domain and use path /, so they can never reach another hostname.
SESSION_COOKIE_NAME = "__Host-sessionid"
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_AGE = 12 * 60 * 60
CSRF_COOKIE_NAME = "__Host-csrftoken"
CSRF_COOKIE_SECURE = True

# ---- Accounts -----------------------------------------------------------------

PASSWORD_HASHERS = ["django.contrib.auth.hashers.Argon2PasswordHasher"]
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
        "OPTIONS": {"user_attributes": ("email", "name")},
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
PASSWORD_RESET_TIMEOUT = 24 * 60 * 60  # how long a `kit link` works

AUTHENTICATION_BACKENDS = ["allauth.account.auth_backends.AuthenticationBackend"]
LOGIN_URL = "account_login"
LOGIN_REDIRECT_URL = "home"
ACCOUNT_LOGOUT_REDIRECT_URL = "account_login"

ACCOUNT_ADAPTER = "accounts.adapters.AccountAdapter"
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_EMAIL_VERIFICATION = "none"
ACCOUNT_SESSION_REMEMBER = False
# Also counts wrong 2FA codes. For passwords, "key" means the account and address
# together: see AccountAdapter. `kit admin` clears these limits.
ACCOUNT_RATE_LIMITS = {"login_failed": "10/m/ip,5/5m/key"}

MFA_SUPPORTED_TYPES = ["totp", "recovery_codes"]
MFA_TOTP_ISSUER = APP_HOST
MFA_TOTP_TOLERANCE = 1  # accepts the neighbouring 30 seconds, for phones with a slightly wrong clock

# The login service for other tools. Its key signs what it tells them, so it has
# to stay the same: `kit` makes it once, like the secret key.
IDP_OIDC_ADAPTER = "accounts.login_service.LoginServiceAdapter"
IDP_OIDC_PRIVATE_KEY = base64.b64decode(os.environ.get("OIDC_SIGNING_KEY", "")).decode()
IDP_OIDC_AUTH_METHODS = ["client_secret_basic", "client_secret_post"]  # every tool has a secret
IDP_OIDC_ACCESS_TOKEN_EXPIRES_IN = 10 * 60  # tools only need it just after someone logs in
IDP_OIDC_REFRESH_TOKEN_EXPIRES_IN = 10 * 60  # always issued, never usable: tools aren't allowed that flow
IDP_OIDC_RATE_LIMITS = {"token": "60/m/ip"}  # each attempt costs a 100 MB password check

# Nothing sends mail yet. Until it does, people get in with `kit link`.
EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"

# ---- Language, static files and logs ------------------------------------------

# To add Swedish: add locale/sv/LC_MESSAGES/django.po and LocaleMiddleware.
LANGUAGE_CODE = "en"
LANGUAGES = [("en", _("English")), ("sv", _("Swedish"))]
LOCALE_PATHS = [BASE_DIR / "locale"]
USE_I18N = True
USE_TZ = True
TIME_ZONE = "Europe/Stockholm"

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# With DEBUG off, Django otherwise only emails errors, and there's no email.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler", "level": "WARNING"}},
    "root": {"handlers": ["console"]},
}
