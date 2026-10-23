"""Leased, at-least-once delivery; destinations are resolved from current services."""

import asyncio
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .bot_text import PRIVATE_TYPES, history_text, tehran_time
from .channels import ChannelService
from .composite import parse_evidence
from .database import rows, transaction
from .definitions import Condition, parse_definition, parse_tree
from .evaluators import PeakConfig, ThresholdConfig
from .quality import SEVERITIES, stored_policy
from .telegram import TelegramError
from .users import UserService, now_ms
from .validation import choice, integer, loads, number, obj, text

SEND_TIMEOUT = 10
type Send = Callable[[str, str], Awaitable[object]]


@dataclass(frozen=True, slots=True)
class DeliveryOptions:
    max_retries: int = 5
    retry_base_ms: int = 2000
    max_age_ms: int = 900_000
    market_max_age_ms: int = 30_000

    def __post_init__(self) -> None:
        integer(self.max_retries, 0, 10)
        integer(self.retry_base_ms, 1, 300_000)
        integer(self.max_age_ms, 1)
        integer(self.market_max_age_ms, 1, 300_000)


def parse_payload(raw: str | None) -> dict[str, Any]:
    """Validate stored JSON before rendering. Legacy message text is never sent."""
    p = obj(loads(raw))
    text(p["symbol"], 80, trim=False, minimum=0)
    integer(p["timestamp"])
    if "definition" in p:
        definition = parse_tree(p["definition"])
        if p["type"] != "COMPOSITE" and (
            not isinstance(definition, Condition) or definition.type != p["type"]
        ):
            raise ValueError("Mismatched event definition")
    if "severity" in p:
        choice(p["severity"], SEVERITIES)
    if p["type"] == "COMPOSITE":
        if p["operator"] is not None:
            choice(p["operator"], ("AND", "OR"))
        if not parse_evidence(json.dumps(p["matches"])):
            raise ValueError("Composite evidence is required")
    else:
        choice(p["type"], PRIVATE_TYPES)
        integer(p["previousVolume"])
        integer(p["currentVolume"])
        number(p["actualPercent"], 0, 100)
        if p["windowMinutes"] is not None:
            number(p["windowMinutes"], -float("inf"))
        if p["type"].endswith(("VOLUME_BELOW", "VOLUME_ABOVE", "PEAK_DROP_PERCENT")):
            definition = parse_tree(p["definition"])
            if (
                not isinstance(definition, Condition)
                or definition.type != p["type"]
                or not isinstance(definition.config, (PeakConfig, ThresholdConfig))
            ):
                raise ValueError("Invalid strategy evidence")
    return p


def format_notification(payload: dict[str, Any], now: int, max_age_ms: int) -> str:
    message = history_text(
        dict(
            payload=json.dumps(payload),
            severity=payload.get("severity", "normal"),
            ins_code=payload.get("insCode"),
        )
    )
    message += "\nزمان اعلان: " + tehran_time(now)
    if now - payload["timestamp"] > max_age_ms:
        message += "\nارسال با تأخیر؛ این هشدار مربوط به زمان دادهٔ بالا است."
    return message


