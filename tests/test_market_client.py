"""MockTransport-only HTTP, native cancellation and shared-request regression tests."""

import asyncio
import json
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import patch

import httpx
from test_parity import ROOT

from trade_alert.market import Instrument, QueueDetector
from trade_alert.market_service import MarketService
from trade_alert.tsetmc import HttpError, TsetmcClient

NOW = int(datetime(2026, 10, 21, 6, 30, tzinfo=timezone.utc).timestamp() * 1000)
ITEM = Instrument("65883838195688438", "خودرو", "ایران خودرو")


def captured(name):
    return json.loads(
        (ROOT / f"tests/fixtures/tsetmc/{name}.json").read_text(encoding="utf-8")
    )


def synthetic_book(item=ITEM, now=NOW, side="none", volume=1000):
    """Synthetic detector input, deliberately separate from captured responses."""
    empty = dict(price=0, volume=0, orderCount=0)
    return dict(
        instrument=dict(insCode=item.ins_code, symbol=item.symbol, name=item.name),
        timestamp=now,
        receivedAt=now,
        verified=True,
        completeness="complete",
        tradingStatus="open",
        limits=dict(insCode=item.ins_code, date="20261021", upper=110, lower=90),
        levels=[]
        if side == "none"
        else [
            dict(
                rank=1,
                bid=dict(price=110, volume=volume, orderCount=1)
                if side == "buy"
                else empty,
                ask=dict(price=90, volume=volume, orderCount=1)
                if side == "sell"
                else empty,
            )
        ],
    )


