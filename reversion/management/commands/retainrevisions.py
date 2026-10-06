import json

from django.core.management.base import CommandError

from reversion.errors import RetentionPlanStale
from reversion.models import Revision
from reversion.management.commands import BaseRevisionCommand
from reversion.retention import (
    build_retention_plan,
    execute_retention_plan,
)


class Command(BaseRevisionCommand):

    help = (
        "Prunes revisions while keeping the most recent versions of each "
        "object and explicitly marked recovery points. A dry run is always "
        "performed first; use --execute to actually delete."
    )

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "--keep",
            default=0,
            type=int,
            help="Number of most recent versions to keep for each object.",
        )
        parser.add_argument(
            "--marker",
            action="append",
            default=[],
            dest="markers",
            help=(
                "Keep every revision whose comment contains this marker "
                "(repeatable)."
            ),
        )
        parser.add_argument(
            "--days",
            default=0,
            type=int,
            help="Delete only revisions older than the specified number of days.",
        )
        parser.add_argument(
            "--execute",
            action="store_true",
            default=False,
            help="Actually delete revisions. Without this flag a dry run is printed.",
        )
        parser.add_argument(
            "--plan-file",
            default=None,
            help=(
                "Path to a JSON plan. When dry-running, the plan is saved; "
                "when executing, the saved plan is verified before deletion."
            ),
        )

    def handle(self, *app_labels, **options):
        verbosity = options["verbosity"]
        using = options["using"]
        model_db = options["model_db"]
        keep = options["keep"]
        markers = options["markers"]
        days = options["days"]
        execute = options["execute"]
        plan_file = options["plan_file"]

        models_ = list(self.get_models(options))

        if execute and plan_file:
            plan = self._load_plan(
                plan_file, models_, keep, markers, days, using, model_db
            )
        else:
            plan = build_retention_plan(
                models_,
                keep=keep,
                markers=markers,
                days=days,
                using=using,
                model_db=model_db,
            )

        self._render_plan(plan, verbosity)

        if not execute:
            self.stdout.write("Dry run: no revisions were deleted.")
            if plan_file:
                self._save_plan(plan, plan_file)
                self.stdout.write(f"Plan written to {plan_file}.")
            return

        try:
            deleted = execute_retention_plan(plan)
        except RetentionPlanStale as error:
            raise CommandError(
                "Aborting: the revision set changed since the plan was built. "
                f"Re-run the dry run. ({error})"
            )
        self.stdout.write(f"Deleted {deleted} revision(s).")

    def _render_plan(self, plan, verbosity):
        self.stdout.write(plan.describe())
        if verbosity < 2:
            return
        delete_revisions = Revision.objects.using(plan.using).filter(
            pk__in=plan.delete_revision_ids
        ).order_by("date_created", "pk")
        for revision in delete_revisions:
            self.stdout.write(
                "DELETE revision {pk} (created {created}, comment {comment!r})".format(
                    pk=revision.pk,
                    created=revision.date_created.isoformat(),
                    comment=revision.comment,
                )
            )
            for version in revision.version_set.using(plan.using).order_by("pk"):
                reasons = ", ".join(
                    plan.version_reasons.get(version.pk, ["delete"])
                )
                self.stdout.write(
                    "  DELETE version {pk} [content type {ct} #{oid}] ({reasons})".format(
                        pk=version.pk,
                        ct=version.content_type_id,
                        oid=version.object_id,
                        reasons=reasons,
                    )
                )
        if verbosity < 3:
            return
        kept_revisions = Revision.objects.using(plan.using).filter(
            pk__in=plan.kept_revision_ids
        ).order_by("pk")
        for revision in kept_revisions:
            reasons = "; ".join(plan.revision_reasons.get(revision.pk, []))
            self.stdout.write(
                f"KEEP revision {revision.pk} ({reasons or 'protected'})"
            )
            for version in revision.version_set.using(plan.using).order_by("pk"):
                reasons = "; ".join(plan.version_reasons.get(version.pk, []))
                self.stdout.write(
                    f"  KEEP version {version.pk} ({reasons or 'kept'})"
                )

    def _plan_payload(self, plan):
        return {
            "using": plan.using,
            "keep": plan.keep,
            "markers": list(plan.markers),
            "days": plan.days,
            "model_labels": sorted(
                f"{model._meta.app_label}.{model._meta.model_name}"
                for model in plan.model_dbs
            ),
            "delete_revision_ids": sorted(plan.delete_revision_ids),
            "kept_revision_ids": sorted(plan.kept_revision_ids),
            "fingerprint": plan.fingerprint,
            "snapshot": plan.snapshot,
        }

    def _save_plan(self, plan, path):
        with open(path, "w", encoding="utf8") as plan_file:
            json.dump(self._plan_payload(plan), plan_file, indent=2)

    def _load_plan(self, path, models_, keep, markers, days, using, model_db):
        with open(path, encoding="utf8") as plan_file:
            payload = json.load(plan_file)
        # Rebuild the plan and compare it against the saved snapshot. This
        # refuses to execute when parameters, scope or the underlying version
        # set have drifted from the preview.
        rebuilt = build_retention_plan(
            models_,
            keep=keep,
            markers=markers,
            days=days,
            using=using,
            model_db=model_db,
        )
        expected_labels = set(payload.get("model_labels", ()))
        actual_labels = {
            f"{model._meta.app_label}.{model._meta.model_name}"
            for model in models_
        }
        if expected_labels != actual_labels:
            raise CommandError(
                "Plan scope differs from the dry run; re-run the dry run."
            )
        if (
            payload.get("keep") != keep
            or list(payload.get("markers", ())) != list(markers)
            or payload.get("days") != days
        ):
            raise CommandError(
                "Plan options differ from the dry run; re-run the dry run."
            )
        if payload.get("fingerprint") != rebuilt.fingerprint:
            raise CommandError(
                "The revision set changed since the dry run; re-run it."
            )
        if set(payload.get("delete_revision_ids", ())) != rebuilt.delete_revision_ids:
            raise CommandError(
                "Computed deletion set differs from the dry run; re-run it."
            )
        return rebuilt
