# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Request dispatch + sequence-numbering helpers for the DAP server.

Handlers live in :class:`tintype.dap.session.SnapshotDebugSession`. This
module wires handlers to a request/response loop and centralizes seq
bookkeeping so handlers can focus on protocol semantics.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from tintype.dap.transport import ByteStream


logger: logging.Logger = logging.getLogger(__name__)


Handler = Callable[[dict[str, Any]], dict[str, Any] | None]
"""Handler signature: ``(arguments: dict) -> body: dict | None``.

Returning ``None`` produces an empty-body response. Handlers signal
failure by raising :class:`DispatchError`. Any other exception is caught
by :class:`Dispatcher` and returned as ``success: false`` with the
error message forwarded to the client for debugging.
"""


class DispatchError(Exception):
    """Raised by a handler to request a ``success: false`` response.

    The message is sent to the client verbatim in ``response.message``.
    """


class Dispatcher:
    """Owns outgoing sequence numbers and the ``command -> handler`` table.

    The dispatcher is intentionally stateless w.r.t. snapshot navigation —
    all of that lives on :class:`SnapshotDebugSession`. We *do* hold a
    reference to the output stream because events need to be sent at any
    point, not just as responses.
    """

    def __init__(self, stream: ByteStream) -> None:
        from tintype.dap.transport import write_message  # local import avoids cycle

        self._stream = stream
        self._write_message = write_message
        self._seq: int = 0
        self._handlers: dict[str, Handler] = {}

    def register(self, command: str, handler: Handler) -> None:
        self._handlers[command] = handler

    def register_many(self, handlers: dict[str, Handler]) -> None:
        self._handlers.update(handlers)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def send_event(self, event: str, body: dict[str, Any] | None = None) -> None:
        """Send a DAP ``event`` message."""
        message: dict[str, Any] = {
            "seq": self._next_seq(),
            "type": "event",
            "event": event,
        }
        if body is not None:
            message["body"] = body
        logger.debug("send event %s body=%s", event, body)
        self._write_message(self._stream, message)

    def handle(self, request: dict[str, Any]) -> None:
        """Dispatch a single request and write the response."""
        command = request.get("command", "")
        request_seq = request.get("seq", 0)
        arguments = request.get("arguments") or {}

        response: dict[str, Any] = {
            "seq": self._next_seq(),
            "type": "response",
            "request_seq": request_seq,
            "command": command,
            "success": True,
        }

        handler = self._handlers.get(command)
        if handler is None:
            logger.info("unknown DAP command: %s", command)
            response["success"] = False
            response["message"] = (
                f"Command '{command}' is not supported by this adapter."
            )
        else:
            try:
                body = handler(arguments)
            except DispatchError as e:
                response["success"] = False
                response["message"] = str(e)
            except Exception as e:  # noqa: BLE001
                # Unhandled exceptions shouldn't kill the server — they'd
                # leave VS Code stuck spinning. Log + surface as a failure.
                logger.exception("handler for '%s' raised", command)
                response["success"] = False
                response["message"] = f"Internal adapter error: {e}"
            else:
                if body is not None:
                    response["body"] = body

        self._write_message(self._stream, response)
