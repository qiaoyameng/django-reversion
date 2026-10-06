"""
Retention plans for pruning revision history without destroying the ability to
revert.

A plan keeps, for every object:

* the most recent ``keep`` versions of the object;
* any version belonging to a revision whose comment contains a configured
  marker (an explicit recovery point);

and then closes the set over the revision boundary: a revision kept because of
one object keeps every version stored in that revision, as revisions are the
unit that can be reverted and sibling versions may be required to satisfy
foreign keys. Revisions referenced by revision metadata models are kept
unconditionally.
"""

from collections import defaultdict
from datetime import timedelta
import hashlib

from django.apps import apps
from django.db import models, router, transaction
from django.utils import timezone

from reversion.errors import RetentionPlanStale
from reversion.revisions import _get_options, is_registered


# Retention reason strings.
REASON_RECENT = "recent:{keep}"
REASON_MARKER = "marker:{marker}"
REASON_SHARED_REVISION = "shared revision"
REASON_META = "revision metadata"
REASON_OUT_OF_SCOPE = "out of scope"


def _meta_revision_ids(using):
    """Return ids of revisions referenced by any revision metadata model."""
    from reversion.models import Revision, Version

    revision_ids = set()
    for model in apps.get_models():
        # Version.revision is the canonical membership link, not metadata.
        if model is Version:
            continue
        for field in model._meta.get_fields():
            if (
                isinstance(field, models.ForeignKey)
                and field.related_model is Revision
            ):
                manager = model._base_manager.using(using)
                revision_ids.update(
                    manager.exclude(
                        **{f"{field.name}_id__isnull": True}
                    ).values_list(f"{field.name}_id", flat=True)
                )
    return revision_ids


def _snapshot(using):
    """Return a fingerprintable snapshot of the revision tables.

    The snapshot captures the row count and the high/low water marks of each
    table. A deletion followed by an insert between the dry run and the
    execution changes the high water mark; an unbalanced change moves the
    count, so neither is masked.
    """
    def table_snapshot(queryset):
        return queryset.aggregate(
            count=models.Count("pk"),
            min_pk=models.Min("pk"),
            max_pk=models.Max("pk"),
            sum_pk=models.Sum("pk"),
        )

    from reversion.models import Revision, Version

    return {
        "revision": table_snapshot(Revision.objects.using(using)),
        "version": table_snapshot(Version.objects.using(using)),
    }


class RetentionPlan:

    """The result of :func:`build_retention_plan`.

    Attributes:
        kept_revision_ids: ids of revisions that must not be deleted.
        delete_revision_ids: ids of revisions the plan would delete.
        revision_reasons: mapping of revision id to a list of human readable
            reasons describing why the revision is kept or deleted.
        version_reasons: mapping of version id to a list of human readable
            reasons describing why the version is kept or deleted.
        fingerprint: opaque checksum of the version set the plan is based on.
    """

    def __init__(
        self,
        using,
        model_dbs,
        keep,
        markers,
        days,
        kept_revision_ids,
        delete_revision_ids,
        revision_reasons,
        version_reasons,
        fingerprint,
        snapshot,
    ):
        self.using = using
        self.model_dbs = model_dbs
        self.keep = keep
        self.markers = markers
        self.days = days
        self.kept_revision_ids = kept_revision_ids
        self.delete_revision_ids = delete_revision_ids
        self.revision_reasons = revision_reasons
        self.version_reasons = version_reasons
        self.fingerprint = fingerprint
        self.snapshot = snapshot

    @property
    def delete_version_ids(self):
        """Ids of versions that belong to a revision the plan deletes."""
        from reversion.models import Version

        return list(
            Version.objects.using(self.using).filter(
                revision_id__in=self.delete_revision_ids
            ).order_by("pk").values_list("pk", flat=True)
        )

    def describe(self):
        """Return a human readable description of the plan."""
        lines = [
            f"Keep {self.keep} most recent version(s) per object.",
        ]
        if self.markers:
            lines.append(
                "Keep revisions whose comment contains: "
                + ", ".join(self.markers)
                + "."
            )
        if self.days:
            lines.append(
                f"Only delete revisions older than {self.days} day(s)."
            )
        lines.append(
            f"Would delete {len(self.delete_revision_ids)} revision(s) "
            f"and their versions, keeping {len(self.kept_revision_ids)}."
        )
        return "\n".join(lines)


