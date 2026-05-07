# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Expression evaluator for DAP ``evaluate`` requests.

We support a safe subset of Python expressions — enough to cover the common
VS Code watch-panel, hover, debug-console, and Copy Value inspection
patterns — without running arbitrary code (no ``eval()`` / ``exec()``). The
evaluator walks the AST directly and only invokes callables that are either:

* in the module-level :data:`_SAFE_BUILTINS` allowlist, or
* resolved via attribute access on an already-captured value (i.e. a method
  call on something that's already in the frame locals).

If the user typed a Python *statement* (``import``, ``x = 5``, etc.), we
produce a targeted error instead of the opaque ``invalid syntax`` that
``ast.parse(mode="eval")`` would otherwise yield.
"""

from __future__ import annotations

import ast
import builtins
import logging
import operator
from typing import Any, Callable

from tintype import SerializedDictObject, SerializedListObject, SerializedObject


logger: logging.Logger = logging.getLogger(__name__)


class EvaluateError(Exception):
    """Raised when an expression cannot be evaluated on the snapshot."""


# Allowlist of builtin names the evaluator may call. Deliberately excludes
# ``open``, ``input``, ``print``, ``exec``, ``eval``, ``compile``, ``exit``,
# ``quit``, ``help``, ``breakpoint``, ``globals``, ``locals``.
_SAFE_BUILTINS: dict[str, Any] = {
    name: getattr(builtins, name)
    for name in (
        # Type conversion / coercion
        "str",
        "repr",
        "int",
        "float",
        "bool",
        "complex",
        "bytes",
        "bytearray",
        # Collection constructors
        "list",
        "tuple",
        "set",
        "frozenset",
        "dict",
        # Introspection
        "type",
        "isinstance",
        "issubclass",
        "callable",
        "id",
        # NOTE: ``getattr``, ``hasattr``, and ``vars`` are deliberately
        # NOT allowlisted. Each one invokes user-defined
        # ``__getattr__`` / ``__getattribute__`` / property / descriptor
        # code, which would bypass the receiver-type filter enforced
        # by :func:`_get_attr` and hand control back to arbitrary user
        # code reachable via any local that happens to be a user class
        # instance. Users who need to reach into ``__dict__`` can write
        # ``obj.__dict__`` directly (the evaluator keeps the explicit
        # dunder-attribute path under the ``_ATTR_SAFE_TYPES`` guard).
        # Numeric helpers
        "abs",
        "min",
        "max",
        "sum",
        "round",
        "divmod",
        "pow",
        # Iteration / sequences
        "len",
        "sorted",
        "reversed",
        "range",
        "enumerate",
        "zip",
        "all",
        "any",
        "iter",
        "next",
        # String / number format helpers
        "chr",
        "ord",
        "hex",
        "oct",
        "bin",
        "format",
        "ascii",
        # NOTE: ``__import__`` is deliberately NOT in the allowlist.
        # Allowing it would make the hand-rolled safe-expression check
        # trivially bypassable (``__import__('os').system(...)``) from
        # any local process that can reach the loopback DAP port. If
        # users need a module reference in inspections, capture it as a
        # snapshot local before capture rather than resolving it live.
    )
}


_BINOP_HANDLERS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.BitAnd: operator.and_,
    ast.BitOr: operator.or_,
    ast.BitXor: operator.xor,
    ast.LShift: operator.lshift,
    ast.RShift: operator.rshift,
}


_UNARYOP_HANDLERS: dict[type, Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
    ast.Not: operator.not_,
    ast.Invert: operator.invert,
}


_CMPOP_HANDLERS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}


# Types whose attribute access via ``getattr`` is trusted to be free of
# user-defined side effects (custom ``__getattr__`` / ``@property`` /
# ``__getattribute__`` etc.). ``SerializedObject`` receivers use a
# dict-style lookup path in :func:`_get_attr` instead of going through
# this list. ``SerializedListObject`` / ``SerializedDictObject`` inherit
# from the built-in ``list`` / ``dict`` and therefore match this tuple
# implicitly.
#
# Restricting to this set matters because ``_frame_locals`` can return
# arbitrary Python objects (the locals captured into the snapshot), not
# only ``Serialized*`` variants. Without a receiver-type check, a single
# ``expr.attr`` could hand control back to user ``__getattr__`` code,
# silently bypassing the evaluator's "no arbitrary code" posture.
_ATTR_SAFE_TYPES: tuple[type, ...] = (
    # Text / bytes
    str,
    bytes,
    bytearray,
    memoryview,
    # Numerics
    int,
    float,
    bool,
    complex,
    # Containers (list / dict also cover SerializedListObject /
    # SerializedDictObject via subclass).
    list,
    tuple,
    set,
    frozenset,
    dict,
    # Iteration helpers returned by allowlisted builtins like
    # ``range(...)``, ``enumerate(...)``, ``zip(...)``. Safe to do
    # things like ``r.start`` on a range.
    range,
    slice,
    # ``x is None`` / ``None.__class__`` edge cases.
    type(None),
    # NOTE: the base ``type`` itself is deliberately NOT listed.
    # Allowing it would let an expression like
    # ``type(obj).some_method(obj)`` invoke user-defined methods on
    # any class instance exposed in the frame locals — every user
    # class is a ``type`` instance, so including ``type`` would route
    # attribute access around the ``_ATTR_SAFE_TYPES`` guard and hand
    # control back to user code. ``type(x).__name__``-style
    # introspection is intentionally not supported as a result;
    # callers who need the class name can use ``type(x)`` alone (the
    # repr renders ``<class '...'>``) or capture the name as a
    # snapshot local at capture time.
)


# Maps statement AST types to actionable error messages we show when
# ``ast.parse(mode="eval")`` rejects the input because it is a statement, not
# an expression.  Ordering matters: more specific types (e.g. ``AsyncFunctionDef``
# subclasses of ``FunctionDef``? no, they're siblings) should precede broader
# ones, but we just look up by exact type here.
_STATEMENT_MESSAGES: dict[type, str] = {
    ast.Import: (
        "Imports aren't supported in the snapshot viewer — snapshots are "
        "captured state, not a live REPL. Reference an already-captured "
        "module attribute (e.g. `module.attr`) or a literal value instead."
    ),
    ast.ImportFrom: (
        "Imports aren't supported in the snapshot viewer — snapshots are "
        "captured state, not a live REPL."
    ),
    ast.Assign: (
        "Assignments aren't supported — snapshots are read-only. Evaluate "
        "the right-hand side on its own if you just want to see its value."
    ),
    ast.AugAssign: (
        "Augmented assignments aren't supported — snapshots are read-only."
    ),
    ast.AnnAssign: (
        "Annotated assignments aren't supported — snapshots are read-only."
    ),
    ast.FunctionDef: (
        "Function and class definitions aren't supported — the viewer "
        "evaluates expressions, not statements."
    ),
    ast.AsyncFunctionDef: (
        "Function and class definitions aren't supported — the viewer "
        "evaluates expressions, not statements."
    ),
    ast.ClassDef: (
        "Function and class definitions aren't supported — the viewer "
        "evaluates expressions, not statements."
    ),
    ast.Return: (
        "`return` statements aren't supported; the viewer evaluates expressions only."
    ),
    ast.Raise: (
        "`raise` statements aren't supported; the viewer evaluates expressions only."
    ),
    ast.For: ("`for` loops aren't supported; the viewer evaluates expressions only."),
    ast.AsyncFor: (
        "`async for` loops aren't supported; the viewer evaluates expressions only."
    ),
    ast.While: (
        "`while` loops aren't supported; the viewer evaluates expressions only."
    ),
    ast.If: (
        "`if` statements aren't supported; the viewer evaluates expressions only."
    ),
    ast.With: (
        "`with` statements aren't supported; the viewer evaluates expressions only."
    ),
    ast.AsyncWith: (
        "`async with` statements aren't supported; the viewer evaluates "
        "expressions only."
    ),
    ast.Try: (
        "`try` statements aren't supported; the viewer evaluates expressions only."
    ),
    ast.Pass: ("`pass` isn't supported; the viewer evaluates expressions only."),
    ast.Break: ("`break` isn't supported; the viewer evaluates expressions only."),
    ast.Continue: (
        "`continue` isn't supported; the viewer evaluates expressions only."
    ),
    ast.Global: (
        "`global` statements aren't supported; the viewer evaluates expressions only."
    ),
    ast.Nonlocal: (
        "`nonlocal` statements aren't supported; the viewer evaluates expressions only."
    ),
    ast.Delete: (
        "`del` statements aren't supported; the viewer evaluates expressions only."
    ),
}


def evaluate_expression(expression: str, locals_: dict[str, Any]) -> Any:
    """Evaluate ``expression`` against ``locals_`` and return the value.

    Raises :class:`EvaluateError` for expressions the evaluator can't
    safely support.
    """
    stripped = expression.strip()
    if not stripped:
        raise EvaluateError("empty expression")

    try:
        tree = ast.parse(stripped, mode="eval")
    except SyntaxError as e:
        raise _syntax_error_to_evaluate_error(stripped, e) from e

    return _eval_node(tree.body, locals_)


def _syntax_error_to_evaluate_error(
    expression: str, original: SyntaxError
) -> EvaluateError:
    """Promote an opaque parse failure into something actionable.

    If the input parses as a statement in ``exec`` mode, surface a
    per-statement-kind message instead of the raw ``invalid syntax``. This is
    what lets the debug console say "Imports aren't supported" rather than
    making the user guess why their ``import os`` was rejected.
    """
    try:
        tree = ast.parse(expression, mode="exec")
    except SyntaxError:
        return EvaluateError(f"invalid expression: {original.msg}")

    if not tree.body:
        return EvaluateError(f"invalid expression: {original.msg}")

    top = tree.body[0]
    # An ``Expr`` statement wraps a lone expression. If exec mode accepts that
    # but eval mode rejected it, something odd is going on — fall back
    # defensively rather than masking the issue with a statement message.
    if isinstance(top, ast.Expr):
        return EvaluateError(f"invalid expression: {original.msg}")

    message = _STATEMENT_MESSAGES.get(type(top))
    if message is not None:
        return EvaluateError(message)

    return EvaluateError(f"invalid expression: {original.msg}")


def _eval_node(node: ast.AST, locals_: dict[str, Any]) -> Any:
    if isinstance(node, ast.Name):
        if node.id in locals_:
            return locals_[node.id]
        if node.id in _SAFE_BUILTINS:
            return _SAFE_BUILTINS[node.id]
        raise EvaluateError(f"name '{node.id}' is not defined")

    if isinstance(node, ast.Attribute):
        parent = _eval_node(node.value, locals_)
        return _get_attr(parent, node.attr)

    if isinstance(node, ast.Subscript):
        parent = _eval_node(node.value, locals_)
        key = _eval_node(node.slice, locals_)
        return _get_item(parent, key)

    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Call):
        return _eval_call(node, locals_)

    if isinstance(node, ast.BinOp):
        handler = _BINOP_HANDLERS.get(type(node.op))
        if handler is None:
            raise EvaluateError(
                f"unsupported binary operator: {type(node.op).__name__}"
            )
        left = _eval_node(node.left, locals_)
        right = _eval_node(node.right, locals_)
        try:
            return handler(left, right)
        except EvaluateError:
            raise
        except Exception as e:  # noqa: BLE001
            raise EvaluateError(f"operator failed: {e}") from e

    if isinstance(node, ast.UnaryOp):
        unary = _UNARYOP_HANDLERS.get(type(node.op))
        if unary is None:
            raise EvaluateError(f"unsupported unary operator: {type(node.op).__name__}")
        return unary(_eval_node(node.operand, locals_))

    if isinstance(node, ast.BoolOp):
        return _eval_boolop(node, locals_)

    if isinstance(node, ast.Compare):
        return _eval_compare(node, locals_)

    if isinstance(node, ast.List):
        return [_eval_node(el, locals_) for el in node.elts]

    if isinstance(node, ast.Tuple):
        return tuple(_eval_node(el, locals_) for el in node.elts)

    if isinstance(node, ast.Set):
        return {_eval_node(el, locals_) for el in node.elts}

    if isinstance(node, ast.Dict):
        result: dict[Any, Any] = {}
        for k_node, v_node in zip(node.keys, node.values):
            if k_node is None:
                raise EvaluateError("dict unpacking (**) is not supported")
            result[_eval_node(k_node, locals_)] = _eval_node(v_node, locals_)
        return result

    if isinstance(node, ast.IfExp):
        cond = _eval_node(node.test, locals_)
        if cond:
            return _eval_node(node.body, locals_)
        return _eval_node(node.orelse, locals_)

    if isinstance(node, ast.JoinedStr):
        return "".join(_eval_fstring_part(part, locals_) for part in node.values)

    if isinstance(node, ast.FormattedValue):
        return _eval_fstring_part(node, locals_)

    raise EvaluateError(f"unsupported expression node: {type(node).__name__}")


def _eval_fstring_part(node: ast.AST, locals_: dict[str, Any]) -> str:
    """Evaluate a single segment of an f-string (``JoinedStr.values`` entry)."""
    if isinstance(node, ast.Constant):
        if not isinstance(node.value, str):
            raise EvaluateError(
                "unsupported f-string segment: expected a string constant"
            )
        return node.value
    if isinstance(node, ast.FormattedValue):
        value = _eval_node(node.value, locals_)
        conv = node.conversion
        if conv == ord("s"):
            value = str(value)
        elif conv == ord("r"):
            value = repr(value)
        elif conv == ord("a"):
            value = ascii(value)
        elif conv != -1:
            raise EvaluateError(f"unsupported f-string conversion: {chr(conv)!r}")
        spec = ""
        if node.format_spec is not None:
            spec_val = _eval_node(node.format_spec, locals_)
            if not isinstance(spec_val, str):
                raise EvaluateError("f-string format spec must evaluate to a string")
            spec = spec_val
        return format(value, spec)
    raise EvaluateError(f"unsupported f-string node: {type(node).__name__}")


def _eval_call(node: ast.Call, locals_: dict[str, Any]) -> Any:
    """Evaluate a function / method call node.

    ``func`` must resolve to either:
      * a builtin name in :data:`_SAFE_BUILTINS`, or
      * an attribute chain terminating in a method on a value we already have.

    ``*args`` / ``**kwargs`` spread is not supported — inspection use cases
    don't need it and it complicates the call site.
    """
    for arg in node.args:
        if isinstance(arg, ast.Starred):
            raise EvaluateError("`*args` spread is not supported in calls")
    for kw in node.keywords:
        if kw.arg is None:
            raise EvaluateError("`**kwargs` spread is not supported in calls")

    func_expr = node.func
    if isinstance(func_expr, ast.Name):
        name = func_expr.id
        if name not in _SAFE_BUILTINS:
            raise EvaluateError(f"function '{name}' is not in the evaluator allowlist")
        callable_ = _SAFE_BUILTINS[name]
    elif isinstance(func_expr, ast.Attribute):
        receiver = _eval_node(func_expr.value, locals_)
        callable_ = _get_attr(receiver, func_expr.attr)
    else:
        raise EvaluateError(f"unsupported call target: {type(func_expr).__name__}")

    args = [_eval_node(a, locals_) for a in node.args]
    kwargs = {kw.arg: _eval_node(kw.value, locals_) for kw in node.keywords}

    try:
        return callable_(*args, **kwargs)
    except EvaluateError:
        raise
    except Exception as e:  # noqa: BLE001
        raise EvaluateError(f"call failed: {e}") from e


def _eval_boolop(node: ast.BoolOp, locals_: dict[str, Any]) -> Any:
    """Evaluate ``and`` / ``or`` with Python's native short-circuit semantics."""
    if isinstance(node.op, ast.And):
        result: Any = True
        for child in node.values:
            result = _eval_node(child, locals_)
            if not result:
                return result
        return result
    if isinstance(node.op, ast.Or):
        result = False
        for child in node.values:
            result = _eval_node(child, locals_)
            if result:
                return result
        return result
    raise EvaluateError(f"unsupported boolean operator: {type(node.op).__name__}")


def _eval_compare(node: ast.Compare, locals_: dict[str, Any]) -> bool:
    """Evaluate chained comparisons (``a < b < c``) with single-evaluation semantics."""
    left = _eval_node(node.left, locals_)
    for op, comparator in zip(node.ops, node.comparators):
        handler = _CMPOP_HANDLERS.get(type(op))
        if handler is None:
            raise EvaluateError(f"unsupported comparison operator: {type(op).__name__}")
        right = _eval_node(comparator, locals_)
        if not handler(left, right):
            return False
        left = right
    return True


def _get_attr(value: Any, name: str) -> Any:
    """Access an attribute on a snapshot value.

    Two access paths:

    * ``SerializedObject`` receivers — dict-style lookup via the captured
      ``__dict__``. ``getattr`` is deliberately NOT used because it could
      invoke user-defined ``__getattr__`` / ``@property`` / descriptor
      code that isn't reflected in the snapshot.
    * Built-in container / numeric / text types in :data:`_ATTR_SAFE_TYPES`
      — generic ``getattr`` pass-through. These types have a bounded set
      of methods defined by CPython and don't hand control back to user
      code. ``SerializedListObject`` / ``SerializedDictObject`` inherit
      from ``list`` / ``dict`` respectively and match implicitly.

    Any other receiver type raises :class:`EvaluateError` — we refuse
    to evaluate attribute access on user-defined classes because their
    attribute machinery can execute arbitrary code and a snapshot is
    supposed to be a read-only view.
    """
    if isinstance(value, SerializedObject):
        attrs = getattr(value, "__dict__", {}) or {}
        if name in attrs:
            return attrs[name]
        raise EvaluateError(f"'{type(value).__name__}' has no attribute '{name}'")
    if not isinstance(value, _ATTR_SAFE_TYPES):
        raise EvaluateError(
            f"attribute access on '{type(value).__name__}' objects is not "
            f"supported by the snapshot evaluator (would invoke arbitrary "
            f"Python getattr / property / descriptor code)"
        )
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
