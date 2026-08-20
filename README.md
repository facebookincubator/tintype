# Tintype

Tintype is a high-performance Python library for capturing exception snapshots. It serializes call stacks, exception chains, local variables, and the full reachable object graph into a compact binary format (`.pytb`) that can be inspected offline.

Built as a C++/Python hybrid using pybind11, tintype is designed for minimal overhead in production environments.

## Features

- **Full object graph capture** — serializes local variables and their entire reachable object tree, not just repr strings
- **Exception chain support** — follows `__cause__` and `__context__` chains automatically
- **Multi-thread snapshots** — captures all Python thread stacks atomically via `sys._current_frames()`
- **Periodic sampling** — built-in C++ timer thread for profiling workloads
- **Exception hook** — one-liner to capture snapshots on unhandled exceptions
- **Compact binary format** — zstd-compressed with cross-snapshot object deduplication
- **Source file embedding** — referenced source files are stored in the snapshot for offline viewing
- **Synthetic tracebacks** — reconstruct Python traceback objects for post-mortem debugging

## Installation

```bash
pip install tintype
```

**Requirements:** Python ≥ 3.12, a C++20 compiler, and the zstd development
headers. Tintype currently supports Linux and macOS; Windows is not supported.
Source builds require macOS 10.15 or newer, while published macOS wheels target
macOS 11 or newer.

Install the zstd development package before building from source:

```bash
# Ubuntu/Debian
sudo apt-get install libzstd-dev

# macOS
brew install zstd
```

## Quick Start

### Capture a snapshot

```python
import tintype

tintype.initialize()

# Capture the current call stack
tintype.take_snapshot()

# Capture an exception with its full chain
try:
    1 / 0
except Exception as e:
    tintype.take_snapshot(e)

# Write to disk
tintype.finalize("/tmp/my_snapshot.pytb", metadata={"app": "my_app"})
```

### Read a snapshot file

```python
import tintype

reader = tintype.SnapshotReader("/tmp/my_snapshot.pytb")

for snap in reader.get_all_snapshots():
    print(f"Timestamp: {snap.timestamp}")
    for st_id, st in snap.stacktraces.items():
        print(f"  Thread {st.thread_name}: {len(st.frames)} frames")
        if st.exception_object:
            print(f"    Exception: {st.exception_object}")
        for frame in st.frames:
            print(f"    {frame.function_name} ({frame.file_path}:{frame.line_number})")
            for name, value in frame.get_locals().items():
                print(f"      {name} = {value!r}")
```

### Capture all threads

```python
import tintype

tintype.initialize()
tintype.snapshot_all_threads(timeout=2.0)
tintype.finalize("/tmp/all_threads.pytb")
```

### Periodic sampling

```python
import tintype

with tintype.sampling(
    interval=0.1,
    mode=tintype.SamplingMode.ALL_THREADS,
    path="/tmp/profile.pytb",
    timeout=2.0,
):
    run_workload()
```

### Exception hook

```python
import tintype

# One-liner: capture a snapshot on any unhandled exception
tintype.install_exception_hook(path="/tmp/crash.pytb")
```

Or with a custom callback:

```python
import tintype

def on_snapshot(path: str) -> None:
    upload_to_crash_service(path)

tintype.install_exception_hook(callback=on_snapshot)
```

## VS Code extension

The Tintype VS Code extension can inspect saved `.pytb` files and capture
snapshots from live Python programs. It uses the `debugpy` debug type for live
capture and the `tintype` debug type for the snapshot viewer.

### Requirements

- VS Code 1.96 or newer.
- Python 3.12 or newer for the extension's snapshot viewer.
- A compatible `tintype` package installed both in the debug target and in the
  interpreter used to open snapshot files.
- `debugpy` for live capture and snappoints.

The extension does not install or modify Python packages automatically. Set
`tintype.pythonPath` if the interpreter containing `tintype` is different from
the active environment selected by the Python extension.

### Inspect a saved snapshot

Right-click a `.pytb` file in the Explorer and select **Open Tintype Snapshot**,
or run the same command from the Command Palette. The snapshot opens as a VS
Code debug session, where the standard CALL STACK and VARIABLES views can be
used to inspect threads, frames, exceptions, and objects.

### Capture from a live program

Start a Python debug session backed by `debugpy`, then use **Tintype: Take
Tintype Snapshot** from the Command Palette or the camera button in the debug
toolbar. Each capture opens a viewer and appears in the **Tintype Snapshots**
view. Additional captures can be taken from the original program, navigated in
the viewer, and saved as a finalized `.pytb` file.

A snappoint captures a snapshot when execution reaches a source line without
leaving the program stopped. Toggle one from the editor gutter or press
`Shift+F9` on a Python line.

Live capture currently expects the debug target and the VS Code extension host
to share a filesystem. Remote-host debugging is not supported yet.

See the [extension documentation](https://github.com/facebookincubator/tintype/tree/main/vscode)
for the complete command and configuration reference.

## API Overview

### Module Functions

| Function | Description |
|----------|-------------|
| `initialize(collect_stats, frame_file_path_filters)` | Initialize the snapshot system. Returns a live `SnapshotReader`. |
| `take_snapshot(traceback_or_exception, ...)` | Capture a snapshot of the current stack, a traceback, or an exception chain. |
| `snapshot_all_threads(timeout, max_frames, ...)` | Capture all Python threads atomically. |
| `finalize(path, metadata, compression_level)` | Compress and write the snapshot file to disk. |
| `enable_sampling(interval, mode, ...)` | Start periodic sampling on a C++ timer thread. |
| `disable_sampling()` | Stop periodic sampling. |
| `sampling(interval, mode, ..., path)` | Context manager: initialize, sample, and finalize. |
| `install_exception_hook(path, callback, ...)` | Install `sys.excepthook` to capture snapshots automatically. |
| `cancel_snapshot()` | Thread-safe cancellation of the current snapshot. |
| `get_stats()` / `reset_stats()` | Performance statistics (when `collect_stats=True`). |

### Classes

| Class | Description |
|-------|-------------|
| `SnapshotReader` | Reads `.pytb` files. Supports iteration, random access, and live reading. |
| `Snapshot` | A single snapshot record with stacktraces and an object map. |
| `Stacktrace` | A thread's call stack or an exception's traceback with frames. |
| `Frame` | A stack frame with file path, function name, line number, and local variables. |
| `SerializedObject` | A deserialized complex object with a custom repr. |
| `SamplingMode` | Enum: `ALL_THREADS` or `SINGLE_THREAD`. |

For the complete API reference, see [PYTHON_API.md](PYTHON_API.md).

## File Format

Tintype uses a custom binary format (`.pytb`) designed for efficient capture and compact storage. See [snapshot_lib/FILE_FORMAT.md](snapshot_lib/FILE_FORMAT.md) for the byte-level specification.

## License

MIT License. See [LICENSE](LICENSE) for details.
