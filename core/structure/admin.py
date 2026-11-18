"""The operators' back office for the tree (structure.md §7). It shows units, people, ties,
grants, seat rules and the audit log, and changes none of them: operators never pass
can() (structure.md §4), so changes go through structure/actions.py, by people whose
positions allow them. The one change here is merging two people, an operator's own,
which the audit log records."""

from django.apps import apps
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Coalesce
from django.utils.translation import gettext_lazy as _
from django.utils.translation import gettext

from .models import SINCE_DELETED, AuditEntry, EmailAddress, Grant, Membership, Person, Position, Reason, SeatRule, Unit, path_and_above


class Shown:
    """Seen, never changed, by anyone, superusers included."""

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class ShownAdmin(Shown, admin.ModelAdmin):
    pass


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
class UnitAdmin(ShownAdmin):
    list_display = ("tree_name", "key", "kind", "end_date", "archived")
    list_filter = ("kind", ArchivedFilter)
    search_fields = ("name", "key")
    ordering = ("path",)

    def get_fields(self, request, obj=None):
        if obj and obj.is_root:
            return ["kind", "key", "name", "description", "primary_colour", "accent_colour"]
        return ["kind", "key", "name", "description", "parent", "primary_colour", "accent_colour", "end_date", "run_for", "taking_part", "archived_at"]

    @admin.display(description=_("name"), ordering="path")
    def tree_name(self, obj):
        return "\u00a0" * 4 * (obj.path.count("/") - 2) + obj.name  # non-breaking, so the page keeps them

    @admin.display(description=_("archived"), boolean=True, ordering="archived_at")
    def archived(self, obj):
        return obj.archived


# ---- People and ties: structure.md §2 and §3 ------------------------------------------


class AddressInline(Shown, admin.TabularInline):
    model = EmailAddress
    fields = ("address", "verified_at")
    extra = 0


class TieInline(Shown, admin.TabularInline):
    extra = 0
    show_change_link = True


class MembershipInline(TieInline):
    model = Membership
    fields = ("association", "kind", "start", "end")


class PositionInline(TieInline):
    model = Position
    fields = ("unit", "role", "title", "start", "end")


class ReasonInline(TieInline):
    model = Reason
    fields = ("unit", "kind", "legal_basis", "start", "expires")


@admin.register(Person)
class PersonAdmin(ShownAdmin):
    """Everyone the association holds data about. Who else may see whom comes with
    authority (structure.md §2 and §4)."""

    list_display = ("shown_name", "addresses", "user", "member_of_list")
    list_select_related = ("user",)
    search_fields = ("name", "emails__address")
    fields = ("name", "user", "created_at")
    inlines = (AddressInline, MembershipInline, PositionInline, ReasonInline)
    actions = ("merge",)

    def get_queryset(self, request):
        current = Membership.objects.current().select_related("association")
        return super().get_queryset(request).prefetch_related("emails", models.Prefetch("memberships", current, to_attr="current_memberships"))

    def get_changelist_instance(self, request):
        self._units = {unit.path: unit for unit in Unit.objects.filter(kind=Unit.Kind.ASSOCIATION)}  # one query for the list
        return super().get_changelist_instance(request)

    @admin.display(description=_("name"), ordering="name")
    def shown_name(self, obj):
        return obj.name or next((e.address for e in obj.emails.all()), "") or str(obj)

    @admin.display(description=_("email addresses"))
    def addresses(self, obj):
        return ", ".join(e.address + ("" if e.verified_at else " (?)") for e in obj.emails.all())

    @admin.display(description=_("member of"))
    def member_of_list(self, obj):
        """Counting upwards, as Person.member_of does, from what the list already loaded."""
        paths = sorted({path for m in obj.current_memberships for path in path_and_above(m.association.path)})
        return ", ".join(str(self._units[path]) for path in paths if path in self._units)

    def has_merge_permission(self, request):
        return self.has_view_permission(request)

    @admin.action(description=_("Merge the two selected into one person"), permissions=["merge"])
    def merge(self, request, queryset):
        people = list(queryset.order_by("pk"))
        if len(people) != 2:
            self.message_user(request, gettext("Select exactly two people to merge."), messages.ERROR)
            return
        try:
            kept = Person.merge(*people, via=AuditEntry.Via.ADMIN, by=request.user)
        except ValidationError as error:
            self.message_user(request, " ".join(error.messages), messages.ERROR)
            return
        self.message_user(request, gettext("Merged into %(person)s.") % {"person": kept})


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


