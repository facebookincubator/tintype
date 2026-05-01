# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Tests for Stacktrace.get_traceback() f_back correctness."""

import os
import tempfile
import unittest
from types import TracebackType

import tintype


def _level_c() -> None:
    """Innermost function in the call chain."""
    tintype.take_snapshot()


def _level_b() -> None:
    _level_c()


def _level_a() -> None:
    _level_b()


def _take_snapshot_with_depth() -> tintype.Stacktrace:
    """Build a real snapshot via a known call chain and return its stacktrace.

    Call chain: _take_snapshot_with_depth -> _level_a -> _level_b -> _level_c
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "test.pytb")
        tintype.initialize()
        _level_a()
        tintype.finalize(path)

        reader = tintype.SnapshotReader(path)
        snaps = reader.get_all_snapshots()
        assert len(snaps) == 1
        stacktraces = snaps[0].stacktraces
        assert len(stacktraces) > 0
        return next(iter(stacktraces.values()))


class GetTracebackTest(unittest.TestCase):
    def test_f_back_chain_is_correct(self) -> None:
        """Each tb_frame.f_back should point to the previous entry's tb_frame.

        For a traceback with entries[0] (outermost) through entries[N-1]
        (innermost), the correct f_back chain is:
          entries[0].tb_frame.f_back is None
          entries[i].tb_frame.f_back is entries[i-1].tb_frame  (for i > 0)
        """
        # Setup: capture a real stacktrace.
        stacktrace = _take_snapshot_with_depth()

        # Execute
        tb = stacktrace.get_traceback()

        # Collect all traceback entries.
        self.assertIsNotNone(tb)
        assert tb is not None
        entries: list[TracebackType] = []
        cur: TracebackType | None = tb
        while cur is not None:
            entries.append(cur)
            cur = cur.tb_next

        # We should have at least the 3 helper frames.
        self.assertGreaterEqual(len(entries), 3)

        # Assert: outermost frame has no caller in the reconstructed chain.
        self.assertIsNone(
            entries[0].tb_frame.f_back,
            "outermost tb_frame.f_back should be None",
        )

        # Assert: every subsequent frame's f_back is the previous frame.
        for i in range(1, len(entries)):
            self.assertIs(
                entries[i].tb_frame.f_back,
                entries[i - 1].tb_frame,
                f"entries[{i}].tb_frame.f_back should be entries[{i - 1}].tb_frame",
            )

    def test_known_functions_appear_in_order(self) -> None:
        """Verify _level_a, _level_b, _level_c appear outermost-to-innermost."""
        # Setup
        stacktrace = _take_snapshot_with_depth()

        # Execute
        tb = stacktrace.get_traceback()

        # Assert
        self.assertIsNotNone(tb)
        assert tb is not None

        func_names: list[str] = []
        cur: TracebackType | None = tb
        while cur is not None:
            func_names.append(cur.tb_frame.f_code.co_name)
            cur = cur.tb_next

        self.assertIn("_level_a", func_names)
        self.assertIn("_level_b", func_names)
        self.assertIn("_level_c", func_names)

        self.assertLess(func_names.index("_level_a"), func_names.index("_level_b"))
        self.assertLess(func_names.index("_level_b"), func_names.index("_level_c"))

    def test_qualname_roundtrips_through_traceback(self) -> None:
        """Verify co_qualname round-trips through the traceback generation."""
        stacktrace = _take_snapshot_with_depth()

        tb = stacktrace.get_traceback()

        self.assertIsNotNone(tb)
        assert tb is not None

        qualnames: list[str] = []
        cur: TracebackType | None = tb
        while cur is not None:
            qualnames.append(cur.tb_frame.f_code.co_qualname)
            cur = cur.tb_next

        self.assertIn("_level_a", qualnames)
        self.assertIn("_level_b", qualnames)
        self.assertIn("_level_c", qualnames)

    def test_function_qualname_available_on_frames(self) -> None:
        """Verify function_qualname is available on snapshot frames."""
        stacktrace = _take_snapshot_with_depth()

        qualnames = [f.function_qualname for f in stacktrace.frames]
        self.assertIn("_level_a", qualnames)
        self.assertIn("_level_b", qualnames)
        self.assertIn("_level_c", qualnames)

    def test_empty_stacktrace_returns_none(self) -> None:
        """A stacktrace with no frames should produce None."""
        # Setup: capture a real stacktrace, then clear its frames.
        stacktrace = _take_snapshot_with_depth()
        stacktrace.frames.clear()

        # Execute
        result = stacktrace.get_traceback()

        # Assert
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
