from allauth.mfa.models import Authenticator
from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin as DjangoGroupAdmin
from django.contrib.auth.models import Group
from django.db.models import Exists, OuterRef
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from . import sync
from .models import AccessSync, FollowUp, User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("email", "name", "is_active", "group_list", "has_2fa", "last_login")
    list_filter = ("is_active", "groups")
    search_fields = ("email", "name")
    ordering = ("email",)
    filter_horizontal = ("groups", "user_permissions")
    readonly_fields = ("last_login", "date_joined", "access_in_tools")

    def get_fields(self, request, obj=None):
        fields = ["email", "name", "is_active", "groups"]
        if request.user.is_superuser:
            # Anyone who could edit these could give themselves everything.
            fields += ["is_superuser", "user_permissions"]
        if obj:
            fields += ["access_in_tools", "last_login", "date_joined"]
        return fields

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
        super().save_model(request, obj, form, change)

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


admin.site.unregister(Group)


@admin.register(Group)
class GroupAdmin(DjangoGroupAdmin):
    def get_fields(self, request, obj=None):
        # Permissions come from roles.py. Editing them here could grant anything.
        return ["name", "permissions"] if request.user.is_superuser else ["name"]

    def has_delete_permission(self, request, obj=None):
        if obj is not None and hasattr(obj, "role"):
            return False
        return super().has_delete_permission(request, obj)


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
        Authenticator.objects.filter(user=obj.user).delete()

    def delete_queryset(self, request, queryset):
        Authenticator.objects.filter(user__in=queryset.values("user")).delete()


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
    def mark_done(self, request, queryset):
        done = queryset.filter(done_at__isnull=True).update(done_at=timezone.now(), done_by=request.user)
        self.message_user(request, ngettext("%d follow-up marked as done.", "%d follow-ups marked as done.", done) % done)
