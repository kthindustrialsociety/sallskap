"""The login service for other tools, built on allauth's OpenID Connect provider.
What's here is what that provider doesn't do for us: it doesn't know our 2FA rule,
our groups, or that we don't use allauth's table of email addresses."""

import secrets

from allauth.idp.oidc.adapter import DefaultOIDCAdapter
from allauth.idp.oidc.views import AuthorizationView as ProviderAuthorizationView
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import AnonymousUser
from django.shortcuts import redirect
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt

SCOPES = ["openid", "email", "profile", "groups"]
CLAIMS = ["sub", "email", "email_verified", "name", "preferred_username", "groups"]


class LoginServiceAdapter(DefaultOIDCAdapter):
    scope_display = {**DefaultOIDCAdapter.scope_display, "groups": _("View your groups")}

    def get_issuer(self):
        # Fixed rather than read from the request: tools store it, so it can never vary.
        return f"https://{settings.APP_HOST}"

    def generate_client_secret(self):
        # The default uses characters like # and % that some tools mishandle.
        return secrets.token_urlsafe(48)

    def get_claims(self, purpose, user, client, scopes, email=None, **kwargs):
        claims = {"sub": self.get_user_sub(client, user)}
        if "email" in scopes:
            # Not verified: nothing sends email yet, and an Operator typed it in.
            claims.update(email=user.email, email_verified=False)
        if "profile" in scopes:
            claims.update(name=user.name or user.email, preferred_username=user.email.split("@")[0])
        if "groups" in scopes:
            claims["groups"] = user.group_keys()
        return claims

    def populate_server_metadata(self, data):
        """Only advertise what's open: see the routes in sallskap/urls.py."""
        data.pop("device_authorization_endpoint", None)
        data.pop("revocation_endpoint", None)
        data.update(
            grant_types_supported=["authorization_code"],
            response_types_supported=["code"],
            scopes_supported=SCOPES,
            claims_supported=CLAIMS,
        )


@method_decorator(csrf_exempt, name="dispatch")  # as on the view this extends
class AuthorizationView(ProviderAuthorizationView):
    """The door other tools send people through. Someone whose group requires 2FA
    can't get through without it, or every other tool would be a way around the rule."""

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and request.user.needs_2fa_setup:
            if "none" in request.GET.get("prompt", "").split():
                # The tool asked us not to show anything, so it has to be told no.
                request.user = AnonymousUser()
            else:
                messages.warning(request, _("Set up two-factor authentication, then log in to the tool again."))
                return redirect("mfa_index")
        return super().dispatch(request, *args, **kwargs)
