"""Explicit network-disabled image check; mounted only by smoke_image.py."""

import asyncio
import io
import json
import os
import shutil
import signal
import sqlite3
from pathlib import Path

import httpx
from telethon import functions, utils
from telethon_fake import API_HASH, API_ID, CHANNEL, FakeClient

import trade_alert.__main__ as entry
import trade_alert.mtproto as mtproto
import trade_alert.runtime as runtime
from trade_alert.alerts import AlertService
from trade_alert.backup import backup_database, restore_database
from trade_alert.channels import ChannelService
from trade_alert.config import parse_env
from trade_alert.database import open_database, rows
from trade_alert.engine import AlertEngine
from trade_alert.evaluators import QueueSnapshot
from trade_alert.health import health_report
from trade_alert.logger import create_logger
from trade_alert.maintenance import main as maintenance
from trade_alert.snapshots import SnapshotRepository
from trade_alert.users import UserService, now_ms


async def main():
    assert os.getuid() == 1000
    assert shutil.which("node") is None
    assert Path("/app/trade_alert").is_dir() and Path("/app/migrations").is_dir()
    assert not any(
        Path("/app", name).exists() for name in (".env", ".venv", "tests", "python")
    )
    path = Path("/app/data/smoke.db")
    token = "123456:" + "x" * 35
    source = dict(
        NODE_ENV="production",
        STOCK_ALERT_CONTAINER="true",
        DATABASE_URL=str(path),
        TELEGRAM_BOT_TOKEN=token,
        TELEGRAM_API_ID=str(API_ID),
        TELEGRAM_API_HASH=API_HASH,
        TELEGRAM_ALLOWED_USER_IDS="1,2",
        TELEGRAM_ADMIN_USER_IDS="1",
        TELEGRAM_CHANNEL_ID=CHANNEL,
        MARKET_SESSIONS_JSON="[]",
    )
    for override in (
        dict(DATABASE_URL="/tmp/outside.db"),
        dict(DATABASE_URL=":memory:"),
        dict(MARKET_MONITOR_FORCE_ACTIVE="true"),
        dict(DEBUG="*"),
    ):
        try:
            parse_env({**source, **override})
        except ValueError:
            pass
        else:
            raise AssertionError("Unsafe configuration accepted")
    link = Path("/app/data/escape")
    link.symlink_to("/tmp", target_is_directory=True)
    try:
        parse_env({**source, "DATABASE_URL": str(link / "outside.db")})
    except ValueError:
        pass
    else:
        raise AssertionError("Symlink confinement bypass")
    link.unlink()
    clock = now_ms()
    item = dict(insCode="123", symbol="TEST", name="Synthetic")
    drop = dict(
        type="BUY_QUEUE_DROP_PERCENT",
        config=dict(thresholdPercent=25, windowMinutes=1, minimumBaselineVolume=0),
    )

    async def verified(channel):
        return dict(channelId=channel, title="Synthetic", canPost=True)

    with open_database(path) as db:
        users = UserService(db, ["1", "2"], lambda: clock)
        users.register("1", "1")
        users.add_symbol("1", item)
        AlertService(db, users, now=lambda: clock).create("1", "123", drop)
        channels = ChannelService(
            db, users, ["1"], CHANNEL, verified, now=lambda: clock
        )
        await channels.create(
            "1",
            item,
            dict(
                type="GROUP",
                operator="AND",
                conditions=[
                    drop,
                    dict(type="BUY_QUEUE_VOLUME_ABOVE", config=dict(minimumVolume=500)),
                ],
            ),
        )
        repository = SnapshotRepository(db)
        engine = AlertEngine(
            db, repository, users, channels=channels, now=lambda: clock
        )
        # Deliberately synthetic accepted snapshots; never evidence about real books.
        for seconds, volume in ((0, 1000), (60, 700)):
            clock += seconds * 1000
            value = QueueSnapshot("123", "TEST", clock, "buy", volume, 110, 1)
            if repository.save(value, clock, "synthetic"):
                engine.on_snapshot(value, "synthetic")
        assert len(rows(db, "SELECT * FROM notification_outbox")) == 2
    sent, polled = [], asyncio.Event()

    class SmokeClient(FakeClient):
        async def __call__(self, request):
            result = await super().__call__(request)
            if isinstance(request, functions.updates.GetStateRequest):
                polled.set()
            return result

        async def send_message(self, peer, message, **kwargs):
            assert kwargs["parse_mode"] is None
            sent.append(str(utils.get_peer_id(peer)))
            return await super().send_message(peer, message, **kwargs)

    telegram = SmokeClient()

    async def market(request):
        raise AssertionError("Closed session must not contact the market")

    async def run(source_ignored, stop):
        await runtime.run_application(
            source,
            stop,
            telegram_client=telegram,
            market_transport=httpx.MockTransport(market),
            now=lambda: clock,
            logger=create_logger("silent", token, io.StringIO()),
        )

    entry.run_application = run
    runtime.HEARTBEAT_INTERVAL = 0.02
    mtproto.UPDATE_WAIT_SECONDS = 0.02
    task = asyncio.create_task(entry.main())
    try:
        await asyncio.wait_for(polled.wait(), 3)
        for _ in range(200):
            if len(sent) == 2:
                break
            await asyncio.sleep(0.02)
        assert sorted(sent) == [CHANNEL, "1"], sent
        report = health_report(now=clock)
        assert report["healthy"] and report["market"] == "MARKET_CLOSED", report
        try:
            maintenance(["migrate", str(path)])
        except RuntimeError:
            pass
        else:
            raise AssertionError("Maintenance bypassed a live instance lock")
        backup = backup_database(path, Path("/backups"), clock)
        assert not Path(str(backup) + "-wal").exists()
        with sqlite3.connect(backup) as db:
            assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, 3)
    assert not health_report(now=clock)["healthy"]
    assert not telegram.connected and not telegram.handlers
    restored = restore_database(backup, Path("/app/data/restored.db"))
    with open_database(restored) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert {r["status"] for r in rows(db, "SELECT * FROM notification_outbox")} == {
            "DELIVERED"
        }
    assert not Path("/app/python/tests").exists()
    assert not Path("/app/python/.venv").exists()
    print(
        json.dumps(
            dict(
                ok=True,
                uid=os.getuid(),
                fake_targets=sorted(sent),
                health=report,
                signal="SIGTERM",
                portable_backup=True,
            )
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
