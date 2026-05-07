# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Tests for tintype.dap.variables.

Covers the expansion logic for each Serialized* variant, primitives,
and cyclic reference detection.
"""

from __future__ import annotations

import unittest
from typing import Any

from tintype import SerializedDictObject, SerializedListObject, SerializedObject
from tintype.dap import variables
from tintype.dap.variables import expand, is_expandable, make_variable, VariableRegistry


class IsExpandableTest(unittest.TestCase):
    def test_primitives_are_leaves(self) -> None:
        for leaf in (None, True, False, 0, 1, 1.5, "hello", b"bytes"):
            self.assertFalse(is_expandable(leaf), f"{leaf!r} should be a leaf")

    def test_empty_container_is_leaf(self) -> None:
        # DAP only shows "expand" affordance when the child count is non-zero.
        self.assertFalse(is_expandable([]))
        self.assertFalse(is_expandable({}))
        self.assertFalse(is_expandable(set()))

    def test_populated_containers_are_expandable(self) -> None:
        self.assertTrue(is_expandable([1]))
        self.assertTrue(is_expandable({"a": 1}))
        self.assertTrue(is_expandable({1, 2}))
        self.assertTrue(is_expandable((1, 2)))

    def test_serialized_object_with_attrs(self) -> None:
        obj = SerializedObject("SomeType(...)")
        obj.__dict__["x"] = 1
        obj.__dict__["y"] = 2
        self.assertTrue(is_expandable(obj))

    def test_serialized_object_without_attrs(self) -> None:
        obj = SerializedObject("Empty()")
        self.assertFalse(is_expandable(obj))


class MakeVariableTest(unittest.TestCase):
    def test_primitive_variable_has_no_reference(self) -> None:
        registry = VariableRegistry()
        var = make_variable("x", 42, registry, eval_name="x")
        self.assertEqual(var["name"], "x")
        self.assertEqual(var["value"], "42")
        self.assertEqual(var["variablesReference"], 0)
        self.assertEqual(var["evaluateName"], "x")

    def test_expandable_variable_gets_reference(self) -> None:
        registry = VariableRegistry()
        var = make_variable("d", {"a": 1}, registry, eval_name="d")
        self.assertGreater(var["variablesReference"], 0)

    def test_repr_fallback_on_exception(self) -> None:
        class BadRepr:
            def __repr__(self) -> str:
                raise RuntimeError("nope")

        registry = VariableRegistry()
        var = make_variable("x", BadRepr(), registry)
        self.assertIn("repr failed", var["value"])


class ExpandTest(unittest.TestCase):
    def test_expand_dict(self) -> None:
        registry = VariableRegistry()
        children = expand({"a": 1, "b": 2}, registry, parent_eval_name="d")
        names = [c["name"] for c in children]
        self.assertEqual(names, ["'a'", "'b'"])
        self.assertEqual(children[0]["evaluateName"], "d['a']")
        self.assertEqual(children[1]["evaluateName"], "d['b']")

    def test_expand_list(self) -> None:
        registry = VariableRegistry()
        children = expand([10, 20, 30], registry, parent_eval_name="xs")
        self.assertEqual([c["name"] for c in children], ["[0]", "[1]", "[2]"])
        self.assertEqual(children[0]["evaluateName"], "xs[0]")
        self.assertEqual(children[0]["value"], "10")

    def test_expand_tuple(self) -> None:
        registry = VariableRegistry()
        children = expand((1, 2), registry, parent_eval_name="t")
        self.assertEqual([c["name"] for c in children], ["[0]", "[1]"])
        self.assertEqual(children[1]["evaluateName"], "t[1]")

    def test_expand_set(self) -> None:
        registry = VariableRegistry()
        children = expand({1, 2, 3}, registry, parent_eval_name="s")
        # Sets have no stable subscript, so no evaluateName is emitted.
        self.assertEqual(len(children), 3)
        for c in children:
            self.assertNotIn("evaluateName", c)

    def test_expand_serialized_object(self) -> None:
        obj = SerializedObject("Point(x=1, y=2)")
        obj.__dict__["x"] = 1
        obj.__dict__["y"] = 2
        registry = VariableRegistry()
        children = expand(obj, registry, parent_eval_name="p")
        names = [c["name"] for c in children]
        self.assertIn("x", names)
        self.assertIn("y", names)
        x_var = next(c for c in children if c["name"] == "x")
        self.assertEqual(x_var["evaluateName"], "p.x")

    def test_expand_serialized_list_object_surfaces_items_and_attrs(self) -> None:
        sl = SerializedListObject([100, 200], "MyList([100, 200], flag=True)")
        sl.__dict__["flag"] = True
        registry = VariableRegistry()
        children = expand(sl, registry, parent_eval_name="sl")
        names = [c["name"] for c in children]
        # Items come before the attrs bundle.
        self.assertEqual(names[0], "[0]")
        self.assertEqual(names[1], "[1]")
        # Attrs are namespaced under the ``(attrs)`` pseudo-variable so they
        # cannot collide with indexed container elements.
        self.assertIn("(attrs)", names)
        self.assertNotIn("flag", names)

    def test_expand_serialized_dict_object_surfaces_items_and_attrs(self) -> None:
        sd = SerializedDictObject({"k": "v"}, "MyDict({'k': 'v'}, tag='hi')")
        sd.__dict__["tag"] = "hi"
        registry = VariableRegistry()
        children = expand(sd, registry, parent_eval_name="sd")
        names = [c["name"] for c in children]
        self.assertIn("'k'", names)
        self.assertIn("(attrs)", names)
        self.assertNotIn("tag", names)

    def test_cyclic_graph_dedupes_reference(self) -> None:
        registry = VariableRegistry()
        d: dict[str, Any] = {}
        d["self"] = d
        parent_var = make_variable("d", d, registry)
        ref = parent_var["variablesReference"]
        children = expand(d, registry)
        # ``d["self"]`` is the same dict, so its ref equals the parent's.
        self_var = children[0]
        self.assertEqual(self_var["variablesReference"], ref)

    def test_drill_down_child_has_evaluate_name(self) -> None:
        """Children expanded from a registered object inherit an evaluateName
        built from the parent's stored prefix."""
        registry = VariableRegistry()
        obj = SerializedObject("Node(value=99)")
        obj.__dict__["value"] = 99
        # Simulate the scope layer: top-level variable ``n`` registered with
        # its eval-name.
        top = make_variable("n", obj, registry, eval_name="n")
        ref = top["variablesReference"]

        # Resolve and drill down the way handle_variables does.
        entry = registry.resolve(ref)
        assert entry is not None
        kind, payload = entry
        self.assertEqual(kind, "object")
        value, prefix = payload
        children = expand(value, registry, parent_eval_name=prefix)
        value_var = next(c for c in children if c["name"] == "value")
        self.assertEqual(value_var["evaluateName"], "n.value")

    def test_serialized_dict_subclass_attrs_bundled(self) -> None:
        """``SerializedDictObject`` attrs must not collide with its keys."""
        sd = SerializedDictObject(
            {"flag": "dict-value"}, "MyDict({'flag': 'dict-value'})"
        )
        sd.__dict__["flag"] = "attr-value"
        registry = VariableRegistry()
        children = expand(sd, registry, parent_eval_name="sd")
        # Exactly one top-level row named ``'flag'`` (the dict entry), plus
        # a single ``(attrs)`` pseudo-variable.
        flag_entries = [c for c in children if c["name"] == "'flag'"]
        self.assertEqual(len(flag_entries), 1)
        self.assertEqual(flag_entries[0]["value"], "'dict-value'")
        attrs_entries = [c for c in children if c["name"] == "(attrs)"]
        self.assertEqual(len(attrs_entries), 1)
        self.assertGreater(attrs_entries[0]["variablesReference"], 0)

    def test_attrs_bundle_expands_to_attributes(self) -> None:
        """Expanding the ``(attrs)`` ref yields the synthesized attribute rows."""
        sd = SerializedDictObject({"a": 1}, "X")
        sd.__dict__["tag"] = "hi"
        registry = VariableRegistry()
        children = expand(sd, registry, parent_eval_name="sd")
        attrs_ref = next(
            c["variablesReference"] for c in children if c["name"] == "(attrs)"
        )
        entry = registry.resolve(attrs_ref)
        assert entry is not None
        kind, payload = entry
        self.assertEqual(kind, "object")
        value, prefix = payload
        attrs_children = expand(value, registry, parent_eval_name=prefix)
        names = [c["name"] for c in attrs_children]
        self.assertIn("tag", names)
        tag_var = next(c for c in attrs_children if c["name"] == "tag")
        self.assertEqual(tag_var["evaluateName"], "sd.tag")

    def test_truncation_marker(self) -> None:
        # Use the module-private threshold via monkeypatch so we don't have
        # to allocate 1000 entries.
        original = variables._MAX_CHILDREN
        try:
            variables._MAX_CHILDREN = 2  # pyre-ignore[9]
            registry = VariableRegistry()
            children = expand([1, 2, 3, 4, 5], registry, parent_eval_name="xs")
            self.assertEqual(children[-1]["name"], "<truncated>")
        finally:
            variables._MAX_CHILDREN = original  # pyre-ignore[9]

    def test_attr_view_sorted_by_name(self) -> None:
        """Object ``__dict__`` attributes are returned in a predictable
        order: public names first, single-underscore private next,
        dunders last, with case-insensitive alphabetical sort
        within each bucket.
        """

        class _Bag:
            pass

        bag = _Bag()
        # Insertion order intentionally scrambled, mix of public /
        # private / dunder / different cases.
        bag.zulu = 1  # pyre-ignore[16]
        bag.__dict__["__dunder__"] = 2
        bag.Alpha = 3  # pyre-ignore[16]
        bag._private = 4  # pyre-ignore[16]
        bag.beta = 5  # pyre-ignore[16]
        bag.__dict__["__aardvark__"] = 6

        registry = VariableRegistry()
        children = expand(bag, registry, parent_eval_name="bag")
        names = [c["name"] for c in children]
        self.assertEqual(
            names,
            [
                "Alpha",
                "beta",
                "zulu",
                "_private",
                "__aardvark__",
                "__dunder__",
            ],
        )

    def test_dict_view_preserves_insertion_order(self) -> None:
        """Python dicts (3.7+) are insertion-ordered and that order is
        semantically meaningful. The attr-sort above must NOT leak
        into the dict-style expansion.
        """
        registry = VariableRegistry()
        d = {"zebra": 1, "apple": 2, "mango": 3}
        children = expand(d, registry, parent_eval_name="d")
        names = [c["name"] for c in children]
        self.assertEqual(names, ["'zebra'", "'apple'", "'mango'"])


