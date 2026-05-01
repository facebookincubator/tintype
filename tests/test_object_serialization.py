# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Tests for object heap serialization of core types."""

import os
import tempfile
import unittest

import tintype


class SnapshotObjectHeapTest(unittest.TestCase):
    """Tests for object heap serialization and reading."""

    def test_string_serialization(self) -> None:
        """Test that string objects are properly serialized."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()
            test_string = "hello world"
            self._capture_local(test_string)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "local_value" in locals_dict:
                    self.assertEqual(locals_dict["local_value"], test_string)
                    break
            else:
                self.fail("local_value not found in any frame")

    def _capture_local(self, value: object) -> None:
        """Helper to capture a local variable in a tintype."""
        local_value = value
        _ = local_value  # Use to avoid unused warning
        tintype.take_snapshot()

    def test_integer_serialization(self) -> None:
        """Test that integer objects are properly serialized."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()
            test_int = 42
            self._capture_local(test_int)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "local_value" in locals_dict:
                    self.assertEqual(locals_dict["local_value"], test_int)
                    break
            else:
                self.fail("local_value not found in any frame")

    def test_float_serialization(self) -> None:
        """Test that float objects are properly serialized."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()
            test_float = 3.14159
            self._capture_local(test_float)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "local_value" in locals_dict:
                    self.assertAlmostEqual(
                        locals_dict["local_value"], test_float, places=4
                    )
                    break
            else:
                self.fail("local_value not found in any frame")

    def test_list_serialization(self) -> None:
        """Test that list objects are properly serialized."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()
            test_list = [1, 2, 3]
            self._capture_local(test_list)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "local_value" in locals_dict:
                    value = locals_dict["local_value"]
                    self.assertIsInstance(value, list)
                    self.assertEqual(len(value), 3)
                    break
            else:
                self.fail("local_value not found in any frame")

    def test_dict_serialization(self) -> None:
        """Test that dict objects are properly serialized."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()
            test_dict = {"a": 1, "b": 2}
            self._capture_local(test_dict)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "local_value" in locals_dict:
                    value = locals_dict["local_value"]
                    self.assertIsInstance(value, dict)
                    self.assertEqual(len(value), 2)
                    break
            else:
                self.fail("local_value not found in any frame")

    def test_none_serialization(self) -> None:
        """Test that None is represented as a magic offset."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()
            test_none = None
            self._capture_local(test_none)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snapshots = reader.get_all_snapshots()

            # Check that None magic offset is in the object map
            # Magic offsets are at the high end of uint64 range
            uint64_max = (1 << 64) - 1
            none_offset = uint64_max - 2
            found_none = none_offset in snapshots[0].object_map.values()
            self.assertTrue(found_none, "None magic offset not found in object map")

    def test_bool_serialization(self) -> None:
        """Test that booleans are represented as magic offsets."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()
            test_true = True
            test_false = False
            self._capture_two_locals(test_true, test_false)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snapshots = reader.get_all_snapshots()

            # Magic offsets are at the high end of uint64 range
            uint64_max = (1 << 64) - 1
            true_offset = uint64_max - 1
            false_offset = uint64_max

            found_true = true_offset in snapshots[0].object_map.values()
            found_false = false_offset in snapshots[0].object_map.values()

            self.assertTrue(found_true, "True magic offset not found in object map")
            self.assertTrue(found_false, "False magic offset not found in object map")

    def _capture_two_locals(self, val1: object, val2: object) -> None:
        """Helper to capture two local variables."""
        local1 = val1
        local2 = val2
        _ = (local1, local2)
        tintype.take_snapshot()


class SnapshotIntBignumTest(unittest.TestCase):
    """Tests for large integer (IntBignum) serialization."""

    def test_large_positive_integer(self) -> None:
        """Test that integers larger than 64-bit are correctly roundtripped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "bigint.pytb")
            tintype.initialize()

            # Integer larger than int64 max (9223372036854775807)
            large_int = 10**20  # 100000000000000000000
            self._capture_value(large_int)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "captured" in locals_dict:
                    self.assertEqual(locals_dict["captured"], large_int)
                    break
            else:
                self.fail("captured not found in any frame")

    def test_large_negative_integer(self) -> None:
        """Test that large negative integers are correctly roundtripped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "bigint_neg.pytb")
            tintype.initialize()

            large_neg_int = -(10**20)
            self._capture_value(large_neg_int)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "captured" in locals_dict:
                    self.assertEqual(locals_dict["captured"], large_neg_int)
                    break
            else:
                self.fail("captured not found in any frame")

    def test_int64_boundary_values(self) -> None:
        """Test integers at the boundary of int64."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "boundary.pytb")
            tintype.initialize()

            int64_max = 9223372036854775807
            int64_min = -9223372036854775808
            just_over_max = int64_max + 1
            just_under_min = int64_min - 1

            self._capture_four_values(
                int64_max, int64_min, just_over_max, just_under_min
            )
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                if frame.function_name == "_capture_four_values":
                    locals_dict = frame.get_locals()
                    self.assertEqual(locals_dict["a"], int64_max)
                    self.assertEqual(locals_dict["b"], int64_min)
                    self.assertEqual(locals_dict["c"], just_over_max)
                    self.assertEqual(locals_dict["d"], just_under_min)
                    break
            else:
                self.fail("_capture_four_values frame not found")

    def _capture_value(self, value: object) -> None:
        captured = value
        _ = captured
        tintype.take_snapshot()

    def _capture_four_values(
        self, v1: object, v2: object, v3: object, v4: object
    ) -> None:
        a, b, c, d = v1, v2, v3, v4
        _ = (a, b, c, d)
        tintype.take_snapshot()


