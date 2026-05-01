# Tintype Python API

The `tintype` module provides a C++ extension for capturing Python traceback snapshots. It serializes call stacks, exception chains, local variables, and the full reachable object graph into a compact binary format that can be inspected offline.

## Quick Start

### Capturing a snapshot

```python
import tintype

# Initialize the module (optional — take_snapshot auto-initializes)
tintype.initialize()

# Capture the current call stack
tintype.take_snapshot()

# Capture an exception with its __cause__/__context__ chain
try:
    1 / 0
except Exception as e:
    tintype.take_snapshot(e)

# Write the snapshot file to disk
tintype.finalize("/tmp/my_snapshot.pytb", metadata={"app": "my_app"})
```

### Reading a snapshot file

```python
import tintype

reader = tintype.SnapshotReader("/tmp/my_snapshot.pytb")

# Iterate all snapshots (oldest first)
for snap in reader.get_all_snapshots():
    print(f"Timestamp: {snap.timestamp}")
    for st_id, st in snap.stacktraces.items():
        print(f"  Stacktrace {st_id}: {len(st.frames)} frames")
        if st.exception_object:
            print(f"    Exception: {st.exception_object}")
        for frame in st.frames:
            print(f"    {frame.function_name} ({frame.file_path}:{frame.line_number})")
            for name, value in frame.get_locals().items():
                print(f"      {name} = {value!r}")
```

### Capturing all threads

```python
import tintype

tintype.initialize()

# Capture every Python thread's call stack in a single snapshot
tintype.snapshot_all_threads(timeout=2.0)

tintype.finalize("/tmp/all_threads.pytb")
```

All threads are captured via `sys._current_frames()` while holding the GIL, which provides a consistent snapshot of all thread stacks.

### Periodic sampling

```python
import tintype

# Context manager: initialize, sample, finalize in one block
with tintype.sampling(
    interval=0.1,                          # sample every 100ms
    mode=tintype.SamplingMode.ALL_THREADS, # or SINGLE_THREAD
    path="/tmp/profile.pytb",
    timeout=2.0,
):
    run_workload()
```

Or with manual control:

```python
import tintype

tintype.initialize()
tintype.enable_sampling(0.1, tintype.SamplingMode.ALL_THREADS, timeout=2.0)

run_workload()

tintype.disable_sampling()
tintype.finalize("/tmp/profile.pytb")
```

The sampling timer runs on a C++ thread (invisible to `snapshot_all_threads()`). In `SINGLE_THREAD` mode, the calling thread is sampled via `sys._current_frames()`.

### Using as an exception hook

```python
import tintype

# One-liner to capture snapshots on unhandled exceptions
tintype.install_exception_hook(path="/tmp/crash.pytb")
```

Or with a custom callback and auto-generated path:

```python
import tintype

def on_snapshot(path: str) -> None:
    upload_to_crash_service(path)

tintype.install_exception_hook(callback=on_snapshot)
```

### Interactive viewer

The `tintype_viewer` provides a curses-based TUI for browsing snapshot files:

```bash
python -m tintype.utils.tintype_viewer /tmp/my_snapshot.pytb
```

Features: browse snapshots and stacktraces, expand frames to inspect local variables, view embedded source files, and extract source files to disk.

## Module-Level Functions

### `initialize(collect_stats=False, frame_file_path_filters=None) -> SnapshotReader`

Initialize the snapshot module. Must be called before `take_snapshot()`, or `take_snapshot()` will auto-initialize with default settings.

Returns a `SnapshotReader` that shares memory with the internal writer, allowing snapshots to be read before `finalize()` is called. The returned reader automatically sees new snapshots as they are taken.

If already initialized, returns the existing reader without reinitializing.

**Parameters:**
- `collect_stats` (`bool`, default `False`): If `True`, collect timing and performance statistics accessible via `get_stats()`.
- `frame_file_path_filters` (`list[str] | None`, default `None`): Optional list of substrings. When processing frames, any frame whose file path contains one of these substrings will be silently skipped. Unlike `max_frames` or timeout truncation, filtered frames do not cause the `truncated` flag to be set on `Stacktrace` or `Snapshot` objects.

**Returns:** A `SnapshotReader` that reads from the writer's in-memory buffer.

**Note:** The returned reader becomes invalid after `finalize()` is called. After finalize, create a new `SnapshotReader` from the output file path.