class TieAdmin(ShownAdmin):
    search_fields = ("person__name", "person__emails__address")

    @admin.display(description=_("current"), boolean=True)
    def current(self, obj):
        return obj.is_current()


@admin.register(Membership)
class MembershipAdmin(TieAdmin):
    list_display = ("person", "association", "kind", "start", "end", "current")
    list_filter = (CurrentFilter, "kind", "association")
    fields = ("person", "association", "kind", "start", "end")
    list_select_related = ("person", "association")


class SeatFilter(admin.SimpleListFilter):
    title = _("from a seat rule")
    parameter_name = "seat"

    def lookups(self, request, model_admin):
        return [("yes", _("Yes")), ("no", _("No"))]

    def queryset(self, request, queryset):
        if self.value() in ("yes", "no"):
            return queryset.filter(seat_rule__isnull=self.value() == "no")
        return queryset


@admin.register(Position)
class PositionAdmin(TieAdmin):
    """A seat is shown with its rule and the position it comes from. A position kept after
    its holder was deleted shows the name kept on it (structure.md §3)."""

    list_display = ("holder", "role", "title", "unit", "start", "end", "current")
    list_filter = (CurrentFilter, SeatFilter, "role", "unit")
    fields = ("holder", "unit", "role", "title", "start", "end", "appointed_by", "minutes")
    readonly_fields = ("holder",)
    list_select_related = ("person", "unit")
    search_fields = (*TieAdmin.search_fields, "kept_name")

    def get_fields(self, request, obj=None):
        return (*self.fields, "seat_rule", "seat_from") if obj is not None and obj.is_seat() else self.fields

    @admin.display(description=_("held by"), ordering=Coalesce("person__name", "kept_name"))
    def holder(self, obj):
        return obj.holder()


def _since_deleted(person):
    """Who gave a grant or made a rule, once they may have been deleted (structure.md §3)."""
    return str(person) if person is not None else str(SINCE_DELETED)


@admin.register(Reason)
class ReasonAdmin(TieAdmin):
    list_display = ("person", "kind", "unit", "legal_basis", "start", "expires", "current")
    list_filter = (CurrentFilter, "kind", "legal_basis")
    fields = ("person", "kind", "unit", "source", "legal_basis", "start", "expires")
    list_select_related = ("person", "unit")


@admin.register(Grant)
class GrantAdmin(TieAdmin):
    """A permission given to a role in a unit (structure.md §4)."""

    list_display = ("permission", "role", "held_in", "unit", "include_below", "start", "end", "current")
    list_filter = (CurrentFilter, "permission", "role")
    fields = ("permission", "role", "held_in", "unit", "include_below", "start", "end", "giver")
    readonly_fields = ("giver",)
    list_select_related = ("held_in", "unit")
    search_fields = ("held_in__name", "unit__name", "given_by__name")

    @admin.display(description=_("given by"))
    def giver(self, obj):
        return _since_deleted(obj.given_by)


@admin.register(SeatRule)
class SeatRuleAdmin(TieAdmin):
    """Seats that come with another position (structure.md §4)."""

    list_display = ("__str__", "start", "end", "current")
    list_filter = (CurrentFilter, "role")
    fields = ("unit", "role", "roles_shown", "held_in", "each_association_under", "start", "end", "maker")
    readonly_fields = ("roles_shown", "maker")
    list_select_related = ("unit", "held_in")
    search_fields = ("unit__name", "held_in__name", "made_by__name")

    @admin.display(description=_("for the roles"))
    def roles_shown(self, obj):
        return ", ".join(obj.role_labels())

    @admin.display(description=_("made by"))
    def maker(self, obj):
        return _since_deleted(obj.made_by)


# ---- The audit log: structure.md §7 ------------------------------------------------------

