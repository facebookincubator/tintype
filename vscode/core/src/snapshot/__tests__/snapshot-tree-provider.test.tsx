/**
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * This source code is licensed under the MIT license found in the
 * LICENSE file in the root directory of this source tree.
 *
 * @format
 */

import {SnapshotItem, SnapshotTreeProvider} from '../snapshot-tree-provider';
import type {SnapshotListResponse, TreeElement} from '../snapshot-tree-provider';
import type {ViewerSummary} from '../snapshot-provider';

jest.mock('vscode', () => ({
  TreeItemCollapsibleState: {None: 0, Expanded: 2},
  TreeItem: class {
    label: string;
    collapsibleState: number;
    description?: string;
    iconPath?: unknown;
    command?: unknown;
    contextValue?: string;
    constructor(label: string, collapsibleState: number) {
      this.label = label;
      this.collapsibleState = collapsibleState;
    }
  },
  ThemeIcon: class {
    id: string;
    constructor(id: string) {
      this.id = id;
    }
  },
  EventEmitter: class {
    private listeners: Array<(arg: unknown) => void> = [];
    event = (fn: (arg: unknown) => void) => {
      this.listeners.push(fn);
    };
    fire(arg?: unknown) {
      this.listeners.forEach(fn => fn(arg));
    }
    dispose() {}
  },
}));

const JUMP_COMMAND = 'test-host.debugger.jump-to-snapshot';

function makeList(currentIndex: number, count: number): SnapshotListResponse {
  const snapshots = [];
  for (let i = 0; i < count; i++) {
    snapshots.push({index: i, timestampUs: (i + 1) * 1_000_000});
  }
  return {currentIndex, snapshots};
}

function makeViewer(overrides: Partial<ViewerSummary> = {}): ViewerSummary {
  return {
    viewerId: 'v1',
    parentSessionName: 'ParentName',
    disambiguator: null,
    parentAlive: true,
    backsLiveInjection: false,
    lastList: null,
    ...overrides,
  };
}

describe('SnapshotItem', () => {
  it('formats label and description from snapshot data', () => {
    const item = new SnapshotItem(2, 1_000_000, false, JUMP_COMMAND, 'v1');
    expect(item.label).toBe('Snapshot #2');
    expect(item.description).toMatch(/\d{2}:\d{2}:\d{2}\.\d{3}/);
  });

  it('uses circle-filled icon for active snapshot', () => {
    const item = new SnapshotItem(0, 1_000_000, true, JUMP_COMMAND, 'v1');
    expect((item.iconPath as {id: string}).id).toBe('circle-filled');
  });

  it('uses circle-outline icon for inactive snapshot', () => {
    const item = new SnapshotItem(0, 1_000_000, false, JUMP_COMMAND, 'v1');
    expect((item.iconPath as {id: string}).id).toBe('circle-outline');
  });

  it('passes [viewerId, index] to the jump command', () => {
    const item = new SnapshotItem(5, 1_000_000, false, JUMP_COMMAND, 'viewer-42');
    expect(item.command).toEqual({
      command: JUMP_COMMAND,
      title: 'Jump to Snapshot',
      arguments: ['viewer-42', 5],
    });
  });

  it('falls back to [index] when constructed without a viewerId (legacy shape)', () => {
    const item = new SnapshotItem(5, 1_000_000, false, JUMP_COMMAND);
    expect(item.command).toEqual({
      command: JUMP_COMMAND,
      title: 'Jump to Snapshot',
      arguments: [5],
    });
  });
});