### `take_snapshot(traceback_or_exception=None, *, max_frames=None, max_object_depth=None, timeout=None, skip_frames=0) -> Snapshot | None`

Take a snapshot. Multiple snapshots can be taken before `finalize()`.

**Parameters:**
- `traceback_or_exception` (`TracebackType | BaseException | None`, default `None`):
  - `None`: Capture the current call stack (thread snapshot, stacktrace ID 0).
  - `TracebackType`: Capture the frames from a traceback object (stacktrace ID 0).
  - `BaseException`: Capture the exception's traceback and the full `__cause__`/`__context__` chain. The thread stacktrace gets ID 0; exception stacktraces get sequential IDs starting from 1.
- `max_frames` (`int | None`, default `None`): Maximum number of frames to capture per stacktrace. `None` means no limit. When the limit is reached, the stacktrace is marked as truncated.
- `max_object_depth` (`int | None`, default `None`): Maximum depth of object graph traversal. Depth 0 is the frame's local variables. When the limit is reached, non-primitive objects are serialized as their `repr()` string with no children. `None` means no limit. When triggered, the `object_depth_truncated` flag is set on the `Stacktrace` object (the `truncated` flag is NOT affected — it only reflects frame omission).
- `timeout` (`float | None`, default `None`): Maximum time in seconds for the entire snapshot operation. `None` means no timeout. If the timeout is reached, processing stops and any in-progress frame is discarded. Completed frames are preserved.
- `skip_frames` (`int`, default `0`): Number of frames to skip from the top of the call stack. Only applies when capturing the current stack (`traceback_or_exception=None`). Useful for internal callers that want to exclude their own frames.

Auto-initializes with `collect_stats=False` if not already initialized.

**Cancellation behavior:** When cancelled (via `cancel_snapshot()`, `max_frames` limit, or `timeout`):
- Frames that were fully processed are preserved.
- The in-progress frame (if any) is discarded.
- If all frames are discarded, the stacktrace is omitted.
- If all stacktraces are omitted, the snapshot is not written.
- The `truncated` flag is set on affected `Stacktrace` and `Snapshot` objects.

### `finalize(path="", metadata=None, compression_level=3) -> None`

Finalize the snapshot module. Builds the file table (embedded source files), writes trailing sections (environment variables, PAR manifest, metadata), compresses, and writes the output file.

After `finalize()`, the module is deinitialized. Call `initialize()` again to start a new capture session.

**Parameters:**
- `path` (`str`, default `""`): Output file path. If empty, the snapshot data is discarded without writing.
- `metadata` (`dict[str, Any] | None`, default `None`): Optional dictionary to store in the snapshot file. Serialized as JSON. Useful for recording application name, PID, timestamps, etc.
- `compression_level` (`int | None`, default `3`): Zstd compression level (1-22). Higher levels produce smaller files but are slower. Pass `None` to write uncompressed.

### `get_stats() -> dict[str, Any]`

Get timing and performance statistics. Only populated if `initialize(collect_stats=True)` was used.

**Returns:** A dictionary with keys:
- `initialize_time_ms`: Time spent in `initialize()`
- `finalize_time_ms`: Time spent in `finalize()`
- `total_snapshot_time_ms`: Total time spent in `take_snapshot()` calls
- `snapshot_count`: Number of snapshots taken
- `total_objects`: Total number of objects serialized to the heap
- `snapshot_breakdown`: Granular snapshot timing:
  - `write_frame_record_time_ms`
  - `total_frame_count`, `total_objects_processed`
- `object_queue_breakdown`: Granular object queue timing:
  - `object_lookup_time_ms`, `object_processing_time_ms`, `repr_time_ms`, `slots_time_ms`, `attr_access_time_ms`, `class_members_time_ms`, `serialization_time_ms`
  - `objects_skipped`
- `object_stats`: Per-type statistics mapping type name to `{count, total_time_ms, avg_time_us, total_bytes, avg_bytes}`
- `finalize_breakdown`: Granular finalize timing:
  - `file_table_time_ms`, `environment_time_ms`, `manifest_time_ms`, `metadata_time_ms`, `msync_time_ms`, `compression_time_ms`, `output_file_time_ms`, `cleanup_time_ms`
  - `file_count`, `uncompressed_data_size`, `compressed_data_size`

### `reset_stats() -> None`

Reset all statistics to zero.

### `snapshot_all_threads(*, timeout=1.0, max_frames=None, max_object_depth=None) -> Snapshot | None`

