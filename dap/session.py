# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Snapshot-navigation state machine and DAP request handlers.

Holds the :class:`SnapshotReader` plus a cursor into the snapshot list.
Each DAP handler is a plain Python method that consumes an ``arguments``
dict and returns a response body dict. :class:`SnapshotDebugSession.wire`
installs the handlers onto a :class:`~tintype.dap.dispatcher.Dispatcher`.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

from tintype import Frame, Snapshot, SnapshotReader, Stacktrace
from tintype.dap.dispatcher import Dispatcher, DispatchError
from tintype.dap.evaluate import evaluate_expression, EvaluateError
from tintype.dap.exceptions import build_exception_info
from tintype.dap.sources import SourceRegistry
from tintype.dap.variables import expand, make_variable, VariableRegistry


logger: logging.Logger = logging.getLogger(__name__)


class SnapshotDebugSession:
    """Owns per-session state: reader, cursor, registries."""

    def __init__(self, dispatcher: Dispatcher) -> None:
        self._dispatcher = dispatcher

        self._reader: SnapshotReader | None = None
        self._pytb_path: str = ""
        self._snapshot_index: int = 0
        self._current_snapshot: Snapshot | None = None

        # Frame-id registry. frame_id -> (stacktrace_id, frame_index). Frame
        # IDs are reissued each snapshot because frame objects change; they
        # start at 1 because 0 means "no frame" in DAP.
        self._next_frame_id: int = 1
        self._frames: dict[int, tuple[int, int]] = {}
        # Reverse index keeps ``_register_frame`` O(1) instead of scanning
        # ``_frames`` linearly (which would make ``stackTrace`` population
        # O(N^2) on deep stacks).
        self._frame_ids: dict[tuple[int, int], int] = {}
        # frame_id -> scope_ref cache so repeated ``scopes(frameId)``
        # requests (VS Code re-issues them when refocusing) return the same
        # reference instead of accumulating stale ones.
        self._frame_scopes: dict[int, int] = {}

        self._variables: VariableRegistry = VariableRegistry()
        self._sources: SourceRegistry = SourceRegistry()

        # Name of the thread the user most recently inspected (captured
        # from ``handle_stack_trace``). ``_pick_stop_thread`` prefers a
        # same-named thread in the new snapshot on every jump so focus
        # doesn't jump around to whatever thread happens to have an
        # exception. Cleared to ``None`` at launch so the very first
        # stop uses the exception-first fallback.
        self._preferred_thread_name: str | None = None

        self._terminated: bool = False

    # ---------------------------------------------------------------
    # Wiring
    # ---------------------------------------------------------------

    def wire(self) -> None:
        """Install handlers onto the dispatcher."""
        self._dispatcher.register_many(
            {
                "initialize": self.handle_initialize,
                "launch": self.handle_launch,
                "attach": self.handle_attach,
                "restart": self.handle_restart,
                "configurationDone": self.handle_configuration_done,
                "threads": self.handle_threads,
                "stackTrace": self.handle_stack_trace,
                "scopes": self.handle_scopes,
                "variables": self.handle_variables,
                "source": self.handle_source,
                "evaluate": self.handle_evaluate,
                "exceptionInfo": self.handle_exception_info,
                "continue": self.handle_continue,
                "next": self.handle_next,
                "stepIn": self.handle_step_in,
                "stepOut": self.handle_step_out,
                "stepBack": self.handle_step_back,
                "reverseContinue": self.handle_reverse_continue,
                "pause": self.handle_pause,
                "disconnect": self.handle_disconnect,
                "terminate": self.handle_terminate,
                "setExceptionBreakpoints": self.handle_set_exception_breakpoints,
                "setBreakpoints": self.handle_set_breakpoints,
            }
        )

    @property
    def terminated(self) -> bool:
        return self._terminated

    # ---------------------------------------------------------------
    # Handlers — lifecycle
    # ---------------------------------------------------------------

    def handle_initialize(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        """Advertise adapter capabilities."""
        return {
            "supportsConfigurationDoneRequest": True,
            "supportsStepBack": True,
            "supportsEvaluateForHovers": True,
            "supportsExceptionInfoRequest": True,
            "supportsTerminateRequest": True,
            "supportsSetVariable": False,
            "supportsRestartRequest": True,
            "supportsSingleThreadExecutionRequests": False,
            "exceptionBreakpointFilters": [],
        }

    def handle_launch(self, arguments: dict[str, Any]) -> dict[str, Any] | None:
        """Open the ``.pytb`` file and emit initial events."""
        pytb = arguments.get("pytbPath") or arguments.get("program")
        if not isinstance(pytb, str) or not pytb:
            raise DispatchError("launch.arguments.pytbPath is required")
        if not os.path.isfile(pytb):
            raise DispatchError(f"pytb file not found: {pytb}")

        try:
            reader = SnapshotReader(pytb)
        except RuntimeError as e:
            raise DispatchError(f"failed to open snapshot: {e}") from e

        if reader.snapshot_count() == 0:
            raise DispatchError("snapshot file contains no snapshots")

        self._reader = reader
        self._pytb_path = pytb
        self._sources.load_from_reader(reader)

        self._dispatcher.send_event("initialized")

        # Load the first snapshot. ``initialized`` is what tells VS Code it
        # can start sending configuration requests (breakpoints, exception
        # filters, etc.); send it before ``stopped`` so the client is ready.
        # ``_load_snapshot`` emits the single ``process`` event for this
        # session — VS Code bakes ``body.name`` into the CALL STACK sub-line
        # at launch time and ignores later emissions, so we only emit once.
        start_index = int(arguments.get("snapshotIndex") or 0)
        start_index = max(0, min(start_index, reader.snapshot_count() - 1))
        self._load_snapshot(start_index)

        self._emit_thread_events_started()
        self._send_stopped_event()
        return None

    def handle_attach(self, _arguments: dict[str, Any]) -> dict[str, Any] | None:
        """Attach mirrors launch — the snapshot adapter has no other state."""
        return self.handle_launch(_arguments)

    def handle_restart(self, _arguments: dict[str, Any]) -> dict[str, Any] | None:
        """Reset the cursor to snapshot #0 and re-emit ``stopped``.

        Restart in a snapshot session doesn't relaunch anything — there's
        no runtime to restart — but the DAP ``restart`` request is the
        natural fit for "jump the cursor back to the first snapshot".
        Reuses :meth:`_load_snapshot` so frame / variable state is rebuilt
        cleanly, then re-emits ``stopped`` so VS Code re-populates the
        CALL STACK and VARIABLES panels against the new cursor.
        """
        self._require_reader()
        self._load_snapshot(0)
        self._send_stopped_event()
        return None

    def handle_configuration_done(
        self, _arguments: dict[str, Any]
    ) -> dict[str, Any] | None:
        # Nothing to do; the client has finished sending breakpoints etc.
        return None

    def handle_set_exception_breakpoints(
        self, _arguments: dict[str, Any]
    ) -> dict[str, Any]:
        # Snapshots are already past any exceptions; accept and ignore.
        return {"breakpoints": []}

    def handle_set_breakpoints(self, arguments: dict[str, Any]) -> dict[str, Any]:
        # Accept but immediately mark each breakpoint as unverified — there's
        # no runtime to hit them.
        bps = arguments.get("breakpoints") or []
        return {
            "breakpoints": [
                {
                    "verified": False,
                    "message": "breakpoints are inert in tintype snapshots",
                }
                for _ in bps
            ]
        }

    # ---------------------------------------------------------------
    # Handlers — introspection
    # ---------------------------------------------------------------

    def handle_threads(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        snapshot = self._require_snapshot()
        threads: list[dict[str, Any]] = []
        for stacktrace in snapshot.stacktraces.values():
            threads.append(
                {
                    "id": int(stacktrace.id),
                    "name": _describe_thread(stacktrace),
                }
            )
        return {"threads": threads}

    def handle_stack_trace(self, arguments: dict[str, Any]) -> dict[str, Any]:
        snapshot = self._require_snapshot()
        thread_id = int(arguments.get("threadId", 0))
        start_frame = int(arguments.get("startFrame") or 0)
        levels = int(arguments.get("levels") or 0)

        stacktrace = snapshot.stacktraces.get(thread_id)
        if stacktrace is None:
            raise DispatchError(f"thread {thread_id} not found in snapshot")

        # Remember this thread as the user's "preferred" one so the next
        # snapshot's stopped event can re-focus a same-named thread
        # instead of bouncing focus to whichever thread happens to have
        # an exception. VS Code emits ``stackTrace`` immediately after
        # the user clicks a thread in CALL STACK, so this is the
        # natural hook.
        self._preferred_thread_name = stacktrace.thread_name or f"Thread {thread_id}"

        # DAP expects frame[0] to be the innermost (most recently called)
        # frame. Tintype already stores frames innermost-first, so we must
        # NOT reverse — return them as-is.
        ordered = list(stacktrace.frames)
        total = len(ordered)

        if start_frame < 0:
            start_frame = 0
        if levels <= 0:
            end = total
        else:
            end = min(total, start_frame + levels)

        dap_frames: list[dict[str, Any]] = []
        for idx in range(start_frame, end):
            frame_obj = ordered[idx]
            frame_id = self._register_frame(thread_id, idx)
            dap_frames.append(self._format_frame(frame_id, frame_obj))

        return {"stackFrames": dap_frames, "totalFrames": total}

    def handle_scopes(self, arguments: dict[str, Any]) -> dict[str, Any]:
        frame_id = int(arguments.get("frameId", 0))
        if frame_id not in self._frames:
            raise DispatchError(f"frame {frame_id} is not registered")
        # Cache scope refs per frame so repeated ``scopes(frameId)`` calls
        # return the same reference (VS Code reissues them on refocus).
        scope_ref = self._frame_scopes.get(frame_id)
        if scope_ref is None:
            scope_ref = self._variables.register_scope(frame_id)
            self._frame_scopes[frame_id] = scope_ref
        return {
            "scopes": [
                {
                    "name": "Locals",
                    "presentationHint": "locals",
                    "variablesReference": scope_ref,
                    "expensive": False,
                }
            ]
        }

    def handle_variables(self, arguments: dict[str, Any]) -> dict[str, Any]:
        ref = int(arguments.get("variablesReference", 0))
        if ref <= 0:
            raise DispatchError("variablesReference must be > 0")

        entry = self._variables.resolve(ref)
        if entry is None:
            raise DispatchError(f"unknown variablesReference {ref}")

        kind, payload = entry
        if kind == "scope":
            return {"variables": self._build_scope_variables(payload)}
        if kind == "object":
            # payload is ``(value, eval_name | None)``. Forward the stored
            # prefix so drill-down children get usable ``evaluateName`` for
            # Copy Value / watch.
            value, eval_name = payload
            return {
                "variables": expand(value, self._variables, parent_eval_name=eval_name)
            }
        raise DispatchError(f"unknown reference kind: {kind}")

    def handle_source(self, arguments: dict[str, Any]) -> dict[str, Any]:
        source = arguments.get("source") or {}
        reference = int(
            source.get("sourceReference") or arguments.get("sourceReference") or 0
        )
        content = self._sources.get_by_reference(reference)
        if content is None:
            raise DispatchError(f"no embedded source for reference {reference}")
        return {"content": content}

    def handle_evaluate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        expression = arguments.get("expression", "")
        frame_id = arguments.get("frameId")
        if frame_id is None:
            raise DispatchError("evaluate requires a frameId")

        frame_id_int = int(frame_id)
        if frame_id_int not in self._frames:
            raise DispatchError(f"frame {frame_id_int} is not registered")

        locals_ = self._frame_locals(frame_id_int)
        try:
            value = evaluate_expression(expression, locals_)
        except EvaluateError as e:
            raise DispatchError(str(e)) from e

        var = make_variable(expression, value, self._variables, eval_name=expression)
        return {
            "result": var["value"],
            "type": var["type"],
            "variablesReference": var["variablesReference"],
        }

    def handle_exception_info(self, arguments: dict[str, Any]) -> dict[str, Any]:
        snapshot = self._require_snapshot()
        thread_id = int(arguments.get("threadId", 0))
        stacktrace = snapshot.stacktraces.get(thread_id)
        if stacktrace is None:
            raise DispatchError(f"thread {thread_id} not found in snapshot")
        body = build_exception_info(stacktrace, self._sources)
        if body is None:
            raise DispatchError(f"thread {thread_id} has no exception info")
        return body

    # ---------------------------------------------------------------
    # Handlers — navigation
    # ---------------------------------------------------------------

    def handle_continue(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        self._advance(+1)
        return {"allThreadsContinued": True}

    # Step* requests reuse ``continue`` semantics on a snapshot — there is
    # nothing to step into / over / out of.
    def handle_next(self, _arguments: dict[str, Any]) -> dict[str, Any] | None:
        self._advance(+1)
        return None

    def handle_step_in(self, _arguments: dict[str, Any]) -> dict[str, Any] | None:
        self._advance(+1)
        return None

    def handle_step_out(self, _arguments: dict[str, Any]) -> dict[str, Any] | None:
        self._advance(+1)
        return None

    def handle_step_back(self, _arguments: dict[str, Any]) -> dict[str, Any] | None:
        self._advance(-1)
        return None

    def handle_reverse_continue(
        self, _arguments: dict[str, Any]
    ) -> dict[str, Any] | None:
        self._advance(-1)
        return None

    def handle_pause(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        raise DispatchError(
            "pause is not meaningful in a tintype snapshot — there is no "
            "running program to suspend."
        )

    def handle_disconnect(self, _arguments: dict[str, Any]) -> dict[str, Any] | None:
        self._terminated = True
        return None

    def handle_terminate(self, _arguments: dict[str, Any]) -> dict[str, Any] | None:
        self._terminated = True
        self._dispatcher.send_event("terminated")
        return None

    # ---------------------------------------------------------------
    # Handlers — custom tintype requests (sidebar UI)
    # ---------------------------------------------------------------

    def handle_tintype_snapshot_list(
        self, _arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Return all snapshot entries + current cursor for the sidebar."""
        reader = self._require_reader()
        total = reader.snapshot_count()
        entries: list[dict[str, Any]] = []
        for index in range(total):
            snap = reader.get_snapshot_at_index(index)
            if snap is None:
                continue
            entries.append(
                {
                    "index": index,
                    "timestampUs": int(snap.timestamp),
                }
            )
        return {
            "currentIndex": self._snapshot_index,
            "snapshots": entries,
        }

    def handle_tintype_jump_to_snapshot(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Move the cursor to a specific snapshot index and re-emit ``stopped``."""
        reader = self._require_reader()
        if "index" not in arguments:
            raise DispatchError("tintypeJumpToSnapshot requires an 'index' argument")
        raw_index = arguments["index"]
        if isinstance(raw_index, bool) or not isinstance(raw_index, int):
            raise DispatchError(
                f"tintypeJumpToSnapshot 'index' must be an int, got {type(raw_index).__name__}"
            )

        total = reader.snapshot_count()
        if raw_index < 0 or raw_index >= total:
            raise DispatchError(
                f"tintypeJumpToSnapshot 'index' {raw_index} is out of range [0, {total})"
            )

        old_threads = self._current_thread_ids()
        self._load_snapshot(raw_index)
        self._reconcile_thread_events(old_threads)
        self._send_stopped_event()
        return {"index": raw_index}

    # ---------------------------------------------------------------
    # Snapshot navigation internals
    # ---------------------------------------------------------------

    def _advance(self, direction: int) -> None:
        """Move cursor by ``direction`` (+1 forward, -1 backward).

        At either edge of the snapshot range we stay parked on the
        current snapshot and re-emit ``stopped`` so the UI remains
        interactive: users should be able to step-back out of the last
        snapshot the same way they can at the first one. Terminating on
        "reached the end" would throw the Tintype Snapshot Viewer away
        mid-session, which is almost never what you want — a live
        capture might gain more snapshots at any moment, and an offline
        ``.pytb`` still has valid state to inspect.
        """
        reader = self._require_reader()
        total = reader.snapshot_count()
        new_index = self._snapshot_index + direction

        if new_index < 0 or new_index >= total:
            # At either edge: refresh per-snapshot state so the new
            # ``stopped`` event doesn't serve stale variable refs the
            # client already consumed, emit a friendly status line, then
            # re-stop on the current snapshot.
            self._refresh_stop_state()
            message = (
                "Already at the last snapshot.\n"
                if direction > 0
                else "Already at the first snapshot.\n"
            )
            self._dispatcher.send_event(
                "output",
                {"category": "console", "output": message},
            )
            self._send_stopped_event()
            return

        old_threads = self._current_thread_ids()
        self._load_snapshot(new_index)
        self._reconcile_thread_events(old_threads)
        self._send_stopped_event()

    def _load_snapshot(self, index: int) -> None:
        reader = self._require_reader()
        snapshot = reader.get_snapshot_at_index(index)
        if snapshot is None:
            raise DispatchError(f"failed to read snapshot at index {index}")

        self._current_snapshot = snapshot
        self._snapshot_index = index

        # Reset per-snapshot state. Frame ids and variable refs are tied to
        # Python values that belong to the current snapshot's object heap.
        self._frames.clear()
        self._frame_ids.clear()
        self._frame_scopes.clear()
        self._next_frame_id = 1
        self._variables.clear()

    def _refresh_stop_state(self) -> None:
        """Reset variable / scope state without moving the snapshot cursor."""
        self._frames.clear()
        self._frame_ids.clear()
        self._frame_scopes.clear()
        self._next_frame_id = 1
        self._variables.clear()

    def _current_thread_ids(self) -> set[int]:
        snapshot = self._current_snapshot
        if snapshot is None:
            return set()
        return {int(st.id) for st in snapshot.stacktraces.values()}

    def _emit_thread_events_started(self) -> None:
        for tid in self._current_thread_ids():
            self._dispatcher.send_event(
                "thread", {"reason": "started", "threadId": tid}
            )

    def _reconcile_thread_events(self, old_threads: set[int]) -> None:
        """Emit ``started`` / ``exited`` thread events after a snapshot change."""
        new_threads = self._current_thread_ids()
        for tid in old_threads - new_threads:
            self._dispatcher.send_event("thread", {"reason": "exited", "threadId": tid})
        for tid in new_threads - old_threads:
            self._dispatcher.send_event(
                "thread", {"reason": "started", "threadId": tid}
            )

    def _send_stopped_event(self) -> None:
        snapshot = self._current_snapshot
        if snapshot is None:
            return
        # Prefer an exception thread so VS Code highlights the Exception Info
        # panel; fall back to the first thread otherwise. If no threads at all,
        # skip the ``stopped`` event (DAP reserves threadId 0 for "no thread").
        thread_id = _pick_stop_thread(snapshot, self._preferred_thread_name)
        if thread_id is None:
            self._dispatcher.send_event(
                "output",
                {
                    "category": "console",
                    "output": "Snapshot contains no threads; nothing to stop on.\n",
                },
            )
            return
        has_exception = any(
            st.exception_object is not None for st in snapshot.stacktraces.values()
        )
        body = {
            "reason": "exception" if has_exception else "pause",
            "threadId": thread_id,
            "preserveFocusHint": False,
            "allThreadsStopped": True,
        }
        self._dispatcher.send_event("stopped", body)

    # ---------------------------------------------------------------
    # Frame / scope helpers
    # ---------------------------------------------------------------

    def _register_frame(self, stacktrace_id: int, frame_index: int) -> int:
        """Return the stable frame id for ``(stacktrace_id, frame_index)``.

        O(1) via the reverse index so deep stacks don't degrade to O(N^2).
        """
        key = (stacktrace_id, frame_index)
        existing = self._frame_ids.get(key)
        if existing is not None:
            return existing
        frame_id = self._next_frame_id
        self._next_frame_id += 1
        self._frames[frame_id] = key
        self._frame_ids[key] = frame_id
        return frame_id

    def _format_frame(self, frame_id: int, frame: Frame) -> dict[str, Any]:
        return {
            "id": frame_id,
            "name": frame.function_qualname or frame.function_name,
            "line": int(frame.line_number),
            "column": 1,
            "source": self._sources.describe(frame.file_path),
            "presentationHint": "normal",
        }

    def _lookup_frame(self, frame_id: int) -> Frame | None:
        snapshot = self._current_snapshot
        if snapshot is None:
            return None
        entry = self._frames.get(frame_id)
        if entry is None:
            return None
        stacktrace_id, frame_index = entry
        stacktrace = snapshot.stacktraces.get(stacktrace_id)
        if stacktrace is None:
            return None
        # Tintype stores frames innermost-first, matching DAP order; we
        # do NOT reverse here, so frame_index indexes into the raw list.
        frames = list(stacktrace.frames)
        if 0 <= frame_index < len(frames):
            return frames[frame_index]
        return None

    def _frame_locals(self, frame_id: int) -> dict[str, Any]:
        frame = self._lookup_frame(frame_id)
        if frame is None:
            return {}
        try:
            return frame.get_locals()
        except Exception:  # noqa: BLE001
            logger.exception("frame.get_locals() raised")
            return {}

    def _build_scope_variables(self, frame_id: int) -> list[dict[str, Any]]:
        """Return the scope's variables, sorted for predictable UX.

        Ordering (matches pydevd / debugpy convention):

        * ``self`` / ``cls`` first — the method-receiver is the
          user's natural starting point when inspecting a frame.
        * Regular names next, alphabetically (case-insensitive).
        * Single-underscore private names after public ones.
        * Dunder (``__x__``) names last — CPython / framework
          machinery that rarely aids debugging.

        This replaces the raw ``locals_.items()`` order so the same
        variable always appears in the same slot across snapshots,
        which makes scanning the Variables panel much easier.
        """
        locals_ = self._frame_locals(frame_id)
        out: list[dict[str, Any]] = []
        for name in sorted(locals_.keys(), key=_scope_sort_key):
            value = locals_[name]
            eval_name = name if name.isidentifier() else None
            out.append(make_variable(name, value, self._variables, eval_name=eval_name))
        return out

    # ---------------------------------------------------------------
    # Required-state helpers
    # ---------------------------------------------------------------

    def _require_reader(self) -> SnapshotReader:
        if self._reader is None:
            raise DispatchError("session not launched")
        return self._reader

    def _require_snapshot(self) -> Snapshot:
        if self._current_snapshot is None:
            raise DispatchError("no current snapshot; did launch succeed?")
        return self._current_snapshot


# ---------------------------------------------------------------
# Module-private formatting helpers
# ---------------------------------------------------------------


def _describe_thread(stacktrace: Stacktrace) -> str:
    """Human-readable label for the CALL STACK panel."""
    name = stacktrace.thread_name
    if stacktrace.exception_object is not None:
        exc_repr: str
        try:
            exc_repr = str(stacktrace.exception_object)
        except Exception:  # noqa: BLE001
            exc_repr = repr(stacktrace.exception_object)
        prefix = name or f"Thread {stacktrace.id}"
        return f"{prefix}: {exc_repr}"
    if name:
        return name
    return f"Thread {stacktrace.id}"


def _scope_sort_key(name: str) -> tuple[int, str]:
    """Sort key for scope locals.

    Ordering buckets:

    * 0: ``self`` / ``cls`` — method-receiver special case, with
      ``self`` before ``cls`` (only one appears per real frame, but
      the fixed order keeps the sort fully deterministic).
    * 1: plain names (public).
    * 2: single-underscore private names (``_foo``).
    * 3: dunders (``__foo__``) — CPython / framework machinery.

    Within buckets 1-3, names are compared case-insensitively so
    ``Foo`` and ``foo`` sort next to each other.
    """
    if name == "self":
        return (0, "0")
    if name == "cls":
        return (0, "1")
    if name.startswith("__") and name.endswith("__"):
        return (3, name.lower())
    if name.startswith("_"):
        return (2, name.lower())
    return (1, name.lower())


def _pick_stop_thread(
    snapshot: Snapshot,
    preferred_thread_name: str | None = None,
    *,
    is_filtered_thread: Callable[[Stacktrace], bool] | None = None,
) -> int | None:
    """Thread ID to attach to the ``stopped`` event, or ``None`` if empty.

    DAP reserves ``threadId`` 0 for "no thread", so we return ``None``
    for empty snapshots and callers skip the ``stopped`` event entirely.

    Priority:
      1. If ``preferred_thread_name`` is set and a thread with that name
         exists in the snapshot, return its id — this preserves the
         user's focus across snapshot jumps.
      2. First thread with an exception — highlights the Exception Info
         panel when the user hasn't selected a thread yet.
      3. First thread in snapshot insertion order.
    """
    if preferred_thread_name is not None:
        for st in snapshot.stacktraces.values():
            name = st.thread_name or f"Thread {int(st.id)}"
            if name == preferred_thread_name:
                return int(st.id)
    for st in snapshot.stacktraces.values():
        if st.exception_object is not None:
            return int(st.id)
    first = next(iter(snapshot.stacktraces.values()), None)
    if first is None:
        return None
    return int(first.id)
