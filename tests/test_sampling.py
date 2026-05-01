# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Tests for periodic sampling functionality."""

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

import tintype
from tintype._sampling import disable_sampling, enable_sampling, sampling, SamplingMode

# Check if we're running in free-threaded Python (3.13t+)
# Sampling is disabled in free-threaded Python due to potential deadlocks
IS_FREE_THREADED: bool = hasattr(sys, "_is_gil_enabled") and not sys._is_gil_enabled()


@unittest.skipIf(
    IS_FREE_THREADED,
    "Sampling is not supported in free-threaded Python",
)
class SamplingAllThreadsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "test.pytb")

    def tearDown(self) -> None:
        # Ensure sampling is stopped
        try:
            disable_sampling()
        except RuntimeError:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_all_threads_basic(self) -> None:
        """Basic enable/disable lifecycle with ALL_THREADS mode."""
        tintype.initialize()
        enable_sampling(0.05, SamplingMode.ALL_THREADS, timeout=2.0)
        time.sleep(0.2)  # allow a few samples
        disable_sampling()
        tintype.finalize(self.path)

        reader = tintype.SnapshotReader(self.path)
        self.assertGreater(reader.snapshot_count(), 0)

    def test_all_threads_captures_workers(self) -> None:
        """Verify worker threads appear in ALL_THREADS snapshots."""
        tintype.initialize()

        num_workers = 3
        barrier: threading.Barrier = threading.Barrier(num_workers + 1)
        done: threading.Event = threading.Event()

        def worker() -> None:
            barrier.wait()
            while not done.is_set():
                _ = 1 + 1  # noqa: F841

        threads = []
        for _ in range(num_workers):
            t = threading.Thread(target=worker)
            t.start()
            threads.append(t)

        barrier.wait()

        enable_sampling(0.05, SamplingMode.ALL_THREADS, timeout=5.0)
        time.sleep(0.3)  # allow a few samples
        disable_sampling()

        done.set()
        for t in threads:
            t.join()

        tintype.finalize(self.path)

        reader = tintype.SnapshotReader(self.path)
        self.assertGreater(reader.snapshot_count(), 0)

        # Check that at least one snapshot has multiple threads
        snap = reader.get_latest_snapshot()
        self.assertIsNotNone(snap)
        assert snap is not None
        # Should have at least the workers + main thread
        self.assertGreaterEqual(len(snap.stacktraces), num_workers + 1)

    def test_sampling_thread_not_in_snapshot(self) -> None:
        """The C++ sampling timer thread should NOT appear in snapshots."""
        tintype.initialize()

        enable_sampling(0.05, SamplingMode.ALL_THREADS, timeout=2.0)
        time.sleep(0.2)
        disable_sampling()

        tintype.finalize(self.path)

        reader = tintype.SnapshotReader(self.path)
        snap = reader.get_latest_snapshot()
        self.assertIsNotNone(snap)
        assert snap is not None

        # Check that no stacktrace contains frames from tintype/_sampling.py
        # (the C++ timer thread has no Python frames, so it shouldn't appear)
        # Note: we check for the specific path to avoid false positives from
        # the test file itself (test_sampling.py)
        for _st_id, st in snap.stacktraces.items():
            for frame in st.frames:
                self.assertFalse(
                    frame.file_path.endswith("tintype/_sampling.py"),
                    f"Sampling module frame found in snapshot: {frame.file_path}",
                )


@unittest.skipIf(
    IS_FREE_THREADED,
    "Sampling is not supported in free-threaded Python",
)
class SamplingSingleThreadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "test.pytb")

    def tearDown(self) -> None:
        try:
            disable_sampling()
        except RuntimeError:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_single_thread_basic(self) -> None:
        """Basic enable/disable lifecycle with SINGLE_THREAD mode."""
        tintype.initialize()
        enable_sampling(0.02, SamplingMode.SINGLE_THREAD, timeout=2.0)

        # Busy-loop in Python to give the sampling timer time to fire.
        # The timer fires every 0.02s; we loop for 0.3s to allow many ticks.
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            _ = 1 + 1  # noqa: F841

        disable_sampling()
        tintype.finalize(self.path)

        reader = tintype.SnapshotReader(self.path)
        self.assertGreater(reader.snapshot_count(), 0)

    def test_single_thread_native_fallback(self) -> None:
        """Target thread blocked in native code should be captured via fallback."""
        tintype.initialize()

        # The main thread will be blocked in event.wait() (native code)
        # so sys._current_frames() captures it in native sleep
        enable_sampling(0.05, SamplingMode.SINGLE_THREAD, timeout=0.5)
        time.sleep(0.3)  # blocked in native sleep
        disable_sampling()

        tintype.finalize(self.path)

        reader = tintype.SnapshotReader(self.path)
        # Should have at least one snapshot from the fallback
        self.assertGreater(reader.snapshot_count(), 0)


