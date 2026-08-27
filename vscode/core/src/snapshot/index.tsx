/**
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * This source code is licensed under the MIT license found in the
 * LICENSE file in the root directory of this source tree.
 *
 * @format
 */

import vscode from 'vscode';
import {SnapshotProvider, type SnapshotProviderHostOptions} from './snapshot-provider';
import {SnapshotTreeProvider, TreeElement} from './snapshot-tree-provider';

export * from './snapshot-provider';
export * from './snapshot-tree-provider';

/**
 * DAP requests that leave the debuggee running, so a capture scheduled
 * against the current stop must be dropped. ``restartFrame`` and ``goto``
 * are included because both resume execution even though neither reads
 * as a "continue" at the UI level.
 */
const RESUME_COMMANDS: ReadonlySet<string> = new Set([
  'continue',
  'next',
  'stepIn',
  'stepOut',
  'stepBack',
  'reverseContinue',
  'restartFrame',
  'goto',
  'restart',
  'disconnect',
  'terminate',
]);

export type SnapshotProviderRegistration = {
  disposable: vscode.Disposable;
  provider: SnapshotProvider;
};

export type SnapshotProviderOptions = {
  /**
   * Debug type of the parent live session the camera button acts on.
   * The owning host is responsible for preparing its capture runtime.
   */
  injectionDebugType: string | readonly string[];
  /**
   * Debug type of the child viewer session. The ``tintype`` adapter
   * (``TintypeDebugAdapterDescriptorFactory``) — speaks the custom
   * DAP requests the sidebar relies on.
   */
  viewerDebugType: string;
  commandPrefix: string;
  snapshotViewId?: string;
  hostOptions?: SnapshotProviderHostOptions;
};

