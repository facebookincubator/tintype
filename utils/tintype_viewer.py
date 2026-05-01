#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""
Interactive curses-based viewer for snapshot files.

Usage:
    python -m tintype.utils.tintype_viewer /path/to/tintype.pytb
"""

from __future__ import annotations

import curses
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import tintype


@dataclass
class ViewerAction:
    """An extensible action that can be triggered by a hotkey in the viewer."""

    key: str  # single character, e.g. "d"
    label: str  # display label, e.g. "Debug"
    views: list[str]  # which views this action appears in, e.g. ["snapshots", "frame"]
    callback: Callable[[SnapshotViewer], None]  # receives the viewer instance


def format_timestamp(timestamp_us: int) -> str:
    """Convert microsecond timestamp to human-readable format."""
    dt = datetime.fromtimestamp(timestamp_us / 1_000_000)
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")


class SnapshotViewer:
    """Curses-based interactive viewer for snapshot files."""

    def __init__(
        self,
        path: str,
        actions: list[ViewerAction] | None = None,
    ) -> None:
        self.path = path
        self.reader = tintype.SnapshotReader(path)
        self.snapshots: list[tintype.Snapshot] = self.reader.get_all_snapshots()
        self.source_files: list[tintype.SourceFile] = self.reader.get_all_source_files()
        self._actions: list[ViewerAction] = actions or []

        # View stack for navigation (each entry is a view state)
        self.view_stack: list[dict[str, Any]] = []
        # Current view: "main", "snapshots", "frame",
        #               "metadata", "manifest", "environment", "statistics",
        #               "sources", "source_content", "traceback"
        self.current_view: str = "main"
        self.cursor: int = 0
        self.scroll_offset: int = 0

        # Selected items
        self.selected_snapshot: tintype.Snapshot | None = None
        self.selected_snapshot_idx: int = 0
        self.selected_stacktrace: Any = None
        self.selected_stacktrace_id: int = 0
        self.selected_frame: tintype.Frame | None = None
        self.selected_frame_idx: int = 0
        self.selected_source_file: tintype.SourceFile | None = None

        # Tree view state: which snapshots are expanded
        self.expanded_snapshots: set[int] = set()

        # Tree view state: which frames are expanded (by frame index)
        self.expanded_frames: set[int] = set()

        # Lines for scrollable content views
        self.content_lines: list[str] = []

        # Highlighted line number (1-based) in source_content view, or None
        self.highlight_line: int | None = None

        # Status message
        self.status_message: str = ""

        # Terminal width (updated each draw cycle)
        self._last_width: int = 80

        # stdscr reference (set during run)
        self._stdscr: Any = None

    def _save_state(self) -> dict[str, Any]:
        return {
            "view": self.current_view,
            "cursor": self.cursor,
            "scroll_offset": self.scroll_offset,
            "content_lines": self.content_lines,
        }

    def _restore_state(self, state: dict[str, Any]) -> None:
        self.current_view = state["view"]
        self.cursor = state["cursor"]
        self.scroll_offset = state["scroll_offset"]
        self.content_lines = state["content_lines"]

    def _push_view(self, new_view: str) -> None:
        self.view_stack.append(self._save_state())
        self.current_view = new_view
        self.cursor = 0
        self.scroll_offset = 0
        self.content_lines = []

    def _pop_view(self) -> bool:
        if self.view_stack:
            self._restore_state(self.view_stack.pop())
            return True
        return False

    def _get_main_menu_items(self) -> list[tuple[str, str]]:
        """Return (label, description) pairs for the main menu."""
        items = []
        items.append(
            (
                f"Snapshots ({len(self.snapshots)})",
                "Browse all snapshots",
            )
        )
        items.append(
            (
                "Metadata",
                "View snapshot metadata",
            )
        )
        items.append(
            (
                "Manifest",
                "View PAR manifest (__manifest__.json)",
            )
        )
        items.append(
            (
                "Environment",
                "View process environment variables",
            )
        )
        items.append(
            (
                "Statistics",
                "View capture statistics",
            )
        )
        items.append(
            (
                f"Source Files ({len(self.source_files)})",
                "Browse embedded source files",
            )
        )
        return items

    def _get_snapshot_tree_items(
        self,
    ) -> list[tuple[str, str, str, int, int | None]]:
        """Build a flat list of tree items for the snapshot/stacktrace tree.

        Returns a list of (label, description, kind, snapshot_idx, st_id) tuples.
        kind is "snapshot" or "stacktrace".
        st_id is the stacktrace id for stacktrace items, None for snapshot items.

        Exception stacktraces are displayed in a nested cause/context hierarchy.
        """
        items: list[tuple[str, str, str, int, int | None]] = []
        for i, snap in enumerate(self.snapshots):
            ts = format_timestamp(snap.timestamp)
            stacktrace_count = len(snap.stacktraces)
            expanded = i in self.expanded_snapshots
            arrow = "▼" if expanded else "▶"
            truncated = " [TRUNCATED]" if snap.truncated else ""
            label = f"{arrow} Snapshot {i + 1}  [{ts}]{truncated}"
            desc = f"{stacktrace_count} stacktrace(s)"
            items.append((label, desc, "snapshot", i, None))
            if expanded:
                # Separate exception and thread stacktraces
                exception_sts: dict[int, tintype.Stacktrace] = {}
                thread_sts: list[tuple[int, tintype.Stacktrace]] = []
                for st_id, st in snap.stacktraces.items():
                    if st.exception_object is not None:
                        exception_sts[st_id] = st
                    else:
                        thread_sts.append((st_id, st))

                # Find which exception IDs are referenced as children
                child_ids: set[int] = set()
                for st in exception_sts.values():
                    if st.cause_id is not None and st.cause_id in exception_sts:
                        child_ids.add(st.cause_id)
                    if st.context_id is not None and st.context_id in exception_sts:
                        child_ids.add(st.context_id)

                # Root exceptions: not referenced by any other exception
                root_ids = [st_id for st_id in exception_sts if st_id not in child_ids]

                # Walk each root's cause/context chain
                for root_id in root_ids:
                    self._append_exception_chain(
                        items, i, exception_sts, root_id, depth=0
                    )

                # Thread stacktraces displayed flat after exceptions
                for st_id, st in thread_sts:
                    trunc = " [TRUNCATED]" if st.truncated else ""
                    # Display thread name if available, otherwise fall back to thread ID
                    if st.thread_name:
                        st_label = f"    Thread: {st.thread_name}{trunc}"
                    else:
                        st_label = f"    Thread {st_id}{trunc}"
                    st_desc = f"{len(st.frames)} frame(s)"
                    items.append((st_label, st_desc, "stacktrace", i, st_id))
        return items

    def _append_exception_chain(
        self,
        items: list[tuple[str, str, str, int, int | None]],
        snapshot_idx: int,
        exception_sts: dict[int, "tintype.Stacktrace"],
        st_id: int,
        depth: int,
        connector: str = "",
        visited: set[int] | None = None,
    ) -> None:
        """Recursively append exception stacktraces following cause/context chains."""
        if visited is None:
            visited = set()
        if st_id in visited:
            return
        visited.add(st_id)

        st = exception_sts[st_id]
        exc_repr = repr(st.exception_object)
        if len(exc_repr) > 60:
            exc_repr = exc_repr[:57] + "..."
        trunc = " [TRUNCATED]" if st.truncated else ""
        frame_desc = f"{len(st.frames)} frame(s)"

        indent = "    " + "   " * depth
        if connector:
            st_label = f"{indent}\u2514 {connector} {exc_repr}{trunc}"
        else:
            st_label = f"{indent}{exc_repr}{trunc}"

        items.append((st_label, frame_desc, "stacktrace", snapshot_idx, st_id))

        # Follow cause chain first, then context chain
        if st.cause_id is not None and st.cause_id in exception_sts:
            self._append_exception_chain(
                items,
                snapshot_idx,
                exception_sts,
                st.cause_id,
                depth + 1,
                connector="caused by:",
                visited=visited,
            )
        elif st.context_id is not None and st.context_id in exception_sts:
            self._append_exception_chain(
                items,
                snapshot_idx,
                exception_sts,
                st.context_id,
                depth + 1,
                connector="during handling of:",
                visited=visited,
            )

    def _get_frame_tree_items(
        self,
    ) -> list[tuple[str, str, str, int]]:
        """Build a flat list of tree items for the frame/locals tree.

        Returns a list of (label, description, kind, frame_idx) tuples.
        kind is "frame" or "local".
        frame_idx is the index of the frame this item belongs to.
        """
        selected_stacktrace = self.selected_stacktrace
        selected_snapshot = self.selected_snapshot
        if selected_stacktrace is None or selected_snapshot is None:
            return []
        items: list[tuple[str, str, str, int]] = []
        n_frames = len(selected_stacktrace.frames)
        for i in range(n_frames):
            frame = selected_stacktrace.frames[i]
            short_path = os.path.basename(frame.file_path)
            expanded = i in self.expanded_frames
            arrow = "▼" if expanded else "▶"
            locals_dict = frame.get_locals()
            n_vars = len(locals_dict)
            label = f"{arrow} #{i}  {frame.function_name}"
            desc = f"{short_path}:{frame.line_number}"
            if n_vars:
                desc += f"  ({n_vars} locals)"
            items.append((label, desc, "frame", i))
            if expanded:
                if not locals_dict:
                    items.append(("      (no local variables)", "", "local", i))
                else:
                    for name, value in locals_dict.items():
                        val = repr(value)
                        if len(val) > 200:
                            val = val[:197] + "..."
                        var_label = f"      {name} = {val}"
                        items.append((var_label, "", "local", i))
        return items

    def _build_metadata_lines(self) -> list[str]:
        raw = self.reader.get_metadata()
        try:
            parsed = json.loads(raw)
            formatted = json.dumps(parsed, indent=2)
        except json.JSONDecodeError:
            formatted = raw
        lines = ["Metadata:", ""]
        lines.extend(formatted.splitlines())
        return lines

    def _build_manifest_lines(self) -> list[str]:
        raw = self.reader.get_manifest()
        if not raw:
            return ["Manifest:", "", "(no manifest captured)"]
        try:
            parsed = json.loads(raw)
            formatted = json.dumps(parsed, indent=2)
        except json.JSONDecodeError:
            formatted = raw
        lines = ["Manifest:", ""]
        lines.extend(formatted.splitlines())
        return lines

    def _build_environment_lines(self) -> list[str]:
        env = self.reader.get_environment()
        if not env:
            return ["Environment:", "", "(no environment captured)"]
        lines = ["Environment:", ""]
        for key in sorted(env):
            lines.append(f"  {key}={env[key]}")
        return lines

    def _format_ns_to_ms(self, ns: int) -> str:
        """Convert nanoseconds to a human-readable milliseconds string."""
        return f"{ns / 1_000_000:.3f} ms"

    def _build_statistics_lines(self) -> list[str]:
        """Build display lines for statistics view."""
        stats: dict[str, int] = self.reader.get_stats()
        if not stats:
            return ["Statistics:", "", "(no statistics captured)"]

        lines: list[str] = ["Statistics:", ""]

        # Group stats by category
        general_timing = [
            "snapshotCount",
            "totalSnapshotTimeNs",
            "initializeTimeNs",
            "finalizeTimeNs",
        ]
        frame_timing = [
            "writeFrameRecordTimeNs",
            "totalFrameCount",
            "framesFiltered",
            "totalObjectsProcessed",
            "snapshotsDiscarded",
        ]
        object_queue_timing = [
            "objectLookupTimeNs",
            "objectProcessingTimeNs",
            "reprTimeNs",
            "slotsTimeNs",
            "attrAccessTimeNs",
            "classMembersTimeNs",
            "objectsSkipped",
            "objectsCacheHit",
            "stringBytesCacheHit",
            "errors",
        ]
        finalize_timing = [
            "finalizeFileTableTimeNs",
            "finalizeEnvironmentTimeNs",
            "finalizeManifestTimeNs",
            "finalizeMetadataTimeNs",
            "finalizeMsyncTimeNs",
            "finalizeCompressionTimeNs",
            "finalizeOutputFileTimeNs",
            "finalizeCleanupTimeNs",
            "finalizeUncompressedDataSize",
            "finalizeCompressedDataSize",
            "finalizeFileCount",
            "fileExtensionTimeNs",
            "fileExtensionBytes",
            "fileExtensionCount",
        ]

        used_keys: set[str] = set()

        def add_section(title: str, keys: list[str]) -> None:
            section_lines: list[str] = []
            for key in keys:
                if key in stats:
                    used_keys.add(key)
                    value = stats[key]
                    if key.endswith("Ns"):
                        display_value = self._format_ns_to_ms(value)
                        display_name = key[:-2]  # Remove "Ns" suffix
                    elif key.endswith("Bytes"):
                        display_value = f"{value:,} bytes"
                        display_name = key
                    elif key.endswith("Count") or key.endswith("Size"):
                        display_value = f"{value:,}"
                        display_name = key
                    else:
                        display_value = f"{value:,}"
                        display_name = key
                    section_lines.append(f"  {display_name}: {display_value}")
            if section_lines:
                lines.append(f"[{title}]")
                lines.extend(section_lines)
                lines.append("")

        add_section("General", general_timing)
        add_section("Frame Walking", frame_timing)
        add_section("Object Queue", object_queue_timing)
        add_section("Finalize", finalize_timing)

        # Per-type stats use format: objectType_<TypeName>_count, objectType_<TypeName>_totalTimeNs, objectType_<TypeName>_totalBytes
        # Extract type names from keys like "objectType_Int64_count"
        type_names: set[str] = set()
        for key in stats:
            if key.startswith("objectType_") and key not in used_keys:
                # Parse "objectType_<TypeName>_<suffix>"
                parts = key.split("_", 2)  # ["objectType", "<TypeName>", "<suffix>"]
                if len(parts) == 3:
                    type_names.add(parts[1])

        if type_names:
            lines.append("[Per-Type Breakdown]")
            for type_name in sorted(type_names):
                type_lines: list[str] = []
                for suffix, label in [
                    ("count", "count"),
                    ("totalTimeNs", "time"),
                    ("totalBytes", "bytes"),
                ]:
                    key = f"objectType_{type_name}_{suffix}"
                    if key in stats:
                        used_keys.add(key)
                        value = stats[key]
                        if suffix == "totalTimeNs":
                            type_lines.append(f"{label}={self._format_ns_to_ms(value)}")
                        elif suffix == "totalBytes":
                            type_lines.append(f"{label}={value:,}")
                        else:
                            type_lines.append(f"{label}={value:,}")
                if type_lines:
                    lines.append(f"  {type_name}: {', '.join(type_lines)}")
            lines.append("")

        # Any remaining stats not in known categories
        remaining = [k for k in stats if k not in used_keys]
        if remaining:
            lines.append("[Other]")
            for key in sorted(remaining):
                value = stats[key]
                if key.endswith("Ns"):
                    display_value = self._format_ns_to_ms(value)
                else:
                    display_value = f"{value:,}"
                lines.append(f"  {key}: {display_value}")
            lines.append("")

        return lines

    def _truncate_path(self, path: str, max_len: int) -> str:
        """Truncate a path from the front so the filename is always visible."""
        if len(path) <= max_len:
            return path
        return "..." + path[-(max_len - 3) :]

    def _build_source_items(self) -> list[tuple[str, str]]:
        items = []
        for sf in self.source_files:
            n_lines = sf.content.count("\n")
            desc = f"{n_lines} lines"
            # Reserve space for prefix " > ", separator "  — ", and description
            # so the path gets truncated to fit while keeping the filename visible
            overhead = 3 + 4 + len(desc)  # " > " + "  — " + desc
            max_path = max(20, self._last_width - overhead - 1)
            label = self._truncate_path(sf.path, max_path)
            items.append((label, desc))
        return items

    def _build_source_content_lines(self) -> list[str]:
        if self.selected_source_file is None:
            return ["No source file selected."]
        sf = self.selected_source_file
        # Truncate path in header, reserving space for "File: " prefix
        display_path = self._truncate_path(sf.path, max(20, self._last_width - 7))
        lines = [f"File: {display_path}", ""]
        for i, line in enumerate(sf.content.splitlines(), 1):
            marker = "►" if i == self.highlight_line else " "
            lines.append(f"{marker}{i:5d} | {line}")
        return lines

    def _build_traceback_lines(self) -> list[str]:
        """Format the selected stacktrace as a Python-style traceback.

        For exception stacktraces, walks the cause/context chain and displays
        the full chain in Python's standard format (deepest cause first).
        For thread stacktraces, displays a simple traceback of the call stack.
        """
        st = self.selected_stacktrace
        if st is None:
            return ["No stacktrace selected."]

        # Build a source-line lookup: path -> list of lines (0-indexed).
        # Embedded source files are indexed first; disk reads are cached
        # on miss since frame paths point to real filesystem locations.
        source_lookup: dict[str, list[str]] = {}
        for sf in self.source_files:
            source_lookup[sf.path] = sf.content.splitlines()
        source_lookup_misses: set[str] = set()

        def _get_source_lines(path: str) -> list[str] | None:
            if path in source_lookup:
                return source_lookup[path]
            if path in source_lookup_misses:
                return None
            try:
                with open(path) as f:
                    lines = f.read().splitlines()
                source_lookup[path] = lines
                return lines
            except OSError:
                source_lookup_misses.add(path)
                return None

        snap = self.selected_snapshot
        is_exception = st.exception_object is not None

        if is_exception and snap is not None:
            # Collect the exception chain: walk cause_id / context_id
            # Each entry is (stacktrace, link_type) where link_type is how
            # this exception relates to the NEXT one in the list.
            chain: list[tuple[tintype.Stacktrace, str]] = []
            visited: set[int] = set()
            current = st
            while current is not None and current.id not in visited:
                visited.add(current.id)
                next_st = None
                link_type = ""
                cause = current.get_cause()
                if cause is not None:
                    next_st = cause
                    link_type = "cause"
                else:
                    context = current.get_context()
                    if context is not None:
                        next_st = context
                        link_type = "context"
                chain.append((current, link_type))
                current = next_st

            # Reverse: Python displays deepest cause first
            chain.reverse()

            lines: list[str] = []
            for idx, (exc_st, _link_type) in enumerate(chain):
                if idx > 0:
                    # After reversal, chain[idx][1] holds the link_type
                    # that describes how chain[idx] relates to chain[idx-1]
                    # (its cause/context).
                    cur_link = chain[idx][1]
                    lines.append("")
                    if cur_link == "cause":
                        lines.append(
                            "The above exception was the direct cause "
                            "of the following exception:"
                        )
                    else:
                        lines.append(
                            "During handling of the above exception, "
                            "another exception occurred:"
                        )
                    lines.append("")

                lines.append("Traceback (most recent call last):")
                for frame in exc_st.frames:
                    lines.append(
                        f'  File "{frame.file_path}", '
                        f"line {frame.line_number}, "
                        f"in {frame.function_name}"
                    )
                    file_lines = _get_source_lines(frame.file_path)
                    if file_lines and 1 <= frame.line_number <= len(file_lines):
                        src = file_lines[frame.line_number - 1].strip()
                        if src:
                            lines.append(f"    {src}")
                if exc_st.truncated:
                    lines.append("  [Truncated — some frames may have been omitted]")

                # Exception line — format as "ExcType: message"
                exc = exc_st.exception_object
                exc_type_name = type(exc).__name__
                if exc_type_name in (
                    "SerializedObject",
                    "SerializedListObject",
                ):
                    # Serialized exception: repr is e.g. "ValueError('msg')"
                    # Parse into "ValueError: msg" (Python traceback fmt)
                    exc_repr = repr(exc)
                    paren = exc_repr.find("(")
                    if paren > 0:
                        type_name = exc_repr[:paren]
                        args = exc_repr[paren + 1 : -1]  # strip ( and )
                        # Remove surrounding quotes from single-arg reprs
                        # Only strip when the inner content has no
                        # unescaped quotes, so multi-arg reprs like
                        # ValueError('a', 'b') are left intact.
                        if (
                            len(args) >= 2
                            and args[0] in ("'", '"')
                            and args[-1] == args[0]
                            and args[0] not in args[1:-1]
                        ):
                            args = args[1:-1]
                        if args:
                            lines.append(f"{type_name}: {args}")
                        else:
                            lines.append(type_name)
                    else:
                        lines.append(exc_repr)
                else:
                    exc_type = type(exc).__qualname__
                    exc_str = str(exc)
                    if exc_str:
                        lines.append(f"{exc_type}: {exc_str}")
                    else:
                        lines.append(exc_type)

            return lines
        else:
            # Thread stacktrace — simple traceback format
            # Include thread name in header if available
            if st.thread_name:
                lines = [
                    f"Thread: {st.thread_name}",
                    "Traceback (most recent call last):",
                ]
            else:
                lines = ["Traceback (most recent call last):"]
            for frame in st.frames:
                lines.append(
                    f'  File "{frame.file_path}", '
                    f"line {frame.line_number}, "
                    f"in {frame.function_name}"
                )
                file_lines = _get_source_lines(frame.file_path)
                if file_lines and 1 <= frame.line_number <= len(file_lines):
                    src = file_lines[frame.line_number - 1].strip()
                    if src:
                        lines.append(f"    {src}")
            if st.truncated:
                lines.append("  [Truncated — some frames may have been omitted]")
            return lines

    def _clamp_cursor(self, item_count: int) -> None:
        if item_count == 0:
            self.cursor = 0
            return
        if self.cursor >= item_count:
            self.cursor = item_count - 1
        if self.cursor < 0:
            self.cursor = 0

    def _ensure_visible(self, visible_rows: int) -> None:
        if self.cursor < self.scroll_offset:
            self.scroll_offset = self.cursor
        if self.cursor >= self.scroll_offset + visible_rows:
            self.scroll_offset = self.cursor - visible_rows + 1

    def _draw_header(self, stdscr: Any, width: int) -> int:
        """Draw the header bar. Returns the next row to draw at."""
        title = f" Snapshot Viewer: {os.path.basename(self.path)}"
        stdscr.attron(curses.A_REVERSE)
        stdscr.addnstr(0, 0, title.ljust(width), width)
        stdscr.attroff(curses.A_REVERSE)
        return 1

    def _draw_footer(self, stdscr: Any, height: int, width: int) -> None:
        """Draw the footer/status bar."""
        row = height - 1
        if self.current_view == "main":
            help_text = " q:Quit  Enter/→:Select"
        elif self.current_view == "source_content":
            help_text = " q/Esc/←:Back  j/k:Scroll  g/G:Top/Bottom  e:Extract"
        elif self.current_view in (
            "metadata",
            "manifest",
            "environment",
            "statistics",
            "traceback",
        ):
            help_text = " q/Esc/←:Back  j/k:Scroll  g/G:Top/Bottom"
        elif self.current_view == "snapshots":
            help_text = " ←:Back/Collapse  →:Expand/Enter  o/O:All"
            help_text += "  t:Traceback"
        elif self.current_view == "frame":
            help_text = " ←:Back/Collapse  →:Expand  o/O:All  f:File"
        elif self.current_view == "sources":
            help_text = " ←:Back  →/Enter:View  e:Extract  a:Extract All"
        else:
            help_text = " q/Esc:Back  Enter:Select  j/k:Up/Down"

        # Append labels for registered actions applicable to this view
        for action in self._actions:
            if self.current_view in action.views:
                help_text += f"  {action.key}:{action.label}"

        if self.status_message:
            help_text = f" {self.status_message}"
            self.status_message = ""

        stdscr.attron(curses.A_REVERSE)
        try:
            stdscr.addnstr(row, 0, help_text.ljust(width), width - 1)
        except curses.error:
            pass
        stdscr.attroff(curses.A_REVERSE)

    def _draw_list(
        self,
        stdscr: Any,
        items: list[tuple[str, str]],
        start_row: int,
        height: int,
        width: int,
    ) -> None:
        """Draw a selectable list of items."""
        visible_rows = height - start_row - 1  # minus footer
        self._clamp_cursor(len(items))
        self._ensure_visible(visible_rows)

        for i in range(visible_rows):
            idx = self.scroll_offset + i
            row = start_row + i
            if idx >= len(items):
                break
            label, desc = items[idx]
            is_selected = idx == self.cursor
            prefix = " > " if is_selected else "   "
            line = f"{prefix}{label}"
            if desc:
                line += f"  — {desc}"

            attr = curses.A_BOLD if is_selected else 0
            try:
                stdscr.addnstr(row, 0, line, width - 1, attr)
            except curses.error:
                pass

    def _wrap_lines(self, lines: list[str], width: int) -> list[str]:
        """Wrap lines to fit within the given display width."""
        display_width = max(1, width - 1)
        wrapped: list[str] = []
        for line in lines:
            if len(line) <= display_width:
                wrapped.append(line)
            else:
                for i in range(0, len(line), display_width):
                    wrapped.append(line[i : i + display_width])
        return wrapped

    def _draw_scrollable_text(
        self, stdscr: Any, lines: list[str], start_row: int, height: int, width: int
    ) -> None:
        """Draw scrollable text content with line wrapping."""
        lines = self._wrap_lines(lines, width)

        visible_rows = height - start_row - 1  # minus footer
        total = len(lines)

        # Clamp scroll_offset
        if total <= visible_rows:
            self.scroll_offset = 0
        elif self.scroll_offset > total - visible_rows:
            self.scroll_offset = total - visible_rows
        if self.scroll_offset < 0:
            self.scroll_offset = 0

        for i in range(visible_rows):
            idx = self.scroll_offset + i
            row = start_row + i
            if idx >= total:
                break
            try:
                stdscr.addnstr(row, 0, lines[idx], width - 1)
            except curses.error:
                pass

        # Show scroll indicator
        if total > visible_rows:
            pct = int(100 * (self.scroll_offset + visible_rows) / total)
            pct = min(pct, 100)
            indicator = f" {pct}% "
            try:
                stdscr.addnstr(
                    start_row,
                    width - len(indicator) - 1,
                    indicator,
                    len(indicator),
                    curses.A_DIM,
                )
            except curses.error:
                pass

    def _draw_source_content(
        self, stdscr: Any, start_row: int, height: int, width: int
    ) -> None:
        """Draw source content with optional line highlighting."""
        lines = self.content_lines
        visible_rows = height - start_row - 1  # minus footer
        total = len(lines)

        # Clamp scroll_offset
        if total <= visible_rows:
            self.scroll_offset = 0
        elif self.scroll_offset > total - visible_rows:
            self.scroll_offset = total - visible_rows
        if self.scroll_offset < 0:
            self.scroll_offset = 0

        # The highlighted line N is at content_lines index N+1 (2-line header)
        highlight_idx = (self.highlight_line + 1) if self.highlight_line else -1

        for i in range(visible_rows):
            idx = self.scroll_offset + i
            row = start_row + i
            if idx >= total:
                break
            attr = curses.A_REVERSE if idx == highlight_idx else 0
            try:
                text = lines[idx]
                if attr:
                    stdscr.addnstr(row, 0, text.ljust(width - 1), width - 1, attr)
                else:
                    stdscr.addnstr(row, 0, text, width - 1)
            except curses.error:
                pass

        # Show scroll indicator
        if total > visible_rows:
            pct = int(100 * (self.scroll_offset + visible_rows) / total)
            pct = min(pct, 100)
            indicator = f" {pct}% "
            try:
                stdscr.addnstr(
                    start_row,
                    width - len(indicator) - 1,
                    indicator,
                    len(indicator),
                    curses.A_DIM,
                )
            except curses.error:
                pass

    def _draw(self, stdscr: Any) -> None:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        self._last_width = width
        if height < 3 or width < 20:
            return

        start_row = self._draw_header(stdscr, width)
        self._draw_footer(stdscr, height, width)

        if self.current_view == "main":
            items = self._get_main_menu_items()
            self._draw_list(stdscr, items, start_row, height, width)

        elif self.current_view == "snapshots":
            tree_items = self._get_snapshot_tree_items()
            if not tree_items:
                try:
                    stdscr.addnstr(start_row, 2, "No snapshots found.", width - 3)
                except curses.error:
                    pass
            else:
                display_items = [(label, desc) for label, desc, *_ in tree_items]
                self._draw_list(stdscr, display_items, start_row, height, width)

        elif self.current_view == "frame":
            st = self.selected_stacktrace
            trunc = " (truncated)" if st is not None and st.truncated else ""
            # Display thread name if available, otherwise fall back to thread ID
            if st is not None and st.thread_name:
                subtitle = f"  Thread: {st.thread_name}{trunc}"
            else:
                subtitle = f"  Thread {self.selected_stacktrace_id}{trunc}"
            try:
                stdscr.addnstr(start_row, 0, subtitle, width - 1, curses.A_DIM)
            except curses.error:
                pass
            tree_items = self._get_frame_tree_items()
            display_items = [(label, desc) for label, desc, *_ in tree_items]
            self._draw_list(stdscr, display_items, start_row + 1, height, width)

        elif self.current_view == "metadata":
            if not self.content_lines:
                self.content_lines = self._build_metadata_lines()
            self._draw_scrollable_text(
                stdscr, self.content_lines, start_row, height, width
            )

        elif self.current_view == "manifest":
            if not self.content_lines:
                self.content_lines = self._build_manifest_lines()
            self._draw_scrollable_text(
                stdscr, self.content_lines, start_row, height, width
            )

        elif self.current_view == "environment":
            if not self.content_lines:
                self.content_lines = self._build_environment_lines()
            self._draw_scrollable_text(
                stdscr, self.content_lines, start_row, height, width
            )

        elif self.current_view == "statistics":
            if not self.content_lines:
                self.content_lines = self._build_statistics_lines()
            self._draw_scrollable_text(
                stdscr, self.content_lines, start_row, height, width
            )

        elif self.current_view == "sources":
            items = self._build_source_items()
            if not items:
                try:
                    stdscr.addnstr(start_row, 2, "No source files.", width - 3)
                except curses.error:
                    pass
            else:
                self._draw_list(stdscr, items, start_row, height, width)

        elif self.current_view == "source_content":
            if not self.content_lines:
                self.content_lines = self._build_source_content_lines()
            self._draw_source_content(stdscr, start_row, height, width)

        elif self.current_view == "traceback":
            if not self.content_lines:
                self.content_lines = self._build_traceback_lines()
            self._draw_scrollable_text(
                stdscr, self.content_lines, start_row, height, width
            )

        stdscr.refresh()

    def _handle_main_enter(self) -> None:
        if self.cursor == 0:
            self._push_view("snapshots")
        elif self.cursor == 1:
            self._push_view("metadata")
        elif self.cursor == 2:
            self._push_view("manifest")
        elif self.cursor == 3:
            self._push_view("environment")
        elif self.cursor == 4:
            self._push_view("statistics")
        elif self.cursor == 5:
            self._push_view("sources")

    def _handle_snapshots_enter(self) -> None:
        """Handle Enter in the snapshot tree view.

        On a snapshot row: toggle expand/collapse.
        On a stacktrace row: navigate to the frames view.
        """
        tree_items = self._get_snapshot_tree_items()
        if self.cursor >= len(tree_items):
            return
        _label, _desc, kind, snap_idx, st_id = tree_items[self.cursor]
        if kind == "snapshot":
            if snap_idx in self.expanded_snapshots:
                self.expanded_snapshots.discard(snap_idx)
            else:
                self.expanded_snapshots.add(snap_idx)
        elif kind == "stacktrace" and st_id is not None:
            snap = self.snapshots[snap_idx]
            self.selected_snapshot = snap
            self.selected_snapshot_idx = snap_idx
            self.selected_stacktrace = snap.stacktraces[st_id]
            self.selected_stacktrace_id = st_id
            self.expanded_frames.clear()
            self._push_view("frame")

    def _handle_frame_enter(self) -> None:
        """Handle Enter in the frame tree view.

        On a frame row: toggle expand/collapse to show/hide locals.
        On a local row: no action.
        """
        tree_items = self._get_frame_tree_items()
        if self.cursor >= len(tree_items):
            return
        _label, _desc, kind, frame_idx = tree_items[self.cursor]
        if kind == "frame":
            if frame_idx in self.expanded_frames:
                self.expanded_frames.discard(frame_idx)
            else:
                self.expanded_frames.add(frame_idx)

    def _handle_sources_enter(self) -> None:
        if self.cursor < len(self.source_files):
            self.selected_source_file = self.source_files[self.cursor]
            self.highlight_line = None
            self._push_view("source_content")

    def _handle_frame_view_file(self) -> None:
        """Open the source file for the currently highlighted frame, centered on its line."""
        tree_items = self._get_frame_tree_items()
        if self.cursor >= len(tree_items):
            return
        _label, _desc, kind, frame_idx = tree_items[self.cursor]
        # For a local variable row, use its parent frame
        if self.selected_stacktrace is None:
            return
        frames = self.selected_stacktrace.frames
        if frame_idx >= len(frames):
            return
        frame = frames[frame_idx]

        # Find the matching source file: try embedded sources first,
        # then fall back to reading from disk (frame paths are real paths).
        target_sf = None
        for sf in self.source_files:
            if sf.path == frame.file_path:
                target_sf = sf
                break

        if target_sf is not None:
            self.selected_source_file = target_sf
            self.highlight_line = frame.line_number
            self._push_view("source_content")
            self.content_lines = self._build_source_content_lines()
        elif os.path.isfile(frame.file_path):
            try:
                with open(frame.file_path) as f:
                    content = f.read()
            except OSError:
                self.status_message = (
                    f"Cannot read: {os.path.basename(frame.file_path)}"
                )
                return
            self.highlight_line = frame.line_number
            self._push_view("source_content")
            display_path = self._truncate_path(
                frame.file_path, max(20, self._last_width - 7)
            )
            self.content_lines = [f"File: {display_path}", ""]
            for i, line in enumerate(content.splitlines(), 1):
                marker = "►" if i == self.highlight_line else " "
                self.content_lines.append(f"{marker}{i:5d} | {line}")
        else:
            self.status_message = (
                f"Source file not found: {os.path.basename(frame.file_path)}"
            )
            return

        # Center the highlighted line in the view.
        # Line numbers are 1-based; the content_lines list has a 2-line header
        # (index 0 = "File: ...", index 1 = ""), so line N is at index N+1.
        target_idx = frame.line_number + 1
        if target_idx >= len(self.content_lines):
            target_idx = max(0, len(self.content_lines) - 1)
        self.scroll_offset = max(0, target_idx - 10)

    def _select_stacktrace_at_cursor(self) -> bool:
        """Select the stacktrace at the current cursor in the snapshots tree.

        Returns True if a stacktrace was selected, False if the cursor is on a
        snapshot node (sets a status message in that case).
        """
        tree_items = self._get_snapshot_tree_items()
        if self.cursor < len(tree_items):
            _label, _desc, kind, snap_idx, st_id = tree_items[self.cursor]
            if kind == "stacktrace" and st_id is not None:
                snap = self.snapshots[snap_idx]
                self.selected_snapshot = snap
                self.selected_snapshot_idx = snap_idx
                self.selected_stacktrace = snap.stacktraces[st_id]
                self.selected_stacktrace_id = st_id
                return True
            elif kind == "snapshot":
                self.status_message = "Select a stacktrace first"
        return False

    def _prompt_path(self, stdscr: Any, prompt: str, default: str) -> str | None:
        """Prompt the user for a file path with horizontal scrolling.

        Supports readline-style keybindings:
          Ctrl-A / Home: beginning of line
          Ctrl-E / End:  end of line
          Ctrl-B / Left: back one char
          Ctrl-F / Right: forward one char
          Alt-b:  back one word
          Alt-f:  forward one word
          Ctrl-U: kill to beginning of line
          Ctrl-K: kill to end of line
          Ctrl-W / Alt-Backspace: kill word backward
          Ctrl-D / Delete: delete char under cursor
          Backspace: delete char before cursor
          Enter: accept
          Escape: cancel

        Returns the entered string, or None if cancelled.
        """
        height: int
        width: int
        height, width = stdscr.getmaxyx()
        footer_row: int = height - 1
        input_row: int = height - 2
        # Usable columns for text input (1-char margin on each side)
        field_width: int = width - 2

        # Draw prompt on the footer row
        curses.curs_set(1)
        stdscr.attron(curses.A_REVERSE)
        try:
            stdscr.addnstr(footer_row, 0, " " * width, width - 1)
            stdscr.addnstr(footer_row, 0, f" {prompt}", width - 1)
        except curses.error:
            pass
        stdscr.attroff(curses.A_REVERSE)

        buf: list[str] = list(default)
        cursor_pos: int = len(buf)
        scroll_pos = 0  # leftmost visible character index

        def _is_word_char(c: str) -> bool:
            return c.isalnum() or c == "_"

        def _word_back(pos: int) -> int:
            """Move backward to the start of the previous word."""
            p = pos
            # Skip non-word characters
            while p > 0 and not _is_word_char(buf[p - 1]):
                p -= 1
            # Skip word characters
            while p > 0 and _is_word_char(buf[p - 1]):
                p -= 1
            return p

        def _word_forward(pos: int) -> int:
            """Move forward to the end of the next word."""
            p = pos
            n = len(buf)
            # Skip word characters
            while p < n and _is_word_char(buf[p]):
                p += 1
            # Skip non-word characters
            while p < n and not _is_word_char(buf[p]):
                p += 1
            return p

        def _redraw() -> None:
            nonlocal scroll_pos
            # Ensure cursor is within the visible window
            if cursor_pos < scroll_pos:
                scroll_pos = cursor_pos
            if cursor_pos >= scroll_pos + field_width:
                scroll_pos = cursor_pos - field_width + 1
            if scroll_pos < 0:
                scroll_pos = 0

            visible = "".join(buf[scroll_pos : scroll_pos + field_width])
            screen_cursor = cursor_pos - scroll_pos
            try:
                stdscr.move(input_row, 0)
                stdscr.clrtoeol()
                stdscr.addnstr(input_row, 1, visible, field_width)
                # Show scroll indicators
                if scroll_pos > 0:
                    stdscr.addch(input_row, 0, curses.ACS_LARROW, curses.A_DIM)
                if scroll_pos + field_width < len(buf):
                    stdscr.addch(
                        input_row,
                        min(width - 1, field_width + 1),
                        curses.ACS_RARROW,
                        curses.A_DIM,
                    )
                stdscr.move(input_row, 1 + screen_cursor)
            except curses.error:
                pass
            stdscr.refresh()

        while True:
            _redraw()
            ch = stdscr.getch()

            # Enter — accept
            if ch in (ord("\n"), curses.KEY_ENTER, 10, 13):
                curses.curs_set(0)
                return "".join(buf)

            # Escape — cancel (also consume any following bytes from
            # Alt-key sequences so they don't leak into the main loop).
            # Use a short timeout to distinguish bare Escape from Alt-key.
            if ch == 27:
                stdscr.timeout(25)
                next_ch = stdscr.getch()
                stdscr.timeout(-1)
                if next_ch == -1:
                    # Plain Escape — cancel
                    curses.curs_set(0)
                    return None
                # Alt-key sequence: next_ch is the modified key
                if next_ch == ord("b"):
                    cursor_pos = _word_back(cursor_pos)
                elif next_ch == ord("f"):
                    cursor_pos = _word_forward(cursor_pos)
                elif next_ch in (curses.KEY_BACKSPACE, 127, 8):
                    # Alt-Backspace: kill word backward
                    new_pos = _word_back(cursor_pos)
                    del buf[new_pos:cursor_pos]
                    cursor_pos = new_pos
                elif next_ch == ord("d"):
                    # Alt-d: kill word forward
                    new_pos = _word_forward(cursor_pos)
                    del buf[cursor_pos:new_pos]
                continue

            # Backspace
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                if cursor_pos > 0:
                    del buf[cursor_pos - 1]
                    cursor_pos -= 1
                continue

            # Delete / Ctrl-D
            if ch == curses.KEY_DC or ch == 4:
                if cursor_pos < len(buf):
                    del buf[cursor_pos]
                continue

            # Left / Ctrl-B
            if ch == curses.KEY_LEFT or ch == 2:
                if cursor_pos > 0:
                    cursor_pos -= 1
                continue

            # Right / Ctrl-F
            if ch == curses.KEY_RIGHT or ch == 6:
                if cursor_pos < len(buf):
                    cursor_pos += 1
                continue

            # Home / Ctrl-A
            if ch == curses.KEY_HOME or ch == 1:
                cursor_pos = 0
                continue

            # End / Ctrl-E
            if ch == curses.KEY_END or ch == 5:
                cursor_pos = len(buf)
                continue

            # Ctrl-U: kill to beginning of line
            if ch == 21:
                del buf[:cursor_pos]
                cursor_pos = 0
                continue

            # Ctrl-K: kill to end of line
            if ch == 11:
                del buf[cursor_pos:]
                continue

            # Ctrl-W: kill word backward
            if ch == 23:
                new_pos = _word_back(cursor_pos)
                del buf[new_pos:cursor_pos]
                cursor_pos = new_pos
                continue

            # Printable ASCII
            if 32 <= ch < 127:
                buf.insert(cursor_pos, chr(ch))
                cursor_pos += 1

    def _extract_all_files(self) -> None:
        """Extract all source files, prompting for destination."""
        if not self.source_files:
            self.status_message = "No source files to extract"
            return
        default_dir = os.path.join(tempfile.gettempdir(), "snapshot_extracted")
        dest = self._prompt_path(self._stdscr, "Extract all files to:", default_dir)
        if dest is None:
            self.status_message = "Extraction cancelled"
            return
        dest = dest.strip()
        if not dest:
            self.status_message = "Extraction cancelled"
            return
        try:
            os.makedirs(dest, exist_ok=True)
            for sf in self.source_files:
                file_dest = os.path.join(dest, sf.path.lstrip("/"))
                os.makedirs(os.path.dirname(file_dest), exist_ok=True)
                with open(file_dest, "w") as f:
                    f.write(sf.content)
            self.status_message = f"Extracted {len(self.source_files)} files to {dest}"
        except OSError as e:
            self.status_message = f"Error: {e}"

    def _extract_single_file(self) -> None:
        """Extract the currently selected source file, prompting for destination."""
        if self.selected_source_file is None:
            self.status_message = "No file selected"
            return
        sf = self.selected_source_file
        default_path = os.path.join(
            tempfile.gettempdir(),
            "snapshot_extracted",
            sf.path.lstrip("/"),
        )
        dest = self._prompt_path(self._stdscr, "Extract to:", default_path)
        if dest is None:
            self.status_message = "Extraction cancelled"
            return
        dest = dest.strip()
        if not dest:
            self.status_message = "Extraction cancelled"
            return
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w") as f:
                f.write(sf.content)
            self.status_message = f"Extracted to {dest}"
        except OSError as e:
            self.status_message = f"Error: {e}"

    def _get_item_count(self) -> int:
        if self.current_view == "main":
            return len(self._get_main_menu_items())
        if self.current_view == "snapshots":
            return len(self._get_snapshot_tree_items())
        if self.current_view == "frame":
            return len(self._get_frame_tree_items())
        if self.current_view == "sources":
            return len(self.source_files)
        if self.current_view in (
            "metadata",
            "manifest",
            "environment",
            "statistics",
            "traceback",
        ):
            return len(self._wrap_lines(self.content_lines, self._last_width))
        if self.current_view == "source_content":
            return len(self.content_lines)
        return 0

    def _handle_input(self, key: int) -> bool:
        """Handle a key press. Returns False to quit."""
        is_text_view = self.current_view in (
            "metadata",
            "manifest",
            "environment",
            "statistics",
            "source_content",
            "traceback",
        )

        # Quit / Back
        if key == ord("q"):
            if not self._pop_view():
                return False
            return True

        if key == 27:  # ESC
            if not self._pop_view():
                return False
            return True

        # Navigation
        if key in (ord("j"), curses.KEY_DOWN):
            if is_text_view:
                total = self._get_item_count()
                if self.scroll_offset < total - 1:
                    self.scroll_offset += 1
            else:
                item_count = self._get_item_count()
                if self.cursor < item_count - 1:
                    self.cursor += 1
            return True

        if key in (ord("k"), curses.KEY_UP):
            if is_text_view:
                if self.scroll_offset > 0:
                    self.scroll_offset -= 1
            else:
                if self.cursor > 0:
                    self.cursor -= 1
            return True

        if key in (ord("g"), curses.KEY_HOME):
            self.cursor = 0
            self.scroll_offset = 0
            return True

        if key in (ord("G"), curses.KEY_END):
            if is_text_view:
                self.scroll_offset = max(0, self._get_item_count() - 1)
            else:
                self.cursor = max(0, self._get_item_count() - 1)
            return True

        # Page up/down
        if key == curses.KEY_PPAGE:
            if is_text_view:
                self.scroll_offset = max(0, self.scroll_offset - 20)
            else:
                self.cursor = max(0, self.cursor - 20)
            return True

        if key == curses.KEY_NPAGE:
            if is_text_view:
                self.scroll_offset = min(
                    max(0, self._get_item_count() - 1),
                    self.scroll_offset + 20,
                )
            else:
                self.cursor = min(self._get_item_count() - 1, self.cursor + 20)
            return True

        # Enter / Select
        if key in (ord("\n"), curses.KEY_ENTER, 10, 13):
            if self.current_view == "main":
                self._handle_main_enter()
            elif self.current_view == "snapshots":
                self._handle_snapshots_enter()
            elif self.current_view == "frame":
                self._handle_frame_enter()
            elif self.current_view == "sources":
                self._handle_sources_enter()
            return True

        # Expand/collapse all (o/O) in snapshots tree view
        if key == ord("o") and self.current_view == "snapshots":
            self.expanded_snapshots = set(range(len(self.snapshots)))
            return True

        if key == ord("O") and self.current_view == "snapshots":
            self.expanded_snapshots.clear()
            return True

        # Expand/collapse all (o/O) in frame tree view
        if key == ord("o") and self.current_view == "frame":
            if self.selected_stacktrace is not None:
                self.expanded_frames = set(range(len(self.selected_stacktrace.frames)))
            return True

        if key == ord("O") and self.current_view == "frame":
            self.expanded_frames.clear()
            return True

        # Right arrow: enter/expand in main, sources, and text views
        if key in (curses.KEY_RIGHT, ord("l")) and self.current_view == "main":
            self._handle_main_enter()
            return True

        if key in (curses.KEY_RIGHT, ord("l")) and self.current_view == "sources":
            self._handle_sources_enter()
            return True

        # Left arrow: go back in sources and text views
        if key in (curses.KEY_LEFT, ord("h")) and self.current_view == "sources":
            self._pop_view()
            return True

        if key in (curses.KEY_LEFT, ord("h")) and self.current_view in (
            "metadata",
            "manifest",
            "environment",
            "statistics",
            "source_content",
            "traceback",
        ):
            self._pop_view()
            return True

        # Left/right arrow expand/collapse in snapshots tree view
        if key in (curses.KEY_RIGHT, ord("l")) and self.current_view == "snapshots":
            tree_items = self._get_snapshot_tree_items()
            if self.cursor < len(tree_items):
                _label, _desc, kind, snap_idx, st_id = tree_items[self.cursor]
                if kind == "snapshot":
                    if snap_idx not in self.expanded_snapshots:
                        self.expanded_snapshots.add(snap_idx)
                elif kind == "stacktrace":
                    # Enter the stacktrace (same as Enter on a stacktrace)
                    self._handle_snapshots_enter()
            return True

        if key in (curses.KEY_LEFT, ord("h")) and self.current_view == "snapshots":
            tree_items = self._get_snapshot_tree_items()
            if self.cursor < len(tree_items):
                _label, _desc, kind, snap_idx, st_id = tree_items[self.cursor]
                if kind == "snapshot":
                    if snap_idx in self.expanded_snapshots:
                        self.expanded_snapshots.discard(snap_idx)
                    else:
                        self._pop_view()
                elif kind == "stacktrace":
                    # Move cursor to the parent snapshot row
                    for j in range(self.cursor - 1, -1, -1):
                        if tree_items[j][2] == "snapshot":
                            self.cursor = j
                            break
            else:
                self._pop_view()
            return True

        # Left/right arrow expand/collapse in frame tree view
        if key in (curses.KEY_RIGHT, ord("l")) and self.current_view == "frame":
            tree_items = self._get_frame_tree_items()
            if self.cursor < len(tree_items):
                _label, _desc, kind, frame_idx = tree_items[self.cursor]
                if kind == "frame" and frame_idx not in self.expanded_frames:
                    self.expanded_frames.add(frame_idx)
            return True

        if key in (curses.KEY_LEFT, ord("h")) and self.current_view == "frame":
            tree_items = self._get_frame_tree_items()
            if self.cursor < len(tree_items):
                _label, _desc, kind, frame_idx = tree_items[self.cursor]
                if kind == "frame":
                    if frame_idx in self.expanded_frames:
                        self.expanded_frames.discard(frame_idx)
                    else:
                        self._pop_view()
                elif kind == "local":
                    # Move cursor to the parent frame row
                    for j in range(self.cursor - 1, -1, -1):
                        if tree_items[j][2] == "frame":
                            self.cursor = j
                            break
            else:
                self._pop_view()
            return True

        # Traceback (t) from snapshots tree view — show Python-style traceback
        if key == ord("t") and self.current_view == "snapshots":
            if self._select_stacktrace_at_cursor():
                self._push_view("traceback")
            return True

        # Registered actions
        for action in self._actions:
            if key == ord(action.key) and self.current_view in action.views:
                if self.current_view == "snapshots":
                    if not self._select_stacktrace_at_cursor():
                        return True
                action.callback(self)
                return True

        # View source file (f) from frame view — open source at frame's line
        if key == ord("f") and self.current_view == "frame":
            self._handle_frame_view_file()
            return True

        # Extract (e) from sources view — extract highlighted file
        if key == ord("e") and self.current_view == "sources":
            if self.cursor < len(self.source_files):
                self.selected_source_file = self.source_files[self.cursor]
                self._extract_single_file()
            return True

        # Extract all (a) from sources view — extract all files
        if key == ord("a") and self.current_view == "sources":
            self._extract_all_files()
            return True

        # Extract (e) from source_content view — extract current file
        if key == ord("e") and self.current_view == "source_content":
            self._extract_single_file()
            return True

        return True

    def run(self, stdscr: Any) -> None:
        self._stdscr = stdscr
        curses.curs_set(0)
        curses.use_default_colors()
        # Set a short escape delay so bare ESC is recognized quickly
        # (default can be 1000ms on some systems, making ESC feel broken)
        curses.set_escdelay(25)
        stdscr.timeout(-1)

        try:
            while True:
                self._draw(stdscr)
                key = stdscr.getch()
                if not self._handle_input(key):
                    break
        except KeyboardInterrupt:
            pass


def main() -> int:
    if len(sys.argv) != 2:
        print(
            "Usage: python -m tintype.utils.tintype_viewer /path/to/tintype.pytb",
            file=sys.stderr,
        )
        return 1

    path = sys.argv[1]

    try:
        viewer = SnapshotViewer(path)
    except RuntimeError as e:
        print(f"Error: Failed to open snapshot file: {path}", file=sys.stderr)
        print(f"  Reason: {e}", file=sys.stderr)
        return 1

    curses.wrapper(viewer.run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
