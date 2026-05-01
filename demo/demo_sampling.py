# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Demo of periodic sampling: captures thread stacks at regular intervals.

Shows both ALL_THREADS and SINGLE_THREAD modes, including the context manager.

Run with: python -m tintype.demo.demo_sampling
"""

import os
import tempfile
import threading
import time

import tintype


def fibonacci(n: int) -> int:
    """Compute fibonacci recursively (intentionally slow)."""
    if n <= 1:
        return n
    return fibonacci(n - 1) + fibonacci(n - 2)


def matrix_multiply(size: int) -> list[list[float]]:
    """Simulate a matrix multiplication."""
    result = [[0.0] * size for _ in range(size)]
    for i in range(size):
        for j in range(size):
            for k in range(size):
                result[i][j] += (i + k) * (k + j)
    return result


def io_bound_work(done: threading.Event) -> None:
    """Simulate I/O-bound work (blocked in native code)."""
    while not done.is_set():
        done.wait(timeout=0.5)


def cpu_bound_work(done: threading.Event) -> None:
    """Simulate CPU-bound work (executing Python code)."""
    while not done.is_set():
        _ = fibonacci(20)


def print_snapshot_summary(reader: tintype.SnapshotReader) -> None:
    """Print a summary of all snapshots in a file."""
    count = reader.snapshot_count()
    print(f"  Total snapshots: {count}")

    if count == 0:
        return

    for i in range(min(count, 3)):
        snap = reader.get_snapshot_at_index(i)
        if snap is None:
            continue
        thread_count = len(snap.stacktraces)
        print(f"  Snapshot {i}: {thread_count} thread(s)")

    if count > 3:
        print(f"  ... and {count - 3} more")

    # Show details of the latest snapshot
    latest = reader.get_latest_snapshot()
    if latest is not None:
        print(f"\n  Latest snapshot ({len(latest.stacktraces)} threads):")
        for thread_id, st in latest.stacktraces.items():
            name = "unknown"
            for t in threading.enumerate():
                if t.ident == thread_id:
                    name = t.name
                    break
            if thread_id == threading.current_thread().ident:
                name = "MainThread"

            top_frame = st.frames[0] if st.frames else None
            if top_frame:
                loc = f"{top_frame.function_name} ({os.path.basename(top_frame.file_path)}:{top_frame.line_number})"
            else:
                loc = "(no frames)"
            print(f"    Thread {name}: {len(st.frames)} frames, top: {loc}")


def demo_all_threads_sampling() -> None:
    """Demo 1: ALL_THREADS mode — sample every thread periodically."""
    print("=" * 60)
    print("Demo 1: ALL_THREADS sampling")
    print("=" * 60)
    print()
    print("Sampling all threads every 100ms for 1 second...")
    print("  - 2 CPU-bound threads (Python code, caught by sys._current_frames())")
    print("  - 1 I/O-bound thread (native code, caught by fallback)")
    print()

    path = os.path.join(tempfile.gettempdir(), "sampling_all_threads.pytb")
    done = threading.Event()

    # Start worker threads
    t1 = threading.Thread(target=cpu_bound_work, args=(done,), name="cpu-worker-1")
    t2 = threading.Thread(target=cpu_bound_work, args=(done,), name="cpu-worker-2")
    t3 = threading.Thread(target=io_bound_work, args=(done,), name="io-worker")
    for t in (t1, t2, t3):
        t.daemon = True
        t.start()

    # Use the context manager for clean lifecycle
    with tintype.sampling(
        interval=0.1,
        mode=tintype.SamplingMode.ALL_THREADS,
        path=path,
        timeout=2.0,
    ):
        time.sleep(1.0)

    done.set()
    for t in (t1, t2, t3):
        t.join(timeout=2.0)

    print(f"Results saved to: {path}")
    reader = tintype.SnapshotReader(path)
    print_snapshot_summary(reader)
    print()


def demo_single_thread_sampling() -> None:
    """Demo 2: SINGLE_THREAD mode — sample only the calling thread."""
    print("=" * 60)
    print("Demo 2: SINGLE_THREAD sampling")
    print("=" * 60)
    print()
    print("Sampling the main thread every 50ms while computing fibonacci...")
    print()

    path = os.path.join(tempfile.gettempdir(), "sampling_single_thread.pytb")

    with tintype.sampling(
        interval=0.05,
        mode=tintype.SamplingMode.SINGLE_THREAD,
        path=path,
        timeout=2.0,
    ):
        # Do CPU-bound work on the main thread
        for n in range(28, 33):
            result = fibonacci(n)
            print(f"  fibonacci({n}) = {result}")

    print()
    print(f"Results saved to: {path}")
    reader = tintype.SnapshotReader(path)
    print_snapshot_summary(reader)
    print()


def demo_manual_api() -> None:
    """Demo 3: Manual enable/disable API (without context manager)."""
    print("=" * 60)
    print("Demo 3: Manual enable/disable API")
    print("=" * 60)
    print()
    print("Using enable_sampling() / disable_sampling() directly...")
    print()

    path = os.path.join(tempfile.gettempdir(), "sampling_manual.pytb")

    tintype.initialize()
    tintype.enable_sampling(
        interval=0.05,
        mode=tintype.SamplingMode.ALL_THREADS,
        timeout=2.0,
    )

    # Simulate a workload
    print("  Running matrix multiplication (150x150)...")
    matrix_multiply(150)
    print("  Done.")

    tintype.disable_sampling()
    tintype.finalize(path)

    print()
    print(f"Results saved to: {path}")
    reader = tintype.SnapshotReader(path)
    print_snapshot_summary(reader)
    print()


def main() -> None:
    print()
    print("Tintype Periodic Sampling Demo")
    print("==============================")
    print()

    demo_all_threads_sampling()
    demo_single_thread_sampling()
    demo_manual_api()

    print("All demos complete!")
    print()


if __name__ == "__main__":
    main()
