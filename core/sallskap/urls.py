from django.contrib import admin
from django.http import Http404
from django.urls import include, path, re_path

from accounts import views


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
]
