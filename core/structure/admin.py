from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from .models import EmailAddress, Membership, Person, Position, Reason, Unit


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


# ---- People and ties: structure.md §2 and §3 ------------------------------------------


class AddressForm(forms.ModelForm):
    class Meta:
        model = EmailAddress
        fields = ["address"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:  # an address is fixed: add the new one and remove the old
            self.fields["address"].disabled = True


class AddressFormSet(forms.BaseInlineFormSet):
    def clean(self):
        super().clean()
        for form in self.deleted_forms:
            if form.instance.pk and form.instance.is_login_address():
                raise ValidationError(_("%(address)s is how they log in. Change the account's address first.") % {"address": form.instance.address})


class AddressInline(admin.TabularInline):
    model = EmailAddress
    form = AddressForm
    formset = AddressFormSet
    fields = ("address", "verified_at")
    readonly_fields = ("verified_at",)
    extra = 0


class TieInline(admin.TabularInline):
    """Shown on the person's page. Each tie is changed on its own page."""

    extra = 0
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class MembershipInline(TieInline):
    model = Membership
    fields = readonly_fields = ("association", "kind", "start", "end")


class PositionInline(TieInline):
    model = Position
    fields = readonly_fields = ("unit", "role", "title", "start", "end")


class ReasonInline(TieInline):
    model = Reason
    fields = readonly_fields = ("unit", "kind", "legal_basis", "start", "expires")


@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    """Everyone the association holds data about. Who else may see whom comes with
    authority (structure.md §2 and §4)."""

    list_display = ("__str__", "addresses", "user", "member_of_list")
    list_select_related = ("user",)
    search_fields = ("name", "emails__address")
    fields = ("name", "user", "created_at")
    readonly_fields = ("user", "created_at")
    inlines = (AddressInline, MembershipInline, PositionInline, ReasonInline)
    actions = ("merge",)

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("emails")

    def get_inlines(self, request, obj):
        return self.inlines if obj else (AddressInline,)  # a new person has no ties yet

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.deletion_problem():
            return False
        return super().has_delete_permission(request, obj)

    @admin.display(description=_("email addresses"))
    def addresses(self, obj):
        return ", ".join(e.address + ("" if e.verified_at else " (?)") for e in obj.emails.all())

    @admin.display(description=_("member of"))
    def member_of_list(self, obj):
        return ", ".join(str(unit) for unit in obj.member_of())

    @admin.action(description=_("Merge the two selected into one person"), permissions=["change"])
    def merge(self, request, queryset):
        people = list(queryset.order_by("pk"))
        if len(people) != 2:
            self.message_user(request, _("Select exactly two people to merge."), messages.ERROR)
            return
        try:
            kept = Person.merge(*people)
        except ValidationError as error:
            self.message_user(request, " ".join(error.messages), messages.ERROR)
            return
        self.message_user(request, _("Merged into %(person)s.") % {"person": kept})


class CurrentFilter(admin.SimpleListFilter):
    title = _("current")
    parameter_name = "current"

    def lookups(self, request, model_admin):
        return [("yes", _("Yes")), ("no", _("No"))]

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.current()
        if self.value() == "no":
            return queryset.exclude(pk__in=queryset.current().values("pk"))
        return queryset


class TieAdmin(admin.ModelAdmin):
    """Ties are ended, never deleted. Who it's for, where and which role are fixed once
    set: end it and add a new one."""

    fixed_on_change = ()
    search_fields = ("person__name", "person__emails__address")
    autocomplete_fields = ("person",)
    actions = ("end_today",)

    def get_readonly_fields(self, request, obj=None):
        return self.fixed_on_change if obj else ()

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description=_("current"), boolean=True)
    def current(self, obj):
        return obj.is_current()

    @admin.action(description=_("End today"), permissions=["change"])
    def end_today(self, request, queryset):
        done = 0
        for tie in queryset.current():
            try:
                tie.end_on()
                done += 1
            except ValidationError as error:
                self.message_user(request, f"{tie}: {' '.join(error.messages)}", messages.ERROR)
        if done:
            self.message_user(request, ngettext("%d ended today.", "%d ended today.", done) % done)


@admin.register(Membership)
class MembershipAdmin(TieAdmin):
    list_display = ("person", "association", "kind", "start", "end", "current")
    list_filter = (CurrentFilter, "kind", "association")
    fields = ("person", "association", "kind", "start", "end")
    fixed_on_change = ("person", "association")
    list_select_related = ("person", "association")


@admin.register(Position)
class PositionAdmin(TieAdmin):
    list_display = ("person", "role", "title", "unit", "start", "end", "current")
    list_filter = (CurrentFilter, "role", "unit")
    fields = ("person", "unit", "role", "title", "start", "end", "appointed_by", "minutes")
    fixed_on_change = ("person", "unit", "role")
    list_select_related = ("person", "unit")

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "unit":
            kwargs["queryset"] = Unit.objects.filter(archived_at__isnull=True)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(Reason)
class ReasonAdmin(TieAdmin):
    list_display = ("person", "kind", "unit", "legal_basis", "start", "expires", "current")
    list_filter = (CurrentFilter, "kind", "legal_basis")
    fields = ("person", "kind", "unit", "source", "legal_basis", "start", "expires")
    fixed_on_change = ("person", "unit", "kind")
    list_select_related = ("person", "unit")
