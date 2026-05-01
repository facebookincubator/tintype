# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Tests for circular reference handling in snapshots."""

import os
import tempfile
import unittest

import tintype
from tintype.tests.helpers import TestClass


class SnapshotCircularReferenceTest(unittest.TestCase):
    """Tests for circular reference handling."""

    def test_list_with_self_reference(self) -> None:
        """Test that a list containing itself is handled correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "circular_list.pytb")
            tintype.initialize()

            circular_list: list[object] = [1, 2, 3]
            circular_list.append(circular_list)
            self._capture_value(circular_list)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "captured" in locals_dict:
                    value = locals_dict["captured"]
                    self.assertIsInstance(value, list)
                    self.assertEqual(len(value), 4)
                    break
            else:
                self.fail("captured not found in any frame")

    def test_dict_with_circular_values(self) -> None:
        """Test that a dict with circular references is handled correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "circular_dict.pytb")
            tintype.initialize()

            circular_dict: dict[str, object] = {"a": 1}
            circular_dict["self"] = circular_dict
            self._capture_value(circular_dict)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "captured" in locals_dict:
                    value = locals_dict["captured"]
                    self.assertIsInstance(value, dict)
                    self.assertEqual(len(value), 2)
                    break
            else:
                self.fail("captured not found in any frame")

    def test_object_cycle(self) -> None:
        """Test objects that form a cycle (A -> B -> A)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "object_cycle.pytb")
            tintype.initialize()

            obj_a = TestClass(1, "A")
            obj_b = TestClass(2, "B")
            obj_a.other = obj_b  # pyre-ignore[16]
            # pyrefly: ignore [missing-attribute]
            obj_b.other = obj_a

            self._capture_value(obj_a)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "captured" in locals_dict:
                    obj = locals_dict["captured"]
                    self.assertIsInstance(obj, tintype.SerializedObject)
                    repr_str = repr(obj)
                    self.assertIn("name='A'", repr_str)
                    # Verify the cycle: A -> B via the other attribute
                    self.assertTrue(hasattr(obj, "other"))
                    other = obj.other
                    self.assertIsInstance(other, tintype.SerializedObject)
                    other_repr = repr(other)
                    self.assertIn("name='B'", other_repr)
                    break
            else:
                self.fail("captured not found in any frame")

    def _capture_value(self, value: object) -> None:
        captured = value
        _ = captured
        tintype.take_snapshot()


if __name__ == "__main__":
    unittest.main()
