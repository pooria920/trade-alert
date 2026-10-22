"""Bounded public MTProxy discovery and sequential transport failover. No token in feeds."""

import asyncio
import ipaddress
import os
import random
import socket
import time
from types import SimpleNamespace
from typing import Any

import httpx
from telethon import functions, types
from telethon.network.connection.connection import ObfuscatedConnection
from telethon.network.connection.tcpmtproxy import (
    ConnectionTcpMTProxyRandomizedIntermediate,
)
from telethon.network.mtprotoplainsender import MTProtoPlainSender

from .config import mtproxy
from .mtproxy_tls import ConnectionTcpMTProxyFakeTLS

SOURCES = (
    "https://raw.githubusercontent.com/SoliSpirit/mtproto/master/all_proxies.txt",
    "https://raw.githubusercontent.com/tgmtproxy/telegram-mtproto-proxy-list/main/proxies.txt",
)
MAX_FEED_BYTES = 512 * 1024
MAX_PROXIES = 200


class ProxyPool:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.transport = transport
        self.items: list[tuple[str, int, str]] = []
        self.position = 0
        self.refresh_at = 0.0
        self.lock = asyncio.Lock()

    async def next(self) -> tuple[str, int, str]:
        async with self.lock:
            if time.monotonic() >= self.refresh_at:
                self.refresh_at = time.monotonic() + 60
                found: list[tuple[str, int, str]] = []
                async with httpx.AsyncClient(
                    transport=self.transport,
                    trust_env=False,
                    timeout=2,
                    follow_redirects=False,
                ) as client:
                    for source in SOURCES:
                        try:
                            count = 0
                            async with client.stream("GET", source) as response:
                                response.raise_for_status()
                                raw = bytearray()
                                async for chunk in response.aiter_bytes(
                                    chunk_size=16384
                                ):
                                    raw.extend(chunk)
                                    if len(raw) > MAX_FEED_BYTES:
                                        raise ValueError("Proxy feed too large")
                                for line in raw.decode("utf-8").splitlines():
                                    try:
                                        candidate = mtproxy(line.strip())
                                    except ValueError:
                                        continue
                                    if candidate and candidate not in found:
                                        found.append(candidate)
                                        count += 1
                                    if (
                                        count >= MAX_PROXIES // len(SOURCES)
                                        or len(found) >= MAX_PROXIES
                                    ):
                                        break
                            if len(found) >= MAX_PROXIES:
                                break
                        except httpx.HTTPError, ValueError, UnicodeError:
                            continue
                if found:
                    random.SystemRandom().shuffle(found)
                    self.items, self.position = found, 0
                    self.refresh_at = time.monotonic() + 600
            if not self.items:
                raise OSError("Automatic Telegram proxy list unavailable")
            candidate = self.items[self.position % len(self.items)]
            self.position += 1
            return candidate


async def public_address(host: str, port: int) -> str:
    """Pin a public IP before connecting; never dial feed-supplied local networks."""
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        results = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )
        addresses = [str(result[4][0]) for result in results[:32]]
    for value in addresses:
        address = ipaddress.ip_address(value)
        if address.is_global and not address.is_multicast:
            return str(address)
    raise OSError("Public Telegram proxy address required")


async def close_writer(writer: Any) -> None:
    if writer is not None:
        writer.close()
        try:
            async with asyncio.timeout(1):
                await writer.wait_closed()
        except OSError, TimeoutError:
            pass


def automatic_connection(pool: ProxyPool) -> Any:
    """One pool per bot, shared across the client's DC/reconnection transports."""

    class AutomaticMTProxy(ObfuscatedConnection):  # type: ignore[misc]
        def __init__(
            self,
            ip: str,
            port: int,
            dc_id: int,
            *,
            loggers: Any,
            proxy: Any = None,
            local_addr: Any = None,
        ) -> None:
            super().__init__(ip, port, dc_id, loggers=loggers, local_addr=local_addr)
            self.loggers = loggers

        async def _connect(self, timeout: float | None = None, ssl: Any = None) -> None:
            try:
                async with asyncio.timeout(min(timeout or 10, 10)):
                    # ponytail: three sequential candidates per native retry; no parallel scanning.
                    for _ in range(3):
                        delegate: Any = None
                        try:
                            host, port, secret = await pool.next()
                            async with asyncio.timeout(4):
                                ip = await public_address(host, port)
                                transport = (
                                    ConnectionTcpMTProxyFakeTLS
                                    if len(secret) > 34
                                    else ConnectionTcpMTProxyRandomizedIntermediate
                                )
                                delegate = transport(
                                    self._ip,
                                    self._port,
                                    self._dc_id,
                                    loggers=self.loggers,
                                    proxy=(
                                        ip,
                                        port,
                                        secret
                                        if len(secret) > 34
                                        else "dd" + secret[-32:],
                                    ),
                                    local_addr=self._local_addr,
                                )
                                await delegate._connect(timeout=4)

                                async def send(data: bytes) -> None:
                                    delegate._send(data)
                                    await delegate._writer.drain()

                                nonce = int.from_bytes(
                                    os.urandom(16), "little", signed=True
                                )
                                # Use Telethon's plain handshake before giving this socket to
                                # authentication. A TCP-open proxy alone isn't a working route.
                                probe = MTProtoPlainSender(
                                    SimpleNamespace(send=send, recv=delegate._recv),
                                    loggers=self.loggers,
                                )
                                reply = await probe.send(
                                    functions.ReqPqMultiRequest(nonce)
                                )
                                if (
                                    not isinstance(reply, types.ResPQ)
                                    or reply.nonce != nonce
                                ):
                                    raise ConnectionError("Proxy handshake rejected")
                                self._reader, self._writer = (
                                    delegate._reader,
                                    delegate._writer,
                                )
                                self._codec, self._obfuscation = (
                                    delegate._codec,
                                    delegate._obfuscation,
                                )
                                return
                        except asyncio.CancelledError:
                            if delegate:
                                await close_writer(delegate._writer)
                            raise
                        except Exception:
                            if delegate:
                                await close_writer(delegate._writer)
                    raise OSError("Automatic Telegram proxies unavailable")
            except TimeoutError:
                raise OSError("Automatic Telegram proxy connection timed out") from None

    return AutomaticMTProxy
