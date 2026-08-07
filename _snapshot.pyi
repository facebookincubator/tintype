# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

from types import TracebackType
from typing import Any

def initialize(
    collect_stats: bool = False,
    frame_file_path_filters: list[str] | None = None,
) -> SnapshotReader:
    """Initialize the snapshot module.

    Returns a SnapshotReader that shares memory with the writer, allowing
    snapshots to be read before finalize() is called. The returned reader
    automatically sees new snapshots as they are taken.

    If already initialized, returns the existing reader without
    reinitializing.

    Note: The returned reader becomes invalid after finalize() is called.
    After finalize(), create a new SnapshotReader from the output file path.

    Args:
        collect_stats: If True, collect timing and performance statistics.
                       Default is False for maximum performance.
        frame_file_path_filters: Optional list of substrings. When processing
                       frames, any frame whose file path contains one of these
                       substrings will be silently skipped. Filtered frames do
                       not cause the stacktrace or snapshot to be marked as
                       truncated.

    Returns:
        A SnapshotReader that can read snapshots from the writer's memory.
    """
    ...

def take_snapshot(
    traceback_or_exception: TracebackType | BaseException | None = None,
    *,
    max_frames: int | None = None,
    max_object_depth: int | None = None,
    timeout: float | None = None,
    skip_frames: int = 0,
) -> Snapshot | None:
    """Take a snapshot.

    Pass None (default) to capture the current call stack, a traceback object
    to capture its frames, or an exception to capture its traceback and the
    full __cause__/__context__ chain.

    If the snapshot module has not been initialized, it will be
    auto-initialized with default settings (collect_stats=False).

    Args:
        traceback_or_exception: Optional traceback, exception, or None.
        max_frames: Maximum number of frames to capture per stacktrace.
                    None means no limit.
        max_object_depth: Maximum depth of object graph traversal. Depth 0 is
                    the frame's local variables. When the limit is reached,
                    non-primitive objects are serialized as their repr() string
                    with no children. None means no limit.
        timeout: Maximum time in seconds for the entire snapshot operation.
                 None means no timeout. Starts a background timer that
                 triggers cancellation.

    Returns:
        The captured Snapshot object, or None if the snapshot could not
        be written or read back.
    """
    ...

def finalize(
    path: str = "",
    metadata: dict[str, Any] | None = None,
    compression_level: int | None = 3,
) -> None:
    """Finalize the snapshot module.

    If path is provided, builds the file table, compresses, and writes the
    output file to that path. If path is empty, discards the snapshot data.

    If stats collection was enabled via initialize(collect_stats=True),
    statistics are automatically embedded in the output file.

    Args:
        path: Optional output file path for the compressed snapshot.
              If empty, the snapshot data is discarded.
        metadata: Optional dictionary of metadata to store in the snapshot file.
                  Will be JSON serialized.
        compression_level: Zstd compression level (1-22). Pass None to write
                          uncompressed. Default is 3.
    """
    ...

def get_stats() -> dict[str, Any]:
    """Get statistics about the snapshot module.

    Returns a dictionary with the following keys:
    - initialize_time_ms: Time spent in initialize() in milliseconds
    - finalize_time_ms: Time spent in finalize() in milliseconds
    - total_snapshot_time_ms: Total time spent taking snapshots in milliseconds
    - snapshot_count: Number of snapshots taken
    - total_objects: Total number of objects serialized
    - snapshot_breakdown: Dict with granular snapshot timing:
        - write_frame_record_time_ms: Time writing frame records
        - total_frame_count: Total frames across all snapshots
        - total_objects_processed: Total objects processed in queue
    - object_queue_breakdown: Dict with granular object queue timing:
        - object_lookup_time_ms: Time checking if objects were already processed
        - object_processing_time_ms: Total time in processObject() calls
        - repr_time_ms: Time calling repr() on objects
        - slots_time_ms: Time iterating __slots__ via MRO
        - attr_access_time_ms: Time accessing object attributes
        - class_members_time_ms: Time extracting class members from type
        - serialization_time_ms: Time writing objects to the heap
        - objects_skipped: Number of objects skipped (already processed)
    - object_stats: Dict mapping type names to per-type statistics:
        - count: Number of objects of this type
        - total_time_ms: Total serialization time for this type
        - avg_time_us: Average serialization time per object in microseconds
        - total_bytes: Total bytes written for this type
        - avg_bytes: Average bytes per object
    """
    ...

def reset_stats() -> None:
    """Reset statistics to zero."""
    ...

def cancel_snapshot() -> None:
    """Request cancellation of the current snapshot operation.

    Thread-safe: can be called from any thread (e.g., a timer thread
    or signal handler).
    """
    ...