describe('SnapshotTreeProvider', () => {
  let provider: SnapshotTreeProvider;
  let summaries: ViewerSummary[];

  beforeEach(() => {
    summaries = [];
    provider = new SnapshotTreeProvider(JUMP_COMMAND);
    provider.bind(() => summaries);
  });

  it('returns empty list when no viewers are registered', () => {
    expect(provider.getChildren()).toEqual([]);
  });

  it('renders one viewer element per registered viewer', () => {
    summaries = [
      makeViewer({viewerId: 'a', parentSessionName: 'Alpha'}),
      makeViewer({viewerId: 'b', parentSessionName: 'Bravo'}),
    ];
    const children = provider.getChildren();
    expect(children.map(c => (c as Extract<TreeElement, {kind: 'viewer'}>).viewerId)).toEqual([
      'a',
      'b',
    ]);
    expect(children.every(c => c.kind === 'viewer')).toBe(true);
  });

  it('renders snapshot children under a viewer element', () => {
    summaries = [makeViewer({viewerId: 'a', lastList: makeList(1, 3)})];
    const grandchildren = provider.getChildren({kind: 'viewer', viewerId: 'a'});
    expect(grandchildren).toHaveLength(3);
    const snaps = grandchildren as Array<Extract<TreeElement, {kind: 'snapshot'}>>;
    expect(snaps[1].isActive).toBe(true);
    expect(snaps[0].isActive).toBe(false);
    expect(snaps[2].isActive).toBe(false);
    expect(snaps.every(s => s.viewerId === 'a')).toBe(true);
  });

  it('returns empty children for snapshot leaves', () => {
    expect(
      provider.getChildren({
        kind: 'snapshot',
        viewerId: 'a',
        index: 0,
        timestampUs: 1,
        isActive: false,
      }),
    ).toEqual([]);
  });

  it('builds a viewer item with the undecorated parent name when unique', () => {
    summaries = [makeViewer({viewerId: 'a', parentSessionName: 'Alpha', lastList: makeList(0, 1)})];
    const item = provider.getTreeItem({kind: 'viewer', viewerId: 'a'});
    expect(item.label).toBe('Alpha');
    expect(item.contextValue).toBe('tintype-viewer');
  });

  it('appends the disambiguator to the viewer label when supplied', () => {
    summaries = [
      makeViewer({
        viewerId: 'a',
        parentSessionName: 'Alpha',
        disambiguator: '#2',
      }),
    ];
    const item = provider.getTreeItem({kind: 'viewer', viewerId: 'a'});
    expect(item.label).toBe('Alpha #2');
  });

  it('renders a plain label when parent is dead (no (disconnected) suffix)', () => {
    summaries = [
      makeViewer({
        viewerId: 'a',
        parentSessionName: 'Alpha',
        parentAlive: false,
      }),
    ];
    const item = provider.getTreeItem({kind: 'viewer', viewerId: 'a'});
    expect(item.label).toBe('Alpha');
    expect(item.contextValue).toBe('tintype-viewer-disconnected');
  });

  it('appends (live) when the viewer backs a live working file', () => {
    summaries = [
      makeViewer({
        viewerId: 'a',
        parentSessionName: 'my_trace.pytb',
        parentAlive: true,
        backsLiveInjection: true,
      }),
    ];
    const item = provider.getTreeItem({kind: 'viewer', viewerId: 'a'});
    expect(item.label).toBe('my_trace.pytb (live)');
  });

  it('does not append (live) when the file has been finalized (parent alive but not injecting)', () => {
    summaries = [
      makeViewer({
        viewerId: 'a',
        parentSessionName: 'my_trace.pytb',
        parentAlive: true,
        backsLiveInjection: false,
      }),
    ];
    const item = provider.getTreeItem({kind: 'viewer', viewerId: 'a'});
    expect(item.label).toBe('my_trace.pytb');
  });

  it('sets contextValue to tintype-viewer-live-injection when the parent is alive AND backing a working file', () => {
    summaries = [
      makeViewer({
        viewerId: 'a',
        parentSessionName: 'Alpha',
        parentAlive: true,
        backsLiveInjection: true,
      }),
    ];
    const item = provider.getTreeItem({kind: 'viewer', viewerId: 'a'});
    expect(item.contextValue).toBe('tintype-viewer-live-injection');
  });

  it('sets contextValue to tintype-viewer when the parent is alive but the file is already finalized', () => {
    summaries = [
      makeViewer({
        viewerId: 'a',
        parentSessionName: 'Alpha',
        parentAlive: true,
        backsLiveInjection: false,
      }),
    ];
    const item = provider.getTreeItem({kind: 'viewer', viewerId: 'a'});
    expect(item.contextValue).toBe('tintype-viewer');
  });

  it('formats the viewer description with count + current cursor', () => {
    summaries = [
      makeViewer({
        viewerId: 'a',
        parentSessionName: 'Alpha',
        lastList: makeList(1, 3),
      }),
    ];
    const item = provider.getTreeItem({kind: 'viewer', viewerId: 'a'});
    expect(item.description).toMatch(/^3 snapshots · current #1 @ \d{2}:\d{2}:\d{2}\.\d{3}$/);
  });

  it('singularizes snapshot count when exactly 1', () => {
    summaries = [
      makeViewer({
        viewerId: 'a',
        parentSessionName: 'Alpha',
        lastList: makeList(0, 1),
      }),
    ];
    const item = provider.getTreeItem({kind: 'viewer', viewerId: 'a'});
    expect(item.description).toMatch(/^1 snapshot · current #0 @ /);
  });

  it('passes [viewerId, index] to the snapshot row command', () => {
    summaries = [
      makeViewer({
        viewerId: 'a',
        parentSessionName: 'Alpha',
        lastList: makeList(0, 2),
      }),
    ];
    const children = provider.getChildren({kind: 'viewer', viewerId: 'a'});
    const row = provider.getTreeItem(children[1]);
    expect(row.command).toEqual({
      command: JUMP_COMMAND,
      title: 'Jump to Snapshot',
      arguments: ['a', 1],
    });
  });

  it('refreshViewer fires the SAME element reference returned by getChildren', () => {
    // Stable reference identity is what VS Code uses to target scoped
    // re-renders. If getChildren hands back a fresh object each call and
    // refreshViewer fires a different fresh object, VS Code sees two
    // unrelated elements and the scoped refresh silently no-ops — which
    // previously caused the second viewer's sidebar to stay empty while
    // step/reverse-step still worked.
    summaries = [
      makeViewer({viewerId: 'a', parentSessionName: 'Alpha'}),
      makeViewer({viewerId: 'b', parentSessionName: 'Bravo'}),
    ];
    const [, viewerB] = provider.getChildren();

    const received: unknown[] = [];
    provider.onDidChangeTreeData(arg => received.push(arg));

    provider.refreshViewer('b');
    expect(received).toHaveLength(1);
    // Reference equality, not just structural equality.
    expect(received[0]).toBe(viewerB);
  });

  it('refreshViewer for an unknown viewer falls back to a root refresh', () => {
    const received: unknown[] = [];
    provider.onDidChangeTreeData(arg => received.push(arg));

    provider.refreshViewer('not-in-cache');
    expect(received).toEqual([undefined]);
  });

  it('getChildren returns a stable reference for the same viewerId across calls', () => {
    summaries = [makeViewer({viewerId: 'a'})];
    const first = provider.getChildren();
    const second = provider.getChildren();
    expect(first[0]).toBe(second[0]);
  });

  it('builds a stable TreeItem.id on viewer nodes for VS Code identity tracking', () => {
    summaries = [makeViewer({viewerId: 'viewer-xyz', parentSessionName: 'X'})];
    const item = provider.getTreeItem({kind: 'viewer', viewerId: 'viewer-xyz'});
    expect((item as {id?: string}).id).toBe('viewer:viewer-xyz');
  });

  it('fires an unscoped change event on refreshAll', () => {
    const calls: unknown[] = [];
    provider.onDidChangeTreeData(arg => calls.push(arg));

    provider.refreshAll();
    expect(calls).toHaveLength(1);
    expect(calls[0]).toBeUndefined();
  });
});
