from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import IntegrityError, connections, transaction

import reversion
from reversion.models import Revision, Version
from test_app.models import (
    TestMeta,
    TestModel,
    TestModelCustomObjectId,
    TestModelEscapePK,
    TestModelWithUniqueConstraint,
)
from test_app.tests.base import TestBase


class BulkCreateRevisionTest(TestBase):

    def setUp(self):
        super().setUp()
        reversion.register(TestModel)

    def testBulkCreateRevision(self):
        objs = reversion.bulk_create_revision([
            TestModel(name="v1"),
            TestModel(name="v2"),
        ])
        self.assertSingleRevision(objs)
        # Versions are created from the persisted state.
        self.assertEqual(
            Version.objects.get_for_object(objs[0]).get().field_dict["name"],
            "v1",
        )

    def testBulkCreateRevisionAutoPk(self):
        # SQLite supports RETURNING, so auto primary keys are set on the objects.
        objs = reversion.bulk_create_revision([TestModel(), TestModel()])
        self.assertTrue(all(obj.pk is not None for obj in objs))
        self.assertSingleRevision(objs)

    def testBulkCreateRevisionEmpty(self):
        self.assertEqual(reversion.bulk_create_revision([]), [])
        self.assertNoRevision()

    def testBulkCreateRevisionUserCommentMeta(self):
        user = User.objects.create(username="bulk")
        objs = reversion.bulk_create_revision(
            [TestModel(), TestModel()],
            user=user,
            comment="bulk import",
            meta=((TestMeta, {"name": "import-1"}),),
        )
        # The whole batch is stored as a single revision.
        self.assertEqual(Revision.objects.count(), 1)
        self.assertSingleRevision(
            objs, user=user, comment="bulk import", meta_names=("import-1",),
        )

    def testBulkCreateRevisionUnregistered(self):
        with self.assertRaises(reversion.RegistrationError):
            reversion.bulk_create_revision([User(username="x")])

    def testBulkCreateRevisionMixedModels(self):
        with self.assertRaises(reversion.RevisionManagementError):
            reversion.bulk_create_revision([TestModel(), TestModelEscapePK(name="a")])

    def testBulkCreateRevisionRollback(self):
        try:
            with transaction.atomic():
                reversion.bulk_create_revision([TestModel(), TestModel()])
                raise Exception("Boom!")
        except Exception:
            pass
        # Neither the business data nor the versions may be left behind.
        self.assertEqual(TestModel.objects.count(), 0)
        self.assertNoRevision()


class BulkCreateRevisionUniqueConstraintTest(TestBase):

    def setUp(self):
        super().setUp()
        reversion.register(TestModelWithUniqueConstraint)

    def testBulkCreateRevisionUniqueConstraintFailure(self):
        TestModelWithUniqueConstraint.objects.create(name="dup")
        with self.assertRaises(IntegrityError):
            reversion.bulk_create_revision([
                TestModelWithUniqueConstraint(name="ok"),
                TestModelWithUniqueConstraint(name="dup"),
            ])
        # The failed batch leaves no business data and no versions behind.
        self.assertEqual(TestModelWithUniqueConstraint.objects.count(), 1)
        self.assertNoRevision()


class BulkCreateRevisionManualPkTest(TestBase):

    def setUp(self):
        super().setUp()
        reversion.register(TestModelEscapePK)

    def testBulkCreateRevisionManualPk(self):
        objs = reversion.bulk_create_revision([
            TestModelEscapePK(name="a"),
            TestModelEscapePK(name="b"),
        ])
        self.assertSingleRevision(objs)

    def testBulkCreateRevisionIgnoreConflicts(self):
        TestModelEscapePK.objects.create(name="a")
        objs = reversion.bulk_create_revision(
            [TestModelEscapePK(name="a"), TestModelEscapePK(name="b")],
            ignore_conflicts=True,
        )
        # Only the actually inserted object is added to the revision.
        self.assertSingleRevision([objs[1]])
        self.assertEqual(Version.objects.get_for_object(objs[0]).count(), 0)

    def testBulkCreateRevisionIgnoreConflictsAllSkipped(self):
        TestModelEscapePK.objects.create(name="a")
        reversion.bulk_create_revision(
            [TestModelEscapePK(name="a")],
            ignore_conflicts=True,
        )
        self.assertNoRevision()

    def testBulkCreateRevisionNoReturningManualPk(self):
        # Databases without RETURNING support can still bulk create objects
        # with a caller-set primary key.
        features = connections["default"].features
        with patch.object(type(features), "can_return_rows_from_bulk_insert", False):
            objs = reversion.bulk_create_revision([TestModelEscapePK(name="a")])
        self.assertSingleRevision(objs)


