/**
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * This source code is licensed under the MIT license found in the
 * LICENSE file in the root directory of this source tree.
 *
 * @format
 */

import vscode from 'vscode';

import type {ViewerSummary} from './snapshot-provider';

/**
 * One row in the Tintype Snapshots sidebar.
 *
 * Shape matches the body of the ``tintypeSnapshotList`` custom DAP
 * response emitted by :class:`tintype.dap.session.SnapshotDebugSession`.
 * The server may also emit rows with ``corrupt: true`` (and no
 * ``timestampUs``) for indices whose payload couldn't be decoded; the
 * tree currently ignores those rows for rendering but the
 * snapshot-provider still inspects them off the wire for telemetry.
 */
export type SnapshotData = {
  index: number;
  timestampUs: number;
};

export type SnapshotListResponse = {
  currentIndex: number;
  snapshots: SnapshotData[];
};

const ACTIVE_ICON = 'circle-filled';
const INACTIVE_ICON = 'circle-outline';

/**
 * Discriminated union of tree elements. ``kind: 'viewer'`` nodes are
 * top-level groups; ``kind: 'snapshot'`` nodes are their children.
 *
 * Kept as plain objects (not classes) so the tree can reconstruct them
 * freely from the latest ``ViewerSummary`` without worrying about stale
 * class instances lingering in the view.
 */
export type TreeElement =
  | {kind: 'viewer'; viewerId: string}
  | {
      kind: 'snapshot';
      viewerId: string;
      index: number;
      timestampUs: number;
      isActive: boolean;
    };

/**
 * Historically the tree only rendered one viewer's worth of snapshots
 * as a flat list, so ``SnapshotItem`` was both the element type AND a
 * ``TreeItem``. The class is kept (and re-exported) for test ergonomics
 * + backwards compat with any callers that still construct rows by
 * hand, but the tree itself now lazily builds ``TreeItem``s in
 * ``getTreeItem``.
 */
export class SnapshotItem extends vscode.TreeItem {
  constructor(
    public readonly index: number,
    public readonly timestampUs: number,
    public readonly isActive: boolean,
    jumpCommand: string,
    viewerId = '',
  ) {
    super(`Snapshot #${index}`, vscode.TreeItemCollapsibleState.None);

    this.description = formatTimestamp(timestampUs);
    this.iconPath = new vscode.ThemeIcon(isActive ? ACTIVE_ICON : INACTIVE_ICON);

    // Pre-``viewerId`` callers (tests without a viewerId, old extensions)
    // get the legacy 1-arg shape so they stay green.
    this.command = {
      command: jumpCommand,
      title: 'Jump to Snapshot',
      arguments: viewerId === '' ? [index] : [viewerId, index],
    };
  }
}

function formatTimestamp(timestampUs: number): string {
  // Snapshot timestamps are microseconds since epoch. JS Date takes
  // milliseconds so divide by 1000.
  const date = new Date(timestampUs / 1000);
  const hours = String(date.getHours()).padStart(2, '0');
  const minutes = String(date.getMinutes()).padStart(2, '0');
  const seconds = String(date.getSeconds()).padStart(2, '0');
  const millis = String(date.getMilliseconds()).padStart(3, '0');
  return `${hours}:${minutes}:${seconds}.${millis}`;
}

/**
 * Data source the tree provider reads from. Kept as a narrow callback
 * so the tree doesn't depend directly on ``SnapshotProvider`` — simpler
 * to unit-test the two sides in isolation.
 */
export type ViewerSummarySource = () => ViewerSummary[];

/**
 * Two-level tree view: viewer group → snapshot row. Each viewer gets
 * its own group; snapshots live underneath.
 *
 * Scoped refresh: ``refreshViewer(viewerId)`` re-renders only that one
 * group's subtree, preserving sibling expansion state. ``refreshAll``
 * is used for structural changes (viewer added / removed / renumbered).
 */
