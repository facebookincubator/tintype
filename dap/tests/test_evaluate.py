# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Tests for tintype.dap.evaluate."""

from __future__ import annotations

import unittest

from tintype import SerializedObject
from tintype.dap.evaluate import evaluate_expression, EvaluateError


class EvaluateNameTest(unittest.TestCase):
    def test_simple_name(self) -> None:
        self.assertEqual(evaluate_expression("x", {"x": 42}), 42)

    def test_missing_name(self) -> None:
        with self.assertRaises(EvaluateError) as cm:
            evaluate_expression("missing", {"x": 1})
        self.assertIn("not defined", str(cm.exception))


class EvaluateAttributeTest(unittest.TestCase):
    def test_dotted_access_on_serialized_object(self) -> None:
        obj = SerializedObject("Node(value=1)")
        obj.__dict__["value"] = 99
        self.assertEqual(evaluate_expression("n.value", {"n": obj}), 99)

    def test_chained_attribute_access(self) -> None:
        inner = SerializedObject("Inner(val=3)")
        inner.__dict__["val"] = 3
        outer = SerializedObject("Outer(inner=...)")
        outer.__dict__["inner"] = inner
        self.assertEqual(evaluate_expression("o.inner.val", {"o": outer}), 3)

    def test_missing_attribute(self) -> None:
        obj = SerializedObject("X()")
        obj.__dict__["a"] = 1
        with self.assertRaises(EvaluateError):
            evaluate_expression("o.b", {"o": obj})


class EvaluateSubscriptTest(unittest.TestCase):
    def test_dict_subscript_string_key(self) -> None:
        self.assertEqual(
            evaluate_expression("d['key']", {"d": {"key": "value"}}), "value"
        )

    def test_list_subscript_positive_index(self) -> None:
        self.assertEqual(evaluate_expression("xs[1]", {"xs": [10, 20, 30]}), 20)

    def test_list_subscript_negative_index(self) -> None:
        self.assertEqual(evaluate_expression("xs[-1]", {"xs": [10, 20, 30]}), 30)

    def test_index_out_of_range(self) -> None:
        with self.assertRaises(EvaluateError):
            evaluate_expression("xs[10]", {"xs": [1, 2, 3]})

    def test_missing_key(self) -> None:
        with self.assertRaises(EvaluateError):
            evaluate_expression("d['k']", {"d": {"other": 1}})


class EvaluateCombinedTest(unittest.TestCase):
    def test_dotted_and_subscript(self) -> None:
        obj = SerializedObject("X()")
        obj.__dict__["items"] = [{"name": "alice"}, {"name": "bob"}]
        self.assertEqual(evaluate_expression("x.items[1]['name']", {"x": obj}), "bob")


class EvaluateRejectionTest(unittest.TestCase):
    """Unsupported grammar must be rejected — we're not a full Python evaluator."""

    def test_rejects_function_call(self) -> None:
        with self.assertRaises(EvaluateError):
            evaluate_expression("foo()", {"foo": lambda: 1})

    def test_rejects_arithmetic(self) -> None:
        with self.assertRaises(EvaluateError):
            evaluate_expression("x + 1", {"x": 1})

    def test_rejects_comprehension(self) -> None:
        with self.assertRaises(EvaluateError):
            evaluate_expression("[x for x in xs]", {"xs": [1, 2]})

    def test_rejects_invalid_syntax(self) -> None:
        with self.assertRaises(EvaluateError):
            evaluate_expression("x..y", {"x": 1})

    def test_rejects_empty(self) -> None:
        with self.assertRaises(EvaluateError):
            evaluate_expression("   ", {})


if __name__ == "__main__":
    unittest.main()
