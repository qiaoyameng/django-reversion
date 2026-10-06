from django.core.management.base import CommandError
from django.db import router
from reversion.models import Revision, Version
from reversion.management.commands import BaseRevisionCommand
from reversion.retention import (
    RetentionPlanStaleError,
    RetentionPolicy,
    build_retention_plan,
)


class Command(BaseRevisionCommand):

    help = "Deletes revisions for a given app [and model]."

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "--days",
            default=0,
            type=int,
            help="Delete only revisions older than the specified number of days.",
        )
        parser.add_argument(
            "--keep",
            default=0,
            type=int,
            help="Keep the specified number of revisions (most recent) for each object.",
        )
        parser.add_argument(
            "--keep-comment",
            action="append",
            default=[],
            metavar="TAG",
            help="Keep restore points whose comment contains TAG. "
                 "Can be specified multiple times.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Only print the retention plan, without deleting anything.",
        )

    def handle(self, *app_labels, **options):
        verbosity = options["verbosity"]
        using = options["using"] or router.db_for_write(Revision)
        model_db = options["model_db"]
        dry_run = options["dry_run"]
        # Build the retention plan (this is the dry-run).
        policy = RetentionPolicy(
            models=tuple(self.get_models(options)),
            keep_last=options["keep"],
            keep_comments=options["keep_comment"],
            days=options["days"],
        )
        plan = build_retention_plan(policy, using=using, model_db=model_db)
        # Print out the plan, with a reason for every kept item.
        if verbosity >= 2:
            for version in Version.objects.using(using).filter(
                pk__in=plan.keep_version_reasons.keys(),
            ).iterator():
                for reason in plan.keep_version_reasons[version.pk]:
                    self.stdout.write("- Keeping version #{version_id} ({object_repr}): {reason}".format(
                        version_id=version.pk,
                        object_repr=version.object_repr,
                        reason=reason,
                    ))
            for revision_id, reasons in sorted(plan.keep_revision_reasons.items()):
                self.stdout.write("- Keeping revision #{revision_id}: {reasons}".format(
                    revision_id=revision_id,
                    reasons=", ".join(reasons),
                ))
        revisions_to_delete = plan.get_delete_revisions()
        versions_to_delete = plan.get_delete_versions()
        if verbosity >= 1:
            for revision in revisions_to_delete.iterator():
                self.stdout.write("- Deleting revision #{revision_id} ({date_created}) {comment}".format(
                    revision_id=revision.pk,
                    date_created=revision.date_created,
                    comment=revision.comment,
                ))
            self.stdout.write("{action} {revision_total} revisions and {version_total} versions.".format(
                action="Would delete" if dry_run else "Deleting",
                revision_total=len(plan.delete_revision_ids),
                version_total=versions_to_delete.count(),
            ))
        if dry_run:
            return
        # Execute the plan, checking that the version set is unchanged.
        try:
            plan.execute()
        except RetentionPlanStaleError as ex:
            raise CommandError(str(ex))