export function registerSnapshotProvider({
  injectionDebugType,
  viewerDebugType,
  commandPrefix,
  snapshotViewId = `${commandPrefix}.snapshot-list`,
  hostOptions,
}: SnapshotProviderOptions): SnapshotProviderRegistration {
  const takeCommand = `${commandPrefix}.debugger.take-snapshot`;
  const jumpCommand = `${commandPrefix}.debugger.jump-to-snapshot`;
  const refreshCommand = `${commandPrefix}.debugger.refresh-snapshot-list`;
  const jumpToLastCommand = `${commandPrefix}.debugger.jump-to-last-snapshot`;
  const takeSnapshotOnParentCommand = `${commandPrefix}.debugger.take-snapshot-on-parent`;
  const finalizeCommand = `${commandPrefix}.debugger.finalize-snapshot-file`;

  const treeProvider = new SnapshotTreeProvider(jumpCommand);
  const snapshot = new SnapshotProvider(
    injectionDebugType,
    viewerDebugType,
    commandPrefix,
    hostOptions,
  );
  snapshot.setTreeProvider(treeProvider);
  treeProvider.bind(() => snapshot.getViewerSummaries());

  // Automatic capture-on-stop. Registered only when the host opted in,
  // so hosts without the settings pay nothing for the extra tracker.
  // A ``stopped`` event on the live parent covers breakpoint hits,
  // exceptions, pause, and step completion alike; the provider decides
  // per event whether the user has the setting enabled.
  const autoSnapshotTrackers =
    hostOptions?.resolveAutoSnapshotConfig == null
      ? []
      : (typeof injectionDebugType === 'string' ? [injectionDebugType] : injectionDebugType).map(
          debugType =>
            vscode.debug.registerDebugAdapterTrackerFactory(debugType, {
              createDebugAdapterTracker(session: vscode.DebugSession) {
                return {
                  // Watch outgoing *requests* rather than relying on the
                  // ``continued`` event: DAP lets an adapter omit that
                  // event when the resume was request-initiated, which
                  // is exactly the stepping case we must not miss. A
                  // scheduled capture that fired after the debuggee
                  // resumed would evaluate against a running thread and
                  // fail instead of capturing.
                  onWillReceiveMessage(message: {type?: string; command?: string}) {
                    if (
                      message.type === 'request' &&
                      message.command != null &&
                      RESUME_COMMANDS.has(message.command)
                    ) {
                      snapshot.cancelPendingAutoSnapshot(session);
                    }
                  },
                  onDidSendMessage(message: {type?: string; event?: string}) {
                    if (message.type !== 'event') {
                      return;
                    }
                    if (message.event === 'stopped') {
                      void snapshot.handleParentStopped(session);
                    } else if (message.event === 'continued') {
                      // Adapter-initiated resume (e.g. another client
                      // continued the session).
                      snapshot.cancelPendingAutoSnapshot(session);
                    }
                  },
                };
              },
            }),
        );

  const disposable = vscode.Disposable.from(
    ...autoSnapshotTrackers,
    vscode.commands.registerCommand(takeCommand, () => snapshot.takeSnapshot()),
    // ``jumpCommand`` is always invoked via the tree item's command, which
    // owns both arguments (viewerId, index). Third-party callers can still
    // pass a bare index; in that case there's no viewer to route to so
    // the call becomes a no-op.
    vscode.commands.registerCommand(
      jumpCommand,
      (viewerIdOrIndex: string | number, maybeIndex?: number) => {
        if (typeof viewerIdOrIndex === 'string' && typeof maybeIndex === 'number') {
          return snapshot.jumpToSnapshot(viewerIdOrIndex, maybeIndex);
        }
        return Promise.resolve();
      },
    ),
    vscode.commands.registerCommand(refreshCommand, (viewerId?: string) => {
      if (viewerId == null) {
        // Fan out to every live viewer when invoked without args.
        return Promise.all(
          snapshot.getViewerSummaries().map(v => snapshot.refreshSnapshotList(v.viewerId)),
        ).then(() => undefined);
      }
      return snapshot.refreshSnapshotList(viewerId);
    }),
    vscode.commands.registerCommand(jumpToLastCommand, () => snapshot.jumpToLastSnapshot()),
    vscode.commands.registerCommand(takeSnapshotOnParentCommand, () =>
      snapshot.takeSnapshotOnParent(),
    ),
    vscode.commands.registerCommand(finalizeCommand, (arg?: TreeElement | string) => {
      // Invoked one of two ways:
      //   * from the inline Save button on a viewer tree item — VS
      //     Code passes the ``TreeElement`` that ``getChildren``
      //     returned (always a ``{kind: 'viewer', viewerId}``
      //     because the ``view/item/context`` ``when`` gates on
      //     ``viewItem == tintype-viewer-live-injection``).
      //   * from the command palette with no arg — fall back to the
      //     active viewer. The palette entry is gated on
      //     ``active-viewer-backs-live-injection`` so this branch
      //     only fires when the active viewer is itself savable.
      let viewerId: string | undefined;
      let trigger: 'saveButton' | 'finalizeCommand' = 'finalizeCommand';
      if (typeof arg === 'string') {
        viewerId = arg;
        trigger = 'saveButton';
      } else if (arg != null && typeof arg === 'object' && arg.kind === 'viewer') {
        viewerId = arg.viewerId;
        trigger = 'saveButton';
      }
      return snapshot.finalizeOnParent(viewerId, trigger);
    }),
    vscode.window.registerTreeDataProvider(snapshotViewId, treeProvider),
    // Refresh the sidebar whenever a viewer session emits a `stopped`
    // event (on launch, tintypeJumpToSnapshot, step-back, etc.). We
    // Register for the viewer type only so the live parent's
    // own stopped events don't trigger refreshes.
    vscode.debug.registerDebugAdapterTrackerFactory(viewerDebugType, {
      createDebugAdapterTracker(session: vscode.DebugSession) {
        return {
          onDidSendMessage(message: {type?: string; event?: string}) {
            if (message.type === 'event' && message.event === 'stopped') {
              snapshot.handleStoppedSession(session);
            }
          },
        };
      },
    }),
    vscode.debug.onDidTerminateDebugSession((session: vscode.DebugSession) =>
      snapshot.handleTerminateSession(session),
    ),
    vscode.debug.onDidStartDebugSession((session: vscode.DebugSession) =>
      snapshot.handleStartSession(session),
    ),
    vscode.debug.onDidChangeActiveDebugSession((session: vscode.DebugSession | undefined) =>
      snapshot.handleChangeActiveSession(session),
    ),
    {
      dispose: () => {
        snapshot.dispose();
        treeProvider.dispose();
      },
    },
  );

  return {disposable, provider: snapshot};
}
