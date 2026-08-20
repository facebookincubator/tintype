/**
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * This source code is licensed under the MIT license found in the
 * LICENSE file in the root directory of this source tree.
 *
 * @format
 */

import {
  SNAPPOINT_EVENT_PREFIX,
  SNAPPOINT_EVENT_SUFFIX,
  extractSnappointCapture,
  rewriteSnappoints,
} from '../src/snappoints';

describe('rewriteSnappoints', () => {
  it('rewrites a snappoint marker into a debugpy logpoint payload', () => {
    const breakpoint = {logMessage: '__tintype_snappoint__'};
    rewriteSnappoints({
      type: 'request',
      command: 'setBreakpoints',
      arguments: {breakpoints: [breakpoint]},
    });

    expect(breakpoint.logMessage).toContain(SNAPPOINT_EVENT_PREFIX);
    expect(breakpoint.logMessage).toContain(SNAPPOINT_EVENT_SUFFIX);
    expect(breakpoint.logMessage).toContain("fromlist=['snappoint_event']");
  });

  it('preserves an existing user condition', () => {
    const breakpoint = {
      condition: 'attempts > 2',
      logMessage: '__tintype_snappoint__',
    };
    rewriteSnappoints({
      type: 'request',
      command: 'setBreakpoints',
      arguments: {breakpoints: [breakpoint]},
    });

    expect(breakpoint.condition).toBe('attempts > 2');
    expect(breakpoint.logMessage).toContain(SNAPPOINT_EVENT_PREFIX);
  });

  it('leaves ordinary breakpoints unchanged', () => {
    const breakpoint = {condition: 'ready'};
    rewriteSnappoints({
      type: 'request',
      command: 'setBreakpoints',
      arguments: {breakpoints: [breakpoint]},
    });

    expect(breakpoint).toEqual({condition: 'ready'});
  });
});

describe('extractSnappointCapture', () => {
  it('extracts a capture result from debugpy output', () => {
    const payload = {
      protocolVersion: 1,
      pid: 42,
      cwd: '/work',
      workingFile: '/tmp/live.pytb',
      captured: true,
      eventSequence: 3,
    };
    expect(
      extractSnappointCapture(
        `prefix ${SNAPPOINT_EVENT_PREFIX}${JSON.stringify(payload)}${SNAPPOINT_EVENT_SUFFIX}\n`,
      ),
    ).toEqual(payload);
  });

  it('rejects incomplete or malformed payloads', () => {
    expect(extractSnappointCapture('ordinary output')).toBeNull();
    expect(
      extractSnappointCapture(`${SNAPPOINT_EVENT_PREFIX}{${SNAPPOINT_EVENT_SUFFIX}`),
    ).toBeNull();
    expect(
      extractSnappointCapture(
        `${SNAPPOINT_EVENT_PREFIX}{"protocolVersion":1}${SNAPPOINT_EVENT_SUFFIX}`,
      ),
    ).toBeNull();
  });
});
