# Tintype File Format Specification

This document describes the binary file format used by the Python traceback snapshot module.

## Overview

The Tintype file stores snapshots of Python traceback information including:
- Call stack frames with source file references, function names, line numbers, and local variables
- Serialized Python objects from local variables
- Source file contents for referenced files
- User-provided metadata

The file is compressed using zstd. All positions (Pos) are absolute byte offsets from the start of the **uncompressed** data. All offsets (Offset) are relative to a section start (e.g., object heap offsets are relative to the object heap start).

All multi-byte integers are stored in little-endian byte order. All on-disk structs use `#pragma pack(push, 1)` to eliminate compiler-inserted alignment padding. This makes the binary format portable across platforms (e.g., files written on Linux can be read on macOS).

## Constants

| Name           | Value          | Description                       |
|----------------|----------------|-----------------------------------|
| Magic Number   | `0x50595442`   | ASCII "PYTB" (Python TraceBac)    |
| Format Version | `1`            | Current format version            |
| Block Size     | `4096`         | Alignment boundary for sections   |
| None Offset    | `UINT64_MAX-2` | Magic offset for None objects     |
| True Offset    | `UINT64_MAX-1` | Magic offset for True objects     |
| False Offset   | `UINT64_MAX`   | Magic offset for False objects    |

## File Layout

```text
+---------------------------+
|       FileHeader          |  (100 bytes)
+---------------------------+
|       (padding)           |  (aligned to 4096 bytes)
+---------------------------+
|  Snapshot Records Section |  (linked list of snapshots)
+---------------------------+
|      Object Heap          |  (serialized Python objects)
+---------------------------+
|      File Table           |  (source file contents)
+---------------------------+
|   Environment Section     |  (null-terminated KEY=VALUE strings)
+---------------------------+
|    Manifest Section       |  (raw JSON string)
+---------------------------+
|    Metadata Section       |  (raw JSON string)
+---------------------------+
|   Statistics Section      |  (name-value pairs, optional)
+---------------------------+
```

## FileHeader (100 bytes)

| Offset | Size | Type   | Field           | Description                                              |
|--------|------|--------|-----------------|----------------------------------------------------------|
| 0      | 4    | uint32 | magic           | Magic number `0x50595442` ("PYTB")                       |
| 4      | 4    | uint32 | version         | Format version (currently 1)                             |
| 8      | 8    | uint64 | lastSnapshotPos | Absolute position of the most recent snapshot header     |
| 16     | 4    | uint32 | snapshotCount   | Number of snapshot records in the file                   |
| 20     | 8    | uint64 | objectHeapPos   | Absolute position of the start of the object heap        |
| 28     | 8    | uint64 | fileTablePos    | Absolute position of the start of the file table         |
| 36     | 4    | uint32 | fileTableCount  | Number of entries in the file table                      |
| 40     | 8    | uint64 | envPos          | Absolute position of the environment section             |
| 48     | 8    | uint64 | envSize         | Total size of environment data in bytes (0 if none)      |
| 56     | 8    | uint64 | manifestPos     | Absolute position of the manifest section                |
| 64     | 8    | uint64 | manifestSize    | Size of manifest JSON in bytes (0 if no manifest)        |
| 72     | 8    | uint64 | metadataPos     | Absolute position of the metadata section                |
| 80     | 8    | uint64 | metadataSize    | Size of metadata JSON in bytes                           |
| 88     | 8    | uint64 | statsPos        | Absolute position of statistics section (0 if none)      |
| 96     | 4    | uint32 | statsCount      | Number of statistics entries (0 if none)                 |

## Snapshot Records Section

Snapshots are stored as a linked list, with each snapshot pointing to the previous one via `prevSnapshotPos`. To read snapshots chronologically, start from `FileHeader.lastSnapshotPos` and follow the chain backwards.

### SnapshotRecordHeader (33 bytes)

| Offset | Size | Type   | Field           | Description                                              |
|--------|------|--------|-----------------|----------------------------------------------------------|
| 0      | 8    | uint64 | timestamp       | Unix timestamp in microseconds                           |
| 8      | 8    | uint64 | prevSnapshotPos | Absolute position of previous snapshot (0 if first)      |
| 16     | 4    | uint32 | stacktraceCount | Number of stacktrace records following                   |
| 20     | 8    | uint64 | objectMapPos    | Absolute position of this snapshot's object map table    |
| 28     | 4    | uint32 | objectMapCount  | Number of entries in the object map                      |
| 32     | 1    | uint8  | flags           | Bit flags: `0x01` = truncated (snapshot was cancelled)   |