Capture all Python threads' call stacks in a single snapshot record. Uses `sys._current_frames()` to get all thread frames while holding the GIL, providing a consistent snapshot.

**Parameters:**
- `timeout` (`float`, default `1.0`): Timeout in seconds for the capture operation.
- `max_frames` (`int | None`, default `None`): Maximum number of frames to capture per thread stacktrace. `None` means no limit.
- `max_object_depth` (`int | None`, default `None`): Maximum depth of object graph traversal. `None` means no limit.

**Returns:** A `Snapshot` containing one `Stacktrace` per captured thread (keyed by thread ID), or `None` if another snapshot operation is already in progress.

Auto-initializes with `collect_stats=False` if not already initialized.

**Reentrancy:** While `snapshot_all_threads()` is running, concurrent calls to `take_snapshot()`, `take_snapshot(exception)`, and `snapshot_all_threads()` return `None` immediately. The reverse also holds — `snapshot_all_threads()` returns `None` if `take_snapshot()` is in progress.

**Stacktrace IDs:** Each stacktrace's `id` is the native thread identifier (same as `threading.get_ident()`), not the sequential IDs used by exception chain snapshots. Thread stacktraces have `exception_object = None`.

### `cancel_snapshot() -> None`

Request cancellation of the current snapshot operation. Thread-safe: can be called from any thread (e.g., a timer thread or signal handler).

When called, the in-progress `take_snapshot()` will stop processing after the current object completes. Any fully processed frames are preserved; the in-progress frame is discarded.

The cancellation flag is automatically cleared at the end of each `take_snapshot()` call, so subsequent calls are unaffected.

### `take_snapshot_from_frame(frame, thread_id, *, max_frames=None, max_object_depth=None, timeout=None) -> Snapshot | None`

Take a snapshot of a single thread given its frame object. Intended for capturing a specific thread's stack from another thread (e.g., the sampling fallback path).

**Parameters:**
- `frame` (`object`): A Python frame object, typically from `sys._current_frames()[thread_id]`.
- `thread_id` (`int`): The thread ID for the stacktrace (used as the stacktrace key).
- `max_frames` (`int | None`, default `None`): Maximum number of frames to capture.
- `max_object_depth` (`int | None`, default `None`): Maximum depth of object graph traversal. `None` means no limit.
- `timeout` (`float | None`, default `None`): Maximum time in seconds for the operation.

**Returns:** The captured `Snapshot`, or `None` if another snapshot is in progress.

Auto-initializes if not already initialized.

### `enable_sampling(interval, mode=SamplingMode.ALL_THREADS, *, max_frames=None, max_object_depth=None, timeout=1.0) -> None`

Start periodic sampling. Spawns a C++ timer thread that periodically takes snapshots at the specified interval.

**Parameters:**
- `interval` (`float`): Seconds between samples.
- `mode` (`SamplingMode`, default `SamplingMode.ALL_THREADS`):
  - `SamplingMode.ALL_THREADS`: Each tick calls `snapshot_all_threads()`.
  - `SamplingMode.SINGLE_THREAD`: Each tick samples only the thread that called `enable_sampling()` via `sys._current_frames()`.
- `max_frames` (`int | None`, default `None`): Maximum frames per stacktrace per sample.
- `max_object_depth` (`int | None`, default `None`): Maximum depth of object graph traversal. `None` means no limit.
- `timeout` (`float`, default `1.0`): Timeout per sample in seconds. In `ALL_THREADS` mode, this is passed to `snapshot_all_threads()`.

**Raises:** `RuntimeError` if sampling is already active or if Python < 3.12.

The sampling timer thread is invisible to `snapshot_all_threads()` — it has no Python frames and does not appear in snapshots.

### `disable_sampling() -> None`

Stop periodic sampling. Blocks until the sampling thread exits.

**Raises:** `RuntimeError` if sampling is not active.

### `sampling(interval, mode=SamplingMode.ALL_THREADS, *, collect_stats=False, frame_file_path_filters=None, max_frames=None, max_object_depth=None, timeout=1.0, path="", metadata=None, compression_level=3, finalize_on_exit=True) -> Generator` (context manager)

Context manager that initializes, samples, and optionally finalizes.

On entry: calls `initialize()` then `enable_sampling()`.
On exit: calls `disable_sampling()`, then `finalize()` if `finalize_on_exit` is `True`.

