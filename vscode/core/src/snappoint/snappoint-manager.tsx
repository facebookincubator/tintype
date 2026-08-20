/**
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * This source code is licensed under the MIT license found in the
 * LICENSE file in the root directory of this source tree.
 *
 * @format
 */

/**
 * Snappoints — a "snapshot-and-continue" breakpoint variant.
 *
 * A snappoint is a real ``vscode.SourceBreakpoint`` whose ``logMessage``
 * field carries a fixed marker string (:data:`SNAPPOINT_LOG_MESSAGE_MARKER`).
 * VS Code renders it with its built-in logpoint diamond and lists it in
 * the Breakpoints view automatically; the snappoint module overlays a
 * camera icon in the gutter so users can distinguish snappoints from
 * regular logpoints at a glance.
 *
 * Each host intercepts the marker before ``setBreakpoints`` reaches its
 * adapter and prepares its capture runtime. Hosts preserve logpoint
 * semantics: evaluate, capture, and continue.
 */

import path from 'path';
import * as vscode from 'vscode';

/**
 * Fixed literal stored in ``SourceBreakpoint.logMessage`` to identify a
 * snappoint. Picked to be visibly distinct in the Breakpoints view
 * label and unlikely to collide with a user's own logpoint text.
 */
export const SNAPPOINT_LOG_MESSAGE_MARKER = '__tintype_snappoint__';

export function isSnappoint(bp: vscode.Breakpoint): bp is vscode.SourceBreakpoint {
  if (!(bp instanceof vscode.SourceBreakpoint)) {
    return false;
  }
  return bp.logMessage === SNAPPOINT_LOG_MESSAGE_MARKER;
}

export type SnappointManagerOptions = {
  /**
   * Absolute path to the extension root. Used to resolve the
   * ``resources/{light,dark}/snappoint.svg`` gutter icon paths.
   */
  extensionPath: string;
  /**
   * Languages the toggle command applies to. Snappoints created on
   * other languages still round-trip through the marker — this is only
   * the authoring filter for the right-click menu invocation.
   */
  supportedLanguages?: ReadonlySet<string>;
  /** Whether this host should paint snappoint gutter decorations. */
  enableDecorations?: boolean;
};

/**
 * Owns snappoint authoring (toggle/add/remove) and the gutter
 * decoration that overlays a camera icon on snappoint lines.
 *
 * Persistence is delegated entirely to VS Code's built-in breakpoint
 * store — snappoints survive reload because they are real
 * ``SourceBreakpoint`` instances. This class is stateless beyond the
 * decoration cache.
 */
export class SnappointManager implements vscode.Disposable {
  private readonly decorationType: vscode.TextEditorDecorationType | null;
  private readonly supportedLanguages: ReadonlySet<string>;
  private readonly disposables: vscode.Disposable[] = [];

  constructor(options: SnappointManagerOptions) {
    this.supportedLanguages = options.supportedLanguages ?? new Set(['python']);
    this.decorationType =
      options.enableDecorations === false
        ? null
        : vscode.window.createTextEditorDecorationType({
            gutterIconPath: vscode.Uri.file(
              path.join(options.extensionPath, 'resources', 'light', 'snappoint.svg'),
            ),
            gutterIconSize: 'contain',
            dark: {
              gutterIconPath: vscode.Uri.file(
                path.join(options.extensionPath, 'resources', 'dark', 'snappoint.svg'),
              ),
            },
          });

    if (this.decorationType != null) {
      this.disposables.push(
        this.decorationType,
        vscode.debug.onDidChangeBreakpoints(() => this.refreshDecorations()),
        vscode.window.onDidChangeVisibleTextEditors(() => this.refreshDecorations()),
      );

      // Initial paint for any editors open at extension activation time.
      this.refreshDecorations();
    }
  }

  public dispose(): void {
    for (const d of this.disposables) {
      d.dispose();
    }
  }

  /**
   * Find the existing snappoint at ``(uri, line)`` if any. ``line`` is
   * 0-based — same as ``vscode.Position`` / ``vscode.SourceBreakpoint``.
   */
  public findAt(uri: vscode.Uri, line: number): vscode.SourceBreakpoint | undefined {
    for (const bp of vscode.debug.breakpoints) {
      if (!isSnappoint(bp)) {
        continue;
      }
      if (bp.location.uri.toString() === uri.toString() && bp.location.range.start.line === line) {
        return bp;
      }
    }
    return undefined;
  }

  public hasSnappointAt(uri: vscode.Uri, line: number): boolean {
    return this.findAt(uri, line) != null;
  }

  /**
   * Add a snappoint at ``(uri, line)``. No-op if one already exists.
   * Returns the new (or pre-existing) ``SourceBreakpoint``.
   */
  public add(uri: vscode.Uri, line: number): vscode.SourceBreakpoint {
    const existing = this.findAt(uri, line);
    if (existing != null) {
      return existing;
    }
    const bp = new vscode.SourceBreakpoint(
      new vscode.Location(uri, new vscode.Position(line, 0)),
      /* enabled */ true,
      /* condition */ undefined,
      /* hitCondition */ undefined,
      SNAPPOINT_LOG_MESSAGE_MARKER,
    );
    vscode.debug.addBreakpoints([bp]);
    return bp;
  }

  /**
   * Remove the snappoint at ``(uri, line)``. No-op if none exists.
   * Returns ``true`` when a snappoint was actually removed.
   */
  public remove(uri: vscode.Uri, line: number): boolean {
    const existing = this.findAt(uri, line);
    if (existing == null) {
      return false;
    }
    vscode.debug.removeBreakpoints([existing]);
    return true;
  }

  /**
   * Toggle the snappoint at ``(uri, line)``. Returns ``'added'`` or
   * ``'removed'`` for the resulting state so callers can log telemetry.
   */
  public toggle(uri: vscode.Uri, line: number): 'added' | 'removed' {
    if (this.remove(uri, line)) {
      return 'removed';
    }
    this.add(uri, line);
    return 'added';
  }

  /**
   * Languages the right-click "Add Snappoint" entry should apply to.
   * Used by callers that want to gate UI on document language.
   */
  public isSupportedLanguage(languageId: string): boolean {
    return this.supportedLanguages.has(languageId);
  }

  /**
   * All snappoints currently registered (across all files). Used by
   * the DAP processor to decide whether it needs to wait on
   * ``ensureSnapshotting`` before forwarding a ``setBreakpoints``.
   */
  public getActiveSnappoints(): vscode.SourceBreakpoint[] {
    const out: vscode.SourceBreakpoint[] = [];
    for (const bp of vscode.debug.breakpoints) {
      if (isSnappoint(bp)) {
        out.push(bp);
      }
    }
    return out;
  }

  private refreshDecorations(): void {
    if (this.decorationType == null) {
      return;
    }
    const byUri = new Map<string, vscode.Range[]>();
    for (const bp of vscode.debug.breakpoints) {
      if (!isSnappoint(bp)) {
        continue;
      }
      const key = bp.location.uri.toString();
      const ranges = byUri.get(key) ?? [];
      ranges.push(bp.location.range);
      byUri.set(key, ranges);
    }
    for (const editor of vscode.window.visibleTextEditors) {
      const ranges = byUri.get(editor.document.uri.toString()) ?? [];
      editor.setDecorations(this.decorationType, ranges);
    }
  }
}
