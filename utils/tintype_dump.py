#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""
CLI tool that reads a .pytb snapshot file and dumps its contents as JSON.

The output mirrors the binary file format structure: metadata, manifest,
environment, snapshots (with object maps), a deduplicated object heap,
embedded source files, and optional statistics.

Usage:
    python -m tintype.utils.tintype_dump /path/to/tintype.pytb
    python -m tintype.utils.tintype_dump --metadata-only /path/to/tintype.pytb
    python -m tintype.utils.tintype_dump --source-files /path/to/tintype.pytb
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any

import tintype

# ObjectType enum values from SnapshotTypes.h
_TYPE_NAMES: dict[int, str] = {
    0: "Int64",
    1: "Float",
    2: "String",
    3: "Bytes",
    4: "List",
    5: "Tuple",
    6: "Dict",
    7: "Set",
    8: "IntBignum",
    9: "SerializedObject",
    10: "SerializedList",
    11: "SerializedSet",
    12: "SerializedTuple",
    13: "SerializedDict",
}


def _format_heap_object(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert a raw object dict from _read_raw_object into per-type JSON
    matching the binary serialization format."""
    type_int = raw.get("type", -1)
    type_name = _TYPE_NAMES.get(type_int, f"Unknown({type_int})")
    value = raw.get("value")

    result: dict[str, Any] = {"type": type_name}

    if type_int in (0, 1):  # Int64, Float
        result["value"] = value
    elif type_int == 2:  # String
        result["value"] = value
    elif type_int == 3:  # Bytes
        result["value"] = repr(value) if isinstance(value, bytes) else value
    elif type_int in (4, 5, 7):  # List, Tuple, Set
        result["elements"] = value
    elif type_int == 6:  # Dict
        result["entries"] = [[k, v] for k, v in value] if value else []
    elif type_int == 8:  # IntBignum
        result["value"] = value
    elif type_int == 9:  # SerializedObject
        result["type_name"] = raw.get("type_name", "")
        result["repr"] = raw.get("repr", "")
        attrs = raw.get("attributes")
        if attrs:
            result["attributes"] = [[n, v] for n, v in attrs]
    elif type_int in (10, 11, 12):  # SerializedList, SerializedSet, SerializedTuple
        result["type_name"] = raw.get("type_name", "")
        result["repr"] = raw.get("repr", "")
        result["elements"] = value
        attrs = raw.get("attributes")
        if attrs:
            result["attributes"] = [[n, v] for n, v in attrs]
    elif type_int == 13:  # SerializedDict
        result["type_name"] = raw.get("type_name", "")
        result["repr"] = raw.get("repr", "")
        result["entries"] = [[k, v] for k, v in value] if value else []
        attrs = raw.get("attributes")
        if attrs:
            result["attributes"] = [[n, v] for n, v in attrs]

    return result


def dump(
    reader: tintype.SnapshotReader,
    include_source_files: bool = False,
    metadata_only: bool = False,
) -> dict[str, Any]:
    """Build a JSON-serializable dict representing the tintype file contents."""
    result: dict[str, Any] = {}

    # Metadata
    raw_meta = reader.get_metadata()
    if raw_meta:
        try:
            result["metadata"] = json.loads(raw_meta)
        except json.JSONDecodeError:
            result["metadata"] = raw_meta
    else:
        result["metadata"] = None

    # Manifest
    raw_manifest = reader.get_manifest()
    if raw_manifest:
        try:
            result["manifest"] = json.loads(raw_manifest)
        except json.JSONDecodeError:
            result["manifest"] = raw_manifest
    else:
        result["manifest"] = None

    if metadata_only:
        return result

    # Environment
    result["environment"] = reader.get_environment()

    # Snapshots
    snapshots = reader.get_all_snapshots()
    all_offsets: set[int] = set()
    snap_list: list[dict[str, Any]] = []

    for snap in snapshots:
        ts_us = snap.timestamp
        dt = datetime.fromtimestamp(ts_us / 1_000_000, tz=timezone.utc)

        snap_data: dict[str, Any] = {
            "timestamp_us": ts_us,
            "timestamp": dt.isoformat(),
            "truncated": snap.truncated,
        }

        # Stacktraces
        st_list: list[dict[str, Any]] = []
        for st_id, st in snap.stacktraces.items():
            has_exc = st.exception_object is not None
            st_data: dict[str, Any] = {
                "id": st_id,
                "type": "exception" if has_exc else "thread",
                "thread_name": st.thread_name or None,
                "truncated": st.truncated,
                "exception_repr": repr(st.exception_object) if has_exc else None,
                "cause_id": st.cause_id,
                "context_id": st.context_id,
            }

            # Frames
            frames: list[dict[str, Any]] = []
            for frame in st.frames:
                local_vars: dict[str, int] = {}
                for lv in frame._local_variables:
                    local_vars[lv.name] = lv.python_id

                frames.append(
                    {
                        "file": frame.original_file_path,
                        "line": frame.line_number,
                        "function": frame.function_name,
                        "qualname": frame.function_qualname,
                        "locals": local_vars,
                    }
                )
            st_data["frames"] = frames
            st_list.append(st_data)

        snap_data["stacktraces"] = st_list

        # Object map
        obj_map: dict[str, Any] = {}
        for python_id, offset in snap.object_map.items():
            if tintype.SnapshotReader.is_magic_offset(offset):
                obj_map[str(python_id)] = (
                    tintype.SnapshotReader.get_magic_offset_type(offset) or "unknown"
                )
            else:
                obj_map[str(python_id)] = offset
                all_offsets.add(offset)
        snap_data["object_map"] = obj_map

        snap_list.append(snap_data)

    result["snapshots"] = snap_list

    # Object heap (deduplicated across all snapshots)
    heap: dict[str, Any] = {}
    for offset in sorted(all_offsets):
        obj = reader._read_raw_object(offset)
        if obj is not None and isinstance(obj, dict):
            heap[str(offset)] = _format_heap_object(obj)
    result["object_heap"] = heap

    # Source files
    sf_list: list[dict[str, Any]] = []
    for sf in reader.get_all_source_files():
        entry: dict[str, Any] = {"path": sf.path}
        if include_source_files:
            entry["content"] = sf.content
        sf_list.append(entry)
    result["source_files"] = sf_list

    # Stats
    stats = reader.get_stats()
    if stats:
        result["stats"] = stats

    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dump the contents of a .pytb snapshot file as JSON.",
    )
    parser.add_argument(
        "snapshot_file",
        help="Path to the .pytb snapshot file",
    )
    parser.add_argument(
        "--source-files",
        action="store_true",
        help="Include embedded source file contents in the output",
    )
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="Print only the metadata and manifest sections",
    )
    args = parser.parse_args()

    try:
        reader = tintype.SnapshotReader(args.snapshot_file)
    except RuntimeError as e:
        print(f"Error: Could not open snapshot file: {e}", file=sys.stderr)
        sys.exit(1)
        raise  # Unreachable, but helps type checker know this branch never continues

    error = reader.get_last_error()
    if error:
        print(f"Warning: {error}", file=sys.stderr)

    result = dump(
        reader,
        include_source_files=args.source_files,
        metadata_only=args.metadata_only,
    )
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