Immediately following the header are `stacktraceCount` stacktrace records, then at `objectMapPos` is the object map table.

### StacktraceRecordHeader (41 bytes)

A stacktrace can represent either a thread's call stack or an exception's traceback.

| Offset | Size | Type   | Field             | Description                                            |
|--------|------|--------|-------------------|--------------------------------------------------------|
| 0      | 8    | uint64 | id                | Stacktrace ID (thread ID for threads, unique for exc.) |
| 8      | 4    | uint32 | frameCount        | Number of frame records following for this stacktrace  |
| 12     | 8    | uint64 | exceptionPythonId | Python id() of exception object (0 if thread)          |
| 20     | 8    | uint64 | causeId           | ID of stacktrace for `__cause__` (equals id if none)   |
| 28     | 8    | uint64 | contextId         | ID of stacktrace for `__context__` (equals id if none) |
| 36     | 1    | uint8  | flags             | Bit flags: `0x01` = truncated (frames were omitted), `0x02` = object depth truncated |
| 37     | 4    | uint32 | threadNameLength  | Length of thread name string following header          |

Immediately after the header, `threadNameLength` bytes of UTF-8 encoded thread name data follows.

For thread stacktraces:
- `id` is the native thread ID (from `threading.get_ident()`)
- `exceptionPythonId` is 0 (no exception)
- `causeId` and `contextId` equal `id` (self-reference means none)
- `threadNameLength` contains the length of the thread name (may be 0 if unknown)