class SnapshotBytesTest(unittest.TestCase):
    """Tests for bytes serialization."""

    def test_bytes_serialization(self) -> None:
        """Test that bytes objects are properly serialized."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "bytes.pytb")
            tintype.initialize()

            test_bytes = b"hello world"
            self._capture_value(test_bytes)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "captured" in locals_dict:
                    self.assertEqual(locals_dict["captured"], test_bytes)
                    break
            else:
                self.fail("captured not found in any frame")

    def test_empty_bytes(self) -> None:
        """Test that empty bytes are serialized correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "empty_bytes.pytb")
            tintype.initialize()

            empty_bytes = b""
            self._capture_value(empty_bytes)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "captured" in locals_dict:
                    self.assertEqual(locals_dict["captured"], b"")
                    break
            else:
                self.fail("captured not found in any frame")

    def test_binary_bytes(self) -> None:
        """Test that bytes with non-UTF8 data are serialized correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "binary_bytes.pytb")
            tintype.initialize()

            # Bytes with null bytes and high bytes
            binary_bytes = b"\x00\x01\x02\xff\xfe\xfd"
            self._capture_value(binary_bytes)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "captured" in locals_dict:
                    self.assertEqual(locals_dict["captured"], binary_bytes)
                    break
            else:
                self.fail("captured not found in any frame")

    def _capture_value(self, value: object) -> None:
        captured = value
        _ = captured
        tintype.take_snapshot()


class SnapshotTupleSetTest(unittest.TestCase):
    """Tests for tuple and set serialization."""

    def test_tuple_serialization(self) -> None:
        """Test that tuples are properly serialized."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "tuple.pytb")
            tintype.initialize()

            test_tuple = (1, "two", 3.0, None)
            self._capture_value(test_tuple)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "captured" in locals_dict:
                    value = locals_dict["captured"]
                    self.assertEqual(len(value), 4)
                    break
            else:
                self.fail("captured not found in any frame")

    def test_set_serialization(self) -> None:
        """Test that sets are properly serialized."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "set.pytb")
            tintype.initialize()

            test_set = {1, 2, 3, 4, 5}
            self._capture_value(test_set)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "captured" in locals_dict:
                    value = locals_dict["captured"]
                    self.assertEqual(len(value), 5)
                    break
            else:
                self.fail("captured not found in any frame")

    def test_frozenset_serialization(self) -> None:
        """Test that frozensets are properly serialized."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "frozenset.pytb")
            tintype.initialize()

            test_frozenset = frozenset({1, 2, 3})
            self._capture_value(test_frozenset)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "captured" in locals_dict:
                    value = locals_dict["captured"]
                    self.assertEqual(len(value), 3)
                    break
            else:
                self.fail("captured not found in any frame")

    def _capture_value(self, value: object) -> None:
        captured = value
        _ = captured
        tintype.take_snapshot()


if __name__ == "__main__":
    unittest.main()
