from allauth.mfa.models import Authenticator
from django.contrib import admin
from django.contrib.auth.models import Group
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from structure.models import AuditEntry, Person

from . import sync
from .models import AccessSync, FollowUp, User

ADMIN = AuditEntry.Via.ADMIN


def account(user):
    """What the audit log compares when an operator changes an account. Its address and
    name are recorded as changed, without their values."""
    if user is None:
        return {}
    permissions = sorted(f"{p.content_type.app_label}.{p.codename}" for p in user.user_permissions.select_related("content_type"))
    return {
        "email": user.email, "name": user.name, "is_active": user.is_active, "is_superuser": user.is_superuser,
        "operator": user.is_operator, "user_permissions": permissions,
    }


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("email", "name", "is_active", "group_list", "has_2fa", "last_login")
    list_filter = ("is_active", "groups")
    search_fields = ("email", "name")
    ordering = ("email",)
    filter_horizontal = ("groups", "user_permissions")
    readonly_fields = ("last_login", "date_joined", "keys", "access_in_tools")

    def get_fields(self, request, obj=None):
        fields = ["email", "name", "is_active", "groups"]
        if request.user.is_superuser:
            # Anyone who could edit these could give themselves everything.
            fields += ["is_superuser", "user_permissions"]
        if obj:
            fields += ["keys", "access_in_tools", "last_login", "date_joined"]
        return fields

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == "groups":
            kwargs["help_text"] = _("Operator is the one group. Everything else comes from memberships and positions.")
        return super().formfield_for_manytomany(db_field, request, **kwargs)

    def get_queryset(self, request):
        two_factor = Authenticator.objects.filter(user=OuterRef("pk"), type=Authenticator.Type.TOTP)
        return super().get_queryset(request).annotate(_has_2fa=Exists(two_factor)).prefetch_related("groups")

    def has_change_permission(self, request, obj=None):
        if obj is not None and obj.is_superuser and not request.user.is_superuser:
            return False
        return super().has_change_permission(request, obj)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.set_unusable_password()  # they choose their own, through `kit link`
        obj.audit_before = account(User.objects.filter(pk=obj.pk).first() if change else None)
        super().save_model(request, obj, form, change)

    def delete_model(self, request, obj):
        self.delete_queryset(request, User.objects.filter(pk=obj.pk))

    @transaction.atomic
    def delete_queryset(self, request, queryset):
        """Only superusers can. Each is a row, and so is whether its person went with it,
        which happens when nothing was ever tied to them."""
        for user in queryset:
            pk, before = user.pk, account(user)
            person = Person.objects.filter(user=user).values_list("pk", flat=True).first()
            user.delete()
            changes = {**AuditEntry.compare(before, {}, "accounts.user"), "person_removed": person is not None and not Person.objects.filter(pk=person).exists()}
            AuditEntry.record("delete_account", ADMIN, obj=user, object_id=pk, changes=changes, **AuditEntry.by(request.user))

    def save_related(self, request, form, formsets, change):
        """After the groups, so Operator shows in the audit log's row."""
        super().save_related(request, form, formsets, change)
        user = form.instance
        changes = AuditEntry.compare(getattr(user, "audit_before", {}), account(user), "accounts.user")
        AuditEntry.record("change_account" if change else "add_account", ADMIN, obj=user, changes=changes, **AuditEntry.by(request.user))

    @admin.display(description=_("keys"))
    def keys(self, obj):
        """What the tools are told about them: structure.md §5."""
        from structure.access import keys

        return ", ".join(keys(obj)) or "-"

    @admin.display(description=_("access in tools"))
    def access_in_tools(self, obj):
        row = AccessSync.objects.filter(user=obj).first()
        return sync.describe(row) if row else _("Nothing to send")

    @admin.display(description=_("groups"))
    def group_list(self, obj):
        return ", ".join(group.name for group in obj.groups.all())

    @admin.display(description=_("2FA"), boolean=True, ordering="_has_2fa")
    def has_2fa(self, obj):
        return obj._has_2fa


# Operator is the one group, made and kept by roles.py, so there's nothing to manage here.
admin.site.unregister(Group)


admin.site.unregister(Authenticator)


@admin.register(Authenticator)
class AuthenticatorAdmin(admin.ModelAdmin):
    """allauth's version shows the `data` field, which holds the 2FA secret. This
    one leaves it out, so an Operator can remove someone's 2FA but never copy it.

    Removing any record removes all of that person's 2FA. Otherwise their old
    recovery codes would still log in after they set up 2FA again."""

    list_display = fields = readonly_fields = ("user", "type", "created_at", "last_used_at")
    list_filter = ("type",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.user.is_superuser and not request.user.is_superuser:
            return False
        return super().has_delete_permission(request, obj)

    def delete_model(self, request, obj):
        self.delete_queryset(request, Authenticator.objects.filter(pk=obj.pk))

    @transaction.atomic
    def delete_queryset(self, request, queryset):
        for user in User.objects.filter(pk__in=queryset.values("user")):
            Authenticator.objects.filter(user=user).delete()
            AuditEntry.record("remove_2fa", ADMIN, obj=user, changes={"two_factor": [True, False]}, **AuditEntry.by(request.user))


@admin.register(FollowUp)
class FollowUpAdmin(admin.ModelAdmin):
    """Only `sync_tools` adds these, and they stay as a record. All anyone can do is say
    one is done, which records who and when."""

    list_display = ("text", "about", "tool", "created_at", "done_at", "done_by")
    list_filter = (("done_at", admin.EmptyFieldListFilter),)
    fields = ("text", "about", "tool", "created_at", "done_at", "done_by")
    readonly_fields = fields
    actions = ("mark_done",)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description=_("Mark as done"), permissions=["change"])
    @transaction.atomic
    def mark_done(self, request, queryset):
        open_ones = list(queryset.filter(done_at__isnull=True).values_list("pk", flat=True))
        done = FollowUp.objects.filter(pk__in=open_ones, done_at__isnull=True).update(done_at=timezone.now(), done_by=request.user)
        for pk in open_ones:
            AuditEntry.record("follow_up_done", ADMIN, obj=FollowUp(pk=pk), changes={"done": [False, True]}, **AuditEntry.by(request.user))
        self.message_user(request, ngettext("%d follow-up marked as done.", "%d follow-ups marked as done.", done) % done)
