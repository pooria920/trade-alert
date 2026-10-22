"""Private registration and ownership-scoped operations. No Telegram I/O."""

import sqlite3
import time
from collections.abc import Callable, Sequence
from typing import Any

from .config import user_ids
from .database import rows, transaction
from .definitions import parse_definition
from .market import Instrument, parse_instrument


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class UserService:
    def __init__(
        self,
        db: sqlite3.Connection,
        allowed_user_ids: Sequence[str],
        now: Callable[[], int] = now_ms,
    ) -> None:
        self.db = db
        self.allowed = frozenset(allowed_user_ids)
        for actor in self.allowed:
            if user_ids(actor) != [actor]:
                raise ValueError("Invalid authorized user ID")
        self.now = now

    def authorize(self, actor: str) -> None:
        if actor not in self.allowed:
            raise PermissionError("Unauthorized user")

    def register(self, actor: str, private_chat_id: str) -> None:
        """Call only after an incoming authorized private interaction is observed."""
        self.authorize(actor)
        if private_chat_id != actor:
            raise PermissionError("A private conversation is required")
        now = self.now()
        self.db.execute(
            "INSERT INTO users (telegram_user_id, private_chat_id, created_at, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT(telegram_user_id) DO UPDATE SET private_chat_id=excluded.private_chat_id, updated_at=excluded.updated_at",
            (actor, private_chat_id, now, now),
        )

    def preferences(self, actor: str) -> dict[str, Any] | None:
        self.authorize(actor)
        result = rows(self.db, "SELECT * FROM users WHERE telegram_user_id=?", (actor,))
        return result[0] if result else None

    def set_notifications(self, actor: str, enabled: bool) -> None:
        self.authorize(actor)
        if type(enabled) is not bool:
            raise ValueError("Expected notification boolean")
        self.db.execute(
            "UPDATE users SET notifications_enabled=?, updated_at=? WHERE telegram_user_id=?",
            (enabled, self.now(), actor),
        )

    def list_symbols(self, actor: str) -> list[dict[str, Any]]:
        self.authorize(actor)
        return rows(
            self.db,
            "SELECT s.ins_code, s.symbol FROM watchlists w JOIN symbols s ON s.ins_code=w.ins_code WHERE w.user_id=? ORDER BY s.symbol, s.ins_code",
            (actor,),
        )

    def find_symbol(self, actor: str, ins_code: str) -> dict[str, Any] | None:
        self.authorize(actor)
        result = rows(
            self.db,
            "SELECT s.ins_code, s.symbol FROM watchlists w JOIN symbols s ON s.ins_code=w.ins_code WHERE w.user_id=? AND w.ins_code=?",
            (actor, ins_code),
        )
        return result[0] if result else None

    def add_symbol(self, actor: str, value: object) -> None:
        self.authorize(actor)
        item = parse_instrument(value)
        if self.preferences(actor) is None:
            raise PermissionError("An observed private registration is required")
        with transaction(self.db):
            self.db.execute(
                "INSERT INTO symbols (ins_code, symbol, created_at) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                (item.ins_code, item.symbol, self.now()),
            )
            self.db.execute(
                "INSERT INTO watchlists (user_id, ins_code, created_at) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                (actor, item.ins_code, self.now()),
            )

    def remove_symbol(self, actor: str, ins_code: str) -> bool:
        self.authorize(actor)
        with transaction(self.db):
            removed = self.db.execute(
                "DELETE FROM watchlists WHERE user_id=? AND ins_code=?",
                (actor, ins_code),
            ).rowcount
            if removed:
                self.db.execute(
                    "DELETE FROM alert_rules WHERE owner_id=? AND ins_code=? AND channel_id IS NULL",
                    (actor, ins_code),
                )
        return removed > 0

    def list_rules(self, actor: str) -> list[dict[str, Any]]:
        self.authorize(actor)
        return rows(
            self.db,
            "SELECT * FROM alert_rules WHERE owner_id=? AND channel_id IS NULL",
            (actor,),
        )

    def remove_rule(self, actor: str, rule_id: str) -> bool:
        self.authorize(actor)
        return (
            self.db.execute(
                "DELETE FROM alert_rules WHERE id=? AND owner_id=? AND channel_id IS NULL",
                (rule_id, actor),
            ).rowcount
            > 0
        )

    def notification_target(self, actor: str, rule_id: str) -> str | None:
        self.authorize(actor)
        result = rows(
            self.db,
            "SELECT u.private_chat_id, r.definition FROM users u JOIN alert_rules r ON r.owner_id=u.telegram_user_id JOIN watchlists w ON w.user_id=u.telegram_user_id AND w.ins_code=r.ins_code WHERE u.telegram_user_id=? AND r.id=? AND r.channel_id IS NULL AND r.enabled=1 AND u.notifications_enabled=1",
            (actor, rule_id),
        )
        if not result or parse_definition(result[0]["definition"]) is None:
            return None
        destination = result[0]["private_chat_id"]
        return actor if destination == actor else None

    def monitored_symbols(self) -> list[Instrument]:
        if not self.allowed:
            return []
        placeholders = ",".join("?" for _ in self.allowed)
        result = rows(
            self.db,
            f"SELECT DISTINCT s.ins_code, s.symbol FROM watchlists w JOIN symbols s ON s.ins_code=w.ins_code WHERE w.user_id IN ({placeholders}) ORDER BY s.ins_code",
            tuple(self.allowed),
        )
        return [Instrument(r["ins_code"], r["symbol"], r["symbol"]) for r in result]
