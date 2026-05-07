# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Tests for tintype.dap.exceptions.

Focuses on the cause/context chain walker and its safety guards: cycle
detection, depth cap, and basic formatting behavior.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import MagicMock

from tintype.dap import exceptions
from tintype.dap.exceptions import build_exception_info


def _make_stacktrace(
    *,
    exception: Any = None,
    frames: list[Any] | None = None,
    cause: Any = None,
    context: Any = None,
) -> Any:
    """Build a MagicMock shaped like ``tintype.Stacktrace``."""
    st = MagicMock()
    st.exception_object = exception
    st.frames = frames or []
    st.get_cause.return_value = cause
    st.get_context.return_value = context
    return st


class BuildExceptionInfoTest(unittest.TestCase):
    def test_returns_none_for_non_exception_stacktrace(self) -> None:
        st = _make_stacktrace(exception=None)
        self.assertIsNone(build_exception_info(st))

    def test_returns_body_for_exception_stacktrace(self) -> None:
        exc = ValueError("boom")
        st = _make_stacktrace(exception=exc)
        body = build_exception_info(st)
        self.assertIsNotNone(body)
        assert body is not None
        self.assertEqual(body["breakMode"], "unhandled")
        self.assertIn("ValueError", body["exceptionId"])
        self.assertIn("boom", body["description"])


class FormatChainCycleGuardTest(unittest.TestCase):
    def test_direct_cycle_does_not_loop(self) -> None:
        """``a.cause is b`` and ``b.cause is a`` must not hang."""
        a = _make_stacktrace(exception=ValueError("a"))
        b = _make_stacktrace(exception=ValueError("b"))
        a.get_cause.return_value = b
        b.get_cause.return_value = a

        body = build_exception_info(a)
        assert body is not None
        trace = body["details"]["stackTrace"]
        # The chain should be cut short with a clear truncation note.
        self.assertIn("loops back", trace)

    def test_self_cycle_does_not_loop(self) -> None:
        """``a.cause is a`` must not hang either."""
        a = _make_stacktrace(exception=ValueError("a"))
        a.get_cause.return_value = a

        body = build_exception_info(a)
        assert body is not None
        self.assertIn("loops back", body["details"]["stackTrace"])

    def test_deep_chain_is_capped(self) -> None:
        """A 100-deep chain must terminate at the _MAX_CHAIN_DEPTH cap."""
        tail = _make_stacktrace(exception=ValueError("tail"))
        head = tail
        for i in range(100):
            new = _make_stacktrace(exception=ValueError(f"level-{i}"))
            new.get_cause.return_value = head
            head = new

        body = build_exception_info(head)
        assert body is not None
        self.assertIn(
            f"Truncated exception chain after {exceptions._MAX_CHAIN_DEPTH}",
            body["details"]["stackTrace"],
        )

    def test_short_chain_is_not_capped(self) -> None:
        """A chain well below the cap renders fully without truncation text."""
        inner = _make_stacktrace(exception=ValueError("inner"))
        outer = _make_stacktrace(exception=ValueError("outer"), cause=inner)

        body = build_exception_info(outer)
        assert body is not None
        trace = body["details"]["stackTrace"]
        self.assertNotIn("Truncated exception chain", trace)
        self.assertNotIn("loops back", trace)
        self.assertIn("outer", trace)
        self.assertIn("inner", trace)

    def test_context_chain_also_guarded(self) -> None:
        """Cycles via ``__context__`` are handled the same way as ``__cause__``."""
        a = _make_stacktrace(exception=ValueError("a"))
        b = _make_stacktrace(exception=ValueError("b"))
        a.get_context.return_value = b
        b.get_context.return_value = a

        body = build_exception_info(a)
        assert body is not None
        self.assertIn("loops back", body["details"]["stackTrace"])


if __name__ == "__main__":
    unittest.main()
