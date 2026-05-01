# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Periodic sampling support for tintype.

Provides a sampling() context manager for periodically capturing thread
snapshots at a configurable interval.
"""

from __future__ import annotations

import contextlib
from collections.abc import Generator
from typing import Any

from tintype._snapshot import (
    disable_sampling,
    enable_sampling,
    finalize as _finalize,
    initialize,
    SamplingMode,
)

# Re-export for convenience
__all__ = ["SamplingMode", "enable_sampling", "disable_sampling", "sampling"]


@contextlib.contextmanager
def sampling(
    interval: float,
    mode: SamplingMode = SamplingMode.ALL_THREADS,
    *,
    # initialize() params
    collect_stats: bool = False,
    frame_file_path_filters: list[str] | None = None,
    # sampling params
    max_frames: int | None = None,
    max_object_depth: int | None = None,
    timeout: float = 1.0,
    # finalize() params
    path: str = "",
    metadata: dict[str, Any] | None = None,
    compression_level: int | None = 3,
    # behavior
    finalize_on_exit: bool = True,
) -> Generator[None, None, None]:
    """Context manager that initializes, samples, and optionally finalizes.

    On entry: calls initialize() then enable_sampling().
    On exit: calls disable_sampling(), then finalize() if finalize_on_exit
    is True.

    Args:
        interval: Seconds between samples.
        mode: SamplingMode.SINGLE_THREAD or SamplingMode.ALL_THREADS.
        collect_stats: Passed to initialize().
        frame_file_path_filters: Passed to initialize().
        max_frames: Max frames per stacktrace.
        timeout: Timeout per sample in seconds.
        path: Output file path for finalize().
        metadata: Passed to finalize().
        compression_level: Passed to finalize().
        finalize_on_exit: If True (default), call finalize() on exit.

    Example::

        with tintype.sampling(0.1, path="/tmp/profile.pytb"):
            run_workload()
    """
    initialize(
        collect_stats=collect_stats,
        frame_file_path_filters=frame_file_path_filters,
    )
    enable_sampling(
        interval,
        mode,
        max_frames=max_frames,
        max_object_depth=max_object_depth,
        timeout=timeout,
    )
    try:
        yield
    finally:
        disable_sampling()
        if finalize_on_exit:
            _finalize(
                path=path,
                metadata=metadata,
                compression_level=compression_level,
            )
