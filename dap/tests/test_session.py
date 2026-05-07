# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Tests for tintype.dap.session — the DAP handler layer.

These are pure JSON-in / JSON-out tests: we fake the transport and the
SnapshotReader so we can assert on response bodies and emitted events
without touching sockets or the filesystem.
"""

from __future__ import annotations

import io
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from tintype.dap.dispatcher import Dispatcher
from tintype.dap.session import (
    DEFAULT_EXCLUDE_FRAME_PATHS,
    extend_default_exclude_frame_paths,
    set_default_exclude_frame_paths,
    SnapshotDebugSession,
)
from tintype.dap.transport import read_message


class RecordingStream(io.BytesIO):
    """Mimic a ByteStream-compatible buffer that records all writes for later assertion."""


def _make_stacktrace(
    stid: int,
    frames: list[Any],
    *,
    thread_name: str = "",
    exception: Any = None,
) -> Any:
    """Build a MagicMock shaped like ``tintype.Stacktrace``."""
    st = MagicMock()
    st.id = stid
    st.frames = frames
    st.thread_name = thread_name
    st.exception_object = exception
    st.get_cause.return_value = None
    st.get_context.return_value = None
    return st


def _make_frame(
    file_path: str, function_name: str, line: int, locals_: dict[str, Any]
) -> Any:
    frame = MagicMock()
    frame.file_path = file_path
    frame.original_file_path = file_path
    frame.function_name = function_name
    frame.function_qualname = function_name
    frame.line_number = line
    frame.get_locals.return_value = locals_
    return frame


def _make_snapshot(stacktraces: list[Any], *, ts: int = 1_700_000_000_000_000) -> Any:
    snapshot = MagicMock()
    snapshot.timestamp = ts
    snapshot.truncated = False
    snapshot.stacktraces = {st.id: st for st in stacktraces}
    return snapshot


def _drain_messages(stream: io.BytesIO) -> list[dict[str, Any]]:
    """Read every DAP message currently buffered in ``stream``."""
    stream.seek(0)
    out: list[dict[str, Any]] = []
    while True:
        try:
            out.append(read_message(stream))
        except Exception:  # noqa: BLE001 — EOF is expected at drain end
            break
    return out


def _send_request(
    session: SnapshotDebugSession,
    dispatcher: Dispatcher,
    *,
    seq: int,
    command: str,
    arguments: dict[str, Any] | None = None,
) -> None:
    dispatcher.handle(
        {
            "seq": seq,
            "type": "request",
            "command": command,
            "arguments": arguments or {},
        }
    )


class SessionHandlerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stream = RecordingStream()
        self.dispatcher = Dispatcher(self.stream)
        self.session = SnapshotDebugSession(self.dispatcher)
        self.session.wire()
        # Snapshot the module-level default exclude list so tests that
        # mutate it (via extend / set helpers) can't leak state between
        # cases. ``tearDown`` restores the list in-place even if the
        # test fails before its own ``finally`` block would run — which
        # is exactly the brittleness that made the previous in-method
        # ``try/finally`` pattern unsafe.
        self._saved_default_exclude: list[str] = list(DEFAULT_EXCLUDE_FRAME_PATHS)

    def tearDown(self) -> None:
        DEFAULT_EXCLUDE_FRAME_PATHS[:] = self._saved_default_exclude

    def _launch_with_snapshots(self, snapshots: list[Any]) -> MagicMock:
        """Patch SnapshotReader to yield ``snapshots`` and send ``launch``."""
        reader = MagicMock()
        reader.snapshot_count.return_value = len(snapshots)
        reader.get_all_source_files.return_value = []
        reader.get_all_snapshots.return_value = snapshots
        reader.get_snapshot_at_index.side_effect = (
            lambda i: snapshots[i] if 0 <= i < len(snapshots) else None
        )

        # Patch SnapshotReader constructor and os.path.isfile.
        with (
            patch("tintype.dap.session.SnapshotReader", return_value=reader),
            patch("tintype.dap.session.os.path.isfile", return_value=True),
        ):
            _send_request(
                self.session,
                self.dispatcher,
                seq=1,
                command="launch",
                arguments={"pytbPath": "/fake/snap.pytb"},
            )
        return reader

    def test_initialize_returns_capabilities(self) -> None:
        _send_request(self.session, self.dispatcher, seq=1, command="initialize")
        messages = _drain_messages(self.stream)
        self.assertEqual(len(messages), 1)
        response = messages[0]
        self.assertEqual(response["type"], "response")
        self.assertTrue(response["success"])
        body = response["body"]
        self.assertTrue(body["supportsStepBack"])
        self.assertTrue(body["supportsExceptionInfoRequest"])
        self.assertTrue(body["supportsConfigurationDoneRequest"])
        self.assertFalse(body["supportsSetVariable"])
        # Restart is used by the viewer toolbar to jump the cursor back to
        # snapshot #0, so the adapter must advertise the capability.
        self.assertTrue(body["supportsRestartRequest"])

    def test_launch_emits_initial_events(self) -> None:
        frame = _make_frame("/a/b.py", "foo", 10, {"x": 1})
        st = _make_stacktrace(100, [frame], thread_name="MainThread")
        snap = _make_snapshot([st])

        self._launch_with_snapshots([snap])

        messages = _drain_messages(self.stream)
        # We expect: launch response, initialized, thread(s), two
        # stopped events (bootstrap + real). The bootstrap carries a
        # generic ``"Snapshot"`` description so VS Code has something to
        # overwrite on its initial-launch race; the real event that
        # follows sets the actual display name the user sees in the
        # CALL STACK panel. We still emit NO ``process`` event — that
        # surface is owned by the Tintype Snapshots sidebar.
        event_names = [m.get("event") for m in messages if m["type"] == "event"]
        self.assertIn("initialized", event_names)
        self.assertNotIn("process", event_names)
        self.assertIn("thread", event_names)
        self.assertEqual(event_names.count("stopped"), 2)

        stopped_events = [m for m in messages if m.get("event") == "stopped"]
        # First is bootstrap — generic "Snapshot" label.
        self.assertEqual(stopped_events[0]["body"]["description"], "Snapshot")
        # Second is the real one — must include a richer description.
        self.assertIn("description", stopped_events[1]["body"])
        self.assertIn("Snapshot", stopped_events[1]["body"]["description"])
        self.assertNotEqual(
            stopped_events[0]["body"]["description"],
            stopped_events[1]["body"]["description"],
        )

    def test_launch_fails_on_empty_snapshot_file(self) -> None:
        reader = MagicMock()
        reader.snapshot_count.return_value = 0
        reader.get_all_source_files.return_value = []
        with (
            patch("tintype.dap.session.SnapshotReader", return_value=reader),
            patch("tintype.dap.session.os.path.isfile", return_value=True),
        ):
            _send_request(
                self.session,
                self.dispatcher,
                seq=1,
                command="launch",
                arguments={"pytbPath": "/fake/snap.pytb"},
            )

        response = _drain_messages(self.stream)[0]
        self.assertFalse(response["success"])

    def test_threads_stack_scopes_variables(self) -> None:
        locals_ = {"x": 42, "d": {"key": "value"}}
        frame = _make_frame("/a/b.py", "foo", 10, locals_)
        st = _make_stacktrace(100, [frame], thread_name="MainThread")
        snap = _make_snapshot([st])
        self._launch_with_snapshots([snap])

        # Clear the buffer so we only see responses to the commands below.
        self.stream.seek(0)
        self.stream.truncate()

        _send_request(self.session, self.dispatcher, seq=10, command="threads")
        threads_resp = _drain_messages(self.stream)[0]
        self.assertTrue(threads_resp["success"])
        self.assertEqual(threads_resp["body"]["threads"][0]["id"], 100)

        self.stream.seek(0)
        self.stream.truncate()
        _send_request(
            self.session,
            self.dispatcher,
            seq=11,
            command="stackTrace",
            arguments={"threadId": 100},
        )
        stack_resp = _drain_messages(self.stream)[0]
        self.assertTrue(stack_resp["success"])
        frames = stack_resp["body"]["stackFrames"]
        self.assertEqual(len(frames), 1)
        frame_id = frames[0]["id"]

        self.stream.seek(0)
        self.stream.truncate()
        _send_request(
            self.session,
            self.dispatcher,
            seq=12,
            command="scopes",
            arguments={"frameId": frame_id},
        )
        scopes_resp = _drain_messages(self.stream)[0]
        self.assertTrue(scopes_resp["success"])
        scope_ref = scopes_resp["body"]["scopes"][0]["variablesReference"]
        self.assertGreater(scope_ref, 0)

        self.stream.seek(0)
        self.stream.truncate()
        _send_request(
            self.session,
            self.dispatcher,
            seq=13,
            command="variables",
            arguments={"variablesReference": scope_ref},
        )
        vars_resp = _drain_messages(self.stream)[0]
        self.assertTrue(vars_resp["success"])
        names = [v["name"] for v in vars_resp["body"]["variables"]]
        self.assertIn("x", names)
        self.assertIn("d", names)

    def test_scope_variables_sorted_by_name(self) -> None:
        """Scope variables are returned in a predictable order:
        ``self`` / ``cls`` first, then public names alphabetically,
        then single-underscore private names, then dunders last.
        Sort is case-insensitive. Insertion order of the captured
        ``locals`` is deliberately not preserved.
        """
        locals_ = {
            "zulu": 1,
            "__dunder__": 2,
            "Alpha": 3,
            "_private": 4,
            "cls": 5,
            "beta": 6,
            "self": 7,
        }
        frame = _make_frame("/a/b.py", "foo", 10, locals_)
        st = _make_stacktrace(100, [frame], thread_name="MainThread")
        snap = _make_snapshot([st])
        self._launch_with_snapshots([snap])

        self.stream.seek(0)
        self.stream.truncate()
        _send_request(
            self.session,
            self.dispatcher,
            seq=400,
            command="stackTrace",
            arguments={"threadId": 100},
        )
        frame_id = _drain_messages(self.stream)[0]["body"]["stackFrames"][0]["id"]

        self.stream.seek(0)
        self.stream.truncate()
        _send_request(
            self.session,
            self.dispatcher,
            seq=401,
            command="scopes",
            arguments={"frameId": frame_id},
        )
        scope_ref = _drain_messages(self.stream)[0]["body"]["scopes"][0][
            "variablesReference"
        ]

        self.stream.seek(0)
        self.stream.truncate()
        _send_request(
            self.session,
            self.dispatcher,
            seq=402,
            command="variables",
            arguments={"variablesReference": scope_ref},
        )
        names = [
            v["name"] for v in _drain_messages(self.stream)[0]["body"]["variables"]
        ]
        self.assertEqual(
            names,
            [
                "self",
                "cls",
                "Alpha",
                "beta",
                "zulu",
                "_private",
                "__dunder__",
            ],
        )

    def test_continue_emits_new_stopped_when_snapshots_remain(self) -> None:
        st1 = _make_stacktrace(
            100, [_make_frame("/a/b.py", "foo", 1, {"i": 1})], thread_name="T"
        )
        st2 = _make_stacktrace(
            100, [_make_frame("/a/b.py", "foo", 2, {"i": 2})], thread_name="T"
        )
        snap1 = _make_snapshot([st1])
        snap2 = _make_snapshot([st2])
        self._launch_with_snapshots([snap1, snap2])

        self.stream.seek(0)
        self.stream.truncate()
        _send_request(self.session, self.dispatcher, seq=20, command="continue")

        messages = _drain_messages(self.stream)
        event_names = [m.get("event") for m in messages if m["type"] == "event"]
        self.assertIn("stopped", event_names)
        self.assertFalse(self.session.terminated)

    def test_continue_at_last_snapshot_stays_parked_and_re_stops(self) -> None:
        """Stepping past the last snapshot must NOT terminate the session;
        users should still be able to inspect state and step back."""
        snap = _make_snapshot(
            [_make_stacktrace(100, [_make_frame("/a/b.py", "foo", 1, {})])]
        )
        self._launch_with_snapshots([snap])

        self.stream.seek(0)
        self.stream.truncate()
        _send_request(self.session, self.dispatcher, seq=30, command="continue")

        messages = _drain_messages(self.stream)
        event_names = [m.get("event") for m in messages if m["type"] == "event"]
        self.assertNotIn("terminated", event_names)
        self.assertIn("stopped", event_names)
        self.assertFalse(self.session.terminated)

        # And a follow-up continue also shouldn't terminate — repeat the
        # call to make sure the parked behaviour is sticky.
        self.stream.seek(0)
        self.stream.truncate()
        _send_request(self.session, self.dispatcher, seq=31, command="continue")
        messages = _drain_messages(self.stream)
        event_names = [m.get("event") for m in messages if m["type"] == "event"]
        self.assertNotIn("terminated", event_names)
        self.assertIn("stopped", event_names)
        self.assertFalse(self.session.terminated)

    def test_step_back_navigates_to_prev_snapshot(self) -> None:
        snap1 = _make_snapshot(
            [_make_stacktrace(100, [_make_frame("/a/b.py", "foo", 1, {})])]
        )
        snap2 = _make_snapshot(
            [_make_stacktrace(100, [_make_frame("/a/b.py", "foo", 2, {})])]
        )
        self._launch_with_snapshots([snap1, snap2])

        # Advance to snapshot 2.
        _send_request(self.session, self.dispatcher, seq=40, command="continue")

        self.stream.seek(0)
        self.stream.truncate()
        _send_request(self.session, self.dispatcher, seq=41, command="stepBack")

        messages = _drain_messages(self.stream)
        event_names = [m.get("event") for m in messages if m["type"] == "event"]
        self.assertIn("stopped", event_names)
        self.assertFalse(self.session.terminated)

    def test_pause_returns_error(self) -> None:
        snap = _make_snapshot(
            [_make_stacktrace(100, [_make_frame("/a/b.py", "foo", 1, {})])]
        )
        self._launch_with_snapshots([snap])
        self.stream.seek(0)
        self.stream.truncate()

        _send_request(self.session, self.dispatcher, seq=50, command="pause")
        response = _drain_messages(self.stream)[0]
        self.assertFalse(response["success"])

    def test_unknown_command_returns_failure(self) -> None:
        _send_request(self.session, self.dispatcher, seq=60, command="bogusCommand")
        response = _drain_messages(self.stream)[0]
        self.assertFalse(response["success"])

    def test_scope_ref_is_cached_per_frame(self) -> None:
        """Repeated ``scopes(frameId)`` calls must return the same scope ref."""
        frame = _make_frame("/a/b.py", "foo", 10, {"x": 1})
        st = _make_stacktrace(100, [frame], thread_name="MainThread")
        snap = _make_snapshot([st])
        self._launch_with_snapshots([snap])

        self.stream.seek(0)
        self.stream.truncate()
        _send_request(
            self.session,
            self.dispatcher,
            seq=70,
            command="stackTrace",
            arguments={"threadId": 100},
        )
        stack_resp = _drain_messages(self.stream)[0]
        frame_id = stack_resp["body"]["stackFrames"][0]["id"]

        self.stream.seek(0)
        self.stream.truncate()
        _send_request(
            self.session,
            self.dispatcher,
            seq=71,
            command="scopes",
            arguments={"frameId": frame_id},
        )
        first = _drain_messages(self.stream)[0]
        self.stream.seek(0)
        self.stream.truncate()
        _send_request(
            self.session,
            self.dispatcher,
            seq=72,
            command="scopes",
            arguments={"frameId": frame_id},
        )
        second = _drain_messages(self.stream)[0]
        self.assertEqual(
            first["body"]["scopes"][0]["variablesReference"],
            second["body"]["scopes"][0]["variablesReference"],
        )

    def test_empty_snapshot_skips_stopped_event(self) -> None:
        """A snapshot with zero stacktraces must not emit ``stopped`` with threadId=0."""
        snap = _make_snapshot([])

        # Patch SnapshotReader to allow launch but present a zero-thread snapshot.
        reader = MagicMock()
        reader.snapshot_count.return_value = 1
        reader.get_all_source_files.return_value = []
        reader.get_snapshot_at_index.return_value = snap

        with (
            patch("tintype.dap.session.SnapshotReader", return_value=reader),
            patch("tintype.dap.session.os.path.isfile", return_value=True),
        ):
            _send_request(
                self.session,
                self.dispatcher,
                seq=80,
                command="launch",
                arguments={"pytbPath": "/fake/snap.pytb"},
            )

        messages = _drain_messages(self.stream)
        stopped = [m for m in messages if m.get("event") == "stopped"]
        self.assertEqual(
            stopped,
            [],
            f"expected no stopped event for empty snapshot, got: {stopped}",
        )

    def test_frame_registration_is_idempotent(self) -> None:
        """Re-calling ``stackTrace`` reuses frame ids via the reverse index."""
        frame = _make_frame("/a/b.py", "foo", 10, {})
        st = _make_stacktrace(100, [frame], thread_name="T")
        snap = _make_snapshot([st])
        self._launch_with_snapshots([snap])

        self.stream.seek(0)
        self.stream.truncate()
        _send_request(
            self.session,
            self.dispatcher,
            seq=90,
            command="stackTrace",
            arguments={"threadId": 100},
        )
        first_ids = [
            f["id"] for f in _drain_messages(self.stream)[0]["body"]["stackFrames"]
        ]

        self.stream.seek(0)
        self.stream.truncate()
        _send_request(
            self.session,
            self.dispatcher,
            seq=91,
            command="stackTrace",
            arguments={"threadId": 100},
        )
        second_ids = [
            f["id"] for f in _drain_messages(self.stream)[0]["body"]["stackFrames"]
        ]
        self.assertEqual(first_ids, second_ids)

    # --- Frame ordering regression ---------------------------------

    def test_stack_trace_frame_order_innermost_first(self) -> None:
        """DAP expects frame[0] to be the innermost (most recently called)
        frame. Tintype stores frames innermost-first, so the session must
        NOT reverse them."""
        inner = _make_frame("/app/inner.py", "inner_fn", 100, {})
        middle = _make_frame("/app/middle.py", "middle_fn", 50, {})
        outer = _make_frame("/app/outer.py", "main", 10, {})
        # Tintype layout: innermost-first.
        st = _make_stacktrace(42, [inner, middle, outer], thread_name="T")
        snap = _make_snapshot([st])
        self._launch_with_snapshots([snap])

        self.stream.seek(0)
        self.stream.truncate()
        _send_request(
            self.session,
            self.dispatcher,
            seq=500,
            command="stackTrace",
            arguments={"threadId": 42},
        )
        resp = _drain_messages(self.stream)[0]
        self.assertTrue(resp["success"])
        names = [f["name"] for f in resp["body"]["stackFrames"]]
        # DAP expects innermost at the top.
        self.assertEqual(names, ["inner_fn", "middle_fn", "main"])

    # --- Custom tintype requests (sidebar UI) -----------------------

    def _first_two_snapshot_launch(self) -> None:
        frame1 = _make_frame("/a.py", "foo", 10, {"x": 1})
        st1 = _make_stacktrace(100, [frame1], thread_name="MainThread")
        snap1 = _make_snapshot([st1], ts=1_700_000_000_000_000)

        frame2 = _make_frame("/a.py", "bar", 20, {"y": 2})
        st2 = _make_stacktrace(100, [frame2], thread_name="MainThread")
        snap2 = _make_snapshot([st2], ts=1_700_000_001_000_000)
        self._launch_with_snapshots([snap1, snap2])

    def test_tintype_snapshot_list_returns_all_entries(self) -> None:
        self._first_two_snapshot_launch()

        self.stream.seek(0)
        self.stream.truncate()
        _send_request(
            self.session, self.dispatcher, seq=200, command="tintypeSnapshotList"
        )
        resp = _drain_messages(self.stream)[0]
        self.assertTrue(resp["success"])
        body = resp["body"]
        self.assertEqual(body["currentIndex"], 0)
        self.assertEqual(len(body["snapshots"]), 2)
        self.assertEqual(
            body["snapshots"][0], {"index": 0, "timestampUs": 1_700_000_000_000_000}
        )
        self.assertEqual(
            body["snapshots"][1], {"index": 1, "timestampUs": 1_700_000_001_000_000}
        )

    def test_tintype_jump_advances_cursor_and_emits_stopped(self) -> None:
        self._first_two_snapshot_launch()

        self.stream.seek(0)
        self.stream.truncate()
        _send_request(
            self.session,
            self.dispatcher,
            seq=201,
            command="tintypeJumpToSnapshot",
            arguments={"index": 1},
        )
        messages = _drain_messages(self.stream)
        # Expect at least one response with success=true and a stopped event.
        resp = [m for m in messages if m.get("type") == "response"][0]
        self.assertTrue(resp["success"])
        # Response carries the new index + total alongside the legacy
        # ``index`` field so the client can update the sidebar's cursor
        # and length without a follow-up tintypeSnapshotList round-trip.
        self.assertEqual(resp["body"]["index"], 1)
        self.assertEqual(resp["body"]["currentIndex"], 1)
        self.assertEqual(resp["body"]["totalSnapshots"], 2)

        stopped = [m for m in messages if m.get("event") == "stopped"]
        self.assertEqual(len(stopped), 1)

        # Following list should report the new current index.
        self.stream.seek(0)
        self.stream.truncate()
        _send_request(
            self.session, self.dispatcher, seq=202, command="tintypeSnapshotList"
        )
        list_resp = _drain_messages(self.stream)[0]
        self.assertEqual(list_resp["body"]["currentIndex"], 1)

    def test_tintype_jump_rejects_missing_index(self) -> None:
        self._first_two_snapshot_launch()
        self.stream.seek(0)
        self.stream.truncate()
        _send_request(
            self.session,
            self.dispatcher,
            seq=203,
            command="tintypeJumpToSnapshot",
            arguments={},
        )
        resp = _drain_messages(self.stream)[0]
        self.assertFalse(resp["success"])
        self.assertIn("index", resp.get("message", ""))

    def test_tintype_jump_rejects_out_of_range_index(self) -> None:
        self._first_two_snapshot_launch()
        for bad in (-1, 2, 99):
            with self.subTest(index=bad):
                self.stream.seek(0)
                self.stream.truncate()
                _send_request(
                    self.session,
                    self.dispatcher,
                    seq=300 + bad,
                    command="tintypeJumpToSnapshot",
                    arguments={"index": bad},
                )
                resp = _drain_messages(self.stream)[0]
                self.assertFalse(resp["success"])
                self.assertIn("out of range", resp.get("message", ""))

    def test_tintype_jump_rejects_non_integer_index(self) -> None:
        self._first_two_snapshot_launch()
        self.stream.seek(0)
        self.stream.truncate()
        _send_request(
            self.session,
            self.dispatcher,
            seq=400,
            command="tintypeJumpToSnapshot",
            arguments={"index": "not-a-number"},
        )
        resp = _drain_messages(self.stream)[0]
        self.assertFalse(resp["success"])
        self.assertIn("int", resp.get("message", ""))

    def test_tintype_jump_rejects_bool_index(self) -> None:
        """``bool`` is a subclass of ``int`` in Python, so a bare
        ``isinstance(raw_index, int)`` check would happily accept
        ``True`` / ``False`` as indices 1 / 0. The handler explicitly
        rejects bools first; cover both values so a refactor that
        drops the ``isinstance(raw_index, bool)`` guard doesn't silently
        re-introduce the issue."""
        self._first_two_snapshot_launch()
        for seq_num, bad_index in ((410, True), (411, False)):
            with self.subTest(index=bad_index):
                self.stream.seek(0)
                self.stream.truncate()
                _send_request(
                    self.session,
                    self.dispatcher,
                    seq=seq_num,
                    command="tintypeJumpToSnapshot",
                    arguments={"index": bad_index},
                )
                resp = _drain_messages(self.stream)[0]
                self.assertFalse(resp["success"])
                self.assertIn("int", resp.get("message", ""))
                self.assertIn("bool", resp.get("message", ""))

    def test_attach_routes_through_launch_handler(self) -> None:
        """An ``attach`` request should run the full launch flow.

        Tintype sessions don't distinguish attach from launch — both
        open the ``.pytb`` and emit the initial process/thread/stopped
        events — so the dispatcher must wire ``attach`` to the same code
        path as ``launch``.
        """
        frame = _make_frame("/a/b.py", "foo", 10, {"x": 1})
        st = _make_stacktrace(100, [frame], thread_name="MainThread")
        snap = _make_snapshot([st])

        reader = MagicMock()
        reader.snapshot_count.return_value = 1
        reader.get_all_source_files.return_value = []
        reader.get_all_snapshots.return_value = [snap]
        reader.get_snapshot_at_index.side_effect = lambda i: snap if i == 0 else None

        with (
            patch("tintype.dap.session.SnapshotReader", return_value=reader),
            patch("tintype.dap.session.os.path.isfile", return_value=True),
        ):
            _send_request(
                self.session,
                self.dispatcher,
                seq=1,
                command="attach",
                arguments={"pytbPath": "/fake/snap.pytb"},
            )

        messages = _drain_messages(self.stream)
        event_names = [m.get("event") for m in messages if m["type"] == "event"]
        # Attach must produce the same initial event sequence as launch.
        # (No ``process`` event — see test_launch_emits_initial_events for
        # why we deliberately don't emit one.)
        self.assertIn("initialized", event_names)
        self.assertNotIn("process", event_names)
        self.assertIn("thread", event_names)
        self.assertIn("stopped", event_names)

        # The attach response must succeed.
        attach_resps = [
            m
            for m in messages
            if m.get("type") == "response" and m.get("command") == "attach"
        ]
        self.assertEqual(len(attach_resps), 1)
        self.assertTrue(attach_resps[0]["success"])

    def test_restart_jumps_cursor_to_first_snapshot_and_re_emits_stopped(
        self,
    ) -> None:
        """``restart`` must reset the cursor to snapshot #0 and re-emit stopped.

        There's no runtime to restart in a snapshot session, so the
        adapter reinterprets the DAP ``restart`` request as "jump back
        to the first snapshot".
        """
        snap1 = _make_snapshot(
            [_make_stacktrace(100, [_make_frame("/a/b.py", "foo", 1, {})])],
            ts=1_700_000_000_000_000,
        )
        snap2 = _make_snapshot(
            [_make_stacktrace(100, [_make_frame("/a/b.py", "foo", 2, {})])],
            ts=1_700_000_005_000_000,
        )
        self._launch_with_snapshots([snap1, snap2])

        # Advance past the first snapshot so restart has somewhere to jump back from.
        _send_request(self.session, self.dispatcher, seq=100, command="continue")
        # Internal state should reflect the cursor being on snapshot #2.
        self.assertEqual(self.session._snapshot_index, 1)

        # Restart should reset the cursor and re-emit stopped.
        self.stream.seek(0)
        self.stream.truncate()
        _send_request(self.session, self.dispatcher, seq=102, command="restart")

        messages = _drain_messages(self.stream)
        restart_resps = [
            m
            for m in messages
            if m.get("type") == "response" and m.get("command") == "restart"
        ]
        self.assertEqual(len(restart_resps), 1)
        self.assertTrue(restart_resps[0]["success"])

        event_names = [m.get("event") for m in messages if m["type"] == "event"]
        self.assertIn("stopped", event_names)
        # Restart does NOT emit a ``process`` event — see
        # test_launch_emits_initial_events for why we deliberately never
        # emit one.
        self.assertNotIn("process", event_names)

        # Stopped event now carries the snapshot ``description``. Unlike
        # launch, restart only emits a single stopped event (no bootstrap)
        # — the VS Code race is an initial-launch quirk, so subsequent
        # stops take effect on the first try.
        stopped_events = [
            m
            for m in messages
            if m.get("type") == "event" and m.get("event") == "stopped"
        ]
        self.assertEqual(len(stopped_events), 1)
        self.assertIn("description", stopped_events[0]["body"])
        self.assertIn("Snapshot", stopped_events[0]["body"]["description"])

        # The cursor must now be back on snapshot #0.
        self.assertEqual(self.session._snapshot_index, 0)
        self.assertFalse(self.session.terminated)

    def test_stopped_event_preserves_last_focused_thread_across_snapshots(
        self,
    ) -> None:
        """After the user inspects a thread via stackTrace, subsequent
        snapshot stops should re-focus a same-named thread.

        The user's focus normally bounces to whichever thread has an
        exception on every stop, which is jarring while navigating
        through snapshots. We track the thread-name of the most recent
        ``stackTrace`` request and prefer that name when picking the
        threadId for the next ``stopped`` event.
        """
        # Snapshot 1: two threads, an exception in Worker.
        main1 = _make_frame("/a/b.py", "main", 1, {})
        worker1 = _make_frame("/a/b.py", "work", 1, {})
        snap1 = _make_snapshot(
            [
                _make_stacktrace(100, [main1], thread_name="MainThread"),
                _make_stacktrace(
                    200, [worker1], thread_name="Worker", exception=ValueError("x")
                ),
            ],
            ts=1_700_000_000_000_000,
        )

        # Snapshot 2: same two threads — both present, exception still in
        # Worker. Without preservation, focus would snap back to Worker
        # (exception-first fallback). With preservation, if the user
        # last inspected MainThread, focus should stay on MainThread.
        main2 = _make_frame("/a/b.py", "main", 2, {})
        worker2 = _make_frame("/a/b.py", "work", 2, {})
        snap2 = _make_snapshot(
            [
                _make_stacktrace(100, [main2], thread_name="MainThread"),
                _make_stacktrace(
                    200, [worker2], thread_name="Worker", exception=ValueError("x")
                ),
            ],
            ts=1_700_000_005_000_000,
        )

        self._launch_with_snapshots([snap1, snap2])

        # Initial stopped event should focus the exception thread
        # (Worker, id=200) because no thread preference exists yet.
        launch_messages = _drain_messages(self.stream)
        launch_stopped = next(m for m in launch_messages if m.get("event") == "stopped")
        self.assertEqual(launch_stopped["body"]["threadId"], 200)

        # User clicks MainThread in the CALL STACK — VS Code issues
        # stackTrace against that thread.
        self.stream.seek(0)
        self.stream.truncate()
        _send_request(
            self.session,
            self.dispatcher,
            seq=100,
            command="stackTrace",
            arguments={"threadId": 100},
        )

        # Jump forward to snapshot 2. The preferred-thread logic should
        # land focus on MainThread (id=100), not the exception thread.
        self.stream.seek(0)
        self.stream.truncate()
        _send_request(self.session, self.dispatcher, seq=101, command="continue")
        jump_messages = _drain_messages(self.stream)
        jump_stopped = next(m for m in jump_messages if m.get("event") == "stopped")
        self.assertEqual(jump_stopped["body"]["threadId"], 100)

    def test_stopped_event_falls_back_when_preferred_thread_vanishes(
        self,
    ) -> None:
        """When the preferred thread isn't in the new snapshot, fall
        back to exception-first — don't just pick the first thread
        blindly."""
        # Snapshot 1: two threads, Worker has an exception.
        snap1 = _make_snapshot(
            [
                _make_stacktrace(
                    100,
                    [_make_frame("/a/b.py", "main", 1, {})],
                    thread_name="MainThread",
                ),
                _make_stacktrace(
                    200,
                    [_make_frame("/a/b.py", "work", 1, {})],
                    thread_name="Worker",
                    exception=ValueError("boom"),
                ),
            ],
            ts=1_700_000_000_000_000,
        )
        # Snapshot 2: the MainThread has disappeared (e.g. it exited).
        # Only Worker remains, still with its exception.
        snap2 = _make_snapshot(
            [
                _make_stacktrace(
                    200,
                    [_make_frame("/a/b.py", "work", 2, {})],
                    thread_name="Worker",
                    exception=ValueError("boom"),
                ),
            ],
            ts=1_700_000_005_000_000,
        )
        self._launch_with_snapshots([snap1, snap2])

        # User focuses MainThread.
        self.stream.seek(0)
        self.stream.truncate()
        _send_request(
            self.session,
            self.dispatcher,
            seq=200,
            command="stackTrace",
            arguments={"threadId": 100},
        )

        # Advance past MainThread's disappearance; focus must fall back
        # to the exception thread (Worker, 200), NOT to any first-in-map
        # sentinel.
        self.stream.seek(0)
        self.stream.truncate()
        _send_request(self.session, self.dispatcher, seq=201, command="continue")
        jump_messages = _drain_messages(self.stream)
        jump_stopped = next(m for m in jump_messages if m.get("event") == "stopped")
        self.assertEqual(jump_stopped["body"]["threadId"], 200)

    def test_default_filter_deemphasizes_pydevd_debugpy_frames(self) -> None:
        """Out of the box, pydevd/debugpy frames should be deemphasized."""
        # Innermost first (DAP order, matching tintype's storage).
        user_frame = _make_frame("/app/main.py", "main", 10, {})
        pydevd_frame = _make_frame(
            "/usr/lib/python3/site-packages/pydevd/pydevd.py",
            "do_wait_suspend",
            1234,
            {},
        )
        debugpy_frame = _make_frame(
            "/usr/lib/python3/site-packages/debugpy/_vendored/pydevd/_pydev_imps/_pydev_execfile.py",
            "execfile",
            25,
            {},
        )
        st = _make_stacktrace(
            100,
            [user_frame, pydevd_frame, debugpy_frame],
            thread_name="MainThread",
        )
        snap = _make_snapshot([st])
        self._launch_with_snapshots([snap])

        self.stream.seek(0)
        self.stream.truncate()
        _send_request(
            self.session,
            self.dispatcher,
            seq=600,
            command="stackTrace",
            arguments={"threadId": 100},
        )
        resp = _drain_messages(self.stream)[0]
        self.assertTrue(resp["success"])
        frames = resp["body"]["stackFrames"]
        self.assertEqual(
            len(frames), 3, "all frames should still be present by default"
        )

        # User frame stays ``normal``; pydevd + debugpy frames get
        # ``deemphasize``.
        hints = [f["presentationHint"] for f in frames]
        self.assertEqual(hints[0], "normal")
        self.assertEqual(hints[1], "deemphasize")
        self.assertEqual(hints[2], "deemphasize")

    def test_hide_filtered_frames_drops_them_from_stack(self) -> None:
        """``hideFilteredFrames: true`` removes matching frames entirely."""
        user_frame = _make_frame("/app/main.py", "main", 10, {})
        pydevd_frame = _make_frame("/pydevd/pydevd.py", "do_wait_suspend", 1, {})
        st = _make_stacktrace(100, [user_frame, pydevd_frame], thread_name="MainThread")
        snap = _make_snapshot([st])

        reader = MagicMock()
        reader.snapshot_count.return_value = 1
        reader.get_all_source_files.return_value = []
        reader.get_all_snapshots.return_value = [snap]
        reader.get_snapshot_at_index.side_effect = lambda i: snap if i == 0 else None

        with (
            patch("tintype.dap.session.SnapshotReader", return_value=reader),
            patch("tintype.dap.session.os.path.isfile", return_value=True),
        ):
            _send_request(
                self.session,
                self.dispatcher,
                seq=1,
                command="launch",
                arguments={
                    "pytbPath": "/fake/snap.pytb",
                    "hideFilteredFrames": True,
                },
            )

        self.stream.seek(0)
        self.stream.truncate()
        _send_request(
            self.session,
            self.dispatcher,
            seq=601,
            command="stackTrace",
            arguments={"threadId": 100},
        )
        resp = _drain_messages(self.stream)[0]
        frames = resp["body"]["stackFrames"]
        # Only the user frame remains.
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["name"], "main")
        # And it's marked normal (no deemphasize in hide mode).
        self.assertEqual(frames[0]["presentationHint"], "normal")
        # totalFrames must reflect the post-filter count so scrolling
        # doesn't try to load phantom frames.
        self.assertEqual(resp["body"]["totalFrames"], 1)

    def test_include_pattern_rescues_specific_file(self) -> None:
        """``includeFramePaths`` wins over excludes for matching paths."""
        user_frame = _make_frame("/app/main.py", "main", 10, {})
        rescued_frame = _make_frame(
            "/pydevd/_pydevd_bundle/pydevd_runpy.py", "run_module_as_main", 1, {}
        )
        other_pydevd_frame = _make_frame("/pydevd/pydevd.py", "do_wait_suspend", 1, {})
        st = _make_stacktrace(
            100,
            [user_frame, rescued_frame, other_pydevd_frame],
            thread_name="MainThread",
        )
        snap = _make_snapshot([st])

        reader = MagicMock()
        reader.snapshot_count.return_value = 1
        reader.get_all_source_files.return_value = []
        reader.get_all_snapshots.return_value = [snap]
        reader.get_snapshot_at_index.side_effect = lambda i: snap if i == 0 else None

        with (
            patch("tintype.dap.session.SnapshotReader", return_value=reader),
            patch("tintype.dap.session.os.path.isfile", return_value=True),
        ):
            _send_request(
                self.session,
                self.dispatcher,
                seq=1,
                command="launch",
                arguments={
                    "pytbPath": "/fake/snap.pytb",
                    "includeFramePaths": ["pydevd_runpy\\.py"],
                },
            )

        self.stream.seek(0)
        self.stream.truncate()
        _send_request(
            self.session,
            self.dispatcher,
            seq=602,
            command="stackTrace",
            arguments={"threadId": 100},
        )
        resp = _drain_messages(self.stream)[0]
        frames = resp["body"]["stackFrames"]
        self.assertEqual(len(frames), 3)
        hints = [f["presentationHint"] for f in frames]
        # User frame: normal. Rescued pydevd_runpy: normal (include wins).
        # Other pydevd frame: deemphasize.
        self.assertEqual(hints[0], "normal")
        self.assertEqual(hints[1], "normal")
        self.assertEqual(hints[2], "deemphasize")

    def test_pydevd_only_thread_is_omitted_from_threads_response(self) -> None:
        """Threads consisting entirely of filtered frames are dropped."""
        user_st = _make_stacktrace(
            100,
            [_make_frame("/app/main.py", "main", 1, {})],
            thread_name="MainThread",
        )
        # pydevd service thread — all frames come from pydevd.
        service_st = _make_stacktrace(
            200,
            [
                _make_frame("/pydevd/pydevd.py", "_on_run", 1, {}),
                _make_frame("/pydevd/pydevd_comm.py", "run", 1, {}),
            ],
            thread_name="pydevd.Reader",
        )
        snap = _make_snapshot([user_st, service_st])
        self._launch_with_snapshots([snap])

        self.stream.seek(0)
        self.stream.truncate()
        _send_request(self.session, self.dispatcher, seq=700, command="threads")
        resp = _drain_messages(self.stream)[0]
        threads = resp["body"]["threads"]
        # Only MainThread survives.
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0]["id"], 100)

    def test_user_thread_with_mixed_frames_is_kept(self) -> None:
        """A thread with any user frame survives — only the frames are filtered."""
        mixed_st = _make_stacktrace(
            100,
            [
                _make_frame("/app/main.py", "main", 1, {}),
                _make_frame("/pydevd/pydevd.py", "do_wait_suspend", 1, {}),
            ],
            thread_name="MainThread",
        )
        snap = _make_snapshot([mixed_st])
        self._launch_with_snapshots([snap])

        self.stream.seek(0)
        self.stream.truncate()
        _send_request(self.session, self.dispatcher, seq=701, command="threads")
        threads_resp = _drain_messages(self.stream)[0]
        self.assertEqual(len(threads_resp["body"]["threads"]), 1)

    def test_explicit_empty_exclude_list_disables_filter(self) -> None:
        """Passing ``excludeFramePaths: []`` disables the default filter."""
        pydevd_frame = _make_frame("/pydevd/pydevd.py", "do_wait_suspend", 1, {})
        st = _make_stacktrace(100, [pydevd_frame], thread_name="MainThread")
        snap = _make_snapshot([st])

        reader = MagicMock()
        reader.snapshot_count.return_value = 1
        reader.get_all_source_files.return_value = []
        reader.get_all_snapshots.return_value = [snap]
        reader.get_snapshot_at_index.side_effect = lambda i: snap if i == 0 else None

        with (
            patch("tintype.dap.session.SnapshotReader", return_value=reader),
            patch("tintype.dap.session.os.path.isfile", return_value=True),
        ):
            _send_request(
                self.session,
                self.dispatcher,
                seq=1,
                command="launch",
                arguments={
                    "pytbPath": "/fake/snap.pytb",
                    "excludeFramePaths": [],
                },
            )

        # With an empty exclude list, the pydevd-only thread should NOT
        # be filtered out — the filter is disabled entirely.
        self.stream.seek(0)
        self.stream.truncate()
        _send_request(self.session, self.dispatcher, seq=702, command="threads")
        threads_resp = _drain_messages(self.stream)[0]
        self.assertEqual(len(threads_resp["body"]["threads"]), 1)
        self.assertEqual(threads_resp["body"]["threads"][0]["id"], 100)

        # And the frame should render normal.
        self.stream.seek(0)
        self.stream.truncate()
        _send_request(
            self.session,
            self.dispatcher,
            seq=703,
            command="stackTrace",
            arguments={"threadId": 100},
        )
        stack_resp = _drain_messages(self.stream)[0]
        self.assertEqual(
            stack_resp["body"]["stackFrames"][0]["presentationHint"], "normal"
        )

    def test_invalid_regex_is_skipped_with_output_event(self) -> None:
        """A bad pattern shouldn't break the session — just warn and skip."""
        user_frame = _make_frame("/app/main.py", "main", 1, {})
        st = _make_stacktrace(100, [user_frame], thread_name="MainThread")
        snap = _make_snapshot([st])

        reader = MagicMock()
        reader.snapshot_count.return_value = 1
        reader.get_all_source_files.return_value = []
        reader.get_all_snapshots.return_value = [snap]
        reader.get_snapshot_at_index.side_effect = lambda i: snap if i == 0 else None

        with (
            patch("tintype.dap.session.SnapshotReader", return_value=reader),
            patch("tintype.dap.session.os.path.isfile", return_value=True),
        ):
            _send_request(
                self.session,
                self.dispatcher,
                seq=1,
                command="launch",
                arguments={
                    "pytbPath": "/fake/snap.pytb",
                    "excludeFramePaths": ["[unclosed"],
                },
            )

        messages = _drain_messages(self.stream)
        outputs = [
            m
            for m in messages
            if m.get("event") == "output"
            and "invalid excludeFramePaths" in m["body"].get("output", "")
        ]
        self.assertEqual(len(outputs), 1)

    def test_default_filter_deemphasizes_threading_frames(self) -> None:
        """``threading.py`` is part of the default exclude set."""
        user_frame = _make_frame("/app/main.py", "main", 10, {})
        threading_bootstrap = _make_frame(
            "/usr/lib/python3.14/threading.py", "_bootstrap", 1012, {}
        )
        threading_inner = _make_frame(
            "/usr/lib/python3.14/threading.py", "_bootstrap_inner", 1022, {}
        )
        st = _make_stacktrace(
            100,
            [user_frame, threading_bootstrap, threading_inner],
            thread_name="Worker",
        )
        snap = _make_snapshot([st])
        self._launch_with_snapshots([snap])

        self.stream.seek(0)
        self.stream.truncate()
        _send_request(
            self.session,
            self.dispatcher,
            seq=800,
            command="stackTrace",
            arguments={"threadId": 100},
        )
        resp = _drain_messages(self.stream)[0]
        frames = resp["body"]["stackFrames"]
        self.assertEqual(
            [f["presentationHint"] for f in frames],
            ["normal", "deemphasize", "deemphasize"],
        )

    def test_default_filter_deemphasizes_queue_frames(self) -> None:
        """``queue.py`` plumbing is part of the default exclude set."""
        user_frame = _make_frame("/app/main.py", "main", 10, {})
        queue_get = _make_frame("/usr/lib/python3.14/queue.py", "get", 171, {})
        st = _make_stacktrace(101, [user_frame, queue_get], thread_name="Worker")
        snap = _make_snapshot([st])
        self._launch_with_snapshots([snap])

        self.stream.seek(0)
        self.stream.truncate()
        _send_request(
            self.session,
            self.dispatcher,
            seq=801,
            command="stackTrace",
            arguments={"threadId": 101},
        )
        resp = _drain_messages(self.stream)[0]
        frames = resp["body"]["stackFrames"]
        self.assertEqual(
            [f["presentationHint"] for f in frames],
            ["normal", "deemphasize"],
        )

    def test_default_filter_deemphasizes_string_exec_frames(self) -> None:
        """Frames whose ``co_filename`` is exactly ``<string>`` —
        produced by ``compile(code, "<string>", "exec")`` (e.g. pydevd
        evaluate requests and user ``eval``/``exec`` calls) — are
        deemphasized without matching real user source files.

        The anchor is ``^<string>$`` (exact match). Historically the
        C++ ``SnapshotReader`` also surfaced munged paths like
        ``/tmp/snapshot_files_Xxxxxx<string>`` via a raw
        ``extractedFilesDir_ + file.path`` concat; the reader now
        skips synthetic ``<...>`` filenames at stage time (see
        ``tintype/snapshot_lib/SnapshotReader.cpp``), so the frame's
        ``file_path`` is preserved as the raw ``<string>`` and the
        tight anchor is sufficient. A hypothetical munged path that
        somehow still appeared should render normally — the
        ``^...$`` anchor protects against over-matching real paths
        that merely happen to end in the literal ``<string>``
        substring.
        """
        user_frame = _make_frame("/app/main.py", "main", 10, {})
        evaluate_frame = _make_frame("<string>", "<module>", 1, {})
        # Munged path that the pre-fix SnapshotReader used to produce.
        # The reader fix prevents this shape from surfacing in new
        # snapshots, so we now treat it as a normal path — we cannot
        # safely deemphasize arbitrary paths ending in ``<string>``
        # because ``<``/``>`` *are* legal on some filesystems.
        hypothetical_munged = _make_frame(
            "/tmp/snapshot_files_Dgqcba<string>", "<module>", 1, {}
        )
        # A deliberately crafted path containing ``<string>`` mid-name
        # — the ``^...$`` anchor rejects these.
        user_lookalike = _make_frame("/app/weird<string>file.py", "run", 5, {})
        st = _make_stacktrace(
            102,
            [user_frame, user_lookalike, evaluate_frame, hypothetical_munged],
            thread_name="MainThread",
        )
        snap = _make_snapshot([st])
        self._launch_with_snapshots([snap])

        self.stream.seek(0)
        self.stream.truncate()
        _send_request(
            self.session,
            self.dispatcher,
            seq=802,
            command="stackTrace",
            arguments={"threadId": 102},
        )
        resp = _drain_messages(self.stream)[0]
        frames = resp["body"]["stackFrames"]
        self.assertEqual(
            [f["presentationHint"] for f in frames],
            ["normal", "normal", "deemphasize", "normal"],
        )

    def test_extend_default_exclude_frame_paths_appends_additively(self) -> None:
        """Meta-internal wrappers can append their own patterns."""
        original = self._saved_default_exclude
        extend_default_exclude_frame_paths(
            [r"/my_internal_framework/", r"/fb_scaffolding\.py$"]
        )
        self.assertEqual(
            DEFAULT_EXCLUDE_FRAME_PATHS,
            original + [r"/my_internal_framework/", r"/fb_scaffolding\.py$"],
        )

        # Verify the appended pattern actually applies at session time.
        user_frame = _make_frame("/app/main.py", "main", 1, {})
        framework_frame = _make_frame(
            "/opt/my_internal_framework/runner.py", "run", 42, {}
        )
        st = _make_stacktrace(
            100, [user_frame, framework_frame], thread_name="MainThread"
        )
        snap = _make_snapshot([st])
        self._launch_with_snapshots([snap])

        self.stream.seek(0)
        self.stream.truncate()
        _send_request(
            self.session,
            self.dispatcher,
            seq=801,
            command="stackTrace",
            arguments={"threadId": 100},
        )
        resp = _drain_messages(self.stream)[0]
        hints = [f["presentationHint"] for f in resp["body"]["stackFrames"]]
        self.assertEqual(hints, ["normal", "deemphasize"])

    def test_extend_default_exclude_frame_paths_ignores_junk(self) -> None:
        """Non-string / empty-string entries are dropped silently."""
        original = self._saved_default_exclude
        extend_default_exclude_frame_paths(
            ["", None, 42, r"/real_pattern/"]  # pyre-ignore[6]
        )
        self.assertEqual(DEFAULT_EXCLUDE_FRAME_PATHS, original + [r"/real_pattern/"])

    def test_set_default_exclude_frame_paths_replaces_wholesale(self) -> None:
        """``set_default_exclude_frame_paths`` swaps the list in-place."""
        set_default_exclude_frame_paths([r"/only_this/"])
        self.assertEqual(DEFAULT_EXCLUDE_FRAME_PATHS, [r"/only_this/"])

        # Previous patterns no longer apply — a pydevd frame now
        # renders ``normal`` because we dropped the baseline.
        pydevd_frame = _make_frame("/pydevd/pydevd.py", "do_wait_suspend", 1, {})
        user_frame = _make_frame("/app/main.py", "main", 1, {})
        st = _make_stacktrace(100, [user_frame, pydevd_frame], thread_name="MainThread")
        snap = _make_snapshot([st])
        self._launch_with_snapshots([snap])

        self.stream.seek(0)
        self.stream.truncate()
        _send_request(
            self.session,
            self.dispatcher,
            seq=802,
            command="stackTrace",
            arguments={"threadId": 100},
        )
        hints = [
            f["presentationHint"]
            for f in _drain_messages(self.stream)[0]["body"]["stackFrames"]
        ]
        self.assertEqual(hints, ["normal", "normal"])


