# Tintype

## Overview

Capture Python execution snapshots without stopping the process, then inspect
threads, stack frames, variables, exceptions, and saved `.pytb` files in the
VS Code debugger.

## Features

### Requirements

- Python 3.12 or newer.
- A compatible `tintype` package installed in the debug target and in the
  interpreter selected by `tintype.pythonPath` (or the active Python extension
  environment).
- `debugpy` for live capture and snappoints.

The extension never installs Python packages automatically.

The public extension can be installed alongside Python Debugger (Meta). Public
live capture observes `debugpy` sessions and uses the `tintype` snapshot viewer;
the Meta extension owns `meta-python` sessions and its separate `tintype-meta`
viewer.

### Usage

- Open a saved `.pytb` file from the Explorer context menu.
- During a supported Python launch session, run **Tintype: Take Tintype
  Snapshot**.
- Add a snappoint with `Shift+F9`; execution continues after the snapshot is
  captured.
- Save a live snapshot file from the Tintype Snapshots view.

## On Call

Report issues through the Tintype GitHub repository.

## Contributors

Maintained by the Tintype contributors at Meta Platforms, Inc.

## Development

Install the pinned dependencies and run the standalone checks from this
directory:

```bash
npm ci
npm run build
npm test
npm run package
```

The extension bundle and packaged VSIX are written to `dist/`.
