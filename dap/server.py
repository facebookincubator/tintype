# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""TCP listener + message loop for the tintype DAP server.

The server binds to ``127.0.0.1:0`` (loopback only, ephemeral port) and
announces its listening endpoint on stdout using a line-oriented JSON
protocol so a spawning process can learn the port without relying on
stderr parsing.  Each announcement line is prefixed with
``TINTYPE_DAP `` so it is trivially distinguishable from unrelated
stdout noise (e.g. warnings emitted by imports before the server binds).

After the announcement the server accepts **one** DAP client connection
and serves it until the client disconnects or sends
``terminate`` / ``disconnect``.

Announcement schema (v=1):

* ``{"v":1,"type":"ready","transport":"tcp","host":"127.0.0.1",
    "port":45678,"pid":12345}`` — server has bound and is listening.
* ``{"v":1,"type":"error","code":"bind_failed","message":"..."}`` —
    server could not bind; will exit with a non-zero status. The
    spawning process uses the structured ``code``/``message`` to surface
    a clean error to the end user.

Unknown keys and unknown ``type`` values MUST be ignored by clients to
keep the protocol forward-compatible.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
from typing import Any, TextIO

from tintype.dap.dispatcher import Dispatcher
from tintype.dap.session import SnapshotDebugSession
from tintype.dap.transport import (
    BufferedSocketIO,
    ByteStream,
    read_message,
    TransportClosed,
    TransportError,
)


logger: logging.Logger = logging.getLogger(__name__)


# Each announcement line is exactly this prefix + a single JSON object +
# ``\n``.  Non-prefixed lines are treated as stdout noise by clients.
ANNOUNCEMENT_PREFIX: str = "TINTYPE_DAP "

# Schema version the server emits.  Clients should reject events with a
# different ``v`` value (forward-compat — they may keep reading in case
# a later line matches their supported schema).
ANNOUNCEMENT_SCHEMA_VERSION: int = 1

# Default ceiling for how long the server waits for a DAP client to dial
# in after announcing its port.  Beyond this we treat the spawn as
# orphaned and exit cleanly so the server can't linger as a zombie
# process when the launcher fails between ``spawn`` and ``connect``.
DEFAULT_ACCEPT_TIMEOUT_SECONDS = 60.0


def _announce(stream: TextIO, event: dict[str, Any]) -> None:
    """Emit one NDJSON announcement line on ``stream``.

    Always flushes — the spawning process relies on prompt delivery of
    the ``ready`` event to bring the session up and can't afford to be
    blocked on Python's default stdout buffering.
    """
    payload = {"v": ANNOUNCEMENT_SCHEMA_VERSION, **event}
    stream.write(ANNOUNCEMENT_PREFIX + json.dumps(payload) + "\n")
    stream.flush()


def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    port_stream: TextIO | None = None,
    accept_timeout: float | None = DEFAULT_ACCEPT_TIMEOUT_SECONDS,
) -> int:
    """Start the DAP server and run one session to completion.

    ``host`` is hard-pinned to IPv4 loopback by default because Dapper's
    proxy normalizes ``::`` / ``::1`` to ``127.0.0.1``; binding to an
    IPv6 address causes the proxy to fail the connection on some
    platforms.

    ``accept_timeout`` bounds how long we wait for a DAP client to dial
    in after announcing the endpoint. Defensive backstop: if the
    launcher spawning us fails between reading the ``ready`` event and
    connecting, we would otherwise sit in ``accept()`` forever. Pass
    ``None`` to block indefinitely.

    Returns the process exit code.
    """
    announce = port_stream if port_stream is not None else sys.stdout

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        try:
            server_sock.bind((host, port))
        except OSError as e:
            # Structured error so the spawner can surface a clean message
            # instead of grepping stderr.
            _announce(
                announce,
                {"type": "error", "code": "bind_failed", "message": str(e)},
            )
            server_sock.close()
            return 1

        server_sock.listen(1)  # listen BEFORE announcing so connect() always wins
        actual_host, actual_port = server_sock.getsockname()[:2]

        _announce(
            announce,
            {
                "type": "ready",
                "transport": "tcp",
                "host": actual_host,
                "port": int(actual_port),
                "pid": os.getpid(),
            },
        )
        logger.info(
            "tintype DAP server listening on %s:%s (accept_timeout=%s)",
            actual_host,
            actual_port,
            accept_timeout,
        )

        if accept_timeout is not None:
            server_sock.settimeout(accept_timeout)
        try:
            client_sock, _peer = server_sock.accept()
        except socket.timeout:
            logger.warning(
                "no DAP client connected within %.1fs; exiting",
                accept_timeout,
            )
            return 2
        # Remove the timeout from the accepted socket — it inherits the
        # listener's timeout on some platforms and we want blocking reads
        # during the session.
        client_sock.settimeout(None)
    except Exception:
        server_sock.close()
        raise

    # Done with the listener — we only ever serve one client.
    server_sock.close()

    try:
        return _run_session(client_sock)
    finally:
        try:
            client_sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        client_sock.close()


def _run_session(client_sock: socket.socket) -> int:
    stream = BufferedSocketIO(client_sock)
    return run_session_on_stream(stream)


def run_session_on_stream(stream: ByteStream) -> int:
    """Run the request/response loop against an already-established stream.

    Exposed for tests: they can drive the server over an in-memory pipe
    instead of a real socket.
    """
    dispatcher = Dispatcher(stream)
    session = SnapshotDebugSession(dispatcher)
    session.wire()

    try:
        while not session.terminated:
            try:
                message = read_message(stream)
            except TransportClosed:
                logger.info("peer closed connection")
                return 0
            except TransportError as e:
                logger.error("transport error: %s", e)
                return 1
            except json.JSONDecodeError as e:
                logger.error("malformed DAP body: %s", e)
                return 1

            if message.get("type") != "request":
                # DAP clients only send requests; ignore anything else but log it.
                logger.warning("ignoring non-request message: %s", message)
                continue

            dispatcher.handle(message)
        return 0
    finally:
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass
