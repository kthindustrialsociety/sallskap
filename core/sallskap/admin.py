from django.conf import settings
from django.contrib import admin, messages
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, resolve_url
from django.urls import reverse
from django.utils.translation import gettext_lazy as _


SHOWN_AT_ONCE = 10


class SallskapAdminSite(admin.AdminSite):
    site_title = site_header = _("Sällskap admin")
    index_title = _("Administration")

    def has_permission(self, request):
        user = request.user
        return user.is_active and user.is_staff and not user.needs_2fa_setup

    def index(self, request, extra_context=None):
        """The two things that shouldn't wait for someone to go looking."""
        from accounts import sync
        from accounts.models import FollowUp

        def more(queryset):
            extra = queryset.count() - SHOWN_AT_ONCE
            return _("...and %(count)d more.") % {"count": extra} if extra > 0 else None

        if request.user.has_perm("accounts.view_user"):
            stuck = sync.stuck()
            for row in stuck[:SHOWN_AT_ONCE]:
                messages.error(request, _("A change to %(person)s hasn't reached every tool yet. %(what)s") % {"person": sync.label(row), "what": sync.describe(row)})
            if more(stuck):
                messages.error(request, more(stuck))
        if request.user.has_perm("accounts.view_followup"):
            open_ones = FollowUp.objects.filter(done_at__isnull=True)
            for follow_up in open_ones[:SHOWN_AT_ONCE]:
                messages.warning(request, follow_up.text)
            if more(open_ones):
                messages.warning(request, more(open_ones))
        return super().index(request, extra_context)

    def login(self, request, extra_context=None):
        """Never shows Django's own login form, which knows nothing about 2FA
        or the login limits. Everyone logs in through allauth."""
        user = request.user
        if not user.is_authenticated:
            # allauth checks this address before using it, so it can't lead to another site.
            wanted = request.GET.get(REDIRECT_FIELD_NAME) or reverse("admin:index")
            return redirect_to_login(wanted, resolve_url(settings.LOGIN_URL))
        if self.has_permission(request):
            return redirect("admin:index")
        if user.is_active and user.is_staff:  # staff, so only 2FA can be missing
            messages.warning(request, _("Set up two-factor authentication to use the admin page."))
            return redirect("mfa_index")
        raise PermissionDenied

    def password_change(self, request, extra_context=None):
        # allauth's page is the one with a rate limit.
        return redirect("account_change_password")