For exception stacktraces:
- `id` is a unique identifier for this exception's stacktrace
- `exceptionPythonId` is the Python id() of the exception object (look up in object map to get heap offset)
- `causeId` references the stacktrace for the exception's `__cause__` (equals `id` if none)
- `contextId` references the stacktrace for the exception's `__context__` (equals `id` if none)
- `threadNameLength` is typically 0 (exception stacktraces don't have thread names)

Each stacktrace record header is immediately followed by `threadNameLength` bytes of thread name, then `frameCount` frame records for that stacktrace.

### Frame Record

Each frame record describes one stack frame in the traceback. Frame records are written field-by-field (not as a struct), so there is no alignment padding between fields.

| Offset    | Size | Type                 | Field            | Description                       |
|-----------|------|----------------------|------------------|-----------------------------------|
| 0         | 4    | uint32               | filePathLength   | Length of file path string         |
| 4         | N    | bytes                | filePath         | File path (UTF-8, not null-term.)  |
| 4+N       | 4    | uint32               | coNameLength     | Length of function name            |
| 8+N       | M    | bytes                | coName           | Function name (UTF-8, not null-t.) |
| 8+N+M     | 4    | uint32               | coQualNameLength | Length of qualified function name  |
| 12+N+M    | Q    | bytes                | coQualName       | Qualified name (UTF-8, not null-t.)|
| 12+N+M+Q  | 4    | uint32               | lineNumber       | Line number in source file         |
| 16+N+M+Q  | 4    | uint32               | localVarCount    | Number of local variable records   |
| 20+N+M+Q  | ...  | LocalVariableRecord[] | localVariables   | Local variables                    |

### Local Variable Record

Each local variable record describes one local variable in a frame. Written field-by-field, no alignment padding.

| Offset | Size | Type   | Field      | Description                         |
|--------|------|--------|------------|-------------------------------------|
| 0      | 8    | uint64 | pythonId   | Python object ID (memory address)   |
| 8      | 4    | uint32 | nameLength | Length of variable name              |
| 12     | N    | bytes  | name       | Variable name (UTF-8, not null-t.)  |

To get the object's data, look up `pythonId` in the snapshot's object map to get the heap offset.

### Object Map Table

Located at `objectMapPos` (absolute position). Contains `objectMapCount` entries.

#### ObjectMapRecord (16 bytes)

| Offset | Size | Type   | Field            | Description                                  |
|--------|------|--------|------------------|----------------------------------------------|
| 0      | 8    | uint64 | pythonId         | Python object ID                             |
| 8      | 8    | uint64 | objectHeapOffset | Offset relative to object heap start         |

## Object Heap

The object heap contains serialized Python objects. Objects are deduplicated across all snapshots using their Python ID (memory address at capture time).

All object heap offsets are **relative to the object heap start** (`FileHeader.objectHeapPos`). To read an object at offset `O`, read from absolute position `FileHeader.objectHeapPos + O`.

### Magic Offsets for Singleton Objects

Some Python objects are singletons and don't need to be written to the heap. Instead, they use special magic offset values at the high end of the uint64 range, which cannot be valid heap offsets.

| Object  | Magic Offset Value | Description             |
|---------|--------------------|-------------------------|
| `None`  | `UINT64_MAX - 2`   | Python None singleton   |
| `True`  | `UINT64_MAX - 1`   | Python True singleton   |
| `False` | `UINT64_MAX`       | Python False singleton  |

When reading an object map entry, check if the `objectHeapOffset` is >= `UINT64_MAX - 2` before attempting to read from the object heap.

### Object Heap Record Header (1 byte)

| Offset | Size | Type   | Field    | Description                         |
|--------|------|--------|----------|-------------------------------------|
| 0      | 1    | uint8  | type     | Object type (see ObjectType enum)   |

The type-specific data immediately follows the 1-byte header.

### Object Types

| Value | Name             | Description                                         |
|-------|------------------|-----------------------------------------------------|
| 0     | Int64            | Integer fitting in signed 64-bit                    |
| 1     | Float            | Python float (64-bit IEEE 754)                      |
| 2     | String           | Python str                                          |
| 3     | Bytes            | Python bytes                                        |
| 4     | List             | Python list                                         |
| 5     | Tuple            | Python tuple                                        |
| 6     | Dict             | Python dict                                         |
| 7     | Set              | Python set or frozenset                             |
| 8     | IntBignum        | Arbitrary precision integer (string repr)           |
| 9     | SerializedObject | Catch-all type with type name + repr + attributes   |
| 10    | SerializedList   | List subclass with list data + serialized data      |
| 11    | SerializedSet    | Set subclass with set data + serialized data        |
| 12    | SerializedTuple  | Tuple subclass with tuple data + serialized data    |
| 13    | SerializedDict   | Dict subclass with dict data + serialized data      |

### Type-Specific Data Formats

#### Int64 (type=0)

| Size | Type  | Description                  |
|------|-------|------------------------------|
| 8    | int64 | Signed 64-bit integer value  |

#### Float (type=1)

| Size | Type   | Description                       |
|------|--------|-----------------------------------|
| 8    | double | IEEE 754 double-precision float   |

#### String (type=2)

| Size | Type   | Description        |
|------|--------|--------------------|
| 4    | uint32 | Length in bytes     |
| N    | bytes  | UTF-8 encoded string |

#### Bytes (type=3)

| Size | Type   | Description    |
|------|--------|----------------|
| 4    | uint32 | Length in bytes |
| N    | bytes  | Raw bytes      |

#### List (type=4)

| Size | Type     | Description            |
|------|----------|------------------------|
| 4    | uint32   | Number of elements     |
| N×8  | uint64[] | Python IDs of elements |

#### Tuple (type=5)
Same format as List.

#### Dict (type=6)

| Size | Type               | Description                                |
|------|--------------------|--------------------------------------------|
| 4    | uint32             | Number of key-value pairs                  |
| N×16 | (uint64, uint64)[] | Pairs of (key pythonId, value pythonId)    |

#### Set (type=7)
Same format as List.

#### IntBignum (type=8)

| Size | Type   | Description                    |
|------|--------|--------------------------------|
| 4    | uint32 | Length of string representation |
| N    | bytes  | Decimal string representation  |

#### SerializedObject (type=9)

| Size | Type               | Description                                          |
|------|--------------------|------------------------------------------------------|
| 4    | uint32             | Type name length                                     |
| N    | bytes              | Type name (UTF-8)                                    |
| 4    | uint32             | Repr length                                          |
| M    | bytes              | repr() output (UTF-8)                                |
| 4    | uint32             | Attribute count                                      |
| K×16 | (uint64, uint64)[] | Pairs of (attr name pythonId, attr value pythonId)   |

#### SerializedList (type=10)
List data followed by serialized object data:

| Size | Type               | Description                                          |
|------|--------------------|------------------------------------------------------|
| 4    | uint32             | Element count                                        |
| N×8  | uint64[]           | Python IDs of elements                               |
| 4    | uint32             | Type name length                                     |
| M    | bytes              | Type name (UTF-8)                                    |
| 4    | uint32             | Repr length                                          |
| P    | bytes              | repr() output (UTF-8)                                |
| 4    | uint32             | Attribute count                                      |
| K×16 | (uint64, uint64)[] | Pairs of (attr name pythonId, attr value pythonId)   |

#### SerializedSet (type=11)
Same format as SerializedList.

#### SerializedTuple (type=12)
Same format as SerializedList.

#### SerializedDict (type=13)
Dict data followed by serialized object data:

| Size | Type               | Description                                          |
|------|--------------------|------------------------------------------------------|
| 4    | uint32             | Entry count                                          |
| N×16 | (uint64, uint64)[] | Pairs of (key pythonId, value pythonId)              |
| 4    | uint32             | Type name length                                     |
| M    | bytes              | Type name (UTF-8)                                    |
| 4    | uint32             | Repr length                                          |
| P    | bytes              | repr() output (UTF-8)                                |
| 4    | uint32             | Attribute count                                      |
| K×16 | (uint64, uint64)[] | Pairs of (attr name pythonId, attr value pythonId)   |

## File Table

The file table stores the contents of source files referenced in frame records. Frame records store the file path directly, so the file table is used only for retrieving file contents (e.g., when the original source file is no longer available).

### FileTableRecord

Each record consists of the `FileTableRecordHeader` struct followed by the path and content data.

| Offset | Size | Type   | Field         | Description                          |
|--------|------|--------|---------------|--------------------------------------|
| 0      | 4    | uint32 | pathLength    | Length of file path                  |
| 4      | 4    | uint32 | contentLength | Length of file contents               |
| 8      | N    | bytes  | path          | File path (UTF-8, not null-term.)    |
| 8+N    | M    | bytes  | content       | File contents (UTF-8)                |

To look up a file's contents by path:
1. Start at `FileHeader.fileTablePos`
2. Iterate through all `fileTableCount` entries
3. Compare each entry's path with the desired path
4. Or, build a path-to-content map on first read by scanning all entries

## Environment Section

The environment section is written after the file table during finalize. It contains the process environment variables at the time of finalization, captured using `extern char **environ`. Each variable is stored as a null-terminated `KEY=VALUE` string, concatenated back-to-back. The total size of all entries (including null terminators) is stored in `FileHeader.envSize`.

If the environment is empty, `envSize` in the FileHeader is 0 and no environment data is written.

```text
+---------------------+-----+---------------------+-----+-----+
| KEY1=value1         | \0  | KEY2=value2         | \0  | ... |
+---------------------+-----+---------------------+-----+-----+
```

To read the environment:
1. Read `envSize` bytes starting at `envPos`
2. Split on null bytes to get individual `KEY=VALUE` strings
3. Split each string on the first `=` to separate key and value

## Manifest Section

The manifest section is written after the environment section during finalize. It contains the contents of `__manifest__.json` from the Python runtime path, if available. This file is present in Buck-built PAR files and lists all modules and their source paths.

If no manifest is found (e.g., running outside a PAR file), `manifestSize` in the FileHeader is 0 and no manifest data is written.

The section contains the raw UTF-8 encoded JSON string. The size is stored in `FileHeader.manifestSize`.

## Metadata Section

The metadata section is written after the manifest section during finalize.

The section contains the raw UTF-8 encoded JSON string. The size is stored in `FileHeader.metadataSize`.

## Statistics Section

The statistics section is written after the metadata section during finalize, only if statistics collection was enabled during `initialize(collect_stats=True)`. It contains timing and performance statistics gathered during snapshot capture.

If statistics collection was not enabled, `statsPos` and `statsCount` in the FileHeader are 0 and no statistics data is written.

### Statistics Entry Format

Each entry is written sequentially as:

| Size | Type   | Description                          |
|------|--------|--------------------------------------|
| 8    | uint64 | Statistic value (nanoseconds or count) |
| 4    | uint32 | Name length                          |
| N    | bytes  | Statistic name (UTF-8, not null-term.) |

Statistics include timing information (in nanoseconds) for various operations:
- `initializeTimeNs`, `finalizeTimeNs`, `totalSnapshotTimeNs`
- `writeFrameRecordTimeNs`
- `objectLookupTimeNs`, `objectProcessingTimeNs`, `reprTimeNs`
- Per-object-type statistics: `{TypeName}Count`, `{TypeName}TimeNs`, `{TypeName}Bytes`

To read the statistics:
1. Check if `statsCount` > 0
2. Read `statsCount` entries starting at `statsPos`
3. For each entry: read value (8 bytes), name length (4 bytes), name (variable)

## Reading the File

### Decompression
The entire file is zstd compressed. Decompress the full file before parsing.

### Output Compaction
During writing, the working file has a zero-filled gap between the snapshot records section and the object heap (used to allow the snapshot records section to grow without relocating the heap). When the output file is produced, this gap is stripped: the snapshot records are immediately followed by the object heap data, and the `FileHeader` position fields (`objectHeapPos`, `fileTablePos`, `envPos`, `manifestPos`, `metadataPos`, `statsPos`) are adjusted to reflect the compacted layout. The `lastSnapshotPos` and all positions within snapshot records (`prevSnapshotPos`, `objectMapPos`) are not adjusted because they fall before the gap. Readers see a contiguous file with no gap.

### Reading Snapshots
1. Read `FileHeader` from offset 0
2. Validate magic number (`0x50595442`) and version (1)
3. Go to `lastSnapshotPos` to read the most recent snapshot
4. Follow `prevSnapshotPos` chain to read older snapshots

### Reading a Snapshot's Objects
1. Parse frame records to collect `pythonId` values from local variables
2. Read object map table at `objectMapPos` (contains `objectMapCount` entries)
3. Build a map of pythonId → heap offset
4. Read object data from the object heap:
   - For magic offsets (>= `UINT64_MAX - 2`), return the singleton value directly
   - For normal offsets, read from `FileHeader.objectHeapPos + offset`

### Reading Source Files
1. Go to `fileTablePos`
2. Read `fileTableCount` file records sequentially
3. Build a path-to-content map by scanning all entries

## Example: Reading a Snapshot

```python
import struct
import zstandard

def read_snapshot_file(path):
    with open(path, 'rb') as f:
        compressed = f.read()

    dctx = zstandard.ZstdDecompressor()
    data = dctx.decompress(compressed)

    # Read FileHeader (100 bytes, packed)
    magic, version = struct.unpack_from('<II', data, 0)
    assert magic == 0x50595442, "Invalid magic number"
    assert version == 1, f"Unsupported version: {version}"

    (last_snapshot_pos, snapshot_count,
     object_heap_pos, file_table_pos,
     file_table_count, env_pos, env_size,
     manifest_pos, manifest_size,
     metadata_pos, metadata_size,
     stats_pos, stats_count) = struct.unpack_from(
        '<QIQIQQQQQQQI', data, 8)

    # Read metadata (raw JSON string, size from header)
    metadata_json = data[metadata_pos:metadata_pos + metadata_size]

    # Read most recent snapshot (32-byte header)
    pos = last_snapshot_pos
  timestamp, prev_pos, stacktrace_count, obj_map_pos, obj_map_count, flags = \
        struct.unpack_from('<QQIQIB', data, pos)
    pos += 33  # sizeof(SnapshotRecordHeader)
    is_truncated = (flags & 0x01) != 0

    # Read stacktraces (37-byte header + thread name + frames)
    for s in range(stacktrace_count):
        id, frame_count, exception_python_id, cause_id, context_id, st_flags, \
            thread_name_length = struct.unpack_from('<QIQQQ BI', data, pos)
        pos += 41  # sizeof(StacktraceRecordHeader)
        st_is_truncated = (st_flags & 0x01) != 0
        st_object_depth_truncated = (st_flags & 0x02) != 0
        thread_name = data[pos:pos + thread_name_length].decode('utf-8')
        pos += thread_name_length

        # Read frames for this stacktrace
        for f in range(frame_count):
            file_path_length = struct.unpack_from('<I', data, pos)[0]
            pos += 4
            file_path = data[pos:pos + file_path_length].decode('utf-8')
            pos += file_path_length

            co_name_length = struct.unpack_from('<I', data, pos)[0]
            pos += 4
            co_name = data[pos:pos + co_name_length].decode('utf-8')
            pos += co_name_length

            co_qualname_length = struct.unpack_from('<I', data, pos)[0]
            pos += 4
            co_qualname = data[pos:pos + co_qualname_length].decode('utf-8')
            pos += co_qualname_length

            line_number, local_var_count = struct.unpack_from('<II', data, pos)
            pos += 8

            # Read local variables
            for v in range(local_var_count):
                python_id = struct.unpack_from('<Q', data, pos)[0]
                pos += 8
                name_length = struct.unpack_from('<I', data, pos)[0]
                pos += 4
                var_name = data[pos:pos + name_length].decode('utf-8')
                pos += name_length

    # Read objects from heap
    # Object heap record header is 1 byte: uint8 type
    # For a given objectHeapOffset from the object map:
    # - If offset >= UINT64_MAX - 2: it's a magic offset (None/True/False)
    # - Otherwise: read from data[object_heap_pos + offset]
```
