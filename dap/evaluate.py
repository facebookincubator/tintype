# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Minimal expression evaluator for DAP ``evaluate`` requests.

We support a safe subset of Python — name lookup, attribute access, and
integer / string subscript — against a frame's locals. That's enough to
cover the common VS Code hover + watch-panel use cases without opening
the door to arbitrary code execution on a read-only snapshot.
"""

from __future__ import annotations

import ast
import logging
from typing import Any

from tintype import SerializedDictObject, SerializedListObject, SerializedObject


logger: logging.Logger = logging.getLogger(__name__)


class EvaluateError(Exception):
    """Raised when an expression cannot be evaluated on the snapshot."""


def evaluate_expression(expression: str, locals_: dict[str, Any]) -> Any:
    """Evaluate ``expression`` against ``locals_`` and return the value.

    Supported grammar:
        * Name:        ``foo``
        * Attribute:   ``foo.bar.baz``
        * Subscript:   ``foo[0]``, ``foo["key"]``, ``foo[-1]``
        * Chained:     ``foo.bar[0].baz``

    Any other node type (call, operator, lambda, comprehension, ...)
    raises :class:`EvaluateError`.
    """
    stripped = expression.strip()
    if not stripped:
        raise EvaluateError("empty expression")

    try:
        tree = ast.parse(stripped, mode="eval")
    except SyntaxError as e:
        raise EvaluateError(f"invalid expression: {e.msg}") from e

    return _eval_node(tree.body, locals_)


def _eval_node(node: ast.AST, locals_: dict[str, Any]) -> Any:
    if isinstance(node, ast.Name):
        if node.id not in locals_:
            raise EvaluateError(f"name '{node.id}' is not defined")
        return locals_[node.id]

    if isinstance(node, ast.Attribute):
        parent = _eval_node(node.value, locals_)
        return _get_attr(parent, node.attr)

    if isinstance(node, ast.Subscript):
        parent = _eval_node(node.value, locals_)
        key = _eval_constant(node.slice)
        return _get_item(parent, key)

    if isinstance(node, ast.Constant):
        return node.value

    raise EvaluateError(
        f"unsupported expression node: {type(node).__name__}. "
        "Only names, attribute access, and literal subscripts are supported."
    )


def _eval_constant(node: ast.AST) -> Any:
    """Evaluate a node expected to be a literal (for subscript keys)."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _eval_constant(node.operand)
        if isinstance(inner, (int, float)):
            return -inner
    raise EvaluateError("subscript keys must be literal strings, numbers, or booleans")


def _get_attr(value: Any, name: str) -> Any:
    """Access an attribute, transparent to SerializedObject / __dict__."""
    if isinstance(value, SerializedObject):
        attrs = getattr(value, "__dict__", {}) or {}
        if name in attrs:
            return attrs[name]
        raise EvaluateError(f"'{type(value).__name__}' has no attribute '{name}'")
    # Generic attribute lookup (covers SerializedListObject / SerializedDictObject
    # subclass attrs too, because they expose them on the instance __dict__).
    try:
        return getattr(value, name)
    except AttributeError as e:
        raise EvaluateError(str(e)) from e


def _get_item(value: Any, key: Any) -> Any:
    """Subscript access that works for dicts, lists, tuples, and SerializedDict."""
    if isinstance(value, (dict, SerializedDictObject)):
        if key in value:
            return value[key]
        raise EvaluateError(f"key {key!r} not in mapping")
    if isinstance(value, (list, tuple, SerializedListObject)):
        if not isinstance(key, int):
            raise EvaluateError(
                f"sequence indices must be integers, got {type(key).__name__}"
            )
        try:
            return value[key]
        except IndexError as e:
            raise EvaluateError(str(e)) from e
    raise EvaluateError(
        f"'{type(value).__name__}' object is not subscriptable in tintype snapshots"
    )
