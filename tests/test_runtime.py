"""Full runtime with mock HTTP and disposable databases; never reads real settings."""

import asyncio
import io
import json
import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fake_telegram import FakeTelegramAPI
from test_bot import TOKEN
from test_market_client import NOW
from test_services import DROP, ITEM

from trade_alert.__main__ import main
from trade_alert.alerts import AlertService
from trade_alert.database import instance_lock, open_database, rows
from trade_alert.delivery import NotificationDispatcher
from trade_alert.health import (
    database_probe,
    health_report,
    process_alive,
    runtime_health,
    write_heartbeat,
)
from trade_alert.logger import create_logger
from trade_alert.monitor import MarketMonitor
from trade_alert.runtime import run_application
from trade_alert.users import UserService


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "runtime.db"
        self.health = Path(directory.name) / "health.json"
        self.env = dict(
            TELEGRAM_BOT_TOKEN=TOKEN,
            TELEGRAM_API_ID="12345",
            TELEGRAM_API_HASH="a" * 32,
            TELEGRAM_ALLOWED_USER_IDS="1,2",
            DATABASE_URL=str(self.path),
            NODE_ENV="test",
        )
        self.stop = asyncio.Event()
        self.polling = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.requests = []
        self.hook = None
        self.poll_count = 0
        self.health_values = []
        self.monitors = []
        self.dispatchers = []
        self.output = io.StringIO()
        self.logger = create_logger("debug", TOKEN, self.output)

        async def handler(request):
            method = request.url.path.rsplit("/", 1)[1]
            self.requests.append((method, json.loads(request.content)))
            if self.hook:
                response = await self.hook(method)
                if response is not None:
                    return response
            if method == "getMe":
                result = dict(id=123456, is_bot=True, username="test_bot")
            elif method == "getUpdates":
                self.poll_count += 1
                if self.poll_count > 1:
                    self.polling.set()
                    try:
                        await asyncio.Future()
                    finally:
                        self.cancelled.set()
                result = []
            else:
                result = True
            return httpx.Response(200, json=dict(ok=True, result=result))

        self.transport = httpx.MockTransport(handler)

        async def reject_market(request):
            raise AssertionError("Closed/empty runtime must not request market data")

        self.market_transport = httpx.MockTransport(reject_market)

    async def asyncSetUp(self):
        self.tasks_before = asyncio.all_tasks()

    async def run_app(self):
        def monitor(*args, **kwargs):
            value = MarketMonitor(*args, **kwargs)
            self.monitors.append(value)
            return value

        def dispatcher(*args, **kwargs):
            value = NotificationDispatcher(*args, **kwargs)
            self.dispatchers.append(value)
            return value

        def publish(value, path):
            self.health_values.append(value)
            write_heartbeat(value, path)

        with (
            patch("trade_alert.runtime.MarketMonitor", side_effect=monitor),
            patch("trade_alert.runtime.NotificationDispatcher", side_effect=dispatcher),
            patch("trade_alert.runtime.write_heartbeat", side_effect=publish),
            patch("trade_alert.runtime.HEARTBEAT_INTERVAL", 0.01),
            patch(
                "trade_alert.runtime.TelegramAPI",
                side_effect=lambda *args, **kwargs: FakeTelegramAPI(
                    args[0], transport=self.transport
                ),
            ),
        ):
            await run_application(
                self.env,
                self.stop,
                market_transport=self.market_transport,
                health_path=self.health,
                now=lambda: NOW,
                logger=self.logger,
            )

    async def launch(self):
        task = asyncio.create_task(self.run_app())
        self.addAsyncCleanup(self.finish, task)
        return task

    async def finish(self, task):
        self.stop.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)

    async def assert_closed(self):
        await asyncio.sleep(0)
        self.assertFalse(self.health.exists())
        for monitor in self.monitors:
            self.assertFalse(monitor.status.is_running)
            self.assertTrue(monitor.task is None or monitor.task.done())
            self.assertFalse(monitor.workers)
            self.assertTrue(monitor.cleanup.task is None or monitor.cleanup.task.done())
        for worker in self.dispatchers:
            self.assertTrue(worker.task is None or worker.task.done())
            self.assertTrue(worker.active is None or worker.active.done())
        with open_database(self.path) as db:
            self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
        leaked = [
            t
            for t in asyncio.all_tasks() - self.tasks_before
            if not t.done() and t is not asyncio.current_task()
        ]
        self.assertEqual(leaked, [])

    async def test_closed_runtime_healthy_only_after_poll_success_and_stops_cleanly(
        self,
    ):
        task = await self.launch()
        await asyncio.wait_for(self.polling.wait(), 1)
        await asyncio.sleep(0.02)
        report = health_report(self.health, NOW)
        self.assertTrue(report["healthy"], report)
        self.assertEqual(report["market"], "MARKET_CLOSED")
        self.assertTrue(
            any(v["last_telegram_success"] is None for v in self.health_values)
        )
        self.assertTrue(
            all(
                v["monitor_progress_at"] is None
                for v in self.health_values
                if not v["initialized"]
            )
        )
        self.assertFalse(any(method == "sendMessage" for method, _ in self.requests))
        with self.assertRaises(RuntimeError):
            with instance_lock(self.path):
                pass
        self.stop.set()
        await asyncio.wait_for(task, 1)
        self.assertTrue(self.cancelled.is_set())
        await self.assert_closed()

    async def test_open_empty_watchlist_is_healthy_without_market_calls(self):
        self.env["MARKET_MONITOR_FORCE_ACTIVE"] = "true"
        task = await self.launch()
        await asyncio.wait_for(self.polling.wait(), 1)
        await asyncio.sleep(0.02)
        report = health_report(self.health, NOW)
        self.assertTrue(report["healthy"], report)
        self.assertEqual(report["market"], "NO_MONITORED_SYMBOLS")
        self.stop.set()
        await task
        await self.assert_closed()

    async def test_startup_failure_closes_database_clients_and_releases_lock(self):
        async def fail(method):
            if method == "getMe":
                return httpx.Response(
                    401, json=dict(ok=False, error_code=401, description=TOKEN)
                )

        self.hook = fail
        with self.assertRaisesRegex(Exception, "401"):
            await self.run_app()
        await self.assert_closed()
        self.assertNotIn(TOKEN, self.output.getvalue())

    async def test_stop_during_initialization_joins_request(self):
        entered = asyncio.Event()

        async def blocked(method):
            if method == "getMe":
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    self.cancelled.set()

        self.hook = blocked
        task = await self.launch()
        await asyncio.wait_for(entered.wait(), 1)
        self.stop.set()
        await asyncio.wait_for(task, 1)
        self.assertTrue(self.cancelled.is_set())
        self.assertFalse(self.monitors[0].status.is_running)
        self.assertIsNone(self.dispatchers[0].task)
        await self.assert_closed()

    async def test_fatal_poll_failure_stops_all_workers(self):
        async def conflict(method):
            if method == "getUpdates":
                return httpx.Response(409, json=dict(ok=False, error_code=409))

        self.hook = conflict
        with self.assertRaisesRegex(Exception, "409"):
            await self.run_app()
        await self.assert_closed()

    async def test_heartbeat_failure_stops_all_workers(self):
        def fail(*args):
            raise OSError("Synthetic filesystem failure")

        with patch("trade_alert.health.write_heartbeat", side_effect=fail):
            # run_app's recorder calls the imported function; patch it locally too.
            with patch(__name__ + ".write_heartbeat", side_effect=fail):
                with self.assertRaises(OSError):
                    await self.run_app()
        await self.assert_closed()

    async def test_external_task_cancellation_also_joins_and_releases_resources(self):
        task = await self.launch()
        await asyncio.wait_for(self.polling.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.assert_closed()

    async def test_stop_with_collection_and_delivery_in_flight(self):
        self.env["MARKET_MONITOR_FORCE_ACTIVE"] = "true"
        with open_database(self.path) as db:
            users = UserService(db, ["1"], lambda: NOW)
            users.register("1", "1")
            users.add_symbol("1", ITEM)
            rule = AlertService(db, users, now=lambda: NOW).create("1", "123", DROP)
            payload = dict(
                symbol="TEST",
                type="BUY_QUEUE_DROP_PERCENT",
                timestamp=NOW,
                previousVolume=1000,
                currentVolume=700,
                actualPercent=30,
                windowMinutes=1,
            )
            db.execute(
                "INSERT INTO alert_events (id, rule_id, occurrence_key, rule_revision, payload, message, created_at) VALUES ('event', ?, 'unique', 1, ?, '', ?)",
                (rule, json.dumps(payload), NOW),
            )
            db.execute(
                "INSERT INTO notification_outbox (event_id, next_attempt_at) VALUES ('event', ?)",
                (NOW,),
            )
        market_started, send_started = asyncio.Event(), asyncio.Event()
        market_ended, send_ended = asyncio.Event(), asyncio.Event()

        async def market(request):
            market_started.set()
            try:
                await asyncio.Future()
            finally:
                market_ended.set()

        async def send(method):
            if method == "sendMessage":
                send_started.set()
                try:
                    await asyncio.Future()
                finally:
                    send_ended.set()

        self.hook = send
        self.market_transport = httpx.MockTransport(market)
        task = await self.launch()
        await asyncio.wait_for(
            asyncio.gather(market_started.wait(), send_started.wait()), 1
        )
        self.stop.set()
        await asyncio.wait_for(task, 1)
        self.assertTrue(market_ended.is_set() and send_ended.is_set())
        await self.assert_closed()
        with open_database(self.path) as db:
            self.assertEqual(
                rows(db, "SELECT status FROM notification_outbox")[0]["status"],
                "PENDING",
            )

    async def test_preset_stop_makes_no_network_calls(self):
        self.stop.set()
        await self.run_app()
        self.assertFalse(self.requests)
        await self.assert_closed()

    async def test_cli_handles_both_signals_and_restores_handlers(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            old = signal.getsignal(signum)

            async def fake_application(source, stop):
                signal.raise_signal(signum)
                await asyncio.wait_for(stop.wait(), 1)

            with patch(
                "trade_alert.__main__.run_application", side_effect=fake_application
            ):
                await main()
            self.assertEqual(signal.getsignal(signum), old)


class HealthTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "health.json"
        self.status = dict(
            is_running=True,
            is_market_open=True,
            monitored_symbols=1,
            fresh_symbols=0,
            last_progress_at=NOW,
            poll_interval_ms=1000,
            symbols=[],
        )

    def health(self, **kwargs):
        args = dict(
            status=self.status,
            initialized=True,
            database_ok=True,
            last_success=NOW,
            telegram_errors=0,
            request_timeout_ms=5000,
            now=NOW,
        )
        args.update(kwargs)
        value = runtime_health(**args)
        write_heartbeat(value, self.path)
        return health_report(self.path, NOW)

    def test_current_process_is_alive_without_signalling_it(self):
        self.assertTrue(process_alive(os.getpid()))
        self.assertFalse(process_alive(999_999_999))

    def test_unverified_and_persistent_upstream_failure_are_degraded_but_live(self):
        self.assertTrue(self.health()["healthy"])
        self.assertEqual(self.health()["market"], "DATA_UNVERIFIED_OR_STALE")
        self.status["symbols"] = [
            dict(last_result="transport-error", consecutive_errors=5)
        ]
        value = self.health()
        self.assertTrue(value["healthy"])
        self.assertEqual(value["market"], "TSETMC_PERSISTENT_FAILURE")

    def test_heartbeat_cannot_refresh_collector_progress(self):
        self.status["last_progress_at"] = NOW - 36_001
        self.assertIn("MONITOR_STALLED", self.health()["reasons"])
        self.status["last_progress_at"] = NOW + 1
        self.assertIn("MONITOR_STALLED", self.health()["reasons"])

    def test_initialization_database_and_recent_polling_are_all_required(self):
        for overrides, reason in (
            (dict(initialized=False), "STARTING"),
            (dict(database_ok=False), "DATABASE_FAILURE"),
            (dict(last_success=None), "TELEGRAM_CONNECTION_FAILURE"),
            (dict(last_success=NOW - 120_001), "TELEGRAM_CONNECTION_FAILURE"),
            (dict(last_success=NOW + 1), "TELEGRAM_CONNECTION_FAILURE"),
        ):
            self.assertIn(reason, self.health(**overrides)["reasons"])
        self.status["is_running"] = False
        self.assertIn("MONITOR_STALLED", self.health()["reasons"])

    def test_dead_process_and_stale_or_malformed_heartbeat_fail(self):
        self.health()
        self.assertIn(
            "PROCESS_NOT_RUNNING",
            health_report(self.path, NOW, lambda pid: False)["reasons"],
        )
        self.assertIn(
            "STALE_HEARTBEAT", health_report(self.path, NOW + 90_001)["reasons"]
        )
        self.path.write_text("{}", encoding="utf-8")
        self.assertEqual(
            health_report(self.path, NOW)["reasons"], ["HEALTH_UNAVAILABLE"]
        )
        self.path.unlink()
        self.assertFalse(health_report(self.path, NOW)["healthy"])

    def test_database_probe_requires_write_and_heartbeat_replacement_is_atomic(self):
        with open_database(self.path.with_suffix(".db")) as db:
            self.assertTrue(database_probe(db, NOW))
            db.execute("PRAGMA query_only=ON")
            self.assertFalse(database_probe(db, NOW))
        self.health()
        before = self.path.read_bytes()
        with patch("pathlib.Path.replace", side_effect=OSError("blocked")):
            with self.assertRaises(OSError):
                self.health()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob("health.json.*")), [])

    def test_logger_omits_raw_errors_updates_env_and_credentials(self):
        output = io.StringIO()
        api_hash = "abcde12345abcde12345abcde12345abc"
        logger = create_logger("debug", TOKEN, output, api_hash=api_hash)
        try:
            raise ValueError("SECRET_RAW_ERROR")
        except ValueError:
            logger.exception(
                "Delivery failed %s %s",
                TOKEN,
                api_hash,
                extra=dict(
                    update={"secret": "UPDATE_SECRET"},
                    environ={"PASSWORD": "ENV_SECRET"},
                    code=503,
                    attempts=2,
                ),
            )
        content = output.getvalue()
        self.assertNotIn(TOKEN, content)
        self.assertNotIn(api_hash, content)
        for secret in ("SECRET_RAW_ERROR", "UPDATE_SECRET", "ENV_SECRET", "Traceback"):
            self.assertNotIn(secret, content)
        self.assertEqual(json.loads(content)["code"], 503)
        self.assertIn("[REDACTED]", content)
