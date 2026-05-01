# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Tests for snapshot navigation via get_prev/get_next_snapshot()."""

import os
import tempfile
import time
import unittest

import tintype


class SnapshotNavigationTest(unittest.TestCase):
    """Tests for navigating between snapshots via get_prev/get_next_snapshot()."""

    def _create_snapshots(
        self, count: int
    ) -> tuple[tintype.SnapshotReader, tintype.Snapshot]:
        """Create count snapshots with small delays and return (reader, latest)."""
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "test.pytb")
        tintype.initialize()
        for i in range(count):
            if i > 0:
                time.sleep(0.01)
            tintype.take_snapshot()
        tintype.finalize(path)
        reader = tintype.SnapshotReader(path)
        latest = reader.get_latest_snapshot()
        self.assertIsNotNone(latest)
        return reader, latest

    def test_single_snapshot_no_prev(self) -> None:
        """Take one snapshot: get_prev_snapshot() returns None."""
        _, snap = self._create_snapshots(1)
        self.assertIsNone(snap.get_prev_snapshot())

    def test_two_snapshots_prev(self) -> None:
        """Take two snapshots: latest snapshot's get_prev_snapshot() returns
        a Snapshot with the earlier timestamp."""
        _, latest = self._create_snapshots(2)

        prev = latest.get_prev_snapshot()
        self.assertIsNotNone(prev)
        self.assertLess(prev.timestamp, latest.timestamp)

    def test_prev_snapshot_has_correct_data(self) -> None:
        """Verify the returned Snapshot has valid stacktraces dict and
        timestamp."""
        _, latest = self._create_snapshots(2)

        prev = latest.get_prev_snapshot()
        self.assertIsNotNone(prev)
        self.assertIsInstance(prev.stacktraces, dict)
        self.assertGreater(len(prev.stacktraces), 0)
        self.assertGreater(prev.timestamp, 0)

    def test_prev_snapshot_chain(self) -> None:
        """Take three snapshots: walk backwards from latest via
        get_prev_snapshot().get_prev_snapshot() and verify timestamps are in
        descending order, and the oldest returns None."""
        _, snap3 = self._create_snapshots(3)

        snap2 = snap3.get_prev_snapshot()
        self.assertIsNotNone(snap2)
        self.assertLess(snap2.timestamp, snap3.timestamp)

        snap1 = snap2.get_prev_snapshot()
        self.assertIsNotNone(snap1)
        self.assertLess(snap1.timestamp, snap2.timestamp)

        self.assertIsNone(snap1.get_prev_snapshot())

    def test_get_prev_snapshot_caching(self) -> None:
        """Call get_prev_snapshot() twice: second call returns the same
        object (assertIs)."""
        _, latest = self._create_snapshots(2)

        first_call = latest.get_prev_snapshot()
        second_call = latest.get_prev_snapshot()
        self.assertIsNotNone(first_call)
        self.assertIs(first_call, second_call)

    def test_prev_snapshot_is_wired(self) -> None:
        """Verify the returned Snapshot's stacktraces have properly wired
        _snapshot back-references and frames work correctly."""
        _, latest = self._create_snapshots(2)

        prev = latest.get_prev_snapshot()
        self.assertIsNotNone(prev)

        for st in prev.stacktraces.values():
            self.assertTrue(hasattr(st, "_snapshot"))
            self.assertIs(st._snapshot, prev)

            self.assertIsNotNone(st.frames)
            self.assertGreater(len(st.frames), 0)

            for frame in st.frames:
                self.assertTrue(hasattr(frame, "_stacktrace"))
                self.assertIs(frame._stacktrace, st)

    # ---- get_next_snapshot() tests ----

    def test_single_snapshot_no_next(self) -> None:
        """Take one snapshot: get_next_snapshot() returns None."""
        _, snap = self._create_snapshots(1)
        self.assertIsNone(snap.get_next_snapshot())

    def test_two_snapshots_next(self) -> None:
        """Take two snapshots: first snapshot's get_next_snapshot() returns
        a Snapshot with the later timestamp."""
        _, latest = self._create_snapshots(2)

        first = latest.get_prev_snapshot()
        self.assertIsNotNone(first)

        nxt = first.get_next_snapshot()
        self.assertIsNotNone(nxt)
        self.assertGreater(nxt.timestamp, first.timestamp)

    def test_latest_snapshot_no_next(self) -> None:
        """The latest snapshot's get_next_snapshot() returns None."""
        _, latest = self._create_snapshots(2)
        self.assertIsNone(latest.get_next_snapshot())

    def test_next_snapshot_chain(self) -> None:
        """Take three snapshots: walk forward from oldest via
        get_next_snapshot() and verify timestamps are in ascending order,
        and the newest returns None."""
        _, snap3 = self._create_snapshots(3)

        snap2 = snap3.get_prev_snapshot()
        self.assertIsNotNone(snap2)
        assert snap2 is not None
        snap1 = snap2.get_prev_snapshot()
        self.assertIsNotNone(snap1)
        assert snap1 is not None

        snap2_via_next = snap1.get_next_snapshot()
        self.assertIsNotNone(snap2_via_next)
        assert snap2_via_next is not None
        self.assertGreater(snap2_via_next.timestamp, snap1.timestamp)

        snap3_via_next = snap2_via_next.get_next_snapshot()
        self.assertIsNotNone(snap3_via_next)
        assert snap3_via_next is not None
        self.assertGreater(snap3_via_next.timestamp, snap2_via_next.timestamp)

        self.assertIsNone(snap3_via_next.get_next_snapshot())

    def test_get_next_snapshot_caching(self) -> None:
        """Call get_next_snapshot() twice: second call returns the same
        object (assertIs)."""
        _, latest = self._create_snapshots(2)

        first = latest.get_prev_snapshot()
        self.assertIsNotNone(first)

        first_call = first.get_next_snapshot()
        second_call = first.get_next_snapshot()
        self.assertIsNotNone(first_call)
        self.assertIs(first_call, second_call)

    def test_next_snapshot_is_wired(self) -> None:
        """Verify the returned Snapshot's stacktraces have properly wired
        _snapshot back-references and frames work correctly."""
        _, latest = self._create_snapshots(2)

        first = latest.get_prev_snapshot()
        self.assertIsNotNone(first)

        nxt = first.get_next_snapshot()
        self.assertIsNotNone(nxt)

        for st in nxt.stacktraces.values():
            self.assertTrue(hasattr(st, "_snapshot"))
            self.assertIs(st._snapshot, nxt)

            self.assertIsNotNone(st.frames)
            self.assertGreater(len(st.frames), 0)

            for frame in st.frames:
                self.assertTrue(hasattr(frame, "_stacktrace"))
                self.assertIs(frame._stacktrace, st)

    # ---- cross-wiring tests ----

    def test_get_prev_sets_next_on_returned_snapshot(self) -> None:
        """get_prev_snapshot() should also set _next_snapshot on the
        returned snapshot, so a subsequent get_next_snapshot() is a
        cache hit."""
        _, latest = self._create_snapshots(2)

        prev = latest.get_prev_snapshot()
        self.assertIsNotNone(prev)
        # prev._next_snapshot should already be set (pointing back to latest)
        self.assertTrue(hasattr(prev, "_next_snapshot"))
        self.assertIs(prev._next_snapshot, latest)

    def test_get_all_snapshots_wires_both_directions(self) -> None:
        """get_all_snapshots() should pre-populate both _prev_snapshot
        and _next_snapshot on every snapshot in the list."""
        reader, _ = self._create_snapshots(3)

        snaps = reader.get_all_snapshots()
        self.assertEqual(len(snaps), 3)

        # First snapshot: no prev, next is second
        self.assertIsNone(snaps[0].get_prev_snapshot())
        self.assertIs(snaps[0].get_next_snapshot(), snaps[1])

        # Middle snapshot: prev is first, next is third
        self.assertIs(snaps[1].get_prev_snapshot(), snaps[0])
        self.assertIs(snaps[1].get_next_snapshot(), snaps[2])

        # Last snapshot: prev is second, no next
        self.assertIs(snaps[2].get_prev_snapshot(), snaps[1])
        self.assertIsNone(snaps[2].get_next_snapshot())

    def test_get_next_sets_prev_on_intermediates(self) -> None:
        """get_next_snapshot() walk should also set _prev_snapshot on
        the intermediate snapshots it creates."""
        _, snap3 = self._create_snapshots(3)

        snap2 = snap3.get_prev_snapshot()
        self.assertIsNotNone(snap2)
        assert snap2 is not None
        snap1 = snap2.get_prev_snapshot()
        self.assertIsNotNone(snap1)
        assert snap1 is not None

        # This triggers the walk from latest, creating intermediates
        snap2 = snap1.get_next_snapshot()
        self.assertIsNotNone(snap2)

        # snap2 should have _prev_snapshot pre-populated (pointing to snap1)
        # without needing a separate get_prev_snapshot() call
        self.assertTrue(hasattr(snap2, "_prev_snapshot"))

    def test_next_snapshot_sees_new_snapshots_on_borrowed_reader(self) -> None:
        """On a borrowed reader, get_next_snapshot() should discover
        snapshots added after it previously returned None."""
        tintype.initialize()
        snap1 = tintype.take_snapshot()
        self.assertIsNotNone(snap1)

        # snap1 is the latest — no next yet
        self.assertIsNone(snap1.get_next_snapshot())

        # Take another snapshot
        time.sleep(0.01)
        snap2 = tintype.take_snapshot()
        self.assertIsNotNone(snap2)

        # snap1 should now see snap2 as its next
        nxt = snap1.get_next_snapshot()
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt.timestamp, snap2.timestamp)

        tintype.finalize()


if __name__ == "__main__":
    unittest.main()
