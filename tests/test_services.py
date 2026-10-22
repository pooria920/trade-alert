"""Ownership, transactions and asynchronous authorization on disposable SQLite."""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from reference import reference

from trade_alert.alerts import AlertService
from trade_alert.channels import ChannelService
from trade_alert.database import open_database, rows
from trade_alert.users import UserService

ITEM = dict(insCode="123", symbol="TEST", name="Test instrument")
DROP = dict(
    type="BUY_QUEUE_DROP_PERCENT",
    config=dict(thresholdPercent=25, windowMinutes=1, minimumBaselineVolume=0),
)
GROUP = dict(
    type="GROUP",
    operator="AND",
    conditions=[
        DROP,
        dict(type="BUY_QUEUE_VOLUME_ABOVE", config=dict(minimumVolume=500)),
    ],
)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_notes_authorization_destination_scope_and_validation(self):
        self.channels.set_symbol_note("1", ITEM, "یادداشت مشترک")
        self.assertEqual(self.channels.symbol_note("123"), "یادداشت مشترک")
        with self.assertRaises(PermissionError):
            self.channels.set_symbol_note("3", ITEM, "unauthorized")
        with self.assertRaises(ValueError):
            self.channels.set_symbol_note("1", ITEM, "x" * 501)
        self.channels.channel_id = "-100999"
        self.assertEqual(self.channels.symbol_note("123"), "")

    async def test_channel_write_rollback_and_invalid_input(self):
        rule = await self.channels.create("1", ITEM, GROUP)
        self.state(rule)
        self.event(rule)
        self.db.execute(
            "CREATE TRIGGER reject_reset BEFORE DELETE ON alert_states BEGIN SELECT RAISE(ABORT, 'test failure'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            await self.channels.update("2", rule, DROP, True, 1)
        current = self.channels.get("1", rule)
        self.assertEqual(
            (current["owner_id"], current["revision"], current["definition"]),
            ("1", 1, GROUP),
        )
        self.assertEqual(
            self.db.execute("SELECT status FROM notification_outbox").fetchone()[0],
            "PENDING",
        )
        self.db.execute(
            "CREATE TRIGGER reject_rule BEFORE INSERT ON alert_rules BEGIN SELECT RAISE(ABORT, 'test failure'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            await self.channels.create("1", {**ITEM, "insCode": "456"}, GROUP)
        self.assertEqual(
            self.db.execute("SELECT ins_code FROM symbols").fetchall(), [("123",)]
        )
        checks = self.checks
        with self.assertRaises(ValueError):
            await self.channels.create("1", ITEM, {**GROUP, "conditions": []})
        for invalid in (None, 1, "false"):
            with self.assertRaises(ValueError):
                await self.channels.set_enabled("1", rule, invalid)
        self.assertEqual(checks, self.checks)

    async def test_swallowed_transport_cancellation_cannot_authorize_write(self):
        entered = asyncio.Event()

        async def ignoring_cancel(channel):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return dict(channelId=channel, title="Test", canPost=True)

        self.channels.verify = ignoring_cancel
        task = asyncio.create_task(self.channels.create("1", ITEM, GROUP))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(self.channels.status("1")["checked_at"])
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM symbols").fetchone()[0], 0
        )

    async def test_private_history_is_bounded_and_scoped(self):
        mine, theirs = self.private(), self.private("2")
        channel = await self.channels.create("1", ITEM, GROUP)
        for rule, event in (
            (mine, "a"),
            (mine, "b"),
            (mine, "c"),
            (theirs, "z"),
            (channel, "zz"),
        ):
            self.event(rule, event)
        alerts = AlertService(self.db, self.users, history_limit=2)
        self.assertEqual([e["id"] for e in alerts.history("1")], ["c", "b"])
        self.assertEqual(alerts.history("4"), [])
        with self.assertRaises(PermissionError):
            self.channels.status("3")

    async def test_service_scenario_matches_typescript(self):
        expected = reference(
            "service-reference", dict(instrument=ITEM, definition=DROP, group=GROUP)
        )
        a = self.private("1")
        self.private("2")
        c = await self.channels.create("1", ITEM, GROUP)

        def instruments(items):
            return [
                dict(insCode=i.ins_code, symbol=i.symbol, name=i.name) for i in items
            ]

        actual = dict(
            shared=instruments(self.users.monitored_symbols()),
            privateHidden=self.alerts.get("2", a) is None,
            channelHidden=self.channels.get("1", a) is None,
            privateChannelHidden=self.alerts.get("1", c) is None,
            targets=[
                self.users.notification_target("1", a),
                self.users.notification_target("2", a),
                self.users.notification_target("1", c),
            ],
        )
        self.users.set_notifications("1", False)
        self.users.register("1", "1")
        actual["muted"] = self.users.notification_target("1", a)
        actual["updated"] = self.alerts.update("1", a, DROP, False, 42)
        edited = self.alerts.get("1", a)
        actual["privateRevision"] = [
            edited["revision"],
            bool(edited["enabled"]),
            edited["cooldown_seconds"],
        ]
        actual["adopted"] = await self.channels.update("2", c, DROP, True, 1)
        adopted = self.channels.get("1", c)
        actual["channelRevision"] = [
            adopted["owner_id"],
            adopted["revision"],
            self.channels.eligible(adopted),
        ]
        actual["staleEdit"] = await self.channels.update("1", c, GROUP, True, 1)
        actual["removed"] = self.users.remove_symbol("1", "123")
        actual["remaining"] = [
            len(self.alerts.list("1")),
            len(self.alerts.list("2")),
            len(self.channels.list("1")),
        ]
        actual["channelSymbols"] = instruments(self.channels.monitored_symbols())
        actual["stored"] = rows(
            self.db,
            "SELECT owner_id, channel_id, revision, enabled, type, cooldown_seconds FROM alert_rules ORDER BY owner_id, channel_id",
        )
        self.assertEqual(actual, expected)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        context = open_database(Path(directory.name) / "services.db")
        self.db = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.clock = 1000
        self.users = UserService(self.db, ["1", "2", "3", "4"], lambda: self.clock)
        self.alerts = AlertService(self.db, self.users, now=lambda: self.clock)
        for actor in ("1", "2", "3"):
            self.users.register(actor, actor)
        self.admins = ["1", "2"]
        self.checks = 0

        async def verify(channel):
            self.checks += 1
            return dict(channelId=channel, title="Test channel", canPost=True)

        self.verify = verify
        self.channels = ChannelService(
            self.db, self.users, self.admins, "-100123", verify, now=lambda: self.clock
        )

    def private(self, actor="1"):
        self.users.add_symbol(actor, ITEM)
        return self.alerts.create(actor, ITEM["insCode"], DROP)

    def event(self, rule, event="event", status="PENDING"):
        self.db.execute(
            "INSERT INTO alert_events (id, rule_id, message, created_at) VALUES (?, ?, 'test', ?)",
            (event, rule, self.clock),
        )
        self.db.execute(
            "INSERT INTO notification_outbox (event_id, status, next_attempt_at) VALUES (?, ?, ?)",
            (event, status, self.clock),
        )

    def state(self, rule):
        self.db.execute(
            "INSERT INTO alert_states (rule_id, revision, session_id, status, last_evaluated_at, recovery_data) VALUES (?, 1, 'session', 'TRIGGERED', ?, '[]')",
            (rule, self.clock),
        )

    async def test_private_ownership_targets_and_removal_preserve_channel(self):
        first, second = self.private(), self.private("2")
        channel = await self.channels.create("1", ITEM, GROUP)
        self.event(first, "first")
        self.event(second, "second")
        self.event(channel, "channel")
        self.db.execute(
            "INSERT INTO queue_snapshots (ins_code, timestamp, queue_type, volume, order_count) VALUES ('123', 1, 'none', 0, 0)"
        )
        self.assertIsNone(self.alerts.get("1", second))
        self.assertFalse(self.alerts.remove("1", second))
        self.assertFalse(self.alerts.remove("1", channel))
        self.assertIsNone(self.users.notification_target("1", channel))
        self.assertIsNone(self.users.notification_target("1", second))
        self.assertEqual(self.users.notification_target("2", second), "2")
        self.users.set_notifications("2", False)
        self.users.register("2", "2")
        self.assertIsNone(self.users.notification_target("2", second))
        self.assertEqual([e["id"] for e in self.alerts.history("1")], ["first"])
        self.assertTrue(self.users.remove_symbol("1", "123"))
        self.assertEqual(self.alerts.list("1"), [])
        self.assertIsNotNone(self.channels.get("2", channel))
        self.assertEqual(len(self.users.list_symbols("2")), 1)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM symbols").fetchone()[0], 1
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM queue_snapshots").fetchone()[0], 1
        )
        self.assertEqual(
            self.db.execute("SELECT id FROM alert_events ORDER BY id").fetchall(),
            [("channel",), ("second",)],
        )

    async def test_unauthorized_and_unobserved_users_never_register_implicitly(self):
        rule = self.private()
        self.assertIsNone(self.users.preferences("4"))
        for action in (
            lambda: self.users.register("9", "9"),
            lambda: self.users.register("1", "2"),
            lambda: self.users.add_symbol("4", ITEM),
            lambda: self.users.list_symbols("9"),
            lambda: self.alerts.create("9", "123", DROP),
            lambda: self.alerts.list("9"),
            lambda: self.alerts.history("9"),
            lambda: self.users.remove_rule("9", rule),
            lambda: self.users.notification_target("9", rule),
            lambda: self.alerts.update("9", rule, DROP),
            lambda: self.users.set_notifications("9", True),
        ):
            with self.assertRaises(PermissionError):
                action()
        self.admins.append("4")
        for actor in ("3", "4", "9"):
            with self.assertRaises(PermissionError):
                await self.channels.create(actor, ITEM, GROUP)
            for action in (self.channels.list, self.channels.status):
                with self.assertRaises(PermissionError):
                    action(actor)
        self.assertEqual(self.checks, 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM users").fetchone()[0], 3)

    async def test_edit_resets_state_and_cancels_only_pending_events_atomically(self):
        rule = self.private()
        self.state(rule)
        self.event(rule, "pending")
        self.event(rule, "sent", "SENT")
        self.assertTrue(self.alerts.update("1", rule, DROP, False, 42))
        row = self.alerts.get("1", rule)
        self.assertEqual(
            (row["revision"], row["enabled"], row["cooldown_seconds"]), (2, 0, 42)
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM alert_states").fetchone()[0], 0
        )
        self.assertEqual(
            self.db.execute(
                "SELECT status FROM notification_outbox ORDER BY event_id"
            ).fetchall(),
            [("CANCELLED",), ("SENT",)],
        )
        self.assertIsNone(self.users.notification_target("1", rule))
        self.state(rule)
        self.db.execute(
            "CREATE TRIGGER reject_reset BEFORE DELETE ON alert_states BEGIN SELECT RAISE(ABORT, 'test failure'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.alerts.update("1", rule, DROP, True)
        self.assertEqual(self.alerts.get("1", rule)["revision"], 2)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM alert_states").fetchone()[0], 1
        )

    async def test_channel_collective_management_adoption_and_scope(self):
        private = self.private()
        first = await self.channels.create("1", ITEM, GROUP)
        second = await self.channels.create("2", ITEM, DROP)
        self.users.set_notifications("1", False)
        self.users.remove_symbol("1", "123")
        self.assertEqual(len(self.channels.monitored_symbols()), 1)
        self.assertIsNone(self.channels.get("1", private))
        self.assertFalse(self.channels.remove("1", private))
        self.assertEqual(self.alerts.list("1"), [])
        self.assertTrue(await self.channels.set_enabled("2", first, False))
        self.assertEqual(self.channels.get("1", first)["owner_id"], "2")
        self.assertFalse(await self.channels.update("1", first, DROP, True, 1))
        self.assertTrue(await self.channels.update("1", first, DROP, True, 2))
        self.assertEqual(self.channels.get("2", first)["revision"], 3)
        self.assertTrue(self.channels.remove("2", first))
        self.assertEqual([r["id"] for r in self.channels.list("1")], [second])
        self.assertEqual(
            self.checks, 3
        )  # create twice and re-enable; disable needs no posting permission

    async def test_revocation_reconfiguration_legacy_and_malformed_quarantine(self):
        rule = await self.channels.create("1", ITEM, GROUP)
        self.admins.remove("1")
        self.assertFalse(self.channels.eligible(self.channels.get("2", rule)))
        self.assertEqual(self.channels.monitored_symbols(), [])
        with self.assertRaises(PermissionError):
            self.channels.list("1")
        self.assertTrue(await self.channels.update("2", rule, GROUP, revision=1))
        self.assertTrue(self.channels.eligible(self.channels.get("2", rule)))
        other = ChannelService(
            self.db, self.users, ["2"], "-100456", self.verify, now=lambda: self.clock
        )
        self.assertEqual(other.list("2"), [])
        self.assertEqual(other.monitored_symbols(), [])
        self.assertFalse(other.remove("2", rule))
        for legacy, owner, definition in (
            ("legacy", None, json.dumps(GROUP)),
            ("malformed", "2", "{}"),
        ):
            self.db.execute(
                "INSERT INTO alert_rules (id, owner_id, channel_id, ins_code, type, definition, created_at) VALUES (?, ?, '-100123', '123', 'BUY_QUEUE_DISAPPEARED', ?, 1)",
                (legacy, owner, definition),
            )
        self.assertIsNone(self.channels.get("2", "legacy"))
        self.assertFalse(self.channels.remove("2", "legacy"))
        self.assertIsNone(self.channels.get("2", "malformed"))
        self.channels.remove("2", rule)
        self.assertEqual(self.channels.monitored_symbols(), [])
        self.assertEqual(
            self.db.execute(
                "SELECT owner_id FROM alert_rules WHERE id='legacy'"
            ).fetchone(),
            (None,),
        )

    async def test_verification_on_each_save_and_permission_failure_metadata(self):
        rule = await self.channels.create("1", ITEM, GROUP)
        self.assertTrue(await self.channels.update("2", rule, DROP))
        self.assertEqual(self.checks, 2)

        async def denied(channel):
            return dict(channelId=channel, title="Denied", canPost=False)

        self.channels.verify = denied
        with self.assertRaises(PermissionError):
            await self.channels.create("1", {**ITEM, "insCode": "456"}, DROP)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM symbols").fetchone()[0], 1
        )
        self.assertEqual(self.channels.status("1")["permission_error"], "CANNOT_POST")
        self.assertTrue(await self.channels.set_enabled("1", rule, False))

        async def wrong(channel):
            return dict(channelId="-100999", title="Wrong", canPost=True)

        self.channels.verify = wrong
        with self.assertRaises(ValueError):
            await self.channels.set_enabled("1", rule, True)
        self.assertEqual(self.channels.status("1")["permission_error"], "UNAVAILABLE")
        self.channels.record_error("FORBIDDEN", self.clock)
        self.channels.remove("2", rule)
        self.assertEqual(
            self.channels.status("1")["last_error"],
            dict(code="FORBIDDEN", timestamp=self.clock),
        )
        self.assertEqual(self.channels.status("1")["total"], 0)

    async def test_permission_await_rechecks_actor_and_revision(self):
        rule = await self.channels.create("1", ITEM, GROUP)
        self.state(rule)
        self.event(rule)

        async def edited(channel):
            self.db.execute(
                "UPDATE alert_rules SET revision=revision+1 WHERE id=?", (rule,)
            )
            return dict(channelId=channel, title="Test", canPost=True)

        self.channels.verify = edited
        self.assertFalse(await self.channels.update("1", rule, DROP, revision=1))
        self.assertEqual(
            self.db.execute("SELECT status FROM notification_outbox").fetchone()[0],
            "PENDING",
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM alert_states").fetchone()[0], 1
        )

        async def revoked(channel):
            self.admins.remove("1")
            return dict(channelId=channel, title="Test", canPost=True)

        self.channels.verify = revoked
        with self.assertRaises(PermissionError):
            await self.channels.create("1", {**ITEM, "insCode": "456"}, GROUP)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM alert_rules").fetchone()[0], 1
        )

    async def test_cancelled_permission_check_does_not_write_or_swallow_cancellation(
        self,
    ):
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def blocked(channel):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.channels.verify = blocked
        task = asyncio.create_task(self.channels.create("1", ITEM, GROUP))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM alert_rules").fetchone()[0], 0
        )
        self.assertIsNone(self.channels.status("1")["checked_at"])

    async def test_status_permission_freshness_and_corrupt_metadata(self):
        await self.channels.refresh()
        self.assertTrue(self.channels.status("1")["can_post"])
        self.clock += 60_001
        self.assertFalse(self.channels.status("1")["can_post"])
        self.clock = 999
        self.assertFalse(self.channels.status("1")["can_post"])
        self.db.execute(
            "INSERT INTO application_state VALUES ('channel_delivery_error:-100123', 'invalid', 1)"
        )
        self.assertIsNone(self.channels.status("1")["last_error"])

    async def test_private_target_revocation_tampering_and_definition_validation(self):
        rule = self.private()
        revoked = UserService(self.db, ["2"])
        self.assertEqual(revoked.monitored_symbols(), [])
        with self.assertRaises(PermissionError):
            revoked.notification_target("1", rule)
        self.db.execute(
            "UPDATE users SET private_chat_id='2' WHERE telegram_user_id='1'"
        )
        self.assertIsNone(self.users.notification_target("1", rule))
        self.users.register("1", "1")
        self.db.execute("UPDATE alert_rules SET definition='{}' WHERE id=?", (rule,))
        self.assertEqual(self.alerts.list("1"), [])
        self.assertIsNone(self.users.notification_target("1", rule))
        before = self.db.execute("SELECT count(*) FROM alert_rules").fetchone()[0]
        for invalid in ({}, GROUP):
            with self.assertRaises((ValueError, KeyError)):
                self.alerts.create("1", "123", invalid)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM alert_rules").fetchone()[0], before
        )
