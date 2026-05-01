# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Tests for cause_id, context_id, get_cause(), and get_context()."""

import os
import tempfile
import unittest

import tintype


class ExceptionChainTest(unittest.TestCase):
    """Tests for exception chain navigation via cause_id/context_id and
    get_cause()/get_context()."""

    def _capture_exception(
        self, exc: BaseException
    ) -> tuple[tintype.SnapshotReader, tintype.Snapshot]:
        """Capture an exception snapshot and return (reader, snapshot)."""
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "test.pytb")
        tintype.initialize()
        tintype.take_snapshot(exc)
        tintype.finalize(path)
        reader = tintype.SnapshotReader(path)
        snap = reader.get_latest_snapshot()
        self.assertIsNotNone(snap)
        return reader, snap

    def _find_exception_st(
        self, snap: tintype.Snapshot, exc_type_name: str
    ) -> tintype.Stacktrace:
        """Find the first stacktrace whose exception_object repr contains
        exc_type_name."""
        for st in snap.stacktraces.values():
            if st.exception_object is not None and exc_type_name in repr(
                st.exception_object
            ):
                return st
        self.fail(f"No stacktrace found for exception type {exc_type_name}")

    def test_no_cause_or_context(self) -> None:
        """Single exception with no chain: cause_id and context_id are both
        None; get_cause() and get_context() return None."""
        try:
            raise ValueError("standalone error")
        except Exception as e:
            _, snap = self._capture_exception(e)

        exc_st = None
        for st in snap.stacktraces.values():
            if st.exception_object is not None:
                exc_st = st
                break
        self.assertIsNotNone(exc_st)

        self.assertIsNone(exc_st.cause_id)
        self.assertIsNone(exc_st.context_id)
        self.assertIsNone(exc_st.get_cause())
        self.assertIsNone(exc_st.get_context())

    def test_explicit_cause(self) -> None:
        """'raise X from Y' creates explicit cause chain: the outer exception's
        cause_id is an int pointing to the inner exception; get_cause() returns
        the inner exception's Stacktrace with the correct exception_object type."""
        try:
            try:
                raise KeyError("original")
            except KeyError as inner:
                raise RuntimeError("wrapper") from inner
        except Exception as e:
            _, snap = self._capture_exception(e)

        outer_st = self._find_exception_st(snap, "RuntimeError")

        self.assertIsNotNone(outer_st.cause_id)
        self.assertIsInstance(outer_st.cause_id, int)

        cause = outer_st.get_cause()
        self.assertIsNotNone(cause)
        self.assertIn("KeyError", repr(cause.exception_object))

    def test_implicit_context(self) -> None:
        """'raise X' during 'except Y' creates implicit context chain: the outer
        exception's context_id is an int pointing to the inner exception;
        get_context() returns the correct Stacktrace."""
        try:
            try:
                raise ValueError("first")
            except ValueError:
                raise TypeError("second")
        except Exception as e:
            _, snap = self._capture_exception(e)

        outer_st = self._find_exception_st(snap, "TypeError")

        self.assertIsNotNone(outer_st.context_id)
        self.assertIsInstance(outer_st.context_id, int)

        context = outer_st.get_context()
        self.assertIsNotNone(context)
        self.assertIn("ValueError", repr(context.exception_object))

    def test_multi_level_chain(self) -> None:
        """Three-level chain: ConnectionError -> ValueError via 'from' ->
        RuntimeError via implicit context. Verify get_cause() and get_context()
        walk the full chain correctly."""
        try:
            try:
                try:
                    raise ConnectionError("network")
                except ConnectionError as ce:
                    raise ValueError("validation") from ce
            except ValueError:
                raise RuntimeError("top-level")
        except Exception as e:
            _, snap = self._capture_exception(e)

        top_st = self._find_exception_st(snap, "RuntimeError")

        # RuntimeError has implicit context -> ValueError
        context_st = top_st.get_context()
        self.assertIsNotNone(context_st)
        self.assertIn("ValueError", repr(context_st.exception_object))

        # ValueError has explicit cause -> ConnectionError
        cause_st = context_st.get_cause()
        self.assertIsNotNone(cause_st)
        self.assertIn("ConnectionError", repr(cause_st.exception_object))

    def test_leaf_exception_has_no_cause(self) -> None:
        """The innermost exception in a chain has cause_id is None and
        get_cause() is None."""
        try:
            try:
                raise KeyError("leaf")
            except KeyError as ke:
                raise RuntimeError("outer") from ke
        except Exception as e:
            _, snap = self._capture_exception(e)

        leaf_st = self._find_exception_st(snap, "KeyError")

        self.assertIsNone(leaf_st.cause_id)
        self.assertIsNone(leaf_st.get_cause())

    def test_get_cause_caching(self) -> None:
        """Call get_cause() twice on the same stacktrace: second call returns
        the same object (assertIs)."""
        try:
            try:
                raise KeyError("inner")
            except KeyError as ke:
                raise RuntimeError("outer") from ke
        except Exception as e:
            _, snap = self._capture_exception(e)

        outer_st = self._find_exception_st(snap, "RuntimeError")

        first_call = outer_st.get_cause()
        second_call = outer_st.get_cause()
        self.assertIsNotNone(first_call)
        self.assertIs(first_call, second_call)

    def test_get_context_caching(self) -> None:
        """Call get_context() twice on the same stacktrace: second call returns
        the same object (assertIs)."""
        try:
            try:
                raise ValueError("first")
            except ValueError:
                raise TypeError("second")
        except Exception as e:
            _, snap = self._capture_exception(e)

        outer_st = self._find_exception_st(snap, "TypeError")

        first_call = outer_st.get_context()
        second_call = outer_st.get_context()
        self.assertIsNotNone(first_call)
        self.assertIs(first_call, second_call)


if __name__ == "__main__":
    unittest.main()
