/**
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * This source code is licensed under the MIT license found in the
 * LICENSE file in the root directory of this source tree.
 *
 * @format
 */

/**
 * Tintype Snapshots sidebar — multi-viewer state.
 *
 * Context keys set by this module:
 *
 *   * ``${commandPrefix}:snapshot:can-take`` — boolean. ``true`` when the
 *     active debug session is a live parent that can capture a snapshot.
 *     Gates the camera button.
 *   * ``${commandPrefix}:snapshot:viewer-active`` — boolean. ``true`` when
 *     ≥1 viewer is live. Retained for backward-compat with existing
 *     ``when`` clauses on the sidebar container + single-viewer commands.
 *   * ``${commandPrefix}:snapshot:viewer-count`` — number. Count of live
 *     viewers in the registry. Use for finer-grained ``when`` clauses that
 *     only make sense with ≥2 viewers (e.g. "focus next viewer").
 */

import crypto from 'crypto';
import path from 'path';
import vscode from 'vscode';

import type {SnapshotListResponse, SnapshotTreeProvider} from './snapshot-tree-provider';

/**
 * Cap any single string extra at this many characters so telemetry payloads
 * remain bounded even when filesystem paths are pathological.
 */
const EXTRAS_STRING_MAX = 256;

function truncate(value: string): string {
  if (value.length <= EXTRAS_STRING_MAX) {
    return value;
  }
  return value.slice(0, EXTRAS_STRING_MAX);
}

type SnapshotEventExtras = Record<string, unknown>;

export type SnapshotTelemetryEvent =
  | 'takeSnapshot'
  | 'initializeSnapshotting'
  | 'corruptSnapshot'
  | 'snapshotNavigate'
  | 'saveSnapshotFile';

export type SnapshotTelemetryErrorCode =
  | 'takeSnapshotError'
  | 'initializeSnapshottingError'
  | 'jumpToSnapshotError'
  | 'jumpToLastSnapshotError'
  | 'saveSnapshotFileError';

export type SnapshotTelemetrySink = {
  logEvent(
    session: vscode.DebugSession | undefined,
    eventName: SnapshotTelemetryEvent,
    extras: SnapshotEventExtras,
  ): void;
  logError(
    session: vscode.DebugSession | undefined,
    error: Error,
    eventName: SnapshotTelemetryEvent,
    errorCode: SnapshotTelemetryErrorCode,
    extras: SnapshotEventExtras,
  ): void;
};

export type SnapshotEvaluator = (expression: string) => Thenable<unknown>;

export type CaptureRuntimePreparation = (
  session: vscode.DebugSession,
  evaluate: SnapshotEvaluator,
) => Promise<void>;

/** Entry point a capture was requested from. Recorded on telemetry. */
export type SnapshotTriggerVariant = 'takeSnapshot' | 'takeSnapshotOnParent' | 'autoSnapshot';

export type AutoSnapshotConfig = {
  enabled: boolean;
  /**
   * Deadline handed to ``tintype.vscode.capture()``. Enforced inside the
   * debuggee, which truncates the snapshot on expiry rather than running
   * to completion — see :meth:`SnapshotProvider.handleParentStopped`.
   */
  timeoutMs: number;
  /**
   * How long the session must sit stopped before a capture starts. ``0``
   * captures immediately on every stop.
   */
  settleMs: number;
};

/**
 * Resolved per stop rather than cached so toggling the setting takes
 * effect on the next step instead of requiring a session restart, and
 * so hosts can scope the lookup to the session's workspace folder.
 */
export type AutoSnapshotConfigResolver = (session: vscode.DebugSession) => AutoSnapshotConfig;

/** Setting leaf names shared by every host, so the two `package.json`
 * contributions and the defaults below cannot drift apart. */
export const AUTO_SNAPSHOT_ENABLED_SETTING = 'autoSnapshotOnStop';
export const AUTO_SNAPSHOT_TIMEOUT_SETTING = 'autoSnapshotTimeoutMs';
export const AUTO_SNAPSHOT_SETTLE_SETTING = 'autoSnapshotSettleMs';

/**
 * The capture runs on the paused thread, so the next step request can't
 * be serviced until it returns. The settle delay means that normally
 * happens while the user is reading rather than stepping; this deadline
 * only bounds the case where they resume right as a capture begins, so
 * it is deliberately well under tintype's own 1s library default.
 */
export const DEFAULT_AUTO_SNAPSHOT_TIMEOUT_MS = 250;

/**
 * Long enough to sit out a burst of held-down stepping, short enough
 * that pausing to look at a line still yields a snapshot. A judgement
 * call, not a measurement — hence the setting.
 */
export const DEFAULT_AUTO_SNAPSHOT_SETTLE_MS = 500;

/**
 * Build a resolver that reads the auto-snapshot settings out of
 * ``section``. Hosts differ only in the section they contribute them
 * under (``tintype`` vs ``python-debugger.tintype``).
 *
 * Any positive timeout is honoured as configured. Very short budgets are
 * genuinely useful — tintype cancels mid-walk and keeps the frames it
 * already completed, and its own tests assert a non-empty snapshot at
 * 50ms even against locals whose ``__repr__`` sleeps 200ms. There is no
 * floor to impose beyond rejecting values that aren't a duration at all:
 * the pybind signatures forward the ``double`` straight through with no
 * validation, so a hand-edited ``NaN`` / ``0`` / negative has no defined
 * behaviour and is treated here as "unset".
 *
 * ``settleMs`` accepts ``0`` where ``timeoutMs`` does not: no delay is a
 * meaningful choice (capture on every stop, trading step latency for a
 * denser timeline), whereas a zero-length capture budget is not.
 */
export function createAutoSnapshotConfigResolver(section: string): AutoSnapshotConfigResolver {
  return (session: vscode.DebugSession) => {
    const config = vscode.workspace.getConfiguration(section, session.workspaceFolder?.uri);
    const timeoutMs = config.get<number>(
      AUTO_SNAPSHOT_TIMEOUT_SETTING,
      DEFAULT_AUTO_SNAPSHOT_TIMEOUT_MS,
    );
    const settleMs = config.get<number>(
      AUTO_SNAPSHOT_SETTLE_SETTING,
      DEFAULT_AUTO_SNAPSHOT_SETTLE_MS,
    );
    return {
      enabled: config.get<boolean>(AUTO_SNAPSHOT_ENABLED_SETTING, false),
      timeoutMs:
        Number.isFinite(timeoutMs) && timeoutMs > 0 ? timeoutMs : DEFAULT_AUTO_SNAPSHOT_TIMEOUT_MS,
      settleMs:
        Number.isFinite(settleMs) && settleMs >= 0 ? settleMs : DEFAULT_AUTO_SNAPSHOT_SETTLE_MS,
    };
  };
}

const CAPTURE_IMPORT = "__import__('tintype.vscode', fromlist=['capture']).capture";
const CAPTURE_CALL = `${CAPTURE_IMPORT}()`;
const CAPTURE_WITH_TIMEOUT_PREFIX = `${CAPTURE_IMPORT}(timeout=`;

/**
 * Detect the ``TypeError`` a pre-timeout tintype raises for
 * ``capture(timeout=...)``. Python has used this wording since 3.0, and
 * the free-threaded / GIL capture paths both surface it verbatim.
 */
function isUnexpectedKeywordArgumentError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error);
  return message.includes('unexpected keyword argument');
}

export type SnapshotProviderHostOptions = {
  prepareCaptureRuntime?: CaptureRuntimePreparation;
  telemetry?: SnapshotTelemetrySink;
  /**
   * Omit to leave automatic capture-on-stop unavailable for this host —
   * :func:`registerSnapshotProvider` then skips the parent-session DAP
   * tracker entirely.
   */
  resolveAutoSnapshotConfig?: AutoSnapshotConfigResolver;
};

const NOOP_TELEMETRY: SnapshotTelemetrySink = {
  logEvent: () => {},
  logError: () => {},
};

/**
 * Per-viewer state tracked by the registry.
 *
 * ``parentSessionName`` is captured at launch time because ``DebugSession``
 * objects aren't keep-alive beyond their session lifetime — we cache the
 * user-visible name so the sidebar can still attribute the viewer group
 * after the parent exits.
 */
export type ViewerState = {
  viewerId: string;
  launchToken: string;
  childSession: vscode.DebugSession;
  parentSessionId: string;
  parentSessionName: string;
  parentStartedAt: number;
  /**
   * ``null`` when this viewer's parent name is unique across live viewers.
   * ``"#2"`` / ``"#3"`` / ... when multiple viewers share a parent name,
   * in insertion order. Recomputed on every add/remove.
   */
  disambiguator: string | null;
  pytbPath: string;
  lastList: SnapshotListResponse | null;
  /**
   * ``false`` once the parent session terminates. Freezes ``lastList``
   * updates on the sidebar description so the UX doesn't advertise a
   * live cursor the parent can no longer contribute new snapshots to.
   * The viewer itself stays alive until the user terminates it.
   */
  parentAlive: boolean;
  /**
   * Per-viewer deduplication set for corrupt-snapshot telemetry. A single
   * corrupt index gets reported once per viewer
   * lifetime, not once per tree refresh (the list response is
   * re-fetched on every ``stopped`` event and we don't want that to
   * spam the pipeline).
   */
  corruptReportedIndices: Set<number>;
};

