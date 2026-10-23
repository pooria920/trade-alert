"""Private alert CRUD; all reads and writes are owner and private-scope bound."""

import builtins
import json
import sqlite3
from collections.abc import Callable, Sequence
from typing import Any
from uuid import uuid4

from .database import rows, transaction
from .definitions import definition_json, parse_definition, parse_tree
from .quality import parse_policy
from .users import UserService, now_ms
from .validation import integer


def reset_rule(db: sqlite3.Connection, rule_id: str) -> None:
    """Must run in the same transaction as the rule edit."""
    db.execute("DELETE FROM alert_states WHERE rule_id=?", (rule_id,))
    db.execute(
        "UPDATE notification_outbox SET status='CANCELLED' WHERE status='PENDING' AND event_id IN (SELECT id FROM alert_events WHERE rule_id=?)",
        (rule_id,),
    )


def evaluation_state(
    db: sqlite3.Connection, rule: dict[str, Any]
) -> dict[str, Any] | None:
    """Internal lookup; callers must first resolve an authorized, scoped rule."""
    found = rows(
        db,
        "SELECT * FROM alert_states WHERE rule_id=? AND revision=?",
        (rule["id"], rule["revision"]),
    )
    return found[0] if found else None


class AlertService:
    def __init__(
        self,
        db: sqlite3.Connection,
        users: UserService,
        default_cooldown: int = 300,
        history_limit: int = 10,
        now: Callable[[], int] = now_ms,
        admins: Sequence[str] = (),
    ) -> None:
        self.db, self.users, self.now = db, users, now
        self.default_cooldown = integer(default_cooldown, 0, 86_400)
        self.history_limit = integer(history_limit, 1, 10)
        self.admins = admins

    def list(self, actor: str) -> list[dict[str, Any]]:
        result = []
        for row in self.users.list_rules(actor):
            definition = parse_definition(row["definition"])
            if definition is not None:
                result.append({**row, "definition": definition_json(definition)})
        return result

    def get(self, actor: str, rule_id: str) -> dict[str, Any] | None:
        rule = next((r for r in self.list(actor) if r["id"] == rule_id), None)
        if rule is not None:
            rule["evaluation"] = evaluation_state(self.db, rule)
            rule["notifications_enabled"] = bool(
                (self.users.preferences(actor) or {}).get("notifications_enabled")
            )
        return rule

    def create(
        self, actor: str, ins_code: str, value: object, cooldown: int | None = None
    ) -> str:
        self.users.authorize(actor)
        definition = definition_json(parse_tree(value, channel=False))
        cooldown = (
            self.default_cooldown if cooldown is None else integer(cooldown, 0, 86_400)
        )
        if not self.users.find_symbol(actor, ins_code):
            raise PermissionError("Symbol is not in your watchlist")
        rule_id = str(uuid4())
        self.db.execute(
            "INSERT INTO alert_rules (id, owner_id, ins_code, type, definition, cooldown_seconds, created_at) VALUES (?, ?, ?, 'BUY_QUEUE_DISAPPEARED', ?, ?, ?)",
            (rule_id, actor, ins_code, json.dumps(definition), cooldown, self.now()),
        )
        return rule_id

    def update(
        self,
        actor: str,
        rule_id: str,
        value: object,
        enabled: bool | None = None,
        cooldown: int | None = None,
        *,
        quality: object = None,
        revision: int | None = None,
    ) -> bool:
        self.users.authorize(actor)
        definition = definition_json(parse_tree(value, channel=False))
        if enabled is not None and type(enabled) is not bool:
            raise ValueError("Expected enabled boolean")
        if cooldown is not None:
            integer(cooldown, 0, 86_400)
        settings = None
        if quality is not None:
            if actor not in self.admins or self.users.preferences(actor) is None:
                raise PermissionError("Advanced settings require a current admin")
            settings = json.dumps(parse_policy(quality).to_json())
        if revision is not None:
            integer(revision, 1)
        with transaction(self.db):
            rule = self.get(actor, rule_id)
            if (
                rule is None
                or not self.users.find_symbol(actor, rule["ins_code"])
                or (revision is not None and rule["revision"] != revision)
            ):
                return False
            self.db.execute(
                "UPDATE alert_rules SET definition=?, revision=revision+1, enabled=?, cooldown_seconds=?, quality_config=?, created_at=? WHERE id=? AND owner_id=? AND channel_id IS NULL",
                (
                    json.dumps(definition),
                    rule["enabled"] if enabled is None else enabled,
                    rule["cooldown_seconds"] if cooldown is None else cooldown,
                    rule["quality_config"] if settings is None else settings,
                    self.now(),
                    rule_id,
                    actor,
                ),
            )
            reset_rule(self.db, rule_id)
        return True

    def set_enabled(self, actor: str, rule_id: str, enabled: bool) -> bool:
        self.users.authorize(actor)
        if type(enabled) is not bool:
            raise ValueError("Expected enabled boolean")
        rule = self.get(actor, rule_id)
        return rule is not None and (
            bool(rule["enabled"]) == enabled
            or self.update(actor, rule_id, rule["definition"], enabled)
        )

    def remove(self, actor: str, rule_id: str) -> bool:
        return self.users.remove_rule(actor, rule_id)

    def history(self, actor: str, page: int = 0) -> builtins.list[dict[str, Any]]:
        self.users.authorize(actor)
        page = integer(page, 0, 1_000_000)
        events = rows(
            self.db,
            "SELECT e.*, r.ins_code FROM alert_events e JOIN alert_rules r ON r.id=e.rule_id WHERE r.owner_id=? AND r.channel_id IS NULL ORDER BY e.created_at DESC, e.id DESC LIMIT ? OFFSET ?",
            (actor, self.history_limit, page * self.history_limit),
        )
        for event in events:
            event["outcomes"] = rows(
                self.db,
                "SELECT * FROM alert_outcomes WHERE event_id=? ORDER BY window_minutes",
                (event["id"],),
            )
        return events
