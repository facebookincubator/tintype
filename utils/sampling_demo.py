# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Demo script that samples all threads at a configurable interval.

Starts several worker threads doing busy work, enables tintype all-threads
sampling for a specified duration, then prints the path to the working
snapshot file so it can be inspected with tintype_dump or the debug launcher.

Usage:
    python -m tintype.utils.sampling_demo --interval 0.1 --duration 5
"""

from __future__ import annotations

import argparse
import math
import threading
import time

import tintype


def _fibonacci_worker(stop: threading.Event) -> None:
    """Compute Fibonacci numbers until stopped."""
    a, b = 0, 1
    while not stop.is_set():
        a, b = b, a + b
        if a > 10**15:
            a, b = 0, 1


def _prime_sieve_worker(stop: threading.Event) -> None:
    """Sieve primes repeatedly until stopped."""
    while not stop.is_set():
        limit = 10000
        is_prime = [True] * limit
        is_prime[0] = is_prime[1] = False
        for i in range(2, int(math.sqrt(limit)) + 1):
            if is_prime[i]:
                for j in range(i * i, limit, i):
                    is_prime[j] = False


def _string_worker(stop: threading.Event) -> None:
    """Build and tear down strings until stopped."""
    while not stop.is_set():
        parts = [f"item-{i}" for i in range(500)]
        _ = ",".join(parts)


def _sleepy_worker(stop: threading.Event) -> None:
    """Sleep in short bursts until stopped."""
    while not stop.is_set():
        time.sleep(0.05)


def main() -> None:
    parser = argparse.ArgumentParser(description="Tintype all-threads sampling demo")
    parser.add_argument(
        "--interval",
        type=float,
        default=0.1,
        help="Sampling interval in seconds (default: 0.1)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=5.0,
        help="How long to sample in seconds (default: 5.0)",
    )
    args = parser.parse_args()

    reader = tintype.initialize()
    working_path = reader.get_working_file_path()
    print(f"Working file: {working_path}")

    stop = threading.Event()
    workers = [
        threading.Thread(target=_fibonacci_worker, args=(stop,), name="fibonacci"),
        threading.Thread(target=_prime_sieve_worker, args=(stop,), name="prime-sieve"),
        threading.Thread(target=_string_worker, args=(stop,), name="string-builder"),
        threading.Thread(target=_sleepy_worker, args=(stop,), name="sleepy"),
    ]
    for t in workers:
        t.start()

    print(f"Sampling all threads every {args.interval}s for {args.duration}s ...")
    tintype.enable_sampling(args.interval, tintype.SamplingMode.ALL_THREADS)

    time.sleep(args.duration)

    tintype.disable_sampling()
    stop.set()
    for t in workers:
        t.join()

    count = reader.snapshot_count()
    tintype.finalize()

    print(f"Captured {count} snapshot(s)")
    print(f"\nTo inspect, run:\n  python -m tintype.utils.tintype_dump {working_path}")


if __name__ == "__main__":
    main()