```python
with tintype.sampling(0.1, path="/tmp/profile.pytb"):
    run_workload()
```

**Parameters:**
- `interval`, `mode`, `max_frames`, `max_object_depth`, `timeout`: Passed to `enable_sampling()`.
- `collect_stats`, `frame_file_path_filters`: Passed to `initialize()`.
- `path`, `metadata`, `compression_level`: Passed to `finalize()`.
- `finalize_on_exit` (`bool`, default `True`): If `False`, `finalize()` is not called on exit. Useful for continuing to take snapshots after the sampling block.

### `SamplingMode` (enum)

| Value | Description |
|-------|-------------|
| `SamplingMode.ALL_THREADS` | Sample all Python threads on each tick (via `snapshot_all_threads()`). |
| `SamplingMode.SINGLE_THREAD` | Sample only the thread that called `enable_sampling()`. |

### `install_exception_hook(*, collect_stats=False, frame_file_path_filters=None, max_frames=None, max_object_depth=None, timeout=None, path="", metadata=None, compression_level=3, callback=_default_exception_hook_callback) -> None`

Install a `sys.excepthook` that automatically captures a tintype snapshot when an unhandled exception occurs. This is a convenience wrapper that orchestrates `initialize()`, `take_snapshot()`, and `finalize()`.

When the hook fires it will call `initialize()`, `take_snapshot()` with the exception, `finalize()`, the callback, and then the previously installed `sys.excepthook`, in that order.

**Parameters:**
- `collect_stats` (`bool`, default `False`): Passed to `initialize()`.
- `frame_file_path_filters` (`list[str] | None`, default `None`): Passed to `initialize()`.
- `max_frames` (`int | None`, default `None`): Passed to `take_snapshot()`.
- `max_object_depth` (`int | None`, default `None`): Passed to `take_snapshot()`. Maximum depth of object graph traversal.
- `timeout` (`float | None`, default `None`): Passed to `take_snapshot()`.
- `path` (`str`, default `""`): Output file path, passed to `finalize()`. If empty, a temporary file is created automatically.
- `metadata` (`dict[str, Any] | None`, default `None`): Passed to `finalize()`.
- `compression_level` (`int | None`, default `3`): Passed to `finalize()`.
- `callback` (`Callable[[str], None]`): Called with the output file path after `finalize()` completes. Default prints `"Tintype exception snapshot written to: {path}"`.

**Raises:** `RuntimeError` if an exception hook is already installed. Call `uninstall_exception_hook()` first to replace it.

### `uninstall_exception_hook() -> None`

Uninstall the tintype exception hook and restore the previous `sys.excepthook`.

**Raises:** `RuntimeError` if no exception hook is currently installed.

### `exception_hook(*, ...) -> Generator` (context manager)

Context manager that installs the tintype exception hook on entry. On clean exit, the hook is uninstalled. If an exception propagates out of the block, the hook is left installed so that `sys.excepthook` can fire it for unhandled exceptions. Accepts the same parameters as `install_exception_hook()`.

```python
with tintype.exception_hook(path="/tmp/crash.pytb"):
    main()
```

## Classes

### `SnapshotReader`

Reads snapshot files from disk or from a shared memory buffer (when returned by `initialize()`).

#### Constructor

```python
SnapshotReader(path: str)
```

Open a snapshot file for reading. The file may be zstd-compressed or uncompressed.

**Raises:** `RuntimeError` if the file cannot be opened or has an invalid format.

#### Methods

| Method | Return Type | Description |
|--------|-------------|-------------|
| `snapshot_count()` | `int` | Get the number of snapshot records in the file. |
| `get_all_snapshots()` | `list[Snapshot]` | Get all snapshots in chronological order (oldest first). |
| `get_latest_snapshot()` | `Snapshot \| None` | Get the most recent snapshot. |
| `get_snapshot_at_index(index)` | `Snapshot \| None` | Get a snapshot by its chronological index (0 = first/oldest). Returns `None` if index is out of range. |
| `get_metadata()` | `str` | Get the metadata JSON string. |
| `get_manifest()` | `str` | Get the PAR manifest JSON string. Empty if not captured. |
| `get_environment()` | `dict[str, str]` | Get environment variables. Empty dict if not captured. |
| `get_stats()` | `dict[str, int]` | Get statistics. Returns live stats for borrowed readers, file stats for file-based readers. |
| `get_all_source_files()` | `list[SourceFile]` | Get all embedded source files. |
| `get_extracted_files_dir()` | `str` | Path to temp directory with extracted source files. |
| `get_last_error()` | `str` | Last error message. Empty if no error. |
| `_read_raw_object(offset)` | `dict \| bool \| None` | Read a raw object from the heap by offset. Internal use only. |
| `get_python_object(python_id, object_map)` | `Any` | Resolve a Python object ID to a Python object. |

