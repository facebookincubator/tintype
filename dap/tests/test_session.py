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
from tintype.dap.session import SnapshotDebugSession
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
        # We expect: launch response, initialized, thread(s), stopped.
        # We deliberately emit NO ``process`` event and NO ``description``
        # on the stopped event — both surfaces (CALL STACK session row
        # and thread row) ended up fighting for the same cell in ways
        # VS Code wouldn't render consistently. The Tintype Snapshots
        # sidebar group description is the single authoritative cursor
        # surface.
        event_names = [m.get("event") for m in messages if m["type"] == "event"]
        self.assertIn("initialized", event_names)
        self.assertNotIn("process", event_names)
        self.assertIn("thread", event_names)
        self.assertIn("stopped", event_names)

        stopped_event = next(m for m in messages if m.get("event") == "stopped")
        self.assertNotIn("description", stopped_event["body"])

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


if __name__ == "__main__":
    unittest.main()
