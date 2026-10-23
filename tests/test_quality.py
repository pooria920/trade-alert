"""Phase 7 synthetic evidence and disposable SQLite only; no external services."""

import asyncio
import hashlib
import json
import logging
import shutil
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_config import REQUIRED
from test_market_client import no_search, synthetic_book
from test_services import DROP, GROUP, ITEM

from trade_alert.alerts import AlertService
from trade_alert.bot_text import history_text, quantity, statistics_text, stock_url
from trade_alert.channels import ChannelService
from trade_alert.cleanup import SnapshotCleanup
from trade_alert.config import parse_env
from trade_alert.database import MIGRATIONS, open_database, rows
from trade_alert.delivery import NotificationDispatcher, format_notification
from trade_alert.engine import AlertEngine
from trade_alert.evaluators import QueueSnapshot
from trade_alert.market import TEHRAN, Instrument, tehran_date
from trade_alert.market_service import MarketService
from trade_alert.monitor import MarketMonitor, MonitorOptions
from trade_alert.outcomes import AlertOutcomeTracker, change_percent
from trade_alert.quality import QualityPolicy, classify, parse_policy, stored_policy
from trade_alert.schedule import MarketSchedule, Session
from trade_alert.snapshots import SnapshotRepository
from trade_alert.users import UserService

START = int(datetime(2026, 10, 21, 10, 0, tzinfo=TEHRAN).timestamp() * 1000)
POLICY = dict(
    escalation_enabled=True,
    additional_drop_percent=25,
    minimum_interval_seconds=10,
    thresholds=[
        dict(severity="important", minimum_drop_percent=50),
        dict(severity="critical", minimum_drop_percent=75),
    ],
)


