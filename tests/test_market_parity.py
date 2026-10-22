"""Captured adapter and synthetic collection parity against offline TypeScript."""

import json
import logging
import tempfile
import unittest
from pathlib import Path

import httpx
from reference import reference
from test_market_client import ITEM, NOW, captured, no_search, synthetic_book
from test_monitor import wire

from trade_alert.cleanup import SnapshotCleanup
from trade_alert.database import open_database, rows
from trade_alert.market import parse_instrument
from trade_alert.market_service import MarketService
from trade_alert.monitor import MarketMonitor, MonitorOptions
from trade_alert.schedule import MarketSchedule, Session
from trade_alert.snapshots import SnapshotRepository
from trade_alert.tsetmc import HttpError, TsetmcClient


class MarketParityTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_and_monitor_scenarios_match_typescript(self):
        clients = [
            dict(
                op="search", query="  كي  ", responses=[dict(body=captured("search"))]
            ),
            dict(
                op="snapshot",
                responses=[
                    dict(body=captured(name))
                    for name in ("best-1", "closing", "limits-date")
                ],
            ),
            dict(
                op="snapshot",
                responses=[
                    dict(body=captured(name))
                    for name in ("best-2", "closing", "limits-zero")
                ],
            ),
        ]
        for payload in (
            dict(instrumentSearch=[]),
            dict(instrumentSearch=[dict(insCode=123, lVal18AFC="TEST", lVal30="Test")]),
            dict(instrumentSearch=[dict(insCode="123", lVal18AFC="ي", lVal30="ك")]),
            dict(unexpected=[]),
        ):
            clients.append(
                dict(op="search", query="TEST", responses=[dict(body=payload)])
            )
        for field, value in (("number", 0), ("qTitMeDem", -1), ("pMeOf", "10")):
            payload = captured("best-1")
            payload["bestLimits"][0][field] = value
            clients.append(dict(op="snapshot", responses=[dict(body=payload)]))
        for status in (302, 404, 429, 503):
            clients.append(
                dict(
                    op="search", query="TEST", responses=[dict(status=status, body={})]
                )
            )
        clients.append(dict(op="search", query="TEST", responses=[dict(raw="{broken")]))

        def step(
            at,
            *,
            side="none",
            volume=1000,
            source=None,
            instruments=None,
            errors=None,
            unverified=False,
        ):
            book = synthetic_book(
                ITEM, NOW + (at if source is None else source), side, volume
            )
            book["receivedAt"] = NOW + at
            if unverified:
                book.update(verified=False, timestamp=None, completeness="unknown")
            return dict(
                at=at,
                instruments=[wire(ITEM)] if instruments is None else instruments,
                books={ITEM.ins_code: book},
                errors=errors or {},
            )

        monitors = [
            [
                step(0, side="buy"),
                step(0, side="buy"),
                step(10_000),
                step(20_000, side="sell"),
                step(30_000, instruments=[]),
            ],
            [
                step(0, unverified=True),
                step(500),
                step(1000, source=-60_000),
                step(3000),
            ],
            [
                step(0, errors={ITEM.ins_code: dict(status=429, retryAfter=60_000)}),
                step(1000),
                step(60_000),
            ],
            [
                step(0, instruments=[wire(ITEM), wire(ITEM)]),
                step(1000),
                step(1000, side="none"),
            ],
        ]
        expected = reference(
            "market-reference",
            dict(now=NOW, instrument=wire(ITEM), clients=clients, monitors=monitors),
        )
        for index, case in enumerate(clients):
            with self.subTest(client=index):
                requests = []
                responses = iter(case["responses"])

                async def transport(request):
                    requests.append(str(request.url))
                    response = next(responses)
                    return httpx.Response(
                        response.get("status", 200),
                        text=response.get("raw") or json.dumps(response.get("body")),
                        headers=response.get("headers"),
                    )

                client = TsetmcClient(
                    "https://tsetmc.invalid/api",
                    max_retries=0,
                    transport=httpx.MockTransport(transport),
                    now=lambda: NOW,
                )
                try:
                    try:
                        if case["op"] == "search":
                            result = [
                                wire(i) for i in await client.search(case["query"])
                            ]
                        else:
                            result = await client.snapshot(ITEM)
                        actual = dict(ok=True, value=result, requests=requests)
                    except ValueError, TypeError, KeyError, HttpError:
                        actual = dict(ok=False, requests=requests)
                    self.assertEqual(actual, expected["clients"][index])
                finally:
                    await client.close()

        logger = logging.getLogger("offline-market-parity")
        logger.disabled = True
        for index, steps in enumerate(monitors):
            with (
                self.subTest(monitor=index),
                tempfile.TemporaryDirectory() as directory,
                open_database(Path(directory) / "monitor.db") as db,
            ):
                clock = NOW
                current = None
                fetches = evaluations = 0

                async def fetch(item):
                    nonlocal fetches
                    fetches += 1
                    error = current["errors"].get(item.ins_code)
                    if error:
                        raise HttpError(error["status"], error.get("retryAfter"))
                    return current["books"][item.ins_code]

                def evaluate(snapshot, session):
                    nonlocal evaluations
                    evaluations += 1

                repository = SnapshotRepository(db)
                market = MarketService(fetch, no_search)
                monitor = MarketMonitor(
                    lambda: [parse_instrument(i) for i in current["instruments"]],
                    market,
                    repository,
                    MarketSchedule(
                        [Session("equities", (1, 2, 3, 4, 5, 6, 7), "00:00", "23:59")]
                    ),
                    SnapshotCleanup(repository, 86_400_000, 900_000, logger),
                    MonitorOptions(1000, 1),
                    evaluate,
                    logger=logger,
                    now=lambda: clock,
                    jitter=lambda: 0,
                )
                trace = []
                try:
                    for current in steps:
                        clock = NOW + current["at"]
                        for item in current["instruments"]:
                            db.execute(
                                "INSERT INTO symbols (ins_code, symbol, created_at) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                                (item["insCode"], item["symbol"], clock),
                            )
                        await monitor.poll_once()
                        status = monitor.get_status()
                        for symbol in status["symbols"]:
                            symbol.pop(
                                "reason"
                            )  # Python exposes fixed safe codes, never raw errors.
                        trace.append(
                            dict(
                                status=status,
                                fetches=fetches,
                                evaluations=evaluations,
                                snapshots=rows(
                                    db,
                                    "SELECT ins_code, symbol, timestamp, queue_type, volume, price, order_count, collected_at, session_id FROM queue_snapshots ORDER BY ins_code, timestamp",
                                ),
                            )
                        )
                    self.assertEqual(trace, expected["monitors"][index])
                finally:
                    await monitor.stop()
                    await market.close()
