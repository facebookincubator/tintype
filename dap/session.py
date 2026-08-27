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
import re
from typing import Any, Callable, Iterable

from tintype import Frame, Snapshot, SnapshotReader, Stacktrace
from tintype.dap.dispatcher import Dispatcher, DispatchError
from tintype.dap.evaluate import evaluate_expression, EvaluateError
from tintype.dap.exceptions import build_exception_info
from tintype.dap.sources import SourceRegistry
from tintype.dap.variables import expand, make_variable, VariableRegistry


logger: logging.Logger = logging.getLogger(__name__)

VIEWER_PROTOCOL_VERSION: int = 1


# Synthetic "thread" ID used by ``handle_threads`` / ``handle_stack_trace``
# to represent a flattened exception chain as a single CALL STACK entry.
# Real ``Stacktrace.id`` values are positive OS thread IDs, and DAP
# reserves ``0`` for "no thread", so a fixed negative ID is guaranteed
# not to collide. The value is an internal identifier — DAP clients see
# it only as an opaque integer.
_EXCEPTION_CHAIN_THREAD_ID = -1

# Separator frame labels. Rendered with ``presentationHint: "label"``
# so VS Code displays them as non-navigable headers between the frames
# of chained exceptions in the CALL STACK panel. The arrows point
# upward (toward the preceding frames) because in VS Code's CALL STACK
# frame[0] is the top (innermost) and the separator sits *below* the
# exception whose cause/context is about to be shown.
_SEPARATOR_LABEL_CAUSE = "⬆ CAUSED BY ⬆"
_SEPARATOR_LABEL_CONTEXT = "⬆ DURING HANDLING OF ⬆"

# Cap on how far we follow ``__cause__`` / ``__context__`` when
# flattening. Mirrors the matching cap in
# :data:`tintype.dap.exceptions._MAX_CHAIN_DEPTH` so the two renderers
# agree on what constitutes a pathological chain.
_MAX_EXCEPTION_CHAIN_DEPTH = 10


# Frame-path patterns (regex, matched against ``frame.file_path`` with
# ``re.search``) whose frames are treated as "framework noise" by
# default. Debugger-injected frames from pydevd/debugpy dominate the
# CALL STACK on snapshots captured while the program is paused under
# debugpy, so we deemphasize them unless the launch config overrides.
# Users can replace this list via the launch config's
# ``excludeFramePaths`` field, or re-include specific files via
# ``includeFramePaths`` (which wins over excludes).
DEFAULT_EXCLUDE_FRAME_PATHS: list[str] = [
    # Directory-anchored pydevd / debugpy / pydev patterns. These catch
    # the real package layouts (``.../pydevd/...``, ``.../debugpy/...``,
    # ``.../_pydev_.../...``) without matching on filename alone, so a
    # user script that happens to be named ``pydevd.py`` does NOT get
    # deemphasized.
    r"/pydevd[_/]",
    r"/debugpy[_/]",
    r"/_pydev_",
    r"/pydev_ipython/",
    # Tintype's own VS Code integration helpers. ``capture()`` and
    # friends are dispatched through a debugpy ``evaluate``, so
    # ``snapshot_all_threads()`` records the very thread performing the
    # capture, with ``vscode.py`` on top of the pydevd command-thread
    # stack. Every other frame in that thread is already matched here,
    # so without this pattern :meth:`TintypeDebugSession._is_filtered_thread`
    # sees one unfiltered frame, keeps the service thread in the threads
    # list, and :func:`_pick_stop_thread` lands the user on tintype's
    # internals instead of their own code. Directory-anchored so a user
    # module named ``vscode.py`` outside a ``tintype`` package is
    # unaffected.
    r"/tintype/vscode\.py$",
    # ``threading.Thread.__init__`` and ``Thread._bootstrap*`` frames
    # bracket every real user thread; they're part of the CPython
    # stdlib plumbing, not the user's call stack. Anchor on the
    # ``lib/python3.X`` path component so a user project file named
    # ``threading.py`` (e.g. ``/my_proj/threading.py``) doesn't get
    # deemphasized along with the stdlib one.
    r"/lib/python3\.\d+/threading\.py$",
    # Same rationale for ``queue.py``: pydevd's command-queue worker
    # and CPython's own ``Thread`` helpers park inside
    # ``queue.Queue.get``/``put`` between work items, so those frames
    # show up at the bottom of every worker-thread stack trace. Anchor
    # on the ``lib/python3.X`` path component so a user project file
    # named ``queue.py`` is not affected.
    r"/lib/python3\.\d+/queue\.py$",
    # ``compile(code_str, "<string>", "exec")`` — used by pydevd to run
    # debug-console ``evaluate`` expressions (including the tintype
    # snapshot-capture helper itself) and by any user call to
    # ``eval``/``exec`` on a bare string. CPython sets ``co_filename``
    # to the literal ``"<string>"`` for these code objects; the top
    # frame's ``co_name`` is the generic ``"<module>"`` top-level
    # scope marker.
    #
    # The ``^...$`` anchor is deliberately strict: angle brackets are
    # not valid characters in filenames on real filesystems, so this
    # cannot collide with a user source file. Historically a looser
    # ``<string>$`` anchor was needed because
    # ``SnapshotReader::extractSourceFiles`` concatenated
    # ``extractedFilesDir_ + file.path`` without a separator,
    # producing munged paths like ``/tmp/snapshot_files_Xxxxxx<string>``
    # that also had to be deemphasized. That bug was fixed in the
    # reader (see ``tintype/snapshot_lib/SnapshotReader.cpp`` —
    # synthetic ``<...>`` filenames are now skipped at stage time and
    # the frame's ``file_path`` is preserved as the raw ``<string>``),
    # so the tighter anchor is safe again.
    r"^<string>$",
]


