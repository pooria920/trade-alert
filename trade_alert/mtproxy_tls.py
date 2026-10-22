"""Fake-TLS framing over Telethon's existing encrypted MTProxy transport.

ClientHello layout adapted from Rainman69/MTPCH, commit d6a5c2991145206a3be9348681dfdf3dbebadc00.
MIT License, Copyright (c) 2026 Rainman69

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import asyncio
import hmac
import os
import time
from typing import Any

from telethon.network.connection.tcpmtproxy import (
    ConnectionTcpMTProxyRandomizedIntermediate,
)

MAX_RECORD = 16384 + 512


def client_hello(secret: bytes, domain: bytes) -> tuple[bytes, bytes, bytes]:
    session = os.urandom(32)
    prime = 2**255 - 19
    public = pow(int.from_bytes(os.urandom(32), "big") % prime, 2, prime).to_bytes(
        32, "little"
    )

    def size(n: int) -> bytes:
        return n.to_bytes(2, "big")

    head = b"".join(
        (
            bytes.fromhex("1603010200010001fc0303"),
            b"\0" * 32,
            b"\x20",
            session,
            bytes.fromhex(
                "0020fafa130113021303c02bc02fc02cc030cca9cca8c013c014009c009d002f0035010001934a4a0000"
            ),
            b"\0\0",
            size(len(domain) + 5),
            size(len(domain) + 3),
            b"\0",
            size(len(domain)),
            domain,
            bytes.fromhex(
                "00170000ff01000100000a000a0008baba001d00170018000b0002010000230000"
            ),
            b"\x00\x10\x00\x0e\x00\x0c\x02h2\x08http/1.1",
            bytes.fromhex(
                "000500050100000000000d0012001004030804040105030805050108060601001200000033002b0029baba000100001d0020"
            ),
            public,
            bytes.fromhex(
                "002d00020101002b000b0a9a9a0304030303020301001b00030200021a1a000100"
            ),
        )
    )
    padding = 517 - len(head) - 4
    if len(secret) != 16 or not domain or padding < 0:
        raise ValueError("Invalid Fake-TLS configuration")
    hello = head + b"\x00\x15" + size(padding) + b"\0" * padding
    digest = hmac.digest(secret, hello, "sha256")
    stamp = int(time.time()).to_bytes(4, "little")
    digest = digest[:28] + bytes(digest[28 + i] ^ stamp[i] for i in range(4))
    return hello[:11] + digest + hello[43:], digest, session


async def read_record(reader: Any) -> tuple[bytes, bytes]:
    header = await reader.readexactly(5)
    length = int.from_bytes(header[3:5], "big")
    if header[1:3] != b"\x03\x03" or not 0 < length <= MAX_RECORD:
        raise ConnectionError("Invalid Fake-TLS record")
    return header, await reader.readexactly(length)


async def verify_server(
    reader: Any, secret: bytes, digest: bytes, session: bytes
) -> None:
    header, body = await read_record(reader)
    if (
        header[0] != 0x16
        or len(body) < 71
        or body[0] != 2
        or int.from_bytes(body[1:4], "big") != len(body) - 4
        or body[4:6] != b"\x03\x03"
        or body[38] != 32
        or not hmac.compare_digest(body[39:71], session)
    ):
        raise ConnectionError("Invalid Fake-TLS ServerHello")
    reply = header + body
    header, body = await read_record(reader)
    if header != b"\x14\x03\x03\x00\x01" or body != b"\x01":
        raise ConnectionError("Invalid Fake-TLS ChangeCipherSpec")
    reply += header + body
    header, body = await read_record(reader)
    if header[0] != 0x17:
        raise ConnectionError("Invalid Fake-TLS handshake data")
    reply += header + body
    expected = hmac.digest(
        secret, digest + reply[:11] + b"\0" * 32 + reply[43:], "sha256"
    )
    if not hmac.compare_digest(reply[11:43], expected):
        raise ConnectionError("Fake-TLS authentication failed")


class TLSReader:
    def __init__(self, reader: Any) -> None:
        self.reader = reader
        self.pending = bytearray()

    async def readexactly(self, count: int) -> bytes:
        # ponytail: text-bot packets capped at 16 MiB; revisit before adding media support.
        if not 0 <= count <= 16 * 1024 * 1024:
            raise ConnectionError("Fake-TLS packet too large")
        while len(self.pending) < count:
            header, body = await read_record(self.reader)
            if header[0] != 0x17:
                raise ConnectionError("Unexpected Fake-TLS record")
            self.pending.extend(body)
        result = bytes(self.pending[:count])
        del self.pending[:count]
        return result


class TLSWriter:
    def __init__(self, writer: Any) -> None:
        self.writer = writer

    def write(self, data: bytes) -> None:
        for offset in range(0, len(data), 16384):
            chunk = data[offset : offset + 16384]
            self.writer.write(b"\x17\x03\x03" + len(chunk).to_bytes(2, "big") + chunk)

    async def drain(self) -> None:
        await self.writer.drain()

    def close(self) -> None:
        self.writer.close()

    async def wait_closed(self) -> None:
        await self.writer.wait_closed()


class ConnectionTcpMTProxyFakeTLS(ConnectionTcpMTProxyRandomizedIntermediate):  # type: ignore[misc]
    _reader: Any
    _writer: Any

    def __init__(
        self,
        ip: str,
        port: int,
        dc_id: int,
        *,
        loggers: Any,
        proxy: tuple[str, int, str],
        local_addr: Any = None,
    ) -> None:
        encoded = bytes.fromhex(proxy[2])
        if len(encoded) <= 17 or encoded[0] != 0xEE:
            raise ValueError("Invalid Fake-TLS secret")
        self.domain = encoded[17:]
        super().__init__(
            ip,
            port,
            dc_id,
            loggers=loggers,
            proxy=(proxy[0], proxy[1], "dd" + encoded[1:17].hex()),
            local_addr=local_addr,
        )

    async def _connect(self, timeout: float | None = None, ssl: Any = None) -> None:
        # Connect only to the configured proxy. This is not an ordinary TLS socket.
        writer = None
        try:
            async with asyncio.timeout(timeout):
                hello, digest, session = client_hello(self._secret, self.domain)
                reader, writer = await asyncio.open_connection(
                    self._ip, self._port, local_addr=self._local_addr
                )
                self._reader, self._writer = reader, writer
                writer.write(hello)
                await writer.drain()
                await verify_server(reader, self._secret, digest, session)
                self._reader, self._writer = TLSReader(reader), TLSWriter(writer)
                self._codec = self.packet_codec(self)
                self._init_conn()
                await self._writer.drain()
        except BaseException as error:
            if writer is not None:
                writer.close()
                try:
                    async with asyncio.timeout(1):
                        await writer.wait_closed()
                except OSError, TimeoutError:
                    pass
            if isinstance(error, asyncio.IncompleteReadError):
                raise ConnectionError("Truncated Fake-TLS handshake") from None
            raise
