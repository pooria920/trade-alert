"""Automatic proxy discovery/rotation with fake HTTP, DNS and socket transports."""

import asyncio
import logging
import struct
import time
import unittest
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from telethon import types
from telethon.extensions import BinaryReader

from trade_alert.config import parse_env
from trade_alert.mtproto import TelegramAPI, TelegramError
from trade_alert.proxy_auto import (
    MAX_FEED_BYTES,
    SOURCES,
    ProxyPool,
    automatic_connection,
    public_address,
)

LINK = "tg://proxy?server=8.8.8.8&port=443&secret=" + "01" * 16
ENV = dict(
    TELEGRAM_BOT_TOKEN="123:" + "x" * 35,
    TELEGRAM_API_ID="123",
    TELEGRAM_API_HASH="a" * 32,
    TELEGRAM_ALLOWED_USER_IDS="1",
)


class AutomaticProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_closes_the_inflight_proxy_socket(self):
        pool = ProxyPool()
        pool.items = [("8.8.8.8", 443, "01" * 16)]
        pool.refresh_at = time.monotonic() + 600
        started = asyncio.Event()
        writer = Mock(wait_closed=AsyncMock())

        class BlockedDelegate:
            def __init__(self, *args, **kwargs):
                self._writer = writer

            async def _connect(self, **kwargs):
                started.set()
                await asyncio.Future()

        with patch(
            "trade_alert.proxy_auto.ConnectionTcpMTProxyRandomizedIntermediate",
            BlockedDelegate,
        ):
            connection = automatic_connection(pool)(
                "149.154.167.51",
                443,
                2,
                loggers=defaultdict(lambda: logging.getLogger("test")),
            )
            task = asyncio.create_task(connection._connect(timeout=10))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        writer.close.assert_called_once()
        writer.wait_closed.assert_awaited_once()

    async def test_feed_bounds_fallback_dedup_cache_and_no_credentials(self):
        requests = []

        def fetch(request):
            requests.append(request)
            if str(request.url) == SOURCES[0]:
                return httpx.Response(503)
            return httpx.Response(200, text="invalid\n" + LINK + "\n" + LINK)

        pool = ProxyPool(transport=httpx.MockTransport(fetch))
        self.assertEqual(await pool.next(), ("8.8.8.8", 443, "01" * 16))
        self.assertEqual(await pool.next(), ("8.8.8.8", 443, "01" * 16))
        self.assertEqual(len(requests), 2)
        for request in requests:
            self.assertNotIn("authorization", request.headers)
            self.assertNotIn(ENV["TELEGRAM_BOT_TOKEN"], str(request.url))
        pool.refresh_at = 0
        await pool.next()
        self.assertEqual(len(requests), 4)
        bad = ProxyPool(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, content=b"x" * (MAX_FEED_BYTES + 1))
            )
        )
        with self.assertRaisesRegex(OSError, "list unavailable"):
            await bad.next()

    async def test_public_dns_is_pinned_and_private_addresses_rejected(self):
        for host in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "224.0.0.1"):
            with self.assertRaises(OSError):
                await public_address(host, 443)
        self.assertEqual(await public_address("8.8.8.8", 443), "8.8.8.8")
        with patch.object(
            asyncio.get_running_loop(),
            "getaddrinfo",
            AsyncMock(
                return_value=[
                    (2, 1, 6, "", ("127.0.0.1", 443)),
                    (2, 1, 6, "", ("8.8.4.4", 443)),
                ]
            ),
        ) as dns:
            self.assertEqual(await public_address("proxy.example", 443), "8.8.4.4")
            dns.assert_awaited_once()

    async def test_rotating_transport_probes_before_adopting_socket_and_closes_failures(
        self,
    ):
        pool = ProxyPool()
        pool.items = [("8.8.8.8", 443, "01" * 16), ("8.8.4.4", 443, "02" * 16)]
        pool.refresh_at = time.monotonic() + 600
        attempts = []

        class Delegate:
            def __init__(self, *args, **kwargs):
                self.proxy = kwargs["proxy"]
                self._writer = Mock(drain=AsyncMock(), wait_closed=AsyncMock())
                self._reader, self._codec, self._obfuscation = (
                    object(),
                    object(),
                    object(),
                )
                attempts.append(self)

            async def _connect(self, **kwargs):
                if self.proxy[0] == "8.8.8.8":
                    raise OSError("synthetic failure")

            def _send(self, data):
                with BinaryReader(data[20:]) as reader:
                    self.nonce = reader.tgread_object().nonce

            async def _recv(self):
                body = bytes(types.ResPQ(self.nonce, 1, b"pq", []))
                return struct.pack("<qqi", 0, 1, len(body)) + body

        with patch(
            "trade_alert.proxy_auto.ConnectionTcpMTProxyRandomizedIntermediate",
            Delegate,
        ):
            transport = automatic_connection(pool)(
                "149.154.167.51",
                443,
                2,
                loggers=defaultdict(lambda: logging.getLogger("test")),
            )
            await transport._connect(timeout=1)
        self.assertEqual(len(attempts), 2)
        attempts[0]._writer.close.assert_called_once()
        attempts[0]._writer.wait_closed.assert_awaited_once()
        self.assertIs(transport._writer, attempts[1]._writer)
        self.assertEqual(attempts[1].proxy[0], "8.8.4.4")
        transport._writer.close()
        attempts.clear()
        pool.position = 0

        class BadNonce(Delegate):
            async def _recv(self):
                body = bytes(types.ResPQ(self.nonce + 1, 1, b"pq", []))
                return struct.pack("<qqi", 0, 1, len(body)) + body

        with (
            patch(
                "trade_alert.proxy_auto.ConnectionTcpMTProxyRandomizedIntermediate",
                BadNonce,
            ),
            patch("trade_alert.proxy_auto.asyncio.open_connection") as direct,
        ):
            transport = automatic_connection(pool)(
                "149.154.167.51",
                443,
                2,
                loggers=defaultdict(lambda: logging.getLogger("test")),
            )
            with self.assertRaises(OSError):
                await transport._connect(timeout=1)
            direct.assert_not_called()
        self.assertEqual(len(attempts), 3)
        for attempt in attempts:
            attempt._writer.close.assert_called_once()
            attempt._writer.wait_closed.assert_awaited_once()

    async def test_auto_configuration_manual_precedence_and_constructor_never_connects_direct(
        self,
    ):
        self.assertFalse(parse_env(ENV)["TELEGRAM_PROXY_AUTO"])
        self.assertTrue(
            parse_env({**ENV, "TELEGRAM_PROXY_AUTO": "true"})["TELEGRAM_PROXY_AUTO"]
        )
        with self.assertRaises(ValueError):
            parse_env({**ENV, "TELEGRAM_PROXY_AUTO": "maybe"})
        with patch("trade_alert.mtproto.TelegramClient") as client:
            api = TelegramAPI(
                ENV["TELEGRAM_BOT_TOKEN"], 123, "a" * 32, ["1"], auto_proxy=True
            )
            self.assertTrue(api.auto_proxy)
            self.assertEqual(
                client.call_args.kwargs["connection"].__name__, "AutomaticMTProxy"
            )
            self.assertNotIn("proxy", client.call_args.kwargs)
            api = TelegramAPI(
                ENV["TELEGRAM_BOT_TOKEN"],
                123,
                "a" * 32,
                ["1"],
                auto_proxy=True,
                proxy_url=LINK,
            )
            self.assertFalse(api.auto_proxy)
            self.assertEqual(client.call_args.kwargs["proxy"][0], "8.8.8.8")

    async def test_initialization_retries_only_network_and_remains_cancellable(self):
        client = SimpleNamespace(disconnect=AsyncMock())
        api = TelegramAPI(
            ENV["TELEGRAM_BOT_TOKEN"], 123, "a" * 32, ["1"], client=Mock()
        )
        api.client, api.auto_proxy = client, True
        api.call = AsyncMock(
            side_effect=[
                TelegramError(0, "NETWORK"),
                dict(id=123, is_bot=True, username="fake_bot"),
            ]
        )
        with patch("trade_alert.mtproto.asyncio.sleep", AsyncMock()):
            await api.initialize()
        self.assertEqual(api.bot_id, 123)
        client.disconnect.assert_awaited_once()
        api.call = AsyncMock(side_effect=TelegramError(401))
        with self.assertRaises(TelegramError):
            await api.initialize()

    async def test_stalled_auto_proxy_reconnects_before_next_request(self):
        client = Mock()
        client.disconnect, client.connect = AsyncMock(), AsyncMock()
        client.is_connected.return_value = True
        client.send_message = AsyncMock(
            side_effect=[OSError("synthetic"), SimpleNamespace(id=7)]
        )
        api = TelegramAPI(
            ENV["TELEGRAM_BOT_TOKEN"], 123, "a" * 32, ["1"], client=client
        )
        api.auto_proxy = True
        with self.assertRaises(TelegramError):
            await api.call("sendMessage", dict(chat_id="1", text="synthetic"))
        self.assertTrue(api._proxy_failed)
        await api.call("sendMessage", dict(chat_id="1", text="synthetic"))
        client.disconnect.assert_awaited_once()
        client.connect.assert_awaited_once()
        self.assertFalse(api._proxy_failed)
