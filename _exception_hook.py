# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
# pyre-strict

import contextlib
import os
import sys
import tempfile
from collections.abc import Generator
from types import TracebackType
from typing import Any, Callable

from tintype._snapshot import finalize, initialize, take_snapshot

_ExceptHook = Callable[[type[BaseException], BaseException, TracebackType | None], Any]


def _default_exception_hook_callback(path: str) -> None:
    print(f"Tintype exception snapshot written to: {path}")


_saved_excepthook: _ExceptHook | None = None


def install_exception_hook(
    *,
    # initialize() params
    collect_stats: bool = False,
    frame_file_path_filters: list[str] | None = None,
    # take_snapshot() params
    max_frames: int | None = None,
    max_object_depth: int | None = None,
    timeout: float | None = None,
    # finalize() params
    path: str = "",
    metadata: dict[str, Any] | None = None,
    compression_level: int | None = 3,
    # hook-specific params
    callback: Callable[[str], None] = _default_exception_hook_callback,
) -> None:
    """Install a sys.excepthook that captures a tintype snapshot on unhandled exceptions.

    When an unhandled exception occurs, the hook will:
    1. Call initialize() with the given parameters
    2. Call take_snapshot() with the exception and max_frames/timeout
    3. Call finalize() to write the snapshot to disk
    4. Call the callback with the output path
    5. Call the previously installed sys.excepthook

    Args:
        collect_stats: Passed to initialize().
        frame_file_path_filters: Passed to initialize().
        max_frames: Passed to take_snapshot().
        timeout: Passed to take_snapshot().
        path: Output file path for finalize(). If empty, a temporary file is
              created automatically.
        metadata: Passed to finalize().
        compression_level: Passed to finalize().
        callback: Called with the output file path after finalize() completes.
                  Default prints "Tintype exception snapshot written to: {path}".

    Raises:
        RuntimeError: If an exception hook is already installed.
    """
    global _saved_excepthook

    if _saved_excepthook is not None:
        raise RuntimeError("tintype exception hook is already installed")

    old_hook: _ExceptHook = sys.excepthook
    _saved_excepthook = old_hook

    def _tintype_excepthook(
        exctype: type[BaseException],
        exc: BaseException,
        tb: TracebackType | None,
    ) -> None:
        try:
            if path:
                output_path = path
            else:
                fd, output_path = tempfile.mkstemp(suffix=".pytb", prefix="tintype_")
                os.close(fd)

            initialize(
                collect_stats=collect_stats,
                frame_file_path_filters=frame_file_path_filters,
            )
            take_snapshot(
                exc,
                max_frames=max_frames,
                max_object_depth=max_object_depth,
                timeout=timeout,
            )
            finalize(
                path=output_path,
                metadata=metadata,
                compression_level=compression_level,
            )
            callback(output_path)
        finally:
            old_hook(exctype, exc, tb)

    sys.excepthook = _tintype_excepthook


def uninstall_exception_hook() -> None:
    """Uninstall the tintype exception hook and restore the previous sys.excepthook.

    Raises:
        RuntimeError: If no exception hook is currently installed.
    """
    global _saved_excepthook

    hook = _saved_excepthook
    if hook is None:
        raise RuntimeError("tintype exception hook is not installed")

    sys.excepthook = hook
    _saved_excepthook = None


@contextlib.contextmanager
def exception_hook(
    *,
    # initialize() params
    collect_stats: bool = False,
    frame_file_path_filters: list[str] | None = None,
    # take_snapshot() params
    max_frames: int | None = None,
    max_object_depth: int | None = None,
    timeout: float | None = None,
    # finalize() params
    path: str = "",
    metadata: dict[str, Any] | None = None,
    compression_level: int | None = 3,
    # hook-specific params
    callback: Callable[[str], None] = _default_exception_hook_callback,
) -> Generator[None, None, None]:
    """Context manager that installs the tintype exception hook on entry.

    On clean exit, the hook is uninstalled. If an exception propagates out of
    the block, the hook is left installed so that sys.excepthook can fire it
    for unhandled exceptions.

    Accepts the same parameters as install_exception_hook().

    Example::

        with tintype.exception_hook(path="/tmp/crash.pytb"):
            main()
    """
    install_exception_hook(
        collect_stats=collect_stats,
        frame_file_path_filters=frame_file_path_filters,
        max_frames=max_frames,
        max_object_depth=max_object_depth,
        timeout=timeout,
        path=path,
        metadata=metadata,
        compression_level=compression_level,
        callback=callback,
    )
    try:
        yield
    except BaseException:
        # Exception propagating out — leave hook installed so sys.excepthook
        # fires it.
        raise
    else:
        # Clean exit — uninstall the hook.
        uninstall_exception_hook()
