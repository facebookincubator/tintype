# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Purpose-built DAP server for tintype `.pytb` snapshots.

Reads snapshots through :class:`tintype.SnapshotReader` and answers Debug
Adapter Protocol requests natively — no pydevd/debugpy, no Python-frame
synthesis. See :mod:`tintype.dap.server` for the entry point.
"""