#### Static Methods

| Method | Return Type | Description |
|--------|-------------|-------------|
| `is_magic_offset(offset)` | `bool` | Check if an offset represents None, True, or False. |
| `get_magic_offset_type(offset)` | `str \| None` | Get the type name of a magic offset. |

### `Snapshot`

Represents a single snapshot record. Contains one or more stacktraces and an object map for resolving variable references.

#### Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `timestamp` | `int` | Unix timestamp in microseconds when the snapshot was taken. |
| `truncated` | `bool` | `True` if this snapshot was truncated due to cancellation, `max_frames`, or timeout. |
| `stacktraces` | `dict[int, Stacktrace]` | Map from stacktrace ID to `Stacktrace`. ID 0 is the thread/traceback snapshot; IDs 1+ are exception chain entries. |
| `object_map` | `dict[int, int]` | Map from Python object ID to heap offset, used to resolve variable references. |

#### Methods

| Method | Return Type | Description |
|--------|-------------|-------------|
| `frames()` | `list[Frame]` | Convenience accessor: frames from the first stacktrace. |
| `get_prev_snapshot()` | `Snapshot \| None` | Get the previous snapshot in the linked list, or `None` if this is the first snapshot. Caches the result. |
| `get_python_object(python_id)` | `Any` | Resolve a Python object ID using this snapshot's object map and reader. |

### `Stacktrace`

Represents a stacktrace within a snapshot. For single-thread snapshots (`take_snapshot`), the ID is the native thread ID. For multi-thread snapshots (`snapshot_all_threads`), the ID is also the native thread identifier.

#### Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `id` | `int` | Stacktrace ID. For `take_snapshot` and `snapshot_all_threads`: the native thread identifier. For exception chains, unique sequential IDs starting from 1. |
| `frames` | `list[Frame]` | Stack frames, ordered from innermost (most recent call) to outermost. |
| `truncated` | `bool` | `True` if this stacktrace was truncated (some frames were omitted). |
| `object_depth_truncated` | `bool` | `True` if any objects in this stacktrace were depth-limited (serialized as repr due to `max_object_depth`). |
| `exception_object` | `Any` | The exception object (resolved from heap), or `None` for thread snapshots. |
| `cause_id` | `int \| None` | Stacktrace ID of the `__cause__` exception, or `None` if none. |
| `context_id` | `int \| None` | Stacktrace ID of the `__context__` exception, or `None` if none. |
| `thread_name` | `str` | Name of the thread (from `threading.Thread.name`), or empty string for exception stacktraces or if unavailable. |

#### Methods

| Method | Return Type | Description |
|--------|-------------|-------------|
| `get_cause()` | `Stacktrace \| None` | Get the `__cause__` stacktrace, or `None` if there is no cause. Caches the result. |
| `get_context()` | `Stacktrace \| None` | Get the `__context__` stacktrace, or `None` if there is no context. Caches the result. |
| `get_traceback()` | `TracebackType \| None` | Generate a synthetic Python traceback object from this stacktrace's frames. Useful for feeding into debuggers. |

#### Internal Attributes

These are set automatically when snapshots are retrieved through `SnapshotReader`:

| Attribute | Type | Description |
|-----------|------|-------------|
| `_snapshot` | `Snapshot` | Back-reference to the parent snapshot. |
| `_frames` | `list[Frame]` | Cached wired frames list. |

### `Frame`

Represents a single stack frame.

#### Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `file_path` | `str` | Path to the source file. Points to the extracted copy in a temp directory when source files are embedded. |
| `original_file_path` | `str` | Original absolute path to the source file as recorded at capture time. |
| `function_name` | `str` | Name of the function (`co_name`). |
| `function_qualname` | `str` | Qualified name of the function (`co_qualname`), e.g. `ClassName.method`. Falls back to `function_name` on Python < 3.11 or if unavailable. |
| `line_number` | `int` | Line number in the source file. |
| `_local_variables` | `list[LocalVariable]` | Raw local variable metadata (names and python IDs). Internal use only. Prefer `get_locals()`. |

