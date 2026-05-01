# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Tests for snapshot cancellation, max_frames, and timeout."""

import os
import tempfile
import time
import unittest

import tintype


class SnapshotCancellationTest(unittest.TestCase):
    """Tests for snapshot cancellation, max_frames, and timeout."""

    def _deep_call_and_snapshot(self, depth: int = 10) -> None:
        """Helper that creates a deep call stack before taking a tintype."""
        if depth > 0:
            self._deep_call_and_snapshot(depth - 1)
        else:
            tintype.take_snapshot()

    def test_normal_snapshot_not_truncated(self) -> None:
        """Test that a normal snapshot has truncated=False."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()
            tintype.take_snapshot()
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snapshots = reader.get_all_snapshots()
            self.assertEqual(len(snapshots), 1)
            self.assertFalse(snapshots[0].truncated)
            for st in snapshots[0].stacktraces.values():
                self.assertFalse(st.truncated)

    def test_max_frames_limits_frames(self) -> None:
        """Test that max_frames limits the number of frames captured."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()
            self._deep_call_and_snapshot(depth=20)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            all_snapshots = reader.get_all_snapshots()
            self.assertGreater(len(all_snapshots[0].frames()), 5)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()
            tintype.take_snapshot(max_frames=5)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snapshots = reader.get_all_snapshots()
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(len(snapshots[0].frames()), 5)
            self.assertTrue(snapshots[0].truncated)
            for st in snapshots[0].stacktraces.values():
                self.assertTrue(st.truncated)

    def test_max_frames_none_captures_all(self) -> None:
        """Test that max_frames=None captures all frames."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()
            tintype.take_snapshot(max_frames=None)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snapshots = reader.get_all_snapshots()
            self.assertEqual(len(snapshots), 1)
            self.assertGreater(len(snapshots[0].frames()), 0)
            self.assertFalse(snapshots[0].truncated)

    def test_cancel_before_snapshot(self) -> None:
        """Test that cancel before take_snapshot results in no tintype."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()
            tintype.cancel_snapshot()
            tintype.take_snapshot()
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snapshots = reader.get_all_snapshots()
            self.assertEqual(len(snapshots), 0)

    def test_cancel_before_snapshot_returns_none(self) -> None:
        """Test that take_snapshot returns None when cancelled."""
        tintype.initialize()
        tintype.cancel_snapshot()
        result = tintype.take_snapshot()
        self.assertIsNone(result)
        tintype.finalize()

    def test_cancel_returns_none_with_previous_snapshots(self) -> None:
        """Test that cancelled take_snapshot returns None even when previous
        snapshots exist (not the previous snapshot)."""
        tintype.initialize()
        first = tintype.take_snapshot()
        self.assertIsNotNone(first)

        tintype.cancel_snapshot()
        result = tintype.take_snapshot()
        self.assertIsNone(result)
        tintype.finalize()

    def test_cancel_flag_cleared_after_snapshot(self) -> None:
        """Test that cancel flag is cleared after a snapshot completes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()

            tintype.cancel_snapshot()
            tintype.take_snapshot()

            tintype.take_snapshot()
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snapshots = reader.get_all_snapshots()
            self.assertEqual(len(snapshots), 1)
            self.assertFalse(snapshots[0].truncated)

    def test_timeout_long_completes_normally(self) -> None:
        """Test that a long timeout allows the snapshot to complete normally."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()
            tintype.take_snapshot(timeout=10.0)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snapshots = reader.get_all_snapshots()
            self.assertEqual(len(snapshots), 1)
            self.assertFalse(snapshots[0].truncated)

    def test_truncated_snapshot_is_readable(self) -> None:
        """Test that a truncated snapshot can be read and its data is valid."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()
            self._deep_call_and_snapshot(depth=20)
            tintype.take_snapshot(max_frames=3)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snapshots = reader.get_all_snapshots()
            self.assertEqual(len(snapshots), 2)

            self.assertFalse(snapshots[0].truncated)
            self.assertGreater(len(snapshots[0].frames()), 3)

            self.assertTrue(snapshots[1].truncated)
            self.assertEqual(len(snapshots[1].frames()), 3)
            for frame in snapshots[1].frames():
                self.assertIsInstance(frame.file_path, str)
                self.assertIsInstance(frame.function_name, str)

    def test_timeout_truncates_with_slow_repr(self) -> None:
        """A short timeout should truncate when serialization is slow.

        Uses a custom class whose __repr__ sleeps, making object serialization
        take longer than the timeout. The cancel timer fires mid-serialization,
        producing a truncated tintype.
        """

        class SlowRepr:
            def __repr__(self) -> str:
                time.sleep(0.2)
                return "SlowRepr()"

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()

            # The slow locals must be in an outer frame so at least one fast
            # (inner) frame is written before the cancel timer fires.
            def outer_with_slow_locals() -> None:
                a = SlowRepr()  # noqa: F841
                b = SlowRepr()  # noqa: F841
                c = SlowRepr()  # noqa: F841
                inner_take_snapshot()

            def inner_take_snapshot() -> None:
                fast_var = 42  # noqa: F841
                result = tintype.take_snapshot(timeout=0.05)
                self.assertIsNotNone(result)
                assert result is not None
                self.assertTrue(
                    result.truncated,
                    "take_snapshot should be truncated due to timeout "
                    "during slow repr()",
                )

            outer_with_slow_locals()
            tintype.finalize(path)


if __name__ == "__main__":
    unittest.main()
