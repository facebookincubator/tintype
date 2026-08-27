/**
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * This source code is licensed under the MIT license found in the
 * LICENSE file in the root directory of this source tree.
 *
 * @format
 */

import {execFile} from 'child_process';
import path from 'path';
import {promisify} from 'util';
import * as vscode from 'vscode';

import {
  createAutoSnapshotConfigResolver,
  registerSnapshotProvider,
  registerSnappointProvider,
} from 'tintype-vscode-core';
import {extractSnappointCapture, rewriteSnappoints} from './snappoints';

const execFileAsync = promisify(execFile);
const COMMAND_PREFIX = 'tintype';
const VIEWER_DEBUG_TYPE = 'tintype';
const LIVE_DEBUG_TYPE = 'debugpy';
const CAPTURE_PROTOCOL_VERSION = 1;
const VIEWER_PROTOCOL_VERSION = 1;

type PythonExtensionApi = {
  environments?: {
    getActiveEnvironmentPath(resource?: vscode.Uri): {path: string} | undefined;
  };
};

export function activate(context: vscode.ExtensionContext): void {
  const snapshotRegistration = registerSnapshotProvider({
    commandPrefix: COMMAND_PREFIX,
    injectionDebugType: LIVE_DEBUG_TYPE,
    viewerDebugType: VIEWER_DEBUG_TYPE,
    snapshotViewId: 'tintype.snapshot-list',
    hostOptions: {
      prepareCaptureRuntime: async (_session, evaluate) => {
        await evaluate("__import__('tintype.vscode', fromlist=['session_info']).session_info()");
      },
      resolveAutoSnapshotConfig: createAutoSnapshotConfigResolver(COMMAND_PREFIX),
    },
  });

  context.subscriptions.push(
    snapshotRegistration.disposable,
    // `onLanguage:python` activates this registration in time to repaint
    // persisted snappoints when a Python editor becomes visible.
    registerSnappointProvider({
      commandPrefix: COMMAND_PREFIX,
      injectionDebugType: LIVE_DEBUG_TYPE,
      extensionPath: context.extensionPath,
      snapshotProvider: snapshotRegistration.provider,
    }),
    vscode.commands.registerCommand('tintype.openSnapshot', (uri?: vscode.Uri) =>
      openSnapshot(uri),
    ),
    vscode.debug.registerDebugAdapterDescriptorFactory(VIEWER_DEBUG_TYPE, {
      createDebugAdapterDescriptor: session => createViewerDescriptor(session),
    }),
    vscode.debug.registerDebugConfigurationProvider(VIEWER_DEBUG_TYPE, {
      resolveDebugConfiguration: (_folder, configuration) =>
        resolveViewerConfiguration(configuration),
    }),
    vscode.debug.registerDebugAdapterTrackerFactory(VIEWER_DEBUG_TYPE, {
      createDebugAdapterTracker: session => ({
        onDidSendMessage(message: unknown) {
          checkViewerProtocol(session, message);
        },
      }),
    }),
    vscode.debug.registerDebugAdapterTrackerFactory(LIVE_DEBUG_TYPE, {
      createDebugAdapterTracker: session => ({
        onWillReceiveMessage(message: unknown) {
          rewriteSnappoints(message);
        },
        onDidSendMessage(message: unknown) {
          void handleDebugpyMessage(
            session,
            message,
            snapshotRegistration.provider.adoptWorkingFile.bind(snapshotRegistration.provider),
          );
        },
      }),
    }),
  );
}

async function resolvePythonPath(resource?: vscode.Uri): Promise<string> {
  const configured = vscode.workspace.getConfiguration('tintype').get<string>('pythonPath', '');
  if (configured.trim() !== '') {
    return configured.trim();
  }

  const extension = vscode.extensions.getExtension<PythonExtensionApi>('ms-python.python');
  if (extension != null) {
    const api = extension.isActive ? extension.exports : await extension.activate();
    const environment = api.environments?.getActiveEnvironmentPath(resource);
    if (environment?.path) {
      return environment.path;
    }
  }
  return 'python';
}

