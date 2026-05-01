# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Debug launcher for tintype snapshot files.

Launches a VS Code debug session from a .pytb snapshot file. Designed to be
run as the ``program`` in a ``meta-python`` launch configuration, where debugpy
acts as the debug adapter.

For each stacktrace in the snapshot, the script reconstructs real Python
traceback frames (with evaluable locals) and suspends the debugger so VS Code
can display the call stack.  When a snapshot contains multiple stacktraces
(e.g. from ``snapshot_all_threads()``), a dedicated worker thread is spawned
for each one so that VS Code's CALL STACK panel shows all threads
simultaneously.

Because the launcher is built as a PAR (Python ARchive), the launch
configuration must set ``"client": "buck"`` so that debugpy uses the
PAR-aware launch path (``prepare_debugger_for_par``) instead of
``runpy.run_path()``, which cannot compile binary PAR files.

Usage in launch.json::

    {
        "type": "meta-python",
        "request": "launch",
        "name": "Debug Tintype Snapshot",
        "program": "<path to debug_launcher PAR>",
        "args": ["/path/to/snapshot.pytb"],
        "client": "buck",
        "justMyCode": false
    }
"""

from __future__ import annotations

import argparse
import datetime
import sys
import threading
from importlib import import_module
from types import ModuleType, TracebackType
from typing import Any

from tintype import Snapshot, SnapshotReader, Stacktrace

_INTERNAL_PATH_MARKERS = (
    "/tintype_debug_launcher.py",
    "/snapshot_live.py",
    "_pydevd_bundle/",
    "_pydevd_sys_monitoring/",
    "pydevd.py",
    "debugpy",
)

_step_back_requested: bool = False


def _request_step_back() -> str:
    """Set the step-back flag.

    Called via a DAP ``evaluate`` request from the VS Code extension when
    the user presses Step Back.  The flag is checked by the coordinator
    after workers return from ``do_wait_suspend``.

    Can also be called manually from the VS Code debug console::

        __import__('sys').modules['tintype.utils.tintype_debug_launcher']._request_step_back()
    """
    global _step_back_requested
    _step_back_requested = True
    return "Stepping back to previous snapshot"


def _import_pydevd() -> tuple[
    ModuleType, ModuleType, ModuleType | None, ModuleType, ModuleType
]:
    """Import pydevd modules injected by debugpy into the process.

    Returns:
        Tuple of ``(pydevd, pydevd_comm_constants, pydevd_sys_monitoring | None,
        pydevd_thread_lifecycle, pydevd_constants)``.

    Raises:
        ImportError: If pydevd is not available (not running under debugpy).
    """
    pydevd = import_module("pydevd")
    pydevd_comm_constants = import_module("_pydevd_bundle.pydevd_comm_constants")

    pydevd_sys_monitoring = None
    if pydevd.PYDEVD_USE_SYS_MONITORING:
        try:
            pydevd_sys_monitoring = import_module(
                "_pydevd_sys_monitoring._pydevd_sys_monitoring"
            )
        except ImportError:
            pass

    pydevd_thread_lifecycle = import_module("_pydevd_bundle.pydevd_thread_lifecycle")
    pydevd_constants = import_module("_pydevd_bundle.pydevd_constants")

    return (
        pydevd,
        pydevd_comm_constants,
        pydevd_sys_monitoring,
        pydevd_thread_lifecycle,
        pydevd_constants,
    )


def _reconstruct_exception(
    stacktrace: Stacktrace,
) -> tuple[type[BaseException], BaseException, TracebackType] | None:
    """Reconstruct an exception with its traceback and chained causes.

    Walks the ``__cause__`` / ``__context__`` chain on the stacktrace and
    mirrors it onto synthetic ``Exception`` objects, each carrying the
    reconstructed traceback from ``get_traceback()``.

    Follows the same chain-reconstruction pattern used internally.
    """
    tb = stacktrace.get_traceback()
    if tb is None:
        return None

    orig_exc = stacktrace.exception_object
    if orig_exc is None:
        return None

    exc = Exception(str(orig_exc))
    exc.__traceback__ = tb

    current_st = stacktrace
    current_exc: BaseException = exc
    while True:
        if cause := current_st.get_cause():
            cause_tb = cause.get_traceback()
            cause_msg = (
                str(cause.exception_object)
                if cause.exception_object
                else "(chained cause)"
            )
            cause_exc = Exception(cause_msg)
            if cause_tb is not None:
                cause_exc.__traceback__ = cause_tb
            current_exc.__cause__ = cause_exc
            current_st = cause
            current_exc = cause_exc
        elif context := current_st.get_context():
            context_tb = context.get_traceback()
            context_msg = (
                str(context.exception_object)
                if context.exception_object
                else "(chained context)"
            )
            context_exc = Exception(context_msg)
            if context_tb is not None:
                context_exc.__traceback__ = context_tb
            current_exc.__context__ = context_exc
            current_st = context
            current_exc = context_exc
        else:
            break

    return (type(exc), exc, tb)


def _find_innermost_user_frame(tb: TracebackType) -> Any | None:
    """Walk the traceback chain and return the innermost non-internal frame."""
    top_frame = None
    current_tb: TracebackType | None = tb
    while current_tb is not None:
        filename = current_tb.tb_frame.f_code.co_filename
        if not any(marker in filename for marker in _INTERNAL_PATH_MARKERS):
            top_frame = current_tb.tb_frame
        current_tb = current_tb.tb_next
    return top_frame


def _worker_show_stacktrace(
    stacktrace: Stacktrace,
    done_event: threading.Event,
    pydevd: ModuleType,
    pydevd_comm_constants: ModuleType,
    pydevd_sys_monitoring: ModuleType | None,
) -> None:
    """Worker thread target: reconstruct frames and suspend the debugger."""
    py_db = pydevd.get_global_debugger()
    if py_db is None:
        done_event.set()
        return

    CMD_SET_BREAK: int = pydevd_comm_constants.CMD_SET_BREAK
    CMD_ADD_EXCEPTION_BREAK: int = pydevd_comm_constants.CMD_ADD_EXCEPTION_BREAK

    thread = threading.current_thread()

    # Handle sys.monitoring for Python >= 3.12.
    start_monitoring = None
    stop_monitoring = None
    is_monitoring_current_thread = False

    if pydevd_sys_monitoring is not None:
        if hasattr(pydevd_sys_monitoring, "is_monitoring_current_thread"):
            is_monitoring_current_thread = (
                pydevd_sys_monitoring.is_monitoring_current_thread()
            )
        start_monitoring = pydevd_sys_monitoring.start_monitoring
        stop_monitoring = pydevd_sys_monitoring.stop_monitoring

        # ensure that we have the thread info, if we don't,
        # disable monitoring for the thread, is just a no-op and won't work.
        pydevd_sys_monitoring._get_thread_info(True, 1)

    additional_thread_info = py_db.set_additional_thread_info(thread)

    try:
        # Suppress tracing while exec builds real frames.
        additional_thread_info.is_tracing += 1
        if stop_monitoring is not None:
            stop_monitoring(all_threads=False)

        # Build tracebacks while monitoring is disabled for this thread.
        if stacktrace.exception_object is not None:
            exc_info = _reconstruct_exception(stacktrace)
            if exc_info is None:
                done_event.set()
                return
            _, _, tb = exc_info
            top_frame = _find_innermost_user_frame(tb)
            if top_frame is None:
                done_event.set()
                return
        else:
            tb = stacktrace.get_traceback()
            if tb is None:
                done_event.set()
                return
            top_frame = _find_innermost_user_frame(tb)
            if top_frame is None:
                done_event.set()
                return
            exc_info = None

        suspend_cmd = (
            CMD_ADD_EXCEPTION_BREAK
            if stacktrace.exception_object is not None
            else CMD_SET_BREAK
        )
        # Issue the stopped event with reason "breakpoint" or "exception"
        # This function will automatically trigger suspend_other_threads = true
        # because multi_threads_single_notification = True
        py_db.set_suspend(thread, suspend_cmd)

        if exc_info is not None:
            py_db.do_wait_suspend(thread, top_frame, "exception", exc_info)
        else:
            py_db.do_wait_suspend(thread, top_frame, "snapshot", None)
    finally:
        additional_thread_info.is_tracing -= 1
        if start_monitoring is not None and is_monitoring_current_thread:
            start_monitoring(all_threads=False)
        done_event.set()


def _describe_stacktrace(stacktrace: Stacktrace) -> str:
    """Return a human-readable one-line description of a stacktrace."""
    if stacktrace.exception_object is not None:
        return str(stacktrace.exception_object)
    if stacktrace.thread_name:
        return stacktrace.thread_name
    return f"Thread: {stacktrace.id}"


def _format_snapshot_header(snapshot: Snapshot, index: int, total: int) -> str:
    """Format a header line for a snapshot being debugged."""
    ts = snapshot.timestamp
    dt = datetime.datetime.fromtimestamp(ts / 1_000_000)
    label = dt.strftime("%Y-%m-%d %H:%M:%S")
    st_count = len(snapshot.stacktraces)
    return f"Snapshot {index}/{total} (captured {label}, {st_count} stacktrace(s))"


def _debug_snapshot(
    snapshot: Snapshot,
    pydevd: ModuleType,
    pydevd_comm_constants: ModuleType,
    pydevd_sys_monitoring: ModuleType | None,
    pydevd_thread_lifecycle: ModuleType,
    pydevd_constants: ModuleType,
) -> None:
    """Debug all stacktraces in a single snapshot.

    Spawns one worker thread per stacktrace so VS Code shows all threads
    simultaneously in the CALL STACK panel.  The main thread acts as a
    coordinator: it starts the workers, waits for any worker to be resumed
    by the user, then programmatically resumes the remaining workers before
    proceeding to the next snapshot.
    """
    stacktraces = list(snapshot.stacktraces.values())
    if not stacktraces:
        print("  No stacktraces in snapshot", file=sys.stderr)
        return

    # Filter to stacktraces that have frames.
    valid: list[tuple[Stacktrace, str]] = []
    for st in stacktraces:
        if not st.frames:
            continue
        valid.append((st, _describe_stacktrace(st)))

    if not valid:
        print("  No displayable stacktraces in snapshot", file=sys.stderr)
        return

    total = len(valid)
    for i, (_, desc) in enumerate(valid, 1):
        print(f"  [{i}/{total}] {desc}")

    # Compute a timestamp prefix for thread names in VS Code's CALL STACK.
    dt = datetime.datetime.fromtimestamp(snapshot.timestamp / 1_000_000)
    ts_label = dt.strftime("%H:%M:%S")

    # Spawn phase — each worker builds its own frames and suspends independently.
    done_event = threading.Event()
    workers: list[threading.Thread] = []
    for st, desc in valid:
        t = threading.Thread(
            target=_worker_show_stacktrace,
            name=f"{ts_label} — {desc}",
            args=(st, done_event, pydevd, pydevd_comm_constants, pydevd_sys_monitoring),
        )
        # set pydev_do_not_trace so that other threads don't suspend this thread
        # with suspend_all_threads call.
        t.pydev_do_not_trace = True  # pyre-ignore[16]
        t.start()
        workers.append(t)

    # Wait phase — blocks until any worker returns from do_wait_suspend
    done_event.wait()

    # Cleanup phase — resume any workers still suspended, then join all
    for t in workers:
        if t.is_alive():
            pydevd_thread_lifecycle.resume_threads(pydevd_constants.get_thread_id(t))
    for t in workers:
        t.join()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Debug a tintype snapshot file (.pytb) in VS Code.",
    )
    parser.add_argument(
        "pytb_file",
        help="Path to the .pytb snapshot file.",
    )
    parser.add_argument(
        "--snapshot-index",
        type=int,
        default=None,
        help=(
            "Start at this snapshot index. "
            "Supports negative indices (-1 = last). Default: 0."
        ),
    )
    # parse_known_args tolerates debugpy-related flags (--connect, --par,
    # etc.) that leak into sys.argv when running as a PAR with client:"buck".
    args, _unknown = parser.parse_known_args()
    return args


def run(args: argparse.Namespace) -> int:
    """Run the debug launcher with the given parsed arguments.

    On success the function enters an infinite loop that cycles through
    snapshots, suspending in ``do_wait_suspend`` after each one.  It
    never returns — the process is terminated externally when the user
    presses Stop in VS Code.

    Returns:
        1 on error (import failure, no snapshots, etc.).
        2 when the file is not a tintype snapshot (``SnapshotReader``
          raised ``RuntimeError``).
    """
    try:
        reader = SnapshotReader(args.pytb_file)
    except RuntimeError:
        return 2

    try:
        (
            pydevd,
            pydevd_comm_constants,
            pydevd_sys_monitoring,
            pydevd_thread_lifecycle,
            pydevd_constants,
        ) = _import_pydevd()
    except ImportError as e:
        print(
            f"Failed to import pydevd: {e}\n"
            "This script must be launched via a VS Code debug configuration "
            "(type: meta-python, request: launch).",
            file=sys.stderr,
        )
        return 1

    # Hide the main thread from VS Code's CALL STACK panel so only
    # worker threads (one per stacktrace) are visible.  We also register
    # it with pydevd so it doesn't create a _DummyThread wrapper (which
    # would appear as "Dummy-N" in the thread list).
    main_thread = threading.current_thread()
    main_thread.is_pydev_daemon_thread = True  # pyre-ignore[16]
    main_thread.pydev_do_not_trace = True  # pyre-ignore[16]
    py_db = pydevd.get_global_debugger()
    if py_db is not None:
        py_db.set_additional_thread_info(main_thread)

    total = reader.snapshot_count()
    if total == 0:
        print("No snapshots found in file", file=sys.stderr)
        return 1

    # Resolve start index (supports negative indices like Python lists).
    if args.snapshot_index is not None:
        idx = args.snapshot_index
        if idx < 0:
            idx = total + idx
        start_index = max(0, min(idx, total - 1))
    else:
        start_index = 0

    snap = reader.get_snapshot_at_index(start_index)
    if snap is None:
        print("Failed to read snapshot", file=sys.stderr)
        return 1

    print(f"Debugging {total} snapshot(s) from {args.pytb_file}\n")

    snap_idx = start_index
    while True:
        total = reader.snapshot_count()
        print(_format_snapshot_header(snap, snap_idx + 1, total))
        _debug_snapshot(
            snap,
            pydevd,
            pydevd_comm_constants,
            pydevd_sys_monitoring,
            pydevd_thread_lifecycle,
            pydevd_constants,
        )

        global _step_back_requested
        if _step_back_requested:
            _step_back_requested = False
            prev = snap.get_prev_snapshot()
            if prev is not None:
                snap = prev
                snap_idx -= 1
        else:
            nxt = snap.get_next_snapshot()
            if nxt is not None:
                snap = nxt
                snap_idx += 1
            else:
                print("Last snapshot reached. Use Stop to end the debug session.")


def main() -> int:
    """Entry point for standalone use (without Meta-internal fallback)."""
    args = _parse_args()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
