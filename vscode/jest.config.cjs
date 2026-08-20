/**
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * This source code is licensed under the MIT license found in the
 * LICENSE file in the root directory of this source tree.
 */

module.exports = {
  clearMocks: true,
  moduleNameMapper: {
    '^tintype-vscode-core$': '<rootDir>/core/src/index.tsx',
    '^tintype-vscode-core/(.*)$': '<rootDir>/core/src/$1',
    '^vscode$': '<rootDir>/__mocks__/vscode.ts',
  },
  preset: 'ts-jest',
  testEnvironment: 'node',
  testMatch: ['**/__tests__/**/*.test.tsx'],
};
