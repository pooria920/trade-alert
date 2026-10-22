"""Synthetic MTProxy protocol checks. No sockets, DNS or Telegram requests."""

import asyncio
import base64
import hashlib
import hmac
import io
import logging
import time
import unittest
import warnings
from collections import defaultdict
from unittest.mock import AsyncMock, Mock, patch

from telethon import connection
from telethon.crypto import AESModeCTR

from trade_alert.config import mtproxy
from trade_alert.logger import create_logger
from trade_alert.mtproto import TelegramAPI
from trade_alert.mtproxy_tls import (
    ConnectionTcpMTProxyFakeTLS,
    TLSReader,
    TLSWriter,
    client_hello,
    verify_server,
)

KEY = bytes(range(16))
SECRET = "ee" + KEY.hex() + b"example.org".hex()
LINK = "https://t.me/proxy?server=proxy.example&port=443&secret="


def record(kind, body):
    return bytes([kind, 3, 3]) + len(body).to_bytes(2, "big") + body


def server_reply(hello, *, key=KEY, size=1024):
    # Independent synthetic ServerHello, CCS and encrypted certificate record.
    session = hello[44:76]
    body = b"\x03\x03" + bytes(32) + b"\x20" + session + b"\x13\x01\x00"
    body = b"\x02" + len(body).to_bytes(3, "big") + body
    reply = record(22, body) + record(20, b"\x01") + record(23, b"x" * size)
    digest = hmac.digest(key, hello[11:43] + reply, "sha256")
    return reply[:11] + digest + reply[43:]


class ProxyConfigTests(unittest.TestCase):
    def test_base64_secrets_normalize_without_misreading_a_textual_ee_prefix(self):
        for raw in (
            KEY,
            b"\xdd" + KEY,
            bytes.fromhex(SECRET),
            b"\x10D" + bytes(range(14)),
        ):
            encoded = base64.urlsafe_b64encode(raw).decode()
            for secret in (encoded, encoded.rstrip("=")):
                self.assertEqual(
                    mtproxy(LINK + secret), ("proxy.example", 443, raw.hex())
                )
                stream = io.StringIO()
                create_logger(stream=stream, proxy_secret=raw.hex()).error(
                    LINK + secret
                )
                self.assertNotIn(secret.rstrip("="), stream.getvalue())
        for raw in (bytes(15), bytes(17), b"\xee" + KEY):
            with self.assertRaises(ValueError):
                mtproxy(LINK + base64.urlsafe_b64encode(raw).decode())
        encoded = base64.urlsafe_b64encode(KEY).decode().rstrip("=")
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        noncanonical = encoded[:-1] + alphabet[alphabet.index(encoded[-1]) + 1]
        with self.assertRaises(ValueError):
            mtproxy(LINK + noncanonical)

    def test_links_transport_selection_and_redaction(self):
        self.assertIsNone(mtproxy(""))
        for host in ("192.0.2.1", "2001:db8::1", "proxy.example"):
            self.assertEqual(
                mtproxy(LINK.replace("proxy.example", host) + SECRET)[0], host
            )
        for key in ("dd" + "00" * 15, "ee" + "00" * 15):
            self.assertEqual(mtproxy(LINK + key)[2], key)
        for secret in (KEY.hex(), "dd" + KEY.hex(), SECRET):
            for prefix in (LINK, LINK.replace("https://t.me/proxy", "tg://proxy")):
                self.assertEqual(
                    mtproxy(prefix + secret), ("proxy.example", 443, secret)
                )
            with patch("trade_alert.mtproto.TelegramClient") as constructor:
                TelegramAPI(
                    "123:" + "x" * 35, 123, "a" * 32, ["1"], proxy_url=LINK + secret
                )
                kwargs = constructor.call_args.kwargs
                self.assertIs(
                    kwargs["connection"],
                    ConnectionTcpMTProxyFakeTLS
                    if secret == SECRET
                    else connection.ConnectionTcpMTProxyRandomizedIntermediate,
                )
                self.assertEqual(
                    kwargs["proxy"],
                    (
                        "proxy.example",
                        443,
                        secret if secret == SECRET else "dd" + KEY.hex(),
                    ),
                )
        with patch("trade_alert.mtproto.TelegramClient") as constructor:
            TelegramAPI("123:" + "x" * 35, 123, "a" * 32, ["1"])
            self.assertNotIn("proxy", constructor.call_args.kwargs)
        with warnings.catch_warnings(record=True) as observed:
            warnings.simplefilter("always")
            api = TelegramAPI(
                "123:" + "x" * 35, 123, "a" * 32, ["1"], proxy_url=LINK + SECRET
            )
            self.assertIs(api.client._connection, ConnectionTcpMTProxyFakeTLS)
            self.assertEqual(api.client._proxy, ("proxy.example", 443, SECRET))
        self.assertEqual(observed, [])
        stream = io.StringIO()
        create_logger(stream=stream, proxy_secret=SECRET).error(
            LINK + SECRET.upper() + " " + KEY.hex()
        )
        self.assertNotIn(KEY.hex(), stream.getvalue().lower())

    def test_invalid_links_fail_without_echoing_credentials(self):
        for link in (
            LINK + "invalid",
            LINK + "ee" + KEY.hex(),
            LINK + "ee" + KEY.hex() + b"a..b".hex(),
            LINK + "ee" + KEY.hex() + (b"a." * 110 + b"b").hex(),
            LINK + SECRET + "&port=123",
            LINK + SECRET + "#fragment",
            LINK.replace("443", "0") + SECRET,
            LINK.replace("443", "65536") + SECRET,
            LINK.replace("proxy.example", "bad/host") + SECRET,
            LINK.replace("https", "http") + SECRET,
        ):
            with self.subTest(link=link), self.assertRaises(ValueError) as error:
                mtproxy(link)
            self.assertEqual(
                str(error.exception), "Invalid or unsupported TELEGRAM_MTPROXY_URL"
            )


class ProxyProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_variable_handshake_and_fresh_client_random(self):
        hello, digest, session = client_hello(KEY, b"example.org")
        self.assertEqual(len(hello), 517)
        expected = hmac.digest(KEY, hello[:11] + bytes(32) + hello[43:], "sha256")
        self.assertEqual(digest[:28], expected[:28])
        timestamp = int.from_bytes(
            bytes(a ^ b for a, b in zip(digest[28:], expected[28:])), "little"
        )
        self.assertLessEqual(abs(time.time() - timestamp), 2)
        self.assertNotEqual(hello, client_hello(KEY, b"example.org")[0])
        for size in (64, 1024, 3925, 8000):
            reader = asyncio.StreamReader()
            reader.feed_data(server_reply(hello, size=size))
            reader.feed_eof()
            await verify_server(reader, KEY, digest, session)
        for reply in (
            server_reply(hello, key=bytes(16)),
            b"\x16\x03\x01\x00\x01x",
            record(22, b"short"),
        ):
            reader = asyncio.StreamReader()
            reader.feed_data(reply)
            reader.feed_eof()
            with self.assertRaises(ConnectionError):
                await verify_server(reader, KEY, digest, session)

    async def test_framing_fragmentation_and_packet_limit(self):
        raw = Mock()
        raw.drain = AsyncMock()
        raw.wait_closed = AsyncMock()
        writer = TLSWriter(raw)
        payload = b"a" * 40000
        writer.write(payload)
        reader = asyncio.StreamReader()
        for call in raw.write.call_args_list:
            data = call.args[0]
            self.assertLessEqual(len(data), 16389)
            reader.feed_data(data[:2])
            reader.feed_data(data[2:])
        reader.feed_eof()
        framed = TLSReader(reader)
        self.assertEqual(
            await framed.readexactly(1) + await framed.readexactly(39999), payload
        )
        with self.assertRaises(ConnectionError):
            await framed.readexactly(16 * 1024 * 1024 + 1)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        raw.close.assert_called_once()
        raw.wait_closed.assert_awaited_once()

    async def test_connection_keeps_telethon_encryption_and_never_falls_back(self):
        for failure in (None, "authentication", "truncated", "timeout", "cancel"):
            with self.subTest(failure=failure):
                reader = asyncio.StreamReader()
                writer = Mock(drain=AsyncMock(), wait_closed=AsyncMock())

                def respond(hello):
                    if hello[0] == 22:
                        if failure in ("timeout", "cancel"):
                            return
                        reply = server_reply(
                            hello, key=bytes(16) if failure == "authentication" else KEY
                        )
                        reader.feed_data(
                            reply[:20] if failure == "truncated" else reply
                        )
                        if failure:
                            reader.feed_eof()

                writer.write.side_effect = respond
                conn = ConnectionTcpMTProxyFakeTLS(
                    "149.154.167.51",
                    443,
                    2,
                    loggers=defaultdict(lambda: logging.getLogger("test")),
                    proxy=("proxy.example", 443, SECRET),
                )
                with patch(
                    "trade_alert.mtproxy_tls.asyncio.open_connection",
                    AsyncMock(return_value=(reader, writer)),
                ) as opened:
                    if failure == "cancel":
                        task = asyncio.create_task(conn._connect(timeout=1))
                        await asyncio.sleep(0)
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    elif failure:
                        with self.assertRaises((ConnectionError, TimeoutError)):
                            await conn._connect(timeout=0.02)
                    else:
                        await conn._connect(timeout=1)
                        wrapped = writer.write.call_args.args[0]
                        self.assertEqual(wrapped[:5], b"\x17\x03\x03\x00\x40")
                        header = wrapped[5:]
                        decrypt = AESModeCTR(
                            hashlib.sha256(header[8:40] + KEY).digest(), header[40:56]
                        )
                        plain = decrypt.encrypt(header)
                        self.assertEqual(plain[56:62], b"\xdd" * 4 + b"\x02\x00")
                        conn._send(b"test")
                        outgoing = decrypt.encrypt(writer.write.call_args.args[0][5:])
                        self.assertIn(
                            int.from_bytes(outgoing[:4], "little"), range(4, 8)
                        )
                        self.assertEqual(outgoing[4:8], b"test")
                        reverse = header[55:7:-1]
                        encrypt = AESModeCTR(
                            hashlib.sha256(reverse[:32] + KEY).digest(), reverse[32:48]
                        )
                        incoming = encrypt.encrypt(b"\x06\0\0\0testXY")
                        reader.feed_data(
                            record(23, incoming[:2]) + record(23, incoming[2:])
                        )
                        self.assertEqual(await conn._recv(), b"test")
                        conn._writer.close()
                        await conn._writer.wait_closed()
                    opened.assert_awaited_once_with(
                        "proxy.example", 443, local_addr=None
                    )
                writer.close.assert_called_once()
                writer.wait_closed.assert_awaited_once()