export class SnapshotTreeProvider implements vscode.TreeDataProvider<TreeElement> {
  private _onDidChangeTreeData = new vscode.EventEmitter<TreeElement | undefined | void>();
  readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

  /**
   * ``undefined`` until the SnapshotProvider calls ``bind`` — before
   * then the tree has no way to fetch viewer state. Tests that only
   * exercise ``SnapshotItem`` formatting don't need to bind.
   */
  private viewerSource: ViewerSummarySource | null = null;

  /**
   * Stable ``TreeElement`` references keyed by viewer id. VS Code's
   * scoped ``_onDidChangeTreeData.fire(element)`` targets an element by
   * **reference identity**, not structural equality — so if we
   * manufactured a fresh ``{kind: 'viewer', viewerId}`` object each time
   * ``getChildren`` runs, a later ``refreshViewer`` fire would miss (VS
   * Code has no mapping from the new object to the one it cached
   * earlier). Caching per-viewer references fixes that: the element we
   * return from ``getChildren`` is the same one we pass to ``fire``.
   */
  private viewerElements: Map<string, Extract<TreeElement, {kind: 'viewer'}>> = new Map();

  constructor(private jumpCommand: string) {}

  /**
   * Attach the data source. Called once by the extension's composition
   * root after both the tree provider and snapshot provider exist.
   */
  bind(source: ViewerSummarySource): void {
    this.viewerSource = source;
    this._onDidChangeTreeData.fire();
  }

  getTreeItem(element: TreeElement): vscode.TreeItem {
    if (element.kind === 'viewer') {
      return this.buildViewerItem(element.viewerId);
    }
    return this.buildSnapshotItem(element);
  }

  getChildren(element?: TreeElement): TreeElement[] {
    if (element == null) {
      const viewers = this.viewerSource?.() ?? [];
      // Drop cached references for viewers that have left the registry
      // so a re-added viewerId doesn't accidentally reuse a stale
      // element instance (mostly a hygiene concern; viewer ids are
      // derived from ``session.id`` which VS Code doesn't reuse).
      const liveIds = new Set(viewers.map(v => v.viewerId));
      for (const id of this.viewerElements.keys()) {
        if (!liveIds.has(id)) {
          this.viewerElements.delete(id);
        }
      }
      return viewers.map(v => {
        let el = this.viewerElements.get(v.viewerId);
        if (el == null) {
          el = {kind: 'viewer' as const, viewerId: v.viewerId};
          this.viewerElements.set(v.viewerId, el);
        }
        return el;
      });
    }
    if (element.kind === 'viewer') {
      const viewers = this.viewerSource?.() ?? [];
      const viewer = viewers.find(v => v.viewerId === element.viewerId);
      if (viewer == null || viewer.lastList == null) {
        return [];
      }
      const currentIndex = viewer.lastList.currentIndex;
      return viewer.lastList.snapshots.map(snap => ({
        kind: 'snapshot' as const,
        viewerId: element.viewerId,
        index: snap.index,
        timestampUs: snap.timestampUs,
        isActive: snap.index === currentIndex,
      }));
    }
    return [];
  }

  /**
   * Fire a scoped change event for one viewer subtree. VS Code
   * re-queries just that node's children instead of tearing down all
   * sibling groups' expansion state.
   */
  refreshViewer(viewerId: string): void {
    const el = this.viewerElements.get(viewerId);
    if (el != null) {
      this._onDidChangeTreeData.fire(el);
      return;
    }
    // Not yet cached — e.g. ``refreshSnapshotList`` completed before
    // VS Code's initial ``getChildren(undefined)`` call populated the
    // cache. Fall back to a root refresh so the tree still picks up the
    // new state instead of silently dropping the event.
    this._onDidChangeTreeData.fire();
  }

  /**
   * Structural refresh — fires for the whole tree. Use for viewer
   * add / remove / disambiguator-rename. Invalidates the cached viewer
   * element map so the next ``getChildren`` call rebuilds fresh entries
   * (VS Code has dropped its side of the identity cache by then anyway).
   */
  refreshAll(): void {
    this.viewerElements.clear();
    this._onDidChangeTreeData.fire();
  }

