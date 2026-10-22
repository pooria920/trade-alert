"""Synchronous persistent evaluation. No Telegram I/O or delivery destinations."""

import json
import logging
import sqlite3
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from .channels import ChannelService
from .composite import definition_recovered, evaluate_definition
from .database import rows, transaction
from .definitions import Group, RuleTree, parse_definition
from .evaluators import QueueSnapshot
from .snapshots import SnapshotRepository
from .users import UserService, now_ms
from .validation import integer, text


class AlertEngine:
    def __init__(
        self,
        db: sqlite3.Connection,
        snapshots: SnapshotRepository,
        users: UserService,
        max_age_ms: int = 30_000,
        now: Callable[[], int] = now_ms,
        channels: ChannelService | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.db, self.snapshots, self.users = db, snapshots, users
        self.max_age_ms = integer(max_age_ms)
        self.now, self.channels = now, channels
        self.logger = logger or logging.getLogger(__name__)
        self.started_at = integer(now())

    def on_snapshot(self, value: QueueSnapshot, session_id: str) -> None:
        """Called after a new detector-accepted insert; persisted state blocks replays.

        An explicit retry after a rolled-back evaluation is safe. Reopening the
        engine never replays snapshots older than this process's startup.
        """
        if not isinstance(value, QueueSnapshot):
            return
        text(session_id, 2**53 - 1, trim=False)
        now = integer(self.now())
        if (
            value.timestamp < self.started_at
            or value.timestamp > now
            or now - value.timestamp > self.max_age_ms
            or not self.snapshots.has_alert_snapshot(value, session_id)
        ):
            return
        history: dict[int, QueueSnapshot | None] = {}

        def at(timestamp: int) -> QueueSnapshot | None:
            if timestamp not in history:
                history[timestamp] = self.snapshots.alert_baseline(
                    value.ins_code, timestamp, session_id
                )
            return history[timestamp]

        for rule in rows(
            self.db,
            "SELECT * FROM alert_rules WHERE ins_code=? AND enabled=1 AND owner_id IS NOT NULL",
            (value.ins_code,),
        ):
            try:
                private = rule["channel_id"] is None
                definition = parse_definition(rule["definition"], channel=not private)
                if (
                    definition is None
                    or value.timestamp < rule["created_at"]
                    or (
                        private
                        and (
                            rule["owner_id"] not in self.users.allowed
                            or not self.users.find_symbol(
                                rule["owner_id"], value.ins_code
                            )
                        )
                    )
                    or (
                        not private
                        and (self.channels is None or not self.channels.eligible(rule))
                    )
                ):
                    continue
                with transaction(self.db):
                    self._evaluate(rule, definition, value, session_id, now, at)
            except Exception:
                # Never include definitions, raw errors, credentials or market payloads.
                self.logger.error(
                    "Rule evaluation failed", extra={"rule_id": rule["id"]}
                )

    def _evaluate(
        self,
        rule: dict[str, Any],
        definition: RuleTree,
        value: QueueSnapshot,
        session_id: str,
        now: int,
        at: Callable[[int], QueueSnapshot | None],
    ) -> None:
        previous = rows(
            self.db, "SELECT * FROM alert_states WHERE rule_id=?", (rule["id"],)
        )
        state = previous[0] if previous else None
        if state and value.timestamp <= state["last_evaluated_at"]:
            return
        if (
            state is None
            or state["revision"] != rule["revision"]
            or state["session_id"] != session_id
        ):
            self.db.execute(
                "INSERT INTO alert_states (rule_id, revision, session_id, status, last_evaluated_at, last_triggered_at) VALUES (?, ?, ?, 'ARMED', ?, ?) ON CONFLICT(rule_id) DO UPDATE SET revision=excluded.revision, session_id=excluded.session_id, status='ARMED', baseline_volume=NULL, recovery_data=NULL, last_evaluated_at=excluded.last_evaluated_at",
                (
                    rule["id"],
                    rule["revision"],
                    session_id,
                    value.timestamp,
                    state["last_triggered_at"] if state else None,
                ),
            )
            return
        cooled = (
            state["last_triggered_at"] is None
            or now - state["last_triggered_at"] >= rule["cooldown_seconds"] * 1000
        )
        if state["status"] != "ARMED":
            state["status"] = "WAITING_FOR_RECOVERY"
            if (
                definition_recovered(
                    definition,
                    value,
                    at,
                    state["baseline_volume"],
                    state["recovery_data"],
                )
                and cooled
            ):
                state.update(status="ARMED", baseline_volume=None, recovery_data=None)
        else:
            result = evaluate_definition(definition, value, at)
            if result["status"] == "TRIGGER" and cooled:
                matches = result.get("matches")
                state.update(
                    status="TRIGGERED",
                    last_triggered_at=now,
                    baseline_volume=result["previousVolume"],
                    recovery_data=json.dumps(matches) if matches else None,
                )
                occurrence = (
                    f"{rule['id']}:{rule['revision']}:{session_id}:{value.timestamp}"
                )
                payload = dict(
                    symbol=value.symbol,
                    type="COMPOSITE"
                    if matches or isinstance(definition, Group)
                    else definition.type,
                    operator=definition.operator
                    if isinstance(definition, Group)
                    else None,
                    timestamp=value.timestamp,
                    previousVolume=result["previousVolume"],
                    currentVolume=result["currentVolume"],
                    actualPercent=result["actualPercent"],
                    windowMinutes=result["windowMinutes"],
                )
                if matches:
                    payload["matches"] = matches
                event_id = str(uuid4())
                inserted = self.db.execute(
                    "INSERT INTO alert_events (id, rule_id, occurrence_key, rule_revision, payload, message, created_at) VALUES (?, ?, ?, ?, ?, '', ?) ON CONFLICT DO NOTHING",
                    (
                        event_id,
                        rule["id"],
                        occurrence,
                        rule["revision"],
                        json.dumps(payload),
                        now,
                    ),
                ).rowcount
                if inserted:
                    self.db.execute(
                        "INSERT INTO notification_outbox (event_id, next_attempt_at) VALUES (?, ?)",
                        (event_id, now),
                    )
        self.db.execute(
            "UPDATE alert_states SET status=?, last_evaluated_at=?, last_triggered_at=?, baseline_volume=?, recovery_data=? WHERE rule_id=?",
            (
                state["status"],
                value.timestamp,
                state["last_triggered_at"],
                state["baseline_volume"],
                state["recovery_data"],
                rule["id"],
            ),
        )
