# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Tests for tintype.utils.tintype_debug_launcher."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import tintype
from tintype.utils import tintype_debug_launcher


class ParseArgsTest(unittest.TestCase):
    def test_positional_pytb_file(self) -> None:
        with patch("sys.argv", ["prog", "/tmp/test.pytb"]):
            args = tintype_debug_launcher._parse_args()
        self.assertEqual(args.pytb_file, "/tmp/test.pytb")
        self.assertIsNone(args.snapshot_index)

    def test_snapshot_index(self) -> None:
        with patch("sys.argv", ["prog", "/tmp/test.pytb", "--snapshot-index", "3"]):
            args = tintype_debug_launcher._parse_args()
        self.assertEqual(args.pytb_file, "/tmp/test.pytb")
        self.assertEqual(args.snapshot_index, 3)

    def test_negative_snapshot_index(self) -> None:
        with patch("sys.argv", ["prog", "/tmp/test.pytb", "--snapshot-index", "-1"]):
            args = tintype_debug_launcher._parse_args()
        self.assertEqual(args.snapshot_index, -1)

    def test_unknown_flags_tolerated(self) -> None:
        with patch(
            "sys.argv",
            ["prog", "/tmp/test.pytb", "--connect", "12345", "--par", "foo"],
        ):
            args = tintype_debug_launcher._parse_args()
        self.assertEqual(args.pytb_file, "/tmp/test.pytb")


class RequestStepBackTest(unittest.TestCase):
    def setUp(self) -> None:
        tintype_debug_launcher._step_back_requested = False

    def test_sets_flag(self) -> None:
        self.assertFalse(tintype_debug_launcher._step_back_requested)
        result = tintype_debug_launcher._request_step_back()
        self.assertTrue(tintype_debug_launcher._step_back_requested)
        self.assertIsInstance(result, str)

    def test_idempotent(self) -> None:
        tintype_debug_launcher._request_step_back()
        tintype_debug_launcher._request_step_back()
        self.assertTrue(tintype_debug_launcher._step_back_requested)

    def tearDown(self) -> None:
        tintype_debug_launcher._step_back_requested = False


class FindInnermostUserFrameTest(unittest.TestCase):
    def test_returns_none_when_all_frames_are_internal(self) -> None:
        """All frames match an internal marker → returns None."""
        # _find_innermost_user_frame walks tb_frame.f_code.co_filename.
        # We can't easily forge a real traceback with arbitrary filenames,
        # so we temporarily widen the markers to match *this* test file.
        original = tintype_debug_launcher._INTERNAL_PATH_MARKERS
        try:
            tintype_debug_launcher._INTERNAL_PATH_MARKERS = (  # pyre-ignore[9]
                # Match every frame in the traceback produced below
                "test_tintype_debug_launcher.py",
            )
            try:
                raise ValueError("test")
            except ValueError:
                tb = sys.exc_info()[2]
            self.assertIsNotNone(tb)
            result = tintype_debug_launcher._find_innermost_user_frame(tb)
            self.assertIsNone(result)
        finally:
            tintype_debug_launcher._INTERNAL_PATH_MARKERS = original

    def test_returns_user_frame(self) -> None:
        """Normal traceback from test code → returns a non-None frame."""
        try:
            raise ValueError("test")
        except ValueError:
            tb = sys.exc_info()[2]
        self.assertIsNotNone(tb)
        frame = tintype_debug_launcher._find_innermost_user_frame(tb)
        self.assertIsNotNone(frame)
        self.assertNotIn("pydevd", frame.f_code.co_filename)


class DescribeStacktraceTest(unittest.TestCase):
    def test_exception_stacktrace(self) -> None:
        st = MagicMock(spec=tintype.Stacktrace)
        st.exception_object = ValueError("something broke")
        result = tintype_debug_launcher._describe_stacktrace(st)
        self.assertEqual(result, "something broke")

    def test_thread_with_name(self) -> None:
        st = MagicMock(spec=tintype.Stacktrace)
        st.exception_object = None
        st.thread_name = "MainThread"
        result = tintype_debug_launcher._describe_stacktrace(st)
        self.assertEqual(result, "MainThread")

    def test_thread_without_name(self) -> None:
        st = MagicMock(spec=tintype.Stacktrace)
        st.exception_object = None
        st.thread_name = ""
        st.id = 42
        result = tintype_debug_launcher._describe_stacktrace(st)
        self.assertEqual(result, "Thread: 42")


class FormatSnapshotHeaderTest(unittest.TestCase):
    def test_format(self) -> None:
        snap = MagicMock(spec=tintype.Snapshot)
        snap.timestamp = 1_700_000_000_000_000  # microseconds
        snap.stacktraces = {"a": None, "b": None}
        result = tintype_debug_launcher._format_snapshot_header(snap, 1, 3)
        self.assertIn("Snapshot 1/3", result)
        self.assertIn("2 stacktrace(s)", result)
        self.assertIn("2023", result)

    def test_single_stacktrace(self) -> None:
        snap = MagicMock(spec=tintype.Snapshot)
        snap.timestamp = 1_700_000_000_000_000
        snap.stacktraces = {"a": None}
        result = tintype_debug_launcher._format_snapshot_header(snap, 2, 5)
        self.assertIn("1 stacktrace(s)", result)


class RunReturnCodesTest(unittest.TestCase):
    def test_returns_2_for_non_tintype_file(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".pytb", delete=False) as f:
            f.write(b"this is not a valid tintype file")
            tmp_path = f.name

        try:
            args = argparse.Namespace(pytb_file=tmp_path, snapshot_index=None)
            result = tintype_debug_launcher.run(args)
            self.assertEqual(result, 2)
        finally:
            os.unlink(tmp_path)

    def test_returns_2_for_nonexistent_file(self) -> None:
        args = argparse.Namespace(
            pytb_file="/nonexistent/path/to/file.pytb", snapshot_index=None
        )
        result = tintype_debug_launcher.run(args)
        self.assertEqual(result, 2)

    def test_returns_1_when_pydevd_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.pytb")
            tintype.initialize()
            tintype.take_snapshot()
            tintype.finalize(path)

            args = argparse.Namespace(pytb_file=path, snapshot_index=None)
            result = tintype_debug_launcher.run(args)
            self.assertEqual(result, 1)


if __name__ == "__main__":
    unittest.main()
