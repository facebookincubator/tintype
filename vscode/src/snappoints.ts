/**
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * This source code is licensed under the MIT license found in the
 * LICENSE file in the root directory of this source tree.
 *
 * @format
 */

import {SNAPPOINT_LOG_MESSAGE_MARKER} from 'tintype-vscode-core/snappoint';

export const SNAPPOINT_EVENT_PREFIX = '__tintype_snappoint_event_6c7746df__';
export const SNAPPOINT_EVENT_SUFFIX = '__tintype_snappoint_event_end__';

export type SnappointCapturePayload = {
  protocolVersion: number;
  pid: number;
  cwd: string;
  workingFile?: string;
  captured?: boolean;
  eventSequence?: number;
  error?: string;
};

export function rewriteSnappoints(message: unknown): void {
  if (message == null || typeof message !== 'object') {
    return;
  }
  const request = message as {
    type?: string;
    command?: string;
    arguments?: {breakpoints?: Array<{condition?: string; logMessage?: string}>};
  };
  if (request.type !== 'request' || request.command !== 'setBreakpoints') {
    return;
  }
  for (const breakpoint of request.arguments?.breakpoints ?? []) {
    if (breakpoint.logMessage !== SNAPPOINT_LOG_MESSAGE_MARKER) {
      continue;
    }
    const event = "__import__('tintype.vscode', fromlist=['snappoint_event']).snappoint_event()";
    const serialized = `__import__('json').dumps(${event}, separators=(',', ':'))`;
    breakpoint.logMessage = `{('${SNAPPOINT_EVENT_PREFIX}' + ${serialized} + '${SNAPPOINT_EVENT_SUFFIX}')}`;
  }
}

export function extractSnappointCapture(output: string): SnappointCapturePayload | null {
  const start = output.indexOf(SNAPPOINT_EVENT_PREFIX);
  if (start < 0) {
    return null;
  }
  const payloadStart = start + SNAPPOINT_EVENT_PREFIX.length;
  const end = output.indexOf(SNAPPOINT_EVENT_SUFFIX, payloadStart);
  if (end < 0) {
    return null;
  }
  try {
    const payload = JSON.parse(output.slice(payloadStart, end)) as Partial<SnappointCapturePayload>;
    if (
      typeof payload.protocolVersion !== 'number' ||
      typeof payload.pid !== 'number' ||
      typeof payload.cwd !== 'string'
    ) {
      return null;
    }
    return payload as SnappointCapturePayload;
  } catch {
    return null;
  }
}