/**
 * Summary shape the tree provider consumes — decouples the tree from
 * the full ``ViewerState`` so renaming fields here doesn't force tree
 * changes.
 */
export type ViewerSummary = {
  viewerId: string;
  parentSessionName: string;
  disambiguator: string | null;
  parentAlive: boolean;
  /**
   * ``true`` iff the viewer's parent is alive AND still accumulating
   * snapshots into a working ``.pytb`` (i.e. ``tintype.vscode.finalize()`` has
   * not been called on it yet). Drives the tree's ``contextValue`` so
   * the Save button only shows on viewers whose file is still live.
   */
  backsLiveInjection: boolean;
  lastList: SnapshotListResponse | null;
};

type PendingLaunch = {
  parentSessionId: string;
  parentSessionName: string;
  parentStartedAt: number;
  pytbPath: string;
};

/**
 * Generate a short unique token used to correlate a viewer launch with
 * the child debug session VS Code reports via ``onDidStartDebugSession``.
 *
 * Exported for tests: the implementation has two branches
 * (``crypto.randomUUID`` vs the Math.random fallback) and we need both
 * exercised so a future refactor that inverts the ``typeof`` check
 * doesn't silently regress to the fallback on modern runtimes (or vice
 * versa).
 */
export function __testOnly_generateLaunchToken(): string {
  return generateLaunchToken();
}

function generateLaunchToken(): string {
  // ``crypto.randomUUID`` is available in all Node versions VS Code ships
  // against; fall back to Math.random + Date.now only when the runtime
  // lacks it (e.g. test envs that stub ``crypto`` globally).
  const maybeRandomUUID: unknown = (crypto as unknown as {randomUUID?: unknown}).randomUUID;
  if (typeof maybeRandomUUID === 'function') {
    return (maybeRandomUUID as () => string)();
  }
  return `${Math.random().toString(36).slice(2)}-${Date.now()}`;
}

/**
 * Drives the camera-icon toolbar button and the "Tintype Snapshots"
 * sidebar. Supports N concurrent viewers, one per ``Take Snapshot`` click.
 *
 * Each viewer is a separate child debug session keyed on its ``session.id``.
 * The parent session may spawn several viewers over its lifetime — e.g. the
 * user takes three snapshots at different points in time — and each gets its
 * own sidebar group.
 *
 * Launch tokens disambiguate concurrent ``Take Snapshot`` clicks on the
 * same parent: two in-flight launches would otherwise collide on the
 * parent-keyed pending map. Each token is generated up front in
 * ``takeSnapshot`` and threaded into the child's launch config as
 * ``tintypeLaunchToken`` so ``handleStartSession`` can match back.
 */
export class SnapshotProvider {
  private injectedSessions: Set<string> = new Set();
  private initializingSessions: Set<string> = new Set();

  /**
   * In-flight ``initializeSnapshotting`` promises keyed by parent
   * session id. Lets ``ensureSnapshotting`` callers (e.g. the
   * snappoints DAP processor) coalesce onto the same injection
   * attempt instead of racing with a click on the camera button.
   */
  private initializationPromises: Map<string, Promise<void>> = new Map();

  /**
   * Live parent sessions indexed by ``session.id``.
   * Drives the ``${commandPrefix}:tintype:active-viewer-has-live-parent``
   * context key which gates the "Take Snapshot on Parent" toolbar
   * button on the viewer.
   *
   * Stored as a ``Map`` rather than a ``Set`` because the
   * ``takeSnapshotOnParent`` flow needs the actual ``DebugSession``
   * object to pass to ``takeSnapshot``, not just its id.
   */
  private liveParents: Map<string, vscode.DebugSession> = new Map();

  /**
   * Per-parent-session cache of the ``.pytb`` path + parent cwd, populated
   * by ``initializeSnapshotting`` and reused on every subsequent
   * ``Take Snapshot`` click so we don't re-issue
   * ``tintype.vscode.session_info()`` (idempotent, but
   * avoids a round-trip on the critical path between click and viewer).
   */
  private parentInjectionState: Map<string, {pytbPath: string; cwd: string}> = new Map();

  /**
   * Registry of live viewers keyed on child ``session.id``. ``Map``
   * iteration order is insertion order — relied on by the disambiguator
   * re-numbering logic and by the tree provider's group rendering.
   */
  private viewers: Map<string, ViewerState> = new Map();

  /**
   * Launches in-flight: takeSnapshot called, ``startDebugging`` awaiting,
   * child ``onDidStartDebugSession`` hasn't fired yet. Keyed on the
   * opaque launch token threaded through the config; a parent can have
   * multiple entries here simultaneously.
   */
  private pendingByLaunchToken: Map<string, PendingLaunch> = new Map();

  private treeProvider: SnapshotTreeProvider | null = null;

  /**
   * Parent sessions with an automatic capture still running, keyed by
   * ``session.id``. Stepping can outrun the capture round-trip, so
   * overlapping stops are dropped rather than queued — this is what
   * keeps auto-snapshot from adding unbounded ``evaluate`` traffic
   * ahead of the user's next step request.
   */
  private autoSnapshotInFlight: Set<string> = new Set();

  /**
   * Captures scheduled by :meth:`handleParentStopped` but not yet fired,
   * keyed by ``session.id``. ``resolve`` settles the promise that method
   * handed back, so a cancelled schedule doesn't leave a caller hanging.
   */
  private pendingAutoSnapshots: Map<
    string,
    {timer: ReturnType<typeof setTimeout>; resolve: () => void}
  > = new Map();

  /**
   * Sessions where an automatic capture has failed once and is therefore
   * switched off for the rest of the session. See :meth:`runAutoSnapshot`.
   * The manual camera button ignores this and still works.
   */
  private autoSnapshotDisabled: Set<string> = new Set();

  /**
   * Failure from the most recent :meth:`takeSnapshotInternal` call, so
   * :meth:`runAutoSnapshot` can name the cause when it latches off. The
   * auto path deliberately swallows the error rather than rethrowing, so
   * this is how the reason survives.
   */
  private lastAutoSnapshotError: Error | undefined;

  /**
   * Sessions whose tintype package predates the ``capture(timeout=...)``
   * parameter. Probed once on first failure; see :meth:`requestCapture`.
   */
  private captureTimeoutUnsupported: Set<string> = new Set();

  private readonly injectionDebugTypes: ReadonlySet<string>;
  private readonly prepareCaptureRuntime: CaptureRuntimePreparation | undefined;
  private readonly telemetry: SnapshotTelemetrySink;
  private readonly resolveAutoSnapshotConfig: AutoSnapshotConfigResolver | undefined;

  constructor(
    injectionDebugType: string | readonly string[],
    private viewerDebugType: string,
    private commandPrefix: string,
    hostOptions: SnapshotProviderHostOptions = {},
  ) {
    this.injectionDebugTypes = new Set(
      typeof injectionDebugType === 'string' ? [injectionDebugType] : injectionDebugType,
    );
    this.prepareCaptureRuntime = hostOptions.prepareCaptureRuntime;
    this.telemetry = hostOptions.telemetry ?? NOOP_TELEMETRY;
    this.resolveAutoSnapshotConfig = hostOptions.resolveAutoSnapshotConfig;
  }

  private supportsLiveSession(session: vscode.DebugSession): boolean {
    return this.injectionDebugTypes.has(session.type);
  }

  public setTreeProvider(treeProvider: SnapshotTreeProvider): void {
    this.treeProvider = treeProvider;
  }

  /**
   * Accessor for the tree provider — returns lightweight summaries so
   * the tree doesn't need to know about internal pending / injection state.
   */
  public getViewerSummaries(): ViewerSummary[] {
    const out: ViewerSummary[] = [];
    for (const v of this.viewers.values()) {
      out.push({
        viewerId: v.viewerId,
        parentSessionName: v.parentSessionName,
        disambiguator: v.disambiguator,
        parentAlive: v.parentAlive,
        // ``injectedSessions.has('')`` is trivially false, so standalone
        // viewers (``parentSessionId === ''``) naturally report
        // ``backsLiveInjection: false`` without a special case.
        //
        // The ``pytbPath`` check guards against the following race:
        //
        //   1. User saves viewer V1 — ``finalize()`` seals V1's working
        //      file and we drop the parent's entry in
        //      ``injectedSessions`` / ``parentInjectionState``.
        //   2. User clicks Take Snapshot. ``initializeSnapshotting``
        //      runs again, spawns a NEW viewer V2 against a fresh
        //      working file, and re-adds the parent to
        //      ``injectedSessions``.
        //   3. V1 is still in the registry with ``parentAlive: true``
        //      and its old ``pytbPath``. Without the path comparison
        //      below, V1 would flip back to ``backsLiveInjection:
        //      true`` — and clicking Save on V1 would finalize the
        //      *new* working file V2 is serving.
        //
        // Requiring ``viewer.pytbPath`` to match the parent's *current*
        // working-file path keeps the Save button bound to the viewer
        // whose file is actually still open for writes.
        backsLiveInjection:
          v.parentAlive &&
          this.injectedSessions.has(v.parentSessionId) &&
          this.parentInjectionState.get(v.parentSessionId)?.pytbPath === v.pytbPath,
        lastList: v.lastList,
      });
    }
    return out;
  }

