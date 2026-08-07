# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

import os
import tempfile
import unittest

import tintype
from tintype._exception import _MAX_CHAIN_DEPTH


class ReconstructExceptionTest(unittest.TestCase):
    def _capture(
        self, exc: BaseException
    ) -> tuple[tintype.SnapshotReader, tintype.Snapshot]:
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "test.pytb")
        tintype.initialize()
        tintype.take_snapshot(exc)
        tintype.finalize(path)
        reader = tintype.SnapshotReader(path)
        snap = reader.get_latest_snapshot()
        self.assertIsNotNone(snap)
        assert snap is not None
        return reader, snap

    def _find(self, snap: tintype.Snapshot, type_name: str) -> tintype.Stacktrace:
        for st in snap.stacktraces.values():
            if st.exception_object is not None and type_name in repr(
                st.exception_object
            ):
                return st
        self.fail(f"No stacktrace found for exception type {type_name}")

    def test_reconstructs_message_and_traceback(self) -> None:
        try:
            raise KeyError("missing")
        except KeyError as e:
            captured = e
        _reader, snap = self._capture(captured)
        st = self._find(snap, "KeyError")

        exc = st.reconstruct_exception()
        self.assertIsNotNone(exc)
        assert exc is not None
        # Class is always Exception (original class is not recoverable), but
        # the message is preserved and the traceback is wired.
        self.assertIsInstance(exc, Exception)
        self.assertIn("missing", str(exc))
        self.assertIsNotNone(exc.__traceback__)

    def test_wires_cause_chain(self) -> None:
        try:
            try:
                raise KeyError("inner")
            except KeyError as inner:
                raise RuntimeError("outer") from inner
        except RuntimeError as e:
            captured = e
        _reader, snap = self._capture(captured)
        st = self._find(snap, "RuntimeError")

        exc = st.reconstruct_exception()
        assert exc is not None
        self.assertIn("outer", str(exc))
        cause = exc.__cause__
        self.assertIsNotNone(cause)
        assert cause is not None
        self.assertIn("inner", str(cause))
        self.assertTrue(exc.__suppress_context__)

    def test_wires_context_chain(self) -> None:
        try:
            try:
                raise ValueError("first")
            except ValueError:
                raise TypeError("second")
        except TypeError as e:
            captured = e
        _reader, snap = self._capture(captured)
        st = self._find(snap, "TypeError")

        exc = st.reconstruct_exception()
        assert exc is not None
        self.assertIn("second", str(exc))
        context = exc.__context__
        self.assertIsNotNone(context)
        assert context is not None
        self.assertIn("first", str(context))

    def test_chain_depth_is_capped(self) -> None:
        def raise_chain(level: int) -> None:
            if level == 0:
                raise RuntimeError("level-0")
            try:
                raise_chain(level - 1)
            except RuntimeError as inner:
                raise RuntimeError(f"level-{level}") from inner

        # Unlike the tests above, the raise is behind a call, so the type
        # checker cannot prove the ``except`` branch runs — seed ``captured``.
        captured: RuntimeError | None = None
        try:
            raise_chain(_MAX_CHAIN_DEPTH + 3)
        except RuntimeError as e:
            captured = e
        assert captured is not None
        _reader, snap = self._capture(captured)
        st = snap.stacktraces[1]

        exc = st.reconstruct_exception()
        assert exc is not None
        depth = 0
        current = exc
        while current.__cause__ is not None or current.__context__ is not None:
            current = current.__cause__ or current.__context__
            assert current is not None
            depth += 1
        self.assertEqual(depth, _MAX_CHAIN_DEPTH)

    def test_none_for_thread_snapshot(self) -> None:
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "thread.pytb")
        tintype.initialize()
        tintype.take_snapshot()
        tintype.finalize(path)
        snap = tintype.SnapshotReader(path).get_latest_snapshot()
        assert snap is not None
        # A plain (non-exception) snapshot has no exception_object, so
        # reconstruct_exception is None.
        for st in snap.stacktraces.values():
            if st.exception_object is None:
                self.assertIsNone(st.reconstruct_exception())
                return
        self.skipTest("no non-exception stacktrace present")