# What a row's changes hold besides a field's before and after.
LISTS = ("ended", "archived_with_it")


def _merged_into(ids):
    """For people merged into another since, who they are now, from the merges' rows, which
    say who went into whom. The rows they acted in keep their old id, as the log is only
    ever added to."""
    into = {changes["person"][0]: changes["person"][1] for changes in AuditEntry.objects.filter(action="merge").values_list("changes", flat=True)}
    found = {}
    for pk in ids:
        seen, now = set(), pk
        while now in into and now not in seen:  # one merged, then merged again
            seen.add(now)
            now = into[now]
        if now != pk:
            found[pk] = now
    return found


def _model(kind):
    try:
        return apps.get_model(kind) if kind else None
    except (LookupError, ValueError):
        return None


def _shown(value):
    return "-" if value is None or value == "" else ", ".join(map(str, value)) if isinstance(value, list) else str(value)


@admin.register(AuditEntry)
class AuditEntryAdmin(ShownAdmin):
    """Who did what, where, and through which way in. People are shown by name while they
    exist, looked up each time, as the log itself names nobody."""

    list_display = ("at", "action", "via", "who", "unit_key", "what", "shown_changes")
    list_filter = ("via", "action")
    search_fields = ("unit_key", "action")
    date_hierarchy = "at"
    list_per_page = 50
    fields = ("at", "action", "via", "who", "unit_key", "what", "shown_changes")
    readonly_fields = ("who", "what", "shown_changes")

    def get_changelist_instance(self, request):
        """Everyone and everything on the page in a few queries, rather than a few for each
        row. The names go on that request's rows."""
        changelist = super().get_changelist_instance(request)
        rows = changelist.result_list
        ids = {row.actor_person for row in rows if row.actor_person}
        people = Person.objects.prefetch_related("emails").in_bulk(ids)
        merged = _merged_into(ids - set(people)) if ids - set(people) else {}
        people.update(Person.objects.prefetch_related("emails").in_bulk(set(merged.values())))
        things = {}
        for kind in {row.object_type for row in rows if row.object_type}:
            model = _model(kind)
            if model is not None:
                related = [f.name for f in model._meta.concrete_fields if f.is_relation and f.name in ("person", "unit", "association", "held_in")]
                ids = {row.object_id for row in rows if row.object_type == kind}
                things.update({(kind, pk): obj for pk, obj in model._default_manager.select_related(*related).in_bulk(ids).items()})
        for row in rows:
            row.found_person = people.get(row.actor_person), people.get(merged.get(row.actor_person))
            row.found_thing = things.get((row.object_type, row.object_id))
        return changelist

    @admin.display(description=_("by"))
    def who(self, obj):
        if obj.actor_person is None:
            return "-"
        if hasattr(obj, "found_person"):
            found, kept = obj.found_person
        else:
            found = Person.objects.filter(pk=obj.actor_person).first()
            kept = Person.objects.filter(pk=_merged_into({obj.actor_person}).get(obj.actor_person)).first() if found is None else None
        if found is not None:
            return str(found)
        if kept is not None:  # they were merged into another, as the merge's own row says
            return gettext("%(person)s, merged since") % {"person": kept}
        return str(SINCE_DELETED)

    @admin.display(description=_("what"))
    def what(self, obj):
        model = _model(obj.object_type)
        if model is None:
            return obj.object_type or "-"
        found = obj.found_thing if hasattr(obj, "found_thing") else model._default_manager.filter(pk=obj.object_id).first()
        return str(found) if found is not None else gettext("a %(kind)s since deleted") % {"kind": model._meta.verbose_name}

    @admin.display(description=_("changes"))
    def shown_changes(self, obj):
        parts = []
        for name, value in obj.changes.items():
            if value is None:
                parts.append(gettext("%(field)s changed") % {"field": name})
            elif name in LISTS or not (isinstance(value, list) and len(value) == 2):
                parts.append(f"{name}: {_shown(value)}")
            else:
                parts.append(f"{name}: {_shown(value[0])} → {_shown(value[1])}")
        return "; ".join(parts) or "-"