  /**
   * Find the first live viewer whose parent is ``parentSessionId``.
   * Returns ``null`` if no matching viewer exists or if the only match
   * has already been disconnected (``parentAlive === false``). Used by
   * ``takeSnapshot`` to decide whether a click should reuse an existing
   * group or spawn a fresh one.
   */
  private findLiveViewerForParent(parentSessionId: string): ViewerState | null {
    // "Live" here means *still backing the parent's current working
    // file*. Without the ``pytbPath`` check, a viewer whose ``.pytb``
    // was finalized would keep matching (``parentAlive`` stays true
    // until the parent session terminates), and callers — both
    // ``takeSnapshotInternal`` and ``notifySnapshotAvailable`` —
    // would refresh the stale viewer instead of launching a fresh
    // one for the new working file. The ``backsLiveInjection`` flag
    // in ``getViewerSummaries`` already uses the same predicate;
    // this keeps the rest of the registry consistent with it.
    const currentPytb = this.parentInjectionState.get(parentSessionId)?.pytbPath;
    if (currentPytb == null) {
      return null;
    }
    for (const v of this.viewers.values()) {
      if (v.parentSessionId === parentSessionId && v.parentAlive && v.pytbPath === currentPytb) {
        return v;
      }
    }
    return null;
  }

  public takeSnapshot(session?: vscode.DebugSession): Promise<void> {
    return this.takeSnapshotInternal(session, 'takeSnapshot').then(() => undefined);
  }

  /**
   * Ensure tintype is injected into ``session`` and a working ``.pytb``
   * + viewer exist. Idempotent and concurrency-safe — concurrent callers
   * share a single in-flight ``initializeSnapshotting`` promise. Returns
   * once injection is complete; throws if injection fails.
   *
   * Used both by ``takeSnapshotInternal`` (camera button) and by the
   * snappoints DAP processor (which must wait for injection before
   * forwarding the rewritten ``setBreakpoints`` to pydevd).
   */
  public async ensureSnapshotting(
    session: vscode.DebugSession,
    variant: SnapshotTriggerVariant = 'takeSnapshot',
    options: {launchViewer?: boolean} = {},
  ): Promise<void> {
    if (this.injectedSessions.has(session.id)) {
      return;
    }
    let inflight = this.initializationPromises.get(session.id);
    if (inflight == null) {
      inflight = this.initializeSnapshotting(
        session,
        variant,
        options.launchViewer ?? true,
      ).finally(() => {
        this.initializationPromises.delete(session.id);
      });
      this.initializationPromises.set(session.id, inflight);
    }
    await inflight;
  }

  /**
   * Public lookup used by the snappoints DAP processor to refresh the
   * correct viewer when a ``tintypeSnapshotAdded`` event fires on its
   * parent session. Returns ``undefined`` when no live viewer is bound
   * to the parent yet (which is normal before the first snapshot).
   */
  public getViewerIdForParent(parentSessionId: string): string | undefined {
    return this.findLiveViewerForParent(parentSessionId)?.viewerId;
  }

  /**
   * Notify the provider that a new snapshot has landed in ``session``'s
   * working ``.pytb`` (typically because a snappoint just fired).
   * Refreshes the live viewer when one exists; otherwise lazily
   * launches a viewer against the cached working file path now that it
   * has at least one snapshot to read.
   *
   * Safe to call before injection completes — silently no-ops when no
   * ``parentInjectionState`` entry exists yet for the session.
   */
  public async notifySnapshotAvailable(session: vscode.DebugSession): Promise<void> {
    // Not yet injected (or post-finalize, which clears the entry):
    // ``ensureSnapshotting`` re-injects and launches a fresh viewer
    // against the current working file. No further refresh needed —
    // the viewer will read the file's contents at launch.
    if (!this.injectedSessions.has(session.id)) {
      await this.ensureSnapshotting(session);
      return;
    }
    // Already injected and a viewer should exist for the current
    // working file. Refresh its snapshot list so the panel reflects
    // the new snapshot count. ``findLiveViewerForParent`` is
    // path-aware, so a viewer left over from a finalized file won't
    // match and we'll return without a stale refresh.
    const existing = this.findLiveViewerForParent(session.id);
    if (existing != null) {
      await this.refreshSnapshotList(existing.viewerId);
    }
  }

  /**
   * Adopt a working file initialized by the capture runtime itself.
   * Public debugpy snappoints use this path because they capture without
   * stopping, then announce the file through a DAP output event.
   */
  public async adoptWorkingFile(
    session: vscode.DebugSession,
    pytbPath: string,
    cwd: string,
  ): Promise<void> {
    this.parentInjectionState.set(session.id, {pytbPath, cwd});
    this.injectedSessions.add(session.id);
    this.updateHasLiveParentContextKey(vscode.debug.activeDebugSession);
    this.updateBacksLiveInjectionContextKey(vscode.debug.activeDebugSession);
    this.treeProvider?.refreshAll();

    const existing = this.findLiveViewerForParent(session.id);
    if (existing != null) {
      await this.refreshSnapshotList(existing.viewerId);
      return;
    }

    const launchPending = Array.from(this.pendingByLaunchToken.values()).some(
      pending => pending.parentSessionId === session.id && pending.pytbPath === pytbPath,
    );
    if (launchPending) {
      return;
    }

    await this.launchViewerFor(session, pytbPath, cwd);
    this.updateContextKeys();
    this.treeProvider?.refreshAll();
  }

