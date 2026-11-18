"""The tree of associations and teams: structure.md §1. The rules are checked on
every save, wherever it comes from, and the database holds the ones it can."""

import re
from contextvars import ContextVar

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.validators import RegexValidator
from django.db import models, transaction
from django.db.models.functions import Lower
from django.db.models.signals import m2m_changed
from django.dispatch import receiver
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

# Lowercase letters, digits and single hyphens, from a letter to a letter or digit, at
# most 40 characters: what every tool accepts as a name. kit/kit.py checks the same.
KEY = r"^[a-z]([a-z0-9-]{0,38}[a-z0-9])?$"


def validate_key(key):
    if not re.fullmatch(KEY, key or "") or "--" in key:
        raise ValidationError(
            _("%(key)s isn't a valid key. Use lowercase letters, digits and single hyphens, starting with a letter and ending with a letter or digit, at most 40 characters."),
            params={"key": repr(key)},
        )


colour = RegexValidator(r"^#[0-9a-fA-F]{6}$", _("Write a colour as # and six hex digits, such as #1a5fb4."))


def path_and_above(path):
    """A unit's path and those of every unit above it: /a/b/ gives /a/ and /a/b/."""
    keys = path.strip("/").split("/")
    return ["/" + "/".join(keys[: i + 1]) + "/" for i in range(len(keys))]


class UnitQuerySet(models.QuerySet):
    def root(self):
        return self.get(parent=None)

    def delete(self):
        # One at a time, so each is checked: see check_can_delete.
        for unit in self:
            unit.check_can_delete()
        return super().delete()


