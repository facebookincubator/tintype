# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Tests for the SnapshotReader and source file capture."""

import os
import tempfile
import unittest

import tintype


class SnapshotReaderTest(unittest.TestCase):
    """Tests for the SnapshotReader."""

    def test_open_nonexistent_file(self) -> None:
        """Test that opening a nonexistent file raises RuntimeError."""
        with self.assertRaises(RuntimeError) as ctx:
            tintype.SnapshotReader("/nonexistent/path/to/file.pytb")
        self.assertIn("Failed to open", str(ctx.exception))

    def test_get_latest_snapshot(self) -> None:
        """Test getting the most recent tintype."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()
            tintype.take_snapshot()
            tintype.take_snapshot()
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            latest = reader.get_latest_snapshot()
            self.assertIsNotNone(latest)

            all_snapshots = reader.get_all_snapshots()
            self.assertEqual(latest.timestamp, all_snapshots[-1].timestamp)

    def test_get_snapshot_at_index(self) -> None:
        """Test getting snapshots by chronological index."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "snapshot_at_index.pytb")
            tintype.initialize()
            tintype.take_snapshot()
            tintype.take_snapshot()
            tintype.take_snapshot()
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            self.assertEqual(reader.snapshot_count(), 3)

            all_snapshots = reader.get_all_snapshots()
            self.assertEqual(len(all_snapshots), 3)

            # Each index should return the matching snapshot
            for i in range(3):
                snap = reader.get_snapshot_at_index(i)
                self.assertIsNotNone(snap)
                self.assertEqual(snap.timestamp, all_snapshots[i].timestamp)

            # Out-of-range indices should return None
            self.assertIsNone(reader.get_snapshot_at_index(3))
            self.assertIsNone(reader.get_snapshot_at_index(100))

    def test_get_snapshot_at_index_single(self) -> None:
        """Test get_snapshot_at_index with a single tintype."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "single.pytb")
            tintype.initialize()
            tintype.take_snapshot()
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            self.assertEqual(reader.snapshot_count(), 1)

            snap = reader.get_snapshot_at_index(0)
            self.assertIsNotNone(snap)

            self.assertIsNone(reader.get_snapshot_at_index(1))

    def test_snapshot_count_zero(self) -> None:
        """Test snapshot_count is zero when no snapshots are taken."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "empty.pytb")
            tintype.initialize()
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            self.assertEqual(reader.snapshot_count(), 0)
            self.assertIsNone(reader.get_snapshot_at_index(0))

    def test_snapshot_count_borrowed_reader(self) -> None:
        """Test that borrowed reader sees snapshot_count update in real-time."""
        reader = tintype.initialize()
        self.assertEqual(reader.snapshot_count(), 0)

        tintype.take_snapshot()
        self.assertEqual(reader.snapshot_count(), 1)

        tintype.take_snapshot()
        self.assertEqual(reader.snapshot_count(), 2)

        tintype.finalize()

    def test_magic_offsets(self) -> None:
        """Test magic offset detection."""
        # These are the magic offset values (high values at the end of uint64 range)
        uint64_max = (1 << 64) - 1
        none_offset = uint64_max - 2
        true_offset = uint64_max - 1
        false_offset = uint64_max

        self.assertTrue(tintype.SnapshotReader.is_magic_offset(none_offset))
        self.assertTrue(tintype.SnapshotReader.is_magic_offset(true_offset))
        self.assertTrue(tintype.SnapshotReader.is_magic_offset(false_offset))
        self.assertFalse(tintype.SnapshotReader.is_magic_offset(uint64_max - 3))
        self.assertFalse(tintype.SnapshotReader.is_magic_offset(12345))

        self.assertEqual(
            tintype.SnapshotReader.get_magic_offset_type(none_offset), "None"
        )
        self.assertEqual(
            tintype.SnapshotReader.get_magic_offset_type(true_offset), "True"
        )
        self.assertEqual(
            tintype.SnapshotReader.get_magic_offset_type(false_offset), "False"
        )

    def test_get_working_file_path_from_file(self) -> None:
        """Test that a file-based reader returns a valid working file path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()
            tintype.take_snapshot()
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            working_path = reader.get_working_file_path()
            self.assertIsNotNone(working_path)
            self.assertIsInstance(working_path, str)
            self.assertTrue(os.path.exists(working_path))


class SnapshotSourceFileTest(unittest.TestCase):
    """Tests for source file capture."""

    def test_source_files_captured(self) -> None:
        """Test that source files are captured in the tintype."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()
            tintype.take_snapshot()
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            source_files = reader.get_all_source_files()

            # Should have at least this test file
            self.assertGreater(len(source_files), 0)

            # Check that paths are not empty
            for sf in source_files:
                self.assertIsInstance(sf.path, str)
                self.assertGreater(len(sf.path), 0)

    def test_synthetic_string_filename_not_staged(self) -> None:
        """CPython synthetic filenames like ``<string>`` (from
        ``compile(code, "<string>", "exec")``) must not be staged into
        the extracted-files directory. Previously the path-construction
        used a raw ``extractedFilesDir_ + file.path`` concat with no
        separator, producing garbage paths like
        ``/tmp/snapshot_files_Xxxxxx<string>`` that then became the
        frame's ``file_path`` on read. The fix skips synthetic
        ``<...>`` entries so the frame keeps its honest ``<string>``
        filename and downstream filters can match an anchored
        pattern.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "string_frame.pytb")
            tintype.initialize()
            # ``compile(..., "<string>", "exec")`` + ``exec`` produces a
            # frame whose ``co_filename`` is ``<string>``. Taking the
            # snapshot from inside that exec captures that frame.
            code = compile("tintype.take_snapshot()", "<string>", "exec")
            exec(code, {"tintype": tintype})
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)

            # 1. No ``<`` paths should be staged. ``os.walk`` safely
            #    yields nothing for empty / nonexistent dirs, so the
            #    assertion runs whether or not the reader created an
            #    extracted-files dir for this snapshot.
            extracted_dir = reader.get_extracted_files_dir() or ""
            for dirpath, _dirs, files in os.walk(extracted_dir):
                for name in files:
                    leaked_path = os.path.join(dirpath, name)
                    self.assertNotIn(
                        "<",
                        name,
                        f"Synthetic filename leaked into extracted dir: {leaked_path}",
                    )
                    self.assertNotIn(
                        ">",
                        name,
                        f"Synthetic filename leaked into extracted dir: {leaked_path}",
                    )

            # 2. Any frame whose original ``co_filename`` was
            #    ``<string>`` must surface with exactly ``<string>`` as
            #    its ``file_path`` (unchanged, no tmp-dir prefix).
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)
            found_string_frame = False
            for st in snap.stacktraces.values():
                for frame in st.frames:
                    if frame.file_path == "<string>":
                        found_string_frame = True
                    # Either the path is exactly ``<string>`` or it
                    # does not contain the ``<string>`` substring at
                    # all (i.e., no munged concatenation).
                    if "<string>" in frame.file_path:
                        self.assertEqual(
                            frame.file_path,
                            "<string>",
                            "Frame file_path contains '<string>' as a "
                            "substring of a munged path: "
                            f"{frame.file_path!r}",
                        )
            self.assertTrue(
                found_string_frame,
                "Test setup did not produce a frame with co_filename "
                "'<string>'; the test cannot verify the staging fix.",
            )


if __name__ == "__main__":
    unittest.main()
