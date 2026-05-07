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


class EvaluateCallTest(unittest.TestCase):
    def test_allowed_function_call(self) -> None:
        self.assertEqual(evaluate_expression("len(xs)", {"xs": [1, 2, 3]}), 3)
        self.assertEqual(evaluate_expression("str(x)", {"x": 42}), "42")
        self.assertEqual(evaluate_expression("repr(s)", {"s": "foo"}), "'foo'")
        self.assertEqual(evaluate_expression("sum(xs)", {"xs": [1, 2, 3]}), 6)
        self.assertEqual(evaluate_expression("min(xs)", {"xs": [3, 1, 2]}), 1)
        self.assertEqual(evaluate_expression("max(xs)", {"xs": [3, 1, 2]}), 3)

    def test_isinstance_with_builtin_type(self) -> None:
        self.assertTrue(evaluate_expression("isinstance(x, int)", {"x": 42}))
        self.assertFalse(evaluate_expression("isinstance(x, str)", {"x": 42}))

    def test_type_class_attribute_access_is_rejected(self) -> None:
        """``type(x).__name__`` and similar ``type`` attribute access
        are rejected: ``type`` is excluded from
        :data:`_ATTR_SAFE_TYPES` because every user class is a ``type``
        instance, and allowing ``type(x).method`` would let expressions
        escape the receiver-type guard and invoke user-defined code.
        Users who need the class name can call ``type(x)`` alone (the
        repr renders ``<class '...'>``) or capture the name as a
        snapshot local at capture time.
        """
        with self.assertRaises(EvaluateError):
            evaluate_expression("type(x).__name__", {"x": 42})

    def test_getattr_builtin_is_rejected(self) -> None:
        """``getattr`` (along with ``hasattr`` and ``vars``) is not in
        :data:`_SAFE_BUILTINS`: each one invokes user-defined
        ``__getattr__`` / ``__getattribute__`` / property / descriptor
        code, which would bypass the :data:`_ATTR_SAFE_TYPES` receiver
        guard enforced by :func:`_get_attr` and hand control back to
        arbitrary user code reachable via any local that happens to be
        a user class instance. Users who need ``__dict__`` access can
        write ``obj.__dict__`` directly — that path stays behind the
        receiver-type check.
        """
        obj = SerializedObject("X()")
        obj.__dict__["present"] = "here"
        with self.assertRaises(EvaluateError):
            evaluate_expression("getattr(o, 'present', 'fallback')", {"o": obj})
        with self.assertRaises(EvaluateError):
            evaluate_expression("hasattr(o, 'present')", {"o": obj})
        with self.assertRaises(EvaluateError):
            evaluate_expression("vars(o)", {"o": obj})

    def test_method_call_on_list(self) -> None:
        self.assertEqual(evaluate_expression("xs.index(20)", {"xs": [10, 20, 30]}), 1)
        self.assertEqual(evaluate_expression("xs.count(2)", {"xs": [1, 2, 2, 3]}), 2)

    def test_method_call_on_dict(self) -> None:
        self.assertEqual(
            evaluate_expression("d.get('k', 'default')", {"d": {"k": "v"}}),
            "v",
        )
        self.assertEqual(
            evaluate_expression("d.get('missing', 'default')", {"d": {"k": "v"}}),
            "default",
        )

    def test_method_call_on_serialized_object_attr(self) -> None:
        obj = SerializedObject("X()")
        obj.__dict__["items"] = [10, 20, 30]
        self.assertEqual(evaluate_expression("o.items.index(20)", {"o": obj}), 1)

    def test_nested_call(self) -> None:
        self.assertEqual(evaluate_expression("len(str(x))", {"x": 12345}), 5)