class BulkCreateRevisionUnsupportedTest(TestBase):

    def setUp(self):
        super().setUp()
        reversion.register(TestModel)
        reversion.register(TestModelWithUniqueConstraint)

    def testBulkCreateRevisionIgnoreConflictsAutoPk(self):
        # With ignore_conflicts, the database does not report which rows were
        # inserted, so auto primary keys cannot be reconciled.
        with self.assertRaises(reversion.RevisionManagementError):
            reversion.bulk_create_revision(
                [TestModelWithUniqueConstraint(name="x")],
                ignore_conflicts=True,
            )

    def testBulkCreateRevisionNoReturningAutoPk(self):
        # Without RETURNING support, auto primary keys cannot be retrieved.
        features = connections["default"].features
        with patch.object(type(features), "can_return_rows_from_bulk_insert", False):
            with self.assertRaises(reversion.RevisionManagementError):
                reversion.bulk_create_revision([TestModel()])
        self.assertNoRevision()


class BulkUpdateRevisionTest(TestBase):

    def setUp(self):
        super().setUp()
        reversion.register(TestModel)
        reversion.register(TestModelCustomObjectId)

    def testBulkUpdateRevision(self):
        obj1 = TestModel.objects.create(name="v1")
        obj2 = TestModel.objects.create(name="v1")
        obj1.name = "v2"
        obj2.name = "v2"
        reversion.bulk_update_revision([obj1, obj2], fields=["name"])
        self.assertSingleRevision([obj1, obj2])
        self.assertEqual(
            Version.objects.get_for_object(obj1).get().field_dict["name"],
            "v2",
        )

    def testBulkUpdateRevisionPersistedState(self):
        obj = TestModelCustomObjectId.objects.create(slug="s1", name="v1")
        # Change a field that is not included in update_fields. The unsaved
        # in-memory value must not leak into the version.
        obj.slug = "s2-not-saved"
        obj.name = "v2"
        reversion.bulk_update_revision([obj], fields=["name"])
        version = Version.objects.get_for_object(obj).get()
        self.assertEqual(version.field_dict["name"], "v2")
        self.assertEqual(version.field_dict["slug"], "s1")

    def testBulkUpdateRevisionMissingObject(self):
        # Objects that do not exist in the database are not versioned.
        obj = TestModel(id=999, name="ghost")
        reversion.bulk_update_revision([obj], fields=["name"])
        self.assertNoRevision()

    def testBulkUpdateRevisionNoPk(self):
        with self.assertRaises(reversion.RevisionManagementError):
            reversion.bulk_update_revision([TestModel()], fields=["name"])

    def testBulkUpdateRevisionEmpty(self):
        self.assertEqual(reversion.bulk_update_revision([], fields=["name"]), [])
        self.assertNoRevision()

    def testBulkUpdateRevisionUserCommentMeta(self):
        user = User.objects.create(username="bulk")
        obj = TestModel.objects.create(name="v1")
        obj.name = "v2"
        reversion.bulk_update_revision(
            [obj],
            fields=["name"],
            user=user,
            comment="bulk rename",
            meta=((TestMeta, {"name": "rename-1"}),),
        )
        self.assertSingleRevision(
            [obj], user=user, comment="bulk rename", meta_names=("rename-1",),
        )

    def testBulkUpdateRevisionRollback(self):
        obj = TestModel.objects.create(name="v1")
        try:
            with transaction.atomic():
                obj.name = "v2"
                reversion.bulk_update_revision([obj], fields=["name"])
                raise Exception("Boom!")
        except Exception:
            pass
        self.assertEqual(TestModel.objects.get().name, "v1")
        self.assertNoRevision()


class BulkRevisionSignalTest(TestBase):

    def setUp(self):
        super().setUp()
        reversion.register(TestModel)

    def testBulkAndSignalSingleRevision(self):
        with reversion.create_revision():
            obj = TestModel.objects.create(name="signal")
            bulk_objs = reversion.bulk_create_revision([
                TestModel(name="bulk1"),
                TestModel(name="bulk2"),
            ])
        # The bulk objects are recorded exactly once, in the same revision as
        # the object saved via the regular signal path.
        self.assertEqual(Revision.objects.count(), 1)
        self.assertSingleRevision([obj] + bulk_objs)

    def testBulkObjectsNotRecordedBySignalPath(self):
        with reversion.create_revision():
            reversion.bulk_create_revision([TestModel(), TestModel()])
        # No duplicate versions from the post_save signal path.
        self.assertEqual(Version.objects.count(), 2)
