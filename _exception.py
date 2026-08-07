# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Reconstruct a ``BaseException`` from a snapshot ``Stacktrace``.

The returned exception carries the reconstructed ``__traceback__`` and a
wired ``__cause__`` / ``__context__`` chain, so it can be fed to a debugger
or the ``traceback`` module.

Caveat: the original exception *classes* are not recoverable from a
snapshot — ``Stacktrace.exception_object`` is a serialized object — so every
node in the reconstructed chain is a plain ``Exception`` carrying the
original message. Read the true class name from the message (e.g.
``KeyError: 'foo'``) or from ``exception_object`` if you need it.
"""

from types import TracebackType
from typing import Any

# Mirror CPython's cap in ``traceback.py`` (and tintype's DAP renderer) so a
# cyclic or pathologically deep ``__cause__`` / ``__context__`` chain cannot
# spin forever or blow the stack.
_MAX_CHAIN_DEPTH = 10


def _synthesize(stacktrace: Any) -> Exception:
    """Build a single synthetic ``Exception`` (message + traceback, no chain)
    from one stacktrace."""
    orig = stacktrace.exception_object
    exc = Exception(str(orig) if orig is not None else "(unknown exception)")
    tb: TracebackType | None = stacktrace.get_traceback()
    if tb is not None:
        exc.__traceback__ = tb
    return exc


def reconstruct_exception(stacktrace: Any) -> BaseException | None:
    """Reconstruct a ``BaseException`` with its traceback and chained causes.

    Returns ``None`` when the stacktrace has no exception (e.g. a thread
    snapshot) or no reconstructable traceback. At most ``_MAX_CHAIN_DEPTH``
    (10) chained exceptions are wired; a longer chain is silently truncated at
    the tail, matching CPython's own cap in ``traceback.py``. See the module
    docstring for the "class is always ``Exception``" caveat.
    """

    if stacktrace.exception_object is None or not stacktrace.frames:
        return None

    head = _synthesize(stacktrace)

    current_st = stacktrace
    current_exc: BaseException = head
    seen: set[int] = {id(stacktrace)}
    depth = 0
    while depth < _MAX_CHAIN_DEPTH:
        # Prefer ``__cause__`` (``raise X from Y``) over ``__context__``
        # (implicit during-handling chain), matching CPython's precedence.
        nxt = current_st.get_cause()
        wire_cause = nxt is not None
        if nxt is None:
            nxt = current_st.get_context()
        if nxt is None or id(nxt) in seen:
            break
        seen.add(id(nxt))
        nxt_exc = _synthesize(nxt)
        if wire_cause:
            current_exc.__cause__ = nxt_exc
            current_exc.__suppress_context__ = True
        else:
            current_exc.__context__ = nxt_exc
        current_st = nxt
        current_exc = nxt_exc
        depth += 1

    return head
