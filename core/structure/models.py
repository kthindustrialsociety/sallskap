"""The tree of associations and teams: structure.md §1. The rules are checked on
every save, wherever it comes from, and the database holds the ones it can."""

import re

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.validators import RegexValidator
from django.db import models, transaction
from django.db.models.signals import m2m_changed
from django.dispatch import receiver
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

# Lowercase letters, digits and single hyphens, from a letter to a letter or digit, at
# most 40 characters: what every tool accepts as a name. kit/kit.py checks the same.
KEY = r"^[a-z]([a-z0-9-]{0,38}[a-z0-9])?$"
# Tools already see groups made by hand as custom:<name> (User.group_keys), so a unit
# called custom would give keys such as custom:lead that look like one of those.
RESERVED_KEYS = {"custom"}


def validate_key(key):
    if not re.fullmatch(KEY, key or "") or "--" in key:
        raise ValidationError(
            _("%(key)s isn't a valid key. Use lowercase letters, digits and single hyphens, starting with a letter and ending with a letter or digit, at most 40 characters."),
            params={"key": repr(key)},
        )
    if key in RESERVED_KEYS:
        raise ValidationError(_("The key %(key)s is reserved."), params={"key": key})


colour = RegexValidator(r"^#[0-9a-fA-F]{6}$", _("Write a colour as # and six hex digits, such as #1a5fb4."))


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
                condition=models.Q(key__regex=KEY) & ~models.Q(key__contains="--") & ~models.Q(key__in=RESERVED_KEYS),
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
        keys = self.path.strip("/").split("/")[:-1]
        paths = ["/" + "/".join(keys[: i + 1]) + "/" for i in range(len(keys))]
        return Unit.objects.filter(path__in=paths).order_by("path")

    def descendants(self):
        return Unit.objects.filter(path__startswith=self.path).exclude(pk=self.pk)

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

    # ---- Archiving and deleting --------------------------------------------------

    def archive(self):
        """Archives this team and everything under it. Ones already archived keep their date."""
        if self.kind != Unit.Kind.TEAM:
            raise ValidationError(_("Only teams are archived. %(unit)s is an association.") % {"unit": self})
        now = timezone.now()
        Unit.objects.filter(path__startswith=self.path, archived_at__isnull=True).update(archived_at=now)
        self.archived_at = self.archived_at or now

    def unarchive(self):
        """Brings back this team alone. The ones under it stay archived until brought back too."""
        if self.parent and self.parent.archived:
            raise ValidationError(_("%(parent)s is archived, so take it out of the archive first.") % {"parent": self.parent})
        Unit.objects.filter(pk=self.pk).update(archived_at=None)
        self.archived_at = None

    def deletion_problem(self):
        """Why this unit can't be deleted, or None. A unit is deleted only if nothing
        ever pointed at it. Otherwise it's archived, so its history is kept."""
        if self.is_root:
            return _("The root can't be deleted.")
        if self.children.exists():
            return _("%(unit)s has units inside it.") % {"unit": self}
        if self.run_by.exists() or self.teams_taking_part.exists():
            return _("Teams are run for %(unit)s, or involve it.") % {"unit": self}
        return None

    def check_can_delete(self):
        problem = self.deletion_problem()
        if problem:
            raise ValidationError(problem)

    def delete(self, *args, **kwargs):
        self.check_can_delete()
        return super().delete(*args, **kwargs)


@receiver(m2m_changed, sender=Unit.taking_part.through)
def _check_taking_part(sender, instance, action, reverse, pk_set, **kwargs):
    if action != "pre_add":
        return
    added = Unit.objects.filter(pk__in=pk_set)
    if reverse:  # an association being added to teams
        for team in added:
            Unit.check_taking_part(team.kind, team.parent, [instance])
    else:
        Unit.check_taking_part(instance.kind, instance.parent, added)


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
        Unit(kind=Unit.Kind.ASSOCIATION, key=key, name=name).save(using=using)
    elif root.key != key:
        raise ImproperlyConfigured(
            f"The config says key = {key!r} under [association], but the association's key is {root.key!r}, "
            f"and a key can't change: tools know it by it. Put key = {root.key!r} back in the config."
        )
    elif root.name != name:
        root.name = name
        root.save(using=using)
