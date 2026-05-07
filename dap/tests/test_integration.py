# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""End-to-end integration test for tintype.dap.

Creates a real ``.pytb`` file via :func:`tintype.take_snapshot`, runs the
full DAP server against it, and drives a complete request sequence over
a TCP socket. This is the closest we can get in a unit test to the path
VS Code + Dapper exercise in production.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from typing import Any, Iterator

import tintype
from tintype.dap.server import run_session_on_stream, serve
from tintype.dap.transport import BufferedSocketIO, read_message, write_message


def _make_snapshot_file(path: str) -> None:
    """Capture a non-trivial snapshot with multiple locals + an exception."""
    tintype.initialize()

    def leaf(x: int, payload: dict[str, Any]) -> None:
        _local_note = "from-leaf"  # noqa: F841  — keep as a frame local
        # Capture while we have a realistic call stack with nested objects
        # in locals.
        tintype.take_snapshot()

    def middle(things: list[int]) -> None:
        note = {"key": "value", "count": len(things)}
        leaf(things[0], note)

    middle([1, 2, 3])

    # Also capture an exception so exceptionInfo can be exercised downstream.
    try:
        raise ValueError("integration-test-exception")
    except ValueError as e:
        tintype.take_snapshot(e)

    tintype.finalize(path)


@contextlib.contextmanager
def _serve_in_thread(pytb_path: str) -> Iterator[int]:
    """Start the DAP server in a background thread, yield the listening port."""
    port_holder: dict[str, int] = {}
    port_ready = threading.Event()

    class CaptureStdout(io.StringIO):
        def write(self, s: str) -> int:
            # The server writes one NDJSON status line per announcement
            # with the ``TINTYPE_DAP `` prefix. We only care about the
            # ``ready`` event here; other events (e.g. ``error``) flow
            # through to the fallback "server failed to start" branch.
            if s.startswith("TINTYPE_DAP "):
                try:
                    evt = json.loads(s[len("TINTYPE_DAP ") :])
                    if evt.get("v") == 1 and evt.get("type") == "ready":
                        port_holder["port"] = int(evt["port"])
                        port_ready.set()
                except (ValueError, KeyError):
                    pass
            return super().write(s)

    capture = CaptureStdout()

    def target() -> None:
        try:
            serve(host="127.0.0.1", port=0, port_stream=capture)
        except Exception:  # noqa: BLE001
            port_ready.set()  # unblock waiters on error

    thread = threading.Thread(target=target, daemon=True)
    thread.start()

    if not port_ready.wait(timeout=10):
        raise RuntimeError("DAP server did not announce a port within 10s")
    port = port_holder.get("port")
    if port is None:
        raise RuntimeError(
            f"DAP server failed to start; stdout: {capture.getvalue()!r}"
        )

    try:
        yield port
    finally:
        thread.join(timeout=5)


class IntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._pytb_path = os.path.join(self._tmpdir.name, "test.pytb")
        _make_snapshot_file(self._pytb_path)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _connect(self, port: int, timeout: float = 5.0) -> socket.socket:
        deadline = time.monotonic() + timeout
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.connect(("127.0.0.1", port))
                return s
            except OSError as e:
                last_err = e
                time.sleep(0.05)
        raise RuntimeError(f"failed to connect within {timeout}s: {last_err!r}")

    def test_full_dap_sequence(self) -> None:
        with _serve_in_thread(self._pytb_path) as port:
            sock = self._connect(port)
            stream = BufferedSocketIO(sock)

            seq = [0]

            def request(
                command: str, arguments: dict[str, Any] | None = None
            ) -> dict[str, Any]:
                seq[0] += 1
                write_message(
                    stream,
                    {
                        "seq": seq[0],
                        "type": "request",
                        "command": command,
                        "arguments": arguments or {},
                    },
                )
                # Consume until we get the response for this command.
                while True:
                    msg = read_message(stream)
                    if (
                        msg.get("type") == "response"
                        and msg.get("request_seq") == seq[0]
                    ):
                        return msg

            init = request("initialize", {})
            self.assertTrue(init["success"])
            self.assertTrue(init["body"]["supportsStepBack"])

            launch = request("launch", {"pytbPath": self._pytb_path})
            self.assertTrue(launch["success"])

            threads_resp = request("threads")
            self.assertTrue(threads_resp["success"])
            self.assertGreater(len(threads_resp["body"]["threads"]), 0)
            thread_id = threads_resp["body"]["threads"][0]["id"]

            stack = request("stackTrace", {"threadId": thread_id})
            self.assertTrue(stack["success"])
            self.assertGreater(len(stack["body"]["stackFrames"]), 0)
            frame_id = stack["body"]["stackFrames"][0]["id"]

            scopes = request("scopes", {"frameId": frame_id})
            self.assertTrue(scopes["success"])
            scope_ref = scopes["body"]["scopes"][0]["variablesReference"]

            variables = request("variables", {"variablesReference": scope_ref})
            self.assertTrue(variables["success"])
            # The leaf() frame sets ``_local_note = "from-leaf"`` — assert we
            # see *some* locals without making exact assertions that would
            # be brittle against snapshot layout changes.
            self.assertGreater(len(variables["body"]["variables"]), 0)

            # Navigate: continue → next snapshot (the exception one).
            cont = request("continue")
            self.assertTrue(cont["success"])

            # Terminate cleanly.
            term = request("disconnect")
            self.assertTrue(term["success"])

            stream.close()


class StdioIntegrationTest(unittest.TestCase):
    """Drive :func:`run_session_on_stream` directly, no TCP involved.

    Uses a ``socketpair`` to give both sides a real bidirectional byte
    stream. Mirrors the happy-path of ``test_full_dap_sequence`` (the
    TCP variant) but exercises the transport-agnostic ``ByteStream``
    path that :func:`run_session_on_stdio` also uses against
    ``sys.stdin.buffer`` / ``sys.stdout.buffer``. Keeps the test fast
    and deterministic without binding a real TCP port.
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._pytb_path = os.path.join(self._tmpdir.name, "test.pytb")
        _make_snapshot_file(self._pytb_path)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_initialize_launch_disconnect_over_stream(self) -> None:
        server_sock, client_sock = socket.socketpair()
        server_stream = BufferedSocketIO(server_sock)
        client_stream = BufferedSocketIO(client_sock)

        server_exit: dict[str, int] = {}

        def target() -> None:
            server_exit["rc"] = run_session_on_stream(server_stream)

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        try:
            seq = [0]

            def request(
                command: str, arguments: dict[str, Any] | None = None
            ) -> dict[str, Any]:
                seq[0] += 1
                write_message(
                    client_stream,
                    {
                        "seq": seq[0],
                        "type": "request",
                        "command": command,
                        "arguments": arguments or {},
                    },
                )
                while True:
                    msg = read_message(client_stream)
                    if (
                        msg.get("type") == "response"
                        and msg.get("request_seq") == seq[0]
                    ):
                        return msg

            init = request("initialize", {})
            self.assertTrue(init["success"])

            launch = request("launch", {"pytbPath": self._pytb_path})
            self.assertTrue(launch["success"])

            # Disconnect terminates the session cleanly.
            term = request("disconnect")
            self.assertTrue(term["success"])
        finally:
            client_stream.close()
            thread.join(timeout=5)

        self.assertEqual(server_exit.get("rc"), 0)


if __name__ == "__main__":
    unittest.main()
