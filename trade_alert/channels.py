"""Admin-scoped channel rules with injected, cancellable permission verification."""

import asyncio
import builtins
import json
import sqlite3
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

from .alerts import evaluation_state, reset_rule
from .config import channel_id
from .database import rows, transaction
from .definitions import definition_json, parse_definition, parse_tree
from .market import Instrument, parse_instrument
from .quality import parse_policy
from .users import UserService, now_ms
from .validation import integer, loads, number, obj, text

type Verification = Callable[[str], Awaitable[object]]


class ChannelService:
    def __init__(
        self,
        db: sqlite3.Connection,
        users: UserService,
        admins: Sequence[str],
        configured_channel: str,
        verify: Verification,
        cooldown: int = 300,
        now: Callable[[], int] = now_ms,
    ) -> None:
        if not set(admins).issubset(users.allowed):
            raise ValueError("Channel admins must be authorized users")
        self.db, self.users, self.admins = db, users, admins
        self.channel_id = channel_id(configured_channel) if configured_channel else ""
        self.verify, self.now = verify, now
        self.cooldown = integer(cooldown, 0, 86_400)
        if self.channel_id:
            self.db.execute(
                "INSERT INTO telegram_channels (channel_id) VALUES (?) ON CONFLICT DO NOTHING",
                (self.channel_id,),
            )

    def is_admin(self, actor: str) -> bool:
        return actor in self.admins and actor in self.users.allowed

    def authorize(self, actor: str) -> None:
        if not self.is_admin(actor) or not self.channel_id:
            raise PermissionError("Channel administration is not authorized")
        registered = self.users.preferences(actor)
        if registered is None or registered["private_chat_id"] != actor:
            raise PermissionError("Channel administration is not authorized")

    def eligible(self, rule: Mapping[str, Any]) -> bool:
        return bool(
            self.channel_id
            and rule["channel_id"] == self.channel_id
            and rule["owner_id"] is not None
            and self.is_admin(rule["owner_id"])
        )

    async def refresh(self) -> bool:
        """The transport verifier must check exact channel and bot posting rights.

        Task cancellation (including the delivery worker's enclosing deadline)
        propagates through the verifier. No transaction spans network awaits.
        """
        if not self.channel_id:
            return False
        try:
            async with asyncio.timeout(10) as deadline:
                result = obj(await self.verify(self.channel_id))
            if deadline.expired():
                raise TimeoutError("Channel verification deadline expired")
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise asyncio.CancelledError
            verified_id = channel_id(result["channelId"])
            title = text(result["title"], 255, trim=False)
            can_post = result["canPost"]
            if verified_id != self.channel_id or type(can_post) is not bool:
                raise ValueError("Invalid channel verification")
            self.db.execute(
                "UPDATE telegram_channels SET title=?, can_post=?, checked_at=?, permission_error=? WHERE channel_id=?",
                (
                    title,
                    can_post,
                    self.now(),
                    None if can_post else "CANNOT_POST",
                    self.channel_id,
                ),
            )
            return bool(can_post)
        except Exception as error:
            # Persist only safe adapter categories, never RPC text/credentials.
            kind = getattr(error, "kind", None)
            reason = "UNAVAILABLE"
            if kind in (
                "DESTINATION_INVALID",
                "DESTINATION_UNRESOLVED",
                "DESTINATION_INACCESSIBLE",
                "ADMIN_REQUIRED",
                "NETWORK",
                "INVALID_RESPONSE",
                "SESSION_CONFLICT",
            ):
                reason = kind
            elif getattr(error, "code", None) == 429:
                reason = "RATE_LIMITED"
            elif isinstance(error, (TimeoutError, OSError)):
                reason = "NETWORK"
            self.db.execute(
                "UPDATE telegram_channels SET can_post=0, checked_at=?, permission_error=? WHERE channel_id=?",
                (self.now(), reason, self.channel_id),
            )
            raise

    def _writable(self) -> bool:
        result = rows(
            self.db,
            "SELECT can_post, checked_at FROM telegram_channels WHERE channel_id=?",
            (self.channel_id,),
        )
        return bool(
            result
            and result[0]["can_post"]
            and result[0]["checked_at"] is not None
            and 0 <= self.now() - result[0]["checked_at"] <= 60_000
        )

    async def _verify_write(self, actor: str) -> None:
        if not await self.refresh():
            raise PermissionError("Channel posting permission must be verified")
        self.authorize(actor)
        if not self._writable():
            raise PermissionError("Channel posting permission must be verified")

    def list(self, actor: str) -> list[dict[str, Any]]:
        self.authorize(actor)
        result = []
        for rule in rows(
            self.db,
            "SELECT r.*, s.symbol FROM alert_rules r JOIN symbols s ON s.ins_code=r.ins_code WHERE r.channel_id=? AND r.owner_id IS NOT NULL ORDER BY r.created_at, r.id",
            (self.channel_id,),
        ):
            definition = parse_definition(rule["definition"], channel=True)
            if definition is not None:
                result.append({**rule, "definition": definition_json(definition)})
        # Internal service results include owner metadata; UI must not display it.
        return result

    def symbol_note(self, ins_code: str) -> str:
        """Internal delivery lookup, scoped to the currently configured destination."""
        result = rows(
            self.db,
            "SELECT note FROM channel_symbol_notes WHERE channel_id=? AND ins_code=?",
            (self.channel_id, ins_code),
        )
        return str(result[0]["note"]) if result else ""

    def set_symbol_note(self, actor: str, instrument: object, value: object) -> None:
        self.authorize(actor)
        item = parse_instrument(instrument)
        note = text(value, 500, minimum=0)
        with transaction(self.db):
            self.db.execute(
                "INSERT INTO symbols (ins_code, symbol, created_at) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                (item.ins_code, item.symbol, self.now()),
            )
            if note:
                self.db.execute(
                    "INSERT INTO channel_symbol_notes (channel_id, ins_code, note, updated_by, updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(channel_id, ins_code) DO UPDATE SET note=excluded.note, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                    (self.channel_id, item.ins_code, note, actor, self.now()),
                )
            else:
                self.db.execute(
                    "DELETE FROM channel_symbol_notes WHERE channel_id=? AND ins_code=?",
                    (self.channel_id, item.ins_code),
                )

    def get(self, actor: str, rule_id: str) -> dict[str, Any] | None:
        rule = next((rule for rule in self.list(actor) if rule["id"] == rule_id), None)
        if rule is not None:
            rule["evaluation"] = evaluation_state(self.db, rule)
            rule["eligible"] = self.eligible(rule)
        return rule

    async def create(
        self,
        actor: str,
        instrument: object,
        value: object,
        *,
        guard: Callable[[], bool] | None = None,
        quality: object = None,
        cooldown: int | None = None,
    ) -> str:
        self.authorize(actor)
        item = parse_instrument(instrument)
        definition = definition_json(parse_tree(value))
        settings = (
            None if quality is None else json.dumps(parse_policy(quality).to_json())
        )
        if cooldown is not None:
            integer(cooldown, 0, 86_400)
        await self._verify_write(actor)
        if guard is not None and not guard():
            raise PermissionError("Draft expired or changed")
        rule_id = str(uuid4())
        with transaction(self.db):
            self.db.execute(
                "INSERT INTO symbols (ins_code, symbol, created_at) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                (item.ins_code, item.symbol, self.now()),
            )
            self.db.execute(
                "INSERT INTO alert_rules (id, owner_id, channel_id, ins_code, type, definition, cooldown_seconds, quality_config, created_at) VALUES (?, ?, ?, ?, 'BUY_QUEUE_DISAPPEARED', ?, ?, ?, ?)",
                (
                    rule_id,
                    actor,
                    self.channel_id,
                    item.ins_code,
                    json.dumps(definition),
                    self.cooldown if cooldown is None else cooldown,
                    settings,
                    self.now(),
                ),
            )
        return rule_id

    async def update(
        self,
        actor: str,
        rule_id: str,
        value: object,
        enabled: bool | None = None,
        revision: int | None = None,
        *,
        guard: Callable[[], bool] | None = None,
        quality: object = None,
        cooldown: int | None = None,
    ) -> bool:
        self.authorize(actor)
        definition = definition_json(parse_tree(value))
        settings = (
            None if quality is None else json.dumps(parse_policy(quality).to_json())
        )
        if cooldown is not None:
            integer(cooldown, 0, 86_400)
        if enabled is not None and type(enabled) is not bool:
            raise ValueError("Expected enabled boolean")
        if revision is not None:
            integer(revision, 1)
        rule = self.get(actor, rule_id)
        if rule is None or (revision is not None and revision != rule["revision"]):
            return False
        expected_revision = rule["revision"]
        if enabled is not False:
            await self._verify_write(actor)
        self.authorize(actor)
        if guard is not None and not guard():
            raise PermissionError("Draft expired or changed")
        with transaction(self.db):
            changed = self.db.execute(
                "UPDATE alert_rules SET owner_id=?, definition=?, enabled=?, quality_config=?, cooldown_seconds=?, revision=revision+1, created_at=? WHERE id=? AND channel_id=? AND owner_id IS NOT NULL AND revision=?",
                (
                    actor,
                    json.dumps(definition),
                    rule["enabled"] if enabled is None else enabled,
                    rule["quality_config"] if settings is None else settings,
                    rule["cooldown_seconds"] if cooldown is None else cooldown,
                    self.now(),
                    rule_id,
                    self.channel_id,
                    expected_revision,
                ),
            ).rowcount
            if not changed:
                return False
            reset_rule(self.db, rule_id)
        return True

    async def set_enabled(self, actor: str, rule_id: str, enabled: bool) -> bool:
        self.authorize(actor)
        if type(enabled) is not bool:
            raise ValueError("Expected enabled boolean")
        rule = self.get(actor, rule_id)
        return rule is not None and await self.update(
            actor, rule_id, rule["definition"], enabled, rule["revision"]
        )

    def remove(self, actor: str, rule_id: str) -> bool:
        self.authorize(actor)
        return (
            self.db.execute(
                "DELETE FROM alert_rules WHERE id=? AND channel_id=? AND owner_id IS NOT NULL",
                (rule_id, self.channel_id),
            ).rowcount
            > 0
        )

    def monitored_symbols(self) -> builtins.list[Instrument]:
        if not self.channel_id:
            return []
        result = {}
        for rule in rows(
            self.db,
            "SELECT r.*, s.symbol FROM alert_rules r JOIN symbols s ON s.ins_code=r.ins_code WHERE r.channel_id=? AND r.enabled=1 AND r.owner_id IS NOT NULL",
            (self.channel_id,),
        ):
            if (
                self.eligible(rule)
                and parse_definition(rule["definition"], channel=True) is not None
            ):
                result[rule["ins_code"]] = Instrument(
                    rule["ins_code"], rule["symbol"], rule["symbol"]
                )
        return list(result.values())

    def history(self, actor: str, page: int = 0) -> builtins.list[dict[str, Any]]:
        self.authorize(actor)
        page = integer(page, 0, 1_000_000)
        events = rows(
            self.db,
            "SELECT e.*, r.ins_code FROM alert_events e JOIN alert_rules r ON r.id=e.rule_id WHERE r.channel_id=? AND r.owner_id IS NOT NULL ORDER BY e.created_at DESC, e.id DESC LIMIT 10 OFFSET ?",
            (self.channel_id, page * 10),
        )
        for event in events:
            event["outcomes"] = rows(
                self.db,
                "SELECT * FROM alert_outcomes WHERE event_id=? ORDER BY window_minutes",
                (event["id"],),
            )
        return events

    async def statistics(self, actor: str) -> dict[str, Any]:
        self.authorize(actor)
        cutoff = max(0, self.now() - 30 * 86_400_000)
        channel = self.channel_id
        path = self.db.execute("PRAGMA database_list").fetchone()[2]
        cancelled = threading.Event()

        def read() -> dict[str, Any]:
            # A separate readonly connection keeps aggregation off the monitor loop.
            try:
                with closing(
                    sqlite3.connect(
                        Path(path).as_uri() + "?mode=ro", uri=True, timeout=1
                    )
                ) as db:
                    db.set_progress_handler(lambda: int(cancelled.is_set()), 1000)
                    db.execute("BEGIN")
                    alerts = rows(
                        db,
                        "SELECT e.severity, count(*) AS count FROM alert_events e JOIN alert_rules r ON r.id=e.rule_id WHERE r.channel_id=? AND r.owner_id IS NOT NULL AND e.created_at>=? GROUP BY e.severity",
                        (channel, cutoff),
                    )
                    outcomes = rows(
                        db,
                        "SELECT window_minutes, status, count(*) AS count, count(price_change_percent) AS price_samples, avg(price_change_percent) AS price_average FROM alert_outcomes WHERE channel_id=? AND target_at>=? GROUP BY window_minutes, status",
                        (channel, cutoff),
                    )
                    suppressed = db.execute(
                        "SELECT coalesce(sum(s.count), 0) FROM alert_suppressions s JOIN alert_rules r ON r.id=s.rule_id WHERE r.channel_id=? AND r.owner_id IS NOT NULL AND s.day_at>=?",
                        (channel, cutoff // 86_400_000 * 86_400_000),
                    ).fetchone()[0]
                    return dict(alerts=alerts, outcomes=outcomes, suppressed=suppressed)
            except sqlite3.OperationalError:
                if cancelled.is_set():
                    return {}
                raise

        task = asyncio.create_task(asyncio.to_thread(read))
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled.set()
            await asyncio.gather(task, return_exceptions=True)
            raise
        self.authorize(actor)
        if self.channel_id != channel:
            raise PermissionError("Channel changed during statistics query")
        return result

    def record_error(self, code: str, timestamp: int) -> None:
        code = text(code, 80, trim=False, minimum=0)
        timestamp = integer(timestamp)
        self.db.execute(
            "INSERT INTO application_state (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (
                f"channel_delivery_error:{self.channel_id}",
                json.dumps(dict(code=code, timestamp=timestamp)),
                timestamp,
            ),
        )

    def status(self, actor: str) -> dict[str, Any]:
        self.authorize(actor)
        channel = rows(
            self.db,
            "SELECT * FROM telegram_channels WHERE channel_id=?",
            (self.channel_id,),
        )[0]
        rules = self.list(actor)
        success = rows(
            self.db,
            "SELECT value FROM application_state WHERE key=?",
            (f"channel_last_success:{self.channel_id}",),
        )
        error = rows(
            self.db,
            "SELECT value FROM application_state WHERE key=?",
            (f"channel_delivery_error:{self.channel_id}",),
        )
        last_success = None
        last_error = None
        try:
            if success:
                last_success = integer(float(success[0]["value"]))
        except ValueError, TypeError, OverflowError:
            pass
        try:
            if error:
                v = obj(loads(error[0]["value"]))
                last_error = dict(
                    code=text(v["code"], 80, trim=False, minimum=0),
                    timestamp=number(v["timestamp"], -float("inf")),
                )
        except ValueError, TypeError, KeyError, OverflowError:
            pass
        return {
            **channel,
            "can_post": self._writable(),
            "total": len(rules),
            "active": sum(bool(r["enabled"] and self.eligible(r)) for r in rules),
            "last_success": last_success,
            "last_error": last_error,
        }