  dispose(): void {
    this._onDidChangeTreeData.dispose();
  }

  // ---------------------------------------------------------------
  // Item building
  // ---------------------------------------------------------------

  private buildViewerItem(viewerId: string): vscode.TreeItem {
    const viewers = this.viewerSource?.() ?? [];
    const viewer = viewers.find(v => v.viewerId === viewerId);

    if (viewer == null) {
      // Transient state: the tree asked for an item whose viewer was
      // just removed. Render something inert so VS Code doesn't crash.
      const item = new vscode.TreeItem('(viewer removed)', vscode.TreeItemCollapsibleState.None);
      item.contextValue = 'tintype-viewer-missing';
      return item;
    }

    const label = buildViewerLabel(viewer);
    const item = new vscode.TreeItem(label, vscode.TreeItemCollapsibleState.Expanded);
    item.id = `viewer:${viewer.viewerId}`;
    item.description = buildViewerDescription(viewer);
    item.iconPath = new vscode.ThemeIcon('debug-console');
    // contextValue drives viewer-scoped inline / right-click menus:
    //   * ``tintype-viewer-live-injection`` — parent alive AND still
    //     accumulating snapshots into a working ``.pytb``. Gates the
    //     inline Save button so it only shows on viewers where
    //     ``tintype.vscode.finalize()`` can actually do something.
    //   * ``tintype-viewer`` — parent alive but the working file has
    //     already been sealed (or was never injected into — unused
    //     today but reserved for future non-injecting viewer modes).
    //   * ``tintype-viewer-disconnected`` — parent has terminated; the
    //     viewer is still navigable but no parent-scoped action can run.
    if (!viewer.parentAlive) {
      item.contextValue = 'tintype-viewer-disconnected';
    } else if (viewer.backsLiveInjection) {
      item.contextValue = 'tintype-viewer-live-injection';
    } else {
      item.contextValue = 'tintype-viewer';
    }
    return item;
  }

  private buildSnapshotItem(element: Extract<TreeElement, {kind: 'snapshot'}>): vscode.TreeItem {
    return new SnapshotItem(
      element.index,
      element.timestampUs,
      element.isActive,
      this.jumpCommand,
      element.viewerId,
    );
  }
}

/**
 * Compose the viewer group's label:
 *   * ``parentSessionName`` alone when the name is unique;
 *   * ``parentSessionName #K`` when duplicates exist;
 *   * either of the above + ``" (live)"`` when the viewer's parent is
 *     still accumulating into a working ``.pytb``. Default is quiet
 *     (no suffix) — so standalone viewers, finalized-file viewers, and
 *     post-termination viewers all render plainly and ``(live)`` stands
 *     out as the actionable state.
 */
function buildViewerLabel(viewer: ViewerSummary): string {
  let label = viewer.parentSessionName;
  if (viewer.disambiguator != null) {
    label = `${label} ${viewer.disambiguator}`;
  }
  if (viewer.backsLiveInjection) {
    label = `${label} (live)`;
  }
  return label;
}

/**
 * Viewer description: ``N snapshots · current #K @ HH:MM:SS.mmm``.
 * Empty when no ``lastList`` has arrived yet. Frozen once the parent
 * has disconnected (``lastList`` still reflects the last observed
 * state but the group label signals disconnection).
 */
function buildViewerDescription(viewer: ViewerSummary): string {
  const list = viewer.lastList;
  if (list == null) {
    return '';
  }
  const count = list.snapshots.length;
  const countStr = `${count} snapshot${count === 1 ? '' : 's'}`;
  if (count === 0) {
    return countStr;
  }
  const current = list.snapshots.find(s => s.index === list.currentIndex);
  if (current == null) {
    return countStr;
  }
  return `${countStr} · current #${current.index} @ ${formatTimestamp(current.timestampUs)}`;
}