class StoppedEventDescriptionTest(unittest.TestCase):
    """``stopped`` events carry a ``description`` field with the snapshot's
    display name. On launch we send a bootstrap ``"Snapshot"`` event first so
    VS Code has something to overwrite on its initial-launch race, followed
    by the real description event the user actually sees."""

    def setUp(self) -> None:
        self.stream = RecordingStream()
        self.dispatcher = Dispatcher(self.stream)
        self.session = SnapshotDebugSession(self.dispatcher)
        self.session.wire()

    def _launch_with_snapshots(self, snapshots: list[Any]) -> MagicMock:
        reader = MagicMock()
        reader.snapshot_count.return_value = len(snapshots)
        reader.get_all_source_files.return_value = []
        reader.get_all_snapshots.return_value = snapshots
        reader.get_snapshot_at_index.side_effect = (
            lambda i: snapshots[i] if 0 <= i < len(snapshots) else None
        )
        with (
            patch("tintype.dap.session.SnapshotReader", return_value=reader),
            patch("tintype.dap.session.os.path.isfile", return_value=True),
        ):
            _send_request(
                self.session,
                self.dispatcher,
                seq=1,
                command="launch",
                arguments={"pytbPath": "/fake/snap.pytb"},
            )
        return reader

    def test_launch_description_includes_timestamp_and_index(self) -> None:
        st = _make_stacktrace(
            100, [_make_frame("/a/b.py", "foo", 1, {})], thread_name="MainThread"
        )
        snap = _make_snapshot([st], ts=1_700_000_000_000_000)
        self._launch_with_snapshots([snap])

        stopped_events = [
            m for m in _drain_messages(self.stream) if m.get("event") == "stopped"
        ]
        real = stopped_events[-1]["body"]
        self.assertIn("description", real)
        self.assertIn("Snapshot", real["description"])
        # HH:MM:SS.fff pattern must show up in the real-stop description.
        self.assertRegex(real["description"], r"\d{2}:\d{2}:\d{2}\.\d{3}")

    def test_description_mentions_exception_type(self) -> None:
        st = _make_stacktrace(
            100,
            [_make_frame("/a/b.py", "foo", 1, {})],
            thread_name="MainThread",
            exception=KeyError("missing"),
        )
        snap = _make_snapshot([st])
        self._launch_with_snapshots([snap])

        real = [m for m in _drain_messages(self.stream) if m.get("event") == "stopped"][
            -1
        ]["body"]
        self.assertIn("KeyError", real["description"])

    def test_description_updates_across_snapshot_jumps(self) -> None:
        snap1 = _make_snapshot(
            [
                _make_stacktrace(
                    100,
                    [_make_frame("/a/b.py", "foo", 1, {})],
                    thread_name="MainThread",
                )
            ],
            ts=1_700_000_000_000_000,
        )
        snap2 = _make_snapshot(
            [
                _make_stacktrace(
                    100,
                    [_make_frame("/a/b.py", "foo", 2, {})],
                    thread_name="MainThread",
                )
            ],
            ts=1_700_000_005_000_000,
        )
        self._launch_with_snapshots([snap1, snap2])

        launch_desc = [
            m for m in _drain_messages(self.stream) if m.get("event") == "stopped"
        ][-1]["body"]["description"]
        self.assertIn("1/2", launch_desc)

        self.stream.seek(0)
        self.stream.truncate()
        _send_request(
            self.session,
            self.dispatcher,
            seq=50,
            command="tintypeJumpToSnapshot",
            arguments={"index": 1},
        )
        jump_events = [
            m for m in _drain_messages(self.stream) if m.get("event") == "stopped"
        ]
        # Jump emits exactly one stopped event (no bootstrap).
        self.assertEqual(len(jump_events), 1)
        jump_desc = jump_events[0]["body"]["description"]
        self.assertIn("2/2", jump_desc)
        self.assertNotEqual(launch_desc, jump_desc)


if __name__ == "__main__":
    unittest.main()
