from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from .models import Unit


class UnitForm(forms.ModelForm):
    class Meta:
        model = Unit
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        parent = self.fields.get("parent")
        if parent is not None and self.instance.parent_id:  # which may be archived, with this unit
            parent.queryset = Unit.objects.filter(models.Q(archived_at__isnull=True) | models.Q(pk=self.instance.parent_id))

    def clean(self):
        cleaned = super().clean()
        if "taking_part" in self.fields:
            kind = cleaned.get("kind", self.instance.kind)
            parent = cleaned.get("parent", self.instance.parent)
            try:
                Unit.check_taking_part(kind, parent, cleaned.get("taking_part") or [])
            except ValidationError as error:
                self.add_error("taking_part", error)
        return cleaned


class ArchivedFilter(admin.SimpleListFilter):
    title = _("archived")
    parameter_name = "archived"

    def lookups(self, request, model_admin):
        return [("no", _("No")), ("yes", _("Yes"))]

    def queryset(self, request, queryset):
        if self.value() in ("no", "yes"):
            return queryset.filter(archived_at__isnull=self.value() == "no")
        return queryset


@admin.register(Unit)
class UnitAdmin(admin.ModelAdmin):
    """The operators' back office for the tree. Who else may change units comes with
    authority (structure.md §4), through the pages and the CLI."""

    form = UnitForm
    list_display = ("tree_name", "key", "kind", "end_date", "archived")
    list_filter = ("kind", ArchivedFilter)
    search_fields = ("name", "key")
    ordering = ("path",)
    filter_horizontal = ("taking_part",)
    actions = ("archive", "unarchive")

    def get_fields(self, request, obj=None):
        if obj and obj.is_root:
            return ["kind", "key", "name", "description", "primary_colour", "accent_colour"]
        fields = ["kind", "key", "name", "description", "parent", "primary_colour", "accent_colour", "end_date", "run_for", "taking_part"]
        return fields + ["archived_at"] if obj else fields

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return []
        fixed = ["key", "archived_at"]  # archived through the actions, so the units under it follow
        return fixed + ["kind", "name"] if obj.is_root else fixed  # the root's name comes from the config

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)  # each unit is checked on its own page
        return actions

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.deletion_problem():
            return False
        return super().has_delete_permission(request, obj)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "parent":
            kwargs["queryset"] = Unit.objects.filter(archived_at__isnull=True)
        if db_field.name == "run_for":
            kwargs["queryset"] = Unit.objects.filter(kind=Unit.Kind.ASSOCIATION)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == "taking_part":
            kwargs["queryset"] = Unit.objects.filter(kind=Unit.Kind.ASSOCIATION)
        return super().formfield_for_manytomany(db_field, request, **kwargs)

    @admin.display(description=_("name"), ordering="path")
    def tree_name(self, obj):
        return "\u00a0" * 4 * (obj.path.count("/") - 2) + obj.name  # non-breaking, so the page keeps them

    @admin.display(description=_("archived"), boolean=True, ordering="archived_at")
    def archived(self, obj):
        return obj.archived

    def _each(self, request, queryset, method, done_message):
        done = 0
        for unit in queryset.order_by("path"):  # from the top, so a team comes back after the one above it
            try:
                getattr(unit, method)()
                done += 1
            except ValidationError as error:
                self.message_user(request, f"{unit}: {' '.join(error.messages)}", messages.ERROR)
        if done:
            self.message_user(request, done_message(done))

    @admin.action(description=_("Archive, with everything inside"), permissions=["change"])
    def archive(self, request, queryset):
        self._each(request, queryset, "archive", lambda n: ngettext("%d unit archived.", "%d units archived.", n) % n)

    @admin.action(description=_("Take out of the archive"), permissions=["change"])
    def unarchive(self, request, queryset):
        self._each(request, queryset, "unarchive", lambda n: ngettext("%d unit taken out of the archive.", "%d units taken out of the archive.", n) % n)