class EvaluateArithmeticTest(unittest.TestCase):
    def test_arithmetic_operators(self) -> None:
        self.assertEqual(evaluate_expression("a + b", {"a": 3, "b": 4}), 7)
        self.assertEqual(evaluate_expression("a - b", {"a": 10, "b": 3}), 7)
        self.assertEqual(evaluate_expression("a * b", {"a": 3, "b": 4}), 12)
        self.assertEqual(evaluate_expression("a / b", {"a": 10, "b": 4}), 2.5)
        self.assertEqual(evaluate_expression("a // b", {"a": 10, "b": 3}), 3)
        self.assertEqual(evaluate_expression("a % b", {"a": 10, "b": 3}), 1)
        self.assertEqual(evaluate_expression("a ** b", {"a": 2, "b": 10}), 1024)

    def test_arithmetic_on_floats(self) -> None:
        self.assertAlmostEqual(
            evaluate_expression("a + b", {"a": 1.5, "b": 2.25}), 3.75
        )
        self.assertAlmostEqual(evaluate_expression("a * b", {"a": 1.5, "b": 2.0}), 3.0)

    def test_string_concatenation(self) -> None:
        self.assertEqual(
            evaluate_expression("a + b", {"a": "foo", "b": "bar"}), "foobar"
        )

    def test_unary_operators(self) -> None:
        self.assertEqual(evaluate_expression("-x", {"x": 5}), -5)
        self.assertEqual(evaluate_expression("+x", {"x": 5}), 5)
        self.assertFalse(evaluate_expression("not x", {"x": True}))
        self.assertEqual(evaluate_expression("~x", {"x": 0}), -1)


class EvaluateComparisonTest(unittest.TestCase):
    def test_comparison_operators(self) -> None:
        self.assertTrue(evaluate_expression("a < b", {"a": 1, "b": 2}))
        self.assertTrue(evaluate_expression("a <= b", {"a": 2, "b": 2}))
        self.assertTrue(evaluate_expression("a == b", {"a": 2, "b": 2}))
        self.assertTrue(evaluate_expression("a != b", {"a": 1, "b": 2}))
        self.assertTrue(evaluate_expression("a > b", {"a": 3, "b": 2}))
        self.assertTrue(evaluate_expression("a >= b", {"a": 2, "b": 2}))
        self.assertFalse(evaluate_expression("a < b", {"a": 2, "b": 1}))

    def test_chained_comparison(self) -> None:
        self.assertTrue(evaluate_expression("a < b < c", {"a": 1, "b": 2, "c": 3}))
        self.assertFalse(evaluate_expression("a < b < c", {"a": 1, "b": 3, "c": 2}))

    def test_in_and_not_in(self) -> None:
        self.assertTrue(evaluate_expression("x in xs", {"x": 2, "xs": [1, 2, 3]}))
        self.assertFalse(evaluate_expression("x in xs", {"x": 5, "xs": [1, 2, 3]}))
        self.assertTrue(evaluate_expression("x not in xs", {"x": 5, "xs": [1, 2, 3]}))

    def test_is_and_is_not(self) -> None:
        self.assertTrue(evaluate_expression("x is None", {"x": None}))
        self.assertTrue(evaluate_expression("x is not None", {"x": 1}))


class EvaluateBooleanTest(unittest.TestCase):
    def test_boolean_short_circuit(self) -> None:
        # If the first operand of `and` is falsy, the second is never looked
        # up — so referencing a name that doesn't exist must not raise.
        self.assertFalse(evaluate_expression("flag and missing", {"flag": False}))
        # Symmetric case for `or`.
        self.assertTrue(evaluate_expression("flag or missing", {"flag": True}))

    def test_and_returns_last_truthy(self) -> None:
        # Python's `and` returns the last operand evaluated (not a bool).
        self.assertEqual(evaluate_expression("a and b", {"a": 1, "b": 2}), 2)
        self.assertEqual(evaluate_expression("a and b", {"a": 0, "b": 2}), 0)

    def test_or_returns_first_truthy(self) -> None:
        self.assertEqual(evaluate_expression("a or b", {"a": 0, "b": 2}), 2)
        self.assertEqual(evaluate_expression("a or b", {"a": 1, "b": 2}), 1)


