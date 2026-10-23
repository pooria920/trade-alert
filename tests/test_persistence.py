"""Session-scoped history and atomic engine checks; no network or real database."""

import asyncio
import json
import logging
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from reference import reference
from test_services import DROP, GROUP, ITEM

from trade_alert.alerts import AlertService
from trade_alert.channels import ChannelService
from trade_alert.cleanup import SnapshotCleanup
from trade_alert.database import open_database, rows
from trade_alert.engine import AlertEngine
from trade_alert.evaluators import QueueSnapshot
from trade_alert.snapshots import SnapshotRepository
from trade_alert.users import UserService

START = 1_800_000_000_000
GONE = dict(type="BUY_QUEUE_DISAPPEARED", config=dict(minimumPreviousVolume=0))


def snapshot(seconds=0, volume=1000, side="buy"):
    return QueueSnapshot(
        "123",
        "TEST",
        START + seconds * 1000,
        side,
        0 if side == "none" else volume,
        None if side == "none" else 110,
        0 if side == "none" else 1,
    )


def create(definition=DROP, **kwargs):
    return dict(op="create", now=0, id="rule", definition=definition, **kwargs)


def update(now, volume=1000, **kwargs):
    return dict(op="update", now=now, volume=volume, **kwargs)


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "engine.db"
        self.context = open_database(self.path)
        self.db = self.context.__enter__()
        self.addCleanup(lambda: self.context.__exit__(None, None, None))
        self.clock = START
        self.users = UserService(self.db, ["1", "2"], lambda: self.clock)
        self.alerts = AlertService(self.db, self.users, now=lambda: self.clock)
        for actor in ("1", "2"):
            self.users.register(actor, actor)
            self.users.add_symbol(actor, ITEM)

        async def verify(channel):
            return dict(channelId=channel, title="Test", canPost=True)

        self.admins = ["1", "2"]
        self.channels = ChannelService(
            self.db,
            self.users,
            self.admins,
            "-100123",
            verify,
            cooldown=0,
            now=lambda: self.clock,
        )
        self.snapshots = SnapshotRepository(self.db)
        self.logger = logging.getLogger("offline-engine-test")
        self.logger.disabled = True
        self.restart()

    def restart(self):
        self.engine = AlertEngine(
            self.db,
            self.snapshots,
            self.users,
            now=lambda: self.clock,
            channels=self.channels,
            logger=self.logger,
        )

    def accept(self, seconds, volume=1000, side="buy", session="A"):
        self.clock = START + seconds * 1000
        value = snapshot(seconds, volume, side)
        inserted = self.snapshots.save(value, self.clock, session)
        if inserted:
            self.engine.on_snapshot(value, session)
        return value

    def states(self):
        return rows(self.db, "SELECT * FROM alert_states ORDER BY rule_id")

    def events(self):
        return rows(self.db, "SELECT * FROM alert_events")

    def test_snapshot_provenance_deduplication_and_conflict(self):
        value = snapshot(0, 0, "none")
        self.assertTrue(self.snapshots.save(value, START + 100, "A"))
        self.assertFalse(self.snapshots.save(value, START + 200, "B"))
        stored = self.snapshots.get_latest_snapshot("123")
        self.assertEqual((stored.volume, stored.collected_at), (0, START + 100))
        self.assertFalse(self.snapshots.has_alert_snapshot(value, "B"))
        self.assertIsNone(self.snapshots.alert_baseline("123", START, "B"))
        self.assertTrue(self.snapshots.has_alert_snapshot(value, "A"))
        self.assertFalse(
            self.snapshots.has_alert_snapshot(replace(value, symbol="OTHER"), "A")
        )
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            self.snapshots.save(snapshot(), START, "A")
        with self.assertRaisesRegex(ValueError, "precedes"):
            self.snapshots.save(value, START - 1)
        for invalid in (-1, True, "1", 2**53):
            with self.assertRaises(ValueError):
                self.snapshots.save(value, invalid)

    def test_bounded_at_or_before_session_history_and_unknown_provenance(self):
        for age in (0, 70, 320, 400):
            self.snapshots.save(snapshot(-age), START, "A")
        for at, expected in ((-60, -70), (-300, -320), (-90, None), (-330, None)):
            found = self.snapshots.get_snapshot_at_or_before("123", START + at * 1000)
            self.assertEqual(
                found.timestamp if found else None,
                START + expected * 1000 if expected is not None else None,
            )
        self.assertEqual(
            [
                s.timestamp
                for s in self.snapshots.get_snapshots_between(
                    "123", START - 320_000, START - 70_000
                )
            ],
            [START - 320_000, START - 70_000],
        )
        with self.assertRaises(ValueError):
            self.snapshots.get_snapshots_between("123", START, START - 1)
        self.assertIsNone(self.snapshots.alert_baseline("123", START, "B"))
        self.db.execute("DELETE FROM queue_snapshots")
        for at, symbol, collected, session in (
            (START, None, START, "A"),
            (START - 1, "TEST", None, "A"),
            (START - 2, "TEST", START, None),
        ):
            self.db.execute(
                "INSERT INTO queue_snapshots (ins_code, timestamp, symbol, collected_at, session_id, queue_type, volume, order_count) VALUES ('123', ?, ?, ?, ?, 'none', 0, 0)",
                (at, symbol, collected, session),
            )
        self.assertIsNone(self.snapshots.alert_baseline("123", START, "A"))
        self.assertEqual(self.snapshots.get_latest_snapshot("123").timestamp, START - 2)
        with self.assertRaisesRegex(ValueError, "legacy"):
            self.snapshots.save(
                replace(snapshot(0, 0, "none"), timestamp=START - 1), START
            )

    def test_retention_is_bounded_and_preserves_configuration(self):
        rule = self.alerts.create("1", "123", DROP)
        self.db.executemany(
            "INSERT INTO queue_snapshots (ins_code, timestamp, queue_type, volume, order_count) VALUES ('123', ?, 'none', 0, 0)",
            [(n,) for n in range(1002)],
        )
        self.assertEqual(self.snapshots.delete_expired(1001), 1000)
        self.assertEqual(self.snapshots.delete_expired(1001), 1)
        self.assertEqual(
            self.db.execute("SELECT timestamp FROM queue_snapshots").fetchall(),
            [(1001,)],
        )
        self.assertIsNotNone(self.alerts.get("1", rule))
        self.assertIsNotNone(self.users.find_symbol("2", "123"))
        for batch in (0, 1001, 1.5, True):
            with self.assertRaises(ValueError):
                self.snapshots.delete_expired(1001, batch)

    def test_atomic_rollback_then_safe_retry_and_rule_isolation(self):
        self.alerts.create("1", "123", DROP)
        second = self.alerts.create("2", "123", DROP)
        self.accept(0)
        self.db.execute(
            "CREATE TRIGGER reject_outbox BEFORE INSERT ON notification_outbox WHEN (SELECT rule_id FROM alert_events WHERE id=NEW.event_id) != '"
            + second
            + "' BEGIN SELECT RAISE(ABORT, 'secret test failure'); END"
        )
        with patch.object(self.logger, "error") as log:
            value = self.accept(60, 750)
        self.assertEqual(log.call_count, 1)
        self.assertNotIn("secret", str(log.call_args))
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.events()[0]["rule_id"], second)
        self.assertEqual(
            sorted(s["last_evaluated_at"] for s in self.states()),
            [START, START + 60_000],
        )
        self.db.execute("DROP TRIGGER reject_outbox")
        self.engine.on_snapshot(value, "A")
        self.engine.on_snapshot(value, "A")
        self.assertEqual(len(self.events()), 2)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM notification_outbox").fetchone()[0], 2
        )

    def test_restart_after_database_close_preserves_frozen_baseline_and_cooldown(self):
        self.alerts.create("1", "123", DROP)
        self.accept(0)
        self.accept(60, 750)
        self.context.__exit__(None, None, None)
        self.context = open_database(self.path)
        self.db = self.context.__enter__()
        self.clock = START + 70_000
        self.users = UserService(self.db, ["1", "2"])
        self.snapshots = SnapshotRepository(self.db)
        self.restart()
        self.engine.on_snapshot(snapshot(60, 750), "A")
        self.assertEqual(self.states()[0]["last_evaluated_at"], START + 60_000)
        self.accept(80, 799)
        self.accept(90, 800)  # recovery alone is insufficient before cooldown
        self.assertEqual(self.states()[0]["status"], "WAITING_FOR_RECOVERY")
        self.assertEqual(self.states()[0]["baseline_volume"], 1000)
        self.accept(361, 800)
        self.assertEqual(self.states()[0]["status"], "ARMED")
        self.accept(421, 600)
        self.assertEqual(len(self.events()), 2)

    async def test_engine_authorization_quarantine_and_channel_independence(self):
        private = self.alerts.create("1", "123", DROP)
        channel = await self.channels.create("2", ITEM, GROUP)
        for id, owner, definition in (
            ("legacy", None, json.dumps(DROP)),
            ("bad", "2", "{"),
        ):
            self.db.execute(
                "INSERT INTO alert_rules (id, owner_id, ins_code, type, definition, created_at) VALUES (?, ?, '123', 'BUY_QUEUE_DISAPPEARED', ?, ?)",
                (id, owner, definition, START),
            )
        self.db.execute("DELETE FROM watchlists WHERE user_id='2'")
        self.users.set_notifications("2", False)
        self.accept(0)
        self.accept(60, 750)
        self.assertEqual({e["rule_id"] for e in self.events()}, {private, channel})
        self.assertEqual({s["rule_id"] for s in self.states()}, {private, channel})
        self.admins.remove("2")
        self.accept(70, 1000)
        self.assertEqual(
            next(s for s in self.states() if s["rule_id"] == channel)[
                "last_evaluated_at"
            ],
            START + 60_000,
        )
        self.users.allowed = frozenset({"2"})
        self.accept(80, 1000)
        self.assertEqual(
            next(s for s in self.states() if s["rule_id"] == private)[
                "last_evaluated_at"
            ],
            START + 70_000,
        )

    def test_duplicate_occurrence_cannot_insert_second_outbox(self):
        rule = self.alerts.create("1", "123", DROP)
        self.accept(0)
        self.db.execute(
            "INSERT INTO alert_events (id, rule_id, occurrence_key, message, created_at) VALUES ('existing', ?, ?, '', ?)",
            (rule, f"{rule}:1:A:{START + 60_000}", START),
        )
        self.accept(60, 750)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM notification_outbox").fetchone()[0], 0
        )
        self.assertEqual(self.states()[0]["status"], "TRIGGERED")

    def test_freshness_persisted_identity_and_priming_guards(self):
        self.clock = START + 10_000
        self.alerts.create("1", "123", DROP)
        self.clock = START + 70_000
        for value, session in (
            (snapshot(0), "A"),  # older than rule creation
            (snapshot(71), "A"),  # future
            (snapshot(39), "A"),  # stale
            (snapshot(70), "B"),  # wrong persisted session
            (replace(snapshot(70), symbol="OTHER"), "A"),
            (snapshot(69), "A"),  # not persisted
        ):
            if value.timestamp <= self.clock and value.timestamp != START + 69_000:
                self.snapshots.save(
                    snapshot((value.timestamp - START) // 1000), self.clock, "A"
                )
            self.engine.on_snapshot(value, session)
        self.assertEqual(self.states(), [])
        self.engine.on_snapshot(snapshot(70), "A")
        self.assertEqual(self.states()[0]["status"], "ARMED")
        self.assertEqual(self.events(), [])

    def test_history_is_shared_per_update_across_owners(self):
        self.alerts.create("1", "123", DROP)
        self.alerts.create("2", "123", DROP)
        self.accept(0)
        with patch.object(
            self.snapshots, "alert_baseline", wraps=self.snapshots.alert_baseline
        ) as lookup:
            self.accept(60, 750)
        self.assertEqual(lookup.call_count, 2)  # window baseline and previous tick
        self.assertEqual(len(self.events()), 2)

    async def test_cleanup_yields_does_not_overlap_and_joins_on_stop(self):
        self.db.executemany(
            "INSERT INTO queue_snapshots (ins_code, timestamp, queue_type, volume, order_count) VALUES ('123', ?, 'none', 0, 0)",
            [(n,) for n in range(2001)],
        )
        stop = asyncio.Event()
        cleanup = SnapshotCleanup(self.snapshots, 0, 900_000, self.logger)
        with patch.object(
            self.snapshots, "delete_expired", wraps=self.snapshots.delete_expired
        ) as delete:
            cleanup.run_if_due(START, stop)
            task = cleanup.task
            cleanup.run_if_due(START + 1_000_000, stop)
            self.assertIs(cleanup.task, task)
            await asyncio.sleep(0)
            self.assertEqual(delete.call_count, 1)
            self.assertEqual(
                self.db.execute("SELECT count(*) FROM queue_snapshots").fetchone()[0],
                1001,
            )
            stop.set()
            await cleanup.wait()
            self.assertEqual(delete.call_count, 1)
            stop.clear()
            cleanup.run_if_due(START + 1, stop)
            await cleanup.wait()
            self.assertEqual(delete.call_count, 1)
            cleanup.run_if_due(START + 900_000, stop)
            await cleanup.wait()
            self.assertEqual(delete.call_count, 3)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM queue_snapshots").fetchone()[0], 0
        )
        self.assertIsNotNone(self.users.find_symbol("1", "123"))

    async def test_cleanup_failure_retries_only_at_next_interval(self):
        cleanup = SnapshotCleanup(self.snapshots, 0, 1000, self.logger)
        stop = asyncio.Event()
        with patch.object(
            self.snapshots, "delete_expired", side_effect=RuntimeError("secret")
        ) as delete:
            with patch.object(self.logger, "error") as log:
                cleanup.run_if_due(START, stop)
                await cleanup.wait()
            self.assertEqual(log.call_args.args, ("Snapshot cleanup failed",))
            cleanup.run_if_due(START + 999, stop)
            self.assertEqual(delete.call_count, 1)
        cleanup.run_if_due(START + 1000, stop)
        await cleanup.wait()
        self.assertTrue(cleanup.task.done())

    async def test_engine_traces_match_typescript(self):
        scenarios = [
            [
                create(),
                update(0),
                update(60, 750),
                update(70, 780),
                update(80, 800),
                update(361, 800),
                update(421, 600),
            ],
            [
                create(GONE, cooldown=0),
                update(0),
                update(10, side="none"),
                update(20, side="none"),
                update(30, 1),
                update(40, side="none"),
            ],
            [
                create(),
                update(0),
                update(50, session="B"),
                update(60, 500, session="B"),
                update(110, 500, session="B"),
            ],
            [
                create(),
                update(0),
                update(60, 750),
                dict(op="restart", now=70),
                update(70, 750, source=60),
                update(80, 790),
                update(361, 800),
            ],
            [create(), update(0), update(30, 100), update(120, 50)],
            [
                create(),
                update(0),
                update(60, 750),
                update(60, 750),
                update(70, 500, source=50),
                update(120, 750, source=60),
                update(120, 1, persist=False),
                update(120, 1, source=121, persist=False),
            ],
            [
                create(),
                update(0),
                update(60, 750),
                dict(op="edit", now=65, id="rule", definition=GONE),
                update(70, side="none"),
                update(80, 1000),
                update(90, side="none"),
            ],
        ]
        for side in ("BUY", "SELL"):
            for window in (1, 5):
                definition = dict(
                    type=f"{side}_QUEUE_DROP_PERCENT",
                    config=dict(
                        thresholdPercent=3,
                        windowMinutes=window,
                        minimumBaselineVolume=0,
                    ),
                )
                scenarios.append(
                    [
                        create(definition, cooldown=0),
                        update(0, side=side.lower()),
                        update(window * 60, 970, side=side.lower()),
                        update(window * 60 + 10, 999, side=side.lower()),
                        update(window * 60 + 20, side=side.lower()),
                        update(window * 60 + 30, side="none"),
                    ]
                )
        for operator in ("AND", "OR"):
            group = {**GROUP, "operator": operator}
            scenarios.append(
                [
                    create(group, channel=True),
                    update(0),
                    update(60, 750),
                    update(70, side="sell"),
                    update(80, 790),
                    update(90, 800),
                    update(100, 600),
                    update(110, 474),
                    update(120, 1000),
                    update(180, 600),
                ]
            )
        expected = reference("engine-reference", scenarios)
        for index, actions in enumerate(scenarios):
            with self.subTest(scenario=index):
                self.db.execute("DELETE FROM alert_rules")
                self.db.execute("DELETE FROM queue_snapshots")
                self.clock = START
                self.restart()
                trace = []
                for action in actions:
                    self.clock = START + action["now"] * 1000
                    if action["op"] == "create":
                        owner = action.get("owner", "1")
                        id = (
                            await self.channels.create(
                                owner, ITEM, action["definition"]
                            )
                            if action.get("channel")
                            else self.alerts.create(
                                owner,
                                "123",
                                action["definition"],
                                action.get("cooldown", 300),
                            )
                        )
                        self.db.execute(
                            "UPDATE alert_rules SET id=? WHERE id=?", (action["id"], id)
                        )
                    elif action["op"] == "edit":
                        self.alerts.update(
                            "1",
                            action["id"],
                            action["definition"],
                            True,
                            action.get("cooldown", 0),
                        )
                    elif action["op"] == "restart":
                        self.restart()
                    else:
                        value = snapshot(
                            action.get("source", action["now"]),
                            action["volume"],
                            action.get("side", "buy"),
                        )
                        session = action.get("session", "A")
                        inserted = (
                            self.snapshots.save(value, self.clock, session)
                            if action.get("persist", True)
                            else False
                        )
                        self.engine.on_snapshot(value, session)
                        states = self.states()
                        for state in states:
                            # Compare the legacy state machine to its unchanged capture.
                            state.pop("quality_data")
                            state.pop("strategy_data")
                            state.pop("evaluation_detail")
                            if state["recovery_data"]:
                                state["recovery_data"] = json.loads(
                                    state["recovery_data"]
                                )
                        events = rows(
                            self.db,
                            "SELECT rule_id, occurrence_key, rule_revision, payload, message, created_at FROM alert_events ORDER BY created_at, rule_id",
                        )
                        for event in events:
                            event["payload"] = json.loads(event["payload"])
                            # Explanation metadata is additive; keep the frozen capture.
                            event["payload"].pop("definition", None)
                        trace.append(
                            dict(
                                inserted=inserted,
                                states=states,
                                events=events,
                                outbox=rows(
                                    self.db,
                                    "SELECT e.rule_id, o.status, o.attempts, o.next_attempt_at FROM notification_outbox o JOIN alert_events e ON e.id=o.event_id ORDER BY e.created_at, e.rule_id",
                                ),
                            )
                        )
                self.assertEqual(trace, expected[index])
