from django.conf import settings
from django.contrib import admin, messages
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, resolve_url
from django.urls import reverse
from django.utils.translation import gettext_lazy as _


class SallskapAdminSite(admin.AdminSite):
    site_title = site_header = _("Sällskap admin")
    index_title = _("Administration")

    def has_permission(self, request):
        user = request.user
        return user.is_active and user.is_staff and not user.needs_2fa_setup

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
