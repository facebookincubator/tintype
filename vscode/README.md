# Tintype

## Overview

Capture Python execution snapshots without stopping the process, then inspect
threads, stack frames, variables, exceptions, and saved `.pytb` files in the
VS Code debugger.

## Requirements

- VS Code 1.96 or newer.
- Python 3.12 or newer.
- A compatible `tintype` package installed in the debug target and in the
  interpreter selected by `tintype.pythonPath` (or the active Python extension
  environment).
- `debugpy` for live capture and snappoints.

The extension never installs Python packages automatically.

## Configure the snapshot interpreter

By default, the extension uses the active environment selected by the Python
extension. Set `tintype.pythonPath` when snapshots should be opened with a
different interpreter:

```json
{
  "tintype.pythonPath": "/path/to/python"
}
```

That interpreter must have a compatible `tintype` package installed.

## Inspect a saved snapshot

Right-click a `.pytb` file in the Explorer and select **Open Tintype Snapshot**,
or run the same command from the Command Palette. The snapshot opens as a debug
session so threads, frames, exceptions, and variables can be inspected through
the standard VS Code debugger views.

## Capture from a live program

Start a Python debug session backed by `debugpy`, then run **Tintype: Take
Tintype Snapshot** or use the camera button in the debug toolbar. Captures open
in a Tintype viewer and appear in the **Tintype Snapshots** view. Use the save
button beside a live capture to write a finalized `.pytb` file.

A snappoint captures execution when a Python source line is reached without
leaving the program stopped. Press `Shift+F9` or use the editor line-number
context menu to toggle one.

Live capture currently requires the debug target and the VS Code extension host
to share a filesystem. Remote-host debugging is not supported.

## Commands

| Command | Purpose |
|---------|---------|
| **Open Tintype Snapshot** | Open a saved `.pytb` file in the snapshot debugger. |
| **Take Tintype Snapshot** | Capture the program in the active `debugpy` session. |
| **Jump to Snapshot** | Navigate a viewer to a selected capture. |
| **Refresh Snapshot List** | Refresh the snapshots shown for a live capture session. |
| **Jump to Last Snapshot** | Navigate a viewer to its newest capture. |
| **Take Snapshot on Parent** | Capture again from the live program associated with a viewer. |
| **Save Tintype Snapshot File...** | Finalize and save a live capture as a `.pytb` file. |
| **Toggle Snappoint** | Add or remove a snappoint on the selected Python line. |
| **Add Snappoint** | Add a snappoint on the selected Python line. |
| **Remove Snappoint** | Remove a snappoint from the selected Python line. |

## On Call

Report issues in the [Tintype GitHub repository](https://github.com/facebookincubator/tintype/issues).

## Contributors

Maintained by the Tintype contributors at Meta Platforms, Inc.

## Development

Install Node.js 20, then install the pinned dependencies and run the standalone
checks from this directory:

```bash
npm ci
npm run build
npm test
npm run package
```

The extension bundle and packaged VSIX are written to `dist/`.
