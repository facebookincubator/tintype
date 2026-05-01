# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Tests for edge cases and NaN handling."""

import math
import os
import tempfile
import unittest

import tintype


class SnapshotEdgeCasesTest(unittest.TestCase):
    """Tests for edge cases and NaN handling."""

    def test_nan_float(self) -> None:
        """Test that NaN float values are serialized correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "nan.pytb")
            tintype.initialize()

            nan_val = float("nan")
            self._capture_value(nan_val)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "captured" in locals_dict:
                    value = locals_dict["captured"]
                    self.assertIsInstance(value, float)
                    self.assertTrue(math.isnan(value))
                    break
            else:
                self.fail("captured not found in any frame")

    def test_deeply_nested_structure(self) -> None:
        """Test deeply nested data structures."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "deep_nesting.pytb")
            tintype.initialize()

            # Create a deeply nested structure
            deep: dict[str, object] = {"value": "leaf"}
            for i in range(20):
                deep = {"level": i, "nested": deep}

            self._capture_value(deep)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "captured" in locals_dict:
                    value = locals_dict["captured"]
                    self.assertIsInstance(value, dict)
                    # Traverse the nested structure and count dicts
                    dict_count = 0
                    current = value
                    while isinstance(current, dict):
                        dict_count += 1
                        current = current.get("nested")
                    # Should have at least 21 dicts (20 levels + 1 leaf)
                    self.assertGreaterEqual(dict_count, 21)
                    break
            else:
                self.fail("captured not found in any frame")

    def test_extracted_files_dir(self) -> None:
        """Test that get_extracted_files_dir returns a valid path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "extracted.pytb")
            tintype.initialize()
            tintype.take_snapshot()
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)

            # get_extracted_files_dir should return a string (may be empty if
            # all files matched originals)
            extracted_dir = reader.get_extracted_files_dir()
            self.assertIsInstance(extracted_dir, str)

    def test_negative_integers(self) -> None:
        """Test negative integer serialization."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "negative_ints.pytb")
            tintype.initialize()

            neg_small = -1
            neg_medium = -12345
            neg_large = -9999999999
            self._capture_three_values(neg_small, neg_medium, neg_large)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                if frame.function_name == "_capture_three_values":
                    locals_dict = frame.get_locals()
                    self.assertEqual(locals_dict["a"], -1)
                    self.assertEqual(locals_dict["b"], -12345)
                    self.assertEqual(locals_dict["c"], -9999999999)
                    break
            else:
                self.fail("_capture_three_values frame not found")

    def test_large_list(self) -> None:
        """Test serialization of a large list."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "large_list.pytb")
            tintype.initialize()

            large_list = list(range(1000))
            self._capture_value(large_list)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "captured" in locals_dict:
                    value = locals_dict["captured"]
                    self.assertIsInstance(value, list)
                    self.assertEqual(len(value), 1000)
                    break
            else:
                self.fail("captured not found in any frame")

    def _capture_value(self, value: object) -> None:
        captured = value
        _ = captured
        tintype.take_snapshot()

    def _capture_three_values(self, v1: object, v2: object, v3: object) -> None:
        a, b, c = v1, v2, v3
        _ = (a, b, c)
        tintype.take_snapshot()


if __name__ == "__main__":
    unittest.main()
