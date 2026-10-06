from datetime import timedelta

import reversion
from reversion.errors import RetentionPlanStale
from reversion.models import Revision, Version
from reversion.retention import (
    build_retention_plan,
    check_retention_plan,
    execute_retention_plan,
)
from django.utils import timezone

from test_app.models import TestMeta, TestModel, TestModelInline
from test_app.tests.base import TestBase


class RetentionPlanTest(TestBase):

    def setUp(self):
        super().setUp()
        reversion.register(TestModel)
        reversion.register(TestModelInline)

    def revision(self, comment):
        return Revision.objects.get(comment=comment)

    def testRetentionPlanSharedRevisionDeletedObjectAndLaterVersions(self):
        # Time flows downward. Deletes do not create revisions in
        # django-reversion, so R2 becomes the deleted b's tombstone.
        # R1: shared snapshot of a and b
        # R2: b edit, shared with an unrelated c (multi-object revision)
        # b is then deleted
        # R3: a edit written after the deletion
        # R4: a latest edit
        with reversion.create_revision():
            b = TestModel.objects.create(name="b-v1")
            a = TestModel.objects.create(name="a-v1")
            reversion.set_comment("R1 shared a,b")
        with reversion.create_revision():
            b.name = "b-v2"
            b.save()
            c = TestModel.objects.create(name="c-v1")
            reversion.set_comment("R2 shared b,c")
        b_pk = b.pk
        c_pk = c.pk
        a_pk = a.pk
        b.delete()
        with reversion.create_revision():
            a.name = "a-v2"
            a.save()
            reversion.set_comment("R3 a after delete")
        with reversion.create_revision():
            a.name = "a-v3"
            a.save()
            reversion.set_comment("R4 a latest")

        plan = build_retention_plan((TestModel,), keep=1)

        r1 = self.revision("R1 shared a,b")
        r2 = self.revision("R2 shared b,c")
        r3 = self.revision("R3 a after delete")
        r4 = self.revision("R4 a latest")

        # Latest per object: a -> R4, deleted b -> R2 (tombstone), c -> R2.
        # R1 is pulled in by revision closure because it is shared with R2? It
        # is not: R2 keeps itself. R1 survives only because b's R1 version is
        # in the same revision as a's R1 version and R1 is a's history ... but
        # keep=1 for a points at R4. R1 must be deleted, R2/R4 kept.
        self.assertEqual(plan.kept_revision_ids, {r2.pk, r4.pk})
        self.assertEqual(plan.delete_revision_ids, {r1.pk, r3.pk})

        # The dry run changes nothing.
        check_retention_plan(plan)
        self.assertEqual(execute_retention_plan(plan, dry_run=True), 0)
        self.assertEqual(Revision.objects.count(), 4)

        execute_retention_plan(plan)
        self.assertFalse(Revision.objects.filter(pk__in=[r1.pk, r3.pk]).exists())

        # Recovery points are not just counts: they really revert. The deleted
        # referenced object b comes back from its R2 tombstone.
        r2.revert()
        self.assertTrue(TestModel.objects.filter(pk=b_pk, name="b-v2").exists())
        self.assertTrue(TestModel.objects.filter(pk=c_pk, name="c-v1").exists())
        r4.revert()
        self.assertTrue(TestModel.objects.filter(pk=a_pk, name="a-v3").exists())

        # get_deleted still reports the object when it is removed again, using
        # the retained tombstone version.
        TestModel.objects.get(pk=b_pk).delete()
        tombstone = Version.objects.get_deleted(TestModel).get(object_id=str(b_pk))
        self.assertEqual(tombstone.revision_id, r2.pk)
        tombstone.revert()
        self.assertTrue(TestModel.objects.filter(pk=b_pk, name="b-v2").exists())

    def testRetentionPlanSharedRevisionClosureKeepsSiblingVersions(self):
        # a and b share R1 and R2; b gets an extra edit R3. keep=1 keeps R3
        # for b and R2 for a; R1 is then obsolete even though it is a shared
        # revision, so its delete is legitimate. To prove closure the other
        # way, a marker on R3 pulls in b and then nothing extra; instead mark
        # R1 and assert the whole shared revision survives.
        with reversion.create_revision():
            a = TestModel.objects.create(name="a-v1")
            b = TestModel.objects.create(name="b-v1")
            reversion.set_comment("R1 shared")
        with reversion.create_revision():
            a.name = "a-v2"
            a.save()
            b.name = "b-v2"
            b.save()
            reversion.set_comment("R2 shared")
        with reversion.create_revision():
            b.name = "b-v3"
            b.save()
            reversion.set_comment("R3 b only")

        plan = build_retention_plan(
            (TestModel,),
            keep=1,
            markers=("BACKUP",),
        )
        r1 = self.revision("R1 shared")
        r2 = self.revision("R2 shared")
        r3 = self.revision("R3 b only")
        self.assertEqual(plan.kept_revision_ids, {r2.pk, r3.pk})
        self.assertEqual(plan.delete_revision_ids, {r1.pk})

        r1.comment = "R1 shared BACKUP"
        r1.save()
        plan = build_retention_plan(
            (TestModel,),
            keep=1,
            markers=("BACKUP",),
        )
        self.assertEqual(plan.kept_revision_ids, {r1.pk, r2.pk, r3.pk})
        self.assertEqual(plan.delete_revision_ids, set())
        execute_retention_plan(plan)
        # The marked shared snapshot restores both objects.
        r1.revert()
        self.assertTrue(TestModel.objects.filter(pk=a.pk, name="a-v1").exists())
        self.assertTrue(TestModel.objects.filter(pk=b.pk, name="b-v1").exists())

    def testRetentionPlanPrunesAndPreservesMarkers(self):
        with reversion.create_revision():
            obj = TestModel.objects.create(name="v1")
            reversion.set_comment("old baseline")
        with reversion.create_revision():
            obj.name = "release-v1"
            obj.save()
            reversion.set_comment("RELEASE 1.0")
        with reversion.create_revision():
            obj.name = "v3"
            obj.save()
            reversion.set_comment("work in progress")
        with reversion.create_revision():
            obj.name = "v4"
            obj.save()
            reversion.set_comment("current")

        plan = build_retention_plan(
            (TestModel,),
            keep=1,
            markers=("RELEASE",),
        )

        baseline = self.revision("old baseline")
        release = self.revision("RELEASE 1.0")
        wip = self.revision("work in progress")
        current = self.revision("current")

        self.assertEqual(plan.delete_revision_ids, {baseline.pk, wip.pk})
        self.assertEqual(plan.kept_revision_ids, {release.pk, current.pk})
        self.assertIn("marker:RELEASE", plan.revision_reasons[release.pk])
        # The deletion manifest enumerates the concrete Version rows too.
        expected_version_ids = set(
            Version.objects.filter(
                revision_id__in=[baseline.pk, wip.pk]
            ).values_list("pk", flat=True)
        )
        self.assertEqual(set(plan.delete_version_ids), expected_version_ids)
        deleted_version_ids = Version.objects.filter(
            revision_id__in=plan.delete_revision_ids
        ).values_list("pk", flat=True)
        for version_id in deleted_version_ids:
            self.assertIn(
                "delete: obsolete revision",
                plan.version_reasons[version_id],
            )

        deleted = execute_retention_plan(plan)
        self.assertEqual(deleted, 2)
        self.assertEqual(
            set(Revision.objects.values_list("pk", flat=True)),
            {release.pk, current.pk},
        )

        release.revert()
        obj.refresh_from_db()
        self.assertEqual(obj.name, "release-v1")
        current.revert()
        obj.refresh_from_db()
        self.assertEqual(obj.name, "v4")

    def testRetentionPlanProtectsMetadataReferences(self):
        with reversion.create_revision():
            obj = TestModel.objects.create(name="v1")
            reversion.set_comment("meta pinned")
            reversion.add_meta(TestMeta, name="audit")
        with reversion.create_revision():
            obj.name = "v2"
            obj.save()
            reversion.set_comment("current")

        plan = build_retention_plan((TestModel,), keep=1)
        pinned = self.revision("meta pinned")
        self.assertIn(pinned.pk, plan.kept_revision_ids)
        self.assertIn("revision metadata", plan.revision_reasons[pinned.pk])
        self.assertEqual(plan.delete_revision_ids, set())
        execute_retention_plan(plan)
        self.assertTrue(TestMeta.objects.filter(name="audit").exists())

    def testRetentionPlanStaleAfterNewWrite(self):
        with reversion.create_revision():
            obj = TestModel.objects.create(name="v1")
            reversion.set_comment("old")
        with reversion.create_revision():
            obj.name = "v2"
            obj.save()
            reversion.set_comment("current")

        plan = build_retention_plan((TestModel,), keep=1)
        old = self.revision("old")
        self.assertEqual(plan.delete_revision_ids, {old.pk})

        # A new revision lands between the preview and the execution.
        with reversion.create_revision():
            obj.name = "v3"
            obj.save()
            reversion.set_comment("written after preview")

        with self.assertRaises(RetentionPlanStale):
            check_retention_plan(plan)
        with self.assertRaises(RetentionPlanStale):
            execute_retention_plan(plan)
        # Nothing was deleted: the stale plan aborted before any write.
        self.assertTrue(Revision.objects.filter(pk=old.pk).exists())
        self.assertEqual(Revision.objects.count(), 3)

        # A fresh plan recomputes against the current version set and succeeds.
        fresh = build_retention_plan((TestModel,), keep=1)
        execute_retention_plan(fresh)
        remaining_comments = set(Revision.objects.values_list("comment", flat=True))
        self.assertEqual(remaining_comments, {"written after preview"})

    def testRetentionPlanScopedModelsDoNotTouchForeignVersions(self):
        target = TestModel.objects.create(name="inline target")
        other_target = TestModel.objects.create(name="inline target 2")
        with reversion.create_revision():
            TestModelInline.objects.create(test_model=target)
            TestModel.objects.create(name="unrelated")
            reversion.set_comment("shared")
        with reversion.create_revision():
            TestModelInline.objects.create(test_model=other_target)
            reversion.set_comment("inline only")

        # Plan covers only inlines. The shared revision also stores a
        # TestModel version and must survive; the inline-only revision is pruned.
        plan = build_retention_plan((TestModelInline,), keep=0)
        inline_only = self.revision("inline only")
        shared = self.revision("shared")
        self.assertEqual(plan.delete_revision_ids, {inline_only.pk})
        self.assertIn("out of scope", plan.revision_reasons[shared.pk])
        execute_retention_plan(plan)
        self.assertTrue(Revision.objects.filter(pk=shared.pk).exists())
        self.assertFalse(Revision.objects.filter(pk=inline_only.pk).exists())

    def testRetentionPlanDaysKeepsRecentRevisions(self):
        with reversion.create_revision():
            obj = TestModel.objects.create(name="v1")
            reversion.set_comment("ancient")
            reversion.set_date_created(timezone.now() - timedelta(days=90))
        with reversion.create_revision():
            obj.name = "v2"
            obj.save()
            reversion.set_comment("recent")

        plan = build_retention_plan((TestModel,), keep=0, days=30)
        ancient = self.revision("ancient")
        recent = self.revision("recent")
        self.assertEqual(plan.delete_revision_ids, {ancient.pk})
        self.assertIn(recent.pk, plan.kept_revision_ids)
        execute_retention_plan(plan)
        self.assertEqual(
            set(Revision.objects.values_list("comment", flat=True)),
            {"recent"},
        )
