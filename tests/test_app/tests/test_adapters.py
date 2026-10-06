import json
from unittest import TestCase

from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.db import connections
from django.utils import timezone

import reversion
from reversion.adapters import (
    VersionAdapterContext,
    get_adapter_chain,
    register_adapter,
    unregister_adapter,
)
from reversion.models import Revision, Version
from test_app.models import TestModelRelated, TestModelVersionAdapter


class VersionAdapterTest(TestCase):

    @staticmethod
    def split_name(records, context):
        assert isinstance(context, VersionAdapterContext)
        assert context.model is TestModelVersionAdapter
        assert context.serializer_format == "json"
        for record in records:
            fields = record["fields"]
            first_name, last_name = fields.pop("name").rsplit(" ", 1)
            fields["first_name"] = first_name
            fields["last_name"] = last_name
            fields["related_count"] = int(fields.pop("related_count"))
            fields["related"] = int(fields.pop("related_id"))
        return records

    @staticmethod
    def fail_at_step_2(records, context):
        raise RuntimeError("boom")

    def setUp(self):
        super().setUp()
        connections.databases["memory"] = dict(connections.databases["default"])
        connections.databases["memory"]["NAME"] = ":memory:"
        connections.databases["memory"]["TEST"] = {"NAME": ":memory:"}
        connection = connections["memory"]
        connection.connect()
        self.addCleanup(connection.close)
        call_command(
            "migrate",
            database="memory",
            verbosity=0,
        )
        reversion.register(TestModelVersionAdapter, format_version=2)
        register_adapter(TestModelVersionAdapter, 1, 2, self.split_name)

    def tearDown(self):
        for model in list(reversion.get_registered_models()):
            reversion.unregister(model)
        for source_version, target_version in ((1, 2), (2, 3), (1, 3), (3, 4)):
            try:
                unregister_adapter(TestModelVersionAdapter, source_version, target_version)
            except reversion.RegistrationError:
                pass
        if "memory" in connections.databases:
            del connections.databases["memory"]
        super().tearDown()

    def create_old_version(self):
        created = timezone.now()
        revision = Revision.objects.using("memory").create(
            date_created=created,
            comment="old snapshot",
        )
        related = TestModelRelated.objects.using("memory").create(name="related")
        content_type = ContentType.objects.db_manager("memory").get_for_model(
            TestModelVersionAdapter
        )
        serialized_data = json.dumps([{
            "model": "test_app.testmodelversionadapter",
            "pk": 1,
            "fields": {
                "name": "Ada Lovelace",
                "related_count": "2",
                "related_id": str(related.pk),
            },
        }])
        version = Version.objects.using("memory").create(
            revision=revision,
            object_id="1",
            content_type=content_type,
            db="memory",
            format="json",
            serialized_data=serialized_data,
            object_repr="old format object",
        )
        return version, revision, related, serialized_data, created

    def testPreviewAndRevertUseSameConversion(self):
        version, revision, related, original_data, created = self.create_old_version()

        self.assertEqual(version.field_dict, {
            "id": 1,
            "first_name": "Ada",
            "last_name": "Lovelace",
            "related_count": 2,
            "related_id": related.pk,
        })

        version.revert()
        obj = TestModelVersionAdapter.objects.using("memory").get(pk=1)
        self.assertEqual(obj.first_name, "Ada")
        self.assertEqual(obj.last_name, "Lovelace")
        self.assertEqual(obj.related_count, 2)
        self.assertEqual(obj.related_id, related.pk)

        version.refresh_from_db()
        self.assertEqual(version.format, "json")
        self.assertEqual(version.serialized_data, original_data)
        self.assertEqual(version.revision.comment, "old snapshot")
        self.assertEqual(version.revision.date_created, created)
        self.assertEqual(version.revision_id, revision.pk)
        self.assertEqual(Version.objects.using("memory").count(), 1)
        self.assertEqual(Revision.objects.using("memory").count(), 1)

    def testCurrentSnapshotUsesDeclaredFormatVersion(self):
        with reversion.create_revision(using="memory", manage_manually=True):
            related = TestModelRelated.objects.using("memory").create(name="related")
            obj = TestModelVersionAdapter.objects.using("memory").create(
                first_name="Grace",
                last_name="Hopper",
                related_count=3,
                related=related,
            )
            reversion.add_to_revision(obj, model_db="memory")

        version = Version.objects.using("memory").get_for_object(obj, model_db="memory").get()
        self.assertEqual(version.format, "json@2")
        self.assertEqual(version.field_dict, {
            "id": obj.pk,
            "first_name": "Grace",
            "last_name": "Hopper",
            "related_count": 3,
            "related_id": related.pk,
        })

    def testCallbackReceivesPureDataAndVersionInfo(self):
        seen = {}

        def inspect_records(records, context):
            seen["records"] = json.loads(json.dumps(records))
            seen["context"] = context
            self.assertNotIn("db", context._fields)
            self.assertNotIn("revision", context._fields)
            for record in records:
                fields = record["fields"]
                first_name, last_name = fields.pop("name").rsplit(" ", 1)
                fields["first_name"] = first_name
                fields["last_name"] = last_name
                fields["related_count"] = int(fields.pop("related_count"))
                fields["related"] = int(fields.pop("related_id"))
            return records

        unregister_adapter(TestModelVersionAdapter, 1, 2)
        register_adapter(TestModelVersionAdapter, 1, 2, inspect_records)
        version, _, _, _, _ = self.create_old_version()

        self.assertEqual(version.field_dict["first_name"], "Ada")
        self.assertEqual(seen["records"][0]["fields"]["name"], "Ada Lovelace")
        self.assertEqual(seen["context"].source_version, 1)
        self.assertEqual(seen["context"].target_version, 2)

    def testFailureMentionsSpecificStep(self):
        reversion.unregister(TestModelVersionAdapter)
        reversion.register(TestModelVersionAdapter, format_version=3)
        unregister_adapter(TestModelVersionAdapter, 1, 2)
        register_adapter(TestModelVersionAdapter, 1, 2, self.split_name)
        register_adapter(TestModelVersionAdapter, 2, 3, self.fail_at_step_2)
        version, _, _, original_data, _ = self.create_old_version()

        with self.assertRaises(reversion.RevertError) as cm:
            version.field_dict
        self.assertIn("2 -> 3", str(cm.exception))
        self.assertIn("boom", str(cm.exception))

        version.refresh_from_db()
        self.assertEqual(version.serialized_data, original_data)

    def testCurrentDeserializationChecksRenamedField(self):
        unregister_adapter(TestModelVersionAdapter, 1, 2)
        register_adapter(TestModelVersionAdapter, 1, 2, lambda records, context: records)
        version, _, _, original_data, _ = self.create_old_version()

        with self.assertRaises(reversion.RevertError) as cm:
            version.field_dict
        self.assertIn("1 -> 2", str(cm.exception))
        version.refresh_from_db()
        self.assertEqual(version.serialized_data, original_data)

    def testCurrentDeserializationChecksTypeAndRelationId(self):
        def bad_type(records, context):
            fields = records[0]["fields"]
            fields.pop("name")
            fields["first_name"] = "Ada"
            fields["last_name"] = "Lovelace"
            fields["related_count"] = "not-an-int"
            fields["related"] = "not-an-id"
            return records

        unregister_adapter(TestModelVersionAdapter, 1, 2)
        register_adapter(TestModelVersionAdapter, 1, 2, bad_type)
        version, _, _, _, _ = self.create_old_version()

        with self.assertRaises(reversion.RevertError) as cm:
            version.field_dict
        self.assertIn("1 -> 2", str(cm.exception))


