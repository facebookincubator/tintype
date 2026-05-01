# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Tests for frame file path filtering in snapshots."""

import os
import tempfile
import unittest

import tintype


class SnapshotFrameFilteringTest(unittest.TestCase):
    """Tests for frame_file_path_filters in initialize()."""

    def test_no_filters_captures_all_frames(self) -> None:
        """Baseline: initialize() with no filters captures all frames."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()
            tintype.take_snapshot()
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snapshots = reader.get_all_snapshots()
            self.assertEqual(len(snapshots), 1)
            self.assertGreater(len(snapshots[0].frames()), 0)
            self.assertFalse(snapshots[0].truncated)

    def test_filter_removes_matching_frames(self) -> None:
        """Frames matching a filter substring are excluded."""
        # First, capture without filters to get the baseline frame count
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()
            tintype.take_snapshot()
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            baseline = reader.get_all_snapshots()
            baseline_count = len(baseline[0].frames())

        # Now capture with a filter that matches unittest runner frames
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize(frame_file_path_filters=["unittest"])
            tintype.take_snapshot()
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            filtered = reader.get_all_snapshots()
            filtered_count = len(filtered[0].frames())

        self.assertLess(filtered_count, baseline_count)

    def test_filtered_frames_not_marked_truncated(self) -> None:
        """Filtered frames do not set truncated flag on stacktrace or tintype."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize(frame_file_path_filters=["unittest"])
            tintype.take_snapshot()
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snapshots = reader.get_all_snapshots()
            self.assertEqual(len(snapshots), 1)
            self.assertFalse(snapshots[0].truncated)
            for st in snapshots[0].stacktraces.values():
                self.assertFalse(st.truncated)

    def test_multiple_filters(self) -> None:
        """Multiple filter strings each exclude matching frames."""
        # Get baseline count
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()
            tintype.take_snapshot()
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            baseline = reader.get_all_snapshots()
            baseline_count = len(baseline[0].frames())

        # Filter with a single filter
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize(frame_file_path_filters=["unittest"])
            tintype.take_snapshot()
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            single_filter = reader.get_all_snapshots()
            single_filter_count = len(single_filter[0].frames())

        # Filter with two filters — should exclude at least as many
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize(
                frame_file_path_filters=["unittest", "test_frame_filtering"]
            )
            tintype.take_snapshot()
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            multi_filter = reader.get_all_snapshots()
            multi_filter_count = len(multi_filter[0].frames())

        self.assertLess(single_filter_count, baseline_count)
        self.assertLess(multi_filter_count, single_filter_count)

    def test_filter_no_match(self) -> None:
        """A filter that matches nothing preserves all frames."""
        # Get baseline count
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()
            tintype.take_snapshot()
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            baseline = reader.get_all_snapshots()
            baseline_count = len(baseline[0].frames())

        # Filter with a non-matching string
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize(
                frame_file_path_filters=["this_will_never_match_any_path_xyz"]
            )
            tintype.take_snapshot()
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            filtered = reader.get_all_snapshots()
            filtered_count = len(filtered[0].frames())

        self.assertEqual(filtered_count, baseline_count)

    def test_filter_with_exception_traceback(self) -> None:
        """Filters work on exception traceback frames without marking truncated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize(frame_file_path_filters=["unittest"])
            try:
                raise ValueError("test exception")
            except ValueError as e:
                tintype.take_snapshot(e)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snapshots = reader.get_all_snapshots()
            self.assertEqual(len(snapshots), 1)
            self.assertFalse(snapshots[0].truncated)
            for st in snapshots[0].stacktraces.values():
                self.assertFalse(st.truncated)
                # Verify no frame has "unittest" in its path
                for frame in st.frames:
                    self.assertNotIn("unittest", frame.file_path)

    def test_empty_filters_list(self) -> None:
        """An empty filters list behaves the same as no filters."""
        # Get baseline count
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()
            tintype.take_snapshot()
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            baseline = reader.get_all_snapshots()
            baseline_count = len(baseline[0].frames())

        # Pass empty list
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize(frame_file_path_filters=[])
            tintype.take_snapshot()
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            filtered = reader.get_all_snapshots()
            filtered_count = len(filtered[0].frames())

        self.assertEqual(filtered_count, baseline_count)


if __name__ == "__main__":
    unittest.main()
