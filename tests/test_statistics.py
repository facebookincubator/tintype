# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Tests for statistics collection and reporting."""

import os
import tempfile
import unittest

import tintype


class SnapshotStatisticsTest(unittest.TestCase):
    """Tests for statistics collection."""

    def test_get_stats_returns_dict(self) -> None:
        """Test that get_stats returns a dictionary with expected keys."""
        tintype.initialize(collect_stats=True)

        # Take a few snapshots with various objects
        x = 42
        y = "hello"
        z = [1, 2, 3]
        _ = (x, y, z)
        tintype.take_snapshot()
        tintype.take_snapshot()

        stats = tintype.get_stats()

        # Verify expected top-level keys
        self.assertIn("initialize_time_ms", stats)
        self.assertIn("total_snapshot_time_ms", stats)
        self.assertIn("snapshot_count", stats)
        self.assertIn("total_objects", stats)
        self.assertIn("snapshot_breakdown", stats)
        self.assertIn("object_queue_breakdown", stats)
        self.assertIn("object_stats", stats)

        # Verify snapshot count
        self.assertEqual(stats["snapshot_count"], 2)

        # Verify object_stats has some entries
        self.assertGreater(len(stats["object_stats"]), 0)

        tintype.finalize()

    def test_reset_stats(self) -> None:
        """Test that reset_stats clears the statistics."""
        tintype.initialize(collect_stats=True)

        x = 42
        _ = x
        tintype.take_snapshot()

        # Get stats before reset
        stats_before = tintype.get_stats()
        self.assertEqual(stats_before["snapshot_count"], 1)

        # Reset stats
        tintype.reset_stats()

        # Take another snapshot
        tintype.take_snapshot()

        # Get stats after reset
        stats_after = tintype.get_stats()
        # Snapshot count should be 1 (only the snapshot after reset)
        self.assertEqual(stats_after["snapshot_count"], 1)

        tintype.finalize()

    def test_stats_without_collection(self) -> None:
        """Test that stats are empty when collect_stats=False."""
        tintype.initialize()  # collect_stats defaults to False

        x = 42
        _ = x
        tintype.take_snapshot()

        stats = tintype.get_stats()

        # When stats collection is disabled, counts should be zero
        self.assertIn("snapshot_count", stats)
        self.assertEqual(stats["snapshot_count"], 0)

        tintype.finalize()

    def test_borrowed_reader_get_stats_returns_live_stats(self) -> None:
        """Test that borrowed reader's get_stats() returns live stats from singleton."""
        reader = tintype.initialize(collect_stats=True)

        # Take a snapshot with some objects
        x = 42
        y = "hello"
        z = [1, 2, 3]
        _ = (x, y, z)
        tintype.take_snapshot()

        # Get stats from the borrowed reader
        stats = reader.get_stats()

        # Verify stats dictionary is not empty
        self.assertIsInstance(stats, dict)
        self.assertGreater(len(stats), 0)

        # Verify expected keys are present (raw stat names from SnapshotStats.flatten())
        self.assertIn("snapshotCount", stats)
        self.assertIn("totalSnapshotTimeNs", stats)
        self.assertIn("writeFrameRecordTimeNs", stats)

        # Verify snapshot count matches what we took
        self.assertEqual(stats["snapshotCount"], 1)

        # Take another snapshot and verify stats update
        tintype.take_snapshot()
        stats_after = reader.get_stats()
        self.assertEqual(stats_after["snapshotCount"], 2)

        tintype.finalize()

    def test_borrowed_reader_get_stats_updates_in_realtime(self) -> None:
        """Test that borrowed reader sees stats updates after each tintype."""
        reader = tintype.initialize(collect_stats=True)

        # Initial stats should have zero snapshots
        initial_stats = reader.get_stats()
        self.assertEqual(initial_stats.get("snapshotCount", 0), 0)

        # Take first snapshot
        x = 42
        _ = x
        tintype.take_snapshot()

        # Stats should now show 1 snapshot
        stats_1 = reader.get_stats()
        self.assertEqual(stats_1["snapshotCount"], 1)
        self.assertGreater(stats_1["totalSnapshotTimeNs"], 0)

        # Take second snapshot
        tintype.take_snapshot()

        # Stats should now show 2 snapshots
        stats_2 = reader.get_stats()
        self.assertEqual(stats_2["snapshotCount"], 2)

        # Total snapshot time should have increased
        self.assertGreaterEqual(
            stats_2["totalSnapshotTimeNs"], stats_1["totalSnapshotTimeNs"]
        )

        tintype.finalize()

    def test_file_reader_get_stats_reads_from_file(self) -> None:
        """Test that file-based reader's get_stats() reads stats from the file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "stats.pytb")

            # Initialize with stats collection enabled
            tintype.initialize(collect_stats=True)

            # Take snapshots with various objects
            x = 42
            y = "hello"
            z = [1, 2, 3]
            _ = (x, y, z)
            tintype.take_snapshot()
            tintype.take_snapshot()
            tintype.take_snapshot()

            # Finalize to write stats to file
            tintype.finalize(path)

            # Open with file-based reader
            reader = tintype.SnapshotReader(path)
            stats = reader.get_stats()

            # Verify stats dictionary is not empty
            self.assertIsInstance(stats, dict)
            self.assertGreater(len(stats), 0)

            # Verify expected keys are present
            self.assertIn("snapshotCount", stats)
            self.assertIn("totalSnapshotTimeNs", stats)

            # Verify snapshot count matches what we took
            self.assertEqual(stats["snapshotCount"], 3)

            # Verify timing stats are populated
            self.assertGreater(stats["totalSnapshotTimeNs"], 0)

    def test_file_reader_get_stats_empty_when_stats_disabled(self) -> None:
        """Test that file-based reader returns empty stats when collection was disabled."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "no_stats.pytb")

            # Initialize WITHOUT stats collection
            tintype.initialize(collect_stats=False)

            x = 42
            _ = x
            tintype.take_snapshot()

            # Finalize
            tintype.finalize(path)

            # Open with file-based reader
            reader = tintype.SnapshotReader(path)
            stats = reader.get_stats()

            # Stats should be empty since collection was disabled
            self.assertIsInstance(stats, dict)
            self.assertEqual(len(stats), 0)

    def test_file_reader_get_stats_contains_object_type_stats(self) -> None:
        """Test that file-based reader stats include per-object-type statistics."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "object_stats.pytb")

            tintype.initialize(collect_stats=True)

            # Create various object types
            int_val = 42
            str_val = "hello world"
            list_val = [1, 2, 3, 4, 5]
            dict_val = {"a": 1, "b": 2}
            _ = (int_val, str_val, list_val, dict_val)
            tintype.take_snapshot()

            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            stats = reader.get_stats()

            # Check for per-type stats (e.g., Int64Count, StringCount, etc.)
            # The exact keys depend on what objects were serialized
            type_stat_keys = [k for k in stats.keys() if "Count" in k or "TimeNs" in k]
            self.assertGreater(len(type_stat_keys), 0)

    def test_borrowed_reader_stats_match_module_get_stats(self) -> None:
        """Test that borrowed reader stats are consistent with module-level get_stats()."""
        reader = tintype.initialize(collect_stats=True)

        x = [1, 2, 3]
        y = {"key": "value"}
        _ = (x, y)
        tintype.take_snapshot()

        # Get stats from both sources
        reader_stats = reader.get_stats()
        module_stats = tintype.get_stats()

        # Both should report the same snapshot count
        # Note: module get_stats() returns a processed dict, reader.get_stats() returns raw
        self.assertEqual(reader_stats["snapshotCount"], module_stats["snapshot_count"])

        tintype.finalize()


if __name__ == "__main__":
    unittest.main()
