"""Application wiring with one database owner and joined, cancellable workers."""

import asyncio
import logging
from collections.abc import Callable, Mapping
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import httpx

from .alerts import AlertService
from .bot import TelegramBot
from .channels import ChannelService
from .cleanup import SnapshotCleanup
from .config import mtproxy, parse_env
from .database import open_database
from .delivery import DeliveryOptions, NotificationDispatcher
from .engine import AlertEngine
from .health import HEALTH_FILE, database_probe, runtime_health, write_heartbeat
from .logger import create_logger
from .market_service import MarketService
from .monitor import MarketMonitor, MonitorOptions
from .schedule import MarketSchedule, parse_sessions
from .snapshots import SnapshotRepository
from .telegram import TelegramAPI
from .tsetmc import TsetmcClient
from .users import UserService, now_ms

HEARTBEAT_INTERVAL = 15


async def run_application(
    source: Mapping[str, str],
    stop: asyncio.Event,
    *,
    telegram_client: Any = None,
    market_transport: httpx.AsyncBaseTransport | None = None,
    health_path: Path = HEALTH_FILE,
    now: Callable[[], int] = now_ms,
    logger: logging.Logger | None = None,
) -> None:
    env = parse_env(source)
    proxy = mtproxy(env["TELEGRAM_MTPROXY_URL"])
    logger = logger or create_logger(
        env["LOG_LEVEL"],
        env["TELEGRAM_BOT_TOKEN"],
        api_hash=env["TELEGRAM_API_HASH"],
        proxy_secret=proxy[2] if proxy else "",
    )
    # open_database acquires the separate OS lock before migrations or app access.
    with open_database(env["DATABASE_URL"]) as db:
        async with AsyncExitStack() as resources:
            api = TelegramAPI(
                env["TELEGRAM_BOT_TOKEN"],
                env["TELEGRAM_API_ID"],
                env["TELEGRAM_API_HASH"],
                env["TELEGRAM_ALLOWED_USER_IDS"],
                env["TELEGRAM_CHANNEL_ID"],
                client=telegram_client,
                admin_users=env["TELEGRAM_ADMIN_USER_IDS"],
                proxy_url=env["TELEGRAM_MTPROXY_URL"],
            )
            resources.push_async_callback(api.close)
            client = TsetmcClient(
                env["TSETMC_BASE_URL"],
                env["TSETMC_REQUEST_TIMEOUT_MS"],
                env["TSETMC_MAX_RETRIES"],
                transport=market_transport,
                now=now,
            )
            resources.push_async_callback(client.close)
            market = MarketService(
                client.snapshot, client.search, env["MARKET_DATA_MAX_AGE_MS"]
            )
            resources.push_async_callback(market.close)
            users = UserService(db, env["TELEGRAM_ALLOWED_USER_IDS"], now)
            channels = ChannelService(
                db,
                users,
                env["TELEGRAM_ADMIN_USER_IDS"],
                env["TELEGRAM_CHANNEL_ID"],
                api.verify_channel,
                env["ALERT_DEFAULT_COOLDOWN_SECONDS"],
                now,
            )
            repository = SnapshotRepository(
                db, env["SNAPSHOT_HISTORY_TOLERANCE_SECONDS"] * 1000
            )
            cleanup = SnapshotCleanup(
                repository,
                env["SNAPSHOT_RETENTION_HOURS"] * 3_600_000,
                env["SNAPSHOT_CLEANUP_INTERVAL_MS"],
                logger,
            )
            schedule = MarketSchedule(
                parse_sessions(env["MARKET_SESSIONS_JSON"]),
                env["MARKET_CLOSED_DATES"],
                env["MARKET_MONITOR_FORCE_ACTIVE"],
                env["MARKET_SESSION_MARKET"],
            )
            alerts = AlertService(
                db,
                users,
                env["ALERT_DEFAULT_COOLDOWN_SECONDS"],
                env["ALERT_HISTORY_LIMIT"],
                now,
            )
            engine = AlertEngine(
                db,
                repository,
                users,
                env["MARKET_DATA_MAX_AGE_MS"],
                now,
                channels,
                logger,
            )
            monitor = MarketMonitor(
                lambda: [*users.monitored_symbols(), *channels.monitored_symbols()],
                market,
                repository,
                schedule,
                cleanup,
                MonitorOptions(
                    env["MARKET_POLL_INTERVAL_MS"],
                    env["MARKET_MAX_CONCURRENT_REQUESTS"],
                    env["MARKET_REQUEST_TIMEOUT_MS"],
                    env["MARKET_DATA_MAX_AGE_MS"],
                ),
                engine.on_snapshot,
                logger=logger,
                now=now,
            )

            async def send(target: str, message: str) -> object:
                return await api.call("sendMessage", dict(chat_id=target, text=message))

            dispatcher = NotificationDispatcher(
                db,
                users,
                send,
                DeliveryOptions(
                    env["NOTIFICATION_MAX_RETRIES"],
                    env["NOTIFICATION_RETRY_BASE_MS"],
                    env["NOTIFICATION_MAX_AGE_MINUTES"] * 60_000,
                    env["MARKET_DATA_MAX_AGE_MS"],
                ),
                channels=channels,
                now=now,
                logger=logger,
            )

            async def stop_workers() -> None:
                results = await asyncio.gather(
                    monitor.stop(), dispatcher.stop(), return_exceptions=True
                )
                if any(isinstance(result, BaseException) for result in results):
                    raise RuntimeError("Worker shutdown failed")

            resources.push_async_callback(stop_workers)
            bot = TelegramBot(
                api,
                users,
                market,
                alerts,
                monitor.get_status,
                channels,
                now=now,
                logger=logger,
            )
            resources.push_async_callback(bot.stop)
            initialized = False

            def publish() -> None:
                stamp = now()
                write_heartbeat(
                    runtime_health(
                        monitor.get_status(),
                        initialized,
                        database_probe(db, stamp),
                        bot.last_telegram_success,
                        bot.telegram_errors,
                        env["MARKET_REQUEST_TIMEOUT_MS"],
                        stamp,
                    ),
                    health_path,
                )

            def begin_workers() -> None:
                nonlocal initialized
                if stop.is_set():
                    return
                initialized = True
                monitor.start()
                dispatcher.start()
                publish()
                logger.info("Bot initialized; collection and delivery enabled")

            async def heartbeat() -> None:
                while True:
                    publish()
                    await asyncio.sleep(HEARTBEAT_INTERVAL)

            health_path.unlink(missing_ok=True)
            tasks = [
                asyncio.create_task(bot.run(begin_workers)),
                asyncio.create_task(heartbeat()),
                asyncio.create_task(stop.wait()),
            ]
            try:
                if not stop.is_set():
                    completed, _ = await asyncio.wait(
                        tasks, return_when=asyncio.FIRST_COMPLETED
                    )
                    if tasks[2] not in completed:
                        for task in completed:
                            await task
                        raise RuntimeError("Application worker stopped unexpectedly")
            finally:
                # Cancellation reaches startup, update consumption, RPCs and delays.
                try:
                    async with asyncio.timeout(15):
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        await resources.aclose()
                finally:
                    health_path.unlink(missing_ok=True)
            logger.info("Bot stopped; workers joined")