class NotificationDispatcher:
    def __init__(
        self,
        db: sqlite3.Connection,
        users: UserService,
        send: Send,
        options: DeliveryOptions = DeliveryOptions(),
        *,
        channels: ChannelService | None = None,
        now: Callable[[], int] = now_ms,
        logger: logging.Logger | None = None,
    ) -> None:
        self.db, self.users, self.send, self.options = db, users, send, options
        self.channels, self.now = channels, now
        self.logger = logger or logging.getLogger(__name__)
        self.task: asyncio.Task[None] | None = None
        self.active: asyncio.Task[None] | None = None
        self.stopped = False

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.stopped = False
            self.task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self.stopped = True
        tasks = [t for t in (self.task, self.active) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _loop(self) -> None:
        while not self.stopped:
            try:
                await self.dispatch_once()
            except Exception:
                self.logger.error("Notification worker failed")
            await asyncio.sleep(1)

    async def dispatch_once(self) -> None:
        if self.stopped:
            return
        if self.active is None or self.active.done():
            self.active = asyncio.create_task(self._deliver())
        # Cancelling one waiter must not orphan or replace a running delivery.
        await asyncio.shield(self.active)

    def _state(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO application_state (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, self.now()),
        )

    def _claim(self) -> dict[str, Any] | None:
        now = self.now()
        with transaction(self.db):
            cooldown = rows(
                self.db,
                "SELECT value FROM application_state WHERE key='telegram_retry_until'",
            )
            if cooldown and int(cooldown[0]["value"]) > now:
                return None
            due = rows(
                self.db,
                "SELECT o.*, e.rule_id, e.payload, e.rule_revision, e.severity, r.channel_id, r.ins_code FROM notification_outbox o JOIN alert_events e ON e.id=o.event_id JOIN alert_rules r ON r.id=e.rule_id WHERE (o.status='PENDING' AND o.next_attempt_at<=?) OR (o.status='SENDING' AND (o.lease_until IS NULL OR o.lease_until<=?)) ORDER BY o.next_attempt_at, o.event_id LIMIT 1",
                (now, now),
            )
            if not due:
                return None
            item = due[0]
            item.update(attempts=item["attempts"] + 1, lease_token=str(uuid4()))
            self.db.execute(
                "UPDATE notification_outbox SET status='SENDING', attempts=?, lease_token=?, lease_until=? WHERE event_id=?",
                (item["attempts"], item["lease_token"], now + 60_000, item["event_id"]),
            )
            return item

    def _finish(
        self,
        item: dict[str, Any],
        status: str,
        error: str | None = None,
        next_at: int | None = None,
    ) -> None:
        with transaction(self.db):
            changed = self.db.execute(
                "UPDATE notification_outbox SET status=?, error_code=?, next_attempt_at=?, lease_token=NULL, lease_until=NULL WHERE event_id=? AND lease_token=?",
                (
                    status,
                    error,
                    self.now() if next_at is None else next_at,
                    item["event_id"],
                    item["lease_token"],
                ),
            ).rowcount
            if changed and status == "DELIVERED":
                self.db.execute(
                    "UPDATE alert_events SET sent_at=? WHERE id=?",
                    (self.now(), item["event_id"]),
                )
                if item["channel_id"] is not None:
                    self._state(
                        f"channel_last_success:{item['channel_id']}", str(self.now())
                    )

    def _target(self, item: dict[str, Any]) -> str | None:
        current = rows(
            self.db, "SELECT * FROM alert_rules WHERE id=?", (item["rule_id"],)
        )
        if not current:
            return None
        rule = current[0]
        try:
            stored_policy(rule["quality_config"])
        except ValueError, TypeError, KeyError, RecursionError, OverflowError:
            return None
        if (
            rule["owner_id"] is None
            or not rule["enabled"]
            or rule["revision"] != item["rule_revision"]
            or rule["channel_id"] != item["channel_id"]
            or parse_definition(
                rule["definition"], channel=rule["channel_id"] is not None
            )
            is None
        ):
            return None
        try:
            if rule["channel_id"] is None:
                return self.users.notification_target(rule["owner_id"], rule["id"])
            if self.channels and self.channels.eligible(rule):
                return str(rule["channel_id"])
        except PermissionError:
            pass
        return None

    def _expired(self, payload: dict[str, Any]) -> bool:
        age = self.now() - payload["timestamp"]
        return bool(age < 0 or age > self.options.max_age_ms)

    def _check_cancelled(self) -> None:
        task = asyncio.current_task()
        if self.stopped or (task is not None and task.cancelling()):
            raise asyncio.CancelledError

    def _failure(self, item: dict[str, Any], error: BaseException) -> None:
        code = error.code if isinstance(error, TelegramError) and error.code else None
        retry = error.retry_after if isinstance(error, TelegramError) else None
        safe_code = "TRANSPORT" if code is None else f"TELEGRAM_{code}"
        delay = max(
            0 if retry is None else retry * 1000,
            min(
                300_000, self.options.retry_base_ms * 2 ** min(item["attempts"] - 1, 20)
            ),
        )
        retry_at = self.now() + delay
        if item["channel_id"] is not None and self.channels is not None:
            self.channels.record_error(safe_code, self.now())
        if code == 429:
            self.db.execute(
                "INSERT INTO application_state (key, value, updated_at) VALUES ('telegram_retry_until', ?, ?) ON CONFLICT(key) DO UPDATE SET value=CAST(MAX(CAST(value AS INTEGER), CAST(excluded.value AS INTEGER)) AS TEXT), updated_at=excluded.updated_at",
                (str(retry_at), self.now()),
            )
        permanent = code is not None and 400 <= code < 500 and code != 429
        self._finish(
            item,
            "FAILED"
            if permanent or item["attempts"] > self.options.max_retries
            else "PENDING",
            safe_code,
            retry_at,
        )
        self.logger.warning(
            "Notification delivery failed",
            extra={"code": code, "attempts": item["attempts"]},
        )

    async def _deliver(self) -> None:
        self._check_cancelled()
        item = self._claim()
        if item is None:
            return
        try:
            payload = parse_payload(item["payload"])
            payload["severity"] = choice(item["severity"], SEVERITIES)
            payload["insCode"] = item["ins_code"]
            message = format_notification(
                payload, self.now(), self.options.market_max_age_ms
            )
            if len(message.encode("utf-16-le")) // 2 > 4096:
                raise ValueError("Notification exceeds Telegram message limit")
        except ValueError, TypeError, KeyError, OverflowError, OSError, RecursionError:
            self._finish(item, "FAILED", "INVALID_PAYLOAD")
            return
        if self._expired(payload):
            self._finish(item, "EXPIRED")
            return
        if not self._target(item):
            self._finish(item, "CANCELLED")
            return
        if item["attempts"] > self.options.max_retries + 1:
            self._finish(item, "FAILED", "RETRY_EXHAUSTED")
            return
        try:
            # One deadline encloses permission requests and the actual send.
            async with asyncio.timeout(SEND_TIMEOUT) as deadline:
                if item["channel_id"] is not None:
                    if self.channels is None or not await self.channels.refresh():
                        raise TelegramError(403)
                self._check_cancelled()
                if deadline.expired():
                    raise TimeoutError
                if self._expired(payload):
                    self._finish(item, "EXPIRED")
                    return
                target = self._target(item)
                if target is None:
                    self._finish(item, "CANCELLED")
                    return
                owned = rows(
                    self.db,
                    "SELECT event_id FROM notification_outbox WHERE event_id=? AND lease_token=? AND status='SENDING' AND lease_until>?",
                    (item["event_id"], item["lease_token"], self.now()),
                )
                if not owned:
                    return
                message = format_notification(
                    payload, self.now(), self.options.market_max_age_ms
                )
                if item["channel_id"] is not None and self.channels:
                    note = self.channels.symbol_note(item["ins_code"])
                    if note:
                        prefix = "\n\nیادداشت نماد:\n"
                        available = (
                            4096 - len((message + prefix).encode("utf-16-le")) // 2
                        )
                        if available > 1:
                            encoded = note.encode("utf-16-le")
                            if len(encoded) // 2 > available:
                                note = (
                                    encoded[: (available - 1) * 2].decode(
                                        "utf-16-le", errors="ignore"
                                    )
                                    + "…"
                                )
                            message += prefix + note
                if len(message.encode("utf-16-le")) // 2 > 4096:
                    self._finish(item, "FAILED", "MESSAGE_TOO_LONG")
                    return
                await self.send(target, message)
                self._check_cancelled()
                if deadline.expired():
                    raise TimeoutError
            self._finish(item, "DELIVERED")
        except asyncio.CancelledError:
            self._failure(item, TelegramError(0, "CANCELLED"))
            raise
        except Exception as error:
            if self._target(item) is None:
                self._finish(item, "CANCELLED")
            else:
                self._failure(item, error)
