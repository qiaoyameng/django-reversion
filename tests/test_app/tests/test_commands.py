import json
import os
import tempfile
from io import StringIO
from datetime import timedelta
from django.core.management import CommandError
from django.utils import timezone
import reversion
from test_app.models import TestMeta, TestModel
from test_app.tests.base import TestBase, TestModelMixin
from reversion.models import Revision


class CreateInitialRevisionsTest(TestModelMixin, TestBase):

    def testCreateInitialRevisions(self):
        obj = TestModel.objects.create()
        self.callCommand("createinitialrevisions")
        self.assertSingleRevision((obj,), comment="Initial version.")

    def testCreateInitialRevisionsAlreadyCreated(self):
        obj = TestModel.objects.create()
        self.callCommand("createinitialrevisions")
        self.callCommand("createinitialrevisions")
        self.assertSingleRevision((obj,), comment="Initial version.")


class CreateInitialRevisionsAppLabelTest(TestModelMixin, TestBase):

    def testCreateInitialRevisionsAppLabel(self):
        obj = TestModel.objects.create()
        self.callCommand("createinitialrevisions", "test_app")
        self.assertSingleRevision((obj,), comment="Initial version.")

    def testCreateInitialRevisionsAppLabelMissing(self):
        with self.assertRaises(CommandError):
            self.callCommand("createinitialrevisions", "boom")

    def testCreateInitialRevisionsModel(self):
        obj = TestModel.objects.create()
        self.callCommand("createinitialrevisions", "test_app.TestModel")
        self.assertSingleRevision((obj,), comment="Initial version.")

    def testCreateInitialRevisionsModelMissing(self):
        with self.assertRaises(CommandError):
            self.callCommand("createinitialrevisions", "test_app.boom")

    def testCreateInitialRevisionsModelMissingApp(self):
        with self.assertRaises(CommandError):
            self.callCommand("createinitialrevisions", "boom.boom")

    def testCreateInitialRevisionsModelNotRegistered(self):
        TestModel.objects.create()
        self.callCommand("createinitialrevisions", "auth.User")
        self.assertNoRevision()


class CreateInitialRevisionsDbTest(TestModelMixin, TestBase):
    databases = {"default", "mysql", "postgres"}

    def testCreateInitialRevisionsDb(self):
        obj = TestModel.objects.create()
        self.callCommand("createinitialrevisions", using="postgres")
        self.assertNoRevision()
        self.assertSingleRevision((obj,), comment="Initial version.", using="postgres")

    def testCreateInitialRevisionsDbMySql(self):
        obj = TestModel.objects.create()
        self.callCommand("createinitialrevisions", using="mysql")
        self.assertNoRevision()
        self.assertSingleRevision((obj,), comment="Initial version.", using="mysql")


class CreateInitialRevisionsModelDbTest(TestModelMixin, TestBase):
    databases = {"default", "postgres"}

    def testCreateInitialRevisionsModelDb(self):
        obj = TestModel.objects.db_manager("postgres").create()
        self.callCommand("createinitialrevisions", model_db="postgres")
        self.assertSingleRevision((obj,), comment="Initial version.", model_db="postgres")


class CreateInitialRevisionsCommentTest(TestModelMixin, TestBase):

    def testCreateInitialRevisionsComment(self):
        obj = TestModel.objects.create()
        self.callCommand("createinitialrevisions", comment="comment v1")
        self.assertSingleRevision((obj,), comment="comment v1")


class CreateInitialRevisionsMetaTest(TestModelMixin, TestBase):
    def testCreateInitialRevisionsComment(self):
        obj = TestModel.objects.create()
        meta_name = "meta name"
        meta = json.dumps({"test_app.TestMeta": {"name": meta_name}})
        self.callCommand("createinitialrevisions", "--meta", meta)
        self.assertSingleRevision((obj,), meta_names=(meta_name, ), comment="Initial version.")


