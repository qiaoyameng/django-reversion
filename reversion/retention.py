"""
Retention-plan based deletion of revisions.

Instead of blindly deleting revisions by age, a retention plan first
decides which versions must be kept (the most recent versions of each
object plus explicitly tagged restore points), then computes the
relational dependencies of those keep targets (versions sharing a
revision, revisions referenced by metadata). Only revisions that are
not needed by any keep target are deleted.

A plan is a dry-run by nature: build it, inspect it, then execute it.
Execution re-checks that the version set the plan was based on has not
changed, so versions written after the plan was built are never
deleted by accident.
"""
from collections import defaultdict
from datetime import timedelta
import hashlib

from django.apps import apps
from django.db import models, router, transaction
from django.utils import timezone


class RetentionPlanStaleError(Exception):

    """Raised when the version set changed between planning and execution."""


def _get_reversion_models():
    # Imported lazily, since this module is imported by reversion/__init__.py,
    # which is loaded before the app registry is ready.
    from reversion.models import Revision, Version
    return Revision, Version


def _get_revision_meta_models():
    """
    Yields all models holding a foreign key to Revision, excluding the
    Version model itself. Such rows are metadata attached to a revision,
    and a revision they reference cannot be deleted.
    """
    Revision, Version = _get_reversion_models()
    for model in apps.get_models():
        if model is Version:
            continue
        for field in model._meta.get_fields():
            if (
                isinstance(field, models.ForeignKey) and
                field.remote_field.model is Revision
            ):
                yield model
                break


def _fingerprint(version_ids, revision_ids, meta_revision_ids):
    hasher = hashlib.md5()
    for ids in (version_ids, revision_ids, meta_revision_ids):
        for pk in sorted(ids):
            hasher.update(str(pk).encode("ascii"))
            hasher.update(b",")
        hasher.update(b"|")
    return hasher.hexdigest()


def _get_meta_revision_ids(using, revision_ids):
    Revision, Version = _get_reversion_models()
    meta_revision_ids = set()
    for meta_model in _get_revision_meta_models():
        field = next(
            field for field in meta_model._meta.get_fields()
            if isinstance(field, models.ForeignKey) and field.remote_field.model is Revision
        )
        meta_revision_ids.update(
            meta_model._default_manager.using(using).filter(
                **{field.name + "__in": revision_ids},
            ).values_list(field.attname, flat=True).iterator()
        )
    return meta_revision_ids


def _collect_scope(*, using, model_db, models_):
    """
    Collects all versions in scope of the plan, plus the ids of
    revisions referenced by metadata models.
    """
    Revision, Version = _get_reversion_models()
    versions = Version.objects.using(using).none()
    for model in models_:
        versions = versions | Version.objects.using(using).get_for_model(
            model, model_db=model_db,
        )
    versions = list(versions.order_by("pk").iterator())
    version_ids = [version.pk for version in versions]
    revision_ids = list({version.revision_id for version in versions})
    meta_revision_ids = _get_meta_revision_ids(using, revision_ids)
    return version_ids, revision_ids, meta_revision_ids, versions


class RetentionPlan:

    """
    The result of planning a retention run.

    Lists the revisions (and their versions) to delete, and records a
    reason for every version and revision that is kept.
    """

    def __init__(self, *, using, model_db, policy, version_ids, revision_ids,
                 meta_revision_ids, keep_version_reasons, keep_revision_reasons,
                 delete_revision_ids):
        self.using = using
        self.model_db = model_db
        self.policy = policy
        self.fingerprint = _fingerprint(version_ids, revision_ids, meta_revision_ids)
        # A dict of {version_id: [reason, ...]} for every kept version.
        self.keep_version_reasons = keep_version_reasons
        # A dict of {revision_id: [reason, ...]} for every kept revision.
        self.keep_revision_reasons = keep_revision_reasons
        # Revisions selected for deletion. Their versions are deleted by cascade.
        self.delete_revision_ids = delete_revision_ids

    def get_delete_versions(self):
        """All versions that will be deleted, as a queryset."""
        Revision, Version = _get_reversion_models()
        return Version.objects.using(self.using).filter(
            revision_id__in=self.delete_revision_ids,
        ).order_by()

    def get_delete_revisions(self):
        """All revisions that will be deleted, as a queryset."""
        Revision, Version = _get_reversion_models()
        return Revision.objects.using(self.using).filter(
            pk__in=self.delete_revision_ids,
        ).order_by()

    def execute(self):
        """
        Deletes the planned revisions inside a transaction.

        Re-checks the fingerprint of the underlying version set first,
        raising RetentionPlanStaleError if versions or metadata were
        added or removed since the plan was built.
        """
        with transaction.atomic(using=self.using):
            if self._current_fingerprint() != self.fingerprint:
                raise RetentionPlanStaleError(
                    "The version set has changed since this retention plan was "
                    "built. Rebuild the plan (dry-run) before executing it."
                )
            self.get_delete_revisions().delete()

    def _current_fingerprint(self):
        version_ids, revision_ids, meta_revision_ids, _versions = _collect_scope(
            using=self.using,
            model_db=self.model_db,
            models_=self.policy.models,
        )
        return _fingerprint(version_ids, revision_ids, meta_revision_ids)


