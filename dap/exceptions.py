# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Build DAP ``ExceptionInfoResponse`` bodies from snapshot stacktraces.

Walks the ``__cause__`` / ``__context__`` chain attached to a stacktrace
and formats a single description string that mirrors what CPython would
print via :func:`traceback.print_exception`.
"""

from __future__ import annotations

import io
import logging
from typing import Any, TYPE_CHECKING

from tintype import Stacktrace


if TYPE_CHECKING:
    from tintype.dap.sources import SourceRegistry


logger: logging.Logger = logging.getLogger(__name__)


_CAUSE_MSG = "\nThe above exception was the direct cause of the following exception:\n"
_CONTEXT_MSG = "\nDuring handling of the above exception, another exception occurred:\n"
# Mirrors CPython's own cap in ``traceback.py`` so we never blow the stack
# (or spin forever) on a cyclic / pathological chain.
_MAX_CHAIN_DEPTH = 10


def build_exception_info(
    stacktrace: Stacktrace,
    sources: SourceRegistry | None = None,
) -> dict[str, Any] | None:
    """Return a DAP ``ExceptionInfoResponse.body`` or ``None`` for non-exception stacktraces.

    When ``sources`` is supplied we resolve source-line decorations via the
    snapshot's embedded source registry (in-memory + extracted-dir). This is
    preferred over a viewer-side ``linecache`` read because ``frame.file_path``
    is captured on a potentially different machine — a viewer with a
    same-named file at that path would otherwise surface its current
    contents, which is misleading at best and an info-leak at worst.

    The response's ``details`` object carries two representations of the
    cause/context chain:

    * ``details.stackTrace`` — a pre-rendered CPython-style multi-line
      string for clients that show the raw traceback as-is.
    * ``details.innerException`` — a DAP-standard nested array of
      :class:`ExceptionDetails` objects, one per chained exception,
      produced via :func:`_build_details`. This lets DAP clients (e.g.
      VS Code's Exception panel) render the chain as an expandable tree
      rather than a single blob of text. The outermost (first raised)
      exception is the top-level ``details``; each ``innerException``
      entry is what caused (or was being handled when) the level above
      it was raised. Chain traversal honours ``__cause__`` first, then
      ``__context__``, mirroring CPython's ``traceback.print_exception``.
    """
    if stacktrace.exception_object is None:
        return None

    details = _build_details(stacktrace, depth=0, seen={id(stacktrace)})
    # ``stackTrace`` (the pre-rendered string) sits at the top level for
    # clients that ignore ``innerException``. Source-line decoration
    # lives there — ``_build_details`` itself is type/message-only.
    details["stackTrace"] = _format_chain(stacktrace, sources)

    type_name = details.get("typeName", "")
    message = details.get("message", "")
    return {
        "exceptionId": type_name,
        "description": message,
        "breakMode": "unhandled",
        "details": details,
    }


def _build_details(
    stacktrace: Stacktrace,
    *,
    depth: int,
    seen: set[int],
) -> dict[str, Any]:
    """Build a DAP ``ExceptionDetails`` node for ``stacktrace``, with
    any cause/context chain nested under ``innerException``.

    The chain walk mirrors :func:`_format_chain`'s guards:

    * Cycles are detected via the ``seen`` set of Python ``id()``s
      passed through the recursion.
    * Depth is capped at :data:`_MAX_CHAIN_DEPTH`.

    Inner nodes do **not** duplicate the pre-rendered ``stackTrace``
    string — that's only set on the top-level details in
    :func:`build_exception_info` to avoid exponential blowup of
    redundant text on deeply chained exceptions. Because source-line
    lookup only matters for the pre-rendered ``stackTrace``, this
    function does not need a :class:`SourceRegistry`.
    """
    exc = stacktrace.exception_object
    type_name, message = _describe(exc) if exc is not None else ("", "")
    node: dict[str, Any] = {
        "message": message,
        "typeName": type_name,
        "fullTypeName": type_name,
    }
    if depth >= _MAX_CHAIN_DEPTH:
        return node

    # Prefer ``__cause__`` (``raise X from Y``) over ``__context__``
    # (implicit during-handling chain), matching CPython's own
    # precedence in ``traceback.print_exception``.
    next_stacktrace: Stacktrace | None = stacktrace.get_cause()
    if next_stacktrace is None:
        next_stacktrace = stacktrace.get_context()
    if next_stacktrace is None:
        return node
    if id(next_stacktrace) in seen:
        return node
    seen.add(id(next_stacktrace))
    node["innerException"] = [
        _build_details(next_stacktrace, depth=depth + 1, seen=seen)
    ]
    return node


def _describe(exc: object) -> tuple[str, str]:
    """Return ``(type_name, message)`` for a snapshot exception object.

    Snapshot exceptions are :class:`~tintype.SerializedObject` instances —
    the original exception class can't be reconstructed, so we derive the
    name from ``type(exc).__name__`` with a best-effort lookup of the
    serialized class name attribute when present.
    """
    # SerializedObject stores the original type's name as part of its repr
    # (e.g. ``ValueError('boom')``). Prefer that over the literal
    # ``SerializedObject`` class name.
    serialized_cls = type(exc).__name__
    type_name = serialized_cls

    try:
        rep = repr(exc)
    except Exception:  # noqa: BLE001
        rep = serialized_cls

    # Heuristic: repr is typically ``TypeName(args...)``.
    paren = rep.find("(")
    if paren > 0:
        candidate = rep[:paren].strip()
        if candidate and candidate != serialized_cls:
            type_name = candidate

    message: str
    try:
        message = str(exc)
    except Exception:  # noqa: BLE001
        message = rep

    return type_name, message


def _format_chain(
    stacktrace: Stacktrace,
    sources: SourceRegistry | None,
) -> str:
    """Render the full cause/context chain as a multi-line string.

    Guards against two failure modes:

    * A cyclic chain (``a.__cause__ is b`` and ``b.__cause__ is a``) —
      tracked via a ``seen`` set keyed on Python ``id()`` of each
      stacktrace we render.
    * A pathologically long chain — bounded by :data:`_MAX_CHAIN_DEPTH`
      (matches CPython's own cap in ``traceback.py``).

    When either guard fires we append an explicit truncation note so the
    user knows the output is not a silent cut-off.
    """
    buf = io.StringIO()
    _render_one(buf, stacktrace, sources)

    seen: set[int] = {id(stacktrace)}
    current: Stacktrace | None = stacktrace
    depth = 0
    while current is not None:
        if depth >= _MAX_CHAIN_DEPTH:
            buf.write(
                f"\n[tintype] Truncated exception chain after {_MAX_CHAIN_DEPTH} frames.\n"
            )
            break

        cause = current.get_cause()
        if cause is not None:
            if id(cause) in seen:
                buf.write("\n[tintype] Exception chain loops back; truncating.\n")
                break
            seen.add(id(cause))
            buf.write(_CAUSE_MSG)
            _render_one(buf, cause, sources)
            current = cause
            depth += 1
            continue

        context = current.get_context()
        if context is not None:
            if id(context) in seen:
                buf.write("\n[tintype] Exception chain loops back; truncating.\n")
                break
            seen.add(id(context))
            buf.write(_CONTEXT_MSG)
            _render_one(buf, context, sources)
            current = context
            depth += 1
            continue

        break

    return buf.getvalue()


def _source_line_from_registry(
    sources: SourceRegistry | None,
    file_path: str,
    line_number: int,
) -> str:
    """Return the source line at ``line_number`` (1-based) from the registry.

    Returns an empty string if the file is not embedded, the line number is
    out of range, or ``sources`` is ``None``. Deliberately does **not** fall
    back to reading the viewer's filesystem — ``frame.file_path`` is
    captured on a different machine and may collide with an unrelated local
    file.
    """
    if sources is None or not file_path or line_number <= 0:
        return ""
    content = sources.get_by_path(file_path)
    if content is None:
        return ""
    lines = content.splitlines()
    if line_number > len(lines):
        return ""
    return lines[line_number - 1]


def _render_one(
    out: io.StringIO,
    stacktrace: Stacktrace,
    sources: SourceRegistry | None,
) -> None:
    """Render a single stacktrace as ``Traceback (most recent call last):`` + frames."""
    out.write("Traceback (most recent call last):\n")

    # Frames in tintype are stored innermost-last (matching CPython traceback
    # order after ``get_traceback`` reverses them). Mirror that order here.
    for frame in stacktrace.frames:
        out.write(f'  File "{frame.file_path}", line {frame.line_number}, ')
        out.write(f"in {frame.function_name}\n")
        # Resolve the source line through the snapshot's embedded source
        # registry (NOT via ``linecache`` on the viewer's filesystem — see
        # the docstring on ``build_exception_info`` for why).
        line = _source_line_from_registry(sources, frame.file_path, frame.line_number)
        if line:
            out.write(f"    {line.strip()}\n")

    exc = stacktrace.exception_object
    if exc is not None:
        type_name, message = _describe(exc)
        if message:
            out.write(f"{type_name}: {message}\n")
        else:
            out.write(f"{type_name}\n")
