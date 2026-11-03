from django.contrib.admin.apps import AdminConfig


class SallskapAdminConfig(AdminConfig):
    default_site = "sallskap.admin.SallskapAdminSite"
