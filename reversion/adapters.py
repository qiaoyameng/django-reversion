"""
Read adapters for historical serialized data formats.

When a model changes in a way that breaks its serialized representation
(for example, a field is split in two, renamed, or changes type), old
``Version`` rows would otherwise become unreadable. Data adapters allow a
per-model upgrade chain to be registered, keyed by historical data format
version, which converts the stored serialized data into the current format
at read time.

The conversion is applied to an in-memory copy of the deserialized data
only. The ``serialized_data`` and revision metadata stored in the database
are never modified by the upgrade process, so old versions remain viewable
and recoverable in their original form.
"""

import json
from collections import namedtuple

from reversion.errors import DataAdapterError, RegistrationError


#: Information about a single adapter step, passed to adapter callbacks.
#:
#: This is pure data only - it deliberately carries no model class, version
#: instance or database connection, so callbacks do not implicitly gain
#: database write access.
DataVersionInfo = namedtuple("DataVersionInfo", (
    "app_label",
    "model_name",
    "object_id",
    "db",
    "format",
    "from_version",
    "to_version",
))


# model -> {from_version: (to_version, callback)}
_adapter_registry = {}

# model -> (current_version, detector)
_data_versions = {}


def register_data_adapter(model, from_version, to_version, callback):
    """
    Register an adapter that upgrades serialized data for ``model`` from
    data format version ``from_version`` to ``to_version``.

    ``callback`` is called as ``callback(data, info)``, where ``data`` is
    the parsed serialized data (a list of ``{"model", "pk", "fields"}``
    dicts) and ``info`` is a :class:`DataVersionInfo`. It may mutate
    ``data`` in place or return a replacement.
    """
    if from_version == to_version:
        raise RegistrationError(
            f"A data adapter for {model._meta.label} cannot have identical "
            f"from_version and to_version ({from_version!r})."
        )
    if not callable(callback):
        raise RegistrationError(
            f"The data adapter for {model._meta.label} must be callable."
        )
    adapters = _adapter_registry.setdefault(model, {})
    if from_version in adapters:
        raise RegistrationError(
            f"A data adapter for {model._meta.label} from version "
            f"{from_version!r} is already registered."
        )
    adapters[from_version] = (to_version, callback)


def unregister_data_adapters(model):
    """Remove all data adapters and the data version for ``model``."""
    _adapter_registry.pop(model, None)
    _data_versions.pop(model, None)


def set_data_version(model, version, detect=None):
    """
    Declare the current data format ``version`` for ``model``.

    ``detect``, if given, is called with the parsed serialized data of a
    stored version and must return the data format version it was saved
    with. If omitted, stored data is assumed to be the source version of
    the registered adapter chain.
    """
    _data_versions[model] = (version, detect)


def get_data_version(model):
    """Return the current data format version for ``model``, or None."""
    entry = _data_versions.get(model)
    return entry[0] if entry else None


def _model_label(model):
    return model._meta.label


def _detect_data_version(model, data):
    current_version, detector = _data_versions[model]
    if detector is not None:
        return detector(data)
    # Default: assume the stored data is at the source of the adapter
    # chain - the from_version that no other step upgrades to.
    adapters = _adapter_registry[model]
    to_versions = {to_version for to_version, _ in adapters.values()}
    sources = [from_version for from_version in adapters if from_version not in to_versions]
    if len(sources) != 1:
        raise DataAdapterError(
            f"Could not detect the data version of {_model_label(model)} serialized data - "
            f"the adapter chain has {len(sources)} source versions. Provide a detect callback "
            f"via set_data_version()."
        )
    return sources[0]


def _resolve_chain(model, stored_version):
    """
    Resolve the ordered list of (from_version, to_version, callback) steps
    needed to upgrade ``stored_version`` to the current data version.

    Raises DataAdapterError on missing steps or cycles.
    """
    current_version, _ = _data_versions[model]
    adapters = _adapter_registry[model]
    chain = []
    seen = set()
    version = stored_version
    while version != current_version:
        if version in seen:
            raise DataAdapterError(
                f"Data adapter cycle detected for {_model_label(model)} at version {version!r}."
            )
        seen.add(version)
        step = adapters.get(version)
        if step is None:
            raise DataAdapterError(
                f"Missing data adapter step for {_model_label(model)} from version "
                f"{version!r} towards current version {current_version!r}."
            )
        to_version, callback = step
        chain.append((version, to_version, callback))
        version = to_version
    return chain


def _parse_serialized_data(format_, serialized_data):
    if format_ == "json":
        return json.loads(serialized_data)
    raise DataAdapterError(
        f"Cannot adapt serialized data in format {format_!r} - only 'json' is supported."
    )


def _dump_serialized_data(format_, data):
    if format_ == "json":
        return json.dumps(data)
    raise DataAdapterError(  # pragma: no cover - guarded by _parse_serialized_data
        f"Cannot adapt serialized data in format {format_!r} - only 'json' is supported."
    )


def adapt_serialized_data(model, format_, serialized_data, object_id=None, db=None):
    """
    Upgrade ``serialized_data`` for ``model`` to the current data format
    version, returning the converted serialized data.

    The conversion is applied to a parsed copy of the data; the stored
    serialized data is never modified. Returns the input unchanged if no
    adapters are registered for the model or the data is already current.
    """
    if model not in _adapter_registry:
        return serialized_data
    if model not in _data_versions:
        raise DataAdapterError(
            f"Data adapters are registered for {_model_label(model)} but no current data "
            f"version is declared. Call set_data_version() to declare one."
        )
    data = _parse_serialized_data(format_, serialized_data)
    stored_version = _detect_data_version(model, data)
    chain = _resolve_chain(model, stored_version)
    if not chain:
        return serialized_data
    for from_version, to_version, callback in chain:
        info = DataVersionInfo(
            app_label=model._meta.app_label,
            model_name=model._meta.model_name,
            object_id=object_id,
            db=db,
            format=format_,
            from_version=from_version,
            to_version=to_version,
        )
        try:
            result = callback(data, info)
        except Exception as exc:
            raise DataAdapterError(
                f"Data adapter step {from_version!r} -> {to_version!r} for "
                f"{_model_label(model)} failed: {exc}"
            ) from exc
        if result is not None:
            data = result
    return _dump_serialized_data(format_, data)
