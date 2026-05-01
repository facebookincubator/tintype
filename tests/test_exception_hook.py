# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Tests for tintype exception hook functionality."""

import io
import os
import sys
import tempfile
import unittest

import tintype
from tintype import _exception_hook


class ExceptionHookTest(unittest.TestCase):
    """Tests for install_exception_hook and uninstall_exception_hook."""

    def tearDown(self) -> None:
        """Ensure hook is uninstalled after each test."""
        _exception_hook._saved_excepthook = None
        sys.excepthook = sys.__excepthook__

    def test_install_and_uninstall(self) -> None:
        """Test basic install/uninstall lifecycle."""
        tintype.install_exception_hook(path="/tmp/test.pytb")
        tintype.uninstall_exception_hook()

    def test_install_replaces_excepthook(self) -> None:
        """Test that install replaces sys.excepthook."""
        original = sys.excepthook
        tintype.install_exception_hook(path="/tmp/test.pytb")
        self.assertIsNot(sys.excepthook, original)

    def test_uninstall_restores_previous_hook(self) -> None:
        """Test that uninstall restores the previously saved hook."""
        original = sys.excepthook
        tintype.install_exception_hook(path="/tmp/test.pytb")
        tintype.uninstall_exception_hook()
        self.assertIs(sys.excepthook, original)

    def test_double_install_raises(self) -> None:
        """Test that installing twice raises RuntimeError."""
        tintype.install_exception_hook(path="/tmp/test.pytb")
        with self.assertRaises(RuntimeError):
            tintype.install_exception_hook(path="/tmp/test.pytb")

    def test_uninstall_without_install_raises(self) -> None:
        """Test that uninstalling without installing raises RuntimeError."""
        with self.assertRaises(RuntimeError):
            tintype.uninstall_exception_hook()

    def test_hook_captures_snapshot_on_exception(self) -> None:
        """Test that the hook writes a snapshot file when an exception occurs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "crash.pytb")
            tintype.install_exception_hook(
                path=path,
                callback=lambda p: None,
            )

            try:
                1 / 0
            except ZeroDivisionError as exc:
                sys.excepthook(type(exc), exc, exc.__traceback__)

            self.assertTrue(os.path.exists(path))
            reader = tintype.SnapshotReader(path)
            snapshots = reader.get_all_snapshots()
            self.assertGreater(len(snapshots), 0)

    def test_hook_auto_generates_path(self) -> None:
        """Test that a temp file is created when no path is provided."""
        captured_paths: list[str] = []

        def capture_callback(p: str) -> None:
            captured_paths.append(p)

        tintype.install_exception_hook(callback=capture_callback)

        try:
            1 / 0
        except ZeroDivisionError as exc:
            sys.excepthook(type(exc), exc, exc.__traceback__)

        self.assertEqual(len(captured_paths), 1)
        generated_path = captured_paths[0]
        self.assertTrue(os.path.exists(generated_path))
        self.assertTrue(generated_path.endswith(".pytb"))
        self.assertIn("tintype_", os.path.basename(generated_path))

        # Verify the file is readable
        reader = tintype.SnapshotReader(generated_path)
        snapshots = reader.get_all_snapshots()
        self.assertGreater(len(snapshots), 0)

        # Clean up
        os.unlink(generated_path)

    def test_hook_calls_old_excepthook(self) -> None:
        """Test that the hook calls the previously installed excepthook."""
        called: list[bool] = []

        def custom_hook(
            exctype: type[BaseException],
            exc: BaseException,
            tb: object,
        ) -> None:
            called.append(True)

        sys.excepthook = custom_hook
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "crash.pytb")
            tintype.install_exception_hook(
                path=path,
                callback=lambda p: None,
            )

            try:
                1 / 0
            except ZeroDivisionError as exc:
                sys.excepthook(type(exc), exc, exc.__traceback__)

            self.assertEqual(called, [True])

    def test_hook_calls_callback_with_path(self) -> None:
        """Test that the callback receives the correct path."""
        captured_paths: list[str] = []

        def capture_callback(p: str) -> None:
            captured_paths.append(p)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "crash.pytb")
            tintype.install_exception_hook(
                path=path,
                callback=capture_callback,
            )

            try:
                1 / 0
            except ZeroDivisionError as exc:
                sys.excepthook(type(exc), exc, exc.__traceback__)

            self.assertEqual(captured_paths, [path])

    def test_default_callback_prints_message(self) -> None:
        """Test that the default callback prints the expected message."""
        captured = io.StringIO()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "crash.pytb")
            tintype.install_exception_hook(path=path)

            old_stdout = sys.stdout
            try:
                sys.stdout = captured
                try:
                    1 / 0
                except ZeroDivisionError as exc:
                    sys.excepthook(type(exc), exc, exc.__traceback__)
            finally:
                sys.stdout = old_stdout

            output = captured.getvalue()
            self.assertIn(f"Tintype exception snapshot written to: {path}", output)

    def test_snapshot_contains_exception_data(self) -> None:
        """Test that the captured snapshot contains the exception's traceback."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "crash.pytb")
            tintype.install_exception_hook(
                path=path,
                callback=lambda p: None,
            )

            try:
                raise ValueError("test exception message")
            except ValueError as exc:
                sys.excepthook(type(exc), exc, exc.__traceback__)

            reader = tintype.SnapshotReader(path)
            snap = reader.get_latest_snapshot()
            self.assertIsNotNone(snap)

            # The snapshot should have stacktraces (ID 0 for thread, 1+ for exceptions)
            self.assertGreater(len(snap.stacktraces), 0)

            # Find the exception stacktrace
            found_exception = False
            for st_id, st in snap.stacktraces.items():
                if st_id > 0 and st.exception_object is not None:
                    found_exception = True
                    break
            self.assertTrue(found_exception, "Exception stacktrace not found")

    def test_context_manager_installs_and_uninstalls(self) -> None:
        """Test that the context manager installs on enter and uninstalls on exit."""
        original = sys.excepthook
        with tintype.exception_hook(path="/tmp/test.pytb"):
            self.assertIsNot(sys.excepthook, original)
        self.assertIs(sys.excepthook, original)

    def test_context_manager_captures_snapshot(self) -> None:
        """Test that the context manager's hook captures a tintype."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "crash.pytb")
            with tintype.exception_hook(path=path, callback=lambda p: None):
                try:
                    1 / 0
                except ZeroDivisionError as exc:
                    sys.excepthook(type(exc), exc, exc.__traceback__)

            self.assertTrue(os.path.exists(path))
            reader = tintype.SnapshotReader(path)
            snapshots = reader.get_all_snapshots()
            self.assertGreater(len(snapshots), 0)

    def test_context_manager_leaves_hook_on_exception(self) -> None:
        """Test that the hook stays installed when an exception propagates out."""
        original = sys.excepthook
        try:
            with tintype.exception_hook(path="/tmp/test.pytb"):
                raise ValueError("test")
        except ValueError:
            # Hook should still be installed since exception propagated out
            self.assertIsNot(sys.excepthook, original)


if __name__ == "__main__":
    unittest.main()