async function createViewerDescriptor(
  session: vscode.DebugSession,
): Promise<vscode.DebugAdapterDescriptor> {
  const python = await resolvePythonPath(session.workspaceFolder?.uri);
  try {
    const {stdout} = await execFileAsync(python, [
      '-c',
      'import tintype.dap.session; print(tintype.dap.session.VIEWER_PROTOCOL_VERSION)',
    ]);
    if (Number.parseInt(stdout.trim(), 10) !== VIEWER_PROTOCOL_VERSION) {
      throw new Error(
        `protocol ${stdout.trim() || 'unknown'} is incompatible with protocol ${VIEWER_PROTOCOL_VERSION}`,
      );
    }
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    throw new Error(
      `Tintype is not available in ${python}. Install or upgrade the tintype package in ` +
        `that environment, then retry. ${detail}`,
    );
  }
  return new vscode.DebugAdapterExecutable(python, ['-m', 'tintype.dap.cli'], {
    cwd: session.configuration.cwd as string | undefined,
  });
}

async function resolveViewerConfiguration(
  configuration: vscode.DebugConfiguration,
): Promise<vscode.DebugConfiguration | undefined> {
  if (configuration.pytbPath == null) {
    const selected = await vscode.window.showOpenDialog({
      canSelectMany: false,
      filters: {'Tintype Snapshots': ['pytb']},
      openLabel: 'Open Snapshot',
    });
    if (selected == null || selected.length === 0) {
      return undefined;
    }
    configuration.pytbPath = selected[0].fsPath;
  }
  return configuration;
}

async function openSnapshot(uri?: vscode.Uri): Promise<void> {
  let snapshot = uri;
  if (snapshot == null) {
    const selected = await vscode.window.showOpenDialog({
      canSelectMany: false,
      filters: {'Tintype Snapshots': ['pytb']},
      openLabel: 'Open Snapshot',
    });
    snapshot = selected?.[0];
  }
  if (snapshot == null) {
    return;
  }
  await vscode.debug.startDebugging(undefined, {
    type: VIEWER_DEBUG_TYPE,
    request: 'launch',
    name: `Tintype (${path.basename(snapshot.fsPath)})`,
    pytbPath: snapshot.fsPath,
    cwd: path.dirname(snapshot.fsPath),
  });
}

async function handleDebugpyMessage(
  session: vscode.DebugSession,
  message: unknown,
  adoptWorkingFile: (
    session: vscode.DebugSession,
    workingFile: string,
    cwd: string,
  ) => Promise<void>,
): Promise<void> {
  if (message == null || typeof message !== 'object') {
    return;
  }
  const event = message as {
    type?: string;
    event?: string;
    body?: {output?: string};
  };
  if (event.type !== 'event' || event.event !== 'output' || event.body?.output == null) {
    return;
  }
  const capture = extractSnappointCapture(event.body.output);
  if (capture == null) {
    return;
  }
  if (capture.protocolVersion !== CAPTURE_PROTOCOL_VERSION) {
    void vscode.window.showErrorMessage(
      `Tintype protocol ${capture.protocolVersion} is incompatible with this extension ` +
        `(expected ${CAPTURE_PROTOCOL_VERSION}). Upgrade Tintype and the extension together.`,
    );
    return;
  }
  if (capture.error != null) {
    void vscode.window.showErrorMessage(`Tintype snappoint failed: ${capture.error}`);
    return;
  }
  if (capture.workingFile != null) {
    await adoptWorkingFile(session, capture.workingFile, capture.cwd);
  }
}

function checkViewerProtocol(session: vscode.DebugSession, message: unknown): void {
  if (message == null || typeof message !== 'object') {
    return;
  }
  const response = message as {
    type?: string;
    command?: string;
    success?: boolean;
    body?: {tintypeProtocolVersion?: number};
  };
  if (response.type !== 'response' || response.command !== 'initialize' || !response.success) {
    return;
  }
  if (response.body?.tintypeProtocolVersion !== VIEWER_PROTOCOL_VERSION) {
    void vscode.window.showErrorMessage(
      `The Tintype package used by ${session.name} is incompatible with this extension. ` +
        'Upgrade Tintype and the extension together.',
    );
  }
}