def snapshot_all_threads(
    *,
    timeout: float = 1.0,
    max_frames: int | None = None,
    max_object_depth: int | None = None,
) -> Snapshot | None:
    """Take a snapshot of all Python threads.

    Uses sys._current_frames() while holding the GIL to capture all thread
    stacks atomically. The GIL ensures all threads are paused during capture.

    Returns None if a snapshot is already in progress (reentrancy guard).
    Any concurrent calls to take_snapshot() also return None while this
    is running.

    Auto-initializes if not already initialized.

    Args:
        timeout: Timeout in seconds for serialization. Default is 1.0 second.
        max_frames: Maximum number of frames to capture per thread stacktrace.
                    None means no limit.

    Returns:
        The captured Snapshot object containing one stacktrace per thread,
        or None if a snapshot is already in progress.
    """
    ...

def take_snapshot_from_frame(
    frame: object,
    thread_id: int,
    *,
    max_frames: int | None = None,
    max_object_depth: int | None = None,
    timeout: float | None = None,
) -> Snapshot | None:
    """Take a snapshot of a single thread given its frame object.

    Used for sampling fallback when the target thread is in native code.
    The frame should come from sys._current_frames().

    Args:
        frame: A Python frame object for the thread to capture.
        thread_id: The thread ID for the stacktrace.
        max_frames: Maximum number of frames to capture. None means no limit.
        timeout: Maximum time in seconds for the operation. None means no
                 timeout.

    Returns:
        The captured Snapshot object, or None if the snapshot could not be
        taken (e.g., another snapshot is in progress).
    """
    ...

class SamplingMode:
    """Sampling mode for enable_sampling().

    SINGLE_THREAD: Sample only the thread that called enable_sampling().
    ALL_THREADS: Sample all Python threads.
    """

    SINGLE_THREAD: SamplingMode
    ALL_THREADS: SamplingMode

def enable_sampling(
    interval: float,
    mode: SamplingMode = ...,
    *,
    max_frames: int | None = None,
    max_object_depth: int | None = None,
    timeout: float = 1.0,
) -> None:
    """Start periodic sampling with a C++ timer thread.

    Args:
        interval: Seconds between samples.
        mode: SamplingMode.SINGLE_THREAD samples only the calling thread;
              SamplingMode.ALL_THREADS samples all Python threads.
        max_frames: Max frames per stacktrace (None = no limit).
        timeout: Timeout per sample in seconds.

    Raises:
        RuntimeError: If sampling is already active.
    """
    ...

def disable_sampling() -> None:
    """Stop periodic sampling. Blocks until the sampling thread exits.

    Raises:
        RuntimeError: If sampling is not active.
    """
    ...

class SerializedObject:
    """A deserialized complex Python object with a custom repr.

    Used for objects that don't map to a builtin Python type.
    Supports arbitrary attribute access via __dict__.
    """

    def __init__(self, repr_str: str) -> None: ...
    def __repr__(self) -> str: ...

class SerializedListObject(list[Any]):
    """A deserialized list subclass with a custom repr.

    Used for list subclasses that have additional attributes.
    Supports arbitrary attribute access via __dict__.
    """

    def __init__(self, items: list[Any], repr_str: str) -> None: ...
    def __repr__(self) -> str: ...

class SerializedDictObject(dict[Any, Any]):
    """A deserialized dict subclass with a custom repr.

    Used for dict subclasses that have additional attributes.
    Supports arbitrary attribute access via __dict__.
    """

    def __init__(self, items: dict[Any, Any], repr_str: str) -> None: ...
    def __repr__(self) -> str: ...

class LocalVariable:
    """Represents a local variable in a frame."""

    name: str
    python_id: int

class Frame:
    """Represents a stack frame in a snapshot."""

    file_path: str
    original_file_path: str
    function_name: str
    function_qualname: str
    line_number: int
    _local_variables: list[LocalVariable]
    """Raw local variable metadata (names and python IDs). Internal use only.
    Prefer get_locals() which returns resolved Python objects."""
    _stacktrace: Stacktrace | None

    def get_locals(self) -> dict[str, Any]:
        """Get a dictionary mapping local variable names to their Python objects.

        Uses wired backreferences to the snapshot's reader and object map.

        Returns:
            A dictionary mapping variable names to their resolved Python objects.
        """
        ...

