# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Generate snapshots that demonstrate truncated stacktraces.

Produces a snapshot file with:
  - Snapshot 1: A deep call stack captured with max_frames=3 (truncated thread)
  - Snapshot 2: An exception chain where the deepest cause is truncated
  - Snapshot 3: A full (non-truncated) snapshot for comparison

Usage:
    python -m tintype.demo.tintype_truncated_demo
    python -m tintype.utils.tintype_viewer /tmp/truncated_demo.pytb
    python -m tintype.utils.tintype_dump /tmp/truncated_demo.pytb
"""

import os
import sys
import time

import tintype


# ---------------------------------------------------------------------------
# Deep call stack helpers
# ---------------------------------------------------------------------------


def level_5() -> None:
    x = "deepest frame"  # noqa
    tintype.take_snapshot(max_frames=3)


def level_4() -> None:
    d = {"key": "value"}  # noqa
    level_5()


def level_3() -> None:
    items = [1, 2, 3]  # noqa
    level_4()


def level_2() -> None:
    counter = 42  # noqa
    level_3()


def level_1() -> None:
    message = "hello from level_1"  # noqa
    level_2()


# ---------------------------------------------------------------------------
# Exception chain helpers
#
# Each exception's traceback needs 4+ frames so that max_frames=2 actually
# triggers truncation.  Python tracebacks only span from the raise site up
# to the enclosing try/except, so we add intermediate calls within each
# try-block scope to deepen the stacks.
# ---------------------------------------------------------------------------


def _io_raise() -> None:
    path = "/nonexistent/file.txt"  # noqa
    raise IOError("disk read failed")


def _io_inner() -> None:
    retries = 3  # noqa
    _io_raise()


def _io_middle() -> None:
    timeout = 30  # noqa
    _io_inner()


def _io_outer() -> None:
    host = "db.example.com"  # noqa
    _io_middle()


def _conn_raise(cause: Exception) -> None:
    msg = "storage backend unavailable"  # noqa
    raise ConnectionError(msg) from cause


def _conn_inner(cause: Exception) -> None:
    pool_size = 10  # noqa
    _conn_raise(cause)


def _conn_middle(cause: Exception) -> None:
    region = "us-east-1"  # noqa
    _conn_inner(cause)


def _conn_outer(cause: Exception) -> None:
    cluster = "primary"  # noqa
    _conn_middle(cause)


def service_call() -> None:
    try:
        try:
            _io_outer()
        except IOError as e:
            _conn_outer(e)
    except ConnectionError as e:
        raise RuntimeError("service request failed") from e


# ---------------------------------------------------------------------------
# Full (non-truncated) snapshot for comparison
# ---------------------------------------------------------------------------


def inner() -> None:
    value = 99  # noqa
    tintype.take_snapshot()


def outer() -> None:
    name = "full snapshot"  # noqa
    inner()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

OUTPUT_PATH = "/tmp/truncated_demo.pytb"


def main() -> None:
    tintype.initialize()

    # Snapshot 1: truncated thread stacktrace (only 3 of 6+ frames kept)
    level_1()

    # Snapshot 2: exception chain with truncated deepest cause
    try:
        service_call()
    except RuntimeError as exc:
        tintype.take_snapshot(exc, max_frames=2)

    # Snapshot 3: full (non-truncated) snapshot for comparison
    outer()

    tintype.finalize(
        OUTPUT_PATH,
        metadata={
            "demo": "truncated_demo",
            "timestamp": time.time(),
            "pid": os.getpid(),
            "python_version": sys.version,
        },
    )
    print(f"Snapshot written to {OUTPUT_PATH}", file=sys.stderr)
    print(
        f"View with: python -m tintype.utils.tintype_viewer {OUTPUT_PATH}",
        file=sys.stderr,
    )