@unittest.skipIf(
    IS_FREE_THREADED,
    "Sampling is not supported in free-threaded Python",
)
class SamplingContextManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "test.pytb")

    def tearDown(self) -> None:
        # Ensure snapshot module is finalized (e.g., after finalize_on_exit=False)
        try:
            tintype.finalize()
        except Exception:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_context_manager_with_finalize(self) -> None:
        """Context manager should initialize, sample, and finalize."""
        with sampling(0.05, SamplingMode.ALL_THREADS, path=self.path, timeout=2.0):
            time.sleep(0.2)

        self.assertTrue(os.path.exists(self.path))
        reader = tintype.SnapshotReader(self.path)
        self.assertGreater(reader.snapshot_count(), 0)

    def test_context_manager_without_finalize(self) -> None:
        """With finalize_on_exit=False, no file should be written."""
        with sampling(
            0.05,
            SamplingMode.ALL_THREADS,
            path=self.path,
            timeout=2.0,
            finalize_on_exit=False,
        ):
            time.sleep(0.1)

        self.assertFalse(os.path.exists(self.path))


@unittest.skipIf(
    IS_FREE_THREADED,
    "Sampling is not supported in free-threaded Python",
)
class SamplingErrorHandlingTest(unittest.TestCase):
    def tearDown(self) -> None:
        try:
            disable_sampling()
        except RuntimeError:
            pass

    def test_double_enable_raises(self) -> None:
        """Calling enable_sampling() twice should raise RuntimeError."""
        tintype.initialize()
        enable_sampling(0.1, SamplingMode.ALL_THREADS)
        with self.assertRaises(RuntimeError):
            enable_sampling(0.1, SamplingMode.ALL_THREADS)
        disable_sampling()
        tintype.finalize()

    def test_disable_without_enable_raises(self) -> None:
        """Calling disable_sampling() without enable should raise RuntimeError."""
        with self.assertRaises(RuntimeError):
            disable_sampling()