  /**
   * Shared implementation for the entry points that take a snapshot:
   * ``Take Snapshot`` on the parent's CALL STACK, ``Take Snapshot on
   * Parent`` on the viewer's toolbar, and automatic capture-on-stop.
   * ``variant`` disambiguates them on the resulting telemetry event.
   *
   * Returns whether the snapshot was taken. Failures are always logged
   * to telemetry, but only *reported* to the user for the two manual
   * variants: a toast answers an action the user just took, whereas
   * auto-capture is a passive event and would repeat the toast on every
   * stop. :meth:`runAutoSnapshot` handles the auto path's reporting.
   */
  private async takeSnapshotInternal(
    session: vscode.DebugSession | undefined,
    variant: SnapshotTriggerVariant,
    timeoutMs?: number,
  ): Promise<boolean> {
    const userInitiated = variant !== 'autoSnapshot';
    const debugSession = session ?? vscode.debug.activeDebugSession;
    if (debugSession == null || !this.supportsLiveSession(debugSession)) {
      if (userInitiated) {
        void vscode.window.showErrorMessage(
          'Cannot take snapshot: No active Python debug session found',
        );
      }
      return false;
    }

    try {
      // ``ensureSnapshotting`` injects tintype + launches the viewer
      // (against an empty ``.pytb`` if needed). It no longer takes a
      // "seed" snapshot; the manual button is responsible for issuing
      // the snapshot itself so the first click always captures the
      // moment the user clicked, regardless of whether the snappoints
      // processor already initialized.
      // ``launchViewer: false`` — this method owns its viewer and opens
      // it below, after the capture has landed, so the viewer can start
      // on the snapshot the user just asked for rather than the oldest
      // one in the file.
      await this.ensureSnapshotting(debugSession, variant, {launchViewer: false});

      await this.requestCapture(debugSession, timeoutMs);

      // If a live viewer already exists for this parent, just refresh it
      // so the new snapshot appears in the same sidebar group. Spawning a
      // fresh viewer on every click would flood the sidebar with empty
      // groups and lose continuity with the user's existing navigation
      // state.
      const existingViewer = this.findLiveViewerForParent(debugSession.id);
      if (existingViewer != null) {
        await this.refreshSnapshotList(existingViewer.viewerId);
        // Read the post-take totals off the just-refreshed list so the
        // telemetry row carries the new snapshot's index and the new
        // post-take total. ``lastList`` is populated by
        // ``refreshSnapshotList`` above.
        const list = existingViewer.lastList;
        const totalSnapshots = list?.snapshots.length ?? 0;
        const snapshotIndex = totalSnapshots > 0 ? totalSnapshots - 1 : 0;
        this.telemetry.logEvent(debugSession, 'takeSnapshot', {
          variant,
          reusedViewer: true,
          snapshotIndex,
          totalSnapshots,
          parentSessionType: debugSession.type,
          launchToken: existingViewer.launchToken,
        });
        return true;
      }

      // Automatic captures never spawn a viewer — the snapshot is now
      // in the working file, and that is the whole job. Recording is a
      // passive activity; opening a debug session for it is what pulls
      // focus off the program being stepped. If a viewer is open the
      // branch above has already refreshed it live.
      if (!userInitiated) {
        this.telemetry.logEvent(debugSession, 'takeSnapshot', {
          variant,
          reusedViewer: false,
          viewerLaunched: false,
          parentSessionType: debugSession.type,
        });
        return true;
      }

      // No registered viewer for the current working file. Two cases:
      //
      //   1. A concurrent click already launched one and the child
      //      session hasn't fired ``onDidStartDebugSession`` yet — the
      //      ``startDebugging`` promise resolves before VS Code dispatches
      //      the child-start event, so there's a short window where a
      //      launch is in flight but not yet in ``viewers``. That viewer
      //      will read the snapshot we just wrote when it finishes
      //      registering.
      //   2. No viewer exists: the user terminated the previous one, or
      //      only automatic captures have run so far and those never
      //      open one. Launch a fresh viewer to host the new snapshot.
      //
      // ``pendingByLaunchToken`` lets us tell the two apart: an entry
      // means case 1; absence means case 2.
      const hasPendingLaunch = Array.from(this.pendingByLaunchToken.values()).some(
        p => p.parentSessionId === debugSession.id,
      );
      if (hasPendingLaunch) {
        // Case 1 — the in-flight launch will pick up the snapshot, and
        // whichever click created it emits its own take-snapshot row.
        return true;
      }

      // Case 2 — user terminated the prior viewer. Launch a fresh
      // viewer against the cached .pytb path so the new snapshot has
      // somewhere to live.
      const cached = this.parentInjectionState.get(debugSession.id);
      if (cached == null) {
        // Should be unreachable — ``injectedSessions`` is only set after
        // ``parentInjectionState`` is populated — but bail quietly
        // instead of crashing the snapshot take if invariant drifts.
        return true;
      }
      const freshLaunchToken = await this.launchViewerFor(
        debugSession,
        cached.pytbPath,
        cached.cwd,
      );
      this.telemetry.logEvent(debugSession, 'takeSnapshot', {
        variant,
        reusedViewer: false,
        parentSessionType: debugSession.type,
        launchToken: freshLaunchToken,
      });
      return true;
    } catch (e) {
      this.telemetry.logError(debugSession, e as Error, 'takeSnapshot', 'takeSnapshotError', {
        variant,
        parentSessionType: debugSession.type,
      });
      if (userInitiated) {
        void vscode.window.showErrorMessage(`Tintype snapshot failed: ${(e as Error).message}`);
      } else {
        // Only the auto path stashes the reason; a manual failure racing
        // an automatic one must not supply the latch message.
        this.lastAutoSnapshotError = e as Error;
      }
      return false;
    }
  }

  /**
   * Issue the capture itself. When ``timeoutMs`` is set the deadline is
   * handed to ``tintype.vscode.capture()`` so the debuggee bounds its own
   * stack walk — truncating the snapshot instead of letting a slow
   * capture hold up the next step. Capturing without a deadline (the
   * manual camera button) keeps tintype's own defaults.
   *
   * ``timeout`` was added to ``capture()`` after the extension shipped,
   * so a user whose installed tintype predates it would otherwise see a
   * ``TypeError`` on every automatic snapshot. Fall back to an uncapped
   * capture once per session and remember the answer.
   */
  private async requestCapture(
    session: vscode.DebugSession,
    timeoutMs: number | undefined,
  ): Promise<void> {
    const useTimeout = timeoutMs != null && !this.captureTimeoutUnsupported.has(session.id);
    const evaluate = (expression: string) =>
      session.customRequest('evaluate', {expression, context: 'repl'});
    if (!useTimeout) {
      await evaluate(CAPTURE_CALL);
      return;
    }

    // Seconds, matching the tintype Python API.
    const timeoutSeconds = timeoutMs / 1000;
    try {
      await evaluate(`${CAPTURE_WITH_TIMEOUT_PREFIX}${timeoutSeconds})`);
    } catch (e) {
      if (!isUnexpectedKeywordArgumentError(e)) {
        throw e;
      }
      this.captureTimeoutUnsupported.add(session.id);
      // eslint-disable-next-line no-console
      console.warn(
        '[tintype] the installed tintype package does not accept a capture timeout; ' +
          'automatic snapshots will run uncapped. Upgrade tintype to bound them.',
      );
      await evaluate(CAPTURE_CALL);
    }
  }

