from collections import namedtuple
from copy import deepcopy
import json
from xml.etree import ElementTree

from django.core import serializers

from reversion.errors import RegistrationError, RevertError


DEFAULT_FORMAT_VERSION = 1
FORMAT_VERSION_SEPARATOR = "@"


VersionAdapterContext = namedtuple("VersionAdapterContext", (
    "model",
    "serializer_format",
    "source_version",
    "target_version",
))


def _model_key(model):
    return model._meta.app_label, model._meta.model_name


_version_adapters = {}


def split_format_version(format_name):
    if FORMAT_VERSION_SEPARATOR not in format_name:
        return format_name, DEFAULT_FORMAT_VERSION
    serializer_format, version = format_name.rsplit(FORMAT_VERSION_SEPARATOR, 1)
    try:
        return serializer_format, int(version)
    except ValueError:
        return format_name, DEFAULT_FORMAT_VERSION


def get_format_version(model):
    from reversion.revisions import _get_options

    options = _get_options(model)
    return options.format, options.format_version


def register_adapter(model, source_version, target_version, adapter):
    if source_version == target_version:
        raise RegistrationError(
            f"{model._meta.label} format version adapter cannot use the same version "
            f"{source_version}."
        )
    if source_version < 1 or target_version < 1:
        raise RegistrationError(
            f"{model._meta.label} format versions must be positive integers."
        )
    if target_version <= source_version:
        raise RegistrationError(
            f"{model._meta.label} format version adapters must move to a newer version."
        )
    if not callable(adapter):
        raise RegistrationError(
            f"{model._meta.label} format version adapter must be callable."
        )

    key = _model_key(model)
    model_adapters = _version_adapters.setdefault(key, {})
    if any(source_version == edge_source for edge_source, edge_target in model_adapters):
        raise RegistrationError(
            f"{model._meta.label} already has a format version adapter starting at "
            f"version {source_version}."
        )
    adapter_key = (source_version, target_version)
    if adapter_key in model_adapters:
        raise RegistrationError(
            f"{model._meta.label} already has a format version adapter "
            f"{source_version} -> {target_version}."
        )
    model_adapters[adapter_key] = adapter


def unregister_adapter(model, source_version, target_version):
    key = _model_key(model)
    adapter_key = (source_version, target_version)
    try:
        del _version_adapters[key][adapter_key]
    except KeyError:
        raise RegistrationError(
            f"{model._meta.label} has no format version adapter "
            f"{source_version} -> {target_version}."
        )
    if not _version_adapters[key]:
        del _version_adapters[key]


def get_adapter_chain(model, source_version, target_version):
    """Return the ordered adapters needed to read a stored format version."""
    if source_version == target_version:
        return ()
    if source_version > target_version:
        raise RevertError(
            f"{model._meta.label} version data is format version {source_version}, but "
            f"the current model uses format version {target_version}."
        )

    chain = []
    current_version = source_version
    visited = set()
    while current_version != target_version:
        if current_version in visited:
            raise RevertError(
                f"{model._meta.label} format version adapters contain a cycle at "
                f"version {current_version}."
            )
        visited.add(current_version)
        model_adapters = _version_adapters.get(_model_key(model), {})
        adapter_key = next((
            (edge_source, edge_target)
            for edge_source, edge_target in model_adapters
            if edge_source == current_version and edge_target <= target_version
        ), None)
        if adapter_key is None:
            raise RevertError(
                f"{model._meta.label} is missing a format version adapter starting at "
                f"version {current_version}."
            )
        next_version, adapter = adapter_key[1], model_adapters[adapter_key]
        if next_version <= current_version:
            raise RevertError(
                f"{model._meta.label} format version adapters contain a cycle: "
                f"{current_version} -> {next_version}."
            )
        chain.append((current_version, next_version, adapter))
        current_version = next_version
    return tuple(chain)


def _xml_text_or_none(element):
    if element.find("None") is not None:
        return None
    return "".join(element.itertext())


def _xml_field_value(field_element):
    objects = field_element.findall("object")
    if objects:
        values = []
        for obj in objects:
            natural_values = [natural.text for natural in obj.findall("natural")]
            values.append(natural_values if natural_values else obj.attrib.get("pk"))
        return values
    natural_values = [natural.text for natural in field_element.findall("natural")]
    if natural_values:
        return natural_values
    return _xml_text_or_none(field_element)


def _decode_xml_records(data):
    root = ElementTree.fromstring(data)
    records = []
    for object_element in root.findall("object"):
        fields = {}
        for field_element in object_element.findall("field"):
            fields[field_element.attrib["name"]] = _xml_field_value(field_element)
        record = {
            "model": object_element.attrib["model"],
            "fields": fields,
        }
        if "pk" in object_element.attrib:
            record["pk"] = object_element.attrib["pk"]
        records.append(record)
    return records


def _decode_python_records(serializer_format, data):
    if serializer_format == "python":
        return deepcopy(data)
    if serializer_format == "json":
        return json.loads(data)
    if serializer_format == "jsonl":
        return [json.loads(line) for line in data.splitlines() if line.strip()]
    if serializer_format == "yaml":
        try:
            import yaml
        except ImportError as ex:
            raise RevertError("PyYAML is required to adapt YAML version data.") from ex
        return yaml.safe_load(data)
    if serializer_format == "xml":
        return _decode_xml_records(data)
    raise RevertError(
        f"Format version adapters cannot decode serializer {serializer_format!r}."
    )


def _deserialize_records(records, use_natural_foreign_keys, using):
    return list(serializers.deserialize(
        "python",
        records,
        ignorenonexistent=False,
        use_natural_foreign_keys=use_natural_foreign_keys,
        using=using,
    ))


def adapt_version_data(model, format_name, data, *, use_natural_foreign_keys=False, using=None):
    serializer_format, source_version = split_format_version(format_name)
    try:
        serializers.get_deserializer(serializer_format)
    except serializers.SerializerDoesNotExist:
        raise
    _, target_version = get_format_version(model)
    chain = get_adapter_chain(model, source_version, target_version)
    if source_version == target_version:
        return None

    try:
        records = _decode_python_records(serializer_format, data)
    except RevertError:
        raise
    except Exception as ex:
        raise RevertError(
            f"{model._meta.label} format version {source_version} data could not be decoded: {ex}"
        )

    for adapter_source, adapter_target, adapter in chain:
        context = VersionAdapterContext(
            model=model,
            serializer_format=serializer_format,
            source_version=adapter_source,
            target_version=adapter_target,
        )
        try:
            adapted_records = adapter(deepcopy(records), context)
        except RevertError:
            raise
        except Exception as ex:
            raise RevertError(
                f"{model._meta.label} format version adapter {adapter_source} -> "
                f"{adapter_target} failed: {ex}"
            )
        if adapted_records is None:
            adapted_records = deepcopy(records)
        if not isinstance(adapted_records, list) or any(
            not isinstance(record, dict) or not isinstance(record.get("fields"), dict)
            for record in adapted_records
        ):
            raise RevertError(
                f"{model._meta.label} format version adapter {adapter_source} -> "
                f"{adapter_target} returned invalid version data."
            )
        records = adapted_records

    try:
        return _deserialize_records(
            records,
            use_natural_foreign_keys,
            using,
        )
    except Exception as ex:
        last_source, last_target = chain[-1][0], chain[-1][1]
        raise RevertError(
            f"{model._meta.label} format version adapter {last_source} -> "
            f"{last_target} produced incompatible version data: {ex}"
        ) from ex