def extend_default_exclude_frame_paths(patterns: Iterable[str]) -> None:
    """Append ``patterns`` to :data:`DEFAULT_EXCLUDE_FRAME_PATHS`.

    Downstream integrations can import this function at module-load time to
    add environment-specific framework paths before the DAP server starts.
    The augmented list is then in place before any ``launch`` request is
    processed.

    This API mutates the module-level list in-place. Callers that
    import ``DEFAULT_EXCLUDE_FRAME_PATHS`` directly will see the
    extended list on subsequent reads. Calling this multiple times
    appends additively — it does not deduplicate, so wrappers should
    call it once per import cycle.

    Duplicates are harmless (``re.search`` short-circuits on the first
    match) but do grow the per-frame filter cost linearly, so keep the
    list reasonable.
    """
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern:
            continue
        DEFAULT_EXCLUDE_FRAME_PATHS.append(pattern)


def set_default_exclude_frame_paths(patterns: Iterable[str]) -> None:
    """Replace :data:`DEFAULT_EXCLUDE_FRAME_PATHS` wholesale.

    Use this when a wrapper needs full control — e.g. a library that
    embeds tintype in a non-Python environment and the default
    pydevd/debugpy patterns aren't relevant. Most callers should prefer
    :func:`extend_default_exclude_frame_paths` so the baseline
    suppression set stays intact.
    """
    DEFAULT_EXCLUDE_FRAME_PATHS[:] = [p for p in patterns if isinstance(p, str) and p]


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
        # Separator "label" frames used in the flattened exception-chain
        # CALL STACK view. Maps frame_id -> human-readable label (e.g.
        # ``"⬆ CAUSED BY ⬆"``). Parallel to ``_frames`` rather than merged
        # into it because the lookup paths differ (separator frames have
        # no source / no scopes / no stacktrace).
        self._separator_frames: dict[int, str] = {}

        self._variables: VariableRegistry = VariableRegistry()
        self._sources: SourceRegistry = SourceRegistry()

        # Name of the thread the user most recently inspected (captured
        # from ``handle_stack_trace``). ``_pick_stop_thread`` prefers a
        # same-named thread in the new snapshot on every jump so focus
        # doesn't jump around to whatever thread happens to have an
        # exception. Cleared to ``None`` at launch so the very first
        # stop uses the exception-first fallback.
        self._preferred_thread_name: str | None = None

        # Frame-path filter state populated from the launch config. See
        # DEFAULT_EXCLUDE_FRAME_PATHS for the baseline pydevd/debugpy
        # suppression set. ``_hide_filtered_frames`` controls whether
        # matching frames are omitted from ``stackTrace`` responses
        # (True) or merely marked with ``presentationHint:
        # "deemphasize"`` (False — default).
        self._exclude_frame_patterns: list[re.Pattern[str]] = []
        self._include_frame_patterns: list[re.Pattern[str]] = []
        self._hide_filtered_frames: bool = False

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
                # Custom tintype requests for the sidebar UI.
                "tintypeSnapshotList": self.handle_tintype_snapshot_list,
                "tintypeJumpToSnapshot": self.handle_tintype_jump_to_snapshot,
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
            "tintypeProtocolVersion": VIEWER_PROTOCOL_VERSION,
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

        self._reader = reader
        self._pytb_path = pytb
        self._sources.load_from_reader(reader)
        self._configure_frame_filters(arguments)

        self._dispatcher.send_event("initialized")

        # An empty ``.pytb`` is a valid launch state — a client may
        # open the viewer against a freshly initialized working file
        # before any snapshots have been written, so snapshots accrue
        # while the viewer is already open. With zero snapshots there
        # is nothing to load and no ``stopped`` event to emit; the
        # viewer stays in a "waiting" state until the client navigates
        # to a snapshot via ``tintypeJumpToSnapshot``, which resolves
        # to a normal ``stopped`` event once the index is valid.
        # ``handle_threads`` tolerates ``_current_snapshot is None``
        # for the same reason.
        if reader.snapshot_count() > 0:
            # Load the requested snapshot. ``initialized`` is what tells VS Code
            # it can start sending configuration requests (breakpoints,
            # exception filters, etc.); send it before ``stopped`` so the
            # client is ready. ``_load_snapshot`` emits the single
            # ``process`` event for this session — VS Code bakes
            # ``body.name`` into the CALL STACK sub-line at launch time and
            # ignores later emissions, so we only emit once.
            start_index = _resolve_start_index(
                arguments.get("snapshotIndex"), reader.snapshot_count()
            )
            self._load_snapshot(start_index)

            self._emit_thread_events_started()
            # Bootstrap-then-real emission pattern: VS Code overwrites the
            # CALL STACK description on the very first ``stopped`` event of a
            # launch, so we send a throwaway ``"Snapshot"`` first and
            # immediately follow it with the real description. The second
            # event is what the user actually sees. See
            # :meth:`_send_stopped_event` for the full rationale.
            self._send_stopped_event(bootstrap=True)
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

    def _configure_frame_filters(self, arguments: dict[str, Any]) -> None:
        """Parse the frame-filter launch arguments into compiled regexes.

        ``excludeFramePaths`` — list of regex strings. Falls back to
        :data:`DEFAULT_EXCLUDE_FRAME_PATHS` when omitted. Pass an empty
        list to disable filtering entirely.

        ``includeFramePaths`` — list of regex strings that override
        excludes (wins if any pattern matches). Useful for pulling a
        specific debugpy file back into view while keeping the rest
        suppressed.

        ``hideFilteredFrames`` — when true, matching frames are dropped
        from ``stackTrace`` responses. When false (default), frames are
        marked with ``presentationHint: "deemphasize"`` so VS Code
        renders them in italic/grey but keeps them clickable.

        Invalid regexes are reported via an ``output`` event and
        skipped, so a typo in one pattern doesn't break the session.
        """
        raw_excludes = arguments.get("excludeFramePaths")
        if raw_excludes is None:
            raw_excludes = DEFAULT_EXCLUDE_FRAME_PATHS
        raw_includes = arguments.get("includeFramePaths") or []
        self._exclude_frame_patterns = self._compile_patterns(
            raw_excludes, label="excludeFramePaths"
        )
        self._include_frame_patterns = self._compile_patterns(
            raw_includes, label="includeFramePaths"
        )
        self._hide_filtered_frames = bool(arguments.get("hideFilteredFrames", False))

    def _compile_patterns(self, raw: object, *, label: str) -> list[re.Pattern[str]]:
        if not isinstance(raw, list):
            return []
        compiled: list[re.Pattern[str]] = []
        for entry in raw:
            if not isinstance(entry, str) or not entry:
                continue
            try:
                compiled.append(re.compile(entry))
            except re.error as exc:
                self._dispatcher.send_event(
                    "output",
                    {
                        "category": "console",
                        "output": (
                            f"tintype: skipping invalid {label} pattern "
                            f"{entry!r}: {exc}\n"
                        ),
                    },
                )
        return compiled

    def _is_filtered_frame(self, frame: Frame) -> bool:
        """True if ``frame`` matches the user's exclude filter.

        Includes win over excludes: if any include pattern matches, the
        frame is kept regardless of what excludes say. This lets users
        "subtract" specific files from a broad exclude pattern.
        """
        path = frame.file_path or ""
        if any(p.search(path) for p in self._include_frame_patterns):
            return False
        return any(p.search(path) for p in self._exclude_frame_patterns)

    def _is_filtered_thread(self, stacktrace: Stacktrace) -> bool:
        """True if every frame in the thread is filtered.

        These are pydevd's service threads (Reader, Writer,
        CommandThread, etc.) — they carry no user value and just clutter
        the threads list. We drop them from ``handle_threads`` entirely;
        there's no DAP concept of a "deemphasized thread" so hide-mode
        is the only sensible behavior here.

        Threads with at least one user frame are never filtered out of
        the threads list — the filter only applies within the stack.
        """
        # ``all()`` short-circuits on the first False, so we can skip
        # materializing the full frame list.  ``all([])`` is True in
        # Python, so we use a sentinel to distinguish "no frames at all"
        # (an empty thread, not filtered) from "every frame is filtered".
        any_frame = False
        for frame in stacktrace.frames:
            any_frame = True
            if not self._is_filtered_frame(frame):
                return False
        return any_frame

    def handle_threads(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        # No snapshot loaded yet — the launch saw an empty ``.pytb``
        # (e.g. snappoint pre-init) and skipped the bootstrap
        # ``_load_snapshot`` call. Return an empty threads list rather
        # than raising; VS Code will simply render an empty CALL STACK
        # until the user navigates to a snapshot via
        # ``tintypeJumpToSnapshot``.
        if self._current_snapshot is None:
            return {"threads": []}
        snapshot = self._current_snapshot
        threads: list[dict[str, Any]] = []

        # When the snapshot carries any exception-bearing stacktraces,
        # collapse them into a single synthetic "Exception: …" entry
        # that presents a flattened ``__cause__`` / ``__context__``
        # chain in the CALL STACK panel (see ``handle_stack_trace``).
        # The individual exception stacktraces are hidden from the
        # threads list because their frames are already surfaced via
        # the virtual chain view — showing them twice would be noise.
        exception_sts = [
            st
            for st in snapshot.stacktraces.values()
            if st.exception_object is not None and not self._is_filtered_thread(st)
        ]
        chain_root = _pick_exception_chain_root(
            snapshot, is_filtered=self._is_filtered_thread
        )
        if chain_root is not None:
            threads.append(
                {
                    "id": _EXCEPTION_CHAIN_THREAD_ID,
                    "name": _describe_exception_chain(chain_root),
                }
            )
        exception_ids: set[int] = {int(st.id) for st in exception_sts}

        for stacktrace in snapshot.stacktraces.values():
            if self._is_filtered_thread(stacktrace):
                continue
            if int(stacktrace.id) in exception_ids:
                continue
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

        if thread_id == _EXCEPTION_CHAIN_THREAD_ID:
            chain_root = _pick_exception_chain_root(
                snapshot, is_filtered=self._is_filtered_thread
            )
            if chain_root is None:
                raise DispatchError(
                    "virtual exception-chain thread has no exception stacktraces"
                )
            # Remember the virtual thread as the user's preferred thread
            # so exception-chain focus persists across snapshot jumps.
            self._preferred_thread_name = _EXCEPTION_CHAIN_PREFERRED_NAME
            return self._build_exception_chain_stack_trace(
                chain_root, start_frame=start_frame, levels=levels
            )

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
        all_frames = list(stacktrace.frames)
        # When ``hideFilteredFrames`` is on, drop matching frames
        # entirely so they never reach the client. Otherwise we keep
        # them and let ``_format_frame`` apply the deemphasize hint.
        if self._hide_filtered_frames:
            ordered = [f for f in all_frames if not self._is_filtered_frame(f)]
        else:
            ordered = all_frames
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

    def _build_exception_chain_stack_trace(
        self,
        chain_root: Stacktrace,
        *,
        start_frame: int,
        levels: int,
    ) -> dict[str, Any]:
        """Build the DAP ``stackTrace`` body for the virtual chain thread.

        Walks ``chain_root`` → ``__cause__`` (preferred) → ``__context__``
        exactly once per chain hop, bounded by
        :data:`_MAX_EXCEPTION_CHAIN_DEPTH` and cycle-guarded via an
        ``id()`` seen-set. The collected chain is then emitted in
        **innermost-first** order (matching CPython's
        ``traceback.print_exception`` layout) with separator "label"
        frames between groups. Reading the panel top-down:

        * frames of the original cause (oldest exception)
        * separator (``⬆ CAUSED BY ⬆`` or ``⬆ DURING HANDLING OF ⬆``
          — arrows point up at the cause that was just rendered)
        * frames of the exception it caused
        * …repeat until the outermost (most recently raised)…

        Within each group, frames are innermost-first per DAP
        convention (``frame[0]`` is the active frame). Separator
        frames are registered in ``_separator_frames`` so the frame
        IDs we hand out can be looked up on subsequent
        ``scopes`` / ``source`` requests without any additional
        bookkeeping path.
        """
        # First pass: walk from the outermost chain root inward,
        # collecting each Stacktrace and recording how we reached it
        # (via ``__cause__`` → ``_SEPARATOR_LABEL_CAUSE``, via
        # ``__context__`` → ``_SEPARATOR_LABEL_CONTEXT``, or ``None``
        # for the first (outermost) entry).
        chain: list[tuple[Stacktrace, str | None]] = [(chain_root, None)]
        seen: set[int] = {id(chain_root)}
        current: Stacktrace | None = chain_root
        depth = 0
        while current is not None and depth < _MAX_EXCEPTION_CHAIN_DEPTH:
            # Prefer ``__cause__`` (``raise X from Y``) over
            # ``__context__`` (implicit during-handling) to match
            # CPython's ``traceback`` precedence.
            next_stacktrace: Stacktrace | None = current.get_cause()
            label = _SEPARATOR_LABEL_CAUSE
            if next_stacktrace is None:
                next_stacktrace = current.get_context()
                label = _SEPARATOR_LABEL_CONTEXT
            if next_stacktrace is None:
                break
            if id(next_stacktrace) in seen:
                break
            seen.add(id(next_stacktrace))
            chain.append((next_stacktrace, label))
            current = next_stacktrace
            depth += 1

        # Emit innermost-first: reverse the walked chain so the
        # original cause is at the top of the panel and the most
        # recently raised exception sits at the bottom, matching
        # CPython's ``traceback.print_exception`` output.
        chain.reverse()

        # Cache the (possibly filtered) frame list per stacktrace so
        # the emit loop below doesn't have to recompute it. Without
        # this, a stacktrace of N frames emitting M of them would
        # drive an O(M·N) worst-case rebuild + filter-match cost
        # when ``hideFilteredFrames`` is on.
        stacktrace_frames_by_id: dict[int, list[Frame]] = {}
        flat: list[tuple[str, Stacktrace | None, int | str]] = []
        for index, (stacktrace, separator_label) in enumerate(chain):
            # ``separator_label`` records how the walk stepped INTO
            # this entry from the next-outer one. After reversal, the
            # next-outer entry now sits BELOW us, so the separator
            # should appear AFTER this entry's frames (pointing up at
            # us, the cause).
            stacktrace_frames = list(stacktrace.frames)
            if self._hide_filtered_frames:
                stacktrace_frames = [
                    f for f in stacktrace_frames if not self._is_filtered_frame(f)
                ]
            stacktrace_frames_by_id[int(stacktrace.id)] = stacktrace_frames
            for idx, _frame in enumerate(stacktrace_frames):
                flat.append(("frame", stacktrace, idx))
            # Append the separator only if there's something below us
            # (i.e., we're not the last / outermost entry) and we have
            # a recorded label.
            if index < len(chain) - 1 and separator_label is not None:
                flat.append(("separator", None, separator_label))

        # Re-filter: if ``hideFilteredFrames`` is on AND some exception
        # frame group winds up empty, the orphan separator just above
        # or below it should also be dropped so we never emit two
        # separators in a row.
        flat = _prune_orphan_separators(flat)
        total = len(flat)

        if start_frame < 0:
            start_frame = 0
        if levels <= 0:
            end = total
        else:
            end = min(total, start_frame + levels)

        dap_frames: list[dict[str, Any]] = []
        for entry in flat[start_frame:end]:
            kind = entry[0]
            if kind == "separator":
                label = entry[2]
                assert isinstance(label, str)
                frame_id = self._register_separator_frame(label)
                dap_frames.append(self._format_separator_frame(frame_id, label))
                continue
            stacktrace = entry[1]
            idx = entry[2]
            assert stacktrace is not None
            assert isinstance(idx, int)
            frame_obj = stacktrace_frames_by_id[int(stacktrace.id)][idx]
            frame_id = self._register_frame(int(stacktrace.id), idx)
            dap_frames.append(self._format_frame(frame_id, frame_obj))

        return {"stackFrames": dap_frames, "totalFrames": total}

    def _register_separator_frame(self, label: str) -> int:
        """Allocate a frame-id for a separator row and remember its label."""
        frame_id = self._next_frame_id
        self._next_frame_id += 1
        self._separator_frames[frame_id] = label
        return frame_id

    def _format_separator_frame(self, frame_id: int, label: str) -> dict[str, Any]:
        """Shape the DAP ``StackFrame`` dict for a separator label row.

        ``presentationHint: "label"`` tells VS Code to render the row
        as a non-navigable header (no source link, no click-to-open).
        ``line: 0`` and omitting ``source`` signal "no location".
        """
        return {
            "id": frame_id,
            "name": label,
            "line": 0,
            "column": 0,
            "presentationHint": "label",
        }

    def handle_scopes(self, arguments: dict[str, Any]) -> dict[str, Any]:
        frame_id = int(arguments.get("frameId", 0))
        # Separator "label" frames in the virtual exception-chain view
        # have no locals/scopes — VS Code may still issue ``scopes`` on
        # them when the user focuses the row, so return an empty list
        # rather than raising.
        if frame_id in self._separator_frames:
            return {"scopes": []}
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
        # The virtual exception-chain thread has no real ``Stacktrace``
        # entry in ``snapshot.stacktraces``; route the request to the
        # **innermost** (original cause) exception. VS Code uses the
        # ExceptionInfo response (``exceptionId`` / ``description``) to
        # drive the red "Exception has occurred" decoration anchored
        # on the top stack frame, which under the flattened chain
        # view is the innermost cause's innermost frame. Surfacing
        # the outer exception's info here would paint the cause's
        # raise site with the effect's message. ``innerException`` is
        # naturally empty from the innermost (it has no further
        # cause), which matches the user's perspective at the top
        # frame; the outer exception's info still surfaces to the
        # user via the CALL STACK panel's flattened chain rows.
        if thread_id == _EXCEPTION_CHAIN_THREAD_ID:
            stacktrace = _pick_exception_chain_innermost(
                snapshot, is_filtered=self._is_filtered_thread
            )
            if stacktrace is None:
                raise DispatchError(
                    "virtual exception-chain thread has no exception stacktraces"
                )
        else:
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
        """Return all snapshot entries + current cursor for the sidebar.

        Entries whose snapshot fails to load are still emitted (with
        ``corrupt: true`` and no timestamp) so the sidebar's row count
        matches ``reader.snapshot_count()`` and the user sees *why* some
        rows are sparse. Also emits a DAP ``output`` event per corrupt
        entry so the debug console carries the diagnostic.
        """
        reader = self._require_reader()
        total = reader.snapshot_count()
        entries: list[dict[str, Any]] = []
        for index in range(total):
            snap = reader.get_snapshot_at_index(index)
            if snap is None:
                logger.warning(
                    "snapshot at index %d could not be loaded; marking corrupt",
                    index,
                )
                self._dispatcher.send_event(
                    "output",
                    body={
                        "category": "important",
                        "output": (
                            f"tintype: snapshot {index} could not be loaded "
                            f"(corrupt or unreadable); the sidebar will show "
                            f"it as unavailable.\n"
                        ),
                    },
                )
                entries.append({"index": index, "corrupt": True})
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
        # Include ``currentIndex`` + ``totalSnapshots`` alongside the old
        # ``index`` field so the client can update the sidebar's cursor /
        # total without a follow-up ``tintypeSnapshotList`` round-trip.
        # ``index`` is kept for backward compatibility with callers that
        # haven't been updated yet.
        return {
            "index": raw_index,
            "currentIndex": raw_index,
            "totalSnapshots": reader.snapshot_count(),
        }

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
        self._separator_frames.clear()
        self._next_frame_id = 1
        self._variables.clear()

    def _refresh_stop_state(self) -> None:
        """Reset variable / scope state without moving the snapshot cursor."""
        self._frames.clear()
        self._frame_ids.clear()
        self._frame_scopes.clear()
        self._separator_frames.clear()
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

    def _send_stopped_event(self, *, bootstrap: bool = False) -> None:
        """Emit a ``stopped`` event for the current snapshot.

        VS Code displays ``body.description`` in the CALL STACK panel
        (next to the session row) and keeps whatever the most recent
        ``stopped`` event provided — an absent ``description`` *clears*
        the field rather than preserving the prior value. That's why we
        set it on every emission.

        The ``bootstrap`` flag is an initial-launch quirk: the very first
        ``stopped`` event of the session is subject to a race where VS
        Code overwrites whichever description the adapter provided.
        Sending a bootstrap event (with a generic ``"Snapshot"`` label)
        immediately followed by the real one lets the "real" description
        settle in as the one the user sees.
        """
        snapshot = self._current_snapshot
        if snapshot is None:
            return
        # Prefer an exception thread so VS Code highlights the Exception Info
        # panel; fall back to the first thread otherwise. If no threads at all,
        # skip the ``stopped`` event (DAP reserves threadId 0 for "no thread").
        thread_id = _pick_stop_thread(
            snapshot,
            self._preferred_thread_name,
            is_filtered_thread=self._is_filtered_thread,
        )
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
        description = "Snapshot" if bootstrap else self._snapshot_display_name(snapshot)
        body: dict[str, Any] = {
            "reason": "exception" if has_exception else "pause",
            "threadId": thread_id,
            "preserveFocusHint": False,
            "allThreadsStopped": True,
        }
        if description:
            body["description"] = description
        # Populate ``body.text`` with the exception's type + message so
        # VS Code renders the red "Exception has occurred" overlay on
        # the offending frame's source line (and a matching hover
        # tooltip). DAP-spec ``stopped.text`` drives this rendering;
        # without it VS Code has no message to attach to the frame.
        # We pick the text from the stopped thread's exception so the
        # overlay always reflects what the user is currently focused
        # on, falling back to any other exception in the snapshot.
        if has_exception:
            text = _format_stopped_exception_text(
                snapshot, thread_id, is_filtered_thread=self._is_filtered_thread
            )
            if text:
                body["text"] = text
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
        # When ``hideFilteredFrames`` is on, callers have already
        # dropped matching frames before we get here — so anything we
        # see is a frame the user wants to see. In the default (not
        # hidden) mode, we keep matching frames but mark them
        # ``"subtle"`` so VS Code renders them italic/grey while still
        # letting the user click through.
        #
        # Note on the DAP spec: ``StackFrame.presentationHint`` is
        # ``"normal" | "label" | "subtle"``. ``"deemphasize"`` is only
        # valid on ``Source.presentationHint`` — using it on a frame
        # is a spec violation that VS Code tolerates but pydevd avoids.
        # We match pydevd's ``"subtle"`` choice here for maximum
        # compatibility.
        hint = "normal"
        if not self._hide_filtered_frames and self._is_filtered_frame(frame):
            hint = "subtle"
        return {
            "id": frame_id,
            "name": frame.function_qualname or frame.function_name,
            "line": int(frame.line_number),
            "column": 1,
            "source": self._sources.describe(frame.file_path),
            "presentationHint": hint,
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
    # Display-name helpers
    # ---------------------------------------------------------------

    def _snapshot_display_name(self, _snapshot: Snapshot) -> str | None:
        """One-line description of the current snapshot.

        Surfaced via the ``stopped`` event's ``description`` field, which
        VS Code renders next to the session row in CALL STACK.

        Currently always returns the static label ``"Snapshot"`` rather
        than a dynamic ``N/M — timestamp`` format. The dynamic format
        looks correct on the initial launch but stays stale on
        subsequent ``tintypeJumpToSnapshot`` requests because VS Code
        does not consistently re-render the CALL STACK description on
        every ``stopped`` event after the first one. A stale dynamic
        label is more misleading than a static one — the snapshot
        index / timestamp the user actually needs is already visible
        in the snapshots panel.
        """
        return "Snapshot"


# ---------------------------------------------------------------
# Module-private formatting helpers
# ---------------------------------------------------------------


# Stable "preferred thread name" sentinel for the virtual exception
# chain view. ``handle_stack_trace`` stashes this in
# ``_preferred_thread_name`` whenever the user focuses the chain
# thread; ``_pick_stop_thread`` then prefers it on later snapshot
# jumps. Real thread names can never clash because a real thread name
# is always ``stacktrace.thread_name`` (never prefixed with ``<<`` /
# ``>>``).
_EXCEPTION_CHAIN_PREFERRED_NAME = "<<exception-chain>>"


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


def _format_stopped_exception_text(
    snapshot: Snapshot,
    stopped_thread_id: int,
    *,
    is_filtered_thread: Callable[[Stacktrace], bool] | None = None,
) -> str | None:
    """Render the ``stopped.text`` overlay for an exception snapshot.

    Picks the exception to surface in the red "Exception has
    occurred" overlay by preferring the stopped thread's exception,
    falling back to the first exception-carrying stacktrace in the
    snapshot. Returns ``None`` when no exception is reachable (the
    caller will then skip setting ``body.text``).

    The output shape is ``"<ExcType>: <message>"`` (matching CPython's
    ``traceback`` output), trimmed to a single line and bounded in
    length so the overlay tooltip stays readable even when the
    exception repr is huge.

    ``is_filtered_thread`` is forwarded to the chain picker when
    ``stopped_thread_id`` is the virtual chain sentinel, so the
    overlay text mirrors the filtered thread selection.
    """
    # Hard cap on the overlay text — VS Code shows this in a small
    # hover, and very long messages make the tooltip unusable.
    _MAX_TEXT_LEN = 512
    # The virtual exception-chain thread has no real entry in
    # ``stacktraces``; route it to the **innermost** (original cause)
    # exception. VS Code anchors ``stopped.text`` on the top stack
    # frame, which under the flattened chain view is the innermost
    # cause's innermost frame (see ``_build_exception_chain_stack_trace``
    # for the flat layout). Surfacing the outermost exception's text
    # there would decorate the cause's raise site with the effect's
    # message — a mismatch that makes the hover actively misleading.
    if stopped_thread_id == _EXCEPTION_CHAIN_THREAD_ID:
        preferred = _pick_exception_chain_innermost(
            snapshot, is_filtered=is_filtered_thread
        )
    else:
        preferred = snapshot.stacktraces.get(stopped_thread_id)
    candidates: list[Stacktrace] = []
    if preferred is not None and preferred.exception_object is not None:
        candidates.append(preferred)
    for st in snapshot.stacktraces.values():
        if st is preferred:
            continue
        if st.exception_object is not None:
            candidates.append(st)

    for st in candidates:
        exc = st.exception_object
        if exc is None:
            continue
        type_name = type(exc).__name__
        # Extract the class name from the repr when tintype serialized
        # the original class (e.g. ``ValueError('x')`` -> ``ValueError``).
        try:
            rep = repr(exc)
        except Exception:  # noqa: BLE001
            rep = ""
        paren = rep.find("(")
        if paren > 0:
            candidate = rep[:paren].strip()
            if candidate:
                type_name = candidate

        message: str
        try:
            message = str(exc)
        except Exception:  # noqa: BLE001
            message = rep
        # Collapse the overlay onto one line — newlines in the
        # message would otherwise be rendered literally.
        message = message.replace("\n", " ").replace("\r", " ").strip()
        text = f"{type_name}: {message}" if message else type_name
        if len(text) > _MAX_TEXT_LEN:
            text = text[: _MAX_TEXT_LEN - 1] + "…"
        return text

    return None


def _pick_exception_chain_root(
    snapshot: Snapshot,
    *,
    is_filtered: Callable[[Stacktrace], bool] | None = None,
) -> Stacktrace | None:
    """Return the stacktrace that should head the virtual chain thread.

    The "root" is the outermost exception — i.e. the one that isn't
    reachable as another stacktrace's ``__cause__`` / ``__context__``.
    Tintype writers conventionally serialize exception stacktraces in
    raised-first order, so the first exception-bearing stacktrace in
    insertion order usually satisfies this criterion. When multiple
    unrelated exceptions exist in a single snapshot (unusual, but
    possible for multi-thread captures), we pick the first one we see
    and accept that the others stay hidden behind the virtual view —
    a follow-up could emit one virtual thread per independent chain.

    ``is_filtered`` (optional) — predicate matching
    :meth:`SnapshotDebugSession._is_filtered_thread`. When provided,
    stacktraces for which it returns ``True`` are skipped. This keeps
    the virtual chain row consistent with ``handle_threads``'s
    thread-level filtering: if every exception-bearing stacktrace is
    framework noise (e.g. all pydevd/debugpy frames), no virtual
    thread is emitted and the caller falls back to non-chain focus
    rather than surfacing an empty CALL STACK. When ``is_filtered`` is
    ``None`` the function accepts all exception stacktraces — used by
    module-level callers that don't have access to the session's
    filter state.

    Returns ``None`` when the snapshot has no (non-filtered) exception
    stacktraces.
    """
    accept: Callable[[Stacktrace], bool] = (
        (lambda st: not is_filtered(st))
        if is_filtered is not None
        else (lambda _st: True)
    )
    inner_ids: set[int] = set()
    for st in snapshot.stacktraces.values():
        if st.exception_object is None:
            continue
        if not accept(st):
            continue
        cause = st.get_cause()
        if cause is not None:
            inner_ids.add(id(cause))
        context = st.get_context()
        if context is not None:
            inner_ids.add(id(context))
    for st in snapshot.stacktraces.values():
        if st.exception_object is None:
            continue
        if not accept(st):
            continue
        if id(st) in inner_ids:
            continue
        return st
    # Fallback: every (non-filtered) exception is reachable from
    # another (pure cycle — shouldn't happen in practice, but be
    # defensive). Use the first non-filtered exception we find so the
    # panel still has something to show.
    for st in snapshot.stacktraces.values():
        if st.exception_object is not None and accept(st):
            return st
    return None


def _pick_exception_chain_innermost(
    snapshot: Snapshot,
    *,
    is_filtered: Callable[[Stacktrace], bool] | None = None,
) -> Stacktrace | None:
    """Return the innermost (original cause) exception in the chain.

    Walks from :func:`_pick_exception_chain_root` down through
    ``__cause__`` (preferred) / ``__context__`` links, bounded by
    :data:`_MAX_EXCEPTION_CHAIN_DEPTH` and cycle-guarded via an
    ``id()`` seen-set, and returns the deepest reachable stacktrace.

    Used to source the ``stopped.text`` overlay for exception
    snapshots: VS Code anchors the red "Exception has occurred" hover
    on the **top** stack frame, which under the flattened chain view
    is the innermost cause's innermost frame. Surfacing the outermost
    exception's text there would label the wrong line (the cause's
    raise site) with the effect's message.

    Returns ``None`` when the snapshot has no (non-filtered) exception
    stacktraces.
    """
    current = _pick_exception_chain_root(snapshot, is_filtered=is_filtered)
    if current is None:
        return None
    seen: set[int] = {id(current)}
    depth = 0
    while depth < _MAX_EXCEPTION_CHAIN_DEPTH:
        next_stacktrace: Stacktrace | None = current.get_cause()
        if next_stacktrace is None:
            next_stacktrace = current.get_context()
        if next_stacktrace is None or id(next_stacktrace) in seen:
            break
        seen.add(id(next_stacktrace))
        current = next_stacktrace
        depth += 1
    return current


def _describe_exception_chain(chain_root: Stacktrace) -> str:
    """Label for the virtual exception-chain thread row.

    Format: ``Exception: <ExcType>(<short message>)``. Falls back to
    just the type name when ``str(exc)`` is empty. Never includes a
    ``Thread N`` prefix — the whole point of the virtual thread is
    that it isn't a thread.
    """
    exc = chain_root.exception_object
    if exc is None:
        return "Exception"
    try:
        rep = repr(exc)
    except Exception:  # noqa: BLE001
        rep = type(exc).__name__
    type_name = type(exc).__name__
    paren = rep.find("(")
    if paren > 0:
        candidate = rep[:paren].strip()
        if candidate:
            type_name = candidate
    try:
        message = str(exc)
    except Exception:  # noqa: BLE001
        message = ""
    # Collapse multi-line messages onto one line for the CALL STACK
    # header row — VS Code truncates but newlines render awkwardly.
    message = message.replace("\n", " ").replace("\r", " ").strip()
    if message:
        return f"Exception: {type_name}({message})"
    return f"Exception: {type_name}"


def _prune_orphan_separators(
    flat: list[tuple[str, Stacktrace | None, int | str]],
) -> list[tuple[str, Stacktrace | None, int | str]]:
    """Drop separator entries that end up adjacent or leading/trailing.

    After ``hideFilteredFrames`` filtering, an exception's entire
    frame group may be empty, leaving two separators back-to-back or
    a separator at the very start/end of the flattened list. Those
    separators carry no useful information and would render as
    orphaned header rows, so we drop them.
    """
    result: list[tuple[str, Stacktrace | None, int | str]] = []
    prev_kind: str | None = None
    for entry in flat:
        kind = entry[0]
        if kind == "separator" and prev_kind in (None, "separator"):
            continue
        result.append(entry)
        prev_kind = kind
    # Trim a trailing separator if the last non-separator group was
    # dropped entirely.
    while result and result[-1][0] == "separator":
        result.pop()
    return result


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


def _resolve_start_index(raw: object, snapshot_count: int) -> int:
    """Resolve the ``snapshotIndex`` launch argument to a real index.

    Negative values index from the end, Python-style, so ``-1`` opens
    the most recent snapshot. That is what the VS Code camera button
    passes: it captures first and then launches the viewer, and the
    snapshot the user just asked for is the last one in the file.

    Out-of-range values clamp rather than error — the caller usually
    cannot know the count at launch time, and refusing to open the
    viewer is a worse outcome than landing on an edge snapshot. A
    missing or non-numeric value opens the first snapshot, preserving
    the behaviour clients that predate this argument rely on.
    """
    if raw is None or isinstance(raw, bool):
        return 0
    try:
        index = int(raw)  # pyre-ignore[6]: guarded by the TypeError catch
    except (TypeError, ValueError):
        return 0
    if index < 0:
        index += snapshot_count
    return max(0, min(index, snapshot_count - 1))


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
      1. If the preferred thread name is the virtual exception-chain
         sentinel (user previously focused the synthetic chain view),
         return :data:`_EXCEPTION_CHAIN_THREAD_ID` so focus stays on
         that view across snapshot jumps as long as it exists.
      2. If ``preferred_thread_name`` is set and a thread with that
         name exists in the snapshot, return its id — this preserves
         the user's focus across snapshot jumps.
      3. If the snapshot has any exception stacktrace, return
         :data:`_EXCEPTION_CHAIN_THREAD_ID` — the virtual chain view is
         the natural landing spot for exception snapshots and makes VS
         Code highlight the Exception Info panel.
      4. First thread in snapshot insertion order.

    ``is_filtered_thread`` is forwarded to
    :func:`_pick_exception_chain_root` so the chain-view landing step
    (priority 1 and 3) only fires when at least one non-filtered
    exception stacktrace exists. This keeps the stop-focus consistent
    with what ``handle_threads`` surfaces in the UI.
    """
    has_chain = (
        _pick_exception_chain_root(snapshot, is_filtered=is_filtered_thread) is not None
    )
    if has_chain and preferred_thread_name == _EXCEPTION_CHAIN_PREFERRED_NAME:
        return _EXCEPTION_CHAIN_THREAD_ID
    if preferred_thread_name is not None:
        for st in snapshot.stacktraces.values():
            name = st.thread_name or f"Thread {int(st.id)}"
            if name == preferred_thread_name:
                return int(st.id)
    if has_chain:
        return _EXCEPTION_CHAIN_THREAD_ID
    first = next(iter(snapshot.stacktraces.values()), None)
    if first is None:
        return None
    return int(first.id)
