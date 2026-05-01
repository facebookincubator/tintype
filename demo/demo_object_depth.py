# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Demo showing the max_object_depth parameter for limiting object graph depth.

Captures snapshots with varying depth limits to show how objects are
progressively truncated to their repr() as depth decreases.

Usage:
    python -m tintype.demo.demo_object_depth
"""

import os
from typing import Any

import tintype


class Config:
    """A config object with nested settings."""

    def __init__(self, name: str, settings: dict[str, Any] | None = None) -> None:
        self.name = name
        self.settings: dict[str, Any] = settings if settings is not None else {}

    def __repr__(self) -> str:
        return f"Config({self.name!r})"


class Server:
    """A server with nested config and connections."""

    def __init__(self, host: str, port: int, config: Config) -> None:
        self.host = host
        self.port = port
        self.config = config
        self.connections: list[dict[str, Any]] = []

    def __repr__(self) -> str:
        return f"Server({self.host}:{self.port})"


class Application:
    """An application with multiple servers and metadata."""

    def __init__(self, name: str, servers: list[Server]) -> None:
        self.name = name
        self.servers = servers
        self.metadata = {
            "version": "1.0",
            "env": {"region": "us-east", "zone": "a"},
        }

    def __repr__(self) -> str:
        return f"Application({self.name!r})"


def build_app() -> Application:
    """Build a deeply nested application structure."""
    db_config = Config("database", {"host": "db.internal", "pool_size": 10})
    cache_config = Config("cache", {"ttl": 300, "max_entries": 1000})

    db_server = Server("db.internal", 5432, db_config)
    cache_server = Server("cache.internal", 6379, cache_config)
    db_server.connections = [{"client": "app1", "active": True}]

    app = Application("my_service", [db_server, cache_server])
    return app


def capture_with_depth(app: Application, depth: int | None) -> None:
    """Capture a snapshot with a given max_object_depth."""
    local_app = app
    nested_dict = {"level1": {"level2": {"level3": {"level4": "deep"}}}}
    nested_list = [[["innermost"]], [["also_inner"]]]
    _ = (local_app, nested_dict, nested_list)
    tintype.take_snapshot(max_object_depth=depth)


def _print_object_children(obj: object) -> None:
    """Print one level of children for a container or serialized object."""
    if isinstance(obj, dict):
        for k, v in list(obj.items())[:3]:
            v_type = type(v).__name__
            print(f"    [{k!r}]: type={v_type}, repr={v!r:.80}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:3]):
            v_type = type(v).__name__
            print(f"    [{i}]: type={v_type}, repr={v!r:.80}")
    elif isinstance(obj, tintype.SerializedObject):
        for attr_name in sorted(obj.__dict__):
            v = getattr(obj, attr_name)
            v_type = type(v).__name__
            print(f"    .{attr_name}: type={v_type}, repr={v!r:.80}")


def print_snapshot_objects(reader: tintype.SnapshotReader, depth_label: str) -> None:
    """Print the objects from the most recent snapshot."""
    snap = reader.get_latest_snapshot()
    if snap is None:
        print(f"  No snapshot captured for {depth_label}")
        return

    print(f"\n{'=' * 60}")
    print(f"  max_object_depth={depth_label}")
    print(f"{'=' * 60}")

    for st in snap.stacktraces.values():
        if st.object_depth_truncated:
            print("  [object_depth_truncated = True]")
        else:
            print("  [object_depth_truncated = False]")

    # Find our capture frame
    for frame in snap.frames():
        if frame.function_name != "capture_with_depth":
            continue

        locals_dict = frame.get_locals()
        for name in ("local_app", "nested_dict", "nested_list"):
            if name not in locals_dict:
                continue
            obj = locals_dict[name]
            obj_type = type(obj).__name__
            has_attrs = bool(getattr(obj, "__dict__", None))

            print(f"\n  {name}: type={obj_type}, has_attrs={has_attrs}")
            print(f"    repr={obj!r:.100}")
            _print_object_children(obj)
        break


def main() -> None:
    app = build_app()
    output_dir = "/tmp/tintype_depth_demo"
    os.makedirs(output_dir, exist_ok=True)

    # Capture with different depth limits
    for depth in (None, 3, 2, 1, 0):
        depth_str = "unlimited" if depth is None else str(depth)
        path = os.path.join(output_dir, f"depth_{depth_str}.pytb")
        tintype.initialize(collect_stats=True)

        capture_with_depth(app, depth)

        tintype.finalize(path)

        # Read back from file (borrowed reader is invalidated after finalize)
        reader = tintype.SnapshotReader(path)
        depth_label = str(depth) if depth is not None else "None (unlimited)"
        print_snapshot_objects(reader, depth_label)

        # Print stats from file
        stats = reader.get_stats()
        objects_processed = stats.get("totalObjectsProcessed", 0)
        print(f"\n  Stats: {objects_processed} objects processed")
        print(f"  File: {path}")

    print(f"\n{'=' * 60}")
    print(f"  Demo complete. Snapshot files written to {output_dir}/")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
