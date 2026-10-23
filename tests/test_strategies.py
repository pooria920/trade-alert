"""Phase 8 synthetic accepted evidence; all databases are disposable, no network."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import test_persistence as persistence
from test_services import ITEM

from trade_alert.alerts import AlertService
from trade_alert.bot_text import history_text
from trade_alert.channels import ChannelService
from trade_alert.composite import evaluate_tree
from trade_alert.database import MIGRATIONS, open_database, rows
from trade_alert.definitions import definition_json, parse_definition, parse_tree
from trade_alert.delivery import NotificationDispatcher, parse_payload
from trade_alert.evaluators import (
    ObservedPeak,
    PeakConfig,
    queue_peak_drop,
)
from trade_alert.users import UserService


def threshold(side="BUY", below=True, volume=500, hysteresis=5):
    return dict(
        type=f"{side}_QUEUE_VOLUME_{'BELOW' if below else 'ABOVE'}",
        config=dict(thresholdVolume=volume, hysteresisPercent=hysteresis),
    )


def peak(side="BUY", percent=50, minimum=100, hysteresis=5):
    return dict(
        type=f"{side}_QUEUE_PEAK_DROP_PERCENT",
        config=dict(
            thresholdPercent=percent,
            minimumPeakVolume=minimum,
            hysteresisPercent=hysteresis,
        ),
    )


def rolling(side="BUY", minutes=5):
    return dict(
        type=f"{side}_QUEUE_DROP_PERCENT",
        config=dict(
            thresholdPercent=30, windowMinutes=minutes, minimumBaselineVolume=100
        ),
    )


class StrategyTests(unittest.IsolatedAsyncioTestCase):
    setUp = persistence.PersistenceTests.setUp
    restart = persistence.PersistenceTests.restart
    accept = persistence.PersistenceTests.accept
    events = persistence.PersistenceTests.events
    states = persistence.PersistenceTests.states

    async def test_threshold_crossings_equality_zero_and_both_sides(self):
        for side in ("BUY", "SELL"):
            for below in (True, False):
                with self.subTest(side=side, below=below):
                    self.db.execute("DELETE FROM alert_rules")
                    self.db.execute("DELETE FROM queue_snapshots")
                    self.clock = persistence.START
                    self.restart()
                    self.alerts.create("1", "123", threshold(side, below), 0)
                    original, active = (1000, 400) if below else (100, 600)
                    self.accept(0, original, side.lower())
                    self.accept(10, 500, side.lower())
                    self.assertEqual(len(self.events()), 1)
                    self.accept(20, active, side.lower())
                    self.restart()
                    self.accept(30, active, side.lower())
                    self.assertEqual(len(self.events()), 1)
                    # An opposite queue is UNKNOWN, including for recovery.
                    self.accept(40, 2000, "sell" if side == "BUY" else "buy")
                    self.assertEqual(self.states()[0]["status"], "WAITING_FOR_RECOVERY")
                    self.accept(50, original, side.lower())
                    self.accept(
                        60, 0 if below else 500, "none" if below else side.lower()
                    )
                    self.assertEqual(len(self.events()), 2)

    async def test_initially_satisfied_unknown_edit_enable_and_hysteresis(self):
        rule = self.alerts.create("1", "123", threshold(hysteresis=10), 0)
        self.accept(0, 100)
        self.restart()
        self.accept(10, 400)
        self.accept(20, 900, "sell")
        self.accept(30, 550)  # Exactly recovery boundary: still not ready.
        self.accept(40, 500)
        self.assertFalse(self.events())
        self.accept(50, 551)
        self.accept(60, 500)
        self.assertEqual(len(self.events()), 1)
        self.assertTrue(self.alerts.update("1", rule, threshold(), revision=1))
        self.accept(70, 100)
        self.accept(80, 100)
        self.assertEqual(len(self.events()), 1)
        self.alerts.set_enabled("1", rule, False)
        self.alerts.set_enabled("1", rule, True)
        self.accept(90, 100)
        self.accept(100, 100)
        self.assertEqual(len(self.events()), 1)
        self.accept(110, 1000)
        self.accept(120, 500)
        self.assertEqual(len(self.events()), 2)

    async def test_initial_unknown_does_not_establish_a_crossing(self):
        self.alerts.create("1", "123", threshold(), 0)
        self.accept(0, 1000, "sell")
        self.accept(10, 100)
        self.assertFalse(self.events())
        self.accept(20, 1000)
        self.accept(30, 500)
        self.assertEqual(len(self.events()), 1)

    async def test_peak_growth_restart_frozen_recovery_new_peak_and_session(self):
        self.alerts.create("1", "123", peak(), 0)
        self.accept(0, 500)
        self.accept(10, 1000)
        self.restart()
        self.accept(20, 500)
        self.assertEqual(
            json.loads(self.events()[0]["payload"])["previousVolume"], 1000
        )
        self.accept(30, 400)
        self.accept(40, 549)
        self.assertEqual(len(self.events()), 1)
        self.accept(50, 550)
        self.accept(60, 2000)
        self.accept(70, 1000)
        self.assertEqual(len(self.events()), 2)
        self.accept(80, 500, session="B")
        self.accept(90, 400, session="B")
        self.assertEqual(len(self.events()), 2)
        self.accept(100, 250, session="B")
        self.assertEqual(len(self.events()), 3)
        stored = rows(self.db, "SELECT * FROM queue_session_peaks ORDER BY session_id")
        self.assertEqual([r["buy_volume"] for r in stored], [2000, 500])
        self.assertEqual(stored[0]["first_observed_at"], persistence.START)

    async def test_peak_shared_sides_minimum_and_no_reset_on_rule_delete(self):
        self.alerts.create("1", "123", peak(minimum=2000), 0)
        self.alerts.create("2", "123", peak("SELL"), 0)
        self.accept(0, 1000)
        self.accept(10, 1000, "sell")
        self.accept(20, 500, "sell")
        self.assertEqual(len(self.events()), 1)
        self.accept(30, 500)
        self.assertEqual(len(self.events()), 1)
        self.db.execute("DELETE FROM alert_rules")
        self.assertEqual(
            rows(self.db, "SELECT * FROM queue_session_peaks")[0]["sell_volume"], 1000
        )
        self.alerts.create("1", "123", peak(), 0)
        self.accept(40, 400)  # Already satisfied against preexisting observed peak.
        self.accept(50, 300)
        self.assertFalse(self.events())
        self.accept(60, 600)
        self.accept(70, 500)
        self.assertEqual(len(self.events()), 1)

    async def test_peak_replays_late_unpersisted_stale_and_retention(self):
        self.alerts.create("1", "123", peak(), 0)
        accepted = self.accept(0, 1000)
        self.accept(20, 800)
        self.engine.on_snapshot(accepted, "A")
        late = persistence.snapshot(10, 5000)
        self.snapshots.save(late, self.clock, "A")
        self.engine.on_snapshot(late, "A")
        self.engine.on_snapshot(persistence.snapshot(20, 8000), "A")
        self.clock += 60_000
        self.engine.on_snapshot(persistence.snapshot(20, 800), "A")
        self.assertEqual(
            rows(self.db, "SELECT * FROM queue_session_peaks")[0]["buy_volume"], 1000
        )
        self.snapshots.delete_expired(self.clock)
        # Snapshot retention does not erase a recent session peak.
        self.assertEqual(
            rows(self.db, "SELECT * FROM queue_session_peaks")[0]["buy_volume"], 1000
        )
        self.assertEqual(self.snapshots.delete_expired_peaks(self.clock), 1)

    async def test_rolling_1_5_15_minutes_bounded_baselines_and_recovery(self):
        for minutes in (1, 5, 15):
            for side in ("BUY", "SELL"):
                with self.subTest(minutes=minutes, side=side):
                    self.db.execute("DELETE FROM alert_rules")
                    self.db.execute("DELETE FROM queue_snapshots")
                    self.clock = persistence.START
                    self.restart()
                    self.alerts.create("1", "123", rolling(side, minutes), 0)
                    self.accept(0, 1000, side.lower())
                    self.accept(minutes * 60 - 10, 900, side.lower())
                    self.assertFalse(self.events())
                    self.accept(minutes * 60, 700, side.lower())
                    self.assertEqual(len(self.events()), 1)
                    self.accept(minutes * 60 + 10, 500, side.lower())
                    self.assertEqual(len(self.events()), 1)
                    self.accept(minutes * 60 + 20, 750, side.lower())
                    self.assertEqual(self.states()[0]["status"], "ARMED")

    async def test_rolling_gaps_zero_opposite_and_cross_session_are_unknown(self):
        for baseline_side in ("none", "sell", "buy"):
            with self.subTest(side=baseline_side):
                self.db.execute("DELETE FROM alert_rules")
                self.db.execute("DELETE FROM queue_snapshots")
                self.clock = persistence.START
                self.restart()
                self.alerts.create("1", "123", rolling(minutes=15), 0)
                self.accept(0, 1000, baseline_side)
                self.accept(900 + 21, 100)
                self.assertFalse(self.events())
                self.accept(950, 100, session="B")
                self.accept(960, 10, session="B")
                self.assertFalse(self.events())

    async def test_composite_threshold_peak_and_rolling_share_context(self):
        tree = dict(
            type="GROUP",
            operator="AND",
            conditions=[threshold(volume=600), peak(), rolling(minutes=1)],
        )
        rule = await self.channels.create("1", ITEM, tree)
        self.accept(0, 1000)
        self.accept(50, 900)
        self.accept(60, 500)
        self.assertEqual(len(self.events()), 1)
        payload = json.loads(self.events()[0]["payload"])
        self.assertEqual(len(payload["matches"]), 3)
        self.assertEqual(payload["type"], "COMPOSITE")
        self.assertEqual(self.states()[0]["rule_id"], rule)
        self.accept(70, 400)
        self.accept(80, 300, "sell")
        self.assertEqual(len(self.events()), 1)
        self.accept(90, 800)
        self.assertEqual(self.states()[0]["status"], "ARMED")

    async def test_private_and_channel_outbox_send_current_destinations(self):
        await self.channels.refresh()
        self.alerts.create("1", "123", threshold(), 0)
        await self.channels.create("1", ITEM, peak())
        self.accept(0, 1000)
        self.accept(10, 500)
        send = AsyncMock()
        dispatcher = NotificationDispatcher(
            self.db, self.users, send, channels=self.channels, now=lambda: self.clock
        )
        await dispatcher.dispatch_once()
        await dispatcher.dispatch_once()
        self.assertEqual(
            {call.args[0] for call in send.call_args_list}, {"1", "-100123"}
        )
        messages = [call.args[1] for call in send.call_args_list]
        self.assertTrue(any("بیشترین حجم مشاهده‌شده" in m for m in messages))
        self.assertTrue(any("کمتر یا مساوی" in m for m in messages))
        self.assertTrue(all("تهران" in m and "اهمیت" in m for m in messages))
        for event in self.events():
            self.assertNotEqual(history_text(event), "هشدار قدیمی")
            parse_payload(event["payload"])
        await dispatcher.dispatch_once()
        self.assertEqual(send.await_count, 2)

    def test_strict_configuration_roundtrip_and_unknown_composites(self):
        for side in ("BUY", "SELL"):
            for definition in (
                threshold(side),
                threshold(side, False),
                peak(side),
                rolling(side, 15),
            ):
                self.assertEqual(
                    definition_json(parse_tree(definition, channel=False)), definition
                )
                for invalid in (True, "5", -1, float("nan"), 2**53):
                    bad = json.loads(json.dumps(definition))
                    field = (
                        "thresholdVolume"
                        if "thresholdVolume" in bad["config"]
                        else "minimumPeakVolume"
                        if "minimumPeakVolume" in bad["config"]
                        else "windowMinutes"
                    )
                    bad["config"][field] = invalid
                    self.assertIsNone(parse_definition(json.dumps(bad)))
        unknown = persistence.snapshot(10, 1000, "sell")
        tree = parse_tree(
            dict(type="GROUP", operator="OR", conditions=[threshold(), peak()])
        )
        self.assertEqual(evaluate_tree(tree, unknown, lambda at: None).truth, "UNKNOWN")
        current = persistence.snapshot(10, 500)
        self.assertEqual(
            queue_peak_drop(
                "buy", PeakConfig(50), current, ObservedPeak(1000, persistence.START)
            ).status,
            "TRIGGER",
        )

    async def test_additive_upgrade_preserves_phase7_rules_destinations_and_history(
        self,
    ):
        # Build an actual pre-Phase-8 database, then upgrade with the current journal.
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            old = folder / "migrations"
            shutil.copytree(MIGRATIONS, old)
            journal_path = old / "meta/_journal.json"
            journal = json.loads(journal_path.read_text())
            journal["entries"] = journal["entries"][:7]
            journal_path.write_text(json.dumps(journal))
            path = folder / "old.db"
            with open_database(path, migrations=old) as db:
                users = UserService(db, ["1"], lambda: persistence.START)
                users.register("1", "1")
                users.add_symbol("1", ITEM)
                alerts = AlertService(db, users, now=lambda: persistence.START)
                private = alerts.create("1", "123", rolling(minutes=5))
                channels = ChannelService(
                    db,
                    users,
                    ["1"],
                    "-100123",
                    AsyncMock(
                        return_value=dict(
                            channelId="-100123", title="Test", canPost=True
                        )
                    ),
                    now=lambda: persistence.START,
                )
                channel = await channels.create(
                    "1",
                    ITEM,
                    dict(type="GROUP", operator="OR", conditions=[rolling(minutes=1)]),
                )
                users.set_notifications("1", False)
                db.execute(
                    "INSERT INTO alert_rules(id, owner_id, ins_code, type, definition, created_at) VALUES ('old', NULL, '123', 'BUY_QUEUE_DISAPPEARED', NULL, 1)"
                )
                for index, rule in enumerate((private, channel)):
                    event = f"event-{index}"
                    db.execute(
                        "INSERT INTO alert_states(rule_id, revision, session_id, status, last_evaluated_at, baseline_volume, recovery_data, quality_data) VALUES (?, 1, 'A', 'WAITING_FOR_RECOVERY', 10, 1000, '[]', '{}')",
                        (rule,),
                    )
                    db.execute(
                        "INSERT INTO alert_events(id, rule_id, message, created_at, occurrence_key, payload, severity) VALUES (?, ?, 'old-message', 10, ?, '{}', 'important')",
                        (event, rule, event),
                    )
                    db.execute(
                        "INSERT INTO notification_outbox(event_id, next_attempt_at, attempts) VALUES (?, 20, 2)",
                        (event,),
                    )
                    db.execute(
                        "INSERT INTO alert_outcomes(event_id, source_event_id, owner_id, channel_id, ins_code, session_id, window_minutes, target_at, cutoff_at, initial_queue_volume, queue_side) VALUES (?, ?, '1', ?, '123', 'A', 5, 300000, 330000, 500, 'buy')",
                        (event, event, None if index == 0 else "-100123"),
                    )
                    db.execute(
                        "INSERT INTO alert_suppressions VALUES (?, 0, 3)", (rule,)
                    )
                tables = (
                    "symbols",
                    "users",
                    "watchlists",
                    "telegram_channels",
                    "alert_rules",
                    "alert_states",
                    "alert_events",
                    "notification_outbox",
                    "alert_outcomes",
                    "alert_suppressions",
                )
                before = {
                    table: rows(db, f"SELECT * FROM {table} ORDER BY rowid")
                    for table in tables
                }
                hashes = db.execute(
                    "SELECT hash FROM __drizzle_migrations ORDER BY created_at"
                ).fetchall()
            with open_database(path) as db:
                for table in tables:
                    after = rows(db, f"SELECT * FROM {table} ORDER BY rowid")
                    if table == "alert_states":
                        for row in after:
                            self.assertIsNone(row.pop("strategy_data"))
                    self.assertEqual(after, before[table], table)
                self.assertEqual(
                    db.execute(
                        "SELECT hash FROM __drizzle_migrations ORDER BY created_at"
                    ).fetchall()[:7],
                    hashes,
                )
                self.assertEqual(
                    db.execute("PRAGMA integrity_check").fetchone()[0], "ok"
                )
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(
                    db.execute("SELECT count(*) FROM queue_session_peaks").fetchone()[
                        0
                    ],
                    0,
                )