@unittest.skipIf(
    IS_FREE_THREADED,
    "Sampling is not supported in free-threaded Python",
)
class SamplingDefaultModeTest(unittest.TestCase):
    """Tests for the default mode parameter of enable_sampling()."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "test.pytb")

    def tearDown(self) -> None:
        try:
            disable_sampling()
        except RuntimeError:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_enable_sampling_default_mode_is_all_threads(self) -> None:
        """enable_sampling() without mode arg should default to ALL_THREADS."""
        tintype.initialize()

        done: threading.Event = threading.Event()

        def worker() -> None:
            while not done.is_set():
                _ = 1 + 1  # noqa: F841

        t = threading.Thread(target=worker)
        t.start()

        enable_sampling(0.05, timeout=2.0)
        time.sleep(0.2)
        disable_sampling()

        done.set()
        t.join()

        tintype.finalize(self.path)

        reader = tintype.SnapshotReader(self.path)
        self.assertGreater(reader.snapshot_count(), 0)

        snap = reader.get_latest_snapshot()
        self.assertIsNotNone(snap)
        assert snap is not None
        self.assertGreaterEqual(
            len(snap.stacktraces),
            2,
            "Default mode should capture all threads (at least main + worker)",
        )


@unittest.skipIf(
    IS_FREE_THREADED,
    "Sampling is not supported in free-threaded Python",
)
class SamplingConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "test.pytb")

    def tearDown(self) -> None:
        try:
            disable_sampling()
        except RuntimeError:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_concurrent_snapshot_all_threads(self) -> None:
        """Calling snapshot_all_threads() during active sampling should work."""
        tintype.initialize()

        done: threading.Event = threading.Event()

        def worker() -> None:
            while not done.is_set():
                _ = 1 + 1  # noqa: F841

        t = threading.Thread(target=worker)
        t.start()

        enable_sampling(0.1, SamplingMode.ALL_THREADS, timeout=2.0)

        # Call snapshot_all_threads() concurrently — should not crash
        # It may return None if a sampling snapshot is in progress
        for _ in range(3):
            tintype.snapshot_all_threads(timeout=2.0)
            time.sleep(0.05)

        disable_sampling()
        done.set()
        t.join()

        tintype.finalize(self.path)

        reader = tintype.SnapshotReader(self.path)
        self.assertGreater(reader.snapshot_count(), 0)


@unittest.skipIf(
    IS_FREE_THREADED,
    "Sampling is not supported in free-threaded Python",
)
class SamplingMaxFramesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "test.pytb")

    def tearDown(self) -> None:
        try:
            disable_sampling()
        except RuntimeError:
            pass
        try:
            tintype.finalize()
        except Exception:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _deep_snapshot_from_frame(self, depth: int, max_frames: int | None) -> None:
        """Create a deep stack, then snapshot via take_snapshot_from_frame."""
        if depth > 0:
            self._deep_snapshot_from_frame(depth - 1, max_frames)
        else:
            import sys as _sys

            frames = _sys._current_frames()
            tid = threading.get_ident()
            frame = frames[tid]
            if max_frames is not None:
                tintype.take_snapshot_from_frame(frame, tid, max_frames=max_frames)
            else:
                tintype.take_snapshot_from_frame(frame, tid)

    def test_take_snapshot_from_frame_max_frames(self) -> None:
        """take_snapshot_from_frame() should respect max_frames."""
        import sys as _sys

        tintype.initialize()

        tid = threading.get_ident()

        # First: a regular take_snapshot to confirm the module works
        regular = tintype.take_snapshot()
        self.assertIsNotNone(regular, "Regular take_snapshot() returned None")

        # Now test take_snapshot_from_frame without limit
        frames = _sys._current_frames()
        frame = frames[tid]
        unlimited = tintype.take_snapshot_from_frame(frame, tid)
        self.assertIsNotNone(unlimited, "take_snapshot_from_frame() returned None")
        assert unlimited is not None
        self.assertGreater(len(unlimited.frames()), 3)

        # Capture with max_frames=5 — should cap at 5 frames
        frames = _sys._current_frames()
        frame = frames[tid]
        limited = tintype.take_snapshot_from_frame(frame, tid, max_frames=5)
        self.assertIsNotNone(
            limited, "take_snapshot_from_frame(max_frames=5) returned None"
        )
        assert limited is not None
        self.assertLessEqual(len(limited.frames()), 5)
        self.assertTrue(limited.truncated)

        tintype.finalize(self.path)

    def test_enable_sampling_max_frames(self) -> None:
        """enable_sampling(max_frames=N) should cap frames in sampled snapshots."""
        tintype.initialize()

        enable_sampling(
            0.02,
            SamplingMode.SINGLE_THREAD,
            max_frames=3,
            timeout=2.0,
        )

        # Deep recursive call to create a tall stack for sampling to capture
        def deep_work(depth: int) -> int:
            if depth <= 0:
                return 0
            return deep_work(depth - 1) + 1

        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            deep_work(30)

        disable_sampling()
        tintype.finalize(self.path)

        reader = tintype.SnapshotReader(self.path)
        self.assertGreater(reader.snapshot_count(), 0)

        # Every snapshot's stacktrace should have at most 3 frames
        for snap in reader.get_all_snapshots():
            for _st_id, st in snap.stacktraces.items():
                self.assertLessEqual(
                    len(st.frames),
                    3,
                    f"Stacktrace has {len(st.frames)} frames, expected <= 3",
                )

    def test_sampling_context_manager_max_frames(self) -> None:
        """sampling(max_frames=N) should cap frames in sampled snapshots."""
        with sampling(
            0.05,
            SamplingMode.ALL_THREADS,
            max_frames=4,
            timeout=2.0,
            path=self.path,
        ):
            # Sleep to let the sampling timer fire several times.
            # The main thread is in native code, so it's captured via
            # sys._current_frames() fallback.
            time.sleep(0.3)

        reader = tintype.SnapshotReader(self.path)
        self.assertGreater(reader.snapshot_count(), 0)

        for snap in reader.get_all_snapshots():
            for _st_id, st in snap.stacktraces.items():
                self.assertLessEqual(
                    len(st.frames),
                    4,
                    f"Stacktrace has {len(st.frames)} frames, expected <= 4",
                )


@unittest.skipIf(
    IS_FREE_THREADED,
    "Sampling is not supported in free-threaded Python",
)
class SamplingTimeoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "test.pytb")

    def tearDown(self) -> None:
        try:
            disable_sampling()
        except RuntimeError:
            pass
        try:
            tintype.finalize()
        except Exception:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_take_snapshot_from_frame_timeout_completes(self) -> None:
        """take_snapshot_from_frame(timeout=N) should complete normally with a long timeout."""
        import sys as _sys

        tintype.initialize()

        tid = threading.get_ident()
        frames = _sys._current_frames()
        frame = frames[tid]
        result = tintype.take_snapshot_from_frame(frame, tid, timeout=10.0)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertGreater(len(result.frames()), 0)
        self.assertFalse(result.truncated)

        tintype.finalize(self.path)

    def test_take_snapshot_from_frame_timeout_truncates(self) -> None:
        """take_snapshot_from_frame with a short timeout should truncate when
        serialization is slow.

        Uses a custom class whose __repr__ sleeps, making object serialization
        take longer than the timeout. The cancel timer fires mid-serialization,
        producing a truncated tintype.
        """
        import sys as _sys

        class SlowRepr:
            """Object whose repr() takes a controlled amount of time."""

            def __repr__(self) -> str:
                time.sleep(0.2)
                return "SlowRepr()"

        tintype.initialize()

        # Create a call stack where:
        # - The innermost frame (capture_frame) has simple locals → serializes fast
        # - An outer frame (frame_with_slow_locals) has SlowRepr locals → slow
        #
        # writeFramesFromFrame processes innermost first. The fast frame
        # completes before the timer fires. The slow frame triggers
        # cancellation mid-serialization. Result: a truncated snapshot
        # with at least one frame.
        def frame_with_slow_locals() -> None:
            a = SlowRepr()  # noqa: F841
            b = SlowRepr()  # noqa: F841
            c = SlowRepr()  # noqa: F841
            d = SlowRepr()  # noqa: F841
            e = SlowRepr()  # noqa: F841
            capture_frame()

        def capture_frame() -> None:
            fast_var = 42  # noqa: F841
            frames = _sys._current_frames()
            tid = threading.get_ident()
            frame = frames[tid]
            result = tintype.take_snapshot_from_frame(frame, tid, timeout=0.05)
            self.assertIsNotNone(
                result,
                "Snapshot should exist (at least one fast frame written before cancel)",
            )
            assert result is not None
            self.assertTrue(
                result.truncated,
                "Snapshot should be truncated due to timeout during slow repr()",
            )

        frame_with_slow_locals()

        tintype.finalize(self.path)

    def test_sampling_timeout_bounds_single_thread_fallback(self) -> None:
        """A short sampling timeout should cause SINGLE_THREAD mode to capture quickly.

        With a very short timeout, sys._current_frames() is used to capture
        the thread's stack. The test verifies that snapshots are still
        produced despite the short timeout.
        """
        tintype.initialize()

        # Very short timeout = 0.1s
        enable_sampling(
            0.02,
            SamplingMode.SINGLE_THREAD,
            timeout=0.1,
        )

        # Main thread blocked in native code — captured via
        # sys._current_frames() on every tick
        time.sleep(0.3)

        disable_sampling()
        tintype.finalize(self.path)

        reader = tintype.SnapshotReader(self.path)
        # Should have snapshots from the fallback path
        self.assertGreater(reader.snapshot_count(), 0)

    def test_sampling_timeout_bounds_all_threads(self) -> None:
        """Sampling timeout should bound snapshot_all_threads per tick.

        With a native-blocked thread, the timeout controls how long
        the capture operation takes via sys._current_frames().
        """
        tintype.initialize()

        done: threading.Event = threading.Event()

        def native_sleeper() -> None:
            while not done.is_set():
                done.wait(timeout=1.0)

        t = threading.Thread(target=native_sleeper, name="sleeper", daemon=True)
        t.start()

        # Short timeout = 0.2s: barrier wait = 0.1s, fallback = 0.1s
        enable_sampling(
            0.05,
            SamplingMode.ALL_THREADS,
            timeout=0.2,
        )

        time.sleep(0.3)

        disable_sampling()
        done.set()
        t.join(timeout=5.0)

        tintype.finalize(self.path)

        reader = tintype.SnapshotReader(self.path)
        self.assertGreater(reader.snapshot_count(), 0)

        # The sleeper thread should appear (captured via fallback after timeout)
        snap = reader.get_latest_snapshot()
        self.assertIsNotNone(snap)
        assert snap is not None
        self.assertGreaterEqual(len(snap.stacktraces), 2)

    def test_sampling_timeout_truncates_with_slow_repr(self) -> None:
        """Sampling with a short timeout should produce truncated snapshots
        when a thread has SlowRepr locals.

        Uses ALL_THREADS mode with a worker thread that has slow-to-serialize
        locals. The per-tick timeout bounds serialization, producing truncated
        snapshots.
        """

        class SlowRepr:
            def __repr__(self) -> str:
                time.sleep(0.2)
                return "SlowRepr()"

        tintype.initialize()

        done: threading.Event = threading.Event()

        def worker_outer_slow() -> None:
            """Outer frame with slow locals — serialized after inner frames."""
            a = SlowRepr()  # noqa: F841
            b = SlowRepr()  # noqa: F841
            c = SlowRepr()  # noqa: F841
            worker_inner_fast()

        def worker_inner_fast() -> None:
            """Inner frame with fast locals — serialized first."""
            fast_var = 42  # noqa: F841
            done.wait()

        t = threading.Thread(target=worker_outer_slow, name="slow-worker", daemon=True)
        t.start()
        time.sleep(0.05)

        # Short timeout = 0.05s per tick. The worker's inner frame serializes
        # fast, but the outer frame's SlowRepr locals take > 0.05s, triggering
        # cancel and producing truncated snapshots.
        enable_sampling(
            0.1,
            SamplingMode.ALL_THREADS,
            timeout=0.05,
        )

        time.sleep(0.5)

        disable_sampling()
        done.set()
        t.join(timeout=5.0)

        tintype.finalize(self.path)

        reader = tintype.SnapshotReader(self.path)
        self.assertGreater(reader.snapshot_count(), 0)

        # At least some snapshots should have truncated stacktraces
        # (truncation is at the stacktrace level for multi-thread snapshots)
        truncated_count = sum(
            1
            for snap in reader.get_all_snapshots()
            if any(st.truncated for st in snap.stacktraces.values())
        )
        self.assertGreater(
            truncated_count,
            0,
            "Expected at least one snapshot with truncated stacktrace "
            "from slow repr() timeout",
        )


@unittest.skipIf(
    IS_FREE_THREADED,
    "Sampling is not supported in free-threaded Python",
)
class SamplingRecoveryTest(unittest.TestCase):
    """Tests that state is cleaned up after failures."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "test.pytb")

    def tearDown(self) -> None:
        try:
            disable_sampling()
        except RuntimeError:
            pass
        try:
            tintype.finalize()
        except Exception:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_take_snapshot_from_frame_state_not_stuck(self) -> None:
        """Verify snapshotInProgress_ is properly released after each call.

        Multiple sequential calls to take_snapshot_from_frame should all
        succeed — none should return None due to a stuck flag.
        """
        import sys as _sys

        tintype.initialize()
        tid = threading.get_ident()

        for i in range(5):
            frames = _sys._current_frames()
            result = tintype.take_snapshot_from_frame(frames[tid], tid)
            self.assertIsNotNone(
                result,
                f"Call {i + 1}: take_snapshot_from_frame returned None "
                "(snapshotInProgress_ may be stuck)",
            )

        # take_snapshot should also work (shares snapshotInProgress_)
        snap = tintype.take_snapshot()
        self.assertIsNotNone(snap, "take_snapshot failed after from_frame calls")

        tintype.finalize(self.path)


if __name__ == "__main__":
    unittest.main()
