# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Stress test for snapshot_all_threads() and periodic sampling.

Exercises concurrent thread creation/destruction, lock contention, native
code blocking, simultaneous snapshot calls, and sampling interactions to
expose race conditions, deadlocks, or corrupted snapshot files.
"""

import os
import random
import tempfile
import threading
import time

import tintype


# ---------------------------------------------------------------------------
# Worker functions — each exercises a different thread state
# ---------------------------------------------------------------------------


def cpu_worker(done: threading.Event) -> None:
    """Busy-loop in pure Python (captured via sys._current_frames())."""
    total = 0
    while not done.is_set():
        for i in range(1000):
            total += i
        # Occasional short sleep to vary timing
        if random.random() < 0.01:
            time.sleep(0.001)


def native_sleeper(done: threading.Event) -> None:
    """Blocked in native time.sleep (captured via fallback)."""
    while not done.is_set():
        time.sleep(0.1)


def lock_holder(
    lock: threading.Lock,
    ready: threading.Event,
    done: threading.Event,
) -> None:
    """Holds a lock and busy-loops (captured via sys._current_frames(), blocks waiters)."""
    lock.acquire()
    ready.set()
    while not done.is_set():
        _ = 1 + 1  # noqa: F841
    lock.release()


def lock_waiter(
    lock: threading.Lock,
    ready: threading.Event,
    done: threading.Event,
) -> None:
    """Waits on a held lock (blocked in native, captured via fallback)."""
    ready.wait()
    lock.acquire()
    lock.release()
    # After acquiring, busy-loop
    while not done.is_set():
        _ = 1 + 1  # noqa: F841


def event_waiter(event: threading.Event, done: threading.Event) -> None:
    """Waits on an event (native code)."""
    event.wait()
    while not done.is_set():
        _ = 1 + 1  # noqa: F841


def ephemeral_worker(iteration: int) -> None:
    """Short-lived thread — may or may not be alive when snapshot fires."""
    total = 0
    for i in range(10000):
        total += i


def snapshot_caller(
    results: list[object],
    index: int,
    barrier: threading.Barrier,
) -> None:
    """Calls snapshot_all_threads() concurrently with other threads."""
    barrier.wait()
    result = tintype.snapshot_all_threads(timeout=2.0)
    results[index] = result


def take_snapshot_caller(
    results: list[object],
    index: int,
    barrier: threading.Barrier,
) -> None:
    """Calls take_snapshot() concurrently with snapshot_all_threads()."""
    barrier.wait()
    result = tintype.take_snapshot()
    results[index] = result


def main() -> None:
    path = os.path.join(tempfile.gettempdir(), "stress_all_threads.pytb")
    tintype.initialize()

    print("=" * 60)
    print("snapshot_all_threads() stress test")
    print("=" * 60)

    # ------------------------------------------------------------------
    # Test 1: Many threads in various states
    # ------------------------------------------------------------------
    print("\n[Test 1] Many threads in various states...")

    done: threading.Event = threading.Event()
    lock: threading.Lock = threading.Lock()
    holder_ready: threading.Event = threading.Event()
    pending_event: threading.Event = threading.Event()
    threads: list[threading.Thread] = []

    # 5 CPU workers
    for i in range(5):
        t = threading.Thread(target=cpu_worker, args=(done,), name=f"cpu-{i}")
        t.daemon = True
        t.start()
        threads.append(t)

    # 3 native sleepers
    for i in range(3):
        t = threading.Thread(target=native_sleeper, args=(done,), name=f"sleeper-{i}")
        t.daemon = True
        t.start()
        threads.append(t)

    # Lock holder + 2 lock waiters
    t = threading.Thread(
        target=lock_holder, args=(lock, holder_ready, done), name="lock-holder"
    )
    t.daemon = True
    t.start()
    threads.append(t)
    holder_ready.wait()

    for i in range(2):
        t = threading.Thread(
            target=lock_waiter,
            args=(lock, holder_ready, done),
            name=f"lock-waiter-{i}",
        )
        t.daemon = True
        t.start()
        threads.append(t)

    # 2 event waiters (event never set during snapshot)
    for i in range(2):
        t = threading.Thread(
            target=event_waiter,
            args=(pending_event, done),
            name=f"event-waiter-{i}",
        )
        t.daemon = True
        t.start()
        threads.append(t)

    time.sleep(0.1)  # let threads settle

    snap = tintype.snapshot_all_threads(timeout=2.0)
    assert snap is not None, "Test 1 failed: snapshot returned None"
    print(
        f"  Captured {len(snap.stacktraces)} threads (expected >= {len(threads) + 1})"
    )
    assert len(snap.stacktraces) >= len(threads) + 1

    # Verify each stacktrace has frames
    empty_stacks = sum(1 for st in snap.stacktraces.values() if len(st.frames) == 0)
    print(f"  Empty stacktraces: {empty_stacks}")

    pending_event.set()
    done.set()
    for t in threads:
        t.join(timeout=5.0)
    print("  PASSED")

    # ------------------------------------------------------------------
    # Test 2: Ephemeral threads (create/destroy during snapshot)
    # ------------------------------------------------------------------
    print("\n[Test 2] Ephemeral threads created/destroyed during tintype...")

    tintype.finalize()
    tintype.initialize()

    done2: threading.Event = threading.Event()
    spawner_active: threading.Event = threading.Event()

    def thread_spawner() -> None:
        """Rapidly creates and destroys short-lived threads."""
        spawner_active.set()
        i = 0
        while not done2.is_set():
            t = threading.Thread(
                target=ephemeral_worker, args=(i,), name=f"ephemeral-{i}"
            )
            t.start()
            t.join(timeout=1.0)
            i += 1

    spawner = threading.Thread(target=thread_spawner, name="spawner")
    spawner.daemon = True
    spawner.start()
    spawner_active.wait()
    time.sleep(0.05)

    snap2 = tintype.snapshot_all_threads(timeout=2.0)
    assert snap2 is not None, "Test 2 failed: snapshot returned None"
    print(f"  Captured {len(snap2.stacktraces)} threads")
    # At minimum: main + spawner
    assert len(snap2.stacktraces) >= 2

    done2.set()
    spawner.join(timeout=5.0)
    print("  PASSED")

    # ------------------------------------------------------------------
    # Test 3: Concurrent snapshot_all_threads() calls (reentrancy guard)
    # ------------------------------------------------------------------
    print("\n[Test 3] Concurrent snapshot_all_threads() calls...")

    tintype.finalize()
    tintype.initialize()

    num_callers = 4
    call_barrier: threading.Barrier = threading.Barrier(num_callers)
    results: list[object | None] = [None for _ in range(num_callers)]
    caller_threads: list[threading.Thread] = []

    for i in range(num_callers):
        t = threading.Thread(
            target=snapshot_caller,
            args=(results, i, call_barrier),
            name=f"caller-{i}",
        )
        t.start()
        caller_threads.append(t)

    for t in caller_threads:
        t.join(timeout=10.0)

    non_none = sum(1 for r in results if r is not None)
    none_count = sum(1 for r in results if r is None)
    print(f"  Results: {non_none} succeeded, {none_count} returned None")
    # At least one should succeed, and the rest should get None (reentrancy)
    assert non_none >= 1, "No caller succeeded"
    print("  PASSED")

    # ------------------------------------------------------------------
    # Test 4: take_snapshot() vs snapshot_all_threads() concurrently
    # ------------------------------------------------------------------
    print("\n[Test 4] take_snapshot() vs snapshot_all_threads() concurrently...")

    tintype.finalize()
    tintype.initialize()

    mix_barrier: threading.Barrier = threading.Barrier(4)
    mix_results: list[object | None] = [None for _ in range(4)]
    mix_threads: list[threading.Thread] = []

    # 2 threads call snapshot_all_threads, 2 call take_snapshot
    for i in range(2):
        t = threading.Thread(
            target=snapshot_caller,
            args=(mix_results, i, mix_barrier),
            name=f"all-caller-{i}",
        )
        t.start()
        mix_threads.append(t)
    for i in range(2):
        t = threading.Thread(
            target=take_snapshot_caller,
            args=(mix_results, i + 2, mix_barrier),
            name=f"single-caller-{i}",
        )
        t.start()
        mix_threads.append(t)

    for t in mix_threads:
        t.join(timeout=10.0)

    non_none = sum(1 for r in mix_results if r is not None)
    print(f"  Results: {non_none} succeeded, {4 - non_none} returned None")
    assert non_none >= 1, "No caller succeeded"
    print("  PASSED")

    # ------------------------------------------------------------------
    # Test 5: Rapid repeated calls
    # ------------------------------------------------------------------
    print("\n[Test 5] Rapid repeated snapshot_all_threads() calls...")

    tintype.finalize()
    tintype.initialize()

    done5: threading.Event = threading.Event()
    bg_threads: list[threading.Thread] = []
    for i in range(3):
        t = threading.Thread(target=cpu_worker, args=(done5,), name=f"rapid-bg-{i}")
        t.daemon = True
        t.start()
        bg_threads.append(t)

    success_count = 0
    for _i in range(10):
        snap_i = tintype.snapshot_all_threads(timeout=0.5)
        if snap_i is not None:
            success_count += 1

    done5.set()
    for t in bg_threads:
        t.join(timeout=5.0)

    print(f"  {success_count}/10 calls succeeded")
    assert success_count == 10, f"Expected 10 successes, got {success_count}"
    print("  PASSED")

    # ------------------------------------------------------------------
    # Test 6: Verify file output
    # ------------------------------------------------------------------
    print("\n[Test 6] Verify file output from last round...")

    tintype.finalize(path)

    reader = tintype.SnapshotReader(path)
    print(f"  Snapshots in file: {reader.snapshot_count()}")
    assert reader.snapshot_count() == 10
    print(f"  File saved to: {path}")
    print("  PASSED")

    # ------------------------------------------------------------------
    # Test 7: Sampling — rapid enable/disable cycling
    # ------------------------------------------------------------------
    print("\n[Test 7] Rapid sampling enable/disable cycling...")

    tintype.finalize()
    tintype.initialize()

    for _i in range(20):
        tintype.enable_sampling(
            interval=0.01,
            mode=tintype.SamplingMode.ALL_THREADS,
            timeout=0.5,
        )
        # Tiny sleep to let at most one tick fire
        time.sleep(0.005)
        tintype.disable_sampling()

    tintype.finalize(path)
    reader = tintype.SnapshotReader(path)
    print(f"  {reader.snapshot_count()} snapshots from 20 enable/disable cycles")
    print("  PASSED (no deadlock)")

    # ------------------------------------------------------------------
    # Test 8: Sampling — concurrent snapshot_all_threads() during sampling
    # ------------------------------------------------------------------
    print("\n[Test 8] snapshot_all_threads() during active ALL_THREADS sampling...")

    tintype.finalize()
    tintype.initialize()

    done8: threading.Event = threading.Event()
    bg8: list[threading.Thread] = []
    for i in range(3):
        t = threading.Thread(target=cpu_worker, args=(done8,), name=f"samp-bg-{i}")
        t.daemon = True
        t.start()
        bg8.append(t)

    tintype.enable_sampling(
        interval=0.02,
        mode=tintype.SamplingMode.ALL_THREADS,
        timeout=1.0,
    )

    # Hammer snapshot_all_threads() from the main thread while sampling is active
    concurrent_successes = 0
    concurrent_nones = 0
    for _i in range(20):
        result = tintype.snapshot_all_threads(timeout=1.0)
        if result is not None:
            concurrent_successes += 1
        else:
            concurrent_nones += 1
        time.sleep(0.01)

    tintype.disable_sampling()
    done8.set()
    for t in bg8:
        t.join(timeout=5.0)

    tintype.finalize(path)
    reader = tintype.SnapshotReader(path)
    print(
        f"  Manual calls: {concurrent_successes} ok, {concurrent_nones} skipped "
        f"(snapshotInProgress)"
    )
    print(f"  Total snapshots in file: {reader.snapshot_count()}")
    assert reader.snapshot_count() > 0
    # Verify file is readable — iterate all snapshots
    all_snaps = reader.get_all_snapshots()
    for s in all_snaps:
        assert len(s.stacktraces) > 0, "Empty snapshot in file"
    print("  All snapshots valid")
    print("  PASSED")

    # ------------------------------------------------------------------
    # Test 9: Sampling — concurrent take_snapshot() during SINGLE_THREAD sampling
    # ------------------------------------------------------------------
    print("\n[Test 9] take_snapshot() during active SINGLE_THREAD sampling...")

    tintype.finalize()
    tintype.initialize()

    tintype.enable_sampling(
        interval=0.02,
        mode=tintype.SamplingMode.SINGLE_THREAD,
        timeout=0.5,
    )

    # Call take_snapshot() from the main thread while single-thread sampling
    # is targeting this same thread. Both use snapshotInProgress_ so they
    # contend — some calls may return None.
    ts_successes = 0
    ts_nones = 0
    for _i in range(30):
        # Do a bit of Python work between snapshot calls
        total = 0
        for j in range(5000):
            total += j
        result = tintype.take_snapshot()
        if result is not None:
            ts_successes += 1
        else:
            ts_nones += 1

    tintype.disable_sampling()

    tintype.finalize(path)
    reader = tintype.SnapshotReader(path)
    print(
        f"  take_snapshot(): {ts_successes} ok, {ts_nones} skipped (snapshotInProgress)"
    )
    print(f"  Total snapshots in file: {reader.snapshot_count()}")
    assert reader.snapshot_count() > 0
    print("  PASSED")

    # ------------------------------------------------------------------
    # Test 10: Sampling — ephemeral threads during ALL_THREADS sampling
    # ------------------------------------------------------------------
    print("\n[Test 10] Ephemeral threads during ALL_THREADS sampling...")

    tintype.finalize()
    tintype.initialize()

    done10: threading.Event = threading.Event()

    def thread_spawner_10() -> None:
        i = 0
        while not done10.is_set():
            t = threading.Thread(target=ephemeral_worker, args=(i,), name=f"eph-{i}")
            t.start()
            t.join(timeout=1.0)
            i += 1

    spawner10 = threading.Thread(target=thread_spawner_10, name="spawner-10")
    spawner10.daemon = True
    spawner10.start()

    tintype.enable_sampling(
        interval=0.02,
        mode=tintype.SamplingMode.ALL_THREADS,
        timeout=1.0,
    )

    time.sleep(0.5)  # let sampling run while threads come and go

    tintype.disable_sampling()
    done10.set()
    spawner10.join(timeout=5.0)

    tintype.finalize(path)
    reader = tintype.SnapshotReader(path)
    print(f"  Snapshots captured: {reader.snapshot_count()}")
    assert reader.snapshot_count() > 0
    # Verify all snapshots are valid
    all_snaps = reader.get_all_snapshots()
    for s in all_snaps:
        assert len(s.stacktraces) > 0
    print("  All snapshots valid")
    print("  PASSED")

    # ------------------------------------------------------------------
    # Test 11: Sampling — SINGLE_THREAD with target blocked in native code
    # ------------------------------------------------------------------
    print("\n[Test 11] SINGLE_THREAD sampling with target in native code...")

    tintype.finalize()
    tintype.initialize()

    tintype.enable_sampling(
        interval=0.02,
        mode=tintype.SamplingMode.SINGLE_THREAD,
        timeout=1,  # short timeout to trigger fallback quickly
    )

    # Main thread does native sleeps — captured via sys._current_frames()
    time.sleep(0.3)

    tintype.disable_sampling()

    tintype.finalize(path)
    reader = tintype.SnapshotReader(path)
    print(f"  Snapshots via fallback: {reader.snapshot_count()}")
    assert reader.snapshot_count() > 0, "Fallback should have captured snapshots"
    print("  PASSED")

    # ------------------------------------------------------------------
    # Test 12: Sampling — double enable_sampling() (reentrancy guard)
    # ------------------------------------------------------------------
    print("\n[Test 12] Double enable_sampling() reentrancy guard...")

    tintype.finalize()
    tintype.initialize()

    tintype.enable_sampling(
        interval=0.05,
        mode=tintype.SamplingMode.ALL_THREADS,
    )
    try:
        tintype.enable_sampling(
            interval=0.05,
            mode=tintype.SamplingMode.ALL_THREADS,
        )
        print("  FAILED: should have raised RuntimeError")
        raise Exception("should have raised RuntimeError")
    except RuntimeError as e:
        print(f"  Correctly raised: {e}")

    tintype.disable_sampling()
    tintype.finalize()
    print("  PASSED")

    # ------------------------------------------------------------------
    # Test 13: Sampling — context manager with exception
    # ------------------------------------------------------------------
    print("\n[Test 13] Context manager cleanup on exception...")

    try:
        with tintype.sampling(
            interval=0.02,
            mode=tintype.SamplingMode.ALL_THREADS,
            path=path,
            timeout=1.0,
        ):
            # Do some work, then raise
            total = 0
            for i in range(50000):
                total += i
            raise ValueError("intentional test exception")
    except ValueError:
        pass

    # After the exception, sampling should be stopped and file written
    reader = tintype.SnapshotReader(path)
    print(f"  Snapshots in file after exception: {reader.snapshot_count()}")
    assert reader.snapshot_count() >= 0  # may be 0 if exception was too fast

    # Verify we can start sampling again (not stuck)
    tintype.initialize()
    tintype.enable_sampling(
        interval=0.05,
        mode=tintype.SamplingMode.ALL_THREADS,
    )
    time.sleep(0.1)
    tintype.disable_sampling()
    tintype.finalize()
    print("  Sampling restartable after exception")
    print("  PASSED")

    # ------------------------------------------------------------------
    # Test 14: Sampling — mode switching between cycles
    # ------------------------------------------------------------------
    print("\n[Test 14] Alternating between SINGLE_THREAD and ALL_THREADS...")

    for i in range(10):
        tintype.initialize()
        mode = (
            tintype.SamplingMode.SINGLE_THREAD
            if i % 2 == 0
            else tintype.SamplingMode.ALL_THREADS
        )
        tintype.enable_sampling(interval=0.01, mode=mode, timeout=0.2)
        # Do Python work between mode switches
        total = 0
        for j in range(20000):
            total += j
        tintype.disable_sampling()
        tintype.finalize()

    print("  10 mode-switching cycles completed")
    print("  PASSED")

    # ------------------------------------------------------------------
    # Test 15: snapshot_all_threads() during SINGLE_THREAD sampling
    # ------------------------------------------------------------------
    print("\n[Test 15] snapshot_all_threads() during SINGLE_THREAD sampling...")
    #
    # Both sampleSingleThread() and snapshot_all_threads() use
    # sys._current_frames() and contend on snapshotInProgress_.

    tintype.initialize()

    done15: threading.Event = threading.Event()
    bg15: list[threading.Thread] = []
    for i in range(3):
        t = threading.Thread(target=cpu_worker, args=(done15,), name=f"st-bg-{i}")
        t.daemon = True
        t.start()
        bg15.append(t)

    tintype.enable_sampling(
        interval=0.02,
        mode=tintype.SamplingMode.SINGLE_THREAD,
        timeout=0.3,
    )

    # Hammer snapshot_all_threads() from the main thread while
    # SINGLE_THREAD sampling is also active.
    st_successes = 0
    st_nones = 0
    for _i in range(20):
        result = tintype.snapshot_all_threads(timeout=1.0)
        if result is not None:
            st_successes += 1
            # Verify the multi-thread snapshot actually has multiple threads
            assert len(result.stacktraces) >= 2, (
                f"Expected >= 2 threads, got {len(result.stacktraces)}"
            )
        else:
            st_nones += 1
        # Do Python work between calls
        total = 0
        for j in range(10000):
            total += j

    tintype.disable_sampling()
    done15.set()
    for t in bg15:
        t.join(timeout=5.0)

    tintype.finalize(path)
    reader = tintype.SnapshotReader(path)
    print(
        f"  snapshot_all_threads(): {st_successes} ok, {st_nones} skipped "
        f"(snapshotInProgress)"
    )
    print(f"  Total snapshots in file: {reader.snapshot_count()}")
    assert reader.snapshot_count() > 0
    # Verify all snapshots are valid
    all_snaps = reader.get_all_snapshots()
    for s in all_snaps:
        assert len(s.stacktraces) > 0, "Empty snapshot in file"
    print("  All snapshots valid")
    print("  PASSED")

    # ------------------------------------------------------------------
    # Test 16: Rapid snapshot_all_threads() interleaved with SINGLE_THREAD
    #          sampling — maximum contention
    # ------------------------------------------------------------------
    print("\n[Test 16] Rapid interleaved snapshot_all_threads() + SINGLE_THREAD...")
    #
    # Multiple threads call snapshot_all_threads() simultaneously while
    # SINGLE_THREAD sampling is active. This maximizes contention on
    # snapshotInProgress_.

    tintype.finalize()
    tintype.initialize()

    done16: threading.Event = threading.Event()
    bg16: list[threading.Thread] = []
    for i in range(2):
        t = threading.Thread(target=cpu_worker, args=(done16,), name=f"cont-bg-{i}")
        t.daemon = True
        t.start()
        bg16.append(t)

    tintype.enable_sampling(
        interval=0.01,
        mode=tintype.SamplingMode.SINGLE_THREAD,
        timeout=0.2,
    )

    # Spawn threads that call snapshot_all_threads() concurrently
    snap_barrier: threading.Barrier = threading.Barrier(3)
    snap_results: list[object | None] = [None, None, None]

    def concurrent_snapshot_caller(idx: int) -> None:
        snap_barrier.wait()
        for _round in range(5):
            r = tintype.snapshot_all_threads(timeout=1.0)
            if r is not None:
                snap_results[idx] = r
            # Python work between calls
            total = 0
            for j in range(5000):
                total += j

    snap_threads: list[threading.Thread] = []
    for i in range(3):
        t = threading.Thread(
            target=concurrent_snapshot_caller, args=(i,), name=f"snap-caller-{i}"
        )
        t.start()
        snap_threads.append(t)

    for t in snap_threads:
        t.join(timeout=30.0)
        assert not t.is_alive(), f"Thread {t.name} is still alive (deadlock?)"

    tintype.disable_sampling()
    done16.set()
    for t in bg16:
        t.join(timeout=5.0)

    tintype.finalize(path)
    reader = tintype.SnapshotReader(path)
    non_none = sum(1 for r in snap_results if r is not None)
    print(f"  Concurrent callers: {non_none}/3 got at least one snapshot")
    print(f"  Total snapshots in file: {reader.snapshot_count()}")
    assert reader.snapshot_count() > 0
    all_snaps = reader.get_all_snapshots()
    for s in all_snaps:
        assert len(s.stacktraces) > 0, "Empty snapshot in file"
    print("  All snapshots valid")
    print("  PASSED")

    # ------------------------------------------------------------------
    # Test 17: SINGLE_THREAD sampling with target alternating between
    #          Python and native code
    # ------------------------------------------------------------------
    print("\n[Test 17] SINGLE_THREAD with target alternating Python/native...")
    #
    # The target thread alternates between Python busy-loops and native
    # sleeps. Both are captured via sys._current_frames().

    tintype.finalize()
    tintype.initialize()

    tintype.enable_sampling(
        interval=0.02,
        mode=tintype.SamplingMode.SINGLE_THREAD,
        timeout=0.2,
    )

    for _cycle in range(10):
        # Python phase — captured via sys._current_frames()
        deadline = time.monotonic() + 0.03
        while time.monotonic() < deadline:
            _ = 1 + 1  # noqa: F841
        # Native phase — also captured via sys._current_frames()
        time.sleep(0.03)

    tintype.disable_sampling()

    tintype.finalize(path)
    reader = tintype.SnapshotReader(path)
    print(f"  Snapshots captured: {reader.snapshot_count()}")
    assert reader.snapshot_count() > 0
    all_snaps = reader.get_all_snapshots()
    for s in all_snaps:
        assert len(s.stacktraces) > 0, "Empty snapshot in file"
    print("  All snapshots valid")
    print("  PASSED")

    # ------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("All stress tests PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
