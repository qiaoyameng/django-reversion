import json

from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

import reversion
from reversion.adapters import adapt_serialized_data
from reversion.models import Revision, Version
from test_app.models import TestModelRelated, TestModelSplitName
from test_app.tests.base import TestBase


def make_old_serialized_data(obj_pk, related_legacy_id):
    """A v1 snapshot: one ``name`` field, ``age`` as a string, legacy related id."""
    return json.dumps([{
        "model": "test_app.testmodelsplitname",
        "pk": obj_pk,
        "fields": {
            "name": "ada lovelace",
            "age": "36",
            "related": related_legacy_id,
        },
    }])


def make_v2_serialized_data(obj_pk, related_pk):
    """A v2 snapshot: split name, int age, but pre-rename field names."""
    return json.dumps([{
        "model": "test_app.testmodelsplitname",
        "pk": obj_pk,
        "fields": {
            "first": "ada",
            "surname": "lovelace",
            "age": 36,
            "related": related_pk,
        },
    }])


def detect_data_version(data):
    fields = data[0]["fields"]
    if "name" in fields:
        return 1
    if "first" in fields:
        return 2
    return 3


class DataAdapterMixin(TestBase):

    def setUp(self):
        super().setUp()
        reversion.register(TestModelSplitName)
        self.related = TestModelRelated.objects.create(name="related v1")
        # Maps legacy related ids to current pks, as pure data.
        self.related_id_map = {1001: self.related.pk}

        def adapt_v1_to_v2(data, info):
            fields = data[0]["fields"]
            first, _, surname = fields.pop("name").partition(" ")
            fields["first"] = first
            fields["surname"] = surname
            fields["age"] = int(fields["age"])
            fields["related"] = self.related_id_map[fields["related"]]

        def adapt_v2_to_v3(data, info):
            fields = data[0]["fields"]
            fields["first_name"] = fields.pop("first")
            fields["last_name"] = fields.pop("surname")

        # Registered out of chain order on purpose: resolution must follow
        # the from/to links, not the registration order.
        reversion.register_data_adapter(TestModelSplitName, 2, 3, adapt_v2_to_v3)
        reversion.register_data_adapter(TestModelSplitName, 1, 2, adapt_v1_to_v2)
        reversion.set_data_version(TestModelSplitName, 3, detect=detect_data_version)

    def tearDown(self):
        reversion.unregister_data_adapters(TestModelSplitName)
        super().tearDown()

    def createOldVersion(self, serialized_data, object_id=1):
        revision = Revision.objects.create(
            date_created=timezone.now(),
            comment="old snapshot",
        )
        return Version.objects.create(
            revision=revision,
            object_id=str(object_id),
            content_type=ContentType.objects.get_for_model(TestModelSplitName),
            db="default",
            format="json",
            serialized_data=serialized_data,
            object_repr="TestModelSplitName object",
        )


class DataAdapterPreviewTest(DataAdapterMixin):

    def testFieldDictAdaptsOldVersion(self):
        version = self.createOldVersion(make_old_serialized_data(1, 1001))
        field_dict = version.field_dict
        self.assertEqual(field_dict["first_name"], "ada")
        self.assertEqual(field_dict["last_name"], "lovelace")
        self.assertEqual(field_dict["age"], 36)
        self.assertEqual(field_dict["related_id"], self.related.pk)

    def testFieldDictAdaptsMidChainVersion(self):
        version = self.createOldVersion(make_v2_serialized_data(1, self.related.pk))
        field_dict = version.field_dict
        self.assertEqual(field_dict["first_name"], "ada")
        self.assertEqual(field_dict["last_name"], "lovelace")
        self.assertEqual(field_dict["age"], 36)
        self.assertEqual(field_dict["related_id"], self.related.pk)


class DataAdapterRevertTest(DataAdapterMixin):

    def testRevertAdaptsOldVersion(self):
        version = self.createOldVersion(make_old_serialized_data(1, 1001))
        version.revert()
        obj = TestModelSplitName.objects.get(pk=1)
        self.assertEqual(obj.first_name, "ada")
        self.assertEqual(obj.last_name, "lovelace")
        self.assertEqual(obj.age, 36)
        self.assertEqual(obj.related_id, self.related.pk)

    def testRevertDoesNotModifyStoredData(self):
        serialized_data = make_old_serialized_data(1, 1001)
        version = self.createOldVersion(serialized_data)
        revision_date_created = version.revision.date_created
        # Preview and revert both run the conversion.
        version.field_dict
        version.revert()
        # The stored serialized data and revision metadata are untouched.
        version.refresh_from_db()
        self.assertEqual(version.serialized_data, serialized_data)
        self.assertEqual(version.revision.comment, "old snapshot")
        self.assertEqual(version.revision.date_created, revision_date_created)