class DeleteRevisionsTest(TestModelMixin, TestBase):

    def testDeleteRevisions(self):
        with reversion.create_revision():
            TestModel.objects.create()
        self.callCommand("deleterevisions")
        self.assertNoRevision()


class DeleteRevisionsAppLabelTest(TestModelMixin, TestBase):

    def testDeleteRevisionsAppLabel(self):
        with reversion.create_revision():
            TestModel.objects.create()
        self.callCommand("deleterevisions", "test_app")
        self.assertNoRevision()

    def testDeleteRevisionsAppLabelMissing(self):
        with self.assertRaises(CommandError):
            self.callCommand("deleterevisions", "boom")

    def testDeleteRevisionsModel(self):
        with reversion.create_revision():
            TestModel.objects.create()
        self.callCommand("deleterevisions", "test_app.TestModel")
        self.assertNoRevision()

    def testDeleteRevisionsModelMissing(self):
        with self.assertRaises(CommandError):
            self.callCommand("deleterevisions", "test_app.boom")

    def testDeleteRevisionsModelMissingApp(self):
        with self.assertRaises(CommandError):
            self.callCommand("deleterevisions", "boom.boom")

    def testDeleteRevisionsModelNotRegistered(self):
        with reversion.create_revision():
            obj = TestModel.objects.create()
        self.callCommand("deleterevisions", "auth.User")
        self.assertSingleRevision((obj,))


class DeleteRevisionsDbTest(TestModelMixin, TestBase):
    databases = {"default", "mysql", "postgres"}

    def testDeleteRevisionsDb(self):
        with reversion.create_revision(using="postgres"):
            TestModel.objects.create()
        self.callCommand("deleterevisions", using="postgres")
        self.assertNoRevision(using="postgres")

    def testDeleteRevisionsDbMySql(self):
        with reversion.create_revision(using="mysql"):
            TestModel.objects.create()
        self.callCommand("deleterevisions", using="mysql")
        self.assertNoRevision(using="mysql")

    def testDeleteRevisionsDbNoMatch(self):
        with reversion.create_revision():
            obj = TestModel.objects.create()
        self.callCommand("deleterevisions", using="postgres")
        self.assertSingleRevision((obj,))


class DeleteRevisionsModelDbTest(TestModelMixin, TestBase):
    databases = {"default", "postgres"}

    def testDeleteRevisionsModelDb(self):
        with reversion.create_revision():
            TestModel.objects.db_manager("postgres").create()
        self.callCommand("deleterevisions", model_db="postgres")
        self.assertNoRevision(using="postgres")


class DeleteRevisionsDaysTest(TestModelMixin, TestBase):

    def testDeleteRevisionsDays(self):
        date_created = timezone.now() - timedelta(days=20)
        with reversion.create_revision():
            TestModel.objects.create()
            reversion.set_date_created(date_created)
        self.callCommand("deleterevisions", days=19)
        self.assertNoRevision()

    def testDeleteRevisionsDaysNoMatch(self):
        date_created = timezone.now() - timedelta(days=20)
        with reversion.create_revision():
            obj = TestModel.objects.create()
            reversion.set_date_created(date_created)
        self.callCommand("deleterevisions", days=21)
        self.assertSingleRevision((obj,), date_created=date_created)


class DeleteRevisionsKeepTest(TestModelMixin, TestBase):

    def testDeleteRevisionsKeep(self):
        with reversion.create_revision():
            obj_1 = TestModel.objects.create()
            reversion.set_comment("obj_1 v1")
        with reversion.create_revision():
            obj_1.save()
            reversion.set_comment("obj_1 v2")
        with reversion.create_revision():
            obj_2 = TestModel.objects.create()
            reversion.set_comment("obj_2 v1")
        with reversion.create_revision():
            obj_2.save()
            reversion.set_comment("obj_2 v2")
        with reversion.create_revision():
            obj_3 = TestModel.objects.create()
        self.callCommand("deleterevisions", keep=1)
        self.assertSingleRevision((obj_1,), comment="obj_1 v2")
        self.assertSingleRevision((obj_2,), comment="obj_2 v2")
        self.assertSingleRevision((obj_3,))