class VariableRegistryTest(unittest.TestCase):
    def test_scope_reference_resolves(self) -> None:
        registry = VariableRegistry()
        ref = registry.register_scope(frame_id=7)
        entry = registry.resolve(ref)
        self.assertIsNotNone(entry)
        assert entry is not None
        kind, payload = entry
        self.assertEqual(kind, "scope")
        self.assertEqual(payload, 7)

    def test_object_reference_resolves(self) -> None:
        registry = VariableRegistry()
        value = {"a": 1}
        ref = registry.register_object(value, eval_name="d")
        entry = registry.resolve(ref)
        assert entry is not None
        kind, payload = entry
        self.assertEqual(kind, "object")
        # Payload is (value, eval_name_prefix).
        stored_value, stored_prefix = payload
        self.assertIs(stored_value, value)
        self.assertEqual(stored_prefix, "d")

    def test_object_reference_without_eval_name(self) -> None:
        registry = VariableRegistry()
        value = [1, 2]
        ref = registry.register_object(value)
        entry = registry.resolve(ref)
        assert entry is not None
        _, payload = entry
        stored_value, stored_prefix = payload
        self.assertIs(stored_value, value)
        self.assertIsNone(stored_prefix)

    def test_unknown_reference_returns_none(self) -> None:
        registry = VariableRegistry()
        self.assertIsNone(registry.resolve(9999))

    def test_clear_resets_state(self) -> None:
        registry = VariableRegistry()
        registry.register_scope(1)
        registry.register_object({"x": 1})
        registry.clear()
        self.assertIsNone(registry.resolve(1))


if __name__ == "__main__":
    unittest.main()
