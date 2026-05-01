# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""End-to-end tests that verify complete snapshot roundtrip."""

import json
import os
import tempfile
import unittest

import tintype
from tintype.tests.helpers import SlotsClass, TestClass


class SnapshotEndToEndTest(unittest.TestCase):
    """End-to-end tests that verify complete snapshot roundtrip."""

    def test_multiple_snapshots_with_all_types(self) -> None:
        """
        Create multiple snapshots with examples of each type and verify
        that all data is correctly stored and retrieved.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "e2e_test.pytb")

            # Define test metadata
            test_metadata = {
                "test_name": "end_to_end_test",
                "version": 1,
                "tags": ["e2e", "comprehensive"],
            }

            # Initialize
            tintype.initialize()

            # Take snapshot 1: primitives
            self._snapshot_primitives()

            # Take snapshot 2: collections
            self._snapshot_collections()

            # Take snapshot 3: nested structures
            self._snapshot_nested()

            # Finalize with metadata
            tintype.finalize(path, test_metadata)

            # Now read and verify everything
            reader = tintype.SnapshotReader(path)

            # Verify metadata
            stored_metadata = json.loads(reader.get_metadata())
            self.assertEqual(stored_metadata, test_metadata)

            # Get all snapshots
            snapshots = reader.get_all_snapshots()
            self.assertEqual(len(snapshots), 3)

            # Verify snapshots are in chronological order
            for i in range(1, len(snapshots)):
                self.assertGreaterEqual(
                    snapshots[i].timestamp, snapshots[i - 1].timestamp
                )

            # Verify snapshot 1 has primitive types
            self._verify_primitives_snapshot(snapshots[0])

            # Verify snapshot 2 has collection types
            self._verify_collections_snapshot(snapshots[1])

            # Verify snapshot 3 has nested structures
            self._verify_nested_snapshot(snapshots[2])

            # Verify source files were captured
            source_files = reader.get_all_source_files()
            self.assertGreater(len(source_files), 0)

            # This test file should be in the source files
            test_file_found = any(
                "test_end_to_end.py" in sf.path for sf in source_files
            )
            self.assertTrue(test_file_found, "Test file not found in source files")

    def _snapshot_primitives(self) -> None:
        """Take a snapshot with primitive types."""
        int_val = 42
        negative_int = -123
        large_int = 9999999999999
        float_val = 3.14159265359
        negative_float = -273.15
        string_val = "hello world"
        empty_string = ""
        unicode_string = "こんにちは世界"
        bytes_val = b"binary data"
        bool_true = True
        bool_false = False
        none_val = None

        # Use all variables to prevent optimization
        _ = (
            int_val,
            negative_int,
            large_int,
            float_val,
            negative_float,
            string_val,
            empty_string,
            unicode_string,
            bytes_val,
            bool_true,
            bool_false,
            none_val,
        )
        tintype.take_snapshot()

    def _snapshot_collections(self) -> None:
        """Take a snapshot with collection types."""
        list_of_ints = [1, 2, 3, 4, 5]
        list_of_strings = ["a", "b", "c"]
        empty_list: list[int] = []
        tuple_val = (1, "two", 3.0)
        dict_val = {"key1": "value1", "key2": 42}
        empty_dict: dict[str, int] = {}
        set_val = {1, 2, 3}
        nested_list = [[1, 2], [3, 4]]
        mixed_list: list[object] = [1, "two", 3.0, None, True]

        _ = (
            list_of_ints,
            list_of_strings,
            empty_list,
            tuple_val,
            dict_val,
            empty_dict,
            set_val,
            nested_list,
            mixed_list,
        )
        tintype.take_snapshot()

    def _snapshot_nested(self) -> None:
        """Take a snapshot with nested and complex structures."""
        nested_dict = {
            "level1": {
                "level2": {
                    "value": 42,
                }
            }
        }
        list_of_dicts = [{"a": 1}, {"b": 2}]
        dict_of_lists = {"numbers": [1, 2, 3], "letters": ["a", "b"]}

        # Custom object (will be serialized)
        custom_obj = TestClass(value=100, name="test_object")

        _ = (nested_dict, list_of_dicts, dict_of_lists, custom_obj)
        tintype.take_snapshot()

    def _verify_primitives_snapshot(self, snap: tintype.Snapshot) -> None:
        """Verify the primitives snapshot contains expected types and values."""
        for frame in snap.frames():
            if frame.function_name == "_snapshot_primitives":
                locals_dict = frame.get_locals()

                # Verify integer values
                self.assertEqual(locals_dict["int_val"], 42)
                self.assertEqual(locals_dict["negative_int"], -123)
                self.assertEqual(locals_dict["large_int"], 9999999999999)

                # Verify float values
                self.assertAlmostEqual(
                    locals_dict["float_val"], 3.14159265359, places=4
                )
                self.assertAlmostEqual(locals_dict["negative_float"], -273.15, places=2)

                # Verify string values
                self.assertEqual(locals_dict["string_val"], "hello world")
                self.assertEqual(locals_dict["empty_string"], "")
                self.assertEqual(locals_dict["unicode_string"], "こんにちは世界")

                # Verify bytes
                self.assertEqual(locals_dict["bytes_val"], b"binary data")

                # Verify bool and None
                self.assertTrue(locals_dict["bool_true"])
                self.assertFalse(locals_dict["bool_false"])
                self.assertIsNone(locals_dict["none_val"])

                break
        else:
            self.fail("_snapshot_primitives frame not found")

    def _verify_collections_snapshot(self, snap: tintype.Snapshot) -> None:
        """Verify the collections snapshot contains expected types and values."""
        for frame in snap.frames():
            if frame.function_name == "_snapshot_collections":
                locals_dict = frame.get_locals()

                # Verify lists
                self.assertEqual(locals_dict["list_of_ints"], [1, 2, 3, 4, 5])
                self.assertEqual(locals_dict["list_of_strings"], ["a", "b", "c"])
                self.assertEqual(locals_dict["empty_list"], [])

                # Verify tuple
                tuple_val = locals_dict["tuple_val"]
                self.assertEqual(len(tuple_val), 3)

                # Verify dicts
                self.assertEqual(
                    locals_dict["dict_val"], {"key1": "value1", "key2": 42}
                )
                self.assertEqual(locals_dict["empty_dict"], {})

                # Verify set
                set_val = locals_dict["set_val"]
                self.assertEqual(len(set_val), 3)

                # Verify nested list
                nested = locals_dict["nested_list"]
                self.assertEqual(len(nested), 2)

                break
        else:
            self.fail("_snapshot_collections frame not found")

    def _verify_nested_snapshot(self, snap: tintype.Snapshot) -> None:
        """Verify the nested structures snapshot."""
        for frame in snap.frames():
            if frame.function_name == "_snapshot_nested":
                locals_dict = frame.get_locals()

                # Verify nested dict structure
                nested = locals_dict["nested_dict"]
                self.assertIsInstance(nested, dict)
                self.assertIn("level1", nested)

                # Verify list of dicts
                list_of_dicts = locals_dict["list_of_dicts"]
                self.assertEqual(len(list_of_dicts), 2)

                # Verify custom object
                custom_obj = locals_dict["custom_obj"]
                self.assertIsInstance(custom_obj, tintype.SerializedObject)
                repr_str = repr(custom_obj)
                self.assertIn("test_object", repr_str)

                break
        else:
            self.fail("_snapshot_nested frame not found")

    def test_frame_local_variables_match_object_map(self) -> None:
        """Verify that local variables in frames reference valid objects."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "locals_test.pytb")
            tintype.initialize()
            self._function_with_known_locals()
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snapshots = reader.get_all_snapshots()
            self.assertEqual(len(snapshots), 1)

            snap = snapshots[0]

            # Find the frame for _function_with_known_locals
            target_frame = None
            for frame in snap.frames():
                if frame.function_name == "_function_with_known_locals":
                    target_frame = frame
                    break

            self.assertIsNotNone(target_frame, "Target frame not found")

            # Verify function_qualname includes the class name
            self.assertEqual(
                target_frame.function_qualname,
                "SnapshotEndToEndTest._function_with_known_locals",
            )

            # Verify local variables
            local_names = [lv.name for lv in target_frame._local_variables]
            self.assertIn("my_int", local_names)
            self.assertIn("my_string", local_names)
            self.assertIn("my_list", local_names)

            # Verify each local variable's python_id is in the object map
            for local_var in target_frame._local_variables:
                self.assertIn(
                    local_var.python_id,
                    snap.object_map,
                    f"Local var {local_var.name} not in object map",
                )

            # Verify we can resolve locals via get_locals and values are correct
            locals_dict = target_frame.get_locals()
            self.assertEqual(locals_dict["my_int"], 12345)
            self.assertEqual(locals_dict["my_string"], "test_local_string")
            self.assertEqual(locals_dict["my_list"], [1, 2, 3])

    def _function_with_known_locals(self) -> None:
        """Helper with known local variables for testing."""
        my_int = 12345
        my_string = "test_local_string"
        my_list = [1, 2, 3]
        _ = (my_int, my_string, my_list)
        tintype.take_snapshot()

    def test_large_string_serialization(self) -> None:
        """Test that large strings are correctly serialized."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "large_string.pytb")
            tintype.initialize()

            # Create a large string
            large_string = "x" * 100000

            self._capture_value(large_string)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "captured" in locals_dict:
                    value = locals_dict["captured"]
                    self.assertIsInstance(value, str)
                    self.assertEqual(len(value), 100000)
                    self.assertEqual(value, large_string)
                    break
            else:
                self.fail("captured not found in any frame")

    def _capture_value(self, value: object) -> None:
        """Helper to capture a value."""
        captured = value
        _ = captured
        tintype.take_snapshot()

    def test_special_float_values(self) -> None:
        """Test serialization of special float values (inf, -inf)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "special_floats.pytb")
            tintype.initialize()

            pos_inf = float("inf")
            neg_inf = float("-inf")
            # Note: NaN comparisons are tricky, so we skip NaN

            self._capture_two_values(pos_inf, neg_inf)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                if frame.function_name == "_capture_two_values":
                    locals_dict = frame.get_locals()
                    self.assertEqual(locals_dict["v1"], float("inf"))
                    self.assertEqual(locals_dict["v2"], float("-inf"))
                    break
            else:
                self.fail("_capture_two_values frame not found")

    def _capture_two_values(self, val1: object, val2: object) -> None:
        """Helper to capture two values."""
        v1 = val1
        v2 = val2
        _ = (v1, v2)
        tintype.take_snapshot()

    def test_empty_collections(self) -> None:
        """Test that empty collections are correctly serialized."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "empty_collections.pytb")
            tintype.initialize()

            empty_list: list[int] = []
            empty_dict: dict[str, int] = {}
            empty_tuple: tuple[()] = ()
            empty_set: set[int] = set()

            self._capture_four_values(empty_list, empty_dict, empty_tuple, empty_set)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                if frame.function_name == "_capture_four_values":
                    locals_dict = frame.get_locals()
                    self.assertEqual(len(locals_dict["a"]), 0)  # empty list
                    self.assertEqual(len(locals_dict["b"]), 0)  # empty dict
                    self.assertEqual(len(locals_dict["c"]), 0)  # empty tuple
                    self.assertEqual(len(locals_dict["d"]), 0)  # empty set
                    break
            else:
                self.fail("_capture_four_values frame not found")

    def _capture_four_values(
        self, v1: object, v2: object, v3: object, v4: object
    ) -> None:
        """Helper to capture four values."""
        a, b, c, d = v1, v2, v3, v4
        _ = (a, b, c, d)
        tintype.take_snapshot()

    def test_slots_object_serialization(self) -> None:
        """Test that objects with __slots__ have their slot values serialized."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "slots_test.pytb")
            tintype.initialize()

            # Create an object with __slots__
            slots_obj = SlotsClass(x=42, y="hello", z=3.14)

            self._capture_value(slots_obj)
            tintype.finalize(path)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            for frame in snap.frames():
                locals_dict = frame.get_locals()
                if "captured" in locals_dict:
                    obj = locals_dict["captured"]
                    self.assertIsInstance(obj, tintype.SerializedObject)

                    # Verify repr contains expected slot values
                    repr_str = repr(obj)
                    self.assertIn("x=42", repr_str)
                    self.assertIn("y='hello'", repr_str)
                    self.assertIn("z=3.14", repr_str)

                    # Verify slot attributes are accessible
                    self.assertTrue(hasattr(obj, "x"))
                    self.assertTrue(hasattr(obj, "y"))
                    self.assertTrue(hasattr(obj, "z"))
                    self.assertEqual(obj.x, 42)
                    self.assertEqual(obj.y, "hello")
                    self.assertAlmostEqual(float(obj.z), 3.14, places=2)
                    break
            else:
                self.fail("captured not found in any frame")