class Stacktrace:
    """Represents a stacktrace in a snapshot.

    A stacktrace can represent either a thread's call stack or an exception's
    traceback.
    """

    id: int
    frames: list[Frame]
    exception_object: Any
    cause_id: int | None
    context_id: int | None
    truncated: bool
    object_depth_truncated: bool
    thread_name: str
    _snapshot: Snapshot | None

    def get_cause(self) -> Stacktrace | None:
        """Get the __cause__ stacktrace, or None if there is no cause."""
        ...

    def get_context(self) -> Stacktrace | None:
        """Get the __context__ stacktrace, or None if there is no context."""
        ...

    def get_traceback(self) -> TracebackType | None:
        """Generate a Python traceback from this stacktrace's frames."""
        ...

    def reconstruct_exception(self) -> BaseException | None:
        """Reconstruct a BaseException from this stacktrace.

        The reconstructed exception carries __traceback__ and a wired
        __cause__/__context__ chain. Returns None if the stacktrace has no
        exception. The reconstructed class is always Exception (the original
        class is not recoverable from a snapshot).
        """
        ...

class Snapshot:
    """Represents a snapshot record."""

    timestamp: int
    truncated: bool
    stacktraces: dict[int, Stacktrace]
    object_map: dict[int, int]

    def frames(self) -> list[Frame]:
        """Get frames from the first stacktrace (convenience accessor)."""
        ...

    def get_prev_snapshot(self) -> Snapshot | None:
        """Get the previous snapshot, or None if this is the first snapshot."""
        ...

    def get_next_snapshot(self) -> Snapshot | None:
        """Get the next snapshot, or None if this is the most recent snapshot."""
        ...

    def get_python_object(self, python_id: int) -> Any:
        """Get a Python object from the heap by pythonId.

        Uses this snapshot's object map to resolve the ID.
        """
        ...

class SourceFile:
    """Represents a source file entry."""

    path: str
    content: str

class SnapshotReader:
    """Class for reading snapshot files."""

    def __init__(self, path: str) -> None:
        """Open a snapshot file for reading.

        Args:
            path: Path to the snapshot file.

        Raises:
            RuntimeError: If the file cannot be opened or is invalid.
        """
        ...

    def get_last_error(self) -> str:
        """Get the last error message. Returns empty string if no error."""
        ...

    def snapshot_count(self) -> int:
        """Get the number of snapshot records in the file."""
        ...

    def get_metadata(self) -> str:
        """Get the metadata JSON string."""
        ...

    def get_manifest(self) -> str:
        """Get the manifest JSON string (from __manifest__.json).

        Returns empty string if no manifest was captured.
        """
        ...

    def get_environment(self) -> dict[str, str]:
        """Get the environment variables as a dictionary.

        Returns an empty dict if no environment was captured.
        """
        ...

    def get_latest_snapshot(self) -> Snapshot | None:
        """Get the most recent snapshot."""
        ...

    def get_snapshot_at_index(self, index: int) -> Snapshot | None:
        """Get a snapshot by its chronological index (0 = first/oldest)."""
        ...

    def get_all_snapshots(self) -> list[Snapshot]:
        """Get all snapshots in chronological order (oldest first)."""
        ...

    def get_all_source_files(self) -> list[SourceFile]:
        """Get all source files."""
        ...

    def get_extracted_files_dir(self) -> str:
        """Get the path to the temporary directory containing extracted source files.

        Returns empty string if no files were extracted or file is not open.
        """
        ...

    def get_working_file_path(self) -> str | None:
        """Get the path to the file that is mmapped by this reader.

        For file-based readers, this is the temporary decompressed file.
        For borrowed-memory readers, this is the writer's backing file.
        Returns None if no path is available.
        """
        ...

    def _read_raw_object(self, offset: int) -> dict[str, Any] | bool | None:
        """Read an object from the heap by offset.

        Returns None for None magic offset, True/False for bool magic offsets,
        or a dict with object data for regular objects.
        """
        ...

    @staticmethod
    def is_magic_offset(offset: int) -> bool:
        """Check if an offset is a magic offset (None, True, False)."""
        ...

    @staticmethod
    def get_magic_offset_type(offset: int) -> str | None:
        """Get the type of magic offset (None, True, or False)."""
        ...

    def get_python_object(self, python_id: int, object_map: dict[int, int]) -> Any:
        """Get a Python object from the heap by pythonId.

        Uses the object map to resolve pythonId to heap offset.
        Returns primitives directly and creates SerializedObject/SerializedDict/
        SerializedList for complex types.

        Args:
            python_id: The Python object ID to look up.
            object_map: Map from pythonId to heap offset for resolving references.

        Returns:
            The Python object, or None if the pythonId is not found.
        """
        ...

    def get_stats(self) -> dict[str, int]:
        """Get statistics as a dictionary.

        For file-based readers, reads stats from the file's statistics section.
        For borrowed-memory readers (returned by initialize()), returns live
        stats.

        Returns:
            A dictionary mapping statistic names to values (nanoseconds or counts).
            Returns an empty dict if no statistics were collected.
        """
        ...
