# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Tests for compression gap skip optimization.

The snapshot file layout has a zero-filled gap between the snapshot records
section and the object heap. The writer should skip this gap when producing
the output file (both compressed and uncompressed), resulting in a compact
file that the reader can still parse correctly.
"""

import json
import os
import tempfile
import unittest

import tintype


class CompressionGapSkipTest(unittest.TestCase):
    """Tests verifying the gap-skip optimization produces valid output."""

    def _write_snapshot_file(
        self,
        path: str,
        compression_level: int,
    ) -> None:
        tintype.initialize()
        self._helper_with_locals()
        tintype.take_snapshot()
        self._helper_with_locals()
        tintype.finalize(path, {"test": "gap_skip"}, compression_level)

    def _helper_with_locals(self) -> None:
        x = 42  # noqa: F841
        y = "hello world"  # noqa: F841
        z = [1, 2, 3, 4, 5]  # noqa: F841
        d = {"key": "value", "num": 99}  # noqa: F841
        _ = (x, y, z, d)
        tintype.take_snapshot()

    def test_compressed_roundtrip(self) -> None:
        """Compressed output with gap skip is readable and correct."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "compressed.pytb")
            self._write_snapshot_file(path, compression_level=3)

            self.assertTrue(os.path.exists(path))
            file_size = os.path.getsize(path)
            self.assertGreater(file_size, 0)

            reader = tintype.SnapshotReader(path)
            snapshots = reader.get_all_snapshots()
            self.assertEqual(len(snapshots), 3)

            metadata = json.loads(reader.get_metadata())
            self.assertEqual(metadata, {"test": "gap_skip"})

            self._verify_snapshots(reader, snapshots)

    def test_uncompressed_roundtrip(self) -> None:
        """Uncompressed output with gap skip is readable and correct."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "uncompressed.pytb")
            self._write_snapshot_file(path, compression_level=0)

            self.assertTrue(os.path.exists(path))
            file_size = os.path.getsize(path)
            self.assertGreater(file_size, 0)

            reader = tintype.SnapshotReader(path)
            snapshots = reader.get_all_snapshots()
            self.assertEqual(len(snapshots), 3)

            metadata = json.loads(reader.get_metadata())
            self.assertEqual(metadata, {"test": "gap_skip"})

            self._verify_snapshots(reader, snapshots)

    def test_uncompressed_file_smaller_than_working_file(self) -> None:
        """Uncompressed output should be smaller than the mmap'd working file.

        The working file contains the zero-filled gap between snapshot records
        and the object heap. The output should skip this gap, so the output
        file must be smaller.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "compact.pytb")

            reader = tintype.initialize()
            working_path = reader.get_working_file_path()
            self.assertIsNotNone(working_path)
            assert working_path is not None
            self._helper_with_locals()
            working_size = os.path.getsize(working_path)

            tintype.finalize(path, compression_level=0)

            output_size = os.path.getsize(path)
            self.assertGreater(working_size, 0)
            self.assertGreater(output_size, 0)
            self.assertLess(
                output_size,
                working_size,
                f"Output ({output_size}) should be smaller than working file "
                f"({working_size}) because the gap is skipped",
            )

    def test_compressed_and_uncompressed_same_structure(self) -> None:
        """Both compression modes should produce equivalent snapshot structure."""
        with tempfile.TemporaryDirectory() as tmpdir:
            compressed_path = os.path.join(tmpdir, "compressed.pytb")
            uncompressed_path = os.path.join(tmpdir, "uncompressed.pytb")

            tintype.initialize()
            tintype.take_snapshot()
            tintype.finalize(compressed_path, {"mode": "compressed"}, 3)

            tintype.initialize()
            tintype.take_snapshot()
            tintype.finalize(uncompressed_path, {"mode": "uncompressed"}, 0)

            r_comp = tintype.SnapshotReader(compressed_path)
            r_uncomp = tintype.SnapshotReader(uncompressed_path)

            self.assertEqual(r_comp.snapshot_count(), r_uncomp.snapshot_count())
            self.assertEqual(r_comp.snapshot_count(), 1)

            snap_c = r_comp.get_latest_snapshot()
            snap_u = r_uncomp.get_latest_snapshot()
            self.assertIsNotNone(snap_c)
            self.assertIsNotNone(snap_u)

            self.assertEqual(len(snap_c.stacktraces), len(snap_u.stacktraces))
            self.assertEqual(len(snap_c.frames()), len(snap_u.frames()))

    def test_object_data_integrity_after_gap_skip(self) -> None:
        """Object heap data should be intact after gap skip."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "integrity.pytb")
            tintype.initialize()

            self._capture_known_values()

            tintype.finalize(path, compression_level=3)

            reader = tintype.SnapshotReader(path)
            snapshots = reader.get_all_snapshots()
            self.assertEqual(len(snapshots), 1)

            target_frame = None
            for frame in snapshots[0].frames():
                if frame.function_name == "_capture_known_values":
                    target_frame = frame
                    break

            self.assertIsNotNone(target_frame)
            assert target_frame is not None

            locals_dict = target_frame.get_locals()
            self.assertEqual(locals_dict["known_int"], 12345)
            self.assertEqual(locals_dict["known_str"], "test_integrity_string")
            self.assertEqual(locals_dict["known_list"], [10, 20, 30])

    def _capture_known_values(self) -> None:
        known_int = 12345  # noqa: F841
        known_str = "test_integrity_string"  # noqa: F841
        known_list = [10, 20, 30]  # noqa: F841
        _ = (known_int, known_str, known_list)
        tintype.take_snapshot()

    def test_multiple_snapshots_with_gap_skip(self) -> None:
        """Multiple snapshots across the gap boundary remain readable."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "multi.pytb")
            tintype.initialize()

            for i in range(5):
                val = f"snapshot_{i}"
                _ = val
                tintype.take_snapshot()

            tintype.finalize(path, compression_level=3)

            reader = tintype.SnapshotReader(path)
            snapshots = reader.get_all_snapshots()
            self.assertEqual(len(snapshots), 5)

            for i in range(1, len(snapshots)):
                self.assertGreaterEqual(
                    snapshots[i].timestamp, snapshots[i - 1].timestamp
                )

    def test_source_files_accessible_after_gap_skip(self) -> None:
        """Source files (in the file table) remain accessible after gap skip."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "source_files.pytb")
            tintype.initialize()
            tintype.take_snapshot()
            tintype.finalize(path, compression_level=3)

            reader = tintype.SnapshotReader(path)
            source_files = reader.get_all_source_files()
            self.assertGreater(len(source_files), 0)

            test_file_found = any(
                "test_compression_gap_skip.py" in sf.path for sf in source_files
            )
            self.assertTrue(test_file_found, "This test file should be in source files")

    def _verify_snapshots(
        self,
        reader: tintype.SnapshotReader,
        snapshots: list[tintype.Snapshot],
    ) -> None:
        for snap in snapshots:
            self.assertGreater(snap.timestamp, 0)
            self.assertGreater(len(snap.frames()), 0)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                self.assertIsInstance(locals_dict, dict)


if __name__ == "__main__":
    unittest.main()
