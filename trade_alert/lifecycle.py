"""Shared observed transitions from accepted evidence only; no transport or users."""

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .database import rows, transaction
from .evaluators import (
    EvaluationResult,
    ObservedPeak,
    PeakConfig,
    QueueSnapshot,
    peak_recovered,
)
from .validation import choice, code, integer, loads, number, obj, text

EVENTS = ("FORMED", "WEAKENED", "CONFIRMED_DISAPPEARED", "REFORMED")


@dataclass(frozen=True, slots=True)
class LifecycleConfig:
    minimum_volume: int = 0
    threshold_percent: float = 30
    minimum_baseline_volume: int = 0
    minimum_disappearance_seconds: int = 0

    def __post_init__(self) -> None:
        integer(self.minimum_volume)
        PeakConfig(self.threshold_percent, self.minimum_baseline_volume)
        integer(self.minimum_disappearance_seconds, 0, 86_400)


@dataclass(frozen=True, slots=True)
class LifecycleOptions:
    confirmation_snapshots: int = 2
    max_gap_ms: int = 30_000
    weakening_percent: int = 20
    escalation_step_percent: int = 10
    minimum_baseline_volume: int = 0
    retention_days: int = 30

    def __post_init__(self) -> None:
        integer(self.confirmation_snapshots, 1, 10)
        integer(self.max_gap_ms, 1000, 300_000)
        integer(self.weakening_percent, 1, 100)
        integer(self.escalation_step_percent, 1, 100)
        integer(self.minimum_baseline_volume)
        integer(self.retention_days, 1, 365)


@dataclass(frozen=True, slots=True)
class LifecycleContext:
    valid_sides: frozenset[str]
    events: tuple[dict[str, Any], ...] = ()
    weak_occurrences: dict[str, int | None] = field(default_factory=dict)


def event_kind(kind: str) -> str:
    return (
        "QUEUE_DISAPPEARED"
        if kind.endswith("CONFIRMED_DISAPPEARED")
        else "QUEUE_" + kind.split("_QUEUE_", 1)[1]
    )


def weakening(kind: str) -> bool:
    return kind.endswith(("DROP_PERCENT", "WEAKENED"))


def validate_event(value: object) -> dict[str, Any]:
    v = obj(value)
    text(v["id"], 80)
    code(v["ins_code"])
    choice(v["side"], ("buy", "sell"))
    text(v["session_id"], 1000)
    choice(
        v["event_type"],
        ("QUEUE_FORMED", "QUEUE_WEAKENED", "QUEUE_DISAPPEARED", "QUEUE_REFORMED"),
    )
    for key in (
        "timestamp",
        "current_volume",
        "cycles",
        "occurrence_at",
        "first_observed_at",
    ):
        integer(v[key])
    for key in (
        "previous_volume",
        "previous_timestamp",
        "peak_volume",
        "disappeared_at",
    ):
        if v[key] is not None:
            integer(v[key])
    if v["drop_percent"] is not None:
        number(v["drop_percent"], 0, 100)
    levels = loads(v["crossed_levels"])
    if not isinstance(levels, list):
        raise ValueError("Invalid weakening levels")
    for level in levels:
        number(level, 0.000001, 100)
    return v


def evaluate_lifecycle(
    kind: str,
    config: LifecycleConfig,
    current: QueueSnapshot,
    context: LifecycleContext | None,
) -> EvaluationResult:
    side = "buy" if kind.startswith("BUY_") else "sell"
    if context is None or side not in context.valid_sides:
        return EvaluationResult("INSUFFICIENT_DATA")
    for event in context.events:
        if (
            event["side"] != side
            or event["event_type"] != event_kind(kind)
            or event["timestamp"] != current.timestamp
            or event["ins_code"] != current.ins_code
        ):
            continue
        previous = event["previous_volume"]
        if kind.endswith("WEAKENED"):
            if (
                previous is None
                or previous <= 0
                or previous < config.minimum_baseline_volume
                or config.threshold_percent not in loads(event["crossed_levels"])
            ):
                continue
        elif kind.endswith("CONFIRMED_DISAPPEARED"):
            if previous is None or previous <= 0 or previous < config.minimum_volume:
                continue
        elif current.volume < max(1, config.minimum_volume):
            continue
        if kind.endswith("REFORMED") and (
            event["disappeared_at"] is None
            or current.timestamp - event["disappeared_at"]
            < config.minimum_disappearance_seconds * 1000
        ):
            continue
        return EvaluationResult(
            "TRIGGER",
            previous if previous is not None else 0,
            current.volume,
            100
            if kind.endswith("CONFIRMED_DISAPPEARED")
            else event["drop_percent"] or 0,
            details={
                **event,
                "crossed_levels": json.dumps([config.threshold_percent])
                if kind.endswith("WEAKENED")
                else "[]",
            },
        )
    return EvaluationResult("CLEAR")


