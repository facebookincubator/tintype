# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Snapshot object graph -> DAP ``Variable`` tree."""

from __future__ import annotations

import logging
from typing import Any

from tintype import SerializedDictObject, SerializedListObject, SerializedObject


logger: logging.Logger = logging.getLogger(__name__)

_MAX_INLINE_REPR = 512
_MAX_CHILDREN = 1000

# Internal bookkeeping attrs set by Serialized* C++ subclasses; hidden from
# the user-visible attribute view so debug plumbing does not leak into DAP.
_INTERNAL_SERIALIZED_ATTRS: frozenset[str] = frozenset({"__serialized_repr__"})


class _AttrBundle:
    """Sentinel wrapper for the ``(attrs)`` pseudo-variable.

    Groups ``__dict__`` attributes under a single expandable row so they
    cannot collide with container keys or indices.
    """

    __slots__ = ("attrs",)

    def __init__(self, attrs: dict[Any, Any]) -> None:
        self.attrs = attrs


class VariableRegistry:
    """Maps ``variablesReference`` integers to expandable values.

    Object entries store ``(value, eval_name | None)`` so drill-down
    children can build ``evaluateName`` strings for Copy Value / watch.
    """

    _SCOPE_SENTINEL = "__scope__"

    def __init__(self) -> None:
        self._next_ref: int = 1
        self._entries: dict[int, tuple[str, Any]] = {}
        self._object_to_ref: dict[int, int] = {}

    def register_scope(self, frame_id: int) -> int:
        ref = self._next_ref
        self._next_ref += 1
        self._entries[ref] = ("scope", frame_id)
        return ref

    def register_object(self, value: Any, *, eval_name: str | None = None) -> int:
        # We key dedupe on ``id(value)`` to recognise repeated references to
        # the same in-memory object within one snapshot (avoids
        # infinite expansion of cyclic graphs). Because Python can reuse
        # an ``id`` once an object is freed, the caller MUST keep every
        # registered object alive at least until :meth:`clear` runs.
        # In the session this is guaranteed: the snapshot's top-level
        # variables pin the entire graph until the next snapshot
        # transition clears the registry. Do not register short-lived
        # wrappers that outlive their source.
        key = id(value)
        existing = self._object_to_ref.get(key)
        if existing is not None:
            return existing
        ref = self._next_ref
        self._next_ref += 1
        self._entries[ref] = ("object", (value, eval_name))
        self._object_to_ref[key] = ref
        return ref

    def resolve(self, reference: int) -> tuple[str, Any] | None:
        return self._entries.get(reference)

    def clear(self) -> None:
        self._entries.clear()
        self._object_to_ref.clear()
        self._next_ref = 1


def _has_user_attrs(value: Any) -> bool:
    attrs = getattr(value, "__dict__", None)
    if not isinstance(attrs, dict):
        return False
    return any(k not in _INTERNAL_SERIALIZED_ATTRS for k in attrs.keys())


def is_expandable(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int, float, bytes, str)):
        return False
    if isinstance(value, _AttrBundle):
        return bool(value.attrs)
    if isinstance(value, SerializedObject):
        return _has_user_attrs(value)
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        if isinstance(value, (SerializedListObject, SerializedDictObject)):
            return True
        return len(value) > 0
    return _has_user_attrs(value)


def _safe_repr(value: Any) -> str:
    try:
        text = repr(value)
    except Exception as e:  # noqa: BLE001
        text = f"<repr failed: {e!r}>"
    if len(text) > _MAX_INLINE_REPR:
        text = text[: _MAX_INLINE_REPR - 3] + "..."
    return text


def _type_name(value: Any) -> str:
    return type(value).__name__


def make_variable(
    name: str,
    value: Any,
    registry: VariableRegistry,
    *,
    eval_name: str | None = None,
) -> dict[str, Any]:
    var: dict[str, Any] = {
        "name": name,
        "value": _safe_repr(value),
        "type": _type_name(value),
        "variablesReference": (
            registry.register_object(value, eval_name=eval_name)
            if is_expandable(value)
            else 0
        ),
    }
    if eval_name is not None:
        var["evaluateName"] = eval_name
    return var


def _filter_internal_attrs(attrs: dict[Any, Any]) -> dict[Any, Any]:
    return {k: v for k, v in attrs.items() if k not in _INTERNAL_SERIALIZED_ATTRS}


def expand(
    value: Any,
    registry: VariableRegistry,
    *,
    parent_eval_name: str | None = None,
) -> list[dict[str, Any]]:
    if isinstance(value, _AttrBundle):
        return _expand_mapping(value.attrs, registry, parent_eval_name, style="attr")
    if isinstance(value, (SerializedListObject, SerializedDictObject)):
        return _expand_serialized_subclass(value, registry, parent_eval_name)
    if isinstance(value, SerializedObject):
        return _expand_object(value, registry, parent_eval_name)
    if isinstance(value, dict):
        return _expand_dict(value, registry, parent_eval_name)
    if isinstance(value, (list, tuple)):
        return _expand_sequence(value, registry, parent_eval_name)
    if isinstance(value, (set, frozenset)):
        return _expand_set(value, registry, parent_eval_name)
    attrs = getattr(value, "__dict__", None)
    if isinstance(attrs, dict):
        return _expand_mapping(
            _filter_internal_attrs(attrs),
            registry,
            parent_eval_name,
            style="attr",
        )
    return []