class EvaluateLiteralTest(unittest.TestCase):
    def test_list_literal(self) -> None:
        self.assertEqual(evaluate_expression("[a, b, 3]", {"a": 1, "b": 2}), [1, 2, 3])

    def test_dict_literal(self) -> None:
        self.assertEqual(
            evaluate_expression("{'a': a, 'b': b}", {"a": 1, "b": 2}),
            {"a": 1, "b": 2},
        )

    def test_set_literal(self) -> None:
        self.assertEqual(evaluate_expression("{a, b, 3}", {"a": 1, "b": 2}), {1, 2, 3})

    def test_tuple_literal(self) -> None:
        self.assertEqual(evaluate_expression("(a, b, 3)", {"a": 1, "b": 2}), (1, 2, 3))

    def test_dict_unpack_rejected(self) -> None:
        with self.assertRaises(EvaluateError):
            evaluate_expression("{**d}", {"d": {"a": 1}})


class EvaluateTernaryTest(unittest.TestCase):
    def test_ternary(self) -> None:
        self.assertEqual(
            evaluate_expression("'yes' if flag else 'no'", {"flag": True}),
            "yes",
        )
        self.assertEqual(
            evaluate_expression("'yes' if flag else 'no'", {"flag": False}),
            "no",
        )

    def test_ternary_short_circuits_unevaluated_branch(self) -> None:
        # The non-selected branch is never evaluated — a missing name in it
        # must not raise.
        self.assertEqual(
            evaluate_expression("a if flag else missing", {"flag": True, "a": 42}),
            42,
        )


class EvaluateFStringTest(unittest.TestCase):
    def test_fstring_simple(self) -> None:
        self.assertEqual(evaluate_expression('f"x={x}"', {"x": 42}), "x=42")

    def test_fstring_with_repr_conversion(self) -> None:
        self.assertEqual(evaluate_expression('f"{s!r}"', {"s": "hi"}), "'hi'")

    def test_fstring_with_format_spec(self) -> None:
        self.assertEqual(evaluate_expression('f"{x:04d}"', {"x": 7}), "0007")

    def test_fstring_with_expression(self) -> None:
        self.assertEqual(
            evaluate_expression('f"total={len(xs)}"', {"xs": [1, 2, 3]}),
            "total=3",
        )


class EvaluateStatementErrorTest(unittest.TestCase):
    """Statements produce targeted messages, not raw 'invalid syntax'."""

    def test_import_statement_gives_helpful_error(self) -> None:
        with self.assertRaises(EvaluateError) as cm:
            evaluate_expression("import os", {})
        message = str(cm.exception)
        self.assertIn("Imports aren't supported", message)
        self.assertNotIn("invalid syntax", message)

    def test_import_from_statement_gives_helpful_error(self) -> None:
        with self.assertRaises(EvaluateError) as cm:
            evaluate_expression("from os import path", {})
        self.assertIn("Imports aren't supported", str(cm.exception))

    def test_assignment_gives_helpful_error(self) -> None:
        with self.assertRaises(EvaluateError) as cm:
            evaluate_expression("x = 5", {})
        message = str(cm.exception)
        self.assertIn("Assignments aren't supported", message)
        self.assertNotIn("invalid syntax", message)

    def test_augmented_assignment_gives_helpful_error(self) -> None:
        with self.assertRaises(EvaluateError) as cm:
            evaluate_expression("x += 1", {"x": 0})
        self.assertIn("Augmented assignments aren't supported", str(cm.exception))

    def test_def_gives_helpful_error(self) -> None:
        with self.assertRaises(EvaluateError) as cm:
            evaluate_expression("def f(): pass", {})
        message = str(cm.exception)
        self.assertIn("Function and class definitions aren't supported", message)
        self.assertNotIn("invalid syntax", message)

    def test_class_def_gives_helpful_error(self) -> None:
        with self.assertRaises(EvaluateError) as cm:
            evaluate_expression("class C: pass", {})
        self.assertIn(
            "Function and class definitions aren't supported",
            str(cm.exception),
        )

    def test_for_loop_gives_helpful_error(self) -> None:
        with self.assertRaises(EvaluateError) as cm:
            evaluate_expression("for x in xs: pass", {"xs": [1, 2]})
        message = str(cm.exception)
        self.assertIn("for", message)
        self.assertIn("aren't supported", message)
        self.assertNotIn("invalid syntax", message)

    def test_delete_gives_helpful_error(self) -> None:
        with self.assertRaises(EvaluateError) as cm:
            evaluate_expression("del x", {"x": 1})
        self.assertIn("del", str(cm.exception))


