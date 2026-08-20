/**
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * This source code is licensed under the MIT license found in the
 * LICENSE file in the root directory of this source tree.
 *
 * @format
 */

import vscode from 'vscode';
import {SnappointManager} from './snappoint-manager';
import type {SnapshotProvider} from '../snapshot/snapshot-provider';

export * from './snappoint-manager';

export type SnappointProviderOptions = {
  /**
   * Debug type or types of the parent live sessions snappoints fire against.
   * The owning host is responsible for preparing its capture runtime.
   */
  injectionDebugType: string | readonly string[];
  commandPrefix: string;
  /** Absolute path to the owning extension's root. */
  extensionPath: string;
  /**
   * Companion snapshot provider. The DAP processor calls
   * :meth:`SnapshotProvider.ensureSnapshotting` before forwarding a
   * snappoint-containing ``setBreakpoints`` and
   * :meth:`SnapshotProvider.refreshSnapshotList` when a
   * ``tintypeSnapshotAdded`` event fires.
   */
  snapshotProvider: SnapshotProvider;
  /** Register authoring commands and gutter decorations for this host. */
  enableAuthoringUI?: boolean;
};

let currentManager: SnappointManager | null = null;
let currentSnapshotProvider: SnapshotProvider | null = null;
let currentInjectionDebugType: string | readonly string[] | null = null;

/**
 * Get the active SnappointManager. Returns ``null`` before
 * :func:`registerSnappointProvider` has run. Read by the
 * ``createSnappointProcessors`` factory in the extension's DAP
 * processor pipeline.
 */
export function getSnappointManager(): SnappointManager | null {
  return currentManager;
}

/**
 * Get the SnapshotProvider the snappoint module is bound to. Used by
 * the DAP processor to ensure injection and refresh viewers.
 */
export function getSnappointSnapshotProvider(): SnapshotProvider | null {
  return currentSnapshotProvider;
}

/**
 * Debug type or types snappoints rewrite against. The DAP processor uses this
 * to skip the snappoint rewrite for sessions of a different type.
 */
export function getSnappointInjectionDebugType(): string | readonly string[] | null {
  return currentInjectionDebugType;
}

export function registerSnappointProvider({
  injectionDebugType,
  commandPrefix,
  extensionPath,
  snapshotProvider,
  enableAuthoringUI = true,
}: SnappointProviderOptions): vscode.Disposable {
  const manager = new SnappointManager({
    extensionPath,
    enableDecorations: enableAuthoringUI,
  });
  currentManager = manager;
  currentSnapshotProvider = snapshotProvider;
  currentInjectionDebugType = injectionDebugType;

  const toggleCommand = `${commandPrefix}.snappoint.toggle`;
  const addCommand = `${commandPrefix}.snappoint.add`;
  const removeCommand = `${commandPrefix}.snappoint.remove`;

  function resolveTarget(arg: unknown): {uri: vscode.Uri; line: number} | undefined {
    // ``editor/lineNumber/context`` invokes the command with a single
    // ``{uri, lineNumber}``-shaped arg (lineNumber is 1-based). Keybinding /
    // command-palette invocations have no arg, so fall back to the active
    // editor's selection.
    if (arg != null && typeof arg === 'object') {
      const candidate = arg as {uri?: vscode.Uri; lineNumber?: number};
      if (candidate.uri instanceof vscode.Uri && typeof candidate.lineNumber === 'number') {
        return {uri: candidate.uri, line: candidate.lineNumber - 1};
      }
    }
    const editor = vscode.window.activeTextEditor;
    if (editor == null) {
      return undefined;
    }
    return {uri: editor.document.uri, line: editor.selection.active.line};
  }

  const disposables: vscode.Disposable[] = [
    manager,
    {
      dispose: () => {
        if (currentManager === manager) {
          currentManager = null;
          currentSnapshotProvider = null;
          currentInjectionDebugType = null;
        }
      },
    },
  ];
  if (enableAuthoringUI) {
    disposables.push(
      vscode.commands.registerCommand(toggleCommand, (arg?: unknown) => {
        const target = resolveTarget(arg);
        if (target == null) {
          return;
        }
        manager.toggle(target.uri, target.line);
      }),
      vscode.commands.registerCommand(addCommand, (arg?: unknown) => {
        const target = resolveTarget(arg);
        if (target == null) {
          return;
        }
        manager.add(target.uri, target.line);
      }),
      vscode.commands.registerCommand(removeCommand, (arg?: unknown) => {
        const target = resolveTarget(arg);
        if (target == null) {
          return;
        }
        manager.remove(target.uri, target.line);
      }),
    );
  }
  return vscode.Disposable.from(...disposables);
}
