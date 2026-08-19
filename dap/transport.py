# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

"""DAP wire-format framing.

The Debug Adapter Protocol wraps each JSON message in a
``Content-Length: N\\r\\n\\r\\n`` header, followed by *N* bytes of UTF-8
encoded JSON body. This module implements the encode / decode primitives.

We must emit the standard framing verbatim — Dapper's proxy rejects
messages that use non-standard headers or capitalization.
"""

from __future__ import annotations

import io
import json
import logging
from typing import Any, Protocol


logger: logging.Logger = logging.getLogger(__name__)

_HEADER_SEP = b"\r\n\r\n"


class ByteStream(Protocol):
    """Minimal structural type for the read / write side of a DAP transport.

    We type on this rather than :class:`typing.BinaryIO` because the
    production ``BufferedSocketIO`` adapter subclasses
    :class:`io.RawIOBase` (which does not formally satisfy ``BinaryIO``),
    while tests fake the transport with :class:`io.BytesIO` and other
    duck-compatible classes. Protocol typing accepts both.
    """

    def read(self, size: int = ..., /) -> bytes: ...
    def write(self, data: bytes, /) -> int: ...
    def flush(self) -> None: ...
    def close(self) -> None: ...


class TransportError(Exception):
    """Raised when the transport stream is unrecoverable."""


class TransportClosed(TransportError):
    """Raised when the peer cleanly closed the stream."""


def encode_message(message: dict[str, Any]) -> bytes:
    """Encode a DAP message dict into the standard ``Content-Length`` frame."""
    body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


def write_message(stream: ByteStream, message: dict[str, Any]) -> None:
    """Write a single DAP message to ``stream``."""
    stream.write(encode_message(message))
    stream.flush()


def _read_exact(stream: ByteStream, n: int) -> bytes:
    """Read exactly *n* bytes, handling short reads. Raises if stream closes."""
    if n == 0:
        return b""
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            if not buf:
                raise TransportClosed("peer closed connection")
            raise TransportError(
                f"stream closed while expecting {n - len(buf)} more bytes"
            )
        buf.extend(chunk)
    return bytes(buf)


def _read_until(stream: ByteStream, sep: bytes, *, max_bytes: int = 8192) -> bytes:
    """Read bytes until ``sep`` is found. Returns everything including ``sep``."""
    buf = bytearray()
    while True:
        if len(buf) > max_bytes:
            raise TransportError(f"header exceeded {max_bytes} bytes without {sep!r}")
        byte = stream.read(1)
        if not byte:
            if not buf:
                raise TransportClosed("peer closed connection")
            raise TransportError("stream closed while reading header")
        buf.extend(byte)
        if buf.endswith(sep):
            return bytes(buf)


def read_message(stream: ByteStream) -> dict[str, Any]:
    """Read a single DAP message from ``stream`` and return it as a dict.

    Raises:
        TransportClosed: If the peer cleanly closed the stream.
        TransportError: If the framing is malformed.
        json.JSONDecodeError: If the body is not valid JSON.
    """
    header = _read_until(stream, _HEADER_SEP)
    # Strip the trailing separator and split into individual header lines.
    header_block = header[: -len(_HEADER_SEP)]
    content_length: int | None = None
    for line in header_block.split(b"\r\n"):
        if not line:
            continue
        if line.lower().startswith(b"content-length:"):
            try:
                content_length = int(line.split(b":", 1)[1].strip())
            except ValueError as e:
                raise TransportError(f"invalid Content-Length: {line!r}") from e

    if content_length is None:
        raise TransportError(f"missing Content-Length header in: {header!r}")
    if content_length < 0:
        raise TransportError(f"negative Content-Length: {content_length}")

    body = _read_exact(stream, content_length)
    return json.loads(body.decode("utf-8"))


class BufferedSocketIO(io.RawIOBase):
    """Thin ``BinaryIO``-compatible wrapper around a connected socket.

    The stdlib ``socket.makefile("rwb")`` already supports this pattern but
    forces us to juggle separate read/write buffers. This adapter keeps the
    code in :func:`read_message` / :func:`write_message` symmetrical and
    makes it trivial to fake in tests.

    Reads are internally buffered so the DAP header-reader loop
    (:func:`_read_until`, which reads one byte at a time until ``\r\n\r\n``)
    doesn't incur a ``recv`` syscall per byte.  One ``recv`` of
    :attr:`_READ_CHUNK_SIZE` typically picks up the whole header and the
    first bytes of the body in a single kernel transition, and subsequent
    single-byte reads are served from memory.
    """

    # Size of each ``recv`` when the internal buffer is empty.  Keep this
    # modest: DAP headers are < 64 bytes and bodies arrive framed, so a
    # larger chunk doesn't help and wastes memory on small messages.
    _READ_CHUNK_SIZE: int = 4096

    def __init__(self, sock: object) -> None:
        super().__init__()
        self._sock = sock
        self._read_buf: bytearray = bytearray()

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return True

    # pyre-ignore[14] — RawIOBase signature allows None return
    def read(self, size: int = -1) -> bytes:
        if size < 0:
            # DAP messages are always framed, so an open-ended read is a bug.
            raise ValueError("BufferedSocketIO.read requires a positive size")
        if size == 0:
            return b""
        if not self._read_buf:
            # pyre-ignore[16] — runtime duck-typing
            chunk = self._sock.recv(self._READ_CHUNK_SIZE)
            if not chunk:
                # EOF — let the caller's short-read handling (see
                # ``_read_exact`` / ``_read_until``) raise the
                # appropriate ``TransportClosed`` / ``TransportError``.
                return b""
            self._read_buf.extend(chunk)
        take = min(size, len(self._read_buf))
        out = bytes(self._read_buf[:take])
        del self._read_buf[:take]
        return out

    def write(self, data: bytes) -> int:  # pyre-ignore[14]
        # pyre-ignore[16] — runtime duck-typing
        self._sock.sendall(data)
        return len(data)

    def close(self) -> None:
        try:
            # pyre-ignore[16]
            self._sock.close()
        except OSError:
            pass
        super().close()
