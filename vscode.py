# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Stable integration surface used by the Tintype VS Code extension."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import TypedDict

import tintype


CAPTURE_PROTOCOL_VERSION: int = 1


class SessionInfo(TypedDict):
    protocolVersion: int
    pid: int
    cwd: str
    workingFile: str


class CaptureResult(SessionInfo):
    captured: bool
    eventSequence: int


class SnappointError(TypedDict):
    protocolVersion: int
    pid: int
    cwd: str
    error: str


_event_sequence: int = 0
_event_sequence_lock = threading.Lock()


def _next_event_sequence() -> int:
    global _event_sequence
    with _event_sequence_lock:
        _event_sequence += 1
        return _event_sequence


def session_info() -> SessionInfo:
    """Return JSON-serializable information for the active capture session."""
    reader = tintype.initialize()
    working_file = reader.get_working_file_path()
    if working_file is None:
        raise RuntimeError("Tintype did not create a working snapshot file")
    return {
        "protocolVersion": CAPTURE_PROTOCOL_VERSION,
        "pid": os.getpid(),
        "cwd": os.getcwd(),
        "workingFile": working_file,
    }


def capture(timeout: float | None = None) -> CaptureResult:
    """Capture Python execution and return the updated working file.

    ``timeout`` bounds the capture in seconds. On expiry tintype cancels
    the walk and keeps the frames it already completed, so a slow
    debuggee yields a truncated snapshot instead of stalling the caller.
    ``None`` keeps each capture function's own default.
    """
    is_gil_enabled = getattr(sys, "_is_gil_enabled", None)
    if is_gil_enabled is not None and not is_gil_enabled():
        snapshot = tintype.take_snapshot(timeout=timeout)
    elif timeout is None:
        # ``snapshot_all_threads`` has its own non-None default timeout;
        # omit the argument rather than overriding it with None.
        snapshot = tintype.snapshot_all_threads()
    else:
        snapshot = tintype.snapshot_all_threads(timeout=timeout)
    event_sequence = _next_event_sequence()
    result: CaptureResult = {
        **session_info(),
        "captured": snapshot is not None,
        "eventSequence": event_sequence,
    }
    return result


def finalize(path: str) -> SessionInfo:
    """Finalize the active capture session to ``path``."""
    destination = str(Path(path).expanduser().resolve())
    info = session_info()
    tintype.finalize(destination)
    return info


def snappoint_event() -> CaptureResult | SnappointError:
    """Capture a snappoint and return a debugpy-logpoint payload."""
    try:
        return capture()
    except Exception as error:
        return {
            "protocolVersion": CAPTURE_PROTOCOL_VERSION,
            "pid": os.getpid(),
            "cwd": os.getcwd(),
            "error": str(error),
        }


def snappoint() -> bool:
    """Capture from a breakpoint condition and always continue execution."""
    snappoint_event()
    return False