class QualityTests(unittest.IsolatedAsyncioTestCase):
    async def test_peak_escalation_freezes_baseline_and_preserves_outcomes(self):
        definition = dict(
            type="BUY_QUEUE_PEAK_DROP_PERCENT",
            config=dict(thresholdPercent=50, minimumPeakVolume=0, hysteresisPercent=5),
        )
        rule = self.alerts.create("1", "123", definition, 0)
        policy = {**POLICY, "outcome_tracking": True}
        self.assertTrue(self.alerts.update("1", rule, definition, quality=policy))
        self.accept(0, 1000)
        self.accept(10, 500)
        self.accept(20, 250)
        events = self.events()
        self.assertEqual(len(events), 2)
        first, escalation = [json.loads(e["payload"]) for e in events]
        self.assertEqual(escalation["previousVolume"], 1000)
        self.assertEqual(escalation["actualPercent"], 75)
        self.assertTrue(escalation["escalation"])
        self.assertEqual(first["previousVolume"], escalation["previousVolume"])
        self.restart()
        self.accept(30, 100)
        self.assertEqual(len(self.events()), 2)
        self.accept(310, 600)
        outcomes = rows(
            self.db,
            "SELECT * FROM alert_outcomes WHERE window_minutes=5 ORDER BY target_at",
        )
        self.assertEqual(outcomes[0]["status"], "completed")
        self.assertEqual(outcomes[0]["initial_queue_volume"], 500)
        self.assertEqual(outcomes[0]["observed_queue_volume"], 600)
        self.assertIsNone(outcomes[0]["price_change_percent"])
        self.assertEqual(outcomes[1]["status"], "pending")

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "quality.db"
        self.context = open_database(self.path)
        self.db = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        self.clock = START
        self.now = lambda: self.clock
        self.users = UserService(self.db, ["1", "2", "3"], self.now)
        self.admins = ["1", "2"]
        for actor in ("1", "2", "3"):
            self.users.register(actor, actor)
            self.users.add_symbol(actor, ITEM)
        self.alerts = AlertService(
            self.db, self.users, now=self.now, admins=self.admins
        )
        self.channels = ChannelService(
            self.db,
            self.users,
            self.admins,
            "-100123",
            AsyncMock(
                return_value=dict(channelId="-100123", title="Test", canPost=True)
            ),
            cooldown=0,
            now=self.now,
        )
        self.snapshots = SnapshotRepository(self.db)
        self.schedule = MarketSchedule(
            [Session("equities", (1, 2, 3, 4, 5, 6, 7), "09:00", "12:30")]
        )
        self.logger = logging.getLogger("offline-quality")
        self.logger.disabled = True
        self.restart()

    def restart(self):
        self.tracker = AlertOutcomeTracker(
            self.db,
            self.snapshots,
            self.schedule,
            lambda row: (
                row["owner_id"] in self.users.allowed
                if row["channel_id"] is None
                else self.channels.eligible(row)
            ),
            now=self.now,
            logger=self.logger,
        )
        self.engine = AlertEngine(
            self.db,
            self.snapshots,
            self.users,
            now=self.now,
            channels=self.channels,
            logger=self.logger,
            outcomes=self.tracker,
        )

    def rule(self, policy=None, cooldown=0, owner="1"):
        rule = self.alerts.create(owner, "123", DROP, cooldown)
        if policy is not None:
            self.assertTrue(self.alerts.update(owner, rule, DROP, quality=policy))
        return rule

    def accept(self, seconds, volume=1000, side="buy", session=None):
        self.clock = START + seconds * 1000
        value = QueueSnapshot(
            "123",
            "TEST",
            self.clock,
            side,
            0 if side == "none" else volume,
            None if side == "none" else 110,
            0 if side == "none" else 1,
        )
        session = session or self.schedule.session_id(self.clock)
        if self.snapshots.save(value, self.clock, session):
            self.engine.on_snapshot(value, session)
        return value

    def events(self):
        return rows(self.db, "SELECT * FROM alert_events ORDER BY created_at, id")

    def outcomes(self):
        return rows(self.db, "SELECT * FROM alert_outcomes ORDER BY target_at")

    def test_policy_validation_and_inclusive_severity(self):
        self.assertEqual(stored_policy(None), QualityPolicy())
        policy = parse_policy(POLICY)
        self.assertEqual(parse_policy(policy.to_json()), policy)
        for percent, expected in (
            (None, "normal"),
            (49.99, "normal"),
            (50, "important"),
            (75, "critical"),
            (100, "critical"),
        ):
            self.assertEqual(classify(policy, percent), expected)
        self.assertEqual(
            classify(parse_policy(dict(severity="important")), None), "important"
        )
        for invalid in (
            {"severity": "urgent"},
            {"severity": True},
            {"outcome_tracking": 1},
            {"escalation_enabled": "true"},
            {"additional_drop_percent": 0},
            {"additional_drop_percent": float("nan")},
            {"minimum_interval_seconds": 0},
            {"unknown": 1},
            {"thresholds": [{"severity": "critical", "minimum_drop_percent": 101}]},
            {
                "thresholds": [
                    dict(severity="critical", minimum_drop_percent=50),
                    dict(severity="important", minimum_drop_percent=75),
                ]
            },
            {
                "thresholds": [
                    dict(severity="important", minimum_drop_percent=50),
                    dict(severity="critical", minimum_drop_percent=50),
                ]
            },
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_policy(invalid)

    def test_defaults_deduplicate_continuation_and_restart_without_rearming(self):
        self.rule()
        self.accept(0)
        value = self.accept(60, 700)
        self.engine.on_snapshot(value, self.schedule.session_id(self.clock))
        self.accept(70, 680)
        self.restart()
        self.accept(80, 400)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.events()[0]["severity"], "normal")
        self.assertEqual(self.outcomes(), [])
        self.assertEqual(
            self.db.execute("SELECT sum(count) FROM alert_suppressions").fetchone()[0],
            2,
        )
        self.accept(90, side="sell")
        self.assertEqual(
            self.db.execute("SELECT status FROM alert_states").fetchone()[0],
            "WAITING_FOR_RECOVERY",
        )
        self.accept(100, 800)
        self.accept(160, 500)
        self.assertEqual(len(self.events()), 2)
        self.assertNotEqual(
            self.events()[0]["occurrence_key"], self.events()[1]["occurrence_key"]
        )

    def test_escalation_frozen_baseline_restart_interval_and_cooldown(self):
        rule = self.rule(POLICY, cooldown=120)
        self.accept(0)
        self.accept(60, 700)
        self.accept(70, 680)
        self.restart()
        self.accept(80, 400)  # Meaningful, but rule cooldown still applies.
        self.assertEqual(len(self.events()), 1)
        self.accept(180, 400)
        self.accept(190, 390)  # No additional 25 points since the last notification.
        self.assertEqual(len(self.events()), 2)
        self.assertEqual(
            [e["severity"] for e in self.events()], ["normal", "important"]
        )
        event = self.events()[1]
        self.assertIn(":escalation:1", event["occurrence_key"])
        self.assertEqual(json.loads(event["payload"])["previousVolume"], 1000)
        self.accept(300, 100)
        self.assertEqual(self.events()[-1]["severity"], "critical")
        self.assertEqual(
            self.db.execute(
                "SELECT baseline_volume FROM alert_states WHERE rule_id=?", (rule,)
            ).fetchone()[0],
            1000,
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM notification_outbox").fetchone()[0], 3
        )

    async def test_composite_whole_occurrence_and_rule_destination_independence(self):
        group = {
            **GROUP,
            "conditions": [
                DROP,
                dict(type="BUY_QUEUE_VOLUME_ABOVE", config=dict(minimumVolume=200)),
            ],
        }
        channel = await self.channels.create("1", ITEM, group, quality=POLICY)
        self.rule(POLICY)
        self.rule(owner="2")
        self.accept(0)
        self.accept(60, 700)
        original = self.db.execute(
            "SELECT recovery_data FROM alert_states WHERE rule_id=?", (channel,)
        ).fetchone()[0]
        self.accept(70, side="sell")  # UNKNOWN cannot rearm or escalate.
        self.accept(80, 400)
        self.assertEqual(len(self.events()), 5)
        self.assertEqual(
            self.db.execute(
                "SELECT recovery_data FROM alert_states WHERE rule_id=?", (channel,)
            ).fetchone()[0],
            original,
        )
        event = next(
            e
            for e in self.events()
            if e["rule_id"] == channel and e["severity"] == "important"
        )
        payload = json.loads(event["payload"])
        self.assertEqual(len(payload["matches"]), 2)
        self.assertEqual(payload["matches"][0]["previousVolume"], 1000)
        # AND becomes false below the volume minimum; cannot escalate even at 90% drop.
        self.accept(90, 100)
        self.assertEqual(len([e for e in self.events() if e["rule_id"] == channel]), 2)

    def test_three_outcome_windows_actual_timestamps_and_absent_traded_prices(self):
        self.rule(dict(outcome_tracking=True))
        self.accept(0)
        self.accept(60, 700)
        self.assertEqual(
            [o["target_at"] for o in self.outcomes()],
            [START + seconds * 1000 for seconds in (360, 960, 1860)],
        )
        self.accept(359, 600)  # Never substitute a pre-target observation.
        self.assertEqual(self.outcomes()[0]["status"], "pending")
        self.accept(365, 560)
        self.accept(960, side="none")
        self.accept(1860, side="sell")
        result = self.outcomes()
        self.assertEqual(
            [o["status"] for o in result],
            ["completed", "completed", "insufficient_data"],
        )
        self.assertEqual(result[0]["observed_at"], START + 365_000)
        self.assertEqual([o["queue_change_percent"] for o in result], [-20, -100, None])
        self.assertTrue(
            all(
                o["initial_price"] is None
                and o["observed_price"] is None
                and o["price_change_percent"] is None
                for o in result
            )
        )
        text = history_text(self.alerts.history("1")[0])
        self.assertIn("۲۰", text)
        self.assertIn("دادهٔ معتبر موجود نیست", text)
        self.assertEqual(self.alerts.history("2"), [])

    def test_outcome_restart_recovers_snapshot_saved_before_callback(self):
        self.rule(dict(outcome_tracking=True))
        self.accept(0)
        self.accept(60, 700)
        self.clock = START + 360_000
        value = QueueSnapshot("123", "TEST", self.clock, "buy", 350, 110, 1)
        self.snapshots.save(value, self.clock, self.schedule.session_id(self.clock))
        self.clock += (
            120_000  # Recover the timely persisted observation after downtime.
        )
        self.restart()
        self.tracker.advance()
        self.assertEqual(self.outcomes()[0]["queue_change_percent"], -50)
        self.assertEqual(self.outcomes()[0]["observed_at"], value.timestamp)
        self.assertEqual(self.outcomes()[0]["initial_queue_volume"], 700)

    def test_tolerance_stale_unpersisted_and_cross_session_do_not_complete(self):
        self.rule(dict(outcome_tracking=True))
        self.accept(0)
        self.accept(60, 700)
        self.clock = START + 360_000
        value = QueueSnapshot("123", "TEST", self.clock, "buy", 350, 110, 1)
        self.tracker.on_snapshot(value, self.schedule.session_id(self.clock))
        self.assertEqual(self.outcomes()[0]["status"], "pending")
        self.accept(360, 350, session="other")
        self.assertEqual(self.outcomes()[0]["status"], "pending")
        self.accept(391, 300)
        self.tracker.advance()
        self.assertEqual(self.outcomes()[0]["status"], "expired")
        self.clock = START + 86_400_000
        self.tracker.advance()
        self.assertTrue(all(o["status"] == "expired" for o in self.outcomes()))

    def test_stale_escalation_and_malformed_stored_policy_fail_closed(self):
        rule = self.rule(POLICY)
        self.accept(0)
        self.accept(60, 700)
        stale = QueueSnapshot("123", "TEST", START + 70_000, "buy", 100, 110, 1)
        session = self.schedule.session_id(stale.timestamp)
        self.snapshots.save(stale, stale.timestamp, session)
        self.clock = START + 120_000
        self.engine.on_snapshot(stale, session)
        self.assertEqual(len(self.events()), 1)
        self.db.execute(
            "UPDATE alert_rules SET quality_config='{}invalid' WHERE id=?", (rule,)
        )
        self.accept(130, 100)
        self.assertEqual(len(self.events()), 1)

    def test_outcome_retention_caps_total_rows_and_preserves_snapshot_independent_baseline(
        self,
    ):
        self.rule(dict(outcome_tracking=True))
        self.accept(0)
        self.accept(60, 700)
        self.db.executemany(
            "INSERT INTO alert_outcomes(source_event_id, owner_id, ins_code, session_id, window_minutes, target_at, cutoff_at, initial_queue_volume, queue_side) VALUES (?, '1', '123', 'old', 5, 1, 2, 700, 'buy')",
            [(f"detached-{i}",) for i in range(1001)],
        )
        self.assertEqual(self.tracker.delete_expired(self.clock), 1000)
        self.assertEqual(self.tracker.delete_expired(self.clock), 1)
        self.snapshots.delete_expired(self.clock + 1)
        self.assertEqual(self.outcomes()[0]["initial_queue_volume"], 700)
        self.accept(360, 350)
        self.assertEqual(self.outcomes()[0]["queue_change_percent"], -50)

    def test_market_close_windows_are_unavailable_not_next_session(self):
        self.schedule = MarketSchedule(
            [Session("equities", (1, 2, 3, 4, 5, 6, 7), "09:00", "10:10")]
        )
        self.restart()
        self.rule(dict(outcome_tracking=True))
        self.accept(0)
        self.accept(60, 700)
        self.assertEqual(
            [o["status"] for o in self.outcomes()],
            ["pending", "insufficient_data", "insufficient_data"],
        )
        self.clock = START + 601_000
        self.tracker.advance()
        self.assertEqual(self.outcomes()[0]["status"], "expired")

    def test_pending_outcomes_keep_symbols_after_watchlist_and_rule_removal(self):
        rule = self.rule(dict(outcome_tracking=True))
        self.accept(0)
        self.accept(60, 700)
        self.users.remove_symbol("1", "123")
        self.assertIsNone(self.alerts.get("1", rule))
        self.assertEqual(len(self.tracker.monitored_symbols()), 1)
        self.assertTrue(all(o["event_id"] is None for o in self.outcomes()))
        self.accept(360, 350)
        self.assertEqual(self.outcomes()[0]["queue_change_percent"], -50)
        self.users.allowed = self.users.allowed - {"1"}
        self.assertEqual(self.tracker.monitored_symbols(), [])

    def test_atomic_event_state_outbox_and_outcome_rollback(self):
        self.rule(dict(outcome_tracking=True))
        self.accept(0)
        self.db.execute(
            "CREATE TRIGGER fail_outcomes BEFORE INSERT ON alert_outcomes BEGIN SELECT RAISE(ABORT, 'test'); END"
        )
        value = self.accept(60, 700)
        self.assertEqual(self.events(), [])
        self.assertEqual(self.outcomes(), [])
        self.assertEqual(
            self.db.execute("SELECT last_evaluated_at FROM alert_states").fetchone()[0],
            START,
        )
        self.db.execute("DROP TRIGGER fail_outcomes")
        self.engine.on_snapshot(value, self.schedule.session_id(value.timestamp))
        self.assertEqual(len(self.outcomes()), 3)

    async def test_current_admin_revision_scope_validation_and_statistics(self):
        private = self.rule()
        user = self.rule(owner="3")
        with self.assertRaises(PermissionError):
            self.alerts.update("3", user, DROP, quality=POLICY)
        self.assertFalse(self.alerts.update("2", private, DROP, quality=POLICY))
        self.assertFalse(
            self.alerts.update("1", private, DROP, quality=POLICY, revision=2)
        )
        channel = await self.channels.create(
            "1", ITEM, DROP, quality={**POLICY, "outcome_tracking": True}
        )
        self.accept(0)
        self.accept(60, 700)
        self.accept(70, 680)
        self.accept(80, 400)
        self.accept(360, 350)
        stats = await self.channels.statistics("2")
        self.assertEqual(sum(r["count"] for r in stats["alerts"]), 2)
        self.assertGreater(stats["suppressed"], 0)
        self.assertIn("دادهٔ کافی موجود نیست", statistics_text(stats))
        with self.assertRaises(PermissionError):
            await self.channels.statistics("3")
        self.assertEqual(len(self.channels.history("2")), 2)
        self.assertTrue(
            all(e["rule_id"] == channel for e in self.channels.history("2"))
        )
        self.assertFalse(await self.channels.update("2", private, DROP, quality=POLICY))
        self.admins.remove("2")
        with self.assertRaises(PermissionError):
            await self.channels.update("2", channel, DROP, quality=POLICY)

    async def test_statistics_rechecks_admin_after_async_read_and_joins_cancellation(
        self,
    ):
        original_thread = asyncio.to_thread
        entered, release = asyncio.Event(), asyncio.Event()

        async def held_read(function):
            entered.set()
            await release.wait()
            return await original_thread(function)

        with patch("trade_alert.channels.asyncio.to_thread", side_effect=held_read):
            task = asyncio.create_task(self.channels.statistics("2"))
            await entered.wait()
            self.admins.remove("2")
            release.set()
            with self.assertRaises(PermissionError):
                await task

        started, finished = threading.Event(), threading.Event()

        def slow_rows(db, sql, parameters):
            started.set()
            try:
                return rows(
                    db,
                    "WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<1000000) SELECT sum(x) FROM n",
                )
            finally:
                finished.set()

        with patch("trade_alert.channels.rows", side_effect=slow_rows):
            task = asyncio.create_task(self.channels.statistics("1"))
            while not started.is_set():
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
            self.assertTrue(
                finished.is_set()
            )  # No readonly worker outlives its request.

    def test_formatter_plain_text_unicode_limits_links_and_optional_fields(self):
        self.rule(POLICY)
        self.accept(0)
        self.accept(60, 700)
        self.accept(70, 400)
        event = self.events()[-1]
        payload = json.loads(event["payload"])
        payload.update(
            symbol="<b>😀&خودرو</b>", severity=event["severity"], insCode="123"
        )
        message = format_notification(payload, self.clock + 60_000, 30_000)
        self.assertIn(
            "<b>😀&خودرو</b>", message
        )  # MTProto transport explicitly disables parse mode.
        self.assertIn("اهمیت: مهم", message)
        self.assertIn("زمان داده", message)
        self.assertIn("زمان اعلان", message)
        self.assertIn("ارسال با تأخیر", message)
        self.assertIn("این پیام ادامهٔ هشدار قبلی است", message)
        self.assertIn(stock_url("123"), message)
        self.assertIn("میلیون", quantity(12_500_000))
        for invalid in (123, "../123", "123?x", "0", "01"):
            with self.assertRaises(ValueError):
                stock_url(invalid)
        for operator in ("AND", "OR"):
            conditions = [
                dict(
                    path=f"0.{i}",
                    status="TRIGGER",
                    condition=DROP,
                    previousVolume=2**53 - 1,
                    currentVolume=1,
                    actualPercent=99.99,
                    windowMinutes=1,
                )
                for i in range(8)
            ]
            message = format_notification(
                dict(
                    symbol="😀" * 80,
                    timestamp=START,
                    type="COMPOSITE",
                    operator=operator,
                    matches=conditions,
                ),
                START,
                30_000,
            )
            self.assertLessEqual(len(message.encode("utf-16-le")) // 2, 4096)
            self.assertNotIn("UNKNOWN", message)
            self.assertNotIn("0.0", message)

    def test_zero_and_invalid_baselines_configuration_and_bounded_retention(self):
        for initial, observed in (
            (0, 1),
            (None, 1),
            (1, None),
            (-1, 1),
            (float("nan"), 1),
            (1, float("inf")),
        ):
            self.assertIsNone(change_percent(initial, observed))
        self.assertEqual(change_percent(100, 0), -100)
        self.assertEqual(
            parse_env(REQUIRED)["ALERT_OUTCOME_WINDOWS_MINUTES"], (5, 15, 30)
        )
        for key, values in {
            "ALERT_OUTCOME_WINDOWS_MINUTES": ("", "5,5", "10", "5, 15"),
            "ALERT_OUTCOME_TOLERANCE_SECONDS": ("0", "301", "NaN"),
            "ALERT_OUTCOME_RETENTION_DAYS": ("0", "366", "-1"),
        }.items():
            for value in values:
                with self.assertRaisesRegex(ValueError, key):
                    parse_env({**REQUIRED, key: value})
        self.rule(dict(outcome_tracking=True))
        self.accept(0)
        self.accept(60, 700)
        self.clock += 31 * 86_400_000
        self.assertEqual(self.tracker.delete_expired(self.clock), 3)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(len(self.alerts.list("1")), 1)

    async def test_monitor_to_engine_outcomes_formatter_and_delivery_no_extra_fetches(
        self,
    ):
        self.rule(dict(outcome_tracking=True))
        item = Instrument("123", "TEST", "TEST")
        self.users.remove_symbol("2", "123")
        self.users.remove_symbol("3", "123")
        volume = 1000

        async def fetch(instrument):
            result = synthetic_book(instrument, self.clock, "buy", volume)
            result["limits"]["date"] = tehran_date(self.clock)
            return result

        fetcher = AsyncMock(side_effect=fetch)
        market = MarketService(fetcher, no_search)
        self.addAsyncCleanup(market.close)
        cleanup = SnapshotCleanup(
            self.snapshots,
            3_600_000,
            900_000,
            self.logger,
            lambda: self.tracker.delete_expired(self.clock),
        )
        monitor = MarketMonitor(
            lambda: [
                *self.users.monitored_symbols(),
                *self.channels.monitored_symbols(),
                *self.tracker.monitored_symbols(),
            ],
            market,
            self.snapshots,
            self.schedule,
            cleanup,
            MonitorOptions(),
            self.engine.on_snapshot,
            now=self.now,
            logger=self.logger,
            before_cycle=self.tracker.advance,
        )
        self.addAsyncCleanup(monitor.stop)
        await monitor.poll_once()
        self.clock += 60_000
        volume = 700
        await monitor.poll_once()
        send = AsyncMock()
        dispatcher = NotificationDispatcher(
            self.db,
            self.users,
            send,
            channels=self.channels,
            now=self.now,
            logger=self.logger,
        )
        self.addAsyncCleanup(dispatcher.stop)
        await dispatcher.dispatch_once()
        self.assertEqual(send.await_count, 1)
        self.assertEqual(send.call_args.args[0], "1")
        self.assertIn("اهمیت: عادی", send.call_args.args[1])
        self.users.remove_symbol("1", "123")
        self.assertEqual(self.users.monitored_symbols(), [])
        self.assertEqual(
            [s.ins_code for s in self.tracker.monitored_symbols()], [item.ins_code]
        )
        self.clock += 300_000
        volume = 350
        await monitor.poll_once()
        self.assertEqual(fetcher.await_count, 3)
        self.assertEqual(self.outcomes()[0]["queue_change_percent"], -50)

    def test_previous_schema_upgrade_preserves_state_events_and_original_hashes(self):
        old_folder = self.path.parent / "old"
        shutil.copytree(MIGRATIONS, old_folder)
        journal_path = old_folder / "meta/_journal.json"
        journal = json.loads(journal_path.read_text())
        journal["entries"] = journal["entries"][:6]
        journal_path.write_text(json.dumps(journal))
        path = self.path.parent / "previous.db"
        with open_database(path, migrations=old_folder) as db:
            db.execute("INSERT INTO users VALUES ('1','1',1,1,1)")
            db.execute("INSERT INTO symbols VALUES ('123','TEST',1)")
            db.execute("INSERT INTO watchlists VALUES ('1','123',1)")
            db.execute(
                "INSERT INTO alert_rules(id, owner_id, ins_code, type, definition, created_at) VALUES ('old','1','123','BUY_QUEUE_DISAPPEARED',?,1)",
                (json.dumps(DROP),),
            )
            db.execute(
                "INSERT INTO alert_states(rule_id, revision, session_id, status, last_evaluated_at, last_triggered_at, baseline_volume) VALUES ('old',1,'session','TRIGGERED',2,2,1000)"
            )
            db.execute(
                "INSERT INTO alert_events(id, rule_id, message, created_at) VALUES ('event','old','legacy',2)"
            )
            db.execute(
                "INSERT INTO notification_outbox(event_id, next_attempt_at) VALUES ('event',2)"
            )
            prior = rows(db, "SELECT * FROM __drizzle_migrations ORDER BY created_at")
        broken = self.path.parent / "broken"
        shutil.copytree(MIGRATIONS, broken)
        migration = broken / "0006_alert_quality.sql"
        migration.write_bytes(
            migration.read_bytes() + b"\n--> statement-breakpoint\nINVALID SQL;"
        )
        with (
            self.assertRaises(sqlite3.OperationalError),
            open_database(path, migrations=broken),
        ):
            pass
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(len(rows(db, "SELECT * FROM __drizzle_migrations")), 6)
            self.assertEqual(
                db.execute("SELECT status FROM alert_states").fetchone()[0], "TRIGGERED"
            )
            self.assertNotIn(
                "quality_config",
                [row[1] for row in db.execute("PRAGMA table_info(alert_rules)")],
            )
        with open_database(path) as db:
            self.assertEqual(
                rows(db, "SELECT * FROM __drizzle_migrations ORDER BY created_at")[:6],
                prior,
            )
            self.assertEqual(
                db.execute("SELECT quality_config FROM alert_rules").fetchone(), (None,)
            )
            self.assertEqual(
                db.execute(
                    "SELECT status, baseline_volume, quality_data FROM alert_states"
                ).fetchone(),
                ("TRIGGERED", 1000, None),
            )
            self.assertEqual(
                db.execute("SELECT message, severity FROM alert_events").fetchone(),
                ("legacy", "normal"),
            )
            self.assertEqual(
                db.execute("SELECT status FROM notification_outbox").fetchone(),
                ("PENDING",),
            )
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        for entry in journal["entries"]:
            self.assertEqual(
                hashlib.sha256(
                    (MIGRATIONS / (entry["tag"] + ".sql")).read_bytes()
                ).hexdigest(),
                next(
                    row["hash"] for row in prior if row["created_at"] == entry["when"]
                ),
            )