def _add_reason(reasons, key, reason):
    if reason not in reasons[key]:
        reasons[key].append(reason)


def build_retention_plan(
    models_,
    keep=0,
    markers=(),
    days=0,
    using=None,
    model_db=None,
):
    """Build a retention plan for the given registered models.

    Args:
        models_: iterable of model classes the plan covers.
        keep: number of most recent versions to keep for each object.
        markers: iterable of comment substrings; every revision whose comment
            contains a marker is kept as a recovery point.
        days: revisions younger than this many days are never deleted.
        using: database holding the revision tables.
        model_db: database holding the versioned model data.
    """
    models_ = tuple(models_)
    from reversion.models import Revision, Version

    using = using or router.db_for_write(Revision)
    model_dbs = {}
    for model in models_:
        if not is_registered(model):
            continue
        model_dbs[model] = model_db or router.db_for_write(model)

    revision_reasons = defaultdict(list)
    version_reasons = defaultdict(list)
    kept_revision_ids = set()

    scoped_revision_ids = set()

    # Versions explicitly selected by the retention rules, keyed by revision.
    for model, current_model_db in model_dbs.items():
        model_versions = Version.objects.using(using).get_for_model(
            model,
            model_db=current_model_db,
        )
        scoped_revision_ids.update(
            model_versions.values_list("revision_id", flat=True)
        )
        object_ids = model_versions.order_by().values_list(
            "object_id", flat=True
        ).distinct()
        # Keep the most recent versions of every object. Versions are ordered
        # by descending revision primary key (Version.Meta.ordering), so the
        # first rows for each object are the most recent.
        if keep:
            recent_reason = REASON_RECENT.format(keep=keep)
            for object_id in object_ids:
                recent_revision_ids = model_versions.filter(
                    object_id=object_id
                ).values_list("revision_id", flat=True)[:keep]
                recent_revision_ids = list(recent_revision_ids)
                kept_revision_ids.update(recent_revision_ids)
                recent_versions = model_versions.filter(
                    object_id=object_id,
                    revision_id__in=recent_revision_ids,
                )
                for version_id, revision_id in recent_versions.values_list(
                    "pk", "revision_id"
                ):
                    _add_reason(version_reasons, version_id, recent_reason)
                    _add_reason(revision_reasons, revision_id, recent_reason)

    # Keep revisions explicitly marked as recovery points. Markers are not
    # scoped to a model: a marked revision is always kept whole.
    marker_q = models.Q()
    for marker in markers:
        marker_q |= models.Q(comment__contains=marker)
    if markers:
        marked_revisions = Revision.objects.using(using).filter(marker_q)
        marked_version_ids = defaultdict(list)
        for version_id, revision_id in Version.objects.using(using).filter(
            revision__in=marked_revisions.values("pk")
        ).values_list("pk", "revision_id"):
            marked_version_ids[revision_id].append(version_id)
        for revision in marked_revisions.only("pk", "comment"):
            kept_revision_ids.add(revision.pk)
            for marker in markers:
                if marker in revision.comment:
                    reason = REASON_MARKER.format(marker=marker)
                    _add_reason(revision_reasons, revision.pk, reason)
                    for version_id in marked_version_ids.get(revision.pk, ()):
                        _add_reason(version_reasons, version_id, reason)

    # Keep revisions referenced by revision metadata models.
    meta_revision_ids = _meta_revision_ids(using)
    for revision_id in meta_revision_ids:
        kept_revision_ids.add(revision_id)
        _add_reason(revision_reasons, revision_id, REASON_META)

    # A scoped revision that also stores a version of a model outside the plan
    # scope is protected: deleting it would cascade to data the plan does not
    # own. (Completely foreign revisions are simply never deletion candidates.)
    scoped_content_type_ids = {
        Version.objects.using(using).get_for_model(
            model,
            model_db=current_model_db,
        ).values_list("content_type", flat=True).first()
        for model, current_model_db in model_dbs.items()
    }
    scoped_content_type_ids.discard(None)
    out_of_scope_revision_ids = set()
    for version in Version.objects.using(using).filter(
        revision_id__in=scoped_revision_ids
    ).values("pk", "revision_id", "content_type_id", "db"):
        if version["content_type_id"] not in scoped_content_type_ids:
            out_of_scope_revision_ids.add(version["revision_id"])
    for revision_id in out_of_scope_revision_ids:
        _add_reason(revision_reasons, revision_id, REASON_OUT_OF_SCOPE)
        kept_revision_ids.add(revision_id)

    # Everything scoped that is not kept is a deletion candidate, subject to
    # the age cutoff.
    delete_revision_ids = scoped_revision_ids - kept_revision_ids
    delete_revisions = Revision.objects.using(using).filter(
        pk__in=delete_revision_ids
    )
    if days:
        delete_revisions = delete_revisions.filter(
            date_created__lt=timezone.now() - timedelta(days=days)
        )
    delete_revision_ids = set(delete_revisions.values_list("pk", flat=True))
    # Revisions inside the age window survive with their own reason.
    age_protected_ids = (
        scoped_revision_ids - kept_revision_ids - delete_revision_ids
    )
    for revision_id in age_protected_ids:
        _add_reason(
            revision_reasons,
            revision_id,
            f"younger than {days} day(s)",
        )
        kept_revision_ids.add(revision_id)

    # Close over the revision boundary: every version stored in a kept
    # revision is kept too, since reverting the revision needs all of them and
    # sibling versions may hold required foreign key targets.
    sibling_versions = Version.objects.using(using).filter(
        revision_id__in=kept_revision_ids
    ).values_list("pk", "revision_id")
    for version_id, revision_id in sibling_versions:
        if not version_reasons[version_id]:
            _add_reason(
                version_reasons, version_id, REASON_SHARED_REVISION
            )

    # Annotate versions that will be deleted.
    delete_reason = "delete: obsolete revision"
    for version_id, revision_id in Version.objects.using(using).filter(
        revision_id__in=delete_revision_ids
    ).values_list("pk", "revision_id"):
        _add_reason(version_reasons, version_id, delete_reason)
        _add_reason(revision_reasons, revision_id, delete_reason)

    snapshot = _snapshot(using)
    fingerprint = hashlib.sha256(
        repr((snapshot, tuple(markers), keep, days)).encode("utf8")
    ).hexdigest()

    return RetentionPlan(
        using=using,
        model_dbs=model_dbs,
        keep=keep,
        markers=tuple(markers),
        days=days,
        kept_revision_ids=kept_revision_ids,
        delete_revision_ids=delete_revision_ids,
        revision_reasons=dict(revision_reasons),
        version_reasons=dict(version_reasons),
        fingerprint=fingerprint,
        snapshot=snapshot,
    )