class EvaluateRejectionTest(unittest.TestCase):
    """Unsupported grammar must be rejected — we're not a full Python evaluator."""

    def test_rejects_unknown_function(self) -> None:
        with self.assertRaises(EvaluateError) as cm:
            evaluate_expression("open('/etc/hosts')", {})
        self.assertIn("allowlist", str(cm.exception))
        self.assertIn("open", str(cm.exception))

    def test_rejects_exec_function(self) -> None:
        # ``exec`` is a builtin but intentionally not in our allowlist.
        with self.assertRaises(EvaluateError) as cm:
            evaluate_expression("exec('x=1')", {})
        self.assertIn("allowlist", str(cm.exception))

    def test_rejects_dunder_import_function(self) -> None:
        # ``__import__`` is a builtin but intentionally NOT in the
        # allowlist: allowing it would make the evaluator's safety
        # posture trivially bypassable
        # (e.g. ``__import__('os').system(...)``) on a shared
        # devserver / OnDemand where the loopback DAP port is
        # reachable by other local processes.
        with self.assertRaises(EvaluateError) as cm:
            evaluate_expression("__import__('os')", {})
        self.assertIn("allowlist", str(cm.exception))
        self.assertIn("__import__", str(cm.exception))

    def test_rejects_attribute_access_on_user_objects(self) -> None:
        # User-defined classes can expose custom ``__getattr__`` /
        # ``@property`` / descriptor code whose side effects aren't
        # captured in the snapshot. The evaluator refuses to dispatch
        # ``expr.attr`` through them — only SerializedObject (dict-style
        # lookup) and a small allowlist of built-in container / numeric
        # / text types are permitted.
        class _Shady:
            def __getattr__(self, name: str) -> str:
                return f"invoked __getattr__({name!r})"

        with self.assertRaises(EvaluateError) as cm:
            evaluate_expression("obj.anything", {"obj": _Shady()})
        self.assertIn("not supported", str(cm.exception))
        self.assertIn("_Shady", str(cm.exception))

    def test_rejects_method_call_on_user_objects(self) -> None:
        # Symmetric to the attribute case: a bound method call on a
        # non-allowlisted receiver is refused before the method runs.
        class _Shady:
            def do_thing(self) -> str:
                return "ran do_thing"

        with self.assertRaises(EvaluateError) as cm:
            evaluate_expression("obj.do_thing()", {"obj": _Shady()})
        self.assertIn("not supported", str(cm.exception))

    def test_rejects_comprehension(self) -> None:
        with self.assertRaises(EvaluateError):
            evaluate_expression("[x for x in xs]", {"xs": [1, 2]})

    def test_rejects_dict_comprehension(self) -> None:
        with self.assertRaises(EvaluateError):
            evaluate_expression("{x: x for x in xs}", {"xs": [1, 2]})

    def test_rejects_generator_expression(self) -> None:
        with self.assertRaises(EvaluateError):
            evaluate_expression("(x for x in xs)", {"xs": [1, 2]})

    def test_rejects_lambda(self) -> None:
        with self.assertRaises(EvaluateError):
            evaluate_expression("lambda x: x", {})

    def test_rejects_walrus(self) -> None:
        with self.assertRaises(EvaluateError):
            evaluate_expression("(x := 1)", {})

    def test_rejects_star_args_spread(self) -> None:
        with self.assertRaises(EvaluateError) as cm:
            evaluate_expression("len(*xs)", {"xs": [1, 2, 3]})
        self.assertIn("*args", str(cm.exception))

    def test_rejects_kwargs_spread(self) -> None:
        with self.assertRaises(EvaluateError) as cm:
            evaluate_expression("dict(**d)", {"d": {"k": 1}})
        self.assertIn("**kwargs", str(cm.exception))

    def test_rejects_invalid_syntax(self) -> None:
        with self.assertRaises(EvaluateError):
            evaluate_expression("x..y", {"x": 1})

    def test_rejects_empty(self) -> None:
        with self.assertRaises(EvaluateError):
            evaluate_expression("   ", {})


if __name__ == "__main__":
    unittest.main()
