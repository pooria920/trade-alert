"""Offline delivery and race checks; every request uses a fake or MockTransport."""

import asyncio
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fake_telegram import FakeTelegramAPI as TelegramAPI
from reference import reference
from test_persistence import START, snapshot
from test_services import DROP, GROUP, ITEM

from trade_alert.alerts import AlertService
from trade_alert.channels import ChannelService
from trade_alert.database import open_database, rows
from trade_alert.delivery import DeliveryOptions, NotificationDispatcher, parse_payload
from trade_alert.engine import AlertEngine
from trade_alert.snapshots import SnapshotRepository
from trade_alert.telegram import TelegramError
from trade_alert.users import UserService


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_notes_follow_shared_alerts_at_send_time_and_never_private_alerts(
        self,
    ):
        await self.trigger(channel=True)
        self.channels.set_symbol_note("1", ITEM, "یادداشت تازهٔ نماد")
        await self.worker().dispatch_once()
        self.assertEqual(self.outbox()["status"], "DELIVERED")
        self.assertIn("یادداشت تازهٔ نماد", self.calls[-1][1])
        self.db.execute(
            "UPDATE notification_outbox SET status='PENDING', next_attempt_at=0"
        )
        self.channels.set_symbol_note("1", ITEM, "😀" * 500)
        with patch("trade_alert.delivery.format_notification", return_value="x" * 4000):
            await self.worker().dispatch_once()
        self.assertEqual(self.outbox()["status"], "DELIVERED")
        self.assertTrue(self.calls[-1][1].startswith("x" * 4000))
        self.assertTrue(self.calls[-1][1].endswith("…"))
        self.assertLessEqual(len(self.calls[-1][1].encode("utf-16-le")) // 2, 4096)
        self.db.execute("DELETE FROM alert_rules")
        self.db.execute("DELETE FROM queue_snapshots")
        self.clock = START
        await self.trigger()
        await self.worker().dispatch_once()
        self.assertNotIn("یادداشت نماد:", self.calls[-1][1])

    async def test_basic_and_supergroup_delivery_rechecks_real_adapter_permissions(
        self,
    ):
        from telethon import utils
        from telethon_fake import (
            API_HASH,
            API_ID,
            CHANNEL,
            TOKEN,
            FakeClient,
            basic_group,
            channel,
        )

        from trade_alert.mtproto import TelegramAPI as MTProtoAPI

        for destination, chat in (
            ("-123", basic_group()),
            (CHANNEL, channel(broadcast=False, megagroup=True)),
        ):
            self.db.execute("DELETE FROM alert_rules")
            self.db.execute("DELETE FROM queue_snapshots")
            self.db.execute("DELETE FROM application_state")
            self.clock = START
            client = FakeClient()
            client.chat, client.can_post = chat, None
            api = MTProtoAPI(
                TOKEN, API_ID, API_HASH, ["1", "2"], destination, client=client
            )
            self.addAsyncCleanup(api.close)
            await api.initialize()
            self.channels = ChannelService(
                self.db,
                self.users,
                self.admins,
                destination,
                api.verify_channel,
                now=lambda: self.clock,
            )
            await self.trigger(channel=True)
            self.users.set_notifications("1", False)
            self.users.remove_symbol("1", "123")

            async def send(target, message):
                await api.call("sendMessage", dict(chat_id=target, text=message))

            worker = self.worker(send)
            await worker.dispatch_once()
            self.assertEqual(self.outbox()["status"], "DELIVERED")
            sent = [value for kind, value in client.calls if kind == "send"]
            self.assertEqual(str(utils.get_peer_id(sent[0][0])), destination)
            self.db.execute(
                "UPDATE notification_outbox SET status='PENDING', next_attempt_at=0"
            )
            if destination == "-123":
                chat.admin_rights = None
            else:
                client.admin = False
            await worker.dispatch_once()
            self.assertEqual(self.outbox()["status"], "FAILED")
            self.assertEqual(len([1 for kind, _ in client.calls if kind == "send"]), 1)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "delivery.db"
        self.context = open_database(self.path)
        self.db = self.context.__enter__()
        self.addCleanup(lambda: self.context.__exit__(None, None, None))
        self.clock = START
        self.users = UserService(self.db, ["1", "2"], lambda: self.clock)
        self.users.register("1", "1")
        self.users.add_symbol("1", ITEM)
        self.alerts = AlertService(self.db, self.users, now=lambda: self.clock)
        self.admins = ["1", "2"]

        async def verify(channel):
            return dict(channelId=channel, title="Test", canPost=True)

        self.channels = ChannelService(
            self.db, self.users, self.admins, "-100123", verify, now=lambda: self.clock
        )
        self.calls = []

        async def send(chat, message):
            self.calls.append((chat, message))

        self.send = send
        self.logger = logging.getLogger("offline-delivery-test")
        self.logger.disabled = True
        self.workers = []
        self.addAsyncCleanup(self.stop_workers)

    async def stop_workers(self):
        for worker in self.workers:
            await worker.stop()

    def worker(self, send=None, **kwargs):
        worker = NotificationDispatcher(
            self.db,
            self.users,
            send or self.send,
            channels=self.channels,
            now=lambda: self.clock,
            logger=self.logger,
            **kwargs,
        )
        self.workers.append(worker)
        return worker

    async def trigger(self, channel=False):
        rule = (
            await self.channels.create("1", ITEM, GROUP)
            if channel
            else self.alerts.create("1", "123", DROP)
        )
        repository = SnapshotRepository(self.db)
        engine = AlertEngine(
            self.db,
            repository,
            self.users,
            channels=self.channels,
            now=lambda: self.clock,
        )
        for seconds, volume in ((0, 1000), (60, 700)):
            self.clock = START + seconds * 1000
            value = snapshot(seconds, volume)
            if repository.save(value, self.clock, "A"):
                engine.on_snapshot(value, "A")
        self.assertEqual(len(rows(self.db, "SELECT * FROM notification_outbox")), 1)
        return rule

    def outbox(self):
        return rows(self.db, "SELECT * FROM notification_outbox")[0]

    async def test_delivers_persian_plain_text_once_and_resolves_observed_target(self):
        await self.trigger()
        worker = self.worker()
        await worker.dispatch_once()
        await worker.dispatch_once()
        self.assertEqual(len(self.calls), 1)
        target, message = self.calls[0]
        self.assertEqual(target, "1")
        for part in ("TEST", "۳۰٪", "تهران", "میلادی"):
            self.assertIn(part, message)
        self.assertEqual(self.outbox()["status"], "DELIVERED")
        self.assertIsNone(self.outbox()["lease_token"])
        self.assertEqual(
            rows(self.db, "SELECT sent_at FROM alert_events")[0]["sent_at"], self.clock
        )

    async def test_retry_after_persists_across_database_and_worker_restart(self):
        await self.trigger()

        async def limited(*args):
            self.clock += 10_000  # Delay is measured from the failure response.
            raise TelegramError(429, retry_after=20)

        await self.worker(limited).dispatch_once()
        self.assertEqual(self.outbox()["next_attempt_at"], START + 90_000)
        self.context.__exit__(None, None, None)
        self.context = open_database(self.path)
        self.db = self.context.__enter__()
        self.users.db = self.channels.db = self.db
        self.clock = START + 89_999
        await self.worker().dispatch_once()
        self.assertFalse(self.calls)
        self.clock += 1
        await self.worker().dispatch_once()
        self.assertEqual(self.outbox()["status"], "DELIVERED")
        self.assertEqual(self.outbox()["attempts"], 2)

    async def test_exponential_retry_budget_and_permanent_errors(self):
        await self.trigger()

        async def unavailable(*args):
            raise TelegramError(503)

        worker = self.worker(unavailable, options=DeliveryOptions(max_retries=2))
        for seconds, next_seconds, status in (
            (60, 62, "PENDING"),
            (62, 66, "PENDING"),
            (66, 74, "FAILED"),
        ):
            self.clock = START + seconds * 1000
            await worker.dispatch_once()
            self.assertEqual(
                self.outbox()["next_attempt_at"], START + next_seconds * 1000
            )
            self.assertEqual(self.outbox()["status"], status)
        self.assertEqual(self.outbox()["attempts"], 3)
        for code in (400, 401, 403):
            with self.subTest(code=code):
                self.db.execute(
                    "UPDATE notification_outbox SET status='PENDING', attempts=0, next_attempt_at=0"
                )

                async def permanent(*args):
                    raise TelegramError(code)

                await self.worker(permanent).dispatch_once()
                self.assertEqual(self.outbox()["status"], "FAILED")
                self.assertEqual(self.outbox()["error_code"], f"TELEGRAM_{code}")

    async def test_retry_exhaustion_after_abandoned_claim_does_not_send(self):
        await self.trigger()
        self.db.execute(
            "UPDATE notification_outbox SET status='SENDING', attempts=6, lease_until=NULL"
        )
        await self.worker().dispatch_once()
        self.assertEqual(self.outbox()["error_code"], "RETRY_EXHAUSTED")
        self.assertFalse(self.calls)

    async def test_lease_exclusion_and_recovery_across_connections(self):
        await self.trigger()
        started, release = asyncio.Event(), asyncio.Event()

        async def blocking(*args):
            started.set()
            await release.wait()
            await self.send(*args)

        first = asyncio.create_task(self.worker(blocking).dispatch_once())
        await started.wait()
        import sqlite3

        other_db = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(other_db.close)
        other = self.worker()
        other.db = other_db
        await other.dispatch_once()
        self.assertEqual(self.outbox()["attempts"], 1)
        release.set()
        await first
        self.assertEqual(len(self.calls), 1)
        self.db.execute(
            "UPDATE notification_outbox SET status='SENDING', attempts=1, lease_token='dead', lease_until=?",
            (self.clock + 60_000,),
        )
        await other.dispatch_once()
        self.assertEqual(len(self.calls), 1)
        self.clock += 60_000
        await other.dispatch_once()
        self.assertEqual(
            len(self.calls), 2
        )  # At-least-once may repeat accepted messages.

    async def test_stale_lease_owner_cannot_overwrite_new_owner(self):
        await self.trigger()
        item = self.worker()._claim()
        self.clock += 60_000
        replacement = self.worker()._claim()
        self.worker()._finish(item, "DELIVERED")
        self.assertEqual(self.outbox()["lease_token"], replacement["lease_token"])
        self.assertIsNone(
            rows(self.db, "SELECT sent_at FROM alert_events")[0]["sent_at"]
        )

    async def test_private_guards_and_malformed_definitions(self):
        rule = await self.trigger()
        for reason in (
            "muted",
            "disabled",
            "revision",
            "authorization",
            "watchlist",
            "chat",
            "null_owner",
            "definition",
        ):
            with self.subTest(reason=reason):
                self.db.execute("SAVEPOINT guard_case")
                self.db.execute(
                    "UPDATE notification_outbox SET status='PENDING', attempts=0, next_attempt_at=0"
                )
                original = self.users.allowed
                if reason == "muted":
                    self.users.set_notifications("1", False)
                elif reason == "authorization":
                    self.users.allowed = frozenset(["2"])
                elif reason == "watchlist":
                    self.db.execute("DELETE FROM watchlists")
                elif reason == "chat":
                    self.db.execute("UPDATE users SET private_chat_id='2'")
                else:
                    column, value = {
                        "disabled": ("enabled", 0),
                        "revision": ("revision", 99),
                        "null_owner": ("owner_id", None),
                        "definition": ("definition", "{}"),
                    }[reason]
                    self.db.execute(
                        f"UPDATE alert_rules SET {column}=? WHERE id=?", (value, rule)
                    )
                # Release savepoint because claims require their own immediate transaction.
                self.db.execute("RELEASE guard_case")
                await self.worker().dispatch_once()
                self.assertEqual(self.outbox()["status"], "CANCELLED")
                self.assertFalse(self.calls)
                self.users.allowed = original
                self.db.execute(
                    "UPDATE alert_rules SET enabled=1, revision=1, owner_id='1', definition=?",
                    (json.dumps(DROP),),
                )
                self.users.register("1", "1")
                self.users.set_notifications("1", True)
                self.users.add_symbol("1", ITEM)

    async def test_future_expired_delayed_and_invalid_payloads(self):
        await self.trigger()
        raw = rows(self.db, "SELECT payload FROM alert_events")[0]["payload"]
        for offset, expected in (
            (-1, "EXPIRED"),
            (900_001, "EXPIRED"),
            (30_001, "DELIVERED"),
        ):
            self.clock = START + 60_000 + offset
            self.db.execute(
                "UPDATE notification_outbox SET status='PENDING', next_attempt_at=0"
            )
            await self.worker().dispatch_once()
            self.assertEqual(self.outbox()["status"], expected)
        self.assertIn("با تأخیر", self.calls[-1][1])
        for payload in (
            None,
            "{}",
            "NaN",
            raw.replace('"previousVolume": 1000', '"previousVolume": true'),
        ):
            self.db.execute(
                "UPDATE notification_outbox SET status='PENDING', next_attempt_at=0"
            )
            self.db.execute("UPDATE alert_events SET payload=?", (payload,))
            await self.worker().dispatch_once()
            self.assertEqual(self.outbox()["error_code"], "INVALID_PAYLOAD")

    async def test_stop_cancels_and_joins_delivery_and_retry_wait(self):
        await self.trigger()
        started, ended = asyncio.Event(), asyncio.Event()

        async def blocking(*args):
            started.set()
            try:
                await asyncio.Future()
            finally:
                ended.set()

        worker = self.worker(blocking)
        worker.start()
        await started.wait()
        await asyncio.wait_for(worker.stop(), 1)
        self.assertTrue(ended.is_set())
        self.assertTrue(worker.task.done())
        self.assertTrue(worker.active.done())
        self.assertEqual(self.outbox()["status"], "PENDING")
        await worker.dispatch_once()
        self.assertEqual(self.outbox()["attempts"], 1)

    async def test_one_waiter_cancellation_cannot_duplicate_active_delivery(self):
        await self.trigger()
        started, release = asyncio.Event(), asyncio.Event()

        async def blocking(*args):
            started.set()
            await release.wait()
            await self.send(*args)

        worker = self.worker(blocking)
        first = asyncio.create_task(worker.dispatch_once())
        await started.wait()
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        second = asyncio.create_task(worker.dispatch_once())
        release.set()
        await second
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.outbox()["attempts"], 1)

    async def test_permission_failure_retry_and_channel_metadata(self):
        rule = await self.trigger(channel=True)

        async def denied(channel):
            return dict(channelId=channel, title="Test", canPost=False)

        self.channels.verify = denied
        await self.worker().dispatch_once()
        self.assertFalse(self.calls)
        self.assertEqual(self.outbox()["status"], "FAILED")
        self.assertEqual(
            self.channels.status("1")["last_error"]["code"], "TELEGRAM_403"
        )
        self.channels.remove("1", rule)
        self.assertEqual(
            self.channels.status("1")["last_error"]["code"], "TELEGRAM_403"
        )

    async def test_channel_is_independent_of_private_preferences_and_watchlist(self):
        await self.trigger(channel=True)
        self.users.set_notifications("1", False)
        self.users.remove_symbol("1", "123")
        await self.worker().dispatch_once()
        self.assertEqual(self.calls[0][0], "-100123")
        self.assertIn("حداقل حجم صف خرید", self.calls[0][1])
        self.assertEqual(self.channels.status("1")["last_success"], self.clock)

    async def test_channel_rechecks_after_async_permissions(self):
        rule = await self.trigger(channel=True)
        for reason, expected in (
            ("revision", "CANCELLED"),
            ("admin", "CANCELLED"),
            ("channel", "CANCELLED"),
            ("expired", "EXPIRED"),
            ("delete", None),
        ):
            self.db.execute(
                "UPDATE notification_outbox SET status='PENDING', next_attempt_at=0"
            )

            async def change(channel):
                await asyncio.sleep(0)
                if reason == "revision":
                    self.db.execute("UPDATE alert_rules SET revision=2")
                elif reason == "admin":
                    self.admins.clear()
                elif reason == "channel":
                    self.channels.channel_id = "-100456"
                elif reason == "expired":
                    self.clock += 900_001
                else:
                    self.channels.remove("1", rule)
                return dict(channelId=channel, title="Test", canPost=True)

            self.channels.verify = change
            await self.worker().dispatch_once()
            if expected is None:
                self.assertFalse(rows(self.db, "SELECT * FROM notification_outbox"))
            else:
                self.assertEqual(self.outbox()["status"], expected)
            self.assertFalse(self.calls)
            self.channels.channel_id = "-100123"
            self.admins[:] = ["1", "2"]
            self.clock = START + 60_000
            self.db.execute("UPDATE alert_rules SET revision=1")

    async def test_temporary_permission_error_retries_without_sending(self):
        await self.trigger(channel=True)
        original = self.channels.verify

        async def unavailable(channel):
            raise TelegramError(503)

        self.channels.verify = unavailable
        await self.worker().dispatch_once()
        self.assertEqual(self.outbox()["status"], "PENDING")
        self.assertFalse(self.calls)
        self.clock += 2000
        self.channels.verify = original
        await self.worker().dispatch_once()
        self.assertEqual(self.outbox()["status"], "DELIVERED")

    async def test_send_and_permissions_share_deadline_including_ignored_cancel(self):
        await self.trigger(channel=True)
        original = self.channels.verify
        for stage in ("verify", "send", "ignored_verify", "ignored_send"):
            with self.subTest(stage=stage):
                self.channels.verify = original
                self.db.execute(
                    "UPDATE notification_outbox SET status='PENDING', attempts=0, next_attempt_at=0"
                )

                async def blocked(*args):
                    try:
                        await asyncio.Future()
                    except asyncio.CancelledError:
                        if not stage.startswith("ignored"):
                            raise
                        return dict(channelId="-100123", title="Test", canPost=True)

                if stage.endswith("verify"):
                    self.channels.verify = blocked
                with patch("trade_alert.delivery.SEND_TIMEOUT", 0.01):
                    await asyncio.wait_for(
                        self.worker(
                            blocked if stage.endswith("send") else None
                        ).dispatch_once(),
                        1,
                    )
                self.assertFalse(self.calls)
                self.assertEqual(self.outbox()["status"], "PENDING")

    async def test_real_transport_is_fake_and_message_has_no_parse_mode(self):
        await self.trigger()
        requests = []

        async def handler(request):
            payload = json.loads(request.content)
            requests.append(payload)
            return httpx.Response(200, json=dict(ok=True, result=dict(message_id=1)))

        api = TelegramAPI("123:" + "x" * 32, transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(api.close)

        async def send(target, message):
            return await api.call("sendMessage", dict(chat_id=target, text=message))

        await self.worker(send).dispatch_once()
        self.assertEqual(requests[0]["chat_id"], "1")
        self.assertNotIn("parse_mode", requests[0])

    def test_payload_boundary_rejects_invalid_composite_evidence(self):
        for value in (
            dict(type="COMPOSITE", operator="AND", matches=[]),
            dict(type="COMPOSITE", operator="X", matches=[{}]),
        ):
            with self.assertRaises(ValueError):
                parse_payload(json.dumps(dict(symbol="TEST", timestamp=START, **value)))

    async def test_offline_typescript_delivery_parity(self):
        cases = [
            dict(channel=channel, outcome=outcome)
            for channel in (False, True)
            for outcome in (
                "success",
                "429",
                "503",
                "403",
                "muted",
                "revision",
                "expired",
                "denied",
            )
        ]
        expected = reference("delivery-reference", cases)
        actual = []
        for case in cases:
            self.db.execute("DELETE FROM alert_rules")
            self.db.execute("DELETE FROM queue_snapshots")
            self.db.execute("DELETE FROM application_state")
            self.clock = START
            self.users.set_notifications("1", True)
            self.calls.clear()

            async def verify(channel):
                return dict(channelId=channel, title="Test", canPost=True)

            self.channels.verify = verify
            rule = await self.trigger(case["channel"])
            if case["outcome"] == "muted":
                self.users.set_notifications("1", False)
            elif case["outcome"] == "revision":
                self.db.execute("UPDATE alert_rules SET revision=2 WHERE id=?", (rule,))
            elif case["outcome"] == "expired":
                self.clock += 900_001
            elif case["outcome"] == "denied":

                async def denied(channel):
                    return dict(channelId=channel, title="Test", canPost=False)

                self.channels.verify = denied

            async def send(target, message):
                self.calls.append((target, message))
                if case["outcome"].isdigit():
                    raise TelegramError(
                        int(case["outcome"]),
                        retry_after=10 if case["outcome"] == "429" else None,
                    )

            await self.worker(send).dispatch_once()
            out = self.outbox()
            actual.append(
                dict(
                    status=out["status"],
                    attempts=out["attempts"],
                    error=out["error_code"],
                    next=out["next_attempt_at"],
                    targets=[c[0] for c in self.calls],
                )
            )
        self.assertEqual(actual, expected)
