# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""Tests for tintype.dap.transport.

Exercises the DAP Content-Length framing end-to-end: encode produces a
standard-compliant header; decode handles contiguous reads, partial
reads (one byte at a time), and malformed input.
"""

from __future__ import annotations

import io
import json
import socket
import unittest
from typing import Any

from tintype.dap.server import serve
from tintype.dap.transport import (
    encode_message,
    read_message,
    TransportClosed,
    TransportError,
    write_message,
)


class OneByteAtATimeStream(io.RawIOBase):
    """Simulates the adversarial case where ``read(n)`` only returns 1 byte."""

    def __init__(self, data: bytes) -> None:
        super().__init__()
        self._data = data
        self._pos = 0

    def readable(self) -> bool:
        return True

    # pyre-ignore[14]
    def read(self, size: int = -1) -> bytes:
        if self._pos >= len(self._data):
            return b""
        chunk = self._data[self._pos : self._pos + 1]
        self._pos += 1
        return chunk


class EncodeMessageTest(unittest.TestCase):
    def test_encodes_standard_framing(self) -> None:
        msg = {"seq": 1, "type": "event", "event": "hello"}
        wire = encode_message(msg)
        self.assertIn(b"Content-Length: ", wire)
        self.assertIn(b"\r\n\r\n", wire)
        # Standard-compliant: the header uses the exact capitalization expected by
        # the DAP spec and by Dapper's proxy.
        self.assertTrue(wire.startswith(b"Content-Length: "))

    def test_roundtrip_bytes(self) -> None:
        msg = {"seq": 7, "arguments": {"nested": {"a": 1, "b": "two"}}}
        wire = encode_message(msg)
        self.assertTrue(wire.endswith(json.dumps(msg).encode("utf-8")))


class ReadMessageTest(unittest.TestCase):
    def _stream_for(self, messages: list[dict[str, Any]]) -> io.BytesIO:
        buf = io.BytesIO()
        for m in messages:
            buf.write(encode_message(m))
        buf.seek(0)
        return buf

    def test_reads_single_message(self) -> None:
        stream = self._stream_for([{"seq": 1, "command": "initialize"}])
        msg = read_message(stream)
        self.assertEqual(msg, {"seq": 1, "command": "initialize"})

    def test_reads_multiple_sequential_messages(self) -> None:
        expected = [{"seq": 1}, {"seq": 2, "foo": "bar"}, {"seq": 3, "x": [1, 2, 3]}]
        stream = self._stream_for(expected)
        for m in expected:
            self.assertEqual(read_message(stream), m)

    def test_handles_partial_reads(self) -> None:
        """Transport must buffer across ``read`` calls that return less than requested."""
        payload = {"seq": 42, "command": "stackTrace"}
        stream = OneByteAtATimeStream(encode_message(payload))
        self.assertEqual(read_message(stream), payload)

    def test_case_insensitive_header(self) -> None:
        body = json.dumps({"ok": True}).encode("utf-8")
        wire = b"content-length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        self.assertEqual(read_message(io.BytesIO(wire)), {"ok": True})

    def test_missing_content_length_raises(self) -> None:
        wire = b"X-Something: 1\r\n\r\n{}"
        with self.assertRaises(TransportError):
            read_message(io.BytesIO(wire))

    def test_closed_stream_raises_transport_closed(self) -> None:
        with self.assertRaises(TransportClosed):
            read_message(io.BytesIO(b""))

    def test_malformed_content_length_raises(self) -> None:
        wire = b"Content-Length: abc\r\n\r\n"
        with self.assertRaises(TransportError):
            read_message(io.BytesIO(wire))

    def test_truncated_body_raises(self) -> None:
        body = b'{"ok": true}'
        # Advertise more body than we'll supply.
        wire = b"Content-Length: 100\r\n\r\n" + body
        with self.assertRaises(TransportError):
            read_message(io.BytesIO(wire))


class WriteMessageTest(unittest.TestCase):
    def test_writes_and_flushes(self) -> None:
        class FlushTrackingBuffer(io.BytesIO):
            def __init__(self) -> None:
                super().__init__()
                self.flushed: int = 0

            def flush(self) -> None:
                self.flushed += 1

        stream = FlushTrackingBuffer()
        write_message(stream, {"seq": 1, "type": "event"})
        self.assertGreaterEqual(stream.flushed, 1)
        stream.seek(0)
        self.assertEqual(read_message(stream), {"seq": 1, "type": "event"})


class ServeAcceptTimeoutTest(unittest.TestCase):
    """Integration-style tests around :func:`serve`'s announcement protocol."""

    def _parse_announcements(self, buf_text: str) -> list[dict[str, Any]]:
        events = []
        for line in buf_text.splitlines():
            if not line.startswith("TINTYPE_DAP "):
                continue
            events.append(json.loads(line[len("TINTYPE_DAP ") :]))
        return events

    def test_serve_times_out_and_returns_nonzero(self) -> None:
        port_buf = io.StringIO()
        exit_code = serve(port=0, port_stream=port_buf, accept_timeout=0.2)
        self.assertEqual(exit_code, 2)
        # The ``ready`` event fires BEFORE accept(), so it's present even
        # though the accept loop timed out.
        events = self._parse_announcements(port_buf.getvalue())
        self.assertEqual(len(events), 1)
        evt = events[0]
        self.assertEqual(evt["v"], 1)
        self.assertEqual(evt["type"], "ready")
        self.assertEqual(evt["transport"], "tcp")
        self.assertEqual(evt["host"], "127.0.0.1")
        self.assertIsInstance(evt["port"], int)
        self.assertGreater(evt["port"], 0)
        self.assertIsInstance(evt["pid"], int)

    def test_ready_event_has_strict_schema(self) -> None:
        """All v=1 ``ready`` events expose exactly this set of fields.

        Adding new fields needs a deliberate schema bump so clients can
        choose whether / when to opt in.
        """
        port_buf = io.StringIO()
        serve(port=0, port_stream=port_buf, accept_timeout=0.05)
        events = self._parse_announcements(port_buf.getvalue())
        self.assertEqual(len(events), 1)
        self.assertEqual(
            set(events[0].keys()),
            {"v", "type", "transport", "host", "port", "pid"},
        )

    def test_bind_failure_emits_structured_error(self) -> None:
        """A port collision should surface as an ``error`` announcement,
        not as an unlabeled Python traceback on stderr."""
        # Grab a port, keep it bound so ``serve()`` collides with us.
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        probe.listen(1)
        busy_port = probe.getsockname()[1]
        try:
            port_buf = io.StringIO()
            # No SO_REUSEADDR magic will save us here — the probe socket
            # is still listening, so bind() on the same port fails on
            # every platform we care about.
            exit_code = serve(
                host="127.0.0.1",
                port=busy_port,
                port_stream=port_buf,
                accept_timeout=0.01,
            )
            self.assertEqual(exit_code, 1)
            events = self._parse_announcements(port_buf.getvalue())
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["v"], 1)
            self.assertEqual(events[0]["type"], "error")
            self.assertEqual(events[0]["code"], "bind_failed")
            self.assertIn("message", events[0])
        finally:
            probe.close()


if __name__ == "__main__":
    unittest.main()