def _expand_object(
    value: SerializedObject, registry: VariableRegistry, parent: str | None
) -> list[dict[str, Any]]:
    attrs = getattr(value, "__dict__", None)
    if not isinstance(attrs, dict):
        return []
    return _expand_mapping(
        _filter_internal_attrs(attrs), registry, parent, style="attr"
    )


def _expand_serialized_subclass(
    value: object, registry: VariableRegistry, parent: str | None
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(value, SerializedDictObject):
        out.extend(_expand_dict(value, registry, parent))
    elif isinstance(value, SerializedListObject):
        out.extend(_expand_sequence(value, registry, parent))
    raw = getattr(value, "__dict__", None)
    if not isinstance(raw, dict):
        return out
    user_attrs = _filter_internal_attrs(raw)
    if not user_attrs:
        return out
    attrs_ref = registry.register_object(_AttrBundle(user_attrs), eval_name=parent)
    out.append(
        {
            "name": "(attrs)",
            "value": f"<{len(user_attrs)} attribute(s)>",
            "type": "attributes",
            "variablesReference": attrs_ref,
            "presentationHint": {"kind": "virtual"},
        }
    )
    return out


def _attr_sort_key(name: str) -> tuple[int, str]:
    """Sort key for object ``__dict__`` attribute views.

    Ordering buckets (matches pydevd / debugpy convention):

    * 0: plain names (public).
    * 1: single-underscore private names (``_foo``).
    * 2: dunders (``__foo__``) — CPython / framework machinery.

    Within a bucket, names are compared case-insensitively so
    ``Foo`` and ``foo`` sort next to each other.
    """
    if name.startswith("__") and name.endswith("__"):
        return (2, name.lower())
    if name.startswith("_"):
        return (1, name.lower())
    return (0, name.lower())


def _expand_mapping(
    mapping: dict[Any, Any],
    registry: VariableRegistry,
    parent: str | None,
    *,
    style: str,
) -> list[dict[str, Any]]:
    """Convert a mapping to a DAP ``Variable`` list.

    ``style``:

    * ``"attr"`` — object ``__dict__`` view. Sorted by attribute
      name with public names first, single-underscore private
      names next, and dunders last (see :func:`_attr_sort_key`).
      Sorting keeps attribute navigation predictable across
      snapshots.
    * ``"dict"`` — dict-key view. **Preserves insertion order** —
      Python 3.7+ dicts are ordered and that order is semantically
      meaningful to the user; sorting it would lose information
      about the literal order the user wrote.
    """
    items = list(mapping.items())
    if style == "attr":
        items.sort(key=lambda kv: _attr_sort_key(str(kv[0])))
    out: list[dict[str, Any]] = []
    count = 0
    for key, child_value in items:
        if count >= _MAX_CHILDREN:
            out.append(
                {
                    "name": "<truncated>",
                    "value": f"(showing first {_MAX_CHILDREN} of {len(mapping)})",
                    "type": "truncation",
                    "variablesReference": 0,
                }
            )
            break
        name = str(key) if style == "attr" else _dict_key_display(key)
        eval_name = _make_eval_name(parent, key, style)
        out.append(make_variable(name, child_value, registry, eval_name=eval_name))
        count += 1
    return out


def _expand_dict(
    value: dict[Any, Any], registry: VariableRegistry, parent: str | None
) -> list[dict[str, Any]]:
    return _expand_mapping(value, registry, parent, style="dict")


def _expand_sequence(
    value: list[Any] | tuple[Any, ...],
    registry: VariableRegistry,
    parent: str | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, child_value in enumerate(value):
        if idx >= _MAX_CHILDREN:
            out.append(
                {
                    "name": "<truncated>",
                    "value": f"(showing first {_MAX_CHILDREN} of {len(value)})",
                    "type": "truncation",
                    "variablesReference": 0,
                }
            )
            break
        eval_name = None if parent is None else f"{parent}[{idx}]"
        out.append(
            make_variable(f"[{idx}]", child_value, registry, eval_name=eval_name)
        )
    return out


def _expand_set(
    value: set[Any] | frozenset[Any],
    registry: VariableRegistry,
    _parent: str | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        items = sorted(value, key=_safe_repr)
    except Exception:  # noqa: BLE001
        items = list(value)
    for idx, child_value in enumerate(items):
        if idx >= _MAX_CHILDREN:
            out.append(
                {
                    "name": "<truncated>",
                    "value": f"(showing first {_MAX_CHILDREN} of {len(value)})",
                    "type": "truncation",
                    "variablesReference": 0,
                }
            )
            break
        out.append(make_variable(f"{{{idx}}}", child_value, registry))
    return out


def _dict_key_display(key: Any) -> str:
    try:
        return repr(key)
    except Exception:  # noqa: BLE001
        return "<unreprable key>"


def _make_eval_name(parent: str | None, key: Any, style: str) -> str | None:
    if parent is None:
        return None
    if style == "attr":
        if isinstance(key, str) and key.isidentifier():
            return f"{parent}.{key}"
        return f"getattr({parent}, {key!r})"
    try:
        return f"{parent}[{key!r}]"
    except Exception:  # noqa: BLE001
        return None