def check_retention_plan(plan):
    """Raise :class:`RetentionPlanStale` if the version set has changed."""
    current = _snapshot(plan.using)
    if current != plan.snapshot:
        raise RetentionPlanStale(
            "The revision/version set has changed since the plan was built. "
            "Re-run the dry run before executing the plan."
        )


def execute_retention_plan(plan, dry_run=False):
    """Delete the revisions selected by the plan inside a transaction.

    The plan's version set is re-checked before deleting, so plans executed
    after new revisions were written fail instead of deleting (or cascading
    away) versions the preview did not account for.

    Returns the number of revisions deleted (always ``0`` for a dry run).
    """
    if dry_run:
        return 0
    from reversion.models import Revision

    with transaction.atomic(using=plan.using):
        check_retention_plan(plan)
        # Defence in depth: never touch a revision the plan did not select,
        # and never delete a revision that still has a metadata reference.
        protected_ids = _meta_revision_ids(plan.using) & set(plan.delete_revision_ids)
        if protected_ids:
            raise RetentionPlanStale(
                "Revisions selected for deletion are referenced by metadata: "
                f"{sorted(protected_ids)}"
            )
        revisions_to_delete = Revision.objects.using(plan.using).filter(
            pk__in=plan.delete_revision_ids
        )
        deleted_total = revisions_to_delete.count()
        revisions_to_delete.delete()
        return deleted_total
