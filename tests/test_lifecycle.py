"""Synthetic accepted lifecycle evidence, temporary SQLite and fake delivery only."""

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import test_persistence as persistence
from test_config import REQUIRED
from test_services import ITEM

from trade_alert.alerts import AlertService
from trade_alert.bot_text import history_text
from trade_alert.composite import evaluate_tree
from trade_alert.config import LIFECYCLE_NUMBERS, parse_env
from trade_alert.database import MIGRATIONS, open_database, rows
from trade_alert.definitions import definition_json, parse_definition, parse_tree
from trade_alert.delivery import NotificationDispatcher, parse_payload
from trade_alert.engine import AlertEngine
from trade_alert.lifecycle import LifecycleContext, LifecycleOptions, LifecycleTracker
from trade_alert.snapshots import SnapshotRepository
from trade_alert.users import UserService


def lifecycle(kind="FORMED", side="BUY", minimum=0, percent=30, duration=0):
    config = (
        dict(thresholdPercent=percent, minimumBaselineVolume=minimum)
        if kind == "WEAKENED"
        else dict(minimumPreviousVolume=minimum)
        if kind == "CONFIRMED_DISAPPEARED"
        else dict(minimumVolume=minimum, minimumDisappearanceSeconds=duration)
        if kind == "REFORMED"
        else dict(minimumVolume=minimum)
    )
    return dict(type=f"{side}_QUEUE_{kind}", config=config)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    setUp = persistence.PersistenceTests.setUp
    accept = persistence.PersistenceTests.accept
    events = persistence.PersistenceTests.events

    def restart(self):
        self.options = getattr(
            self, "options", LifecycleOptions(confirmation_snapshots=1)
        )
        self.tracker = LifecycleTracker(self.db, self.options)
        self.engine = AlertEngine(
            self.db,
            self.snapshots,
            self.users,
            now=lambda: self.clock,
            channels=self.channels,
            logger=self.logger,
            lifecycle=self.tracker,
        )

    def life_events(self, side="buy"):
        return rows(
            self.db,
            "SELECT * FROM queue_lifecycle_events WHERE side=? ORDER BY timestamp",
            (side,),
        )

    def state(self, side="buy"):
        return rows(
            self.db,
            "SELECT * FROM queue_lifecycle_states WHERE ins_code='123' AND side=?",
            (side,),
        )[0]

    def rule(self, kind="FORMED", **kwargs):
        return self.alerts.create("1", "123", lifecycle(kind, **kwargs), 0)

    async def test_complete_cycles_both_sides_preserve_actual_lineage(self):
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                self.db.execute("DELETE FROM queue_lifecycle_states")
                self.db.execute("DELETE FROM queue_lifecycle_events")
                self.db.execute("DELETE FROM queue_snapshots")
                self.clock = persistence.START
                self.restart()
                self.accept(0, side="none")
                self.assertEqual(self.state(side)["status"], "NO_QUEUE")
                self.accept(10, 1000, side)
                self.accept(20, 800, side)
                self.accept(30, 700, side)
                self.accept(40, side="none")
                self.accept(50, 500, side)
                self.accept(60, 500, side)
                self.accept(70, side="none")
                self.accept(80, 700, side)
                events = self.life_events(side)
                self.assertEqual(
                    [e["event_type"] for e in events],
                    [
                        "QUEUE_FORMED",
                        "QUEUE_WEAKENED",
                        "QUEUE_WEAKENED",
                        "QUEUE_DISAPPEARED",
                        "QUEUE_REFORMED",
                        "QUEUE_DISAPPEARED",
                        "QUEUE_REFORMED",
                    ],
                )
                gone = events[3]
                self.assertEqual(
                    (gone["previous_volume"], gone["previous_timestamp"]),
                    (700, persistence.START + 30_000),
                )
                self.assertEqual(events[4]["disappeared_at"], gone["timestamp"])
                self.assertEqual(
                    events[1]["previous_timestamp"], persistence.START + 10_000
                )
                self.assertEqual(self.state(side)["cycles"], 2)
                self.assertEqual(
                    self.state(side)["first_observed_at"], persistence.START
                )

    async def test_initial_queue_is_not_formation_and_none_is_required(self):
        self.rule()
        self.accept(0, 1000)
        self.accept(10, 1000)
        self.assertFalse(self.life_events())
        self.accept(20, 2000, "sell")
        self.assertEqual(self.state()["status"], "ACTIVE")
        self.assertFalse(self.state()["observation_valid"])
        self.accept(30, 500)
        self.accept(40, 500)
        self.assertFalse(self.life_events())
        self.assertFalse(self.events())

    async def test_buy_sell_weakening_configuration_is_independent(self):
        self.rule("WEAKENED", side="BUY", percent=10)
        self.rule("WEAKENED", side="SELL", percent=50)
        self.accept(0, 1000)
        self.accept(10, 900)
        self.accept(20, 1000, "sell")
        self.accept(30, 900, "sell")
        self.assertEqual(len(self.life_events("sell")), 0)
        self.accept(40, 800, "sell")
        self.accept(50, 500, "sell")
        self.assertEqual(len(self.life_events("buy")), 1)
        self.assertEqual(len(self.life_events("sell")), 2)
        self.assertEqual(len(self.events()), 2)

    async def test_confirmation_duplicate_interruptions_and_return_do_not_create_events(
        self,
    ):
        self.options = LifecycleOptions(confirmation_snapshots=2)
        self.restart()
        first = self.accept(0, side="none")
        self.engine.on_snapshot(first, "A")
        self.assertEqual(self.state()["pending_count"], 1)
        self.accept(10, side="none")
        self.accept(20, 1000)
        self.accept(30, side="none")
        self.accept(40, 1000)
        self.assertFalse(self.life_events())
        self.accept(50, 1000)
        self.assertEqual(len(self.life_events()), 1)
        self.tracker.unavailable("123", "trading-state")
        self.assertEqual(self.state()["status"], "ACTIVE")
        self.assertFalse(self.state()["observation_valid"])
        self.accept(60, side="none")
        self.accept(70, side="none")
        self.assertEqual(self.state()["status"], "NO_QUEUE")
        self.assertEqual(len(self.life_events()), 1)
        self.accept(80, 500)
        self.accept(90, 500)
        self.accept(100, 500)
        self.accept(110, 500)
        self.assertEqual(
            [e["event_type"] for e in self.life_events()],
            ["QUEUE_FORMED", "QUEUE_FORMED"],
        )
        self.assertEqual(self.state()["cycles"], 0)

    async def test_long_gap_new_session_stale_late_unpersisted_and_unknown(self):
        self.accept(0, side="none")
        self.accept(10, 1000)
        self.accept(80, side="none")  # Missing interval cannot establish disappearance.
        self.accept(90, 500)
        self.assertEqual(
            [e["event_type"] for e in self.life_events()],
            ["QUEUE_FORMED", "QUEUE_FORMED"],
        )
        self.accept(100, 600, session="B")
        self.accept(110, 600, session="B")
        self.assertEqual(self.state()["cycles"], 0)
        self.assertEqual(self.state()["session_id"], "B")
        before = self.state().copy()
        late = persistence.snapshot(105, 5000)
        self.snapshots.save(late, self.clock, "B")
        self.engine.on_snapshot(late, "B")
        self.engine.on_snapshot(persistence.snapshot(110, 3000), "B")
        self.clock += 60_000
        self.engine.on_snapshot(persistence.snapshot(110, 600), "B")
        self.assertEqual(self.state(), before)
        self.assertFalse([e for e in self.life_events() if e["session_id"] == "B"])

    async def test_restart_preserves_confirmation_event_ids_and_cycles(self):
        self.options = LifecycleOptions(confirmation_snapshots=2)
        self.restart()
        self.accept(0, side="none")
        self.accept(10, side="none")
        self.accept(20, 1000)
        self.context.__exit__(None, None, None)
        self.context = open_database(self.path)
        self.db = self.context.__enter__()
        self.users = UserService(self.db, ["1", "2"], lambda: self.clock)
        self.alerts = AlertService(self.db, self.users, now=lambda: self.clock)
        self.snapshots = SnapshotRepository(self.db)
        self.restart()
        formed = self.accept(30, 1000)
        events = self.life_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "QUEUE_FORMED")
        self.engine.on_snapshot(formed, "A")
        self.restart()
        self.engine.on_snapshot(formed, "A")
        self.assertEqual(self.life_events(), events)

    async def test_weakening_exact_demand_thresholds_minimum_and_frozen_recovery(self):
        self.rule("WEAKENED", percent=25, minimum=1000)
        self.rule("WEAKENED", percent=50, minimum=1000)
        self.accept(0, 1000)
        self.accept(10, 900)
        self.accept(20, 750)
        self.assertEqual(len(self.events()), 1)
        self.accept(30, 500)
        self.assertEqual(len(self.events()), 2)
        weak = self.life_events()[-1]
        self.assertEqual(weak["previous_volume"], 1000)
        self.assertEqual(weak["occurrence_at"], persistence.START + 20_000)
        self.accept(40, 600)
        self.accept(50, 800)  # 25% trigger recovers at 80% of frozen baseline.
        self.accept(60, 750)
        self.assertEqual(len(self.events()), 3)
        peak = rows(self.db, "SELECT buy_volume FROM queue_session_peaks")[0][
            "buy_volume"
        ]
        self.assertEqual(peak, 1000)

    async def test_formation_minimum_and_reformation_minimum_duration(self):
        self.rule(minimum=2000)
        self.rule("REFORMED", minimum=500, duration=20)
        self.accept(0, side="none")
        self.accept(10, 1000)
        self.assertFalse(self.events())
        self.accept(20, side="none")
        self.accept(30, 600)  # Too soon; the event is not kept TRUE later.
        self.accept(40, 600)
        self.assertFalse(self.events())
        self.accept(50, side="none")
        self.accept(60, side="none")
        self.accept(70, 700)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.state()["cycles"], 2)

    async def test_private_channel_delivery_and_shared_event_not_per_user(self):
        self.rule()
        self.alerts.create("2", "123", lifecycle(), 0)
        await self.channels.create("1", ITEM, lifecycle())
        self.accept(0, side="none")
        self.accept(10, 1000)
        self.assertEqual(len(self.life_events()), 1)
        self.assertEqual(len(self.events()), 3)
        send = AsyncMock()
        worker = NotificationDispatcher(
            self.db, self.users, send, channels=self.channels, now=lambda: self.clock
        )
        for _ in range(4):
            await worker.dispatch_once()
        self.assertEqual(
            {c.args[0] for c in send.call_args_list}, {"1", "2", "-100123"}
        )
        self.assertEqual(send.await_count, 3)
        for event in self.events():
            parse_payload(event["payload"])
            message = history_text(event)
            self.assertIn("تشکیل صف خرید", message)
            self.assertIn("اهمیت", message)
            self.assertIn("تهران", message)
            self.assertIn("نه توصیهٔ معامله", message)

    async def test_composites_require_same_update_and_rearm_for_new_pulses(self):
        volume = dict(type="BUY_QUEUE_VOLUME_ABOVE", config=dict(minimumVolume=500))
        tree = dict(
            type="GROUP", operator="AND", conditions=[lifecycle("REFORMED"), volume]
        )
        await self.channels.create("1", ITEM, tree)
        self.accept(0, 1000)
        self.accept(10, side="none")
        self.accept(20, 300)  # Reformation pulse, volume condition is FALSE.
        self.accept(30, 1000)  # Old reformation cannot satisfy the new volume.
        self.assertFalse(self.events())
        self.accept(40, side="none")
        self.accept(50, 1000)
        self.assertEqual(len(self.events()), 1)
        self.accept(60, side="none")
        self.accept(70, 1000)
        self.assertEqual(len(self.events()), 2)
        unknown = evaluate_tree(
            parse_tree(tree),
            persistence.snapshot(100, 1000),
            lambda t: None,
            lifecycle=None,
        )
        self.assertEqual(unknown.truth, "UNKNOWN")
        clear = evaluate_tree(
            parse_tree(tree),
            persistence.snapshot(100, 1000),
            lambda t: None,
            lifecycle=LifecycleContext(frozenset({"buy"})),
        )
        self.assertEqual(clear.truth, "FALSE")

    async def test_cooldown_independent_rules_and_no_replay(self):
        rule = self.alerts.create("1", "123", lifecycle("REFORMED"), 30)
        self.accept(0, 1000)
        self.accept(10, side="none")
        first = self.accept(20, 1000)
        self.accept(30, side="none")
        self.accept(40, 1000)
        self.accept(50, side="none")
        self.accept(60, 1000)
        self.assertEqual(len(self.events()), 2)
        self.engine.on_snapshot(first, "A")
        self.assertEqual(len(self.events()), 2)
        self.assertEqual(self.alerts.get("1", rule)["cooldown_seconds"], 30)

    async def test_weakening_escalation_uses_quality_policy_and_disappearance_ends_it(
        self,
    ):
        rule = self.rule("WEAKENED", percent=30)
        self.alerts.admins = ["1"]
        policy = dict(
            escalation_enabled=True,
            additional_drop_percent=20,
            minimum_interval_seconds=10,
        )
        self.assertTrue(
            self.alerts.update("1", rule, lifecycle("WEAKENED"), quality=policy)
        )
        self.accept(0, 1000)
        self.accept(10, 700)
        self.accept(20, 500)
        self.assertEqual(len(self.events()), 2)
        self.assertTrue(json.loads(self.events()[1]["payload"])["escalation"])
        self.accept(30, side="none")
        self.assertEqual(len(self.events()), 2)

    async def test_event_state_transaction_retention_does_not_delete_active_state(self):
        self.accept(0, side="none")
        original = self.tracker._save

        def fail(state):
            if state["side"] == "sell":
                raise RuntimeError("rollback")
            original(state)

        with patch.object(self.tracker, "_save", side_effect=fail):
            self.assertTrue(
                self.snapshots.save(
                    persistence.snapshot(10, 1000), persistence.START + 10_000, "A"
                )
            )
            self.clock += 10_000
            with self.assertRaises(RuntimeError):
                self.engine.on_snapshot(persistence.snapshot(10, 1000), "A")
        self.assertFalse(self.life_events())
        self.assertEqual(self.state()["status"], "NO_QUEUE")
        self.engine.on_snapshot(persistence.snapshot(10, 1000), "A")
        self.assertEqual(len(self.life_events()), 1)
        saved = self.state()
        self.assertEqual(self.tracker.delete_expired(self.clock + 31 * 86_400_000), 1)
        self.assertEqual(self.state(), saved)
        self.db.execute("DELETE FROM alert_rules")
        self.assertEqual(self.state(), saved)

    async def test_missing_data_return_cannot_escalate_an_old_weakening_occurrence(
        self,
    ):
        rule = self.rule("WEAKENED", percent=30)
        self.alerts.admins = ["1"]
        self.alerts.update(
            "1",
            rule,
            lifecycle("WEAKENED"),
            quality=dict(
                escalation_enabled=True,
                additional_drop_percent=20,
                minimum_interval_seconds=10,
            ),
        )
        self.accept(0, 1000)
        self.accept(10, 700)
        self.accept(20, 500)
        self.assertEqual(len(self.events()), 2)
        self.tracker.unavailable("123")
        self.accept(30, 100)
        self.accept(40, 100)
        self.assertEqual(len(self.events()), 2)
        self.accept(50, 70)
        self.assertEqual(len(self.events()), 3)
        payload = json.loads(self.events()[-1]["payload"])
        self.assertNotIn("escalation", payload)
        self.assertEqual(payload["previousVolume"], 100)

    async def test_unconfirmed_disappearance_cannot_recover_weakening(self):
        self.options = LifecycleOptions(confirmation_snapshots=2)
        self.restart()
        self.rule("WEAKENED", percent=30)
        self.accept(0, 1000)
        self.accept(10, 1000)
        self.accept(20, 700)
        self.accept(30, 700)
        self.assertEqual(len(self.events()), 1)
        self.accept(40, side="none")
        state = rows(self.db, "SELECT * FROM alert_states")[0]
        self.assertEqual(state["status"], "WAITING_FOR_RECOVERY")
        self.assertIsNotNone(state["quality_data"])
        self.accept(50, 700)
        self.assertEqual(len(self.events()), 1)

    def test_strict_definitions_global_settings_and_unchanged_old_migration_bytes(self):
        for side in ("BUY", "SELL"):
            for kind in ("FORMED", "WEAKENED", "CONFIRMED_DISAPPEARED", "REFORMED"):
                value = lifecycle(kind, side)
                self.assertEqual(
                    definition_json(parse_tree(value, channel=False)), value
                )
                for invalid in (-1, True, "1", float("inf"), 2**53):
                    bad = json.loads(json.dumps(value))
                    field = next(iter(bad["config"]))
                    bad["config"][field] = invalid
                    self.assertIsNone(parse_definition(json.dumps(bad)))
        for key, (_, minimum, maximum) in LIFECYCLE_NUMBERS.items():
            for invalid in ("true", "1.5", "NaN", "-1", str(maximum + 1)):
                with self.assertRaisesRegex(ValueError, key):
                    parse_env({**REQUIRED, key: invalid})
            self.assertEqual(parse_env({**REQUIRED, key: str(minimum)})[key], minimum)

    async def test_phase8_upgrade_preserves_all_existing_tables_and_migration_hashes(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            old = Path(directory) / "migrations"
            shutil.copytree(MIGRATIONS, old)
            journal_path = old / "meta/_journal.json"
            journal = json.loads(journal_path.read_text())
            journal["entries"] = journal["entries"][:8]
            journal_path.write_text(json.dumps(journal))
            path = Path(directory) / "phase8.db"
            with open_database(path, migrations=old) as db:
                users = UserService(db, ["1"], lambda: persistence.START)
                users.register("1", "1")
                users.add_symbol("1", ITEM)
                rule = AlertService(db, users, now=lambda: persistence.START).create(
                    "1",
                    "123",
                    dict(
                        type="BUY_QUEUE_VOLUME_BELOW", config=dict(thresholdVolume=500)
                    ),
                )
                db.execute(
                    "INSERT INTO alert_states(rule_id, revision, session_id, status, last_evaluated_at, strategy_data) VALUES (?, 1, 'A', 'ARMED', 10, '{\"0\":true}')",
                    (rule,),
                )
                SnapshotRepository(db).observe_peaks(persistence.snapshot(0), "A")
                tables = [
                    r[0]
                    for r in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                    )
                ]
                before = {t: rows(db, f'SELECT * FROM "{t}"') for t in tables}
                digests = {
                    e["tag"]: hashlib.sha256(
                        (old / (e["tag"] + ".sql")).read_bytes()
                    ).hexdigest()
                    for e in journal["entries"]
                }
            with open_database(path) as db:
                for table in tables:
                    if table == "__drizzle_migrations":
                        self.assertEqual(
                            rows(db, "SELECT * FROM __drizzle_migrations")[:8],
                            before[table],
                        )
                    else:
                        after = rows(db, f'SELECT * FROM "{table}"')
                        if table == "alert_states":
                            for row in after:
                                self.assertIsNone(row.pop("evaluation_detail"))
                        self.assertEqual(after, before[table])
                self.assertEqual(
                    db.execute("PRAGMA integrity_check").fetchone()[0], "ok"
                )
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(
                    db.execute(
                        "SELECT count(*) FROM queue_lifecycle_states"
                    ).fetchone()[0],
                    0,
                )
            for tag, digest in digests.items():
                self.assertEqual(
                    hashlib.sha256(
                        (MIGRATIONS / (tag + ".sql")).read_bytes()
                    ).hexdigest(),
                    digest,
                )
