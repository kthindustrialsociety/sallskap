from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode


def password_link(user):
    """A link that lets one person choose a password, once, within
    PASSWORD_RESET_TIMEOUT. It doesn't log them in, so it can't get around 2FA."""
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return f"https://{settings.APP_HOST}{reverse('set_password', args=[uid, token])}"
