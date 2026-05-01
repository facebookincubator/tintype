# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Demo of snapshot_all_threads(): captures every Python thread's stack in one snapshot."""

import os
import tempfile
import threading
import time

import tintype


def database_query(query: str) -> None:
    """Simulate a slow database query."""
    time.sleep(30)


def process_request(request_id: int) -> None:
    """Simulate processing an HTTP request."""
    result = 0
    for i in range(10**8):
        result += i
        if i % 10**7 == 0:
            pass


def background_worker(queue: list[str]) -> None:
    """Simulate a background task consumer."""
    while True:
        if queue:
            item = queue.pop(0)
            _ = item.upper()
        else:
            time.sleep(0.001)


def main() -> None:
    path = os.path.join(tempfile.gettempdir(), "all_threads.pytb")
    tintype.initialize()

    # Start threads doing different things
    threads = []

    # Thread doing CPU work (will be caught by sys._current_frames())
    t1 = threading.Thread(target=process_request, args=(42,), name="request-handler")
    t1.daemon = True
    t1.start()
    threads.append(t1)

    # Thread blocked in native sleep (will be caught by fallback)
    t2 = threading.Thread(
        target=database_query, args=("SELECT * FROM users",), name="db-query"
    )
    t2.daemon = True
    t2.start()
    threads.append(t2)

    # Thread in a mixed loop
    queue: list[str] = ["task1", "task2", "task3"]
    t3 = threading.Thread(target=background_worker, args=(queue,), name="bg-worker")
    t3.daemon = True
    t3.start()
    threads.append(t3)

    # Give threads a moment to start
    time.sleep(0.1)

    # Capture all threads
    print("Capturing all threads...")
    snap = tintype.snapshot_all_threads(timeout=3.0)
    print()

    if snap is None:
        print("ERROR: snapshot_all_threads returned None")
        return

    print(f"Captured {len(snap.stacktraces)} thread(s):\n")

    for thread_id, st in snap.stacktraces.items():
        # Try to find the thread name
        name = "unknown"
        for t in threading.enumerate():
            if t.ident == thread_id:
                name = t.name
                break
        if thread_id == threading.current_thread().ident:
            name = "MainThread"

        print(f"--- Thread {thread_id} ({name}) ---")
        for frame in st.frames:
            print(f"  {frame.file_path}:{frame.line_number} in {frame.function_name}")
        print()

    tintype.finalize(path)
    print(f"Snapshot saved to: {path}")

    # Verify it's readable
    reader = tintype.SnapshotReader(path)
    print(f"Verified: {reader.snapshot_count()} snapshot(s) in file")


if __name__ == "__main__":
    main()
