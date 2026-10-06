from datetime import timedelta

from django.utils import timezone
import reversion
from reversion.models import Revision, Version
from reversion.retention import (
    RetentionPlanStaleError,
    RetentionPolicy,
    build_retention_plan,
)
from test_app.models import TestModel, TestModelRelated, TestMeta
from test_app.tests.base import TestBase, TestModelMixin


class RetentionTestMixin(TestModelMixin):

    def buildPlan(self, **kwargs):
        return build_retention_plan(RetentionPolicy(
            models=reversion.get_registered_models(),
            **kwargs
        ))


class RetentionSharedRevisionTest(RetentionTestMixin, TestBase):
    # A revision shared by multiple objects can only be deleted when no
    # retained version needs it anymore.

    def testSharedRevisionKeptAsDependency(self):
        with reversion.create_revision():
            obj_1 = TestModel.objects.create(name="obj_1 v1")
        with reversion.create_revision():
            obj_1.name = "obj_1 v2"
            obj_1.save()
            obj_2 = TestModel.objects.create(name="obj_2 v1")
        with reversion.create_revision():
            obj_1.name = "obj_1 v3"
            obj_1.save()
        plan = self.buildPlan(keep_last=1)
        plan.execute()
        # The first revision is deleted, the shared revision is kept whole.
        self.assertEqual(Revision.objects.count(), 2)
        # obj_1 v2 is kept as a dependency of obj_2 v1, not as a keep target.
        obj_1_versions = Version.objects.get_for_object(obj_1)
        self.assertEqual(obj_1_versions.count(), 2)
        self.assertEqual(
            plan.keep_version_reasons[obj_1_versions[1].pk],
            ["shares revision #{revision_id} with a kept version".format(
                revision_id=obj_1_versions[1].revision_id,
            )],
        )
        self.assertEqual(Version.objects.get_for_object(obj_2).count(), 1)

    def testSharedRevisionRevertAfterCleanup(self):
        with reversion.create_revision():
            obj_1 = TestModel.objects.create(name="obj_1 v1")
        with reversion.create_revision():
            obj_1.name = "obj_1 v2"
            obj_1.save()
            obj_2 = TestModel.objects.create(name="obj_2 v1")
        with reversion.create_revision():
            obj_1.name = "obj_1 v3"
            obj_1.save()
        self.buildPlan(keep_last=1).execute()
        # The retained restore point actually reverts.
        revision = Version.objects.get_for_object(obj_2).get().revision
        revision.revert()
        obj_1.refresh_from_db()
        obj_2.refresh_from_db()
        self.assertEqual(obj_1.name, "obj_1 v2")
        self.assertEqual(obj_2.name, "obj_2 v1")


class RetentionDeletedObjectTest(RetentionTestMixin, TestBase):
    # Deleting by age would remove the only recoverable state of a
    # deleted object. keep_last retains it, and it actually reverts.

    def testDeletedObjectRestorable(self):
        with reversion.create_revision():
            obj = TestModel.objects.create(name="obj v1")
        with reversion.create_revision():
            obj.name = "obj v2"
            obj.save()
        obj_id = obj.pk
        obj.delete()
        self.buildPlan(keep_last=1).execute()
        versions = Version.objects.get_for_object_reference(TestModel, obj_id)
        self.assertEqual(versions.count(), 1)
        self.assertFalse(TestModel.objects.filter(pk=obj_id).exists())
        versions[0].revert()
        self.assertEqual(TestModel.objects.get(pk=obj_id).name, "obj v2")


class RetentionDeletedReferencedObjectTest(RetentionTestMixin, TestBase):
    # A deleted object referenced by a retained version must have its
    # own version retained for the restore point to actually revert.

    def setUp(self):
        super().setUp()
        reversion.register(TestModelRelated)

    def testDeletedReferencedObjectRestorable(self):
        with reversion.create_revision():
            obj = TestModel.objects.create(name="obj v0")
        with reversion.create_revision():
            related = TestModelRelated.objects.create(name="related v1")
            obj.name = "obj v1"
            obj.related.add(related)
            obj.save()
            reversion.add_to_revision(obj)
        with reversion.create_revision():
            obj.name = "obj v2"
            obj.save()
        related_id = related.pk
        related.delete()
        self.buildPlan(keep_last=1).execute()
        # Only the first revision is deleted.
        self.assertEqual(Revision.objects.count(), 2)
        # The retained restore point restores the deleted referenced object.
        revision = Version.objects.get_for_object_reference(
            TestModelRelated, related_id,
        ).get().revision
        revision.revert()
        self.assertEqual(TestModelRelated.objects.get(pk=related_id).name, "related v1")
        obj.refresh_from_db()
        self.assertEqual(obj.name, "obj v1")
        self.assertEqual(
            list(obj.related.values_list("pk", flat=True)),
            [related_id],
        )


