from django.db import IntegrityError, connection, transaction
from unittest.mock import patch

import reversion
from reversion.models import Revision, Version
from test_app.models import TestMeta, TestModel

from .base import TestBase, TestBaseTransaction, TestModelMixin


class BulkCreateTest(TestModelMixin, TestBase):

    def testBulkCreate(self):
        with reversion.create_revision():
            reversion.set_comment("bulk create")
            objs = reversion.bulk_create([
                TestModel(name="v1"),
                TestModel(name="v2"),
            ])
        self.assertEqual(len(objs), 2)
        self.assertTrue(all(obj.pk is not None for obj in objs))
        self.assertSingleRevision(objs, comment="bulk create")
        self.assertEqual(
            Version.objects.get_for_object(objs[0]).get().field_dict["name"],
            "v1",
        )
        self.assertEqual(
            Version.objects.get_for_object(objs[1]).get().field_dict["name"],
            "v2",
        )

    def testBulkCreateEmpty(self):
        with reversion.create_revision():
            result = reversion.bulk_create([])
        self.assertEqual(result, [])
        self.assertNoRevision()

    def testBulkCreateNoBlock(self):
        with self.assertRaises(reversion.RevisionManagementError):
            reversion.bulk_create([TestModel(name="v1")])
        self.assertEqual(TestModel.objects.count(), 0)

    def testBulkCreateUnregistered(self):
        reversion.unregister(TestModel)
        with reversion.create_revision():
            with self.assertRaises(reversion.RegistrationError):
                reversion.bulk_create([TestModel(name="v1")])
        self.assertNoRevision()
        self.assertEqual(TestModel.objects.count(), 0)

    def testBulkCreateIgnoreConflictsExplicitPks(self):
        existing = TestModel.objects.create(id=10, name="existing")
        with reversion.create_revision():
            objs = reversion.bulk_create([
                TestModel(id=10, name="conflict"),
                TestModel(id=11, name="inserted"),
            ], ignore_conflicts=True)
        existing.refresh_from_db()
        self.assertEqual(existing.name, "existing")
        self.assertSingleRevision((TestModel.objects.get(id=11),))
        self.assertEqual(Version.objects.get_for_object(existing).count(), 0)
        self.assertEqual(len(objs), 2)

    def testBulkCreateIgnoreConflictsAutoPksRejected(self):
        with reversion.create_revision():
            with self.assertRaises(reversion.RevisionManagementError):
                reversion.bulk_create(
                    [TestModel(name="v1")],
                    ignore_conflicts=True,
                )
        self.assertNoRevision()
        self.assertEqual(TestModel.objects.count(), 0)

    def testBulkCreateAutoPksWithoutReturningBackendRejected(self):
        with patch.object(
            type(connection.features),
            "can_return_rows_from_bulk_insert",
            False,
        ):
            with reversion.create_revision():
                with self.assertRaises(reversion.RevisionManagementError):
                    reversion.bulk_create([TestModel(name="v1")])
        self.assertNoRevision()
        self.assertEqual(TestModel.objects.count(), 0)

    def testBulkCreateExplicitPksWithoutReturningBackend(self):
        with patch.object(
            type(connection.features),
            "can_return_rows_from_bulk_insert",
            False,
        ):
            with reversion.create_revision():
                objs = reversion.bulk_create([TestModel(id=20, name="v1")])
        self.assertEqual(objs[0].pk, 20)
        self.assertSingleRevision(objs)

    def testBulkCreateSingleRevisionMetadata(self):
        with reversion.create_revision():
            reversion.set_comment("one revision")
            reversion.add_meta(TestMeta, name="meta v1")
            first = reversion.bulk_create([TestModel(name="v1")])
            second = reversion.bulk_create([TestModel(name="v2")])
        objects = tuple(first) + tuple(second)
        self.assertSingleRevision(objects, comment="one revision", meta_names=("meta v1",))

    def testBulkCreateOnOtherDatabase(self):
        with reversion.create_revision(using="mysql"):
            objs = reversion.bulk_create(
                [TestModel(name="v1")],
                model_db="mysql",
            )
        self.assertNoRevision()
        self.assertSingleRevision(objs, using="mysql", model_db="mysql")
        self.assertEqual(TestModel.objects.using("mysql").count(), 1)

    def testBulkCreateSignalPathDoesNotDuplicate(self):
        with reversion.create_revision():
            objs = reversion.bulk_create([TestModel(name="v1")])
            # Saving again inside the same revision must not add a second
            # Version row for the bulk-created object.
            objs[0].name = "v1 saved"
            objs[0].save()
        self.assertSingleRevision(objs)
        self.assertEqual(
            Version.objects.get_for_object(objs[0]).get().field_dict["name"],
            "v1 saved",
        )


