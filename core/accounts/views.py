from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import PasswordResetConfirmView
from django.shortcuts import render
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _


@login_required
def home(request):
    return render(request, "home.html")


class SetPasswordView(PasswordResetConfirmView):
    """Where `kit link` points. Django's view spends the token and never logs
    anyone in, so the person still goes through login and 2FA afterwards."""

    template_name = "set_password.html"
    success_url = reverse_lazy("account_login")

    def get_user(self, uidb64):
        user = super().get_user(uidb64)
        return user if user is not None and user.is_active else None

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, _("Your password is set. Log in to continue."))
        return response
