/**
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * This source code is licensed under the MIT license found in the
 * LICENSE file in the root directory of this source tree.
 *
 * @format
 */

import type vscode from 'vscode';
import cryptoModuleImport from 'crypto';
import {__testOnly_generateLaunchToken, SnapshotProvider} from '../snapshot-provider';
import {SnapshotTreeProvider} from '../snapshot-tree-provider';

const mockExecuteCommand = jest.fn();
const mockShowErrorMessage = jest.fn();
const mockShowInformationMessage = jest.fn();
const mockShowWarningMessage = jest.fn();
const mockShowSaveDialog = jest.fn();
const mockStartDebugging = jest.fn();
const mockFsStat = jest.fn();

type StartDebuggingCall = [workspaceFolder: unknown, config: Record<string, unknown>];

type MockSession = Pick<
  vscode.DebugSession,
  'id' | 'type' | 'customRequest' | 'workspaceFolder' | 'configuration' | 'name'
>;

let mockActiveDebugSession: MockSession | null = null;

jest.mock('vscode', () => ({
  debug: {
    get activeDebugSession() {
      return mockActiveDebugSession;
    },
    startDebugging: async (...args: unknown[]): Promise<boolean> => {
      return (await mockStartDebugging(...args)) as boolean;
    },
  },
  window: {
    showErrorMessage: (...args: unknown[]): unknown => mockShowErrorMessage(...args) as unknown,
    showInformationMessage: (...args: unknown[]): unknown =>
      mockShowInformationMessage(...args) as unknown,
    showWarningMessage: (...args: unknown[]): unknown => mockShowWarningMessage(...args) as unknown,
    showSaveDialog: (...args: unknown[]): unknown => mockShowSaveDialog(...args) as unknown,
  },
  workspace: {
    fs: {
      stat: (uri: unknown): Promise<unknown> => mockFsStat(uri) as Promise<unknown>,
    },
  },
  commands: {
    executeCommand: (...args: unknown[]): unknown => mockExecuteCommand(...args) as unknown,
  },
  Uri: {
    file: (fsPath: string) => ({fsPath, path: fsPath, scheme: 'file'}),
  },
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

jest.mock('child_process', () => ({
  execFile: jest.fn(),
}));

jest.mock('util', () => ({
  promisify: () =>
    jest.fn().mockResolvedValue({
      stdout: '/resolved/runtime/path\n',
    }),
}));

const INJECTION_TYPE = 'test-python';
const VIEWER_TYPE = 'tintype';
const JUMP_COMMAND = 'test-host.debugger.jump-to-snapshot';

function createParentSession(
  overrides: Partial<{id: string; name: string}> = {},
): MockSession & {customRequest: jest.Mock<unknown>} {
  const id = overrides.id ?? 'parent-1';
  const name = overrides.name ?? 'Parent Launch';
  const customRequest = jest
    .fn()
    .mockImplementation((command: string, args?: {expression?: string}) => {
      const argObj = args ?? {};
      if (
        command === 'evaluate' &&
        argObj.expression ===
          "__import__('tintype.vscode', fromlist=['session_info']).session_info()['workingFile']"
      ) {
        return Promise.resolve({result: "'/tmp/tintype_snapshot.pytb'"});
      }
      if (
        command === 'evaluate' &&
        argObj.expression ===
          "__import__('tintype.vscode', fromlist=['session_info']).session_info()['cwd']"
      ) {
        return Promise.resolve({result: "'/home/alice/project'"});
      }
      return Promise.resolve({});
    });
  return {
    id,
    type: INJECTION_TYPE,
    name,
    customRequest,
    workspaceFolder: undefined,
    configuration: {type: INJECTION_TYPE, name, request: 'launch'},
  };
}

function childSessionFromConfig(
  id: string,
  config: Record<string, unknown>,
  customRequest?: jest.Mock<unknown>,
): MockSession {
  const fallback = jest.fn().mockResolvedValue({});
  return {
    id,
    type: VIEWER_TYPE,
    name: (config.name as string) ?? 'Tintype Snapshot',
    customRequest: (customRequest ?? fallback) as unknown as vscode.DebugSession['customRequest'],
    workspaceFolder: undefined,
    configuration: {
      type: VIEWER_TYPE,
      request: 'launch',
      name: (config.name as string) ?? 'Tintype Snapshot',
      ...config,
    },
  };
}

/**
 * Helper: run the full initial ``takeSnapshot`` flow against ``parent``
 * and return the child session that was launched (as reconstructed from
 * the captured startDebugging config). Mirrors the real extension flow
 * where the factory actually creates the session after startDebugging
 * resolves.
 */
async function primeViewer(
  provider: SnapshotProvider,
  parent: MockSession,
  overrides: {childId?: string; childCustomRequest?: jest.Mock<unknown>} = {},
): Promise<MockSession> {
  // Mirror the real VS Code event order: the parent session fires
  // ``onDidStartDebugSession`` before we ever attempt a snapshot. This
  // is what populates the provider's live-parent map so downstream
  // viewer-scoped commands can resolve the parent back to a
  // DebugSession instance.
  provider.handleStartSession(parent as unknown as vscode.DebugSession);

  mockActiveDebugSession = parent;
  await provider.takeSnapshot();

  const calls = mockStartDebugging.mock.calls as unknown as StartDebuggingCall[];
  const lastCall = calls[calls.length - 1];
  const config = lastCall[1];
  const child = childSessionFromConfig(
    overrides.childId ?? 'child-' + parent.id,
    config,
    overrides.childCustomRequest,
  );
  provider.handleStartSession(child as unknown as vscode.DebugSession);
  // Flush the refreshSnapshotList microtask.
  await Promise.resolve();
  await Promise.resolve();
  return child;
}

describe('SnapshotProvider', () => {
  let provider: SnapshotProvider;
  let treeProvider: SnapshotTreeProvider;

  beforeEach(() => {
    jest.clearAllMocks();
    mockActiveDebugSession = null;
    mockStartDebugging.mockResolvedValue(true);
    // Default: the save-dialog target path does NOT exist, so the
    // overwrite-warning branch stays off unless a test opts in.
    mockFsStat.mockRejectedValue(new Error('ENOENT'));
    treeProvider = new SnapshotTreeProvider(JUMP_COMMAND);
    provider = new SnapshotProvider(INJECTION_TYPE, VIEWER_TYPE, 'test-host');
    provider.setTreeProvider(treeProvider);
    treeProvider.bind(() => provider.getViewerSummaries());
  });

  describe('takeSnapshot', () => {
    it('shows error when no active debug session', async () => {
      mockActiveDebugSession = null;

      await provider.takeSnapshot();

      expect(mockShowErrorMessage).toHaveBeenCalledWith(
        'Cannot take snapshot: No active Python debug session found',
      );
    });

    it('shows error when active session is wrong type', async () => {
      const fakeNode: MockSession = {
        id: 's',
        type: 'node',
        name: 'n',
        customRequest: jest.fn() as unknown as vscode.DebugSession['customRequest'],
        workspaceFolder: undefined,
        configuration: {type: 'node', name: 'n', request: 'launch'},
      };
      mockActiveDebugSession = fakeNode;

      await provider.takeSnapshot();

      expect(mockShowErrorMessage).toHaveBeenCalledWith(
        'Cannot take snapshot: No active Python debug session found',
      );
    });

    it('initializes snapshotting and launches a tintype viewer with the decorated session name', async () => {
      const parent = createParentSession({name: 'MyLaunch'});
      mockActiveDebugSession = parent;

      await provider.takeSnapshot();

      // The session name bakes in the parent name so CALL STACK is
      // unambiguous per viewer.
      expect(mockStartDebugging).toHaveBeenCalledWith(
        undefined,
        expect.objectContaining({
          type: VIEWER_TYPE,
          // ``attach`` (not ``launch``) so the stop button renders as
          // the disconnect-plug icon instead of the red-square launch
          // icon.
          request: 'attach',
          name: 'Tintype (MyLaunch)',
          pytbPath: '/tmp/tintype_snapshot.pytb',
          parentSessionId: parent.id,
          parentSessionName: 'MyLaunch',
          cwd: '/home/alice/project',
        }),
      );
      const lastConfig = (mockStartDebugging.mock.calls.slice(-1)[0] as StartDebuggingCall)[1];
      // A non-empty launch token is threaded through so concurrent
      // Take Snapshot clicks don't collide in the pending map.
      expect(typeof lastConfig.tintypeLaunchToken).toBe('string');
      expect((lastConfig.tintypeLaunchToken as string).length).toBeGreaterThan(0);
    });

    it('reuses the existing viewer on subsequent clicks from the same parent', async () => {
      const parent = createParentSession();
      const childCustom = jest.fn().mockResolvedValue({
        currentIndex: 0,
        snapshots: [{index: 0, timestampUs: 1_000_000}],
      });
      const child = await primeViewer(provider, parent, {
        childId: 'child-reuse',
        childCustomRequest: childCustom,
      });
      expect(mockStartDebugging).toHaveBeenCalledTimes(1);
      childCustom.mockClear();

      // Second click on the same parent: snapshot is taken, but NO new
      // viewer is spawned. The existing viewer receives a refresh.
      mockActiveDebugSession = parent;
      await provider.takeSnapshot();

      expect(mockStartDebugging).toHaveBeenCalledTimes(1);
      expect(provider.getViewerSummaries().map(v => v.viewerId)).toEqual([child.id]);
      expect(childCustom).toHaveBeenCalledWith('tintypeSnapshotList');
    });

    it('launches a fresh viewer when the previous one was terminated', async () => {
      const parent = createParentSession();
      const child1 = await primeViewer(provider, parent, {childId: 'child-first'});
      expect(mockStartDebugging).toHaveBeenCalledTimes(1);

      // User terminates the viewer but keeps the parent alive.
      provider.handleTerminateSession(child1 as unknown as vscode.DebugSession);
      expect(provider.getViewerSummaries()).toHaveLength(0);

      // Next click on the still-live parent should launch a fresh viewer.
      mockActiveDebugSession = parent;
      await provider.takeSnapshot();
      expect(mockStartDebugging).toHaveBeenCalledTimes(2);
    });
  });

  describe('multi-viewer registry', () => {
    it('keeps a second viewer from a different parent independent of the first', async () => {
      const parentA = createParentSession({id: 'parent-A', name: 'Alpha'});
      const parentB = createParentSession({id: 'parent-B', name: 'Bravo'});
      const childA = await primeViewer(provider, parentA, {childId: 'child-A'});
      const childB = await primeViewer(provider, parentB, {childId: 'child-B'});

      const summaries = provider.getViewerSummaries();
      expect(summaries).toHaveLength(2);
      expect(summaries.map(v => v.viewerId)).toEqual([childA.id, childB.id]);
      expect(summaries.map(v => v.parentSessionName)).toEqual(['Alpha', 'Bravo']);
      // Single-member groups -> no disambiguator.
      expect(summaries.every(v => v.disambiguator == null)).toBe(true);
    });

    it('assigns #2/#3 when multiple parents share a name and re-numbers on leave', async () => {
      // Three *distinct* parent sessions that happen to share a launch
      // name — the scenario disambiguators exist to solve. (Same parent
      // + multiple clicks now reuses the existing viewer, so we can no
      // longer exercise the re-numbering path from a single parent.)
      const parentA = createParentSession({id: 'parent-A', name: 'Same'});
      const parentB = createParentSession({id: 'parent-B', name: 'Same'});
      const parentC = createParentSession({id: 'parent-C', name: 'Same'});
      const child1 = await primeViewer(provider, parentA, {childId: 'child-1'});
      const child2 = await primeViewer(provider, parentB, {childId: 'child-2'});
      const child3 = await primeViewer(provider, parentC, {childId: 'child-3'});

      let summaries = provider.getViewerSummaries();
      expect(summaries.map(v => v.disambiguator)).toEqual([null, '#2', '#3']);

      // Terminate the middle viewer.
      provider.handleTerminateSession(child2 as unknown as vscode.DebugSession);
      summaries = provider.getViewerSummaries();
      // Still two remain; the first stays null and the survivor renumbers to #2.
      expect(summaries.map(v => v.viewerId)).toEqual([child1.id, child3.id]);
      expect(summaries.map(v => v.disambiguator)).toEqual([null, '#2']);
    });

    it('routes jumpToSnapshot to the correct viewer', async () => {
      const parentA = createParentSession({id: 'parent-A', name: 'Alpha'});
      const parentB = createParentSession({id: 'parent-B', name: 'Bravo'});
      const childACustom = jest.fn().mockResolvedValue({});
      const childBCustom = jest.fn().mockResolvedValue({});
      const childA = await primeViewer(provider, parentA, {
        childId: 'child-A',
        childCustomRequest: childACustom,
      });
      const childB = await primeViewer(provider, parentB, {
        childId: 'child-B',
        childCustomRequest: childBCustom,
      });
      childACustom.mockClear();
      childBCustom.mockClear();

      await provider.jumpToSnapshot(childA.id, 5);
      expect(childACustom).toHaveBeenCalledWith('tintypeJumpToSnapshot', {index: 5});
      expect(childBCustom).not.toHaveBeenCalled();

      await provider.jumpToSnapshot(childB.id, 1);
      expect(childBCustom).toHaveBeenCalledWith('tintypeJumpToSnapshot', {index: 1});
    });

    it('jumpToSnapshot for an unknown viewer id is a no-op', async () => {
      await expect(provider.jumpToSnapshot('nonexistent', 3)).resolves.toBeUndefined();
    });
  });

  describe('handleTerminateSession', () => {
    it('removes only the terminated viewer and updates the viewer-count context key', async () => {
      const parentA = createParentSession({id: 'parent-A', name: 'Alpha'});
      const parentB = createParentSession({id: 'parent-B', name: 'Bravo'});
      const childA = await primeViewer(provider, parentA, {childId: 'child-A'});
      await primeViewer(provider, parentB, {childId: 'child-B'});
      mockExecuteCommand.mockClear();

      provider.handleTerminateSession(childA as unknown as vscode.DebugSession);

      expect(provider.getViewerSummaries().map(v => v.viewerId)).toEqual(['child-B']);
      expect(mockExecuteCommand).toHaveBeenCalledWith(
        'setContext',
        'test-host:snapshot:viewer-count',
        1,
      );
      expect(mockExecuteCommand).toHaveBeenCalledWith(
        'setContext',
        'test-host:snapshot:viewer-active',
        true,
      );
    });

    it('flips parentAlive to false when the parent dies first, leaving the viewer live', async () => {
      const parent = createParentSession();
      const child = await primeViewer(provider, parent);

      provider.handleTerminateSession(parent as unknown as vscode.DebugSession);

      const summaries = provider.getViewerSummaries();
      expect(summaries).toHaveLength(1);
      expect(summaries[0].viewerId).toBe(child.id);
      expect(summaries[0].parentAlive).toBe(false);
    });

    it('clears the viewer-active context when the last viewer terminates', async () => {
      const parent = createParentSession();
      const child = await primeViewer(provider, parent);
      mockExecuteCommand.mockClear();

      provider.handleTerminateSession(child as unknown as vscode.DebugSession);

      expect(mockExecuteCommand).toHaveBeenCalledWith(
        'setContext',
        'test-host:snapshot:viewer-active',
        false,
      );
      expect(mockExecuteCommand).toHaveBeenCalledWith(
        'setContext',
        'test-host:snapshot:viewer-count',
        0,
      );
    });
  });

  describe('handleStartSession', () => {
    it('registers tintype viewers that do not match any pending launch token as standalone viewers', async () => {
      const parent = createParentSession();
      await primeViewer(provider, parent, {childId: 'known-child'});

      // Some unrelated tintype viewer the user opened from a launch
      // config — no token, no matching parent pending entry. It must
      // still show up in the sidebar so users can navigate snapshots
      // in a ``.pytb`` they opened directly.
      const otherChild = childSessionFromConfig('other-child', {
        name: 'Tintype Snapshot (User Opened)',
        pytbPath: '/tmp/user-opened.pytb',
      });
      provider.handleStartSession(otherChild as unknown as vscode.DebugSession);
      await Promise.resolve();

      expect(provider.getViewerSummaries().map(v => v.viewerId)).toEqual([
        'known-child',
        'other-child',
      ]);
      const standalone = provider.getViewerSummaries().find(v => v.viewerId === 'other-child');
      expect(standalone).toBeDefined();
      expect(standalone?.parentAlive).toBe(false);
    });
  });

  describe('handleStoppedSession', () => {
    it('refreshes only the viewer whose stopped event fired', async () => {
      const parentA = createParentSession({id: 'parent-A', name: 'Alpha'});
      const parentB = createParentSession({id: 'parent-B', name: 'Bravo'});
      const childACustom = jest.fn().mockResolvedValue({currentIndex: 0, snapshots: []});
      const childBCustom = jest.fn().mockResolvedValue({currentIndex: 0, snapshots: []});
      const childA = await primeViewer(provider, parentA, {
        childId: 'child-A',
        childCustomRequest: childACustom,
      });
      await primeViewer(provider, parentB, {
        childId: 'child-B',
        childCustomRequest: childBCustom,
      });
      childACustom.mockClear();
      childBCustom.mockClear();

      provider.handleStoppedSession(childA as unknown as vscode.DebugSession);
      await Promise.resolve();
      await Promise.resolve();

      expect(childACustom).toHaveBeenCalledWith('tintypeSnapshotList');
      expect(childBCustom).not.toHaveBeenCalled();
    });

    it('ignores stopped events from sessions not in the registry', async () => {
      const parent = createParentSession();
      const childCustom = jest.fn().mockResolvedValue({currentIndex: 0, snapshots: []});
      await primeViewer(provider, parent, {
        childId: 'child-1',
        childCustomRequest: childCustom,
      });
      childCustom.mockClear();

      const stray = childSessionFromConfig('stray', {name: 'Stray'});
      provider.handleStoppedSession(stray as unknown as vscode.DebugSession);
      await Promise.resolve();

      expect(childCustom).not.toHaveBeenCalled();
    });
  });

  describe('handleChangeActiveSession', () => {
    it('sets can-take true for parent debug type', () => {
      const parent = createParentSession();
      provider.handleChangeActiveSession(parent as unknown as vscode.DebugSession);
      expect(mockExecuteCommand).toHaveBeenCalledWith(
        'setContext',
        'test-host:snapshot:can-take',
        true,
      );
    });

    it('sets can-take false when the active session is undefined', () => {
      provider.handleChangeActiveSession(undefined);
      expect(mockExecuteCommand).toHaveBeenCalledWith(
        'setContext',
        'test-host:snapshot:can-take',
        false,
      );
    });
  });

  describe('viewer toolbar commands', () => {
    it('jumpToLastSnapshot routes to the active viewer at the last snapshot index', async () => {
      const parent = createParentSession();
      const childCustom = jest.fn().mockImplementation((command: string) => {
        if (command === 'tintypeSnapshotList') {
          return Promise.resolve({
            currentIndex: 0,
            snapshots: [
              {index: 0, timestampUs: 1_000_000},
              {index: 1, timestampUs: 2_000_000},
              {index: 2, timestampUs: 3_000_000},
            ],
          });
        }
        return Promise.resolve({});
      });
      const child = await primeViewer(provider, parent, {
        childId: 'child-jump-last',
        childCustomRequest: childCustom,
      });

      mockActiveDebugSession = child;
      childCustom.mockClear();

      await provider.jumpToLastSnapshot();

      // Three snapshots means the last index is 2.
      expect(childCustom).toHaveBeenCalledWith('tintypeJumpToSnapshot', {index: 2});
    });

    it('jumpToLastSnapshot is a no-op when the active session is not a tintype viewer', async () => {
      const parent = createParentSession();
      const childCustom = jest.fn().mockResolvedValue({
        currentIndex: 0,
        snapshots: [{index: 0, timestampUs: 1_000_000}],
      });
      await primeViewer(provider, parent, {
        childId: 'child-active-parent',
        childCustomRequest: childCustom,
      });

      // Parent is active, not the viewer — command should short-circuit.
      mockActiveDebugSession = parent;
      childCustom.mockClear();

      await provider.jumpToLastSnapshot();

      expect(childCustom).not.toHaveBeenCalled();
    });

    it('takeSnapshotOnParent dispatches the snapshot evaluate on the parent, not the viewer', async () => {
      const parent = createParentSession({id: 'parent-takes', name: 'Takes'});
      const childCustom = jest.fn().mockResolvedValue({});
      const child = await primeViewer(provider, parent, {
        childId: 'child-takes',
        childCustomRequest: childCustom,
      });

      mockActiveDebugSession = child;
      // The parent's customRequest has seen initializeSnapshotting by now,
      // so clear it to verify the command dispatches a fresh evaluate on
      // the parent rather than a jump on the viewer.
      const parentRequest = parent.customRequest as jest.Mock;
      parentRequest.mockClear();
      childCustom.mockClear();

      await provider.takeSnapshotOnParent();

      // The take-snapshot path evaluates the shared VS Code capture API
      // on the parent session. The child must not see any customRequest
      // beyond the follow-up ``tintypeSnapshotList`` refresh.
      expect(parentRequest).toHaveBeenCalledWith('evaluate', {
        expression: "__import__('tintype.vscode', fromlist=['capture']).capture()",
        context: 'repl',
      });
      expect(childCustom).not.toHaveBeenCalledWith(
        'evaluate',
        expect.objectContaining({
          expression: "__import__('tintype.vscode', fromlist=['capture']).capture()",
        }),
      );
    });

    it('takeSnapshotOnParent is a no-op when the parent has terminated', async () => {
      const parent = createParentSession();
      const child = await primeViewer(provider, parent);

      provider.handleTerminateSession(parent as unknown as vscode.DebugSession);
      mockActiveDebugSession = child;
      const parentRequest = parent.customRequest as jest.Mock;
      parentRequest.mockClear();

      await provider.takeSnapshotOnParent();

      expect(parentRequest).not.toHaveBeenCalled();
    });
  });

  describe('active-viewer-has-live-parent context key', () => {
    it('flips to true when focusing a viewer whose parent is live', async () => {
      const parent = createParentSession();
      const child = await primeViewer(provider, parent);
      mockExecuteCommand.mockClear();

      provider.handleChangeActiveSession(child as unknown as vscode.DebugSession);

      expect(mockExecuteCommand).toHaveBeenCalledWith(
        'setContext',
        'test-host:tintype:active-viewer-has-live-parent',
        true,
      );
    });

    it('flips to false when the parent terminates while the viewer is focused', async () => {
      const parent = createParentSession();
      const child = await primeViewer(provider, parent);
      provider.handleChangeActiveSession(child as unknown as vscode.DebugSession);
      mockActiveDebugSession = child;
      mockExecuteCommand.mockClear();

      provider.handleTerminateSession(parent as unknown as vscode.DebugSession);

      expect(mockExecuteCommand).toHaveBeenCalledWith(
        'setContext',
        'test-host:tintype:active-viewer-has-live-parent',
        false,
      );
    });

    it('is false while a parent session is active (not a viewer)', async () => {
      const parent = createParentSession();
      await primeViewer(provider, parent);
      mockExecuteCommand.mockClear();

      provider.handleChangeActiveSession(parent as unknown as vscode.DebugSession);

      expect(mockExecuteCommand).toHaveBeenCalledWith(
        'setContext',
        'test-host:tintype:active-viewer-has-live-parent',
        false,
      );
    });
  });

  describe('finalizeOnParent', () => {
    it('evaluates tintype.vscode.finalize() on the parent with the chosen path', async () => {
      const parent = createParentSession();
      const child = await primeViewer(provider, parent);

      mockActiveDebugSession = child;
      mockShowSaveDialog.mockResolvedValue({fsPath: '/tmp/final.pytb'});
      mockShowWarningMessage.mockResolvedValue('Save');
      const parentRequest = parent.customRequest as jest.Mock;
      parentRequest.mockClear();

      await provider.finalizeOnParent();

      expect(parentRequest).toHaveBeenCalledWith('evaluate', {
        expression: `__import__('tintype.vscode', fromlist=['finalize']).finalize("/tmp/final.pytb")`,
        context: 'repl',
      });
      expect(mockShowInformationMessage).toHaveBeenCalledWith(
        expect.stringContaining('/tmp/final.pytb'),
      );
    });

    it('warns the user before overwriting an existing file at the chosen save path', async () => {
      // OS-level save dialogs on desktop show a native "file exists,
      // replace?" prompt, but the VS Code web / remote hosts don't
      // always delegate to the OS. Defensive explicit overwrite
      // confirmation keeps us safe from silent clobbers \u2014
      // ``tintype.vscode.finalize()`` is irreversible.
      const parent = createParentSession();
      const child = await primeViewer(provider, parent);

      mockActiveDebugSession = child;
      mockShowSaveDialog.mockResolvedValue({fsPath: '/tmp/final.pytb'});
      // Simulate "file already exists" by returning a stat object.
      mockFsStat.mockResolvedValue({type: 1, size: 100, ctime: 0, mtime: 0});
      mockShowWarningMessage.mockResolvedValue('Overwrite');
      const parentRequest = parent.customRequest as jest.Mock;
      parentRequest.mockClear();

      await provider.finalizeOnParent();

      // The confirmation uses overwrite-specific title / detail /
      // action so users can't miss what's about to happen.
      expect(mockShowWarningMessage).toHaveBeenCalledWith(
        expect.stringContaining('Overwrite') as unknown,
        expect.objectContaining({
          modal: true,
          detail: expect.stringContaining('OVERWRITTEN') as unknown,
        }) as unknown,
        'Overwrite',
      );
      // Finalize still runs when the user confirms the overwrite.
      expect(parentRequest).toHaveBeenCalledWith('evaluate', {
        expression: `__import__('tintype.vscode', fromlist=['finalize']).finalize("/tmp/final.pytb")`,
        context: 'repl',
      });
    });

    it('is a no-op when the user dismisses the overwrite confirmation', async () => {
      const parent = createParentSession();
      const child = await primeViewer(provider, parent);

      mockActiveDebugSession = child;
      mockShowSaveDialog.mockResolvedValue({fsPath: '/tmp/existing.pytb'});
      mockFsStat.mockResolvedValue({type: 1, size: 42, ctime: 0, mtime: 0});
      // User dismissed the overwrite confirmation (clicked Cancel or
      // closed the modal). Return undefined to mirror VS Code's
      // cancel behaviour.
      mockShowWarningMessage.mockResolvedValue(undefined);
      const parentRequest = parent.customRequest as jest.Mock;
      parentRequest.mockClear();

      await provider.finalizeOnParent();

      // No finalize() eval fired, but we still saw the overwrite
      // confirmation (so the user was warned).
      expect(mockShowWarningMessage).toHaveBeenCalledWith(
        expect.stringContaining('Overwrite') as unknown,
        expect.objectContaining({
          detail: expect.stringContaining('OVERWRITTEN') as unknown,
        }) as unknown,
        'Overwrite',
      );
      expect(parentRequest).not.toHaveBeenCalledWith(
        'evaluate',
        expect.objectContaining({
          expression: expect.stringContaining("__import__('tintype.vscode'") as unknown,
        }) as unknown,
      );
    });

    it('uses the non-overwrite confirmation when the target path does not exist yet', async () => {
      const parent = createParentSession();
      const child = await primeViewer(provider, parent);

      mockActiveDebugSession = child;
      mockShowSaveDialog.mockResolvedValue({fsPath: '/tmp/brand-new.pytb'});
      // Default beforeEach mock rejects stat, matching the
      // "file does not exist" case.
      mockShowWarningMessage.mockResolvedValue('Save');

      await provider.finalizeOnParent();

      // The confirmation message avoids the overwrite language and
      // presents "Save" as the action so the dialog copy matches the
      // safe-save case.
      expect(mockShowWarningMessage).toHaveBeenCalledWith(
        'Save this snapshot file?',
        expect.objectContaining({
          modal: true,
          detail: expect.stringContaining('will be sealed and written') as unknown,
        }) as unknown,
        'Save',
      );
    });

    it('finalizes the viewer identified by an explicit viewerId even when focus is elsewhere', async () => {
      const parent = createParentSession();
      const child = await primeViewer(provider, parent, {childId: 'targeted-child'});
      // Focus is on the parent session, NOT the viewer — this is the
      // inline-button case: clicking a tree item doesn't change the
      // active debug session.
      mockActiveDebugSession = parent;
      mockShowSaveDialog.mockResolvedValue({fsPath: '/tmp/targeted.pytb'});
      mockShowWarningMessage.mockResolvedValue('Save');
      const parentRequest = parent.customRequest as jest.Mock;
      parentRequest.mockClear();

      await provider.finalizeOnParent('targeted-child');

      expect(parentRequest).toHaveBeenCalledWith('evaluate', {
        expression: `__import__('tintype.vscode', fromlist=['finalize']).finalize("/tmp/targeted.pytb")`,
        context: 'repl',
      });
      // Unused-variable guard: ``child`` is registered so the lookup
      // by id succeeds; the assertion above proves the wiring.
      expect(child.id).toBe('targeted-child');
    });

    it('is a no-op when given a viewerId that no longer exists', async () => {
      const parent = createParentSession();
      await primeViewer(provider, parent);
      const parentRequest = parent.customRequest as jest.Mock;
      parentRequest.mockClear();

      await provider.finalizeOnParent('nonexistent-viewer');

      expect(mockShowSaveDialog).not.toHaveBeenCalled();
      expect(parentRequest).not.toHaveBeenCalled();
    });

    it('escapes backslashes and double quotes in the chosen path', async () => {
      const parent = createParentSession();
      const child = await primeViewer(provider, parent);

      mockActiveDebugSession = child;
      mockShowSaveDialog.mockResolvedValue({
        fsPath: 'C:\\snap\\"quoted\\".pytb',
      });
      mockShowWarningMessage.mockResolvedValue('Save');
      const parentRequest = parent.customRequest as jest.Mock;
      parentRequest.mockClear();

      await provider.finalizeOnParent();

      expect(parentRequest).toHaveBeenCalledWith('evaluate', {
        expression: `__import__('tintype.vscode', fromlist=['finalize']).finalize("C:\\\\snap\\\\\\"quoted\\\\\\".pytb")`,
        context: 'repl',
      });
    });

    it('neutralizes embedded newlines in the chosen path so they do not inject a second Python statement', async () => {
      // Defense-in-depth: the save dialog is supposed to hand us a
      // real path, but if ``fsPath`` ever carries an embedded ``\n`` /
      // ``\r`` (e.g. shell hack in a crafted workspace, Unix filename
      // with a literal newline) the Python string literal would close
      // mid-statement and the attacker-controlled tail would execute
      // as a follow-up statement in the ``evaluate`` request. Verify
      // the evaluator gets a single-line, properly-escaped literal.
      const parent = createParentSession();
      const child = await primeViewer(provider, parent);

      mockActiveDebugSession = child;
      mockShowSaveDialog.mockResolvedValue({
        fsPath: '/tmp/a.pytb\nimport os; os.system("pwned")',
      });
      mockShowWarningMessage.mockResolvedValue('Save');
      const parentRequest = parent.customRequest as jest.Mock;
      parentRequest.mockClear();

      await provider.finalizeOnParent();

      expect(parentRequest).toHaveBeenCalledTimes(1);
      const call = parentRequest.mock.calls[0] as [string, {expression: string}];
      const expression = call[1].expression;
      // The hostile payload must end up inside the Python string
      // literal (escaped), never on its own line.
      expect(expression).not.toContain('\n');
      expect(expression).toMatch(
        /^__import__\('tintype\.vscode', fromlist=\['finalize'\]\)\.finalize\(".*"\)$/,
      );
      expect(expression).toContain('\\n');
    });

    it('short-circuits with an info message when the parent session is live but its working file is already sealed', async () => {
      // Race: the Command Palette entry is gated on the
      // ``active-viewer-backs-live-injection`` context key, but the
      // key can lag if the user previously ran ``finalizeOnParent``
      // on the same parent. Re-check ``injectedSessions`` inside
      // ``finalizeOnParent`` so we surface a friendly message
      // instead of issuing an evaluate() against a parent whose
      // working file was already sealed (which would come back as
      // an opaque Python error).
      const parent = createParentSession();
      const child = await primeViewer(provider, parent);

      // Simulate "working file already sealed": parent session is
      // still live (in liveParents) but no longer in injectedSessions.
      (provider as unknown as {injectedSessions: Map<string, unknown>}).injectedSessions.delete(
        parent.id,
      );

      mockActiveDebugSession = child;
      const parentRequest = parent.customRequest as jest.Mock;
      parentRequest.mockClear();

      await provider.finalizeOnParent();

      expect(mockShowSaveDialog).not.toHaveBeenCalled();
      expect(parentRequest).not.toHaveBeenCalled();
      expect(mockShowInformationMessage).toHaveBeenCalledWith(
        expect.stringContaining('already been saved'),
      );
    });

    it('is a no-op when the user cancels the save dialog', async () => {
      const parent = createParentSession();
      const child = await primeViewer(provider, parent);

      mockActiveDebugSession = child;
      mockShowSaveDialog.mockResolvedValue(undefined);
      const parentRequest = parent.customRequest as jest.Mock;
      parentRequest.mockClear();

      await provider.finalizeOnParent();

      expect(parentRequest).not.toHaveBeenCalled();
      expect(mockShowWarningMessage).not.toHaveBeenCalled();
    });

    it('is a no-op when the user dismisses the confirmation', async () => {
      const parent = createParentSession();
      const child = await primeViewer(provider, parent);

      mockActiveDebugSession = child;
      mockShowSaveDialog.mockResolvedValue({fsPath: '/tmp/final.pytb'});
      mockShowWarningMessage.mockResolvedValue(undefined);
      const parentRequest = parent.customRequest as jest.Mock;
      parentRequest.mockClear();

      await provider.finalizeOnParent();

      expect(parentRequest).not.toHaveBeenCalled();
    });

    it('short-circuits with an error when the parent has terminated', async () => {
      const parent = createParentSession();
      const child = await primeViewer(provider, parent);

      provider.handleTerminateSession(parent as unknown as vscode.DebugSession);
      mockActiveDebugSession = child;
      const parentRequest = parent.customRequest as jest.Mock;
      parentRequest.mockClear();

      await provider.finalizeOnParent();

      expect(mockShowSaveDialog).not.toHaveBeenCalled();
      expect(parentRequest).not.toHaveBeenCalled();
      expect(mockShowErrorMessage).toHaveBeenCalledWith(expect.stringContaining('ended'));
    });

    it('is a no-op when no tintype viewer is active', async () => {
      const parent = createParentSession();
      await primeViewer(provider, parent);

      // Parent is active, not the viewer.
      mockActiveDebugSession = parent;

      await provider.finalizeOnParent();

      expect(mockShowSaveDialog).not.toHaveBeenCalled();
    });

    it('drops the cached injection state so the next take-snapshot re-initializes', async () => {
      const parent = createParentSession();
      const child = await primeViewer(provider, parent);

      mockActiveDebugSession = child;
      mockShowSaveDialog.mockResolvedValue({fsPath: '/tmp/final.pytb'});
      mockShowWarningMessage.mockResolvedValue('Save');
      await provider.finalizeOnParent();

      // Flip focus back to the parent and take another snapshot — the
      // cache-drop means we should see a fresh initialization evaluate
      // for ``tintype.vscode.session_info()`` rather than a direct snapshot.
      mockActiveDebugSession = parent;
      const parentRequest = parent.customRequest as jest.Mock;
      parentRequest.mockClear();

      await provider.takeSnapshot();

      const initializeEvaluates = parentRequest.mock.calls.filter((call: unknown[]) => {
        const command = call[0] as string;
        const args = call[1] as {expression?: string} | undefined;
        return (
          command === 'evaluate' &&
          typeof args?.expression === 'string' &&
          args.expression.includes('session_info')
        );
      });
      expect(initializeEvaluates.length).toBeGreaterThan(0);
    });

    it('hides backsLiveInjection on the old viewer after finalize + re-init spawns a new viewer', async () => {
      // Reproducer for the stale-Save-button bug: after finalize seals
      // V1's file, the user takes another snapshot on the parent. That
      // re-inits against a FRESH working file and spawns V2 alongside
      // V1 in the registry. Without the pytbPath comparison in
      // getViewerSummaries, V1 would flip ``backsLiveInjection`` back
      // to true and its Save button would reappear \u2014 clicking it would
      // finalize V2's new working file, not V1's old one.
      const parent = createParentSession();
      const oldViewer = await primeViewer(provider, parent, {childId: 'viewer-old'});

      // Seal the first viewer's file.
      mockActiveDebugSession = oldViewer;
      mockShowSaveDialog.mockResolvedValue({fsPath: '/tmp/first.pytb'});
      mockShowWarningMessage.mockResolvedValue('Save');
      await provider.finalizeOnParent();

      // Right after finalize the old viewer must not advertise
      // savability (nothing to save on a sealed file).
      const oldAfterFinalize = provider.getViewerSummaries().find(v => v.viewerId === 'viewer-old');
      expect(oldAfterFinalize).toBeDefined();
      expect(oldAfterFinalize?.backsLiveInjection).toBe(false);

      // Re-init will report a DIFFERENT working file path \u2014 the whole
      // point of finalize is that the next injection goes elsewhere.
      (parent.customRequest as jest.Mock).mockImplementation(
        (_command: string, args?: {expression?: string}) => {
          if (args?.expression?.includes("['workingFile']")) {
            return Promise.resolve({result: "'/tmp/second_working.pytb'"});
          }
          if (args?.expression?.includes("['cwd']")) {
            return Promise.resolve({result: "'/home/alice/project'"});
          }
          return Promise.resolve({});
        },
      );

      mockActiveDebugSession = parent;
      await provider.takeSnapshot();

      // Register the new viewer VS Code would create in response to
      // the startDebugging call the provider just issued.
      const calls = mockStartDebugging.mock.calls as unknown as StartDebuggingCall[];
      const newConfig = calls[calls.length - 1][1];
      const newViewer = childSessionFromConfig('viewer-new', newConfig);
      provider.handleStartSession(newViewer as unknown as vscode.DebugSession);
      await Promise.resolve();
      await Promise.resolve();

      const summaries = provider.getViewerSummaries();
      const oldSummary = summaries.find(v => v.viewerId === 'viewer-old');
      const newSummary = summaries.find(v => v.viewerId === 'viewer-new');
      expect(oldSummary).toBeDefined();
      expect(newSummary).toBeDefined();
      // Critical: only the NEW viewer's Save button lights up. The
      // old viewer stays inert so clicking its (now stale) Save
      // button can't fire a finalize against the new working file.
      expect(oldSummary?.backsLiveInjection).toBe(false);
      expect(newSummary?.backsLiveInjection).toBe(true);
    });

    it('bails with an info message when an explicit finalize targets a viewer whose file is already sealed', async () => {
      // Defensive depth: the ``getViewerSummaries`` gate should hide
      // the button, but a stale context key or a tree item that
      // predates the refresh can still route a click through
      // ``finalizeOnParent(explicitId)``. The per-viewer pytbPath
      // check inside finalizeOnParent catches that case and emits the
      // same "already saved" message the injectedSessions-miss path
      // uses \u2014 preventing the wrong file from being baked into the
      // user's chosen save path.
      const parent = createParentSession();
      await primeViewer(provider, parent, {childId: 'viewer-old'});

      mockActiveDebugSession = parent;
      mockShowSaveDialog.mockResolvedValue({fsPath: '/tmp/first.pytb'});
      mockShowWarningMessage.mockResolvedValue('Save');
      // Focus parent, invoke finalize via explicit viewerId (mirrors
      // the inline tree-item Save button path).
      await provider.finalizeOnParent('viewer-old');

      // Parent re-inits against a new file.
      (parent.customRequest as jest.Mock).mockImplementation(
        (_command: string, args?: {expression?: string}) => {
          if (args?.expression?.includes("['workingFile']")) {
            return Promise.resolve({result: "'/tmp/second_working.pytb'"});
          }
          if (args?.expression?.includes("['cwd']")) {
            return Promise.resolve({result: "'/home/alice/project'"});
          }
          return Promise.resolve({});
        },
      );
      await provider.takeSnapshot();

      // Stale click on the OLD viewer (which now points to a sealed
      // file) must not issue a finalize() eval \u2014 if it did, it would
      // seal the parent's current working file under the user's
      // chosen save path.
      const parentRequest = parent.customRequest as jest.Mock;
      parentRequest.mockClear();
      mockShowSaveDialog.mockClear();
      mockShowInformationMessage.mockClear();

      await provider.finalizeOnParent('viewer-old');

      expect(mockShowSaveDialog).not.toHaveBeenCalled();
      const finalizeEvaluates = parentRequest.mock.calls.filter((call: unknown[]) => {
        const command = call[0] as string;
        const args = call[1] as {expression?: string} | undefined;
        return (
          command === 'evaluate' &&
          typeof args?.expression === 'string' &&
          args.expression.includes("__import__('tintype.vscode'") &&
          args.expression.includes('.finalize')
        );
      });
      expect(finalizeEvaluates).toHaveLength(0);
      expect(mockShowInformationMessage).toHaveBeenCalledWith(
        expect.stringContaining('already been saved'),
      );
    });
  });

  describe('standalone viewer', () => {
    it('registers a viewer when a tintype session starts with no pending launch', async () => {
      const standalone = childSessionFromConfig('standalone-1', {
        type: VIEWER_TYPE,
        request: 'launch',
        name: 'My Saved Snapshot',
        pytbPath: '/tmp/saved.pytb',
      });

      provider.handleStartSession(standalone as unknown as vscode.DebugSession);
      // Flush the refreshSnapshotList microtask the path kicks off.
      await Promise.resolve();
      await Promise.resolve();

      const summaries = provider.getViewerSummaries();
      expect(summaries).toHaveLength(1);
      expect(summaries[0].viewerId).toBe('standalone-1');
      // No parent exists to be "alive" — the parent-scoped toolbar
      // buttons rely on this staying false.
      expect(summaries[0].parentAlive).toBe(false);
      // The sidebar container is gated on this context key; it must
      // flip to true for a standalone viewer to become visible.
      expect(mockExecuteCommand).toHaveBeenCalledWith(
        'setContext',
        'test-host:snapshot:viewer-active',
        true,
      );
    });

    it('uses the session name as the group label for a standalone viewer', async () => {
      const standalone = childSessionFromConfig('standalone-2', {
        type: VIEWER_TYPE,
        request: 'launch',
        name: 'Standalone Snapshot Name',
        pytbPath: '/tmp/other.pytb',
      });

      provider.handleStartSession(standalone as unknown as vscode.DebugSession);
      await Promise.resolve();

      const summaries = provider.getViewerSummaries();
      expect(summaries[0].parentSessionName).toBe('Standalone Snapshot Name');
    });

    it('terminating a standalone viewer removes it from the registry', async () => {
      const standalone = childSessionFromConfig('standalone-3', {
        type: VIEWER_TYPE,
        request: 'launch',
        name: 'To Be Closed',
        pytbPath: '/tmp/close.pytb',
      });
      provider.handleStartSession(standalone as unknown as vscode.DebugSession);
      await Promise.resolve();
      expect(provider.getViewerSummaries()).toHaveLength(1);

      provider.handleTerminateSession(standalone as unknown as vscode.DebugSession);

      expect(provider.getViewerSummaries()).toHaveLength(0);
    });
  });

  describe('active-viewer-backs-live-injection context key', () => {
    const CONTEXT_KEY = 'test-host:tintype:active-viewer-backs-live-injection';

    it('is true after a primed viewer with an active injection is focused', async () => {
      const parent = createParentSession();
      const child = await primeViewer(provider, parent);
      mockExecuteCommand.mockClear();

      provider.handleChangeActiveSession(child as unknown as vscode.DebugSession);

      expect(mockExecuteCommand).toHaveBeenCalledWith('setContext', CONTEXT_KEY, true);
    });

    it('refreshes viewer gates when adopting a different working file', async () => {
      const parent = createParentSession();
      const child = await primeViewer(provider, parent);
      mockActiveDebugSession = child;
      provider.handleChangeActiveSession(child as unknown as vscode.DebugSession);
      mockExecuteCommand.mockClear();

      await provider.adoptWorkingFile(
        parent as unknown as vscode.DebugSession,
        '/tmp/replacement.pytb',
        '/home/alice/project',
      );

      expect(mockExecuteCommand).toHaveBeenCalledWith(
        'setContext',
        'test-host:tintype:active-viewer-has-live-parent',
        true,
      );
      expect(mockExecuteCommand).toHaveBeenCalledWith('setContext', CONTEXT_KEY, false);
      expect(provider.getViewerSummaries()[0].backsLiveInjection).toBe(false);
    });

    it('flips to false when the parent debug session terminates', async () => {
      const parent = createParentSession();
      const child = await primeViewer(provider, parent);
      provider.handleChangeActiveSession(child as unknown as vscode.DebugSession);
      mockActiveDebugSession = child;
      mockExecuteCommand.mockClear();

      provider.handleTerminateSession(parent as unknown as vscode.DebugSession);

      expect(mockExecuteCommand).toHaveBeenCalledWith('setContext', CONTEXT_KEY, false);
    });

    it('flips to false after finalizeOnParent succeeds', async () => {
      const parent = createParentSession();
      const child = await primeViewer(provider, parent);
      mockActiveDebugSession = child;
      provider.handleChangeActiveSession(child as unknown as vscode.DebugSession);
      mockShowSaveDialog.mockResolvedValue({fsPath: '/tmp/final.pytb'});
      mockShowWarningMessage.mockResolvedValue('Save');
      mockExecuteCommand.mockClear();

      await provider.finalizeOnParent();

      expect(mockExecuteCommand).toHaveBeenCalledWith('setContext', CONTEXT_KEY, false);
    });

    it('is false for a standalone viewer (no parent at all)', async () => {
      const standalone = childSessionFromConfig('standalone', {
        type: VIEWER_TYPE,
        request: 'launch',
        name: 'Standalone',
        pytbPath: '/tmp/standalone.pytb',
      });
      provider.handleStartSession(standalone as unknown as vscode.DebugSession);
      await Promise.resolve();
      mockExecuteCommand.mockClear();

      provider.handleChangeActiveSession(standalone as unknown as vscode.DebugSession);

      expect(mockExecuteCommand).toHaveBeenCalledWith('setContext', CONTEXT_KEY, false);
    });

    it('is false while a parent session (not a viewer) is focused', async () => {
      const parent = createParentSession();
      await primeViewer(provider, parent);
      mockExecuteCommand.mockClear();

      provider.handleChangeActiveSession(parent as unknown as vscode.DebugSession);

      expect(mockExecuteCommand).toHaveBeenCalledWith('setContext', CONTEXT_KEY, false);
    });
  });
});

describe('generateLaunchToken', () => {
  // ``crypto.randomUUID`` is available in every Node version VS Code ships
  // against, so the happy path is covered implicitly by every launch flow
  // in the parent ``SnapshotProvider`` suite.  These two cases pin the
  // branch behaviour so a future refactor that inverts the ``typeof``
  // check (or drops the fallback entirely) can't silently regress.
  const cryptoModule: {randomUUID?: unknown} = cryptoModuleImport;
  const originalRandomUUID = cryptoModule.randomUUID;

  afterEach(() => {
    cryptoModule.randomUUID = originalRandomUUID;
  });

  it('returns the crypto.randomUUID() value when available', () => {
    cryptoModule.randomUUID = jest.fn(() => 'deadbeef-0000-4000-8000-000000000000');
    const token = __testOnly_generateLaunchToken();
    expect(token).toBe('deadbeef-0000-4000-8000-000000000000');
  });

  it('falls back to Math.random / Date.now when crypto.randomUUID is absent', () => {
    delete cryptoModule.randomUUID;
    const token = __testOnly_generateLaunchToken();
    // Fallback shape: ``<36-ish-alphanum>-<unix-ms>``.
    expect(typeof token).toBe('string');
    expect(token.length).toBeGreaterThan(0);
    expect(token).toMatch(/^[a-z0-9]+-\d+$/);
  });
});
