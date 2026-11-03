from allauth.account.adapter import DefaultAccountAdapter


class AccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        return False  # accounts are created by an Operator

    def _get_login_attempts_cache_key(self, request, **credentials):
        """Count wrong passwords per account and address. allauth counts them per
        account alone, so anyone who knows an email could keep that person out.
        This is a private method: Lockout in tests.py fails if an upgrade changes it."""
        return f"{super()._get_login_attempts_cache_key(request, **credentials)}:{self.get_client_ip(request)}"
