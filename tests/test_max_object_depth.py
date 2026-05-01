# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Tests for max_object_depth parameter."""

import os
import tempfile
import unittest

import tintype


class NestedObj:
    def __init__(self, child: object = None) -> None:
        self.child = child
        self.value = 42


class MaxObjectDepthTest(unittest.TestCase):
    """Tests for the max_object_depth parameter."""

    def _capture_locals_with_depth(
        self,
        max_object_depth: int | None,
        **kwargs: object,
    ) -> None:
        """Helper: assigns kwargs as locals, then takes a snapshot."""
        # Each kwarg becomes a local variable in this frame
        local_vars = kwargs
        _ = local_vars  # prevent unused warning
        tintype.take_snapshot(max_object_depth=max_object_depth)

    def test_depth_none_no_limit(self) -> None:
        """max_object_depth=None captures full object graph."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()

            obj = NestedObj(NestedObj(NestedObj()))
            self._capture_with_nested(obj, None)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            # Find our frame and get the nested object
            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "nested" in locals_dict:
                    nested = locals_dict["nested"]
                    # Should be a fully serialized object with attributes
                    self.assertIsInstance(nested, tintype.SerializedObject)
                    self.assertTrue(hasattr(nested, "child"))
                    self.assertTrue(hasattr(nested, "value"))
                    # The child should also be fully serialized
                    child = nested.child
                    self.assertIsInstance(child, tintype.SerializedObject)
                    self.assertTrue(hasattr(child, "child"))
                    break
            else:
                self.fail("nested not found in any frame")

    def _capture_with_nested(self, obj: object, max_object_depth: int | None) -> None:
        nested = obj
        _ = nested
        tintype.take_snapshot(max_object_depth=max_object_depth)

    def test_depth_zero_locals_become_repr(self) -> None:
        """max_object_depth=0: non-primitive locals become repr."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()

            obj = NestedObj()
            self._capture_with_nested(obj, 0)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "nested" in locals_dict:
                    nested = locals_dict["nested"]
                    # Should be a SerializedObject but with no child attributes
                    self.assertIsInstance(nested, tintype.SerializedObject)
                    # At depth 0, the object should have no attributes
                    # (serialized as repr only)
                    self.assertFalse(hasattr(nested, "child"))
                    self.assertFalse(hasattr(nested, "value"))
                    break
            else:
                self.fail("nested not found in any frame")

    def test_depth_one_children_processed(self) -> None:
        """max_object_depth=1: direct children processed, grandchildren repr."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()

            obj = NestedObj(NestedObj())
            self._capture_with_nested(obj, 1)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "nested" in locals_dict:
                    nested = locals_dict["nested"]
                    # The top-level object should be fully serialized
                    self.assertIsInstance(nested, tintype.SerializedObject)
                    self.assertTrue(hasattr(nested, "child"))
                    self.assertTrue(hasattr(nested, "value"))
                    # But the child should be depth-limited (no attributes)
                    child = nested.child
                    self.assertIsInstance(child, tintype.SerializedObject)
                    self.assertFalse(hasattr(child, "child"))
                    self.assertFalse(hasattr(child, "value"))
                    break
            else:
                self.fail("nested not found in any frame")

    def test_primitives_unaffected_by_depth(self) -> None:
        """max_object_depth=0: primitive locals still fully serialized."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()

            self._capture_primitives()
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "my_int" in locals_dict:
                    self.assertEqual(locals_dict["my_int"], 42)
                    self.assertEqual(locals_dict["my_str"], "hello")
                    self.assertAlmostEqual(locals_dict["my_float"], 3.14, places=2)
                    self.assertTrue(locals_dict["my_bool"])
                    self.assertIsNone(locals_dict["my_none"])
                    self.assertEqual(locals_dict["my_bytes"], b"data")
                    break
            else:
                self.fail("my_int not found in any frame")

    def _capture_primitives(self) -> None:
        my_int = 42
        my_str = "hello"
        my_float = 3.14
        my_bool = True
        my_none = None
        my_bytes = b"data"
        _ = (my_int, my_str, my_float, my_bool, my_none, my_bytes)
        tintype.take_snapshot(max_object_depth=0)

    def test_list_depth_limited(self) -> None:
        """max_object_depth=1: inner lists become repr."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()

            self._capture_nested_list()
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "my_list" in locals_dict:
                    my_list = locals_dict["my_list"]
                    # Outer list should be a real list
                    self.assertIsInstance(my_list, list)
                    self.assertEqual(len(my_list), 2)
                    # Inner lists should be depth-limited SerializedObjects
                    for item in my_list:
                        self.assertIsInstance(item, tintype.SerializedObject)
                    break
            else:
                self.fail("my_list not found in any frame")

    def _capture_nested_list(self) -> None:
        my_list = [[1, 2], [3, 4]]
        _ = my_list
        tintype.take_snapshot(max_object_depth=1)

    def test_dict_depth_limited(self) -> None:
        """max_object_depth=1: inner dicts become repr."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()

            self._capture_nested_dict()
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "my_dict" in locals_dict:
                    my_dict = locals_dict["my_dict"]
                    # Outer dict should be a real dict
                    self.assertIsInstance(my_dict, dict)
                    self.assertIn("inner", my_dict)
                    # Inner dict should be depth-limited
                    self.assertIsInstance(my_dict["inner"], tintype.SerializedObject)
                    break
            else:
                self.fail("my_dict not found in any frame")

    def _capture_nested_dict(self) -> None:
        my_dict = {"inner": {"a": 1, "b": 2}}
        _ = my_dict
        tintype.take_snapshot(max_object_depth=1)

    def test_custom_object_depth_limited(self) -> None:
        """max_object_depth=1: nested custom object attributes become repr."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()

            inner = NestedObj()
            outer = NestedObj(inner)
            self._capture_with_nested(outer, 1)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "nested" in locals_dict:
                    nested = locals_dict["nested"]
                    self.assertIsInstance(nested, tintype.SerializedObject)
                    # Outer object has attributes
                    self.assertTrue(hasattr(nested, "child"))
                    # But child object is depth-limited (no attributes)
                    child = nested.child
                    self.assertIsInstance(child, tintype.SerializedObject)
                    self.assertFalse(hasattr(child, "value"))
                    break
            else:
                self.fail("nested not found in any frame")

    def test_object_depth_truncated_flag_set(self) -> None:
        """object_depth_truncated flag is True when depth limit is hit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()

            obj = NestedObj(NestedObj())
            self._capture_with_nested(obj, 0)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for st in snap.stacktraces.values():
                self.assertTrue(st.object_depth_truncated)
                # truncated flag should NOT be set (no frames were omitted)
                self.assertFalse(st.truncated)

    def test_object_depth_truncated_flag_not_set(self) -> None:
        """object_depth_truncated flag is False when no depth limit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()

            obj = NestedObj()
            self._capture_with_nested(obj, None)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for st in snap.stacktraces.values():
                self.assertFalse(st.object_depth_truncated)

    def test_max_object_depth_with_max_frames(self) -> None:
        """Both max_frames and max_object_depth work together."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()

            self._capture_both_limits()
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            # Should have limited frames
            self.assertLessEqual(len(snap.frames()), 3)

    def _capture_both_limits(self) -> None:
        obj = NestedObj(NestedObj())
        _ = obj
        tintype.take_snapshot(max_frames=3, max_object_depth=1)

    def test_exception_snapshot_depth_limited(self) -> None:
        """max_object_depth works with exception snapshots."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()

            try:
                raise ValueError("test error")
            except ValueError as e:
                tintype.take_snapshot(e, max_object_depth=1)

            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)
            self.assertGreater(len(snap.stacktraces), 0)


if __name__ == "__main__":
    unittest.main()
