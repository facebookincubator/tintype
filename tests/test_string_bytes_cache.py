# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Tests for cross-snapshot string/bytes caching (dedup via PyObject_Hash)."""

import os
import tempfile
import unittest

import tintype


class StringBytesCacheStatsTest(unittest.TestCase):
    """Tests that the string_bytes_cache_hit stat is tracked correctly."""

    def test_string_cache_hit_stat_after_multiple_snapshots(self) -> None:
        tintype.initialize(collect_stats=True)

        for _ in range(3):
            self._capture_strings()

        stats = tintype.get_stats()
        oq = stats["object_queue_breakdown"]
        self.assertIn("string_bytes_cache_hit", oq)
        self.assertGreater(oq["string_bytes_cache_hit"], 0)

        tintype.finalize()

    def test_bytes_cache_hit_stat_after_multiple_snapshots(self) -> None:
        tintype.initialize(collect_stats=True)

        for _ in range(3):
            self._capture_bytes()

        stats = tintype.get_stats()
        oq = stats["object_queue_breakdown"]
        self.assertGreater(oq["string_bytes_cache_hit"], 0)

        tintype.finalize()

    def test_string_bytes_cache_hit_in_file_reader_stats(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "cache_stats.pytb")
            tintype.initialize(collect_stats=True)

            for _ in range(3):
                self._capture_strings()

            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            stats = reader.get_stats()
            self.assertIn("stringBytesCacheHit", stats)
            self.assertGreater(stats["stringBytesCacheHit"], 0)

    def test_no_cache_hits_with_single_snapshot(self) -> None:
        tintype.initialize(collect_stats=True)

        self._capture_strings()

        stats = tintype.get_stats()
        oq = stats["object_queue_breakdown"]
        self.assertEqual(oq["string_bytes_cache_hit"], 0)

        tintype.finalize()

    def _capture_strings(self) -> None:
        s1 = "A" * 1000
        s2 = "B" * 1000
        _ = (s1, s2)
        tintype.take_snapshot()

    def _capture_bytes(self) -> None:
        b1 = b"X" * 1000
        b2 = b"Y" * 1000
        _ = (b1, b2)
        tintype.take_snapshot()


class StringBytesCacheCorrectnessTest(unittest.TestCase):
    """Tests that cached strings/bytes are correctly deserialized."""

    def test_cached_strings_deserialized_correctly(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "cached_strings.pytb")
            tintype.initialize()

            self._capture_string_value("hello cached world")
            self._capture_string_value("hello cached world")

            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snapshots = reader.get_all_snapshots()
            self.assertEqual(len(snapshots), 2)

            for snap in snapshots:
                found = False
                for frame in snap.frames():
                    locals_dict = frame.get_locals()
                    if "captured" in locals_dict:
                        found = True
                        self.assertEqual(locals_dict["captured"], "hello cached world")
                        break
                self.assertTrue(found, "'captured' not found in any frame")

    def test_cached_bytes_deserialized_correctly(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "cached_bytes.pytb")
            tintype.initialize()

            self._capture_bytes_value(b"\x00\x01\x02\xff")
            self._capture_bytes_value(b"\x00\x01\x02\xff")

            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snapshots = reader.get_all_snapshots()
            self.assertEqual(len(snapshots), 2)

            for snap in snapshots:
                found = False
                for frame in snap.frames():
                    locals_dict = frame.get_locals()
                    if "captured" in locals_dict:
                        found = True
                        self.assertEqual(locals_dict["captured"], b"\x00\x01\x02\xff")
                        break
                self.assertTrue(found, "'captured' not found in any frame")

    def test_distinct_strings_not_conflated(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "distinct_strings.pytb")
            tintype.initialize()

            self._capture_string_value("first_value")
            self._capture_string_value("second_value")

            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snapshots = reader.get_all_snapshots()
            self.assertEqual(len(snapshots), 2)

            expected = ["first_value", "second_value"]
            for i, snap in enumerate(snapshots):
                found = False
                for frame in snap.frames():
                    locals_dict = frame.get_locals()
                    if "captured" in locals_dict:
                        found = True
                        self.assertEqual(locals_dict["captured"], expected[i])
                        break
                self.assertTrue(found, "'captured' not found in any frame")

    def test_large_string_cached_across_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "large_cached.pytb")
            tintype.initialize(collect_stats=True)

            large_str = "Z" * 50_000
            self._capture_string_value(large_str)
            self._capture_string_value(large_str)

            stats = tintype.get_stats()
            tintype.finalize(path)

            oq = stats["object_queue_breakdown"]
            self.assertGreater(oq["string_bytes_cache_hit"], 0)

            reader = tintype.SnapshotReader(path)
            snapshots = reader.get_all_snapshots()
            for snap in snapshots:
                found = False
                for frame in snap.frames():
                    locals_dict = frame.get_locals()
                    if "captured" in locals_dict:
                        found = True
                        self.assertEqual(locals_dict["captured"], large_str)
                        break
                self.assertTrue(found, "'captured' not found in any frame")

    def _capture_string_value(self, value: str) -> None:
        captured = value
        _ = captured
        tintype.take_snapshot()

    def _capture_bytes_value(self, value: bytes) -> None:
        captured = value
        _ = captured
        tintype.take_snapshot()


if __name__ == "__main__":
    unittest.main()