#### Methods

| Method | Return Type | Description |
|--------|-------------|-------------|
| `get_locals()` | `dict[str, Any]` | Get a dictionary mapping local variable names to their resolved Python objects. Uses wired back-references to traverse `frame._stacktrace._snapshot._reader` to reach the `SnapshotReader` and resolve objects from the heap. |

#### Internal Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `_stacktrace` | `Stacktrace` | Back-reference to the parent stacktrace. |

### `LocalVariable`

Represents a local variable reference in a frame.

#### Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `name` | `str` | Variable name. |
| `python_id` | `int` | Python object ID. Use `snapshot.get_python_object(python_id)` or `frame.get_locals()` to resolve to the actual value. |

### `SourceFile`

Represents an embedded source file. Source files referenced by stack frames are automatically captured and stored in the snapshot file during `finalize()`.

#### Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `path` | `str` | Original absolute file path. |
| `content` | `str` | Full file contents as a string. |

### `SerializedObject`

Represents a deserialized complex Python object that doesn't map to a builtin type. Supports arbitrary attribute access via `__dict__`. The `repr()` returns the original object's repr as captured at snapshot time.

### `SerializedListObject(list)`

A `list` subclass representing a deserialized list (or list subclass) with a custom repr and additional attributes accessible via `__dict__`.

### `SerializedDictObject(dict)`

A `dict` subclass representing a deserialized dict (or dict subclass) with a custom repr and additional attributes accessible via `__dict__`.

## Object Wiring

When snapshots are retrieved through `SnapshotReader` methods (`get_all_snapshots`, `get_latest_snapshot`), the returned objects are "wired" with back-references that enable navigation:

```
SnapshotReader
  └── Snapshot._reader → SnapshotReader
        └── Stacktrace._snapshot → Snapshot
              └── Frame._stacktrace → Stacktrace
```

This wiring allows `Frame.get_locals()` to resolve variable values by traversing up to the reader. Without wiring (e.g., accessing C++ attributes directly), `get_locals()` will fail.

## Lifecycle

### One-shot snapshots

```
initialize()  ──►  take_snapshot()  ──►  take_snapshot()  ──►  finalize(path)
     │                                                              │
     ▼                                                              ▼
 SnapshotReader (live)                                    File written to disk
 reads from writer memory                                 SnapshotReader(path)
```

1. **`initialize()`**: Allocates an mmap'd buffer (starts at 16 MB, grows on demand up to 16 GB). Returns a live `SnapshotReader` that shares this memory.
2. **`take_snapshot()`**: Captures frames, local variables, and the reachable object graph into the buffer. Can be called multiple times.
3. **`finalize(path)`**: Collects source files, environment, manifest, and metadata. Compresses with zstd and writes to `path`. Deallocates the buffer. The live reader is invalidated.

After `finalize()`, open the file with `SnapshotReader(path)` for offline reading.

### Periodic sampling

```
initialize()  ──►  enable_sampling()  ──►  [timer ticks]  ──►  disable_sampling()  ──►  finalize(path)
                        │                       │
                        ▼                       ▼
                   C++ timer thread       snapshot_all_threads() or
                   (invisible)           take_snapshot() per tick
```

Or using the context manager:

```
with sampling(interval, path=path):
    run_workload()
# initialize + enable_sampling on entry
# disable_sampling + finalize on exit
```

## Command-Line Tools

| Command | Description |
|---------|-------------|
| `python -m tintype.utils.tintype_dump /path/to/file.pytb` | Dump a snapshot file as structured text or JSON. |
| `python -m tintype.utils.tintype_viewer /path/to/file.pytb` | Interactive curses-based snapshot file viewer. |

## Demo Scripts

| Script | Description |
|--------|-------------|
| `python -m tintype.demo.tintype_demo` | Captures snapshots with complex object graphs. |
| `python -m tintype.demo.demo_all_threads` | Captures all threads (CPU workers, native sleepers, lock contention). |
| `python -m tintype.demo.demo_sampling` | Periodic sampling with ALL_THREADS and SINGLE_THREAD modes. |
| `python -m tintype.demo.stress_all_threads` | Stress test: concurrent snapshots, ephemeral threads, sampling interactions. |

## Related Documentation

- [FILE_FORMAT.md](FILE_FORMAT.md) — Binary file format specification with byte-level struct definitions.