class SnapshotThreadNameTest(unittest.TestCase):
    """Tests for thread_name attribute on stacktraces from take_snapshot()."""

    def test_take_snapshot_captures_current_thread_name(self) -> None:
        """take_snapshot() should capture the current thread's name."""

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "thread_name_test.pytb")

            tintype.initialize()
            snap = tintype.take_snapshot()
            tintype.finalize(path)

            self.assertIsNotNone(snap)

            # There should be exactly one stacktrace
            self.assertEqual(len(snap.stacktraces), 1)

            # Get the stacktrace and verify it has the main thread's name
            st = next(iter(snap.stacktraces.values()))
            self.assertEqual(st.thread_name, "MainThread")

    def test_take_snapshot_from_worker_thread(self) -> None:
        """take_snapshot() called from a named worker thread should capture that name."""
        import threading

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "worker_thread_test.pytb")

            tintype.initialize()

            captured_snap: list[tintype.Snapshot | None] = [None]
            captured_thread_id: list[int] = []

            def worker() -> None:
                captured_thread_id.append(threading.get_ident())
                captured_snap[0] = tintype.take_snapshot()

            t = threading.Thread(target=worker, name="snapshot-worker")
            t.start()
            t.join()

            tintype.finalize(path)

            snap = captured_snap[0]
            self.assertIsNotNone(snap)

            # There should be exactly one stacktrace
            self.assertEqual(len(snap.stacktraces), 1)

            # Get the stacktrace and verify it has the worker thread's name
            st = next(iter(snap.stacktraces.values()))
            self.assertEqual(st.thread_name, "snapshot-worker")

            # Verify the thread ID matches
            self.assertEqual(st.id, captured_thread_id[0])

    def test_thread_name_persists_in_file(self) -> None:
        """Thread name should be readable from the finalized file."""

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "persist_test.pytb")

            tintype.initialize()
            tintype.take_snapshot()
            tintype.finalize(path)

            # Read from file and verify thread name
            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            # Get the stacktrace and verify it has the main thread's name
            st = next(iter(snap.stacktraces.values()))
            self.assertEqual(st.thread_name, "MainThread")


if __name__ == "__main__":
    unittest.main()
