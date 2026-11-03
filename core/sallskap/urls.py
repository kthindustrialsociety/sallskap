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
    # allauth's own templates need these URL names, so they stay defined but
    # answer 404. There's no signup, and email isn't part of the system yet.
    re_path(r"^accounts/(signup|email|confirm-email|password/reset|login/code|phone)(/|$)", not_found),
    path("accounts/", include("allauth.urls")),
]
