"""The login service for other tools, built on allauth's OpenID Connect provider.
What's here is what that provider doesn't do for us: it doesn't know our 2FA rule,
our groups, or that we don't use allauth's table of email addresses."""

import json
import secrets

from allauth.idp.oidc.adapter import DefaultOIDCAdapter
from allauth.idp.oidc.models import Client, Token
from allauth.idp.oidc.views import AuthorizationView as ProviderAuthorizationView
from allauth.idp.oidc.views import TokenView as ProviderTokenView
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import AnonymousUser
from django.shortcuts import redirect, render
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt
from oauthlib.oauth2.rfc6749.errors import InvalidTokenError, MismatchingRedirectURIError

from .models import ToolAccess

SCOPES = ["openid", "email", "profile", "groups"]
CLAIMS = ["sub", "email", "name", "preferred_username", "groups"]


ACCESS_TOKEN_SECONDS = 10 * 60  # most tools only need it just after someone logs in


def may_use(user, tool):
    access = ToolAccess.objects.filter(tool=tool).first()
    return access is None or bool(set(access.group_keys) & set(user.group_keys()))


def access_token_seconds(request):
    access = ToolAccess.objects.filter(tool=request.client).first()
    return access.session_hours * 3600 if access and access.session_hours else ACCESS_TOKEN_SECONDS


class LoginServiceAdapter(DefaultOIDCAdapter):
    scope_display = {**DefaultOIDCAdapter.scope_display, "groups": _("View your groups")}

    def get_issuer(self):
        # Fixed rather than read from the request: tools store it, so it can never vary.
        return f"https://{settings.APP_HOST}"

    def generate_client_secret(self):
        # The default uses characters like # and % that some tools mishandle.
        return secrets.token_urlsafe(48)

    def get_claims(self, purpose, user, client, scopes, email=None, **kwargs):
        # Every token and every question about one comes through here, so someone who
        # has left a tool's groups since logging in gets no further either.
        if not may_use(user, client):
            raise InvalidTokenError()
        claims = {"sub": self.get_user_sub(client, user)}
        if "email" in scopes:
            # No email_verified: nothing sends email yet, and an Operator typed it in, so
            # core doesn't know. Tools read an explicit false as a claim that it's wrong,
            # and Vaultwarden refuses the person. Mail can start sending true.
            claims["email"] = user.email
        if "profile" in scopes:
            claims.update(name=user.get_full_name(), preferred_username=user.tool_username())
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
    can't get through without it, or every other tool would be a way around the rule.
    A tool can also be limited to certain groups: the group check comes first, as
    there's no point in setting up 2FA for a tool someone can't use."""

    def dispatch(self, request, *args, **kwargs):
        if self.has_wrong_return_address(request):
            return render(request, "idp/oidc/error.html", {"error": MismatchingRedirectURIError()})
        if request.user.is_authenticated:
            # A form post carries no tool here, but it's sent back to this address first.
            tool = Client.objects.filter(pk=request.GET.get("client_id")).first()
            refused = tool is not None and not may_use(request.user, tool)
            if refused or request.user.needs_2fa_setup:
                if "none" in request.GET.get("prompt", "").split():
                    # The tool asked us not to show anything, so it has to be told no.
                    request.user = AnonymousUser()
                elif refused:
                    return render(request, "tool_refused.html", {"tool": tool}, status=403)
                else:
                    messages.warning(request, _("Set up two-factor authentication, then log in to the tool again."))
                    return redirect("mfa_index")
        return super().dispatch(request, *args, **kwargs)

    def has_wrong_return_address(self, request):
        """allauth also accepts a registered address with extra query text added, so
        `callback?next=https://evil` would receive a real login code. Only an exact
        match gets through here."""
        address = request.GET.get("redirect_uri")
        tool = Client.objects.filter(pk=request.GET.get("client_id")).first()
        return bool(address and tool and address not in tool.get_redirect_uris())


class TokenView(ProviderTokenView):
    """oauthlib always issues a refresh token, even though tools aren't allowed to
    use one. Holding on to one a tool can't use could make it fail later, or log
    someone out, so it never leaves here."""

    def post(self, request):
        response = super().post(request)
        if response.status_code == 200:
            data = json.loads(response.content)
            refresh_token = data.pop("refresh_token", None)
            if refresh_token:
                Token.objects.by_value(refresh_token).filter(type=Token.Type.REFRESH_TOKEN).delete()
                response.content = json.dumps(data)
                response.headers.pop("Content-Length", None)
        return response
