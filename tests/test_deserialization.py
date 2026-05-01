# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Tests for getPythonObject, get_locals, and Serialized*Object types."""

import os
import tempfile
import unittest

import tintype


class SnapshotGetPythonObjectTest(unittest.TestCase):
    """Tests for getPythonObject and get_locals functionality."""

    def test_get_python_object_primitives(self) -> None:
        """Test getPythonObject returns correct Python primitives."""

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "primitives.pytb")
            tintype.initialize()

            int_val = 42
            str_val = "hello"
            float_val = 3.14
            self._capture_three_values(int_val, str_val, float_val)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snapshots = reader.get_all_snapshots()
            snap = snapshots[0]

            # Find the frame with our locals
            target_frame = None
            for frame in snap.frames():
                if frame.function_name == "_capture_three_values":
                    target_frame = frame
                    break

            self.assertIsNotNone(target_frame)

            # Use get_locals to get Python objects
            locals_dict = target_frame.get_locals()

            self.assertIn("a", locals_dict)
            self.assertIn("b", locals_dict)
            self.assertIn("c", locals_dict)

            self.assertEqual(locals_dict["a"], 42)
            self.assertEqual(locals_dict["b"], "hello")
            self.assertAlmostEqual(locals_dict["c"], 3.14, places=5)

    def test_get_python_object_collections(self) -> None:
        """Test getPythonObject returns correct Python collections."""

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "collections.pytb")
            tintype.initialize()

            list_val = [1, 2, 3]
            dict_val = {"a": 1, "b": 2}
            self._capture_two_values(list_val, dict_val)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snapshots = reader.get_all_snapshots()
            snap = snapshots[0]

            target_frame = None
            for frame in snap.frames():
                if frame.function_name == "_capture_two_values":
                    target_frame = frame
                    break

            self.assertIsNotNone(target_frame)

            locals_dict = target_frame.get_locals()

            self.assertIn("v1", locals_dict)
            self.assertIn("v2", locals_dict)

            self.assertEqual(locals_dict["v1"], [1, 2, 3])
            self.assertEqual(locals_dict["v2"], {"a": 1, "b": 2})

    def test_get_python_object_none_true_false(self) -> None:
        """Test getPythonObject handles magic offsets correctly."""

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "singletons.pytb")
            tintype.initialize()

            none_val = None
            true_val = True
            false_val = False
            self._capture_three_values(none_val, true_val, false_val)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snapshots = reader.get_all_snapshots()
            snap = snapshots[0]

            target_frame = None
            for frame in snap.frames():
                if frame.function_name == "_capture_three_values":
                    target_frame = frame
                    break

            self.assertIsNotNone(target_frame)

            locals_dict = target_frame.get_locals()

            self.assertIn("a", locals_dict)
            self.assertIn("b", locals_dict)
            self.assertIn("c", locals_dict)

            self.assertIsNone(locals_dict["a"])
            self.assertTrue(locals_dict["b"])
            self.assertFalse(locals_dict["c"])

    def _capture_three_values(self, v1: object, v2: object, v3: object) -> None:
        a, b, c = v1, v2, v3
        _ = (a, b, c)
        tintype.take_snapshot()

    def _capture_two_values(self, v1: object, v2: object) -> None:
        _ = (v1, v2)
        tintype.take_snapshot()


class SnapshotSerializedTypesTest(unittest.TestCase):
    """Tests for SerializedObject, SerializedListObject, SerializedDictObject types."""

    def test_serialized_object_dict(self) -> None:
        """Test that SerializedObject supports __dict__ for attribute access."""
        obj = tintype.SerializedObject("myrepr")
        self.assertEqual(repr(obj), "myrepr")

        # Should be able to set and get attributes
        obj.foo = 42  # pyre-ignore[16]
        # pyrefly: ignore [missing-attribute]
        self.assertEqual(obj.foo, 42)

        # __dict__ should expose the attribute
        self.assertIn("foo", obj.__dict__)
        self.assertEqual(obj.__dict__["foo"], 42)

    def test_serialized_list_object_dict(self) -> None:
        """Test that SerializedListObject supports __dict__ for attribute access."""
        obj = tintype.SerializedListObject([1, 2, 3], "mylistrepr")
        self.assertEqual(repr(obj), "mylistrepr")
        self.assertEqual(list(obj), [1, 2, 3])

        # Should be able to set and get attributes
        obj.bar = 99  # pyre-ignore[16]
        # pyrefly: ignore [missing-attribute]
        self.assertEqual(obj.bar, 99)

        # __dict__ should expose the attribute
        self.assertIn("bar", obj.__dict__)
        self.assertEqual(obj.__dict__["bar"], 99)

    def test_serialized_dict_object_dict(self) -> None:
        """Test that SerializedDictObject supports __dict__ for attribute access."""
        obj = tintype.SerializedDictObject({"a": 1}, "mydictrepr")
        self.assertEqual(repr(obj), "mydictrepr")
        self.assertEqual(dict(obj), {"a": 1})

        # Should be able to set and get attributes
        obj.baz = 77  # pyre-ignore[16]
        # pyrefly: ignore [missing-attribute]
        self.assertEqual(obj.baz, 77)

        # __dict__ should expose the attribute
        self.assertIn("baz", obj.__dict__)
        self.assertEqual(obj.__dict__["baz"], 77)


if __name__ == "__main__":
    unittest.main()