class DataAdapterChainTest(DataAdapterMixin):

    def testMissingStep(self):
        reversion.unregister_data_adapters(TestModelSplitName)

        def adapt_v1_to_v2(data, info):
            pass

        reversion.register_data_adapter(TestModelSplitName, 1, 2, adapt_v1_to_v2)
        reversion.set_data_version(TestModelSplitName, 3, detect=detect_data_version)
        version = self.createOldVersion(make_old_serialized_data(1, 1001))
        with self.assertRaises(reversion.DataAdapterError) as cm:
            version.field_dict
        self.assertIn("2", str(cm.exception))
        self.assertIn("3", str(cm.exception))

    def testCycle(self):
        def adapt_v3_to_v2(data, info):
            pass

        reversion.register_data_adapter(TestModelSplitName, 3, 2, adapt_v3_to_v2)
        # Current version 4 is unreachable: 2 -> 3 -> 2 loops forever.
        reversion.set_data_version(TestModelSplitName, 4, detect=detect_data_version)
        version = self.createOldVersion(make_old_serialized_data(1, 1001))
        with self.assertRaises(reversion.DataAdapterError) as cm:
            version.field_dict
        self.assertIn("cycle", str(cm.exception).lower())

    def testCallbackReceivesPureDataAndVersionInfo(self):
        calls = []

        def adapt_v1_to_v2(data, info):
            calls.append((data, info))
            # Delegate to the already-registered behavior via pure data.
            fields = data[0]["fields"]
            first, _, surname = fields.pop("name").partition(" ")
            fields["first"] = first
            fields["surname"] = surname
            fields["age"] = int(fields["age"])
            fields["related"] = self.related_id_map[fields["related"]]

        reversion.unregister_data_adapters(TestModelSplitName)

        def adapt_v2_to_v3(data, info):
            fields = data[0]["fields"]
            fields["first_name"] = fields.pop("first")
            fields["last_name"] = fields.pop("surname")

        reversion.register_data_adapter(TestModelSplitName, 1, 2, adapt_v1_to_v2)
        reversion.register_data_adapter(TestModelSplitName, 2, 3, adapt_v2_to_v3)
        reversion.set_data_version(TestModelSplitName, 3, detect=detect_data_version)
        version = self.createOldVersion(make_old_serialized_data(1, 1001))
        version.field_dict
        self.assertEqual(len(calls), 1)
        data, info = calls[0]
        # Pure serialized data, not model instances.
        self.assertIsInstance(data, list)
        self.assertIsInstance(data[0], dict)
        self.assertEqual(data[0]["model"], "test_app.testmodelsplitname")
        # Version info is plain data, with no database write handles.
        self.assertEqual(info.from_version, 1)
        self.assertEqual(info.to_version, 2)
        self.assertEqual(info.object_id, "1")
        self.assertEqual(info.app_label, "test_app")
        self.assertEqual(info.model_name, "testmodelsplitname")
        self.assertFalse(hasattr(info, "save"))
        self.assertFalse(hasattr(info, "revision"))

    def testAdaptedDataPassesDeserializationChecks(self):
        def bad_adapter(data, info):
            data[0]["fields"]["age"] = "not-a-number"

        reversion.register_data_adapter(TestModelSplitName, 3, 4, bad_adapter)
        reversion.set_data_version(TestModelSplitName, 4, detect=lambda data: 3)
        version = self.createOldVersion(make_old_serialized_data(1, 1001))
        with self.assertRaises(reversion.RevertError):
            version.field_dict

    def testAdapterFailureNamesStep(self):
        def broken_adapter(data, info):
            raise ValueError("boom")

        reversion.unregister_data_adapters(TestModelSplitName)
        reversion.register_data_adapter(TestModelSplitName, 1, 2, broken_adapter)
        reversion.set_data_version(TestModelSplitName, 2, detect=detect_data_version)
        version = self.createOldVersion(make_old_serialized_data(1, 1001))
        with self.assertRaises(reversion.DataAdapterError) as cm:
            version.field_dict
        self.assertIn("1", str(cm.exception))
        self.assertIn("2", str(cm.exception))
        self.assertIn("boom", str(cm.exception))


class DataAdapterRegistrationTest(TestBase):

    def tearDown(self):
        reversion.unregister_data_adapters(TestModelSplitName)
        super().tearDown()

    def testRegisterSameFromAndTo(self):
        with self.assertRaises(reversion.RegistrationError):
            reversion.register_data_adapter(TestModelSplitName, 1, 1, lambda data, info: None)

    def testRegisterDuplicateFromVersion(self):
        reversion.register_data_adapter(TestModelSplitName, 1, 2, lambda data, info: None)
        with self.assertRaises(reversion.RegistrationError):
            reversion.register_data_adapter(TestModelSplitName, 1, 3, lambda data, info: None)

    def testRegisterNonCallable(self):
        with self.assertRaises(reversion.RegistrationError):
            reversion.register_data_adapter(TestModelSplitName, 1, 2, "not-callable")

    def testGetDataVersion(self):
        self.assertIsNone(reversion.get_data_version(TestModelSplitName))
        reversion.set_data_version(TestModelSplitName, 3)
        self.assertEqual(reversion.get_data_version(TestModelSplitName), 3)


class DataAdapterPassthroughTest(TestBase):

    def setUp(self):
        super().setUp()
        self.addCleanup(reversion.unregister_data_adapters, TestModelSplitName)

    def testNoAdaptersPassthrough(self):
        data = '[{"model": "test_app.testmodelsplitname", "pk": 1, "fields": {}}]'
        self.assertEqual(
            adapt_serialized_data(TestModelSplitName, "json", data),
            data,
        )

    def testCurrentVersionPassthrough(self):
        reversion.register(TestModelSplitName)
        reversion.register_data_adapter(TestModelSplitName, 1, 2, lambda data, info: None)
        reversion.set_data_version(TestModelSplitName, 2, detect=lambda data: 2)
        data = '[{"model": "test_app.testmodelsplitname", "pk": 1, "fields": {}}]'
        self.assertEqual(
            adapt_serialized_data(TestModelSplitName, "json", data),
            data,
        )

    def testMissingDataVersion(self):
        reversion.register_data_adapter(TestModelSplitName, 1, 2, lambda data, info: None)
        data = '[{"model": "test_app.testmodelsplitname", "pk": 1, "fields": {}}]'
        with self.assertRaises(reversion.DataAdapterError):
            adapt_serialized_data(TestModelSplitName, "json", data)
