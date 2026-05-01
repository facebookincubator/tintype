# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Tests for snapshot_all_threads()."""

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

import tintype

# Check if we're running in free-threaded Python (3.13t+)
# snapshot_all_threads is disabled in free-threaded Python due to potential deadlocks
IS_FREE_THREADED: bool = hasattr(sys, "_is_gil_enabled") and not sys._is_gil_enabled()


@unittest.skipIf(
    IS_FREE_THREADED,
    "snapshot_all_threads() is not supported in free-threaded Python",
)
class SnapshotAllThreadsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "test.pytb")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_single_thread(self) -> None:
        """Capturing with only the calling thread should produce one stacktrace."""
        tintype.initialize()
        snap = tintype.snapshot_all_threads(timeout=2.0)
        tintype.finalize(self.path)

        self.assertIsNotNone(snap)
        assert snap is not None
        stacktraces = snap.stacktraces
        self.assertEqual(len(stacktraces), 1)

        # The single stacktrace should have frames
        st = next(iter(stacktraces.values()))
        self.assertGreater(len(st.frames), 0)

    def test_multiple_threads(self) -> None:
        """All threads should appear as stacktraces in the tintype."""
        tintype.initialize()

        num_workers = 3
        barrier: threading.Barrier = threading.Barrier(num_workers + 1)
        done: threading.Event = threading.Event()

        def worker() -> None:
            barrier.wait()
            # Busy-loop in Python so sys._current_frames() captures us
            while not done.is_set():
                _ = 1 + 1  # noqa: F841

        threads = []
        for _ in range(num_workers):
            t = threading.Thread(target=worker)
            t.start()
            threads.append(t)

        # Wait for all workers to be running
        barrier.wait()

        snap = tintype.snapshot_all_threads(timeout=5.0)

        # Release workers
        done.set()
        for t in threads:
            t.join()

        tintype.finalize(self.path)

        self.assertIsNotNone(snap)
        assert snap is not None
        stacktraces = snap.stacktraces
        # We should have at least num_workers + 1 (main thread) stacktraces
        self.assertGreaterEqual(len(stacktraces), num_workers + 1)

    def test_timeout_with_native_blocked_thread(self) -> None:
        """A thread blocked in native code should be captured via fallback."""
        tintype.initialize()

        started: threading.Event = threading.Event()
        done: threading.Event = threading.Event()

        def sleeper() -> None:
            started.set()
            # time.sleep is native code — captured via sys._current_frames()
            done.wait(timeout=10.0)

        t = threading.Thread(target=sleeper)
        t.start()
        started.wait()

        snap = tintype.snapshot_all_threads(timeout=1.0)

        done.set()
        t.join()

        tintype.finalize(self.path)

        self.assertIsNotNone(snap)
        assert snap is not None
        # The sleeping thread should be captured via fallback
        self.assertGreaterEqual(len(snap.stacktraces), 2)

    def test_lock_contention(self) -> None:
        """Both the lock holder and the lock waiter should be captured.

        Thread A holds a lock and executes Python. Thread B is blocked on
        lock.acquire() (native code). Both are captured via
        sys._current_frames() while holding the GIL.
        """
        tintype.initialize()

        lock: threading.Lock = threading.Lock()
        holder_ready: threading.Event = threading.Event()
        waiter_ready: threading.Event = threading.Event()
        done: threading.Event = threading.Event()

        def lock_holder() -> None:
            lock.acquire()
            holder_ready.set()
            # Busy-loop while holding the lock
            while not done.is_set():
                _ = 1 + 1  # noqa: F841
            lock.release()

        def lock_waiter() -> None:
            holder_ready.wait()  # ensure holder has the lock first
            waiter_ready.set()
            lock.acquire()  # blocks in native code until holder releases
            lock.release()

        t1 = threading.Thread(target=lock_holder, name="lock-holder")
        t2 = threading.Thread(target=lock_waiter, name="lock-waiter")
        t1.start()
        t2.start()

        # Wait for both threads to be in position
        holder_ready.wait()
        waiter_ready.wait()

        snap = tintype.snapshot_all_threads(timeout=1.0)

        done.set()
        t1.join()
        t2.join()

        tintype.finalize(self.path)

        self.assertIsNotNone(snap)
        assert snap is not None
        # Should have at least 3 stacktraces: main + holder + waiter
        self.assertGreaterEqual(len(snap.stacktraces), 3)

    def test_max_frames(self) -> None:
        """max_frames should cap the number of frames per stacktrace."""
        tintype.initialize()
        snap = tintype.snapshot_all_threads(timeout=2.0, max_frames=5)
        tintype.finalize(self.path)

        self.assertIsNotNone(snap)
        assert snap is not None
        for st in snap.stacktraces.values():
            self.assertLessEqual(len(st.frames), 5)

    def test_snapshot_readable_from_file(self) -> None:
        """Verify the snapshot can be read back from the finalized file."""
        tintype.initialize()

        done: threading.Event = threading.Event()

        def worker() -> None:
            while not done.is_set():
                _ = 1 + 1  # noqa: F841

        t = threading.Thread(target=worker)
        t.start()

        tintype.snapshot_all_threads(timeout=2.0)
        done.set()
        t.join()

        tintype.finalize(self.path)

        reader = tintype.SnapshotReader(self.path)
        self.assertGreaterEqual(reader.snapshot_count(), 1)
        snap = reader.get_latest_snapshot()
        self.assertIsNotNone(snap)
        assert snap is not None
        self.assertGreaterEqual(len(snap.stacktraces), 2)

    def test_timeout_truncates_with_slow_repr(self) -> None:
        """A short timeout should truncate when a thread has slow-to-serialize locals.

        Uses a custom class whose __repr__ sleeps. The worker thread has
        SlowRepr locals in an outer frame and fast locals in an inner frame.
        The inner frame serializes before the timer fires; the outer frame's
        slow repr() triggers cancellation.
        """

        class SlowRepr:
            def __repr__(self) -> str:
                time.sleep(0.2)
                return "SlowRepr()"

        tintype.initialize()

        done: threading.Event = threading.Event()

        def worker_outer_slow() -> None:
            a = SlowRepr()  # noqa: F841
            b = SlowRepr()  # noqa: F841
            c = SlowRepr()  # noqa: F841
            worker_inner_fast()

        def worker_inner_fast() -> None:
            fast_var = 42  # noqa: F841
            done.wait()

        t = threading.Thread(target=worker_outer_slow, name="slow-worker", daemon=True)
        t.start()
        time.sleep(0.05)

        # Very short timeout: barrier wait = 0.025s, fallback = 0.025s.
        snap = tintype.snapshot_all_threads(timeout=0.05)

        done.set()
        t.join(timeout=5.0)

        tintype.finalize(self.path)

        self.assertIsNotNone(snap)
        assert snap is not None
        self.assertGreater(len(snap.stacktraces), 0)
        # Truncation is at the stacktrace level for multi-thread snapshots
        any_truncated = any(st.truncated for st in snap.stacktraces.values())
        self.assertTrue(
            any_truncated,
            "At least one stacktrace should be truncated due to slow repr()",
        )


