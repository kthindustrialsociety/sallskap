from django.contrib import admin
from django.http import Http404
from django.urls import include, path, re_path

from accounts import views
from accounts.login_service import AuthorizationView, TokenView


def not_found(request, *args, **kwargs):
    raise Http404


urlpatterns = [
    path("", views.home, name="home"),
    path("admin/", admin.site.urls),
    path("set-password/<uidb64>/<token>/", views.SetPasswordView.as_view(), name="set_password"),
    # Only what we use is open. Everything else under accounts/ answers 404, so a
    # route added by an allauth upgrade can't go live by itself. allauth's own
    # templates need the closed routes' names, so they stay defined below.
    re_path(r"^accounts/(?!(login|logout|reauthenticate|password/change|inactive)/?$|2fa(/|$))", not_found),
    path("accounts/", include("allauth.urls")),
    # The login service for other tools, closed in the same way. The device, revoke,
    # introspect and registration routes stay shut: the device flow would be a way
    # around the 2FA rule, and tools are only added through `kit`.
    re_path(r"^identity/o/(?!(authorize|logout|api/token|api/userinfo)$)", not_found),
    path("identity/o/authorize", AuthorizationView.as_view()),
    path("identity/o/api/token", TokenView.as_view()),
    path("", include("allauth.idp.urls")),
]