async def no_search(query):
    return []


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = NOW
        self.requests = []

    def client(self, handler, **kwargs):
        async def transport(request):
            self.requests.append(request)
            return await handler(request)

        client = TsetmcClient(
            "https://tsetmc.invalid/api",
            transport=httpx.MockTransport(transport),
            now=lambda: self.clock,
            jitter=lambda: 0,
            **kwargs,
        )
        self.addAsyncCleanup(client.close)
        return client

    async def test_captured_search_keeps_string_codes_and_normalizes_query(self):
        async def response(request):
            self.assertEqual(request.headers["Accept"], "application/json")
            return httpx.Response(200, json=captured("search"))

        result = await self.client(response).search("  كي  ")
        self.assertEqual(len(result), 41)
        self.assertTrue(all(isinstance(i.ins_code, str) for i in result))
        self.assertEqual(
            self.requests[0].url.path, "/api/Instrument/GetInstrumentSearch/کی"
        )
        self.assertIn("%DA%A9", str(self.requests[0].url))

    async def test_captured_book_stays_unverified_and_uses_latest_same_day_limits(self):
        thresholds = captured("limits-date")
        thresholds["staticThreshold"].extend(
            [
                dict(dEven=20261020, hEven=235959, psGelStaMax=999, psGelStaMin=1),
                dict(dEven=20261021, hEven=60126, psGelStaMax=720, psGelStaMin=680),
            ]
        )
        payloads = [captured("best-1"), captured("closing"), thresholds]

        async def response(request):
            return httpx.Response(200, json=payloads.pop(0))

        book = await self.client(response).snapshot(ITEM)
        self.assertEqual(
            book["levels"][0]["bid"], dict(price=675, volume=58605, orderCount=1)
        )
        self.assertEqual(
            book["limits"],
            dict(insCode=ITEM.ins_code, date="20261021", upper=720, lower=680),
        )
        self.assertEqual(
            (book["timestamp"], book["completeness"], book["verified"]),
            (None, "unknown", False),
        )
        self.assertEqual(QueueDetector().detect(book, NOW).reason, "unverified-book")
        self.assertTrue(self.requests[-1].url.path.endswith("/20261021"))

    async def test_missing_current_threshold_and_old_closing_stay_unknown(self):
        closing = captured("closing")
        closing["closingPriceInfo"]["dEven"] = 20261020
        payloads = [captured("best-2"), closing, captured("limits-zero")]

        async def response(request):
            return httpx.Response(200, json=payloads.pop(0))

        book = await self.client(response).snapshot(ITEM)
        self.assertIsNone(book["limits"])
        self.assertEqual(book["tradingStatus"], "unknown")

    async def test_invalid_payloads_and_redirects_do_not_retry_or_follow(self):
        for payload in (
            dict(instrumentSearch=[dict(insCode=123, lVal18AFC="TEST", lVal30="Test")]),
            dict(instrumentSearch=[dict(insCode="123", lVal18AFC=None, lVal30="Test")]),
            dict(unexpected=[]),
        ):

            async def response(request):
                return httpx.Response(200, json=payload)

            before = len(self.requests)
            with self.assertRaises((ValueError, KeyError)):
                await self.client(response).search("TEST")
            self.assertEqual(len(self.requests), before + 1)
        for status, body in (
            (404, ""),
            (302, ""),
            (200, "{broken"),
            (200, '{"instrumentSearch":NaN}'),
        ):

            async def response(request):
                return httpx.Response(
                    status, text=body, headers={"Location": "https://never.invalid/"}
                )

            before = len(self.requests)
            with self.assertRaises((ValueError, HttpError)):
                await self.client(response).search("TEST")
            self.assertEqual(len(self.requests), before + 1)

    async def test_malformed_bestlimits_stops_before_other_routes(self):
        for field, value in (
            ("qTitMeDem", -1),
            ("zOrdMeOf", 2**53),
            ("number", True),
            ("pMeDem", "675"),
        ):
            payload = captured("best-1")
            payload["bestLimits"][0][field] = value

            async def response(request):
                return httpx.Response(200, json=payload)

            before = len(self.requests)
            with self.assertRaises(ValueError):
                await self.client(response).snapshot(ITEM)
            self.assertEqual(len(self.requests), before + 1)

    async def test_retry_budget_delays_and_global_server_cooldown(self):
        async def advance(seconds):
            self.clock += int(seconds * 1000)

        for error in (httpx.ConnectError("hidden"), HttpError(503)):
            attempts = 0

            async def response(request):
                nonlocal attempts
                attempts += 1
                if attempts <= 2:
                    if isinstance(error, HttpError):
                        return httpx.Response(error.status)
                    raise error
                return httpx.Response(200, json=dict(instrumentSearch=[]))

            with patch(
                "trade_alert.tsetmc.asyncio.sleep", side_effect=advance
            ) as sleep:
                self.assertEqual(await self.client(response).search("TEST"), [])
            self.assertEqual(attempts, 3)
            self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.5, 1.0])

        async def unavailable(request):
            return httpx.Response(503)

        before = len(self.requests)
        with patch("trade_alert.tsetmc.asyncio.sleep", side_effect=advance):
            with self.assertRaises(HttpError):
                await self.client(unavailable, max_retries=1).search("TEST")
        self.assertEqual(len(self.requests), before + 2)

        async def rate_limit(request):
            return httpx.Response(429, headers={"Retry-After": "60"})

        client = self.client(rate_limit)
        before = len(self.requests)
        for _ in range(2):
            with self.assertRaises(HttpError) as error:
                await client.search("TEST")
            self.assertEqual(error.exception.retry_after, 60_000)
        self.assertEqual(len(self.requests), before + 1)
        self.clock += 60_000
        with self.assertRaises(HttpError):
            await client.search("TEST")
        self.assertEqual(len(self.requests), before + 2)

    async def test_retry_after_http_date_and_invalid_header(self):
        for header, expected in (
            ("Wed, 21 Oct 2026 06:31:00 GMT", 60_000),
            ("garbage", None),
            ("-2", 0),
            ("1e308", None),
            ("Wed, 21 Oct 2026 06:31:00", None),
        ):

            async def response(request):
                return httpx.Response(503, headers={"Retry-After": header})

            with self.assertRaises(HttpError) as error:
                await self.client(response, max_retries=0).search("TEST")
            self.assertEqual(error.exception.retry_after, expected)

    async def test_timeout_and_shutdown_cancel_transport_and_retry_wait(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def hanging(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with self.assertRaises(TimeoutError):
            await self.client(hanging, timeout_ms=10, max_retries=0).search("TEST")
        self.assertTrue(cancelled.is_set())
        entered.clear()
        cancelled.clear()
        client = self.client(hanging)
        pending = asyncio.create_task(client.search("TEST"))
        await entered.wait()
        await client.close()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertTrue(cancelled.is_set())

        async def retryable(request):
            return httpx.Response(503, headers={"Retry-After": "10"})

        entered.clear()

        async def sleep(seconds):
            entered.set()
            await asyncio.Event().wait()

        client = self.client(retryable)
        with patch("trade_alert.tsetmc.asyncio.sleep", side_effect=sleep):
            pending = asyncio.create_task(client.search("TEST"))
            await entered.wait()
            await client.close()
            with self.assertRaises(asyncio.CancelledError):
                await pending
        self.assertEqual(client.cooldown_until, self.clock + 10_000)
        self.assertFalse(client.active)

    async def test_swallowed_cancellation_cannot_return_success(self):
        entered = asyncio.Event()

        async def ignoring_cancel(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return httpx.Response(200, json=dict(instrumentSearch=[]))

        client = self.client(ignoring_cancel)
        pending = asyncio.create_task(client.search("TEST"))
        await entered.wait()
        await client.close()
        with self.assertRaises(asyncio.CancelledError):
            await pending


class SharedRequestTests(unittest.IsolatedAsyncioTestCase):
    def service(self, fetch):
        market = MarketService(fetch, no_search)
        self.addAsyncCleanup(market.close)
        return market

    async def test_shared_batch_users_and_eviction_after_success_or_failure(self):
        calls = 0

        async def fetch(item):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            if calls == 1:
                raise ValueError("synthetic failure")
            return synthetic_book(item)

        market = self.service(fetch)
        with self.assertRaises(ValueError):
            await market.snapshot(ITEM)
        batch, other = await asyncio.gather(
            market.snapshots([ITEM, ITEM]), market.snapshot(ITEM)
        )
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0], other)
        self.assertEqual(calls, 2)
        await market.snapshot(ITEM)
        self.assertEqual(calls, 3)
        self.assertFalse(market.pending)

    async def test_secondary_cancellation_does_not_cancel_shared_request(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def fetch(item):
            entered.set()
            await release.wait()
            return synthetic_book(item)

        market = self.service(fetch)
        first = asyncio.create_task(market.snapshot(ITEM))
        await entered.wait()
        second = asyncio.create_task(market.snapshot(ITEM))
        await asyncio.sleep(0)
        second.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await second
        self.assertEqual(len(market.pending), 1)
        release.set()
        self.assertEqual((await first)["instrument"]["insCode"], ITEM.ins_code)

    async def test_creator_cancellation_keeps_unresolved_request_registered(self):
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = 0

        async def fetch(item):
            nonlocal calls
            calls += 1
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return synthetic_book(item)

        market = self.service(fetch)
        first = asyncio.create_task(market.snapshot(ITEM))
        await entered.wait()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        await cancelled.wait()
        second = asyncio.create_task(market.snapshot(ITEM))
        await asyncio.sleep(0)
        self.assertEqual(calls, 1)
        self.assertEqual(len(market.pending), 1)
        release.set()
        await second
        self.assertFalse(market.pending)

    async def test_mismatch_invalid_book_and_shutdown(self):
        payload = synthetic_book()
        payload["instrument"]["insCode"] = "456"

        async def fetch(item):
            return payload

        market = self.service(fetch)
        with self.assertRaisesRegex(ValueError, "mismatch"):
            await market.snapshot(ITEM)
        payload = deepcopy(synthetic_book())
        payload["verified"] = "true"
        with self.assertRaises(ValueError):
            await market.snapshot(ITEM)
        await market.close()
        with self.assertRaises(RuntimeError):
            await market.snapshot(ITEM)
        with self.assertRaises(RuntimeError):
            await market.search("TEST")