class RetainRevisionsCommandTest(TestModelMixin, TestBase):

    def _history(self, obj):
        with reversion.create_revision():
            obj.name = "v1"
            obj.save()
            reversion.set_comment("old")
        with reversion.create_revision():
            obj.name = "RELEASE"
            obj.save()
            reversion.set_comment("RELEASE marker")
        with reversion.create_revision():
            obj.name = "v3"
            obj.save()
            reversion.set_comment("wip")
        with reversion.create_revision():
            obj.name = "v4"
            obj.save()
            reversion.set_comment("current")

    def testRetainRevisionsDryRunDeletesNothing(self):
        obj = TestModel.objects.create()
        self._history(obj)
        stdout = StringIO()
        self.callCommand(
            "retainrevisions", "test_app.TestModel",
            keep=1, marker=["RELEASE"], verbosity=2, stdout=stdout,
        )
        output = stdout.getvalue()
        self.assertIn("Dry run", output)
        self.assertIn("DELETE revision", output)
        self.assertEqual(Revision.objects.count(), 4)

    def testRetainRevisionsExecute(self):
        obj = TestModel.objects.create()
        self._history(obj)
        stdout = StringIO()
        self.callCommand(
            "retainrevisions", "test_app.TestModel",
            keep=1, marker=["RELEASE"], execute=True, verbosity=1, stdout=stdout,
        )
        self.assertIn("Deleted 2 revision(s).", stdout.getvalue())
        comments = set(Revision.objects.values_list("comment", flat=True))
        self.assertEqual(comments, {"RELEASE marker", "current"})
        # The marked recovery point still reverts after pruning.
        release = Revision.objects.get(comment="RELEASE marker")
        release.revert()
        obj.refresh_from_db()
        self.assertEqual(obj.name, "RELEASE")

    def testRetainRevisionsPlanFileDetectsDrift(self):
        obj = TestModel.objects.create()
        self._history(obj)
        plan_fd, plan_path = tempfile.mkstemp(suffix=".json")
        os.close(plan_fd)
        try:
            self.callCommand(
                "retainrevisions", "test_app.TestModel",
                keep=1, marker=["RELEASE"], plan_file=plan_path, verbosity=0,
            )
            # New revision written after the preview.
            with reversion.create_revision():
                obj.name = "v5"
                obj.save()
                reversion.set_comment("after preview")
            with self.assertRaises(CommandError):
                self.callCommand(
                    "retainrevisions", "test_app.TestModel",
                    keep=1, marker=["RELEASE"],
                    execute=True, plan_file=plan_path, verbosity=0,
                )
            # Nothing deleted despite the rejected execution.
            self.assertEqual(Revision.objects.count(), 5)
            # Re-running the dry run refreshes the plan and execution succeeds.
            self.callCommand(
                "retainrevisions", "test_app.TestModel",
                keep=1, marker=["RELEASE"], plan_file=plan_path, verbosity=0,
            )
            self.callCommand(
                "retainrevisions", "test_app.TestModel",
                keep=1, marker=["RELEASE"],
                execute=True, plan_file=plan_path, verbosity=0,
            )
            comments = set(Revision.objects.values_list("comment", flat=True))
            self.assertEqual(
                comments,
                {"RELEASE marker", "after preview"},
            )
        finally:
            os.remove(plan_path)

    def testRetainRevisionsKeepsMetaReferencedRevision(self):
        obj = TestModel.objects.create()
        self._history(obj)
        with reversion.create_revision():
            obj.name = "meta edit"
            obj.save()
            reversion.add_meta(TestMeta, name="audit")
            reversion.set_comment("meta only")
        self.callCommand(
            "retainrevisions", "test_app.TestModel",
            keep=1, execute=True, verbosity=0,
        )
        self.assertTrue(
            Revision.objects.filter(comment="meta only").exists()
        )