class BulkUpdateTest(TestModelMixin, TestBase):

    def testBulkUpdate(self):
        first = TestModel.objects.create(name="v1")
        second = TestModel.objects.create(name="v1")
        first.name = "v2"
        second.name = "v3"
        with reversion.create_revision():
            reversion.set_comment("bulk update")
            rows_updated = reversion.bulk_update([first, second], ["name"])
        self.assertEqual(rows_updated, 2)
        self.assertSingleRevision((first, second), comment="bulk update")
        self.assertEqual(
            Version.objects.get_for_object(first).get().field_dict["name"],
            "v2",
        )
        self.assertEqual(
            Version.objects.get_for_object(second).get().field_dict["name"],
            "v3",
        )

    def testBulkUpdateVersionFromDatabaseState(self):
        obj = TestModel.objects.create(name="v1")
        obj.name = "v2"
        with reversion.create_revision():
            reversion.bulk_update([obj], ["name"])
            # Mutate the in-memory instance after the write. The version must
            # be built from the persisted row, not from this stale instance.
            obj.name = "not persisted"
        self.assertEqual(
            Version.objects.get_for_object(obj).get().field_dict["name"],
            "v2",
        )

    def testBulkUpdateEmpty(self):
        with reversion.create_revision():
            result = reversion.bulk_update([], ["name"])
        self.assertEqual(result, 0)
        self.assertNoRevision()

    def testBulkUpdateNoBlock(self):
        obj = TestModel.objects.create(name="v1")
        obj.name = "v2"
        with self.assertRaises(reversion.RevisionManagementError):
            reversion.bulk_update([obj], ["name"])
        obj.refresh_from_db()
        self.assertEqual(obj.name, "v1")

    def testBulkCreateAndBulkUpdateShareRevision(self):
        obj = TestModel.objects.create(name="v1")
        with reversion.create_revision():
            reversion.set_comment("mixed batch")
            created = reversion.bulk_create([TestModel(name="v1")])
            obj.name = "v2"
            reversion.bulk_update([obj], ["name"])
        self.assertSingleRevision((obj,) + tuple(created), comment="mixed batch")


class BulkRevisionRollbackTest(TestModelMixin, TestBaseTransaction):

    def testBulkCreateRollback(self):
        with self.assertRaises(Exception):
            with reversion.create_revision():
                reversion.bulk_create([
                    TestModel(name="v1"),
                    TestModel(name="v2"),
                ])
                raise Exception("Boom!")
        self.assertEqual(TestModel.objects.count(), 0)
        self.assertEqual(Revision.objects.count(), 0)
        self.assertEqual(Version.objects.count(), 0)

    def testBulkCreateUniqueConstraintFailure(self):
        TestModel.objects.create(id=10, name="existing")
        with self.assertRaises(IntegrityError):
            with reversion.create_revision():
                reversion.bulk_create([
                    TestModel(id=10, name="conflict"),
                    TestModel(id=11, name="would be orphan"),
                ])
        # No half batch: the non-conflicting row must not exist either.
        self.assertFalse(TestModel.objects.filter(id=11).exists())
        self.assertEqual(TestModel.objects.count(), 1)
        self.assertEqual(Revision.objects.count(), 0)
        self.assertEqual(Version.objects.count(), 0)

    def testBulkUpdateRollback(self):
        first = TestModel.objects.create(name="v1")
        second = TestModel.objects.create(name="v1")
        first.name = "v2"
        second.name = "v2"
        with self.assertRaises(Exception):
            with reversion.create_revision():
                reversion.bulk_update([first, second], ["name"])
                raise Exception("Boom!")
        self.assertEqual(TestModel.objects.filter(name="v1").count(), 2)
        self.assertEqual(Revision.objects.count(), 0)
        self.assertEqual(Version.objects.count(), 0)

    def testBulkUpdateThenUniqueConstraintFailure(self):
        first = TestModel.objects.create(name="v1")
        second = TestModel.objects.create(id=10, name="v1")
        first.name = "v2"
        with self.assertRaises(IntegrityError):
            with reversion.create_revision():
                reversion.bulk_update([first], ["name"])
                reversion.bulk_create([TestModel(id=10, name="conflict")])
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.name, "v1")
        self.assertEqual(second.name, "v1")
        self.assertEqual(TestModel.objects.count(), 2)
        self.assertEqual(Revision.objects.count(), 0)
        self.assertEqual(Version.objects.count(), 0)

    def testBulkCreateInSavepointRollback(self):
        # Business code can recover from a failing nested savepoint; the
        # enclosing revision must still commit cleanly without the failed
        # batch.
        with reversion.create_revision():
            reversion.bulk_create([TestModel(name="committed")])
            try:
                with transaction.atomic():
                    reversion.bulk_create([TestModel(id=1, name="conflict")])
            except IntegrityError:
                pass
        self.assertEqual(TestModel.objects.filter(name="committed").count(), 1)
        self.assertEqual(Revision.objects.count(), 1)
