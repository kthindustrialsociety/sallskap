from django.contrib.admin.apps import AdminConfig


def _no_history(*args, **kwargs):
    """Django's own admin history kept the name or address of everything an operator
    changed, with nothing to delete it. The audit log records those changes instead,
    naming nobody (structure.md §7)."""


class SallskapAdminConfig(AdminConfig):
    default_site = "sallskap.admin.SallskapAdminSite"

    def ready(self):
        super().ready()
        from django.contrib.admin.models import LogEntryManager

        # Every way the admin page records a change goes through these. A test checks
        # that changes on the admin page leave no history, so an upgrade can't undo this.
        LogEntryManager.log_action = LogEntryManager.log_actions = _no_history