  private async initializeSnapshotting(
    session: vscode.DebugSession,
    triggerVariant: SnapshotTriggerVariant = 'takeSnapshot',
    launchesViewer = true,
  ): Promise<void> {
    this.initializingSessions.add(session.id);
    try {
      const evaluate = (expression: string) =>
        session.customRequest('evaluate', {expression, context: 'repl'});

      const prepareCaptureRuntime =
        this.prepareCaptureRuntime ??
        (async (_session: vscode.DebugSession, targetEvaluate: SnapshotEvaluator) => {
          await targetEvaluate('import tintype');
        });
      await prepareCaptureRuntime(session, evaluate);

      let workingFilePath: string;
      let parentCwd: string;
      let launchToken: string | undefined;
      try {
        const response = (await evaluate(
          "__import__('tintype.vscode', fromlist=['session_info']).session_info()['workingFile']",
        )) as {
          result: string;
        };
        workingFilePath = response.result.replace(/^['"]|['"]$/g, '');

        // Ask the debuggee for its own cwd so the child viewer can resolve
        // relative source paths (e.g. ``./empty_script.py``) that haven't
        // been baked into the ``.pytb``'s source table yet. The pytb is
        // a *working file* — ``tintype.vscode.finalize()`` has not necessarily
        // run yet, so we can't rely on
        // ``SnapshotReader.get_all_source_files()`` while it's live.
        const cwdResponse = (await evaluate(
          "__import__('tintype.vscode', fromlist=['session_info']).session_info()['cwd']",
        )) as {result: string};
        parentCwd = cwdResponse.result.replace(/^['"]|['"]$/g, '');

        this.parentInjectionState.set(session.id, {
          pytbPath: workingFilePath,
          cwd: parentCwd,
        });
        // Launch the viewer against the working file for user-initiated
        // captures. The viewer tolerates a zero-snapshot ``.pytb`` at
        // launch time and stays in a "waiting" state until snapshots
        // accrue. Callers that need a "first" snapshot — e.g. the manual
        // camera button — issue an explicit ``evaluate`` of
        // ``tintype.vscode.capture()`` AFTER this init returns.
        //
        // ``takeSnapshotInternal`` opts out: it launches its own viewer
        // *after* writing the snapshot, so the viewer can open on it
        // (see ``snapshotIndex: -1`` in ``launchSnapshotDebugSession``).
        // Automatic captures opt out and never launch one at all — a
        // viewer reports ``stopped`` and the tintype adapter never sends
        // ``continued``, so once one exists VS Code treats it as
        // permanently stopped and steals focus onto it whenever the
        // program under test resumes for a perceptible interval (a
        // ``time.sleep`` is enough). See :meth:`handleParentStopped`.
        if (launchesViewer) {
          launchToken = await this.launchViewerFor(session, workingFilePath, parentCwd);
        }
      } catch (e) {
        this.telemetry.logError(
          session,
          e as Error,
          'initializeSnapshotting',
          'initializeSnapshottingError',
          {parentSessionType: session.type, triggerVariant},
        );
        throw e;
      }

      this.injectedSessions.add(session.id);
      // A fresh injection flips the Save-button gate on for any viewer
      // focused on this parent.
      this.updateBacksLiveInjectionContextKey(vscode.debug.activeDebugSession);
      // Tree contextValue is derived from ``backsLiveInjection`` — rebuild
      // affected items so the inline Save icon appears on their rows.
      this.treeProvider?.refreshAll();

      this.telemetry.logEvent(session, 'initializeSnapshotting', {
        pytbPath: truncate(workingFilePath),
        cwd: truncate(parentCwd),
        parentSessionType: session.type,
        launchToken,
      });
    } finally {
      this.initializingSessions.delete(session.id);
    }
  }

  /**
   * Bake a fresh launch token + pending entry, then call
   * ``launchSnapshotDebugSession``. Split out so both the first-click
   * (``initializeSnapshotting``) and subsequent-click (``takeSnapshot``)
   * paths can share it.
   */
  private async launchViewerFor(
    parentSession: vscode.DebugSession,
    pytbFilePath: string,
    cwd: string,
  ): Promise<string> {
    const launchToken = generateLaunchToken();
    const decoratedParentName = this.decorateParentNameAtLaunch(parentSession.name);

    this.pendingByLaunchToken.set(launchToken, {
      parentSessionId: parentSession.id,
      parentSessionName: parentSession.name,
      parentStartedAt: Date.now(),
      pytbPath: pytbFilePath,
    });

    try {
      await this.launchSnapshotDebugSession(
        parentSession,
        pytbFilePath,
        cwd,
        launchToken,
        decoratedParentName,
      );
      return launchToken;
    } catch (e) {
      // startDebugging threw — drop the pending entry so it doesn't leak.
      this.pendingByLaunchToken.delete(launchToken);
      throw e;
    }
  }

  /**
   * Compute the name-at-launch disambiguator. Counts live viewers (and
   * any pending launches targeting the same parent name) that already
   * share the parent name, so the VS Code debug picker / CALL STACK
   * header bakes in ``#2`` / ``#3`` at the moment of launch.
   *
   * The sidebar tree labels are recomputed freely later via
   * ``recomputeDisambiguators``; only the session name itself is frozen
   * once VS Code creates it.
   */
  private decorateParentNameAtLaunch(parentName: string): string {
    let collisions = 0;
    for (const v of this.viewers.values()) {
      if (v.parentSessionName === parentName) {
        collisions += 1;
      }
    }
    for (const p of this.pendingByLaunchToken.values()) {
      if (p.parentSessionName === parentName) {
        collisions += 1;
      }
    }
    // 0 collisions -> no decoration (first viewer with this name)
    // ≥1 collisions -> "#2" for the 2nd, "#3" for the 3rd, etc.
    if (collisions === 0) {
      return parentName;
    }
    return `${parentName} #${collisions + 1}`;
  }

  /**
   * Launch the child "Tintype Snapshot Viewer" session against the
   * working ``.pytb`` file. The owning host provides the viewer adapter for
   * its configured debug type.
   *
   * ``cwd`` is forwarded through to the DAP server so relative source
   * paths captured in frames resolve directly on disk — critical when
   * the snapshot hasn't been finalized.
   */
  private async launchSnapshotDebugSession(
    parentSession: vscode.DebugSession,
    pytbFilePath: string,
    cwd: string,
    launchToken: string,
    decoratedParentName: string,
  ): Promise<void> {
    const config: vscode.DebugConfiguration = {
      type: this.viewerDebugType,
      // ``attach`` (not ``launch``) so VS Code renders the stop button
      // as the disconnect-plug icon. The tintype adapter dispatches
      // ``attach`` through the same launch handler server-side.
      request: 'attach',
      // Session name is immutable after startDebugging, so we bake the
      // parent name (plus any at-launch disambiguator) into it here so
      // CALL STACK headers aren't ambiguous across viewers.
      name: `Tintype (${decoratedParentName})`,
      pytbPath: pytbFilePath,
      // Negative indices count from the end, so ``-1`` opens on the most
      // recent snapshot. Every launch through here follows a capture the
      // user just triggered — the camera button, Take Snapshot on
      // Parent, or a snappoint firing — so the newest snapshot is the
      // one they want to see. Opening on the oldest would bury it under
      // however many snapshots had already accumulated.
      snapshotIndex: -1,
      parentSessionId: parentSession.id,
      parentSessionName: parentSession.name,
      tintypeLaunchToken: launchToken,
      cwd,
    };

    const started = await vscode.debug.startDebugging(parentSession.workspaceFolder, config);
    if (!started) {
      throw new Error('Failed to launch tintype snapshot debug session');
    }
  }

  /** Refresh the snapshot list for a registered viewer. */
  public async refreshSnapshotList(viewerId: string): Promise<void> {
    const viewer = this.viewers.get(viewerId);
    if (viewer == null || this.treeProvider == null) {
      return;
    }

    try {
      const response = (await viewer.childSession.customRequest(
        'tintypeSnapshotList',
      )) as SnapshotListResponse;
      viewer.lastList = response;
      this.treeProvider.refreshViewer(viewerId);
      this.reportCorruptSnapshots(viewer, response);
    } catch (e) {
      // The viewer may have terminated between the stopped event and
      // this request — a real race we have to swallow. But genuine
      // DAP errors (Python exceptions in
      // ``handle_tintype_snapshot_list``, malformed responses, etc.)
      // end up here too; leaving them silent means the sidebar shows
      // stale state with no diagnostic trail. Log to the devtools
      // console so bug reports have a footprint to reference.
      // eslint-disable-next-line no-console
      console.warn(`[tintype] refreshSnapshotList(${viewerId}) failed:`, e);
    }
  }

  /**
   * Inspect the just-received ``tintypeSnapshotList`` response for
   * ``corrupt: true`` rows and emit one telemetry event per
   * never-before-reported index. Dedup state
   * lives on the viewer itself (``corruptReportedIndices``) so a
   * refresh loop can't spam the pipeline and terminated viewers
   * naturally drop their dedup state.
   */
  private reportCorruptSnapshots(viewer: ViewerState, response: SnapshotListResponse): void {
    // The wire response may carry ``corrupt: true`` per-row even
    // though the public ``SnapshotData`` type only declares ``index``
    // / ``timestampUs``. Cast at the inspection site so the type
    // contract stays honest for the tree-provider consumer while
    // telemetry can still see the runtime flag.
    type MaybeCorruptSnapshot = {index: number; corrupt?: boolean};
    // Defensive: an unexpected response shape (test stubs, DAP
    // protocol drift) can leave ``snapshots`` undefined. Bail quietly
    // rather than crashing the refresh pipeline.
    const snapshotsUnknown = (response?.snapshots ?? []) as unknown;
    if (!Array.isArray(snapshotsUnknown)) {
      return;
    }
    const snapshots = snapshotsUnknown as MaybeCorruptSnapshot[];
    const totalSnapshots = snapshots.length;
    for (const snap of snapshots) {
      if (snap == null || snap.corrupt !== true) {
        continue;
      }
      if (viewer.corruptReportedIndices.has(snap.index)) {
        continue;
      }
      viewer.corruptReportedIndices.add(snap.index);
      this.telemetry.logEvent(viewer.childSession, 'corruptSnapshot', {
        index: snap.index,
        totalSnapshots,
        firstTimeSeenForViewer: true,
        parentSessionId: viewer.parentSessionId || undefined,
        launchToken: viewer.launchToken,
      });
    }
  }

  /**
   * Ask a specific viewer to navigate to ``index``. The server-side
   * handler advances the cursor and emits ``stopped``; the tracker
   * picks that up and re-fires ``refreshSnapshotList``, so the UI
   * updates without any extra round-trips from this method.
   */
  public async jumpToSnapshot(viewerId: string, index: number): Promise<void> {
    const viewer = this.viewers.get(viewerId);
    if (viewer == null) {
      return;
    }

    const list = viewer.lastList;
    const currentIndex = list?.currentIndex ?? index;
    const totalSnapshots = list?.snapshots?.length ?? 0;

    try {
      await viewer.childSession.customRequest('tintypeJumpToSnapshot', {index});
      // Success path is intentionally NOT logged per-event: navigation
      // telemetry lives at the aggregate session level so
      // sidebar-click jumps and DAP-level step / continue / reverse
      // navigation all contribute to the same engagement signal
      // without a per-UI-surface coverage bias.
    } catch (e) {
      // Same rationale as ``refreshSnapshotList``: termination races
      // are real, but a DAP error from the jump handler is worth a
      // devtools console entry for post-mortem debugging.
      // eslint-disable-next-line no-console
      console.warn(`[tintype] jumpToSnapshot(${viewerId}, ${index}) failed:`, e);
      this.telemetry.logError(
        viewer.childSession,
        e as Error,
        'snapshotNavigate',
        'jumpToSnapshotError',
        {
          trigger: 'sidebarClick',
          index,
          currentIndex,
          totalSnapshots,
          parentSessionId: viewer.parentSessionId || undefined,
          launchToken: viewer.launchToken,
        },
      );
    }
  }

  public handleTerminateSession(session: vscode.DebugSession): void {
    if (this.supportsLiveSession(session)) {
      this.injectedSessions.delete(session.id);
      this.parentInjectionState.delete(session.id);
      this.liveParents.delete(session.id);
      this.cancelPendingAutoSnapshot(session);
      this.autoSnapshotInFlight.delete(session.id);
      this.autoSnapshotDisabled.delete(session.id);
      this.captureTimeoutUnsupported.delete(session.id);

      // If a parent dies while one of its viewers is still live, flip
      // parentAlive to false so the sidebar decorates the group as
      // "(disconnected)" and stops advancing its description as the
      // child DAP session emits new stopped events.
      let anyChanged = false;
      for (const v of this.viewers.values()) {
        if (v.parentSessionId === session.id && v.parentAlive) {
          v.parentAlive = false;
          anyChanged = true;
        }
      }
      // Drop any orphaned pending launches whose parent just died.
      for (const [token, pending] of this.pendingByLaunchToken.entries()) {
        if (pending.parentSessionId === session.id) {
          this.pendingByLaunchToken.delete(token);
        }
      }
      if (anyChanged && this.treeProvider != null) {
        this.treeProvider.refreshAll();
      }
    }

    if (this.viewers.has(session.id)) {
      this.viewers.delete(session.id);
      this.recomputeDisambiguators();
      this.updateContextKeys();
      this.treeProvider?.refreshAll();
    }

    // Parent termination must flip the gate on the viewer toolbar so
    // the Focus Parent / Take Snapshot on Parent / Save buttons grey
    // out immediately.
    this.updateHasLiveParentContextKey(vscode.debug.activeDebugSession);
    this.updateBacksLiveInjectionContextKey(vscode.debug.activeDebugSession);
  }

  public handleStartSession(session: vscode.DebugSession): void {
    if (this.supportsLiveSession(session)) {
      this.liveParents.set(session.id, session);
      void vscode.commands.executeCommand(
        'setContext',
        `${this.commandPrefix}:snapshot:can-take`,
        true,
      );
      // A parent coming online may unlock the viewer-has-live-parent
      // gate for an already-active viewer that was previously orphaned
      // (rare, but possible if the user reopens a ``.pytb`` before
      // restarting its parent).  The backs-live-injection gate stays
      // false until the parent actually starts accumulating snapshots
      // via ``initializeSnapshotting``.
      this.updateHasLiveParentContextKey(vscode.debug.activeDebugSession);
      this.updateBacksLiveInjectionContextKey(vscode.debug.activeDebugSession);
      return;
    }

    // Capture the child viewer session when the factory finishes
    // spawning it. Match on launch token first (atomic across
    // concurrent launches on the same parent) and fall back to
    // parent-id matching for callers that predate the token field.
    if (session.type !== this.viewerDebugType) {
      return;
    }

    const config = session.configuration as Record<string, unknown>;
    const token = config.tintypeLaunchToken as string | undefined;

    let pending: PendingLaunch | undefined;
    if (token != null) {
      pending = this.pendingByLaunchToken.get(token);
      if (pending != null) {
        this.pendingByLaunchToken.delete(token);
      }
    }
    if (pending == null) {
      // Fallback for older launch configs — match the FIRST pending
      // entry whose parent id matches. With the launch-token path
      // covering the happy case this branch is only hit when somebody
      // launches a viewer config by hand without a token.
      const parentId = config.parentSessionId as string | undefined;
      if (parentId != null) {
        let fallbackToken: string | null = null;
        for (const [t, p] of this.pendingByLaunchToken.entries()) {
          if (p.parentSessionId === parentId) {
            fallbackToken = t;
            pending = p;
            break;
          }
        }
        if (fallbackToken != null) {
          this.pendingByLaunchToken.delete(fallbackToken);
        }
      }
    }

    const viewer: ViewerState =
      pending != null
        ? {
            viewerId: session.id,
            launchToken: token ?? '',
            childSession: session,
            parentSessionId: pending.parentSessionId,
            parentSessionName: pending.parentSessionName,
            parentStartedAt: pending.parentStartedAt,
            disambiguator: null,
            pytbPath: pending.pytbPath,
            lastList: null,
            parentAlive: true,
            corruptReportedIndices: new Set<number>(),
          }
        : {
            // No pending launch matched — the user opened this ``.pytb``
            // directly from a launch config (``type: "tintype"``),
            // bypassing the Take Snapshot flow. Register a standalone
            // viewer so the sidebar still picks it up. Empty
            // ``parentSessionId`` (never equal to a real
            // ``session.id``) keeps parent-keyed lookups miss-safe, and
            // ``parentAlive: false`` keeps the parent-scoped toolbar
            // buttons (Take Snapshot on Parent, Save) gated off.
            viewerId: session.id,
            launchToken: token ?? '',
            childSession: session,
            parentSessionId: '',
            parentSessionName: session.name,
            parentStartedAt: Date.now(),
            disambiguator: null,
            pytbPath: (config.pytbPath as string | undefined) ?? '',
            lastList: null,
            parentAlive: false,
            corruptReportedIndices: new Set<number>(),
          };
    this.viewers.set(viewer.viewerId, viewer);
    this.recomputeDisambiguators();
    this.updateContextKeys();
    this.treeProvider?.refreshAll();
    this.updateHasLiveParentContextKey(vscode.debug.activeDebugSession);
    this.updateBacksLiveInjectionContextKey(vscode.debug.activeDebugSession);
    void this.refreshSnapshotList(viewer.viewerId);
  }

  public handleChangeActiveSession(session: vscode.DebugSession | undefined): void {
    const canTake = session != null && this.supportsLiveSession(session);
    void vscode.commands.executeCommand(
      'setContext',
      `${this.commandPrefix}:snapshot:can-take`,
      canTake,
    );
    this.updateHasLiveParentContextKey(session);
    this.updateBacksLiveInjectionContextKey(session);
  }

  /**
   * Set the ``tintype:active-viewer-has-live-parent`` context key to
   * ``true`` iff the active session is a tintype viewer whose parent
   * is still in the live-parents map. Drives the enablement gate on
   * the "Focus Parent Session" and "Take Snapshot on Parent" toolbar
   * buttons.
   */
  private updateHasLiveParentContextKey(activeSession: vscode.DebugSession | undefined): void {
    let hasLiveParent = false;
    if (activeSession != null && activeSession.type === this.viewerDebugType) {
      const viewer = this.viewers.get(activeSession.id);
      if (viewer != null && this.liveParents.has(viewer.parentSessionId)) {
        hasLiveParent = true;
      }
    }
    void vscode.commands.executeCommand(
      'setContext',
      `${this.commandPrefix}:tintype:active-viewer-has-live-parent`,
      hasLiveParent,
    );
  }

  /**
   * Set the ``tintype:active-viewer-backs-live-injection`` context key
   * to ``true`` iff the active session is a tintype viewer whose parent
   * is BOTH live AND currently accumulating snapshots into a working
   * ``.pytb``. Gates the Save button so it hides for:
   *   * standalone viewers (no parent at all),
   *   * viewers whose parent session ended, and
   *   * viewers whose working file was already sealed via
   *     :meth:`finalizeOnParent` — ``parent`` may still be debugging
   *     but the tintype injection state has been dropped, so there's
   *     nothing new to finalize.
   *
   * Stricter than :meth:`updateHasLiveParentContextKey`, which gates
   * "Take Snapshot on Parent" — taking a snapshot can transparently
   * re-initialize against a fresh working file, so a finalized file
   * is not a blocker there.
   */
  private updateBacksLiveInjectionContextKey(activeSession: vscode.DebugSession | undefined): void {
    let backsLiveInjection = false;
    if (activeSession != null && activeSession.type === this.viewerDebugType) {
      const viewer = this.viewers.get(activeSession.id);
      if (
        viewer != null &&
        this.liveParents.has(viewer.parentSessionId) &&
        this.injectedSessions.has(viewer.parentSessionId) &&
        // Must match the viewer's working file against the parent's
        // current working file — see ``getViewerSummaries`` for the
        // full save-after-finalize race this guard prevents.
        this.parentInjectionState.get(viewer.parentSessionId)?.pytbPath === viewer.pytbPath
      ) {
        backsLiveInjection = true;
      }
    }
    void vscode.commands.executeCommand(
      'setContext',
      `${this.commandPrefix}:tintype:active-viewer-backs-live-injection`,
      backsLiveInjection,
    );
  }

  /**
   * Resolve the active tintype viewer, or return ``null`` when the
   * currently focused session isn't one of ours. Used by the three
   * viewer-scoped toolbar commands.
   */
  private activeViewer(): ViewerState | null {
    const active = vscode.debug.activeDebugSession;
    if (active == null || active.type !== this.viewerDebugType) {
      return null;
    }
    return this.viewers.get(active.id) ?? null;
  }

  /**
   * Jump the cursor to the last snapshot in the active viewer. No-op
   * if the active session isn't a tintype viewer we know about or if
   * the viewer has no snapshots yet.
   */
  public async jumpToLastSnapshot(): Promise<void> {
    const viewer = this.activeViewer();
    if (viewer == null) {
      return;
    }
    const list = viewer.lastList;
    const totalSnapshots = list?.snapshots.length ?? 0;
    if (list == null || totalSnapshots === 0) {
      return;
    }
    const lastIndex = totalSnapshots - 1;

    try {
      await this.jumpToSnapshot(viewer.viewerId, lastIndex);
      // Success intentionally unlogged; see jumpToSnapshot for the
      // "aggregate, don't per-event" rationale. The Refresh-icon
      // button's contribution to engagement folds into the session's
      // aggregate navigation count.
    } catch (e) {
      // ``jumpToSnapshot`` swallows its own errors and logs its own
      // navigation telemetry event; we only end up
      // here if something upstream (e.g. the active-viewer
      // resolution) threw. Emit a scoped error event so the
      // Refresh-icon failure mode is still distinguishable on
      // dashboards via its own errorCode, even though the success
      // path is unlogged.
      this.telemetry.logError(
        viewer.childSession,
        e as Error,
        'snapshotNavigate',
        'jumpToLastSnapshotError',
        {
          trigger: 'jumpToLastButton',
          totalSnapshots,
          parentSessionId: viewer.parentSessionId || undefined,
          launchToken: viewer.launchToken,
        },
      );
      throw e;
    }
  }

  /**
   * Fire the existing ``takeSnapshot`` flow on the viewer's parent so
   * the user can capture a new snapshot without having to click back
   * over to the parent's CALL STACK entry first. Defensive early
   * return when the parent has terminated — races between the
   * enablement context key and user clicks are possible.
   */
  public async takeSnapshotOnParent(): Promise<void> {
    const viewer = this.activeViewer();
    if (viewer == null) {
      return;
    }
    const parent = this.liveParents.get(viewer.parentSessionId);
    if (parent == null) {
      return;
    }
    await this.takeSnapshotInternal(parent, 'takeSnapshotOnParent');
  }

  /**
   * Seal the active viewer's working ``.pytb`` file by evaluating
   * ``tintype.vscode.finalize()`` on the parent session. Prompts the user for
   * the destination path and surfaces a modal warning so the
   * "future snapshots go to a new file" contract doesn't surprise
   * them.
   *
   * No-op when:
   *   * The active session isn't a tintype viewer we own, or
   *   * The viewer's parent session has terminated (the evaluate
   *     target is gone, so we can't finalize).
   *
   * After a successful finalize we drop the per-parent injection
   * cache so the next ``takeSnapshot`` click re-runs
   * ``initializeSnapshotting`` against a fresh working file — tintype
   * requires re-initialization once ``finalize()`` has been called.
   */
  public async finalizeOnParent(
    viewerId?: string,
    trigger: 'saveButton' | 'finalizeCommand' = 'finalizeCommand',
  ): Promise<void> {
    const viewer = viewerId != null ? (this.viewers.get(viewerId) ?? null) : this.activeViewer();
    if (viewer == null) {
      return;
    }
    const parent = this.liveParents.get(viewer.parentSessionId);
    if (parent == null) {
      void vscode.window.showErrorMessage(
        'Cannot save: the debug session that captured this snapshot has ended.',
      );
      return;
    }
    // The context key that gates the Command Palette entry
    // (``active-viewer-backs-live-injection``) is updated eagerly on
    // activeDebugSession / start / terminate events, but races are
    // possible: a previous ``finalize()`` could have cleared
    // ``injectedSessions`` for this parent between the user opening
    // the palette and confirming the Save dialog. Re-check here so
    // the command surfaces a friendly message instead of issuing an
    // ``evaluate(__import__('tintype').finalize(...))`` against a
    // parent whose working file is already sealed (which would come
    // back as an opaque Python error in the debug console).
    if (!this.injectedSessions.has(parent.id)) {
      void vscode.window.showInformationMessage(
        'Cannot save: this snapshot file has already been saved. Take a new ' +
          'snapshot on the parent session to begin a fresh working file.',
      );
      return;
    }
    // Per-viewer guard: the parent may be mid-injection on a fresh
    // working file, but *this* viewer was attached to a previous one
    // that's already been sealed. Without this check, clicking Save
    // on a stale viewer would finalize the parent's *current* working
    // file into the user-chosen destination path, which is almost
    // never what the user intended — they're looking at the old file
    // and thought they were saving that.
    //
    // The ``getViewerSummaries`` / ``updateBacksLiveInjectionContextKey``
    // gates above should hide the Save button on stale viewers, but a
    // stale context-key race (or a palette command that predates the
    // gate) can still land us here. Bail with the same message the
    // ``injectedSessions``-miss path uses so the error UX stays
    // consistent.
    const currentInjection = this.parentInjectionState.get(parent.id);
    if (currentInjection == null || currentInjection.pytbPath !== viewer.pytbPath) {
      void vscode.window.showInformationMessage(
        'Cannot save: this snapshot file has already been saved. Take a new ' +
          'snapshot on the parent session to begin a fresh working file.',
      );
      return;
    }

    const savePath = await vscode.window.showSaveDialog({
      defaultUri: this.buildFinalizedSaveUri(viewer.pytbPath),
      filters: {'Tintype Snapshots': ['pytb']},
      title: 'Save Tintype Snapshot File',
    });
    if (savePath == null) {
      // User dismissed the save dialog.
      this.telemetry.logEvent(viewer.childSession, 'saveSnapshotFile', {
        trigger,
        destinationPath: '',
        userCancelled: true,
        parentSessionId: viewer.parentSessionId || undefined,
        launchToken: viewer.launchToken,
      });
      return;
    }

    // Modal confirmation — make sure users understand the sticky
    // side-effect ("any further snapshots are written to a new file")
    // before we fire an irreversible ``finalize()`` call on the
    // debuggee. If the target path already exists the message is
    // strengthened to call out the overwrite explicitly, because the
    // OS-level save-dialog overwrite prompt that users see on desktop
    // is not guaranteed on remote / web VS Code hosts.
    const willOverwrite = await this.targetFileExists(savePath);
    const confirmTitle = willOverwrite
      ? 'Overwrite existing snapshot file?'
      : 'Save this snapshot file?';
    const confirmDetail = willOverwrite
      ? `A file already exists at "${savePath.fsPath}" and will be OVERWRITTEN. ` +
        'The existing contents cannot be recovered. Any further snapshots ' +
        'taken from this debug session will be written to a new file.'
      : `The file will be sealed and written to "${savePath.fsPath}". ` +
        'Any further snapshots taken from this debug session will be ' +
        'written to a new file.';
    const confirmAction = willOverwrite ? 'Overwrite' : 'Save';
    const confirmation = await vscode.window.showWarningMessage(
      confirmTitle,
      {modal: true, detail: confirmDetail},
      confirmAction,
    );
    if (confirmation !== confirmAction) {
      this.telemetry.logEvent(viewer.childSession, 'saveSnapshotFile', {
        trigger,
        destinationPath: truncate(savePath.fsPath),
        userCancelled: true,
        parentSessionId: viewer.parentSessionId || undefined,
        launchToken: viewer.launchToken,
      });
      return;
    }

    try {
      // Build the Python string literal via ``JSON.stringify`` so
      // embedded newlines / carriage returns / tabs from a save-dialog
      // path don't break out of the literal and inject a second
      // Python statement (the same hardening applied to
      // ``initializeSnapshotting``).
      const pathLiteral = JSON.stringify(savePath.fsPath);
      await parent.customRequest('evaluate', {
        expression: `__import__('tintype.vscode', fromlist=['finalize']).finalize(${pathLiteral})`,
        context: 'repl',
      });
      // Invalidate the cached working-file path so the next
      // take-snapshot click re-initializes cleanly — the working file
      // this viewer was reading is now sealed.
      this.parentInjectionState.delete(parent.id);
      this.injectedSessions.delete(parent.id);
      // The working file this viewer backs is now sealed, so the
      // Save button should hide until a fresh injection begins.
      this.updateBacksLiveInjectionContextKey(vscode.debug.activeDebugSession);
      // Tree contextValue is derived from ``backsLiveInjection`` — rebuild
      // the affected item so the inline Save icon disappears from its row.
      this.treeProvider?.refreshAll();
      void vscode.window.showInformationMessage(`Saved snapshot file to ${savePath.fsPath}`);

      const snapshotCount = viewer.lastList?.snapshots.length ?? 0;
      this.telemetry.logEvent(viewer.childSession, 'saveSnapshotFile', {
        trigger,
        destinationPath: truncate(savePath.fsPath),
        snapshotCount,
        userCancelled: false,
        parentSessionId: viewer.parentSessionId || undefined,
        launchToken: viewer.launchToken,
      });
    } catch (e) {
      this.telemetry.logError(
        viewer.childSession,
        e as Error,
        'saveSnapshotFile',
        'saveSnapshotFileError',
        {
          trigger,
          destinationPath: truncate(savePath.fsPath),
          userCancelled: false,
          parentSessionId: viewer.parentSessionId || undefined,
          launchToken: viewer.launchToken,
        },
      );
      void vscode.window.showErrorMessage(`Failed to save snapshot file: ${(e as Error).message}`);
    }
  }

  /**
   * Propose a default save path for the save dialog. We drop into the
   * working file's directory and append ``_finalized`` to the stem so
   * the freshly-sealed file doesn't overwrite the working file's name
   * (tintype treats those as separate paths — the working file stays
   * valid until ``finalize()`` actually runs).
   *
   * Returns ``undefined`` when the viewer's working path is missing or
   * malformed; the save dialog falls back to the user's home directory
   * in that case, which is a reasonable fallback.
   */
  private buildFinalizedSaveUri(workingPytbPath: string): vscode.Uri | undefined {
    if (!workingPytbPath) {
      return undefined;
    }
    try {
      const dir = path.dirname(workingPytbPath);
      const base = path.basename(workingPytbPath);
      const stem = base.endsWith('.pytb') ? base.slice(0, -'.pytb'.length) : base;
      const defaultName = `${stem || 'snapshot'}_finalized.pytb`;
      return vscode.Uri.file(path.join(dir, defaultName));
    } catch {
      return undefined;
    }
  }

  /**
   * Probe the filesystem for the user-chosen save destination so the
   * confirmation modal can call out an overwrite explicitly. VS Code
   * desktop's native save dialog shows its own "file exists, replace?"
   * prompt, but the VS Code web / remote hosts can't always delegate
   * that to the OS, and silently clobbering an existing snapshot file
   * is an especially bad failure mode because ``tintype.vscode.finalize()``
   * is irreversible.
   *
   * ``workspace.fs.stat`` throws when the path doesn't exist; we
   * interpret any thrown error as "not present" so permission errors,
   * stale FS stats, etc. fall through to the non-overwrite message
   * rather than blocking the save with a false-positive warning.
   */
  private async targetFileExists(uri: vscode.Uri): Promise<boolean> {
    try {
      await vscode.workspace.fs.stat(uri);
      return true;
    } catch {
      return false;
    }
  }

  /**
   * Refresh the sidebar when a viewer emits ``stopped`` (launch, jump,
   * or step-back). Dispatches to the viewer-scoped refresh so a stop
   * in one viewer never clobbers another viewer's state.
   */
  public handleStoppedSession(session: vscode.DebugSession): void {
    if (!this.viewers.has(session.id)) {
      return;
    }
    void this.refreshSnapshotList(session.id);
  }

  /**
   * Capture automatically because the live parent ``session`` just hit a
   * ``stopped`` event — a breakpoint, an exception, or the end of a
   * step (DAP reports all of them as ``stopped``, so this single hook
   * covers "stopped or stepped").
   *
   * Opt-in: no-op unless the host supplied a
   * :type:`AutoSnapshotConfigResolver` and the user enabled the setting.
   *
   * Automatic capture is a *recording* activity and never opens UI. In
   * particular it never launches a viewer debug session: the viewer
   * reports ``stopped`` and the tintype adapter never sends
   * ``continued``, so VS Code treats a live viewer as permanently
   * stopped and moves focus onto it whenever the program under test
   * resumes for a perceptible interval — stepping over a
   * ``time.sleep(1)`` is enough to make that visible on every step. The
   * viewer is opened only by an explicit user action (the camera button
   * or opening a ``.pytb``), where a focus change is expected.
   *
   * The capture runs on the paused thread, so the debuggee cannot
   * service the next step request until it finishes. Three guards keep
   * that off the user's critical path:
   *
   *   * **Settle delay.** The capture is scheduled ``settleMs`` out and
   *     cancelled by the next stop or by any resume. While the user is
   *     stepping, no capture is ever started, so there is nothing for
   *     their next step to queue behind. This is the guard that actually
   *     fixes stepping latency; the other two only bound the damage.
   *   * **Deadline.** ``timeoutMs`` is passed down to
   *     ``tintype.vscode.capture()``, so the debuggee cancels its own
   *     stack walk on expiry and returns a truncated snapshot. It covers
   *     the residual case: stepping resumed just as a capture began. The
   *     extension cannot cancel an in-flight DAP request, so bounding
   *     only its own wait would not have stopped the debuggee working.
   *   * **Overlap.** While a capture is in flight for a session, later
   *     stops on it are dropped, so a debuggee slower than the settle
   *     delay can't accumulate queued captures. This also covers the
   *     first stop of a session, which additionally pays for runtime
   *     injection and spawning the viewer.
   *
   * The returned promise resolves once the scheduled capture completes,
   * or immediately if it is cancelled or skipped.
   */
  public handleParentStopped(session: vscode.DebugSession): Promise<void> {
    if (this.resolveAutoSnapshotConfig == null || !this.supportsLiveSession(session)) {
      return Promise.resolve();
    }
    if (this.autoSnapshotDisabled.has(session.id)) {
      return Promise.resolve();
    }
    const {enabled, timeoutMs, settleMs} = this.resolveAutoSnapshotConfig(session);
    // A new stop supersedes whatever the previous one scheduled — this is
    // what makes held-down stepping produce no captures at all.
    this.cancelPendingAutoSnapshot(session);
    if (!enabled) {
      return Promise.resolve();
    }

    return new Promise<void>(resolve => {
      const timer = setTimeout(() => {
        this.pendingAutoSnapshots.delete(session.id);
        void this.runAutoSnapshot(session, timeoutMs).then(resolve, resolve);
      }, settleMs);
      this.pendingAutoSnapshots.set(session.id, {timer, resolve});
    });
  }

  /**
   * Drop a scheduled capture that has not fired yet. Called when the
   * session resumes — the debuggee is about to be running, and an
   * ``evaluate`` against a running thread fails rather than capturing.
   *
   * Safe to call for sessions with nothing pending.
   */
  public cancelPendingAutoSnapshot(session: vscode.DebugSession): void {
    const pending = this.pendingAutoSnapshots.get(session.id);
    if (pending == null) {
      return;
    }
    clearTimeout(pending.timer);
    this.pendingAutoSnapshots.delete(session.id);
    pending.resolve();
  }

  /** Cancel every scheduled capture. Called when the host disposes. */
  public dispose(): void {
    for (const sessionId of Array.from(this.pendingAutoSnapshots.keys())) {
      const pending = this.pendingAutoSnapshots.get(sessionId);
      if (pending != null) {
        clearTimeout(pending.timer);
        pending.resolve();
      }
    }
    this.pendingAutoSnapshots.clear();
  }

  /**
   * Run a settled automatic capture, and give up on this session for
   * good if it fails.
   *
   * The latch matters more than it looks. A failing
   * ``ensureSnapshotting`` leaves ``injectedSessions`` unpopulated and
   * clears its own in-flight promise, so without this every subsequent
   * stop would re-run the entire injection sequence — the runtime probe,
   * several sequential ``evaluate`` round-trips and a ``startDebugging``
   * — against the paused thread, which is the slowest path in the
   * feature. The usual cause (tintype missing from the debuggee's
   * interpreter) cannot resolve itself mid-session, so retrying only
   * spends the user's stepping latency to fail again.
   */
  private async runAutoSnapshot(session: vscode.DebugSession, timeoutMs: number): Promise<void> {
    if (this.autoSnapshotInFlight.has(session.id) || this.autoSnapshotDisabled.has(session.id)) {
      return;
    }
    this.autoSnapshotInFlight.add(session.id);
    let captured: boolean;
    try {
      captured = await this.takeSnapshotInternal(session, 'autoSnapshot', timeoutMs);
    } finally {
      this.autoSnapshotInFlight.delete(session.id);
    }
    if (captured) {
      return;
    }

    this.autoSnapshotDisabled.add(session.id);
    const reason = this.lastAutoSnapshotError?.message ?? 'unknown error';
    this.lastAutoSnapshotError = undefined;
    this.telemetry.logEvent(session, 'takeSnapshot', {
      variant: 'autoSnapshot',
      autoSnapshotDisabled: true,
      parentSessionType: session.type,
    });
    // Exactly one message per session, and only because a feature the
    // user switched on has just switched itself off. The passive-event
    // no-toast rule targets routine and repeating notifications; this is
    // a one-shot terminal state change the user has to act on.
    void vscode.window.showWarningMessage(
      `Automatic Tintype snapshots are disabled for this debug session: ${reason}. ` +
        'Use Take Tintype Snapshot to capture manually.',
    );
  }

  /**
   * Walk live viewers, group by parent name, and assign ``#2`` / ``#3``
   * suffixes in insertion order for groups with ≥2 members. Single
   * members get ``null``. Triggered on every add and remove so a
   * departing viewer can free up its group's disambiguator.
   */
  private recomputeDisambiguators(): void {
    const groups: Map<string, ViewerState[]> = new Map();
    for (const v of this.viewers.values()) {
      const bucket = groups.get(v.parentSessionName);
      if (bucket == null) {
        groups.set(v.parentSessionName, [v]);
      } else {
        bucket.push(v);
      }
    }
    for (const bucket of groups.values()) {
      if (bucket.length === 1) {
        bucket[0].disambiguator = null;
      } else {
        bucket.forEach((v, idx) => {
          v.disambiguator = idx === 0 ? null : `#${idx + 1}`;
        });
      }
    }
  }

  private updateContextKeys(): void {
    const count = this.viewers.size;
    void vscode.commands.executeCommand(
      'setContext',
      `${this.commandPrefix}:snapshot:viewer-active`,
      count > 0,
    );
    void vscode.commands.executeCommand(
      'setContext',
      `${this.commandPrefix}:snapshot:viewer-count`,
      count,
    );
  }
}