class Unit(models.Model):
    class Kind(models.TextChoices):
        ASSOCIATION = "association", _("association")
        TEAM = "team", _("team")

    kind = models.CharField(_("kind"), max_length=20, choices=Kind.choices)
    key = models.CharField(
        _("key"), max_length=40, unique=True, validators=[validate_key],
        help_text=_("Fixed once set. Tools know the unit by it, so renaming the unit never changes anyone's access."),
    )
    name = models.CharField(_("name"), max_length=100)
    description = models.TextField(_("description"), blank=True)
    parent = models.ForeignKey(
        "self", verbose_name=_("inside"), null=True, blank=True, on_delete=models.PROTECT, related_name="children",
        help_text=_("The unit this one sits under. It owns it, and appoints its head."),
    )
    # The keys from the root down, as /root/robotics/development/, so a subtree is one
    # query and the list sorts as a tree. Kept up to date by save().
    path = models.CharField(max_length=1000, editable=False, db_index=True)
    primary_colour = models.CharField(_("primary colour"), max_length=7, blank=True, validators=[colour])
    accent_colour = models.CharField(_("accent colour"), max_length=7, blank=True, validators=[colour])
    end_date = models.DateField(
        _("end date"), null=True, blank=True,
        help_text=_("For a project. It's shown, and nothing happens on the day: archive the project when it's over."),
    )
    archived_at = models.DateTimeField(_("archived"), null=True, blank=True)
    taking_part = models.ManyToManyField(
        "self", verbose_name=_("associations taking part"), symmetrical=False, blank=True, related_name="teams_taking_part",
        help_text=_("Other associations involved in this team, besides the one that owns it."),
    )
    run_for = models.ForeignKey(
        "self", verbose_name=_("run for"), null=True, blank=True, on_delete=models.PROTECT, related_name="run_by",
        help_text=_("Another association this service is run for, such as one association running another's website."),
    )

    objects = UnitQuerySet.as_manager()

    class Meta:
        verbose_name = _("unit")
        verbose_name_plural = _("units")
        ordering = ["path"]
        constraints = [
            models.CheckConstraint(condition=models.Q(kind__in=["association", "team"]), name="unit_kind_is_known"),
            models.CheckConstraint(
                condition=models.Q(key__regex=KEY) & ~models.Q(key__contains="--"),
                name="unit_key_is_valid",
            ),
            # Exactly one root: at most one unit with no parent, and it's an association.
            models.UniqueConstraint(fields=["kind"], condition=models.Q(parent__isnull=True), name="one_root"),
            models.CheckConstraint(condition=models.Q(parent__isnull=False) | models.Q(kind="association"), name="root_is_association"),
            models.CheckConstraint(
                condition=models.Q(kind="team") | models.Q(end_date__isnull=True, archived_at__isnull=True, run_for__isnull=True),
                name="only_teams_end_or_serve",
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def archived(self):
        return self.archived_at is not None

    @property
    def is_root(self):
        return self.parent_id is None

    def ancestors(self):
        """From the root down to the unit above this one."""
        return Unit.objects.filter(path__in=path_and_above(self.path)[:-1]).order_by("path")

    def descendants(self):
        return Unit.objects.filter(path__startswith=self.path).exclude(pk=self.pk)

    def association(self):
        """The association this unit is, or else the one it sits in, which owns it."""
        if self.kind == Unit.Kind.ASSOCIATION:
            return self
        return self.ancestors().filter(kind=Unit.Kind.ASSOCIATION).last()

    # ---- Rules -------------------------------------------------------------------

    def clean(self):
        super().clean()
        self.check_rules()

    def check_rules(self):
        """Everything in structure.md §1 that one unit's fields can break. Raises a
        ValidationError with every problem found."""
        problems = []
        stored = Unit.objects.filter(pk=self.pk).first() if self.pk else None
        Kind = Unit.Kind

        if stored and stored.key != self.key:
            problems.append(_("A unit's key can't change once set."))
        elif Unit.objects.filter(key=self.key).exclude(pk=self.pk).exists():
            problems.append(_("Another unit already has the key %(key)s. Keys are never reused, even after a unit is archived.") % {"key": self.key})

        parent = self.parent
        if parent is None:
            if self.kind != Kind.ASSOCIATION:
                problems.append(_("The root, the unit with nothing above it, is an association."))
            if stored and stored.parent_id is not None or Unit.objects.filter(parent=None).exclude(pk=self.pk).exists():
                problems.append(_("Every unit but the root sits inside another."))
        else:
            if stored and stored.parent_id is None:
                problems.append(_("The root can't be moved."))
            if self.kind == Kind.ASSOCIATION and parent.kind != Kind.ASSOCIATION:
                problems.append(_("An association can only sit inside an association, not inside a team."))
            if stored and parent.path.startswith(stored.path):
                problems.append(_("A unit can't sit inside itself or a unit under it."))
            if parent.archived and not self.archived:
                problems.append(_("%(parent)s is archived, so nothing can be added or moved into it, or taken out of the archive under it.") % {"parent": parent})

        if stored and stored.kind != self.kind and stored.deletion_problem_from_ties():
            problems.append(_("People are or were tied to %(unit)s, so it can't change between association and team.") % {"unit": stored})

        if self.kind == Kind.ASSOCIATION:
            if self.end_date or self.archived_at:
                problems.append(_("Only teams have an end date or are archived."))
            if self.run_for_id:
                problems.append(_("Only a team can be run for another association."))
            if self.pk and self.taking_part.exists():
                problems.append(_("Only a team can involve other associations."))
        elif stored and stored.kind == Kind.ASSOCIATION:
            if stored.children.filter(kind=Kind.ASSOCIATION).exists():
                problems.append(_("This association has associations inside it, so it can't become a team."))
            if stored.run_by.exists() or stored.teams_taking_part.exists():
                problems.append(_("Teams are run for this association, or involve it, so it can't become a team."))

        if self.run_for_id:
            target = self.run_for
            if target.kind != Kind.ASSOCIATION:
                problems.append(_("A service is run for an association, not a team."))
            elif parent is not None and parent.path.startswith(target.path):
                problems.append(_("A service is run for another association, not its own or one it sits inside."))

        if stored and parent is not None and stored.parent_id != parent.pk:
            problems += self._problems_after_moving(stored, parent.path + self.key + "/")

        if problems:
            raise ValidationError(problems)

    def _problems_after_moving(self, stored, new_path):
        """Moving a unit moves everything under it, so a team in there can end up inside
        the association it's run for, or one taking part in it."""
        problems = []
        moved = Unit.objects.filter(path__startswith=stored.path).filter(models.Q(run_for__isnull=False) | models.Q(taking_part__isnull=False)).distinct()
        for team in moved.select_related("run_for").prefetch_related("taking_part"):
            team_path = new_path + team.path[len(stored.path):]
            targets = ([team.run_for] if team.run_for_id and team.pk != self.pk else []) + list(team.taking_part.all())
            for target in targets:
                if team_path.startswith(target.path):
                    problems.append(_("After the move, %(team)s would sit inside %(target)s, which it's run for or which takes part in it.") % {"team": team, "target": target})
        return problems

    @staticmethod
    def check_taking_part(team_kind, parent, associations):
        """For the associations taking part in a team: see the m2m_changed handler below."""
        if not associations:
            return
        if team_kind != Unit.Kind.TEAM:
            raise ValidationError(_("Only a team can involve other associations."))
        for unit in associations:
            if unit.kind != Unit.Kind.ASSOCIATION:
                raise ValidationError(_("%(unit)s is a team. Only associations take part in another's team.") % {"unit": unit})
            if parent is not None and parent.path.startswith(unit.path):
                raise ValidationError(_("%(unit)s owns this team, or sits above it, so it isn't listed as taking part.") % {"unit": unit})

    def save(self, *args, **kwargs):
        self.clean_fields(exclude=["path"])
        with transaction.atomic():
            self.check_rules()
            before = Unit.objects.filter(pk=self.pk).values_list("path", flat=True).first() if self.pk else None
            self.path = (self.parent.path if self.parent else "/") + self.key + "/"
            super().save(*args, **kwargs)
            if before and before != self.path:  # it moved, so everything under it did too
                below = list(Unit.objects.filter(path__startswith=before).exclude(pk=self.pk))
                for unit in below:
                    unit.path = self.path + unit.path[len(before):]
                Unit.objects.bulk_update(below, ["path"])
                problems = self.membership_problems() + SeatRule.problems_after_moving()
                if problems:  # rolls the move back
                    raise ValidationError(problems)
                from .access import in_units, tell_tools
                from .seats import after_tree_changed

                after_tree_changed(SeatRule.objects.filter(each_association_under=True))  # associations moved in or out of reach
                # What they're members of, and where a tool puts their teams, can change.
                tell_tools(in_units(Unit.objects.filter(path__startswith=self.path)))

    # ---- Archiving and deleting --------------------------------------------------

    def archive(self):
        """Archives this team and everything under it. Ones already archived keep their date."""
        if self.kind != Unit.Kind.TEAM:
            raise ValidationError(_("Only teams are archived. %(unit)s is an association.") % {"unit": self})
        from .seats import after_tree_changed

        from .access import in_units, tell_tools

        now = timezone.now()
        with transaction.atomic():
            Unit.objects.filter(path__startswith=self.path, archived_at__isnull=True).update(archived_at=now)
            self.archived_at = self.archived_at or now
            after_tree_changed(SeatRule.objects.filter(held_in__path__startswith=self.path))  # a position in an archived team gives no seat
            tell_tools(in_units(Unit.objects.filter(path__startswith=self.path)))  # their keys from here end now

    def unarchive(self):
        """Brings back this team alone. The ones under it stay archived until brought back too."""
        from .access import in_units, tell_tools
        from .seats import after_tree_changed

        if self.parent and self.parent.archived:
            raise ValidationError(_("%(parent)s is archived, so take it out of the archive first.") % {"parent": self.parent})
        with transaction.atomic():
            Unit.objects.filter(pk=self.pk).update(archived_at=None)
            self.archived_at = None
            after_tree_changed(SeatRule.objects.filter(models.Q(held_in=self) | models.Q(unit=self)))
            tell_tools(in_units(Unit.objects.filter(pk=self.pk)))

    def deletion_problem(self):
        """Why this unit can't be deleted, or None. A unit is deleted only if nothing
        ever pointed at it. Otherwise it's archived, so its history is kept."""
        if self.is_root:
            return _("The root can't be deleted.")
        if self.children.exists():
            return _("%(unit)s has units inside it.") % {"unit": self}
        if self.run_by.exists() or self.teams_taking_part.exists():
            return _("Teams are run for %(unit)s, or involve it.") % {"unit": self}
        return self.deletion_problem_from_ties()

    def deletion_problem_from_ties(self):
        if self.memberships.exists() or self.positions.exists() or self.reasons.exists():
            return _("People are or were tied to %(unit)s, and that record is kept.") % {"unit": self}
        if self.grants.exists() or self.grants_to_roles.exists():
            return _("A grant covers %(unit)s, or goes to a role in it, now or before, and that record is kept.") % {"unit": self}
        if self.seat_rules.exists() or self.seat_rules_from.exists():
            return _("A seat rule gives seats in %(unit)s, or comes from a role in it, now or before, and that record is kept.") % {"unit": self}
        return None

    def membership_problems(self, below=True):
        """For the current board roles and leads here, and with `below` in every unit
        under it: any whose holder isn't the member the role needs, as after a move."""
        units = Unit.objects.filter(path__startswith=self.path) if below else Unit.objects.filter(pk=self.pk)
        held = Position.objects.current().filter(unit__in=units, role__in=Position.NEEDS_MEMBERSHIP).select_related("person", "unit")
        return [problem for position in held if (problem := position.membership_problem(on=_today()))]

    def check_can_delete(self):
        problem = self.deletion_problem()
        if problem:
            raise ValidationError(problem)

    def check_leads_without(self, removed):
        """Before associations are taken off this team's list: a lead who is a member only
        of one of them would be left a non-member (structure.md §3)."""
        today = _today()
        remaining = [association for association in self.taking_part.all() if association.pk not in removed]
        needed = [self.association(), *remaining]
        leads = Tie.overlapping(Position.objects.filter(unit=self, role=Position.Role.LEAD), today, None).select_related("person")
        for position in leads:
            if not any(position.person.is_member(association, on=max(position.start, today)) for association in needed):
                raise ValidationError(_("%(person)s leads %(team)s as a member of an association taken off its list. Hand over the lead first.") % {"person": position.person, "team": self})

    def delete(self, *args, **kwargs):
        self.check_can_delete()
        return super().delete(*args, **kwargs)


@receiver(m2m_changed, sender=Unit.taking_part.through)
def _check_taking_part(sender, instance, action, reverse, pk_set, **kwargs):
    if action == "pre_add":
        added = Unit.objects.filter(pk__in=pk_set)
        if reverse:  # an association being added to teams
            for team in added:
                Unit.check_taking_part(team.kind, team.parent, [instance])
        else:
            Unit.check_taking_part(instance.kind, instance.parent, added)
    elif action in ("pre_remove", "pre_clear"):
        if reverse:  # an association taken off teams' lists
            teams = Unit.objects.filter(pk__in=pk_set) if action == "pre_remove" else instance.teams_taking_part.all()
            for team in teams:
                team.check_leads_without({instance.pk})
        else:
            instance.check_leads_without(set(pk_set) if action == "pre_remove" else set(instance.taking_part.values_list("pk", flat=True)))


def ensure_root(using="default", apps=None, **kwargs):
    """Create the root from the config, which kit passes on in root.json, or keep its
    name in step. Runs after every migrate. Stops core from starting if the config's key
    differs from the root's, as a key never changes."""
    config = settings.SALLSKAP_ROOT
    if not config:
        return
    if apps is not None:
        try:
            apps.get_model("structure", "Unit")
        except LookupError:  # migrated back to before units existed
            return
    try:
        logged = apps is None or apps.get_model("structure", "AuditEntry")
    except LookupError:  # migrated back to before the audit log
        logged = False
    name, key = config["name"], config["key"]
    try:
        validate_key(key)
    except ValidationError as error:
        raise ImproperlyConfigured(f"In the config, under [association]: {error.messages[0]}") from None
    units = Unit.objects.using(using)
    root = units.filter(parent=None).first()
    if root is None:
        if units.filter(key=key).exists():
            raise ImproperlyConfigured(f"The config's key {key!r} under [association] is already another unit's. Choose another.")
        with transaction.atomic(using=using):
            root = Unit(kind=Unit.Kind.ASSOCIATION, key=key, name=name)
            root.save(using=using)
            if logged:
                AuditEntry.record("create_root", AuditEntry.Via.SERVER, unit=root, obj=root, changes={"key": [None, key], "name": [None, name]}, using=using)
    elif root.key != key:
        raise ImproperlyConfigured(
            f"The config says key = {key!r} under [association], but the association's key is {root.key!r}, "
            f"and a key can't change: tools know it by it. Put key = {root.key!r} back in the config."
        )
    elif root.name != name:
        with transaction.atomic(using=using):
            before, root.name = root.name, name
            root.save(using=using)
            if logged:
                AuditEntry.record("rename_root", AuditEntry.Via.SERVER, unit=root, obj=root, changes={"name": [before, name]}, using=using)


# ---- People and ties: structure.md §2 and §3 ----------------------------------------


def _today():
    return timezone.localdate()


# How someone deleted with nothing left is shown where a person was (structure.md §3).
SINCE_DELETED = _("someone since deleted")

# Set while seats.py saves a seat: Position.save refuses one saved from anywhere else.
_seating = ContextVar("seating", default=False)


def _erase(queryset):
    """Removes rows that are otherwise never deleted: a duplicate left by a merge, the
    memberships and reasons of someone deleted with nothing left (deleting.py), or a
    made-up installation being replaced. Nothing else may call this."""
    return models.QuerySet.delete(queryset)


class Person(models.Model):
    """Everyone the association holds data about. Kept to a minimum: a name and email
    addresses. Everything else is a tie to a unit."""

    name = models.CharField(_("name"), max_length=150, blank=True)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, verbose_name=_("account"), null=True, blank=True, on_delete=models.SET_NULL, related_name="person",
        help_text=_("Optional. People who only sign up for events or subscribe never need one."),
    )
    created_at = models.DateTimeField(_("added"), default=timezone.now, editable=False)

    class Meta:
        verbose_name = _("person")
        verbose_name_plural = _("people")
        ordering = ["name", "pk"]

    def __str__(self):
        if self.name:
            return self.name
        address = self.emails.order_by("verified_at").first() if self.pk else None
        return address.address if address else _("Person %(id)s") % {"id": self.pk}

    def save(self, *args, **kwargs):
        stored = Person.objects.filter(pk=self.pk).values("user").first() if self.pk else None
        super().save(*args, **kwargs)
        user = self.user
        if user is not None and user.name != self.name:  # the tools read the account's name
            user.name = self.name
            user.save(update_fields=["name"])
        if stored is not None and stored["user"] != self.user_id:  # ties moved to or from an account
            from accounts import sync
            from django.contrib.auth import get_user_model

            for account in get_user_model().objects.filter(pk__in=[stored["user"], self.user_id]):
                sync.request_for(account)

    def deletion_problem(self):
        if self.user_id:
            return _("%(person)s has an account.") % {"person": self}
        if self.memberships.exists() or self.positions.exists() or self.reasons.exists() or self.grants_given.exists() or self.seat_rules_made.exists():
            return _("%(person)s is or was tied to a unit, and that record is kept.") % {"person": self}
        return None

    def delete(self, *args, **kwargs):
        problem = self.deletion_problem()
        if problem:
            raise ValidationError(problem)
        return super().delete(*args, **kwargs)

    # ---- What they're a member of ----------------------------------------------------

    def is_member(self, association, on=None, excluding=None):
        """A member of `association` on that day, directly or through an association
        inside it: membership counts upwards."""
        memberships = self.memberships.current(on).filter(association__path__startswith=association.path)
        if excluding is not None:
            memberships = memberships.exclude(pk=excluding)
        return memberships.exists()

    def member_of(self, on=None):
        """Every association they're a member of, counting upwards. Worked out from the
        tree each time, never stored, so a moved unit can't leave it wrong."""
        paths = set()
        for path in self.memberships.current(on).values_list("association__path", flat=True):
            paths.update(path_and_above(path))
        return Unit.objects.filter(path__in=paths).order_by("path")

    # ---- Merging -------------------------------------------------------------------

    @staticmethod
    @transaction.atomic
    def merge(first, second, via=None, by=None):
        """Makes two rows one person, and returns the one kept: the one with an account,
        or else `first`. Two people with accounts are never merged, as one of their subs
        would vanish. Logged as core's own change, or as `by`'s, an operator's account,
        through `via`."""
        if first.pk == second.pk:
            return first
        if first.user_id and second.user_id:
            raise ValidationError(_("%(a)s and %(b)s both have an account, so they can't be merged.") % {"a": first, "b": second})
        kept, gone = (second, first) if second.user_id else (first, second)

        for email in gone.emails.all():
            twin = kept.emails.filter(address=email.address).first()
            if twin is None:
                EmailAddress.objects.filter(pk=email.pk).update(person=kept)
                continue
            verified_at = twin.verified_at or email.verified_at
            EmailAddress.objects.filter(pk=email.pk).delete()  # before the twin, as a verified address is unique
            EmailAddress.objects.filter(pk=twin.pk).update(verified_at=verified_at)

        for membership in gone.memberships.all():
            twins = list(Membership.overlapping(kept.memberships.filter(association=membership.association_id), membership.start, membership.end))
            if twins:
                rows = [membership, *twins]
                membership.start = min(row.start for row in rows)
                membership.end = None if any(row.end is None for row in rows) else max(row.end for row in rows)
                _erase(Membership.objects.filter(pk__in=[row.pk for row in twins]))
            Membership.objects.filter(pk=membership.pk).update(person=kept, start=membership.start, end=membership.end)
        Position.objects.filter(person=gone).update(person=kept)
        Reason.objects.filter(person=gone).update(person=kept)
        Grant.objects.filter(given_by=gone).update(given_by=kept)
        SeatRule.objects.filter(made_by=gone).update(made_by=kept)

        if gone.user_id:
            user = gone.user
            Person.objects.filter(pk=gone.pk).update(user=None)
            kept.user = user
            kept.name = user.name or kept.name
        kept.name = kept.name or gone.name
        kept.save()
        _erase(Person.objects.filter(pk=gone.pk))
        actor = AuditEntry.by(by) if by is not None else {}
        AuditEntry.record("merge", via or AuditEntry.Via.CORE, obj=kept, changes={"person": [gone.pk, kept.pk]}, **actor)
        from .access import tell_tools

        tell_tools(Person.objects.filter(pk=kept.pk))  # their ties moved over
        return kept


def normalize_address(address):
    return (address or "").strip().lower()


class EmailAddress(models.Model):
    """A person's address. Verified means the person has shown it's theirs, or an
    operator typed it in for an account. A verified address belongs to one person."""

    person = models.ForeignKey(Person, verbose_name=_("person"), on_delete=models.CASCADE, related_name="emails")
    address = models.EmailField(_("email address"))
    verified_at = models.DateTimeField(_("verified"), null=True, blank=True)

    class Meta:
        verbose_name = _("email address")
        verbose_name_plural = _("email addresses")
        ordering = ["address"]
        constraints = [
            models.UniqueConstraint(fields=["person", "address"], name="one_of_each_address"),
            models.UniqueConstraint(fields=["address"], condition=models.Q(verified_at__isnull=False), name="verified_address_is_one_persons"),
            models.CheckConstraint(condition=models.Q(address=Lower("address")), name="address_is_lowercase"),
        ]

    def __str__(self):
        return self.address

    def clean(self):
        self.address = normalize_address(self.address)
        super().clean()
        self.check_rules()

    def check_rules(self):
        stored = EmailAddress.objects.filter(pk=self.pk).first() if self.pk else None
        if stored and (stored.address != self.address or stored.person_id != self.person_id):
            raise ValidationError(_("An address can't be changed: add the new one and remove the old."))
        if self.verified_at and EmailAddress.objects.filter(address=self.address, verified_at__isnull=False).exclude(pk=self.pk).exists():
            raise ValidationError(_("%(address)s is already someone's verified address.") % {"address": self.address})

    def save(self, *args, **kwargs):
        self.address = normalize_address(self.address)
        self.check_rules()
        super().save(*args, **kwargs)

    def is_login_address(self):
        user = self.person.user if self.person_id else None
        return user is not None and user.email == self.address

    def delete(self, *args, **kwargs):
        if self.is_login_address():
            raise ValidationError(_("%(address)s is how they log in. Change the account's address first.") % {"address": self.address})
        return super().delete(*args, **kwargs)

    @transaction.atomic
    def verify(self):
        """Marks it verified. If someone else already holds it verified, the two become
        one person, as structure.md §2 says. Returns the person it ends up on."""
        holder = EmailAddress.objects.filter(address=self.address, verified_at__isnull=False).exclude(person=self.person_id).first()
        person = self.person
        if holder is not None:
            person = Person.merge(holder.person, person)
        address = person.emails.get(address=self.address)
        if address.verified_at is None:
            address.verified_at = timezone.now()
            address.save()
        return person


def account_address_problem(user):
    """Why an account can't have this login address, or None. It's someone else's
    verified address, and they have an account of their own."""
    address = normalize_address(user.email)
    holder = EmailAddress.objects.filter(address=address, verified_at__isnull=False).select_related("person__user").first()
    other = holder.person.user if holder else None
    # Another account's own login address is left to the check that addresses are unique.
    if other is not None and other.pk != user.pk and other.email != address:
        return _("%(address)s is a verified address of someone with another account.") % {"address": address}
    return None


@transaction.atomic
def link_account(user, old_address=None):
    """Gives an account its person, and its login address verified there. An account
    for an address a person without one already holds verified joins that person. Runs
    after an account is saved with a new address or name: see apps.py, which also
    checks account_address_problem before the account is written. An old login address
    stays on the person, unverified: it was only vouched for as the login address, and
    it can go to someone else, such as a role's shared address."""
    person = Person.objects.filter(user=user).first()
    holder = EmailAddress.objects.filter(address=user.email, verified_at__isnull=False).select_related("person").first()
    if person is None:
        if holder is not None:  # someone already known: the account joins them
            person = holder.person
            person.user = user
            person.name = user.name or person.name
            person.save()
        else:
            person = Person(name=user.name, user=user)
            person.save()
    elif person.name != user.name:
        person.name = user.name
        Person.objects.filter(pk=person.pk).update(name=user.name)
    if holder is not None and holder.person_id != person.pk:
        person = Person.merge(person, holder.person)
    if old_address and old_address != user.email:
        person.emails.filter(address=old_address).update(verified_at=None)
    address, _created = EmailAddress.objects.get_or_create(person=person, address=user.email)
    if address.verified_at is None:
        address.verified_at = timezone.now()
        address.save()


class TieQuerySet(models.QuerySet):
    start_field, end_field = "start", "end"

    def current(self, on=None):
        on = on or _today()
        return self.filter(**{f"{self.start_field}__lte": on}).filter(
            models.Q(**{f"{self.end_field}__isnull": True}) | models.Q(**{f"{self.end_field}__gt": on})
        )

    def delete(self):
        raise ValidationError(_("Ties are never deleted, so the record is kept. End it instead."))


class ReasonQuerySet(TieQuerySet):
    end_field = "expires"


class Tie(models.Model):
    """What connects a person to a unit. Ending one sets its end and never deletes it.
    The end is the first day it no longer holds, so a tie ended today stops today."""

    start = models.DateField(_("start"), default=_today)
    end = models.DateField(_("end"), null=True, blank=True, help_text=_("The first day it no longer holds. Empty while it lasts."))

    objects = TieQuerySet.as_manager()
    end_field = "end"
    unit_field = None  # set by each kind of tie

    class Meta:
        abstract = True

    def is_current(self, on=None):
        on = on or _today()
        end = getattr(self, self.end_field)
        return self.start <= on and (end is None or end > on)

    def end_on(self, day=None):
        """Ends it on `day`, today if not given. An end already set only moves earlier, to
        put a mistake right: later would give back what it gave, over days already past."""
        day, end = day or _today(), getattr(self, self.end_field)
        if end is not None and day > end:
            raise ValidationError(_("It already ends on %(end)s, so it can only be ended earlier.") % {"end": end})
        setattr(self, self.end_field, day)
        self.save()

    def delete(self, *args, **kwargs):
        raise ValidationError(_("Ties are never deleted, so the record is kept. End it instead."))

    @staticmethod
    def overlapping(queryset, start, end):
        if end is not None:
            queryset = queryset.filter(start__lt=end)
        return queryset.filter(models.Q(end__isnull=True) | models.Q(end__gt=start))

    def identity(self):
        """Who it's for and where: fixed once set, and needed before the rules can be checked."""
        return ("person_id", f"{self.unit_field}_id")

    def clean(self):
        super().clean()
        if all(getattr(self, name) for name in self.identity()):  # a form shows a missing one by itself
            self.check_rules()

    def check_rules(self):
        problems = self.problems()
        if problems:
            raise ValidationError(problems)

    def problems(self):
        problems = []
        end = getattr(self, self.end_field)
        if end is not None and self.start and end < self.start:
            problems.append(_("It can't end before it starts."))
        stored = type(self).objects.filter(pk=self.pk).first() if self.pk else None
        if stored is not None:
            fixed = (*self.identity(), *self.fixed_fields)
            if any(getattr(stored, name) != getattr(self, name) for name in fixed):
                problems.append(_("Who it's for, where, and which role can't change once set: end it and add a new one."))
        return problems

    fixed_fields = ()

    def save(self, *args, **kwargs):
        self.clean_fields()
        with transaction.atomic():
            self.check_rules()
            self.before_save()
            super().save(*args, **kwargs)
            self.after_save()

    def before_save(self):
        pass

    def after_save(self):
        pass


class Membership(Tie):
    class Kind(models.TextChoices):
        ORDINARY = "ordinary", _("ordinary")
        HONORARY = "honorary", _("honorary")

    person = models.ForeignKey(Person, verbose_name=_("person"), on_delete=models.PROTECT, related_name="memberships")
    association = models.ForeignKey(Unit, verbose_name=_("association"), on_delete=models.PROTECT, related_name="memberships")
    kind = models.CharField(_("kind"), max_length=20, choices=Kind.choices, default=Kind.ORDINARY)

    unit_field = "association"

    class Meta:
        verbose_name = _("membership")
        verbose_name_plural = _("memberships")
        ordering = ["-start", "pk"]
        constraints = [
            models.CheckConstraint(condition=models.Q(end__isnull=True) | models.Q(end__gte=models.F("start")), name="membership_ends_after_start"),
            models.UniqueConstraint(fields=["person", "association"], condition=models.Q(end__isnull=True), name="one_open_membership"),
        ]

    def __str__(self):
        return f"{self.person}: {self.association}"

    def problems(self):
        problems = super().problems()
        if self.association.kind != Unit.Kind.ASSOCIATION:
            problems.append(_("%(unit)s is a team. Membership is only of associations: in teams, people hold positions.") % {"unit": self.association})
        if self.start and Tie.overlapping(Membership.objects.filter(person=self.person_id, association=self.association_id).exclude(pk=self.pk), self.start, self.end).exists():
            problems.append(_("%(person)s is already a member of %(unit)s at that time.") % {"person": self.person, "unit": self.association})
        if self.pk and self.end is not None:
            problems += self._positions_resting_on_it()
        return problems

    def after_save(self):
        from .access import tell_tools

        tell_tools(Person.objects.filter(pk=self.person_id))

    def _positions_resting_on_it(self):
        """A membership can't end while a board role or lead rests on it."""
        problems = []
        held = Position.objects.filter(person=self.person_id, role__in=Position.NEEDS_MEMBERSHIP).filter(models.Q(end__isnull=True) | models.Q(end__gt=self.end))
        for position in held.select_related("unit"):
            if position.membership_problem(on=max(self.end, position.start), excluding=self.pk):
                problems.append(_("%(person)s is %(role)s of %(unit)s, which needs this membership. End or hand over that position first.") % {"person": self.person, "role": position.get_role_display(), "unit": position.unit})
        return problems


class Position(Tie):
    class Role(models.TextChoices):
        # The keys never change. What people see is the label.
        CHAIR = "chair", _("chair")
        VICE_CHAIR = "vice_chair", _("vice chair")
        TREASURER = "treasurer", _("treasurer")
        SECRETARY = "secretary", _("secretary")
        BOARD_MEMBER = "board_member", _("board member")
        LEAD = "lead", _("lead")
        PARTICIPANT = "participant", _("participant")

    BOARD_ROLES = (Role.CHAIR, Role.VICE_CHAIR, Role.TREASURER, Role.SECRETARY, Role.BOARD_MEMBER)
    TEAM_ROLES = (Role.LEAD, Role.PARTICIPANT)
    NEEDS_MEMBERSHIP = (*BOARD_ROLES, Role.LEAD)

    # Empty once its holder is deleted, with nothing left (structure.md §3): their name is
    # then kept here, as the record of who held what, and nothing else of theirs.
    person = models.ForeignKey(Person, verbose_name=_("person"), null=True, blank=True, on_delete=models.PROTECT, related_name="positions")
    kept_name = models.CharField(_("held by, since deleted"), max_length=150, blank=True, editable=False)
    unit = models.ForeignKey(Unit, verbose_name=_("unit"), on_delete=models.PROTECT, related_name="positions")
    role = models.CharField(_("role"), max_length=20, choices=Role.choices)
    title = models.CharField(_("title"), max_length=100, blank=True, help_text=_("Shown beside the role, such as Head of PR. It gives nothing beyond the role."))
    appointed_by = models.CharField(_("appointed by"), max_length=200, blank=True, help_text=_("Such as Annual meeting 2026, or the board."))
    minutes = models.CharField(_("minutes"), max_length=500, blank=True, help_text=_("Where the decision is recorded."))
    # A seat (structure.md §4): core sets both, and keeps its dates in step. Empty for a
    # position someone appointed.
    seat_rule = models.ForeignKey("SeatRule", verbose_name=_("seat rule"), null=True, blank=True, editable=False, on_delete=models.PROTECT, related_name="seats")
    seat_from = models.ForeignKey("self", verbose_name=_("comes from"), null=True, blank=True, editable=False, on_delete=models.PROTECT, related_name="seats_given")
    # The day a move or bringing a team back put the position it comes from in the rule's
    # reach, and the day a move or archiving put it out, if either did: see seats.py.
    seat_since = models.DateField(_("in reach from"), null=True, blank=True, editable=False)
    seat_until = models.DateField(_("out of reach from"), null=True, blank=True, editable=False)

    unit_field = "unit"
    fixed_fields = ("role", "seat_rule_id", "seat_from_id")

    class Meta:
        verbose_name = _("position")
        verbose_name_plural = _("positions")
        ordering = ["-start", "pk"]
        constraints = [
            models.CheckConstraint(condition=models.Q(end__isnull=True) | models.Q(end__gte=models.F("start")), name="position_ends_after_start"),
            models.UniqueConstraint(fields=["unit"], condition=models.Q(role="chair", end__isnull=True), name="one_open_chair"),
            models.CheckConstraint(
                condition=models.Q(seat_rule__isnull=True, seat_from__isnull=True) | models.Q(seat_rule__isnull=False, seat_from__isnull=False),
                name="seat_has_rule_and_source",
            ),
            models.UniqueConstraint(fields=["seat_rule", "seat_from"], condition=models.Q(seat_until__isnull=True), name="one_open_seat_per_rule_and_source"),
            # Only the ended positions of someone deleted are kept without them.
            models.CheckConstraint(condition=models.Q(person__isnull=False) | models.Q(end__isnull=False), name="kept_position_has_ended"),
        ]

    # What can't change on a position kept after its holder was deleted.
    KEPT = ("start", "end", "title", "appointed_by", "minutes", "kept_name")

    def __str__(self):
        return f"{self.holder()}: {self.get_role_display()}, {self.unit}"

    def holder(self):
        """Who holds it, or held it: once they're deleted, the name kept on it."""
        if self.person_id is not None:
            return str(self.person)
        return self.kept_name or str(SINCE_DELETED)

    def membership_problem(self, on, excluding=None):
        """Why this person can't hold this role here on that day, as a non-member, or None.
        A board role needs a membership of the association, and a lead one of the
        association owning the team or of one taking part in it."""
        if self.role in self.BOARD_ROLES:
            needed = [self.unit]
        elif self.role == self.Role.LEAD:
            needed = [self.unit.association(), *self.unit.taking_part.all()]
        else:
            return None
        if any(self.person.is_member(association, on=on, excluding=excluding) for association in needed):
            return None
        names = ", ".join(str(association) for association in needed)
        return _("Only a member can be %(role)s here: %(person)s isn't a member of %(names)s on %(day)s.") % {"role": self.get_role_display(), "person": self.person, "names": names, "day": on}

    @classmethod
    def role_problem(cls, role, unit):
        """Why this role can't sit in this unit, or None: for positions and grants alike."""
        if role in cls.BOARD_ROLES and unit.kind != Unit.Kind.ASSOCIATION:
            return _("A board role sits in an association. %(unit)s is a team.") % {"unit": unit}
        if role in cls.TEAM_ROLES and unit.kind != Unit.Kind.TEAM:
            return _("A lead or participant sits in a team. %(unit)s is an association.") % {"unit": unit}
        return None

    def problems(self):
        stored = Position.objects.filter(pk=self.pk).first() if self.pk else None
        if stored is None and self.person_id is None:
            return [_("Say who holds the position.")]
        if stored is not None and stored.person_id is None and not _seating.get():
            if any(getattr(stored, name) != getattr(self, name) for name in self.KEPT):
                return [_("This position is kept as the record of someone since deleted, so it can't change.")]
        problems = super().problems()
        unit = self.unit
        if problem := self.role_problem(self.role, unit):
            problems.append(problem)
        self._dates_changed = stored is None or (stored.start, stored.end) != (self.start, self.end)  # for after_save
        if stored is None and unit.archived:
            problems.append(_("%(unit)s is archived, so nobody is added to it.") % {"unit": unit})
        if not problems and self.start and (stored is None or stored.start != self.start):
            problem = self.membership_problem(on=self.start)
            if problem:
                problems.append(problem)
        if self.role == self.Role.CHAIR and self.start:
            others = Tie.overlapping(Position.objects.filter(unit=self.unit_id, role=self.Role.CHAIR).exclude(pk=self.pk), self.start, self.end)
            if stored is None:  # a new chair ends an open one that started before
                others = others.exclude(start__lt=self.start, end__isnull=True)
            if others.exists():
                problems.append(_("%(unit)s has one chair at a time, and %(other)s is chair then.") % {"unit": unit, "other": others.first().holder()})
        return problems

    def is_seat(self):
        return self.seat_rule_id is not None

    def before_save(self):
        if self.is_seat() and not _seating.get():  # and problems() keeps a seat a seat
            raise ValidationError(_("%(seat)s is a seat, from a seat rule, so core alone sets, changes and ends it. Change the rule, or the position it comes from.") % {"seat": self})
        for chair in self.chairs_it_ends():
            chair.end_on(self.start)

    def chairs_it_ends(self):
        """Appointing the next chair ends the last one's position on the day the new one
        starts: the open ones that started before, for a new chair, and none otherwise."""
        if self.role != self.Role.CHAIR or self.pk is not None:
            return Position.objects.none()
        return Position.objects.filter(unit=self.unit_id, role=self.Role.CHAIR, start__lt=self.start, end__isnull=True)

    def after_save(self):
        from .access import tell_tools

        tell_tools(Person.objects.filter(pk=self.person_id))  # seats too: they give keys like any position
        if not self.is_seat():
            from .seats import after_position_saved

            after_position_saved(self, dates_changed=getattr(self, "_dates_changed", True))


class Reason(Tie):
    """Why data is held on someone with no other tie. It always expires."""

    class Kind(models.TextChoices):
        APPLICANT = "applicant", _("applicant")
        ALUMNUS = "alumnus", _("alumnus")
        PARTNER_CONTACT = "partner_contact", _("partner contact")
        CONTACT_FORM = "contact_form", _("contact-form sender")

    class Basis(models.TextChoices):  # GDPR art. 6
        CONSENT = "consent", _("consent")
        CONTRACT = "contract", _("contract")
        LEGITIMATE_INTEREST = "legitimate_interest", _("legitimate interest")
        LEGAL_OBLIGATION = "legal_obligation", _("legal obligation")

    end = None
    person = models.ForeignKey(Person, verbose_name=_("person"), on_delete=models.PROTECT, related_name="reasons")
    kind = models.CharField(_("kind"), max_length=20, choices=Kind.choices)
    unit = models.ForeignKey(Unit, verbose_name=_("unit"), on_delete=models.PROTECT, related_name="reasons")
    source = models.CharField(_("source"), max_length=200, blank=True, help_text=_("Where it came from, such as the contact form or the alumni offer."))
    legal_basis = models.CharField(_("legal basis"), max_length=20, choices=Basis.choices)
    expires = models.DateField(_("expires"), help_text=_("The first day it no longer holds. Ending it sets this to today."))

    objects = ReasonQuerySet.as_manager()
    end_field = "expires"
    unit_field = "unit"
    fixed_fields = ("kind",)

    class Meta:
        verbose_name = _("reason")
        verbose_name_plural = _("reasons")
        ordering = ["-start", "pk"]
        constraints = [
            models.CheckConstraint(condition=models.Q(expires__gte=models.F("start")), name="reason_expires_after_start"),
        ]

    def __str__(self):
        return f"{self.person}: {self.get_kind_display()}, {self.unit}"


# ---- Authority: structure.md §4 --------------------------------------------------------


class Permission(models.TextChoices):
    """What can() is asked about: see authority.py. The keys never change."""

    SEE_MEMBERS = "see_members", _("see members")
    MANAGE_MEMBERSHIPS = "manage_memberships", _("manage memberships")
    MANAGE_EVENTS = "manage_events", _("manage events")
    SEND_MAILINGS = "send_mailings", _("send mailings")
    MANAGE_DOCUMENTS = "manage_documents", _("manage documents")
    MANAGE_FINANCES = "manage_finances", _("fees and treasury")
    # These can't be given away, so authority over people and units stays with heads.
    MANAGE_PARTICIPANTS = "manage_participants", _("manage participants")
    APPOINT = "appoint", _("appoint")
    MANAGE_UNITS = "manage_units", _("manage units")
    GIVE_GRANTS = "give_grants", _("give grants")
    END_SERVICE = "end_service", _("end a service")


GRANTABLE = (
    Permission.SEE_MEMBERS, Permission.MANAGE_MEMBERSHIPS, Permission.MANAGE_EVENTS,
    Permission.SEND_MAILINGS, Permission.MANAGE_DOCUMENTS, Permission.MANAGE_FINANCES,
)


class Grant(Tie):
    """One permission given to a role in a unit, such as the root's treasurer, over one
    unit or it and everything inside it. Whoever holds the role has it, so it passes to
    the next holder. Dated like a tie, ended and never deleted."""

    permission = models.CharField(_("permission"), max_length=30, choices=[(p.value, p.label) for p in GRANTABLE])
    role = models.CharField(_("to the role"), max_length=20, choices=Position.Role.choices)
    held_in = models.ForeignKey(
        Unit, verbose_name=_("held in"), on_delete=models.PROTECT, related_name="grants_to_roles",
        help_text=_("Where the role is held, such as the association whose treasurer it's for."),
    )
    unit = models.ForeignKey(Unit, verbose_name=_("covers"), on_delete=models.PROTECT, related_name="grants")
    include_below = models.BooleanField(_("and everything inside it"), default=False)
    # Empty once the giver is deleted (structure.md §3). The grant stays as it was.
    given_by = models.ForeignKey(
        Person, verbose_name=_("given by"), null=True, blank=True, on_delete=models.PROTECT, related_name="grants_given",
        help_text=_("A chair or vice chair with authority over the unit it covers, on the day it starts."),
    )

    fixed_fields = ("permission", "role", "include_below")

    class Meta:
        verbose_name = _("grant")
        verbose_name_plural = _("grants")
        ordering = ["-start", "pk"]
        constraints = [
            models.CheckConstraint(condition=models.Q(end__isnull=True) | models.Q(end__gte=models.F("start")), name="grant_ends_after_start"),
            models.CheckConstraint(condition=models.Q(permission__in=[p.value for p in GRANTABLE]), name="grant_only_what_can_be_given"),
        ]

    def __str__(self):
        where = _("%(unit)s and everything inside it") if self.include_below else "%(unit)s"
        return _("%(role)s of %(held_in)s: %(permission)s in %(where)s") % {
            "role": self.get_role_display(), "held_in": self.held_in, "permission": self.get_permission_display(),
            "where": where % {"unit": self.unit},
        }

    def identity(self):
        return ("held_in_id", "unit_id", "given_by_id")

    def problems(self):
        from .authority import ONLY_IN, can

        problems = super().problems()
        held_in = self.held_in
        if problem := Position.role_problem(self.role, held_in):
            problems.append(problem)
        stored = Grant.objects.filter(pk=self.pk).first() if self.pk else None
        if stored is None and self.given_by_id is None:
            problems.append(_("Say who gives the grant."))
        if stored is None:
            for unit in {held_in, self.unit}:
                if unit.archived:
                    problems.append(_("%(unit)s is archived, so nothing new is given in it.") % {"unit": unit})
            covered = Unit.objects.filter(path__startswith=self.unit.path) if self.include_below else Unit.objects.filter(pk=self.unit_id)
            if self.permission in ONLY_IN and not covered.filter(ONLY_IN[self.permission]).exists():
                problems.append(_("%(permission)s means nothing in any unit this grant covers, so it would give nothing. Memberships, for one, are only of associations.") % {"permission": self.get_permission_display()})
        if not problems and self.start and (stored is None or stored.start != self.start):
            if not can(self.given_by, Permission.GIVE_GRANTS, self.unit, on=self.start):
                problems.append(_("%(person)s couldn't give grants in %(unit)s on %(day)s: only a chair or vice chair with authority there can.") % {"person": self.given_by, "unit": self.unit, "day": self.start})
        return problems


# ---- Seats that come with another position: structure.md §4 ------------------------------

# Any role a unit can have, but chair: a unit has one at a time, and a rule can seat several.
SEAT_ROLES = [(value, label) for value, label in Position.Role.choices if value != Position.Role.CHAIR]


class SeatRule(Tie):
    """Whoever holds one of some roles, in one unit or in each association directly under
    it, has a seat in this unit, for as long as they hold it: such as the chair of each
    association under the root on the root's board. Core makes, changes and ends the
    seats, which are positions: see seats.py. Dated like a tie, ended and never deleted."""

    unit = models.ForeignKey(Unit, verbose_name=_("seats in"), on_delete=models.PROTECT, related_name="seat_rules")
    role = models.CharField(_("as"), max_length=20, choices=SEAT_ROLES)
    from_roles = models.JSONField(_("for the roles"), default=list, help_text=_("Whoever holds one of these roles gets the seat."))
    held_in = models.ForeignKey(
        Unit, verbose_name=_("held in"), on_delete=models.PROTECT, related_name="seat_rules_from",
        help_text=_("Where those roles are held, such as the association whose vice chair it's for."),
    )
    each_association_under = models.BooleanField(
        _("in each association directly under it"), default=False,
        help_text=_("Such as the chair of each association directly under the root, rather than the root's own."),
    )
    # Empty once its maker is deleted (structure.md §3). The rule stays as it was.
    made_by = models.ForeignKey(
        Person, verbose_name=_("made by"), null=True, blank=True, on_delete=models.PROTECT, related_name="seat_rules_made",
        help_text=_("Someone who could appoint in the unit with the seats, on the day it starts."),
    )

    fixed_fields = ("role", "from_roles", "each_association_under")

    class Meta:
        verbose_name = _("seat rule")
        verbose_name_plural = _("seat rules")
        ordering = ["-start", "pk"]
        constraints = [
            models.CheckConstraint(condition=models.Q(end__isnull=True) | models.Q(end__gte=models.F("start")), name="seat_rule_ends_after_start"),
            models.CheckConstraint(condition=~models.Q(role="chair"), name="seat_is_never_a_chair"),
        ]

    def role_labels(self):
        return [str(Position.Role(role).label) for role in self.from_roles if role in Position.Role.values]

    def __str__(self):
        labels = self.role_labels()
        roles = _("%(some)s or %(last)s") % {"some": ", ".join(labels[:-1]), "last": labels[-1]} if len(labels) > 1 else "".join(labels)
        if self.each_association_under:
            source = _("the %(roles)s of each association directly under %(unit)s")
        else:
            source = _("the %(roles)s of %(unit)s")
        return _("%(role)s of %(unit)s, for %(source)s") % {
            "role": self.get_role_display(), "unit": self.unit, "source": source % {"roles": roles, "unit": self.held_in},
        }

    def identity(self):
        return ("unit_id", "held_in_id", "made_by_id")

    def save(self, *args, **kwargs):
        if isinstance(self.from_roles, list):  # in the kit's order, each once, so a rule reads the same however it was given
            known = [role for role in Position.Role.values if role in self.from_roles]
            self.from_roles = known + [role for role in self.from_roles if role not in known]
        super().save(*args, **kwargs)

    def after_save(self):
        from .seats import after_rule_saved

        after_rule_saved(self)

    # ---- Whose seats

    def reaches(self, unit):
        """Whether the roles it comes from are held in this unit, by today's tree."""
        if self.each_association_under:
            return unit.kind == Unit.Kind.ASSOCIATION and unit.parent_id == self.held_in_id
        return unit.pk == self.held_in_id

    def takes(self, position):
        """Whether this rule seats whoever holds that position. Seats come only from
        appointed positions, so there are no chains of seats, and no loops."""
        return not position.is_seat() and position.role in self.from_roles and self.reaches(position.unit)

    def shares_places(self, other):
        """Whether both take roles held in some same unit, by today's tree."""
        if self.each_association_under == other.each_association_under:
            return self.held_in_id == other.held_in_id
        each, named = (self, other) if self.each_association_under else (other, self)
        return each.reaches(named.held_in)

    def sources(self):
        """The appointed positions, current or not, whose holders it seats by today's tree."""
        if self.each_association_under:
            where = models.Q(unit__parent=self.held_in_id, unit__kind=Unit.Kind.ASSOCIATION)
        else:
            where = models.Q(unit=self.held_in_id)
        return Position.objects.filter(where, role__in=self.from_roles, seat_rule__isnull=True)

    # ---- Rules

    def membership_problem(self):
        """A seat that needs a membership (structure.md §3) comes only from board roles held
        in an association at or inside the one the seat needs. Then every holder, now and
        later, already is a member, and a rule here never stops an appointment there."""
        if self.role not in Position.NEEDS_MEMBERSHIP:
            return None
        needed = self.unit if self.role in Position.BOARD_ROLES else self.unit.association()
        if all(role in Position.BOARD_ROLES for role in self.from_roles) and self.held_in.path.startswith(needed.path):
            return None
        return _("A %(role)s of %(unit)s has to be a member of %(needed)s, so the seat can only come from board roles held in %(needed)s or an association inside it.") % {
            "role": self.get_role_display(), "unit": self.unit, "needed": needed,
        }

    @staticmethod
    def problems_after_moving():
        """After a move: any rule that's not ended whose seats would need a membership their
        holders needn't have."""
        rules = SeatRule.objects.filter(models.Q(end__isnull=True) | models.Q(end__gt=_today())).select_related("unit", "held_in")
        return [problem for rule in rules if (problem := rule.membership_problem())]

    def problems(self):
        from .authority import can

        problems = super().problems()
        roles = self.from_roles
        if not isinstance(roles, list) or not roles or not all(role in Position.Role.values for role in roles):
            return [*problems, _("Choose the roles it comes from, from the kit's roles.")]
        unit, held_in = self.unit, self.held_in
        if problem := Position.role_problem(self.role, unit):  # never chair: see SEAT_ROLES
            problems.append(problem)
        if self.each_association_under:
            if held_in.kind != Unit.Kind.ASSOCIATION or not all(role in Position.BOARD_ROLES for role in roles):
                problems.append(_("Only an association has associations directly under it, and they hold board roles alone."))
        else:
            problems += list(dict.fromkeys(problem for role in roles if (problem := Position.role_problem(role, held_in))))
        stored = SeatRule.objects.filter(pk=self.pk).first() if self.pk else None
        if stored is None and self.made_by_id is None:
            problems.append(_("Say who makes the seat rule."))
        if stored is None:
            for where in {unit, held_in}:
                if where.archived:
                    problems.append(_("%(unit)s is archived, so nothing new is set in it.") % {"unit": where})
        if problems:
            return problems

        if problem := self.membership_problem():
            problems.append(problem)
        if self.role in roles and self.reaches(unit):
            problems.append(_("A seat can't come from the same role in the same unit."))
        if self.start:
            alike = Tie.overlapping(SeatRule.objects.filter(unit=unit, role=self.role).exclude(pk=self.pk), self.start, self.end)
            if any(set(other.from_roles) & set(roles) and self.shares_places(other) for other in alike.select_related("held_in")):
                problems.append(_("Another seat rule already gives this seat for one of these roles, held in the same place, at that time."))
        if not problems and self.start and (stored is None or stored.start != self.start):
            if not can(self.made_by, Permission.APPOINT, unit, on=self.start):
                problems.append(_("%(person)s couldn't appoint in %(unit)s on %(day)s, so they can't make its seat rules.") % {"person": self.made_by, "unit": unit, "day": self.start})
        return problems


# ---- The audit log: structure.md §7 -------------------------------------------------------


class AuditQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError(_("The audit log is only ever added to."))

    def delete(self):
        raise ValidationError(_("The audit log is only ever added to."))


class AuditEntry(models.Model):
    """One row for each action, and for each change operators and core make themselves.
    It names nobody: people are ids, and a tie's row leads to its person through the tie,
    while the tie exists. It has no foreign keys, so it never stops a unit or a person
    from being deleted and never needs changing when they are. It's only ever added to."""

    class Via(models.TextChoices):
        ADMIN = "admin", _("the admin page")
        SERVER = "server", _("a command on the server")
        CORE = "core", _("core itself")

    # What can say something about a person: recorded as changed, without its value.
    HIDDEN = {"structure.reason": ("source",), "accounts.user": ("email", "name")}
    # Who a tie is for is left out, so the row leads to them only through the tie. Whoever
    # gave a grant or made a rule is the actor, and the rest is worked out by core.
    LEFT_OUT = ("id", "path", "person", "kept_name", "given_by", "made_by", "seat_since", "seat_until")

    at = models.DateTimeField(_("when"), default=timezone.now, db_index=True)
    action = models.CharField(_("action"), max_length=50, db_index=True)
    via = models.CharField(_("way in"), max_length=20, choices=Via.choices)
    # Ids, as the rows they point at have: BigAutoField.
    actor_account = models.BigIntegerField(_("by account"), null=True, blank=True)
    actor_person = models.BigIntegerField(_("by person"), null=True, blank=True, db_index=True)
    unit = models.BigIntegerField(_("unit"), null=True, blank=True, help_text=_("The unit the permission check asked about."))
    unit_key = models.CharField(_("unit"), max_length=40, blank=True, db_index=True)
    object_type = models.CharField(_("kind of thing"), max_length=50, blank=True)
    object_id = models.BigIntegerField(_("which"), null=True, blank=True)
    changes = models.JSONField(_("changes"), default=dict, blank=True)

    objects = AuditQuerySet.as_manager()

    class Meta:
        verbose_name = _("audit log entry")
        verbose_name_plural = _("audit log")
        ordering = ["-at", "-pk"]
        indexes = [models.Index(fields=["object_type", "object_id"], name="audit_object")]

    def __str__(self):
        return f"{self.at:%Y-%m-%d %H:%M} {self.action}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(_("The audit log is only ever added to."))
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(_("The audit log is only ever added to."))

    @classmethod
    def record(cls, action, via, *, account=None, person=None, unit=None, obj=None, object_id=None, changes=None, using="default"):
        """The one way a row is written. A mistyped way in is a bug, so it raises."""
        cls.Via(via)
        entry = cls(
            action=action, via=via, actor_account=account, actor_person=person,
            unit=unit.pk if unit is not None else None, unit_key=unit.key if unit is not None else "",
            object_type=obj._meta.label_lower if obj is not None else "",
            object_id=object_id if object_id is not None else getattr(obj, "pk", None),
            changes=changes or {},
        )
        entry.save(using=using)
        return entry

    @staticmethod
    def by(user):
        """An operator's account, and its person, as the actor of a row."""
        return {"account": user.pk, "person": Person.objects.filter(user=user.pk).values_list("pk", flat=True).first()}

    @classmethod
    def fields(cls, obj):
        """What a row shows of a unit, a tie, a grant or a rule: its own fields, a unit by
        its key, and dates as text."""
        found = {}
        for field in obj._meta.concrete_fields:
            if field.name in cls.LEFT_OUT:
                continue
            value = getattr(obj, field.attname)
            if field.is_relation and value is not None and field.related_model is Unit:
                value = getattr(obj, field.name).key
            elif hasattr(value, "isoformat"):
                value = value.isoformat()
            found[field.name] = None if value == "" else value
        return found

    @classmethod
    def created(cls, obj):
        """The changes for something new: every field it was given."""
        return cls.compare({}, cls.fields(obj), obj._meta.label_lower)

    @classmethod
    def compare(cls, before, after, kind):
        """Each field that changed, before and after, or None for one whose value isn't
        shown. `kind` is the model's label, such as structure.reason."""
        hidden = cls.HIDDEN.get(kind, ())
        changes = {}
        for name in [*before, *(name for name in after if name not in before)]:
            if before.get(name) != after.get(name):
                changes[name] = None if name in hidden else [before.get(name), after.get(name)]
        return changes
