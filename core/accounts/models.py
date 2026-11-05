from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import Group, PermissionsMixin
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .roles import ADMIN_ROLES, TWO_FACTOR_ROLES


def normalize_email(email):
    return email.strip().lower()


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email, password=None, **extra_fields):
        """With no password, the account can't be logged in to until a link is used."""
        if not email:
            raise ValueError("An email address is required.")
        user = self.model(email=normalize_email(email), **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        return self.create_user(email, password, is_superuser=True, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(_("email address"), unique=True)
    name = models.CharField(_("name"), max_length=150, blank=True)
    is_active = models.BooleanField(
        _("active"),
        default=True,
        help_text=_("Turn this off instead of deleting an account. It ends their sessions and blocks login."),
    )
    date_joined = models.DateTimeField(_("date joined"), default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")
        constraints = [
            # With this, the unique email column also means unique ignoring case.
            models.CheckConstraint(condition=models.Q(email=Lower("email")), name="email_is_lowercase"),
        ]

    def __str__(self):
        return self.email

    def clean(self):
        super().clean()
        self.email = normalize_email(self.email)

    def save(self, *args, **kwargs):
        self.email = normalize_email(self.email)
        super().save(*args, **kwargs)

    def get_full_name(self):
        return self.name or self.email

    def get_short_name(self):
        return self.name or self.email

    def has_role(self, *keys):
        return self.groups.filter(role__key__in=keys).exists()

    def group_keys(self):
        """What other tools are told. Built-in groups go by their fixed key, so
        renaming one can't silently change what someone can do in a tool. Others are
        marked as custom, so one named "operator" can't pass for the real thing."""
        groups = self.groups.select_related("role")
        return sorted(group.role.key if hasattr(group, "role") else f"custom:{group.name}" for group in groups)

    def tool_username(self):
        """The part of the email before the @. Tools like Forgejo allow one account per
        name, so if an earlier account has the same one, this one gets its id added. That
        covers the usual clash, not all of them: a tool may alter names or reserve some.
        It can also change if the earlier account goes, so tools identify people by `sub`."""
        name = self.email.split("@")[0]
        taken = User.objects.filter(email__startswith=f"{name}@", pk__lt=self.pk).exists()
        return f"{name}-{self.pk}" if taken else name

    # A property rather than Django's usual stored flag, so groups alone decide:
    # removing someone from a group removes their admin access at their next click.
    @property
    def is_staff(self):
        return self.is_superuser or self.has_role(*ADMIN_ROLES)

    @property
    def requires_2fa(self):
        return self.is_superuser or self.has_role(*TWO_FACTOR_ROLES)

    @property
    def needs_2fa_setup(self):
        from allauth.mfa.models import Authenticator
        from allauth.mfa.utils import is_mfa_enabled

        return self.requires_2fa and not is_mfa_enabled(self, [Authenticator.Type.TOTP])


class ToolAccess(models.Model):
    """Which groups may log in to a tool through the login service. A tool with no row
    is open to every account, and decides for itself what they can do."""

    tool = models.OneToOneField("allauth_idp_oidc.Client", on_delete=models.CASCADE, related_name="access")
    group_keys = models.JSONField(default=list)  # the same keys as User.group_keys
    # For a tool whose sessions last as long as its access token does. Empty: the usual 10 minutes.
    session_hours = models.PositiveSmallIntegerField(null=True, blank=True)

    def __str__(self):
        return f"{self.tool}: {', '.join(self.group_keys)}"


class GroupRole(models.Model):
    """Ties a group to a built-in role by a key that never changes, so the rules
    in roles.py still apply if the association renames the group."""

    group = models.OneToOneField(Group, on_delete=models.CASCADE, related_name="role")
    key = models.SlugField(unique=True)

    def __str__(self):
        return self.key
