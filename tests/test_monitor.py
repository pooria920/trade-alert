"""Collector integration on temporary SQLite and synthetic detector inputs only."""

import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_market_client import ITEM, NOW, no_search, synthetic_book

from trade_alert.alerts import AlertService
from trade_alert.channels import ChannelService
from trade_alert.cleanup import SnapshotCleanup
from trade_alert.database import open_database
from trade_alert.engine import AlertEngine
from trade_alert.market import Instrument
from trade_alert.market_service import MarketService
from trade_alert.monitor import MarketMonitor, MonitorOptions
from trade_alert.schedule import MarketSchedule, Session
from trade_alert.snapshots import SnapshotRepository
from trade_alert.tsetmc import HttpError
from trade_alert.users import UserService

GONE = dict(type="BUY_QUEUE_DISAPPEARED", config=dict(minimumPreviousVolume=0))


def wire(item):
    return dict(insCode=item.ins_code, symbol=item.symbol, name=item.name)


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        context = open_database(Path(directory.name) / "monitor.db")
        self.db = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.clock = NOW
        self.users = UserService(self.db, ["1", "2"], lambda: self.clock)
        for user in ("1", "2"):
            self.users.register(user, user)
        self.users.add_symbol("1", wire(ITEM))
        self.logger = logging.getLogger("offline-monitor-test")
        self.logger.disabled = True
        self.fetch = AsyncMock(
            side_effect=lambda item: synthetic_book(item, self.clock)
        )
        self.market = MarketService(self.fetch, no_search)
        self.addAsyncCleanup(self.market.close)
        self.repository = SnapshotRepository(self.db)
        self.cleanup = SnapshotCleanup(
            self.repository, 86_400_000, 900_000, self.logger
        )
        self.schedule = MarketSchedule(
            [Session("equities", (1, 2, 3, 4, 5, 6, 7), "00:00", "23:59")]
        )

        async def verify(channel):
            return dict(channelId=channel, title="Test", canPost=True)

        self.admins = ["1"]
        self.channels = ChannelService(
            self.db, self.users, self.admins, "-100123", verify, now=lambda: self.clock
        )
        self.alerts = AlertService(self.db, self.users, now=lambda: self.clock)
        self.engine = AlertEngine(
            self.db,
            self.repository,
            self.users,
            now=lambda: self.clock,
            channels=self.channels,
            logger=self.logger,
        )
        self.callback = lambda snapshot, session: self.engine.on_snapshot(
            snapshot, session
        )
        self.monitors = []
        self.addAsyncCleanup(self.stop_monitors)

    async def stop_monitors(self):
        for monitor in self.monitors:
            await monitor.stop()

    def monitor(self, schedule=None, concurrency=2, timeout=1000, interval=1000):
        monitor = MarketMonitor(
            lambda: [
                *self.users.monitored_symbols(),
                *self.channels.monitored_symbols(),
            ],
            self.market,
            self.repository,
            schedule or self.schedule,
            self.cleanup,
            MonitorOptions(interval, concurrency, timeout, 30_000),
            self.callback,
            logger=self.logger,
            now=lambda: self.clock,
            jitter=lambda: 0,
        )
        self.monitors.append(monitor)
        return monitor

    async def test_shared_private_channel_collection_evaluates_new_inserts_once(self):
        for user in ("1", "2"):
            self.users.add_symbol(user, wire(ITEM))
            self.alerts.create(user, ITEM.ins_code, GONE, 0)
        channel = await self.channels.create("1", wire(ITEM), GONE)
        self.users.set_notifications("1", False)
        self.fetch.side_effect = lambda item: synthetic_book(item, self.clock, "buy")
        monitor = self.monitor()
        with patch.object(
            self.engine, "on_snapshot", wraps=self.engine.on_snapshot
        ) as evaluate:
            await monitor.poll_once()
            await monitor.poll_once()
            self.assertEqual(evaluate.call_count, 1)
            self.clock += 10_000
            self.fetch.side_effect = lambda item: synthetic_book(item, self.clock)
            await monitor.poll_once()
            self.assertEqual(evaluate.call_count, 2)
        self.assertEqual(self.fetch.call_count, 3)
        self.assertEqual(monitor.get_status()["monitored_symbols"], 1)
        self.assertEqual(monitor.get_status()["stored_snapshots"], 2)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM alert_events").fetchone()[0], 3
        )
        self.assertEqual(
            self.db.execute(
                "SELECT count(*) FROM notification_outbox WHERE status='PENDING'"
            ).fetchone()[0],
            3,
        )
        self.users.remove_symbol("1", ITEM.ins_code)
        self.users.remove_symbol("2", ITEM.ins_code)
        self.assertIsNotNone(self.channels.get("1", channel))
        self.clock += 10_000
        await monitor.poll_once()
        self.assertEqual(self.fetch.call_count, 4)
        self.admins.clear()
        self.clock += 10_000
        await monitor.poll_once()
        self.assertEqual(self.fetch.call_count, 4)
        self.assertEqual(monitor.get_status()["symbols"], [])
        self.assertEqual(self.db.execute("SELECT count(*) FROM users").fetchone()[0], 2)

    async def test_bounded_workers_no_overlap_and_shared_ondemand_request(self):
        self.users.remove_symbol("1", ITEM.ins_code)
        for code in ("1", "2", "3"):
            self.users.add_symbol("1", dict(insCode=code, symbol=code, name=code))
        entered, release = asyncio.Event(), asyncio.Event()
        active = maximum = 0

        async def gated(item):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                entered.set()
            await release.wait()
            active -= 1
            return synthetic_book(item, self.clock)

        self.fetch.side_effect = gated
        monitor = self.monitor(concurrency=2)
        first = asyncio.create_task(monitor.poll_once())
        await asyncio.wait_for(entered.wait(), 1)
        await monitor.poll_once()
        self.assertEqual(self.fetch.call_count, 2)
        release.set()
        await first
        self.assertEqual(maximum, 2)
        self.assertEqual(self.fetch.call_count, 3)
        entered.clear()
        release.clear()
        item = Instrument("1", "1", "1")
        demand = asyncio.create_task(self.market.snapshot(item))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        cycle = asyncio.create_task(monitor.poll_once())
        await asyncio.wait_for(entered.wait(), 1)
        release.set()
        await asyncio.gather(demand, cycle)
        self.assertEqual(self.fetch.call_count, 6)  # three symbols, shared first

    async def test_http_success_is_not_verified_evidence_and_failure_never_means_none(
        self,
    ):
        monitor = self.monitor()
        invalid = synthetic_book()
        invalid.update(verified=False, completeness="unknown", timestamp=None)
        self.fetch.side_effect = lambda item: invalid
        await monitor.poll_once()
        state = monitor.get_status()
        self.assertEqual(state["last_successful_fetch"], NOW)
        self.assertIsNone(state["last_verified_snapshot"])
        self.assertEqual(state["symbols"][0]["reason"], "unverified-book")
        self.assertIsNone(self.repository.get_latest_snapshot(ITEM.ins_code))
        self.clock += 1000
        self.fetch.side_effect = lambda item: synthetic_book(item, self.clock - 60_000)
        await monitor.poll_once()
        self.assertEqual(
            monitor.get_status()["symbols"][0]["last_result"], "stale-data"
        )
        self.assertEqual(monitor.get_status()["fresh_symbols"], 0)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM queue_snapshots").fetchone()[0], 0
        )

    async def test_per_symbol_backoff_cap_sparse_logs_and_recovery(self):
        monitor = self.monitor()
        self.fetch.side_effect = RuntimeError("secret upstream payload")
        with patch.object(self.logger, "warning") as warn:
            for attempt in range(12):
                await monitor.poll_once()
                state = monitor.get_status()["symbols"][0]
                self.assertEqual(
                    state["retry_at"] - self.clock,
                    min(300_000, 1000 * 2 ** min(attempt, 10)),
                )
                calls = self.fetch.call_count
                await monitor.poll_once()
                self.assertEqual(self.fetch.call_count, calls)
                self.clock = int(state["retry_at"])
            self.assertEqual(warn.call_count, 4)  # failures 1, 2, 4, 8
            self.assertNotIn("secret", str(warn.call_args_list))
        self.fetch.side_effect = lambda item: synthetic_book(item, self.clock)
        with patch.object(self.logger, "info") as info:
            await monitor.poll_once()
        self.assertTrue(
            any(c.args == ("Market data recovered",) for c in info.call_args_list)
        )
        state = monitor.get_status()
        self.assertEqual(state["consecutive_errors"], 0)
        self.assertEqual(state["fresh_symbols"], 1)
        state["symbols"][0]["reason"] = "external mutation"
        self.assertIsNone(monitor.get_status()["symbols"][0]["reason"])
        self.clock += 30_001
        self.assertEqual(monitor.get_status()["fresh_symbols"], 0)

    async def test_global_cooldown_stops_queued_symbols(self):
        self.users.add_symbol("2", dict(insCode="2", symbol="Second", name="Second"))
        self.fetch.side_effect = HttpError(429, 60_000)
        monitor = self.monitor(concurrency=1)
        await monitor.poll_once()
        await monitor.poll_once()
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(monitor.get_status()["rate_limited_until"], NOW + 60_000)
        self.clock += 60_000
        self.fetch.side_effect = lambda item: synthetic_book(item, self.clock)
        await monitor.poll_once()
        self.assertEqual(self.fetch.call_count, 3)

    async def test_membership_revocation_and_session_switch_during_fetch_prevent_insert(
        self,
    ):
        monitor = self.monitor()

        async def revoke(item):
            self.users.remove_symbol("1", item.ins_code)
            return synthetic_book(item, self.clock)

        self.fetch.side_effect = revoke
        await monitor.poll_once()
        self.assertIsNone(self.repository.get_latest_snapshot(ITEM.ins_code))
        self.users.add_symbol("1", wire(ITEM))
        self.schedule = MarketSchedule(
            [
                Session("equities", (3,), "10:00", "10:01"),
                Session("equities", (3,), "10:01", "10:02"),
            ]
        )
        monitor = self.monitor()
        self.clock = NOW + 59_000

        async def switch(item):
            self.clock = NOW + 61_000
            return synthetic_book(item, NOW + 59_000)

        self.fetch.side_effect = switch
        await monitor.poll_once()
        self.assertIsNone(self.repository.get_latest_snapshot(ITEM.ins_code))
        self.assertIsNone(monitor.get_status()["last_verified_snapshot"])

    async def test_closed_empty_progress_and_cleanup_outside_sessions(self):
        self.db.execute(
            "INSERT INTO queue_snapshots (ins_code, timestamp, queue_type, volume, order_count) VALUES (?, 1, 'none', 0, 0)",
            (ITEM.ins_code,),
        )
        monitor = self.monitor(schedule=MarketSchedule([]), interval=10)
        called = asyncio.Event()
        run = self.cleanup.run_if_due

        def cleanup(now, stop):
            run(now, stop)
            called.set()

        with patch.object(self.cleanup, "run_if_due", side_effect=cleanup):
            monitor.start()
            monitor.start()
            await asyncio.wait_for(called.wait(), 1)
            await self.cleanup.wait()
            self.assertEqual(
                self.db.execute("SELECT count(*) FROM queue_snapshots").fetchone()[0], 0
            )
            self.assertEqual(monitor.get_status()["last_progress_at"], NOW)
            called.clear()
            self.clock += 1000
            await asyncio.wait_for(called.wait(), 1)
            self.assertEqual(monitor.get_status()["last_progress_at"], self.clock)
            await monitor.stop()
        self.fetch.assert_not_called()
        self.assertFalse(monitor.get_status()["is_running"])
        self.users.remove_symbol("1", ITEM.ins_code)
        empty = self.monitor()
        await empty.poll_once()
        self.assertEqual(empty.get_status()["monitored_symbols"], 0)
        self.assertEqual(empty.get_status()["last_progress_at"], self.clock)

    async def test_stop_cancels_and_joins_loop_oneoff_and_native_requests(self):
        for loop in (False, True):
            entered, cancelled = asyncio.Event(), asyncio.Event()

            async def hanging(item):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

            self.fetch.side_effect = hanging
            monitor = self.monitor()
            if loop:
                monitor.start()
                cycle = monitor.task
            else:
                cycle = asyncio.create_task(monitor.poll_once())
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(monitor.stop(), 1)
            await cycle
            self.assertTrue(cancelled.is_set())
            self.assertFalse(self.market.pending)
            self.assertEqual(monitor.workers, [])
            self.assertEqual(monitor.get_status()["consecutive_errors"], 0)
            self.assertIsNone(self.repository.get_latest_snapshot(ITEM.ins_code))

    async def test_timeout_with_ignored_cancel_retains_pending_and_prevents_duplicates(
        self,
    ):
        cancelled, release = asyncio.Event(), asyncio.Event()

        async def ignoring_cancel(item):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return synthetic_book(item, self.clock)

        self.fetch.side_effect = ignoring_cancel
        monitor = self.monitor(timeout=10)
        await asyncio.wait_for(monitor.poll_once(), 1)
        await asyncio.wait_for(cancelled.wait(), 1)
        self.assertEqual(monitor.get_status()["consecutive_errors"], 1)
        self.clock += 1000
        await asyncio.wait_for(monitor.poll_once(), 1)
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(len(self.market.pending), 1)
        release.set()
        await monitor.stop()
        self.assertFalse(self.market.pending)
        self.assertIsNone(self.repository.get_latest_snapshot(ITEM.ins_code))

    async def test_evaluator_failure_does_not_interrupt_collection(self):
        def fail(snapshot, session):
            raise RuntimeError("secret")

        self.callback = fail
        monitor = self.monitor()
        with patch.object(self.logger, "error") as error:
            await monitor.poll_once()
        self.assertEqual(
            error.call_args.args, ("Alert evaluation failed; collection continues",)
        )
        self.assertEqual(monitor.get_status()["stored_snapshots"], 1)
        self.assertEqual(monitor.get_status()["consecutive_errors"], 0)
        stored = self.repository.get_latest_snapshot(ITEM.ins_code)
        self.assertEqual((stored.queue_type, stored.volume), ("none", 0))

    async def test_unexpected_loop_failure_still_allows_complete_stop(self):
        monitor = self.monitor(schedule=MarketSchedule([]))
        failed = asyncio.Event()

        def fail(now, stop):
            failed.set()
            raise RuntimeError("secret cleanup failure")

        with patch.object(self.cleanup, "run_if_due", side_effect=fail):
            with patch.object(self.logger, "error") as error:
                monitor.start()
                await asyncio.wait_for(failed.wait(), 1)
                await asyncio.wait_for(monitor.stop(), 1)
        self.assertEqual(error.call_args.args, ("Market monitor stopped unexpectedly",))
        self.assertFalse(monitor.get_status()["is_running"])
        self.assertEqual(monitor.get_status()["consecutive_errors"], 1)
        self.assertTrue(monitor.cycle_done.is_set())
        self.assertFalse(self.market.pending)
