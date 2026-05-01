# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Tests for primitive and collection subclass serialization."""

import os
import tempfile
import unittest

import tintype
from tintype.tests.helpers import (
    DictSubclass,
    FloatSubclass,
    IntSubclass,
    ListSubclass,
    SetSubclass,
    StrSubclass,
    TupleSubclass,
)


class SnapshotPrimitiveSubclassTest(unittest.TestCase):
    """Tests for primitive subclass serialization."""

    def test_int_subclass_serialized_as_object(self) -> None:
        """Test that int subclass is serialized as SerializedObject, not Int64."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "int_subclass.pytb")
            tintype.initialize()

            int_sub = IntSubclass(42)
            self._capture_value(int_sub)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "captured" in locals_dict:
                    obj = locals_dict["captured"]
                    self.assertIsInstance(obj, tintype.SerializedObject)
                    # Verify repr shows the value
                    repr_str = repr(obj)
                    self.assertIn("42", repr_str)
                    break
            else:
                self.fail("captured not found in any frame")

    def test_str_subclass_serialized_as_object(self) -> None:
        """Test that str subclass is serialized as SerializedObject, not String."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "str_subclass.pytb")
            tintype.initialize()

            str_sub = StrSubclass("hello")
            self._capture_value(str_sub)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "captured" in locals_dict:
                    obj = locals_dict["captured"]
                    self.assertIsInstance(obj, tintype.SerializedObject)
                    break
            else:
                self.fail("captured not found in any frame")

    def test_float_subclass_serialized_as_object(self) -> None:
        """Test that float subclass is serialized as SerializedObject, not Float."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "float_subclass.pytb")
            tintype.initialize()

            float_sub = FloatSubclass(3.14)
            self._capture_value(float_sub)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "captured" in locals_dict:
                    obj = locals_dict["captured"]
                    self.assertIsInstance(obj, tintype.SerializedObject)
                    break
            else:
                self.fail("captured not found in any frame")

    def _capture_value(self, value: object) -> None:
        captured = value
        _ = captured
        tintype.take_snapshot()


class SnapshotSerializedCollectionsTest(unittest.TestCase):
    """Tests for collection subclass serialization.

    Collection subclasses (list/dict/set/tuple) are serialized as their
    specialized types (SerializedList/Dict/Set/Tuple) which preserve both
    the collection data and the object metadata (type name, repr, attributes).
    """

    def test_list_subclass_as_serialized_list(self) -> None:
        """Test that list subclass is serialized as SerializedListObject."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "list_subclass.pytb")
            tintype.initialize()

            list_sub = ListSubclass([1, 2, 3], extra_attr="custom")
            self._capture_value(list_sub)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "captured" in locals_dict:
                    obj = locals_dict["captured"]
                    self.assertIsInstance(obj, tintype.SerializedListObject)
                    self.assertEqual(len(obj), 3)
                    # Verify it has attributes (extra_attr)
                    self.assertTrue(hasattr(obj, "extra_attr"))
                    break
            else:
                self.fail("captured not found in any frame")

    def test_list_subclass_deserialized_as_list(self) -> None:
        """Test that list subclass deserializes as SerializedListObject (a list subclass)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "list_subclass.pytb")
            tintype.initialize()

            list_sub = ListSubclass([10, 20, 30], extra_attr="test")
            self._capture_value(list_sub)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            # Find the list subclass via local variables
            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "list_sub" in locals_dict:
                    obj = locals_dict["list_sub"]
                    self.assertIsInstance(obj, tintype.SerializedListObject)
                    self.assertIsInstance(obj, list)
                    self.assertEqual(list(obj), [10, 20, 30])
                    self.assertTrue(hasattr(obj, "extra_attr"))
                    self.assertEqual(obj.extra_attr, "test")
                    self.assertIn("extra_attr", obj.__dict__)
                    # Verify list methods work
                    self.assertEqual(len(obj), 3)
                    self.assertEqual(obj[0], 10)
                    break
            else:
                self.fail("list_sub not found in any frame")

    def test_dict_subclass_as_serialized_dict(self) -> None:
        """Test that dict subclass is serialized as SerializedDictObject."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "dict_subclass.pytb")
            tintype.initialize()

            dict_sub = DictSubclass({"a": 1, "b": 2}, extra_attr="custom")
            self._capture_value(dict_sub)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "captured" in locals_dict:
                    obj = locals_dict["captured"]
                    self.assertIsInstance(obj, tintype.SerializedDictObject)
                    self.assertEqual(len(obj), 2)
                    # Verify it has attributes
                    self.assertTrue(hasattr(obj, "extra_attr"))
                    break
            else:
                self.fail("captured not found in any frame")

    def test_dict_subclass_deserialized_as_dict(self) -> None:
        """Test that dict subclass deserializes as SerializedDictObject (a dict subclass)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "dict_subclass.pytb")
            tintype.initialize()

            dict_sub = DictSubclass({"x": 10, "y": 20}, extra_attr="test")
            self._capture_value(dict_sub)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            # Find the dict subclass via local variables
            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "dict_sub" in locals_dict:
                    obj = locals_dict["dict_sub"]
                    self.assertIsInstance(obj, tintype.SerializedDictObject)
                    self.assertIsInstance(obj, dict)
                    self.assertEqual(dict(obj), {"x": 10, "y": 20})
                    self.assertTrue(hasattr(obj, "extra_attr"))
                    self.assertEqual(obj.extra_attr, "test")
                    self.assertIn("extra_attr", obj.__dict__)
                    # Verify dict methods work
                    self.assertEqual(len(obj), 2)
                    self.assertEqual(obj["x"], 10)
                    self.assertIn("x", obj.keys())
                    self.assertEqual(sorted(obj.keys()), ["x", "y"])
                    self.assertEqual(sorted(obj.items()), [("x", 10), ("y", 20)])
                    break
            else:
                self.fail("dict_sub not found in any frame")

    def test_set_subclass_as_serialized_set(self) -> None:
        """Test that set subclass is serialized and roundtripped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "set_subclass.pytb")
            tintype.initialize()

            set_sub = SetSubclass({1, 2, 3}, extra_attr="custom")
            self._capture_value(set_sub)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "captured" in locals_dict:
                    obj = locals_dict["captured"]
                    self.assertEqual(len(obj), 3)
                    break
            else:
                self.fail("captured not found in any frame")

    def test_tuple_subclass_as_serialized_tuple(self) -> None:
        """Test that tuple subclass is serialized and roundtripped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "tuple_subclass.pytb")
            tintype.initialize()

            tuple_sub = TupleSubclass((1, 2, 3), extra_attr="custom")
            self._capture_value(tuple_sub)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "captured" in locals_dict:
                    obj = locals_dict["captured"]
                    self.assertEqual(len(obj), 3)
                    break
            else:
                self.fail("captured not found in any frame")

    def _capture_value(self, value: object) -> None:
        captured = value
        _ = captured
        tintype.take_snapshot()


if __name__ == "__main__":
    unittest.main()