class RetentionPolicy:

    """
    Defines which versions are keep targets.

    :param models: The registered models to plan deletion for.
    :param keep_last: Keep this many most recent versions per object.
    :param keep_comments: Keep restore points (whole revisions) whose
        comment contains any of these tags.
    :param days: Only delete revisions older than this many days.
    """

    def __init__(self, models, keep_last=0, keep_comments=(), days=0):
        self.models = tuple(models)
        self.keep_last = keep_last
        self.keep_comments = tuple(keep_comments)
        self.days = days


def build_retention_plan(policy, *, using=None, model_db=None):
    """
    Builds a retention plan for the given policy. This is a dry-run:
    nothing is deleted until plan.execute() is called.
    """
    Revision, Version = _get_reversion_models()
    using = using or router.db_for_write(Revision)
    version_ids, revision_ids, meta_revision_ids, versions = _collect_scope(
        using=using,
        model_db=model_db,
        models_=policy.models,
    )
    keep_version_reasons = defaultdict(list)
    keep_revision_reasons = defaultdict(list)

    def keep_version(version, reason):
        keep_version_reasons[version.pk].append(reason)

    # Keep target: the most recent `keep_last` versions of each object.
    if policy.keep_last:
        versions_by_object = defaultdict(list)
        for version in versions:
            versions_by_object[(version.content_type_id, version.object_id, version.db)].append(version)
        for object_versions in versions_by_object.values():
            recent = sorted(object_versions, key=lambda version: version.pk, reverse=True)
            for version in recent[:policy.keep_last]:
                keep_version(version, "among the last {keep_last} versions of {object_repr}".format(
                    keep_last=policy.keep_last,
                    object_repr=version.object_repr,
                ))
    # Keep target: tagged restore points. A restore point is a whole
    # revision, since revert() operates on revisions.
    if policy.keep_comments:
        comments_by_revision_id = {
            revision.pk: revision.comment
            for revision in Revision.objects.using(using).filter(
                pk__in={version.revision_id for version in versions},
            ).iterator()
        }
        for version in versions:
            comment = comments_by_revision_id.get(version.revision_id, "")
            for tag in policy.keep_comments:
                if tag in comment:
                    keep_version(version, "tagged restore point (comment contains {tag!r})".format(
                        tag=tag,
                    ))
                    break
    # Dependency: a revision is the atomic revert unit, so every version
    # sharing a revision with a kept version is required to restore it.
    kept_revision_ids = {
        version.revision_id
        for version in versions
        if version.pk in keep_version_reasons
    }
    for version in versions:
        if version.pk not in keep_version_reasons and version.revision_id in kept_revision_ids:
            keep_version(version, "shares revision #{revision_id} with a kept version".format(
                revision_id=version.revision_id,
            ))
    # Dependency: a revision referenced by metadata cannot be deleted.
    for revision_id in meta_revision_ids:
        keep_revision_reasons[revision_id].append("referenced by revision metadata")
    for version in versions:
        if version.pk not in keep_version_reasons and version.revision_id in meta_revision_ids:
            keep_version(version, "revision #{revision_id} is referenced by metadata".format(
                revision_id=version.revision_id,
            ))
    # Record why kept revisions are kept.
    for version in versions:
        if version.pk in keep_version_reasons:
            if version.revision_id not in keep_revision_reasons:
                keep_revision_reasons[version.revision_id].append("contains kept versions")
    # Age limit: only revisions older than `days` may be deleted.
    if policy.days:
        cutoff = timezone.now() - timedelta(days=policy.days)
        recent_revision_ids = set(
            Revision.objects.using(using).filter(
                pk__in=revision_ids,
                date_created__gte=cutoff,
            ).values_list("pk", flat=True)
        )
        for version in versions:
            if version.pk not in keep_version_reasons and version.revision_id in recent_revision_ids:
                keep_version(version, "revision is newer than {days} days".format(days=policy.days))
                if version.revision_id not in keep_revision_reasons:
                    keep_revision_reasons[version.revision_id].append(
                        "newer than {days} days".format(days=policy.days),
                    )
    # A revision can only be deleted if none of its versions are kept.
    kept_version_revision_ids = {
        version.revision_id for version in versions if version.pk in keep_version_reasons
    }
    delete_revision_ids = [
        revision_id
        for revision_id in revision_ids
        if revision_id not in kept_version_revision_ids
    ]
    return RetentionPlan(
        using=using,
        model_db=model_db,
        policy=policy,
        version_ids=version_ids,
        revision_ids=revision_ids,
        meta_revision_ids=meta_revision_ids,
        keep_version_reasons=dict(keep_version_reasons),
        keep_revision_reasons=dict(keep_revision_reasons),
        delete_revision_ids=delete_revision_ids,
    )
