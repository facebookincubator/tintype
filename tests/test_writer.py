# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Tests for the snapshot writer (Snapshotter)."""

import json
import os
import tempfile
import unittest

import tintype


class SnapshotWriterTest(unittest.TestCase):
    """Tests for the snapshot writer (Snapshotter)."""

    def test_initialize_creates_file(self) -> None:
        """Test that initialize creates a snapshot file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()
            tintype.finalize(path, {"test": "metadata"})
            self.assertTrue(os.path.exists(path))

    def test_initialize_returns_reader(self) -> None:
        """Test that initialize returns a SnapshotReader."""
        reader = tintype.initialize()
        self.assertIsInstance(reader, tintype.SnapshotReader)
        tintype.finalize()

    def test_initialize_reader_reads_snapshots(self) -> None:
        """Test that the reader returned by initialize can read snapshots."""
        reader = tintype.initialize()

        # No snapshots yet
        snapshots = reader.get_all_snapshots()
        self.assertEqual(len(snapshots), 0)

        # Take a snapshot
        tintype.take_snapshot()

        # Reader should see the new snapshot
        snapshots = reader.get_all_snapshots()
        self.assertEqual(len(snapshots), 1)

        # Take another snapshot
        tintype.take_snapshot()

        # Reader should see both snapshots
        snapshots = reader.get_all_snapshots()
        self.assertEqual(len(snapshots), 2)

        tintype.finalize()

    def test_initialize_reader_has_working_file_path(self) -> None:
        """Test that the borrowed reader from initialize has a working file path."""
        reader = tintype.initialize()
        working_path = reader.get_working_file_path()
        self.assertIsNotNone(working_path)
        self.assertIsInstance(working_path, str)
        self.assertTrue(os.path.exists(working_path))
        tintype.finalize()

    def test_initialize_with_metadata(self) -> None:
        """Test that metadata is stored and retrievable."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            metadata = {"key1": "value1", "key2": 42, "nested": {"a": 1}}
            tintype.initialize()
            tintype.finalize(path, metadata)

            reader = tintype.SnapshotReader(path)
            stored_metadata = json.loads(reader.get_metadata())
            self.assertEqual(stored_metadata, metadata)

    def test_take_snapshot_records_frames(self) -> None:
        """Test that take_snapshot captures the call stack."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()
            self._helper_function_for_snapshot()
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snapshots = reader.get_all_snapshots()
            self.assertEqual(len(snapshots), 1)

            # Check that we captured some frames
            self.assertGreater(len(snapshots[0].frames()), 0)

            # Check that this test function is in the frame list
            function_names = [f.function_name for f in snapshots[0].frames()]
            self.assertIn("_helper_function_for_snapshot", function_names)

    def test_function_qualname_captured(self) -> None:
        """Test that function_qualname is captured for each frame."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "qualname_test.pytb")
            tintype.initialize()
            self._helper_function_for_snapshot()
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snapshots = reader.get_all_snapshots()
            self.assertEqual(len(snapshots), 1)

            frames = snapshots[0].frames()
            self.assertGreater(len(frames), 0)

            for frame in frames:
                self.assertIsInstance(frame.function_qualname, str)
                self.assertGreater(len(frame.function_qualname), 0)

            # Verify a method's qualname includes the class name
            helper_frames = [
                f for f in frames if f.function_name == "_helper_function_for_snapshot"
            ]
            self.assertEqual(len(helper_frames), 1)
            self.assertEqual(
                helper_frames[0].function_qualname,
                "SnapshotWriterTest._helper_function_for_snapshot",
            )

    def _helper_function_for_snapshot(self) -> None:
        """Helper function to create a deeper call stack."""
        local_var = 42
        another_var = "hello"
        _ = local_var + len(another_var)  # Use vars to avoid unused warnings
        tintype.take_snapshot()

    def test_multiple_snapshots(self) -> None:
        """Test that multiple snapshots can be taken."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()
            tintype.take_snapshot()
            tintype.take_snapshot()
            tintype.take_snapshot()
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snapshots = reader.get_all_snapshots()
            self.assertEqual(len(snapshots), 3)

            # Check timestamps are in order
            for i in range(1, len(snapshots)):
                self.assertGreaterEqual(
                    snapshots[i].timestamp, snapshots[i - 1].timestamp
                )

    def test_take_snapshot_returns_snapshot(self) -> None:
        """Test that take_snapshot returns a Snapshot object."""
        tintype.initialize()
        result = tintype.take_snapshot()
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIsInstance(result, tintype.Snapshot)
        self.assertGreater(result.timestamp, 0)
        self.assertIsInstance(result.stacktraces, dict)
        self.assertGreater(len(result.stacktraces), 0)
        self.assertGreater(len(result.frames()), 0)
        tintype.finalize()

    def test_take_snapshot_return_matches_reader(self) -> None:
        """Test that the returned Snapshot matches what the reader sees."""
        reader = tintype.initialize()
        returned_snap = tintype.take_snapshot()
        latest_snap = reader.get_latest_snapshot()

        self.assertIsNotNone(returned_snap)
        self.assertIsNotNone(latest_snap)
        assert returned_snap is not None
        assert latest_snap is not None
        self.assertEqual(returned_snap.timestamp, latest_snap.timestamp)
        self.assertEqual(len(returned_snap.stacktraces), len(latest_snap.stacktraces))
        tintype.finalize()

    def test_take_snapshot_return_has_wired_frames(self) -> None:
        """Test that the returned Snapshot has properly wired back-references."""
        tintype.initialize()
        result = self._helper_and_return_snapshot()

        # Verify stacktrace -> snapshot wiring
        for st in result.stacktraces.values():
            self.assertIs(st._snapshot, result)
            # Verify frame -> stacktrace wiring
            for frame in st.frames:
                self.assertIs(frame._stacktrace, st)

        # Verify get_locals works on the returned snapshot
        for st in result.stacktraces.values():
            for frame in st.frames:
                if frame.function_name == "_helper_and_return_snapshot":
                    locals_dict = frame.get_locals()
                    self.assertIn("local_var", locals_dict)
                    self.assertEqual(locals_dict["local_var"], 99)
                    break
        tintype.finalize()

    def _helper_and_return_snapshot(self) -> tintype.Snapshot:
        """Helper that takes a snapshot with a known local and returns it."""
        local_var = 99
        _ = local_var
        result = tintype.take_snapshot()
        assert result is not None
        return result


if __name__ == "__main__":
    unittest.main()