class LifecycleTracker:
    def __init__(
        self, db: sqlite3.Connection, options: LifecycleOptions = LifecycleOptions()
    ) -> None:
        self.db, self.options = db, options
        self.policy_key = repr(
            (
                options.confirmation_snapshots,
                options.max_gap_ms,
                options.weakening_percent,
                options.escalation_step_percent,
                options.minimum_baseline_volume,
            )
        )

    def unavailable(self, ins_code: str, reason: str = "unavailable") -> None:
        code(ins_code)
        # Reasons come from detector/monitor codes, never raw exceptions or payloads.
        safe_reason = (
            "trading-state"
            if reason == "trading-state"
            else "closed"
            if reason == "closed"
            else "unavailable"
        )
        self.db.execute(
            "UPDATE queue_lifecycle_states SET observation_valid=0, observation_reason=?, continuous=0, pending_kind=NULL, pending_count=0, pending_since=NULL WHERE ins_code=?",
            (safe_reason, ins_code),
        )

    def _save(self, state: dict[str, Any]) -> None:
        fields = tuple(state)
        self.db.execute(
            f"INSERT INTO queue_lifecycle_states ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)}) ON CONFLICT(ins_code, side) DO UPDATE SET "
            + ",".join(
                f"{k}=excluded.{k}" for k in fields if k not in ("ins_code", "side")
            ),
            tuple(state.values()),
        )

    def observe(
        self,
        value: QueueSnapshot,
        session: str,
        peaks: dict[str, ObservedPeak],
        levels: dict[str, list[float]] | None = None,
    ) -> LifecycleContext:
        text(session, 2**53 - 1, trim=False)
        with transaction(self.db):
            for side in ("buy", "sell"):
                demands = sorted(
                    {
                        float(self.options.weakening_percent),
                        *(
                            number(v, 0.000001, 100)
                            for v in (levels or {}).get(side, [])
                        ),
                    }
                )
                found = rows(
                    self.db,
                    "SELECT * FROM queue_lifecycle_states WHERE ins_code=? AND side=?",
                    (value.ins_code, side),
                )
                old = found[0] if found else None
                if old is not None and value.timestamp <= old["last_source_at"]:
                    continue
                if old is None or old["session_id"] != session:
                    state: dict[str, Any] = dict(
                        ins_code=value.ins_code,
                        side=side,
                        session_id=session,
                        status="UNKNOWN",
                        first_observed_at=value.timestamp,
                        last_source_at=value.timestamp,
                        last_volume=None,
                        last_volume_at=None,
                        last_queue_at=None,
                        last_formed_at=None,
                        last_disappeared_at=None,
                        cycles=0,
                        observation_valid=0,
                        observation_reason=None,
                        continuous=0,
                        pending_kind=None,
                        pending_count=0,
                        pending_since=None,
                        weak_baseline=None,
                        weak_baseline_at=None,
                        weak_boundary=None,
                        weak_levels="[]",
                        weak_last_percent=0,
                        weak_occurrence_at=None,
                        cycle_peak_volume=0,
                        cycle_peak_timestamp=None,
                        policy_key=self.policy_key,
                    )
                else:
                    state = old
                    if (
                        value.timestamp - state["last_source_at"]
                        > self.options.max_gap_ms
                        or state["policy_key"] != self.policy_key
                    ):
                        state.update(
                            continuous=0,
                            pending_kind=None,
                            pending_count=0,
                            pending_since=None,
                        )
                    state["last_source_at"] = value.timestamp
                    state["policy_key"] = self.policy_key
                if value.queue_type not in (side, "none"):
                    state.update(
                        observation_valid=0,
                        observation_reason="opposite-side",
                        continuous=0,
                        pending_kind=None,
                        pending_count=0,
                        pending_since=None,
                    )
                else:
                    state.update(observation_valid=1, observation_reason=None)
                    self._step(state, value, peaks.get(side), demands)
                self._save(state)
        states = rows(
            self.db,
            "SELECT side, weak_occurrence_at FROM queue_lifecycle_states WHERE ins_code=? AND session_id=? AND last_source_at=? AND observation_valid=1 AND continuous=1",
            (value.ins_code, session, value.timestamp),
        )
        events = rows(
            self.db,
            "SELECT * FROM queue_lifecycle_events WHERE ins_code=? AND session_id=? AND timestamp=? ORDER BY side, event_type",
            (value.ins_code, session, value.timestamp),
        )
        return LifecycleContext(
            frozenset(s["side"] for s in states),
            tuple(events),
            {s["side"]: s["weak_occurrence_at"] for s in states},
        )

    def _confirm(self, s: dict[str, Any], kind: str, timestamp: int) -> bool:
        if s["pending_kind"] != kind:
            s.update(pending_kind=kind, pending_count=0, pending_since=timestamp)
        s["pending_count"] += 1
        return bool(s["pending_count"] >= self.options.confirmation_snapshots)

    def _clear_pending(self, s: dict[str, Any]) -> None:
        s.update(pending_kind=None, pending_count=0, pending_since=None)

    def _emit(
        self,
        s: dict[str, Any],
        value: QueueSnapshot,
        kind: str,
        peak: ObservedPeak | None,
        *,
        baseline: int | None = None,
        percent: float | None = None,
        crossed: Sequence[float] = (),
    ) -> None:
        occurrence = (
            s["weak_occurrence_at"] if kind == "QUEUE_WEAKENED" else value.timestamp
        )
        key = f"trade-alert:lifecycle:{value.ins_code}:{s['side']}:{s['session_id']}:{kind}:{value.timestamp}"
        self.db.execute(
            "INSERT INTO queue_lifecycle_events(id, ins_code, side, session_id, event_type, timestamp, previous_volume, previous_timestamp, current_volume, peak_volume, drop_percent, disappeared_at, cycles, occurrence_at, first_observed_at, crossed_levels) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (
                str(uuid5(NAMESPACE_URL, key)),
                value.ins_code,
                s["side"],
                s["session_id"],
                kind,
                value.timestamp,
                s["last_volume"] if baseline is None else baseline,
                s["weak_baseline_at"]
                if kind == "QUEUE_WEAKENED"
                else s["last_volume_at"],
                value.volume,
                baseline if kind == "QUEUE_WEAKENED" else peak.volume if peak else None,
                percent,
                value.timestamp
                if kind == "QUEUE_DISAPPEARED"
                else s["last_disappeared_at"],
                s["cycles"],
                occurrence,
                s["first_observed_at"],
                json.dumps(list(crossed)),
            ),
        )

    def _step(
        self,
        s: dict[str, Any],
        value: QueueSnapshot,
        peak: ObservedPeak | None,
        levels: list[float],
    ) -> None:
        target = "NO_QUEUE" if value.queue_type == "none" else "ACTIVE"
        if not s["continuous"]:
            if self._confirm(s, "PRIME_" + target, value.timestamp):
                s.update(
                    status=target,
                    continuous=1,
                    last_volume=value.volume,
                    last_volume_at=value.timestamp,
                    last_queue_at=value.timestamp if target == "ACTIVE" else None,
                    weak_baseline=None,
                    weak_baseline_at=None,
                    weak_boundary=None,
                    weak_levels="[]",
                    weak_last_percent=0,
                    weak_occurrence_at=None,
                    cycle_peak_volume=value.volume,
                    cycle_peak_timestamp=value.timestamp
                    if target == "ACTIVE"
                    else None,
                )
                self._clear_pending(s)
            return
        active = s["status"] in ("ACTIVE", "WEAKENED")
        if target == "NO_QUEUE" and active:
            if self._confirm(s, "DISAPPEARED", value.timestamp):
                self._emit(s, value, "QUEUE_DISAPPEARED", peak)
                s.update(
                    status="DISAPPEARED",
                    last_disappeared_at=value.timestamp,
                    last_volume=0,
                    last_volume_at=value.timestamp,
                    weak_baseline=None,
                    weak_baseline_at=None,
                    weak_boundary=None,
                    weak_levels="[]",
                    weak_last_percent=0,
                    weak_occurrence_at=None,
                )
                self._clear_pending(s)
            return
        if target == "ACTIVE" and not active:
            if self._confirm(s, "ACTIVE", value.timestamp):
                reformed = s["status"] == "DISAPPEARED"
                if reformed:
                    s["cycles"] += 1
                self._emit(
                    s, value, "QUEUE_REFORMED" if reformed else "QUEUE_FORMED", peak
                )
                s.update(
                    status="ACTIVE",
                    last_formed_at=value.timestamp,
                    last_volume=value.volume,
                    last_volume_at=value.timestamp,
                    last_queue_at=value.timestamp,
                    cycle_peak_volume=value.volume,
                    cycle_peak_timestamp=value.timestamp,
                )
                self._clear_pending(s)
            return
        if target == "NO_QUEUE":
            self._clear_pending(s)
            s["last_volume"] = 0
            s["last_volume_at"] = value.timestamp
            return
        if value.volume > s["cycle_peak_volume"]:
            s.update(
                cycle_peak_volume=value.volume, cycle_peak_timestamp=value.timestamp
            )
        baseline = s["weak_baseline"] or s["cycle_peak_volume"]
        percent = (baseline - value.volume) / baseline * 100 if baseline else 0
        if s["weak_baseline"] is not None and peak_recovered(
            s["side"], PeakConfig(s["weak_boundary"]), value, baseline
        ):
            s.update(
                status="ACTIVE",
                weak_baseline=None,
                weak_baseline_at=None,
                weak_boundary=None,
                weak_levels="[]",
                weak_last_percent=0,
                weak_occurrence_at=None,
            )
            self._clear_pending(s)
        else:
            previous_levels = json.loads(s["weak_levels"])
            highest_level = max(previous_levels, default=0)
            # Retain current configured levels and one escalation high-water mark.
            configured = set(levels)
            notified = [
                level
                for level in previous_levels
                if (level in configured or level == highest_level)
                and not peak_recovered(s["side"], PeakConfig(level), value, baseline)
            ]
            s["weak_levels"] = json.dumps(notified)
            notified_levels = set(notified)
            candidates = [
                v
                for v in levels
                if v not in notified_levels and s["weak_last_percent"] < v <= percent
            ]
            if s["weak_baseline"] is not None:
                next_level = (
                    max(notified, default=s["weak_boundary"])
                    + self.options.escalation_step_percent
                )
                if next_level <= percent and next_level <= 100:
                    candidates.append(next_level)
            if (
                baseline > 0
                and baseline >= self.options.minimum_baseline_volume
                and candidates
            ):
                # Keep confirming the same boundary while its evidence remains true.
                boundary = min(candidates)
                if self._confirm(s, f"WEAK_{boundary}", value.timestamp):
                    if s["weak_baseline"] is None:
                        s.update(
                            weak_baseline=baseline,
                            weak_baseline_at=s["cycle_peak_timestamp"],
                            weak_boundary=boundary,
                            weak_occurrence_at=value.timestamp,
                        )
                    s["status"] = "WEAKENED"
                    self._emit(
                        s,
                        value,
                        "QUEUE_WEAKENED",
                        peak,
                        baseline=baseline,
                        percent=percent,
                        crossed=candidates,
                    )
                    s["weak_levels"] = json.dumps(sorted(set(notified + candidates)))
                    s["weak_last_percent"] = percent
                    self._clear_pending(s)
            else:
                self._clear_pending(s)
                s["weak_last_percent"] = percent
        s.update(
            last_volume=value.volume,
            last_volume_at=value.timestamp,
            last_queue_at=value.timestamp,
        )

    def history(
        self, ins_code: str, session: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        return rows(
            self.db,
            "SELECT * FROM queue_lifecycle_events WHERE ins_code=? AND session_id=? ORDER BY timestamp DESC, id DESC LIMIT ?",
            (
                code(ins_code),
                text(session, 2**53 - 1, trim=False),
                integer(limit, 1, 100),
            ),
        )

    def delete_expired(self, now: int) -> int:
        cutoff = max(0, integer(now) - self.options.retention_days * 86_400_000)
        return self.db.execute(
            "DELETE FROM queue_lifecycle_events WHERE id IN (SELECT id FROM queue_lifecycle_events WHERE timestamp<? ORDER BY timestamp LIMIT 1000)",
            (cutoff,),
        ).rowcount