@unittest.skipIf(
    IS_FREE_THREADED,
    "snapshot_all_threads() is not supported in free-threaded Python",
)
class SnapshotAllThreadsThreadNameTest(unittest.TestCase):
    """Tests for thread_name attribute on stacktraces."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "test.pytb")

    def tearDown(self) -> None:
        try:
            tintype.finalize()
        except Exception:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_main_thread_has_name(self) -> None:
        """Main thread should have its name captured."""
        tintype.initialize()
        snap = tintype.snapshot_all_threads(timeout=2.0)
        tintype.finalize(self.path)

        self.assertIsNotNone(snap)

        # Find the main thread's stacktrace
        main_thread_id = threading.get_ident()
        self.assertIn(main_thread_id, snap.stacktraces)

        main_st = snap.stacktraces[main_thread_id]
        self.assertEqual(main_st.thread_name, "MainThread")

    def test_named_threads_have_names(self) -> None:
        """Worker threads with explicit names should have those names captured."""
        tintype.initialize()

        barrier: threading.Barrier = threading.Barrier(3)
        done: threading.Event = threading.Event()
        thread_ids: dict[str, int] = {}

        def worker(name: str) -> None:
            thread_ids[name] = threading.get_ident()
            barrier.wait()
            while not done.is_set():
                _ = 1 + 1  # noqa: F841

        t1 = threading.Thread(
            target=worker, args=("worker-alpha",), name="worker-alpha"
        )
        t2 = threading.Thread(target=worker, args=("worker-beta",), name="worker-beta")
        t1.start()
        t2.start()

        barrier.wait()
        snap = tintype.snapshot_all_threads(timeout=5.0)

        done.set()
        t1.join()
        t2.join()

        tintype.finalize(self.path)

        self.assertIsNotNone(snap)

        # Verify both worker threads have their names
        alpha_id = thread_ids["worker-alpha"]
        beta_id = thread_ids["worker-beta"]

        self.assertIn(alpha_id, snap.stacktraces)
        self.assertIn(beta_id, snap.stacktraces)

        self.assertEqual(snap.stacktraces[alpha_id].thread_name, "worker-alpha")
        self.assertEqual(snap.stacktraces[beta_id].thread_name, "worker-beta")

    def test_thread_names_persist_in_file(self) -> None:
        """Thread names should be readable from the finalized file."""
        tintype.initialize()

        barrier: threading.Barrier = threading.Barrier(2)
        done: threading.Event = threading.Event()
        worker_thread_id: list[int] = []

        def worker() -> None:
            worker_thread_id.append(threading.get_ident())
            barrier.wait()
            while not done.is_set():
                _ = 1 + 1  # noqa: F841

        t = threading.Thread(target=worker, name="persistent-worker")
        t.start()

        barrier.wait()
        tintype.snapshot_all_threads(timeout=2.0)

        done.set()
        t.join()

        tintype.finalize(self.path)

        # Read from file and verify thread names
        reader = tintype.SnapshotReader(self.path)
        snap = reader.get_latest_snapshot()
        self.assertIsNotNone(snap)

        # Verify the worker thread has its name
        self.assertIn(worker_thread_id[0], snap.stacktraces)
        self.assertEqual(
            snap.stacktraces[worker_thread_id[0]].thread_name, "persistent-worker"
        )

        # Verify main thread also has its name
        main_thread_id = threading.get_ident()
        self.assertIn(main_thread_id, snap.stacktraces)
        self.assertEqual(snap.stacktraces[main_thread_id].thread_name, "MainThread")

    def test_default_thread_names(self) -> None:
        """Threads without explicit names should have default names like Thread-N."""
        tintype.initialize()

        barrier: threading.Barrier = threading.Barrier(2)
        done: threading.Event = threading.Event()
        thread_id_and_name: list[tuple[int, str]] = []

        def worker() -> None:
            # Capture both the thread ID and the default name
            thread_id_and_name.append(
                (threading.get_ident(), threading.current_thread().name)
            )
            barrier.wait()
            while not done.is_set():
                _ = 1 + 1  # noqa: F841

        # Create thread without explicit name - Python assigns "Thread-N"
        t = threading.Thread(target=worker)
        t.start()

        barrier.wait()
        snap = tintype.snapshot_all_threads(timeout=2.0)

        done.set()
        t.join()

        tintype.finalize(self.path)

        self.assertIsNotNone(snap)

        # Verify the thread has its default name
        tid, expected_name = thread_id_and_name[0]
        self.assertIn(tid, snap.stacktraces)
        self.assertEqual(snap.stacktraces[tid].thread_name, expected_name)
        # Default names start with "Thread-"
        self.assertTrue(snap.stacktraces[tid].thread_name.startswith("Thread-"))


@unittest.skipIf(
    IS_FREE_THREADED,
    "snapshot_all_threads() is not supported in free-threaded Python",
)
class SnapshotAllThreadsStatisticsTest(unittest.TestCase):
    """Tests that stats are updated properly when snapshot_all_threads() is called."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "test.pytb")

    def tearDown(self) -> None:
        try:
            tintype.finalize()
        except Exception:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_snapshot_all_threads_updates_snapshot_count(self) -> None:
        """snapshot_count increments for each snapshot_all_threads() call."""
        tintype.initialize(collect_stats=True)

        tintype.snapshot_all_threads()
        tintype.snapshot_all_threads()
        tintype.snapshot_all_threads()

        stats = tintype.get_stats()
        self.assertEqual(stats["snapshot_count"], 3)

        tintype.finalize(self.path)

    def test_snapshot_all_threads_updates_timing(self) -> None:
        """total_snapshot_time_ms is populated after snapshot_all_threads()."""
        tintype.initialize(collect_stats=True)

        tintype.snapshot_all_threads()

        stats = tintype.get_stats()
        self.assertGreater(stats["total_snapshot_time_ms"], 0)

        tintype.finalize(self.path)

    def test_snapshot_all_threads_updates_frame_count(self) -> None:
        """total_frame_count includes frames from all captured threads."""
        tintype.initialize(collect_stats=True)

        barrier: threading.Barrier = threading.Barrier(3)
        done: threading.Event = threading.Event()

        def worker() -> None:
            barrier.wait()
            while not done.is_set():
                _ = 1 + 1  # noqa: F841

        threads = []
        for _ in range(2):
            t = threading.Thread(target=worker)
            t.start()
            threads.append(t)

        barrier.wait()
        tintype.snapshot_all_threads(timeout=5.0)

        done.set()
        for t in threads:
            t.join()

        stats = tintype.get_stats()
        breakdown = stats["snapshot_breakdown"]
        # Main thread + 2 workers = at least 3 threads, each with >= 1 frame
        self.assertGreaterEqual(breakdown["total_frame_count"], 3)

        tintype.finalize(self.path)

    def test_snapshot_all_threads_updates_object_stats(self) -> None:
        """Object serialization stats are populated from thread locals."""
        tintype.initialize(collect_stats=True)

        barrier: threading.Barrier = threading.Barrier(2)
        done: threading.Event = threading.Event()

        def worker_with_locals() -> None:
            x = 42  # noqa: F841
            y = "hello"  # noqa: F841
            z = [1, 2, 3]  # noqa: F841
            barrier.wait()
            while not done.is_set():
                _ = 1 + 1  # noqa: F841

        t = threading.Thread(target=worker_with_locals)
        t.start()
        barrier.wait()

        tintype.snapshot_all_threads(timeout=5.0)

        done.set()
        t.join()

        stats = tintype.get_stats()
        self.assertGreater(stats["total_objects"], 0)
        self.assertGreater(len(stats["object_stats"]), 0)

        tintype.finalize(self.path)

    def test_mixed_take_snapshot_and_all_threads_stats(self) -> None:
        """Stats accumulate across both take_snapshot() and snapshot_all_threads()."""
        tintype.initialize(collect_stats=True)

        tintype.take_snapshot()
        tintype.take_snapshot()
        tintype.snapshot_all_threads()

        stats = tintype.get_stats()
        self.assertEqual(stats["snapshot_count"], 3)
        self.assertGreater(stats["total_snapshot_time_ms"], 0)

        tintype.finalize(self.path)

    def test_reset_stats_clears_all_threads_stats(self) -> None:
        """reset_stats() clears stats from snapshot_all_threads() calls."""
        tintype.initialize(collect_stats=True)

        tintype.snapshot_all_threads()
        stats_before = tintype.get_stats()
        self.assertEqual(stats_before["snapshot_count"], 1)

        tintype.reset_stats()

        tintype.snapshot_all_threads()
        stats_after = tintype.get_stats()
        self.assertEqual(stats_after["snapshot_count"], 1)

        tintype.finalize(self.path)

    def test_borrowed_reader_sees_all_threads_stats(self) -> None:
        """Borrowed reader sees real-time stats updates from snapshot_all_threads()."""
        reader = tintype.initialize(collect_stats=True)

        initial_stats = reader.get_stats()
        self.assertEqual(initial_stats.get("snapshotCount", 0), 0)

        tintype.snapshot_all_threads()

        stats_1 = reader.get_stats()
        self.assertEqual(stats_1["snapshotCount"], 1)
        self.assertGreater(stats_1["totalSnapshotTimeNs"], 0)

        tintype.snapshot_all_threads()

        stats_2 = reader.get_stats()
        self.assertEqual(stats_2["snapshotCount"], 2)
        self.assertGreaterEqual(
            stats_2["totalSnapshotTimeNs"], stats_1["totalSnapshotTimeNs"]
        )

        tintype.finalize(self.path)

    def test_stats_not_collected_when_disabled(self) -> None:
        """Stats are zero when collect_stats is not enabled."""
        tintype.initialize()  # collect_stats defaults to False

        tintype.snapshot_all_threads()

        stats = tintype.get_stats()
        self.assertEqual(stats["snapshot_count"], 0)

        tintype.finalize(self.path)


if __name__ == "__main__":
    unittest.main()