class RetentionStalePlanTest(RetentionTestMixin, TestBase):
    # Versions written after the plan was built must not be deleted.

    def testStalePlanAborts(self):
        with reversion.create_revision():
            obj = TestModel.objects.create(name="obj v1")
        plan = self.buildPlan()
        # A new version is written after the plan (preview) was built.
        with reversion.create_revision():
            obj.name = "obj v2"
            obj.save()
        with self.assertRaises(RetentionPlanStaleError):
            plan.execute()
        # Nothing was deleted.
        self.assertEqual(Revision.objects.count(), 2)
        self.assertEqual(Version.objects.get_for_object(obj).count(), 2)

    def testRebuiltPlanDeletes(self):
        with reversion.create_revision():
            obj = TestModel.objects.create(name="obj v1")
        plan = self.buildPlan()
        with reversion.create_revision():
            obj.name = "obj v2"
            obj.save()
        with self.assertRaises(RetentionPlanStaleError):
            plan.execute()
        # Rebuilding the plan on the changed version set works, and the
        # newly written version is kept by the retention policy.
        self.buildPlan(keep_last=1).execute()
        self.assertEqual(Revision.objects.count(), 1)
        self.assertEqual(Version.objects.get_for_object(obj).get().field_dict["name"], "obj v2")

    def testStalePlanOnMetadataChange(self):
        with reversion.create_revision():
            TestModel.objects.create()
        plan = self.buildPlan()
        revision = Revision.objects.get()
        TestMeta.objects.create(revision=revision, name="meta v1")
        with self.assertRaises(RetentionPlanStaleError):
            plan.execute()
        self.assertEqual(Revision.objects.count(), 1)


class RetentionMetadataTest(RetentionTestMixin, TestBase):
    # A revision referenced by metadata cannot be deleted.

    def testMetadataReferencedRevisionKept(self):
        with reversion.create_revision():
            TestModel.objects.create()
            reversion.add_meta(TestMeta, name="meta v1")
        plan = self.buildPlan()
        revision = Revision.objects.get()
        self.assertEqual(plan.delete_revision_ids, [])
        self.assertEqual(
            plan.keep_revision_reasons[revision.pk],
            ["referenced by revision metadata"],
        )
        plan.execute()
        self.assertEqual(Revision.objects.count(), 1)
        self.assertEqual(TestMeta.objects.count(), 1)


class RetentionKeepCommentsTest(RetentionTestMixin, TestBase):
    # Tagged restore points are kept, and actually revert.

    def testKeepComments(self):
        with reversion.create_revision():
            obj = TestModel.objects.create(name="obj v1")
            reversion.set_comment("restore point: release-1.0")
        with reversion.create_revision():
            obj.name = "obj v2"
            obj.save()
        with reversion.create_revision():
            obj.name = "obj v3"
            obj.save()
        plan = self.buildPlan(keep_comments=("release-1.0",))
        plan.execute()
        self.assertEqual(Revision.objects.count(), 1)
        revision = Revision.objects.get()
        self.assertEqual(revision.comment, "restore point: release-1.0")
        revision.revert()
        obj.refresh_from_db()
        self.assertEqual(obj.name, "obj v1")

    def testKeepCommentsNoMatch(self):
        with reversion.create_revision():
            TestModel.objects.create()
            reversion.set_comment("some comment")
        self.buildPlan(keep_comments=("release",)).execute()
        self.assertEqual(Revision.objects.count(), 0)


class RetentionDaysTest(RetentionTestMixin, TestBase):

    def testDays(self):
        date_created = timezone.now() - timedelta(days=20)
        with reversion.create_revision():
            TestModel.objects.create()
            reversion.set_date_created(date_created)
        with reversion.create_revision():
            TestModel.objects.create()
        self.buildPlan(days=19).execute()
        self.assertEqual(Revision.objects.count(), 1)


class RetentionDeleteAllTest(RetentionTestMixin, TestBase):

    def testDeleteAll(self):
        with reversion.create_revision():
            TestModel.objects.create()
        plan = self.buildPlan()
        self.assertEqual(len(plan.delete_revision_ids), 1)
        self.assertEqual(plan.get_delete_versions().count(), 1)
        plan.execute()
        self.assertEqual(Revision.objects.count(), 0)
        self.assertEqual(Version.objects.count(), 0)
