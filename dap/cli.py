# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""CLI entry point for the tintype DAP server.

Dedicated binary (``tintype_dap_server``) that serves a ``.pytb``
snapshot over the Debug Adapter Protocol.

Transport:

* **stdio (default)** — reads DAP requests from ``sys.stdin.buffer`` and
  writes responses to ``sys.stdout.buffer``. This is the standard DAP
  transport for editor-spawned adapters; it lets VS Code connect via
  ``DebugAdapterExecutable`` without any announcement protocol.
* **TCP (opt-in via ``--listen``)** — delegates to :func:`tintype.dap.server.serve`,
  binding a loopback port and announcing it on stdout using the
  ``TINTYPE_DAP `` NDJSON event protocol. Used for ad-hoc inspection
  with a standalone DAP client or for tests that want a real socket.
"""

from __future__ import annotations

import argparse
import enum
import os
import sys

from tintype.dap.server import run_session_on_stdio, serve


@enum.unique
class LauncherExitCode(enum.IntEnum):
    """Exit codes returned by the tintype DAP CLI.

    ``IntEnum`` rather than plain ``Enum`` so each value still behaves
    as a bare ``int`` when passed to ``sys.exit(...)`` or compared in
    tests — callers that want the symbolic name (e.g. for logging) can
    still reach for ``LauncherExitCode(rc).name``.
    """

    # Clean shutdown — DAP CLI finished serving its client.
    OK = 0
    # Generic failure: import error, no snapshots available,
    # unexpected downstream error, etc.
    ERROR = 1
    # Input validation failure: the ``.pytb`` file is missing, not a
    # regular file, or SnapshotReader rejected it as malformed.
    INVALID_PYTB = 2
    # SIGINT / Ctrl-C received before the session cleanly
    # disconnected — 128 + SIGINT(2) per POSIX convention.
    KEYBOARD_INTERRUPT = 130


def _parse_listen(value: str) -> tuple[str, int]:
    """Parse ``--listen`` argument into ``(host, port)``.

    Accepts ``PORT`` (binds ``127.0.0.1:PORT``) or ``HOST:PORT``. Raises
    :class:`argparse.ArgumentTypeError` on malformed input so argparse
    produces the standard ``error:`` output.
    """
    if ":" in value:
        host, _, port_str = value.rpartition(":")
        if not host:
            host = "127.0.0.1"
    else:
        host = "127.0.0.1"
        port_str = value
    try:
        port = int(port_str)
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"--listen expects PORT or HOST:PORT; got {value!r}"
        ) from e
    if port < 0 or port > 65535:
        raise argparse.ArgumentTypeError(f"--listen port out of range: {port}")
    return host, port


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tintype_dap_server",
        description=(
            "Serve a tintype snapshot (.pytb) over the Debug Adapter "
            "Protocol. Defaults to stdio; pass --listen to opt into TCP."
        ),
    )
    parser.add_argument(
        "pytb_file",
        nargs="?",
        default=None,
        help=(
            "Optional path to the .pytb snapshot file. When supplied, the CLI "
            "fast-fails with INVALID_PYTB if the path is missing or not a "
            "regular file. When omitted, all path validation is deferred to "
            "the DAP ``launch`` request's ``pytbPath`` argument."
        ),
    )
    parser.add_argument(
        "--listen",
        type=_parse_listen,
        default=None,
        metavar="[HOST:]PORT",
        help=(
            "Bind a TCP listener on the given endpoint instead of using "
            "stdio. Pass ``0`` for an ephemeral port (announced on stdout "
            "via the TINTYPE_DAP NDJSON 'ready' event). Host defaults to "
            "127.0.0.1 when omitted."
        ),
    )
    # ``parse_known_args`` tolerates unrelated flags passed through by
    # launch wrappers or debugging harnesses (e.g. ``--par``).
    args, _unknown = parser.parse_known_args(argv)
    return args


def main(argv: list[str] | None = None) -> LauncherExitCode:
    """Run the DAP server over stdio (default) or TCP (``--listen``).

    When ``pytb_file`` is supplied as a positional argument, fast-fails
    on a missing path with :attr:`LauncherExitCode.INVALID_PYTB` before
    the transport starts. When omitted, all path validation is
    deferred to the DAP ``launch`` handler — which surfaces failures
    as DAP errors the client can render cleanly via
    ``arguments.pytbPath`` — so the path can live entirely in the
    launch request body.

    Returns:
        :attr:`LauncherExitCode.OK` on clean shutdown.
        :attr:`LauncherExitCode.ERROR` when the session loop returns a
        non-zero status.
        :attr:`LauncherExitCode.INVALID_PYTB` when a ``pytb_file`` was
        supplied but does not exist or is not a regular file.
        :attr:`LauncherExitCode.KEYBOARD_INTERRUPT` on SIGINT during the
        serve loop.
    """
    args = _parse_args(argv)

    if args.pytb_file is not None and not os.path.isfile(args.pytb_file):
        print(
            f"tintype_dap_server: pytb path does not exist or is not a "
            f"file: {args.pytb_file}",
            file=sys.stderr,
        )
        return LauncherExitCode.INVALID_PYTB

    try:
        if args.listen is not None:
            host, port = args.listen
            rc = serve(host=host, port=port)
        else:
            rc = run_session_on_stdio()
    except KeyboardInterrupt:
        return LauncherExitCode.KEYBOARD_INTERRUPT

    # Map the bare ``int`` return value onto the enum so callers see a
    # uniform exit-code surface. Zero => OK; anything else => generic
    # error rather than leaking the raw value.
    return LauncherExitCode.OK if rc == 0 else LauncherExitCode.ERROR


if __name__ == "__main__":
    sys.exit(main())