class VersionAdapterChainTest(TestCase):

    def tearDown(self):
        for source_version, target_version in ((1, 3), (3, 4), (2, 1)):
            try:
                unregister_adapter(TestModelVersionAdapter, source_version, target_version)
            except reversion.RegistrationError:
                pass
        super().tearDown()

    def testRejectsMissingStep(self):
        register_adapter(TestModelVersionAdapter, 1, 3, lambda records, context: records)
        register_adapter(TestModelVersionAdapter, 3, 4, lambda records, context: records)

        with self.assertRaises(reversion.RevertError) as cm:
            get_adapter_chain(TestModelVersionAdapter, 1, 2)
        self.assertIn("version 1", str(cm.exception))

    def testRejectsDowngradeCycle(self):
        with self.assertRaises(reversion.RegistrationError):
            register_adapter(TestModelVersionAdapter, 2, 1, lambda records, context: records)

    def testVersionMismatchRequiresAdapterChain(self):
        reversion.register(TestModelVersionAdapter, format_version=2)
        try:
            with self.assertRaises(reversion.RevertError) as cm:
                get_adapter_chain(TestModelVersionAdapter, 1, 2)
            self.assertIn("version 1", str(cm.exception))
        finally:
            reversion.unregister(TestModelVersionAdapter)
