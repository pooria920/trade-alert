"""Durable queue observations from the accepted pipeline; no market requests.

Queue price is not a traded/closing price. Price fields remain NULL until the
pipeline supplies a verified, explicitly identified traded-price metric.
"""

import logging
import sqlite3
from collections.abc import Callable, Sequence
from typing import Any

from .database import rows
from .evaluators import QueueSnapshot
from .market import Instrument
from .schedule import MarketSchedule
from .snapshots import SnapshotRepository
from .users import now_ms
from .validation import integer, number


def change_percent(initial: float | None, observed: float | None) -> float | None:
    if initial is None or observed is None:
        return None
    try:
        number(initial)
        number(observed)
        if initial <= 0:
            return None
        return number((observed - initial) / initial * 100, -float("inf"))
    except ValueError, OverflowError:
        return None


class AlertOutcomeTracker:
    def __init__(
        self,
        db: sqlite3.Connection,
        snapshots: SnapshotRepository,
        schedule: MarketSchedule,
        eligible: Callable[[dict[str, Any]], bool],
        windows: Sequence[int] = (5, 15, 30),
        tolerance_ms: int = 30_000,
        retention_days: int = 30,
        now: Callable[[], int] = now_ms,
        logger: logging.Logger | None = None,
        max_age_ms: int = 30_000,
    ) -> None:
        if not windows or any(
            type(w) is not int or w not in (5, 15, 30) for w in windows
        ):
            raise ValueError("Invalid outcome windows")
        if len(set(windows)) != len(windows):
            raise ValueError("Duplicate outcome window")
        self.db, self.snapshots, self.schedule = db, snapshots, schedule
        self.eligible, self.now = eligible, now
        self.windows = tuple(windows)
        self.tolerance_ms = integer(tolerance_ms, 1000, 300_000)
        self.retention_ms = integer(retention_days, 1, 365) * 86_400_000
        self.max_age_ms = integer(max_age_ms, 1, 300_000)
        self.logger = logger or logging.getLogger(__name__)

    def schedule_event(
        self, event_id: str, rule: dict[str, Any], value: QueueSnapshot, session: str
    ) -> None:
        """Called inside the engine's event/state/outbox transaction."""
        for window in self.windows:
            target = value.timestamp + window * 60_000
            cutoff = target + self.tolerance_ms
            status = "pending"
            if self.schedule.session_id(target) != session:
                status = "insufficient_data"
            # Observations must also match session_id, including near-close windows.
            self.db.execute(
                "INSERT INTO alert_outcomes (event_id, source_event_id, owner_id, channel_id, ins_code, session_id, window_minutes, target_at, cutoff_at, initial_queue_volume, queue_side, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    event_id,
                    rule["owner_id"],
                    rule["channel_id"],
                    value.ins_code,
                    session,
                    window,
                    target,
                    cutoff,
                    value.volume,
                    value.queue_type,
                    status,
                ),
            )
        self.logger.info(
            "Outcome observations scheduled", extra={"windows": len(self.windows)}
        )

    def _observe(self, row: dict[str, Any], value: QueueSnapshot) -> None:
        volume = (
            value.volume if value.queue_type in (row["queue_side"], "none") else None
        )
        # A none baseline has no original side; a later queue cannot be compared.
        status = "completed" if volume is not None else "insufficient_data"
        self.db.execute(
            "UPDATE alert_outcomes SET observed_at=?, observed_queue_volume=?, queue_change_percent=?, status=? WHERE id=? AND status='pending'",
            (
                value.timestamp,
                volume,
                change_percent(row["initial_queue_volume"], volume),
                status,
                row["id"],
            ),
        )
        self.logger.info(
            "Outcome observation recorded",
            extra={"status": status, "window_minutes": row["window_minutes"]},
        )

    def on_snapshot(self, value: QueueSnapshot, session: str) -> None:
        now = self.now()
        if (
            not 0 <= now - value.timestamp <= self.max_age_ms
            or not self.snapshots.has_alert_snapshot(value, session)
        ):
            return
        for row in rows(
            self.db,
            "SELECT * FROM alert_outcomes WHERE status='pending' AND ins_code=? AND target_at<=? AND cutoff_at>=? AND session_id=?",
            (value.ins_code, value.timestamp, value.timestamp, session),
        ):
            if self.eligible(row):
                self._observe(row, value)

    def monitored_symbols(self) -> list[Instrument]:
        """Keep pending instruments in the existing collector's deduplicated union."""
        return [
            Instrument(row["ins_code"], row["symbol"], row["symbol"])
            for row in rows(
                self.db,
                "SELECT DISTINCT o.ins_code, s.symbol, o.owner_id, o.channel_id FROM alert_outcomes o JOIN symbols s ON s.ins_code=o.ins_code WHERE o.status='pending'",
            )
            if self.eligible(row)
        ]

    def advance(self) -> None:
        now = self.now()
        # Due rows only; indexed and bounded so offline periods cannot stall polling.
        for row in rows(
            self.db,
            "SELECT * FROM alert_outcomes WHERE status='pending' AND target_at<=? ORDER BY target_at, id LIMIT 1000",
            (now,),
        ):
            if not self.eligible(row):
                self.db.execute(
                    "UPDATE alert_outcomes SET status='expired' WHERE id=?",
                    (row["id"],),
                )
                continue
            found = rows(
                self.db,
                "SELECT * FROM queue_snapshots WHERE ins_code=? AND session_id=? AND timestamp BETWEEN ? AND ? AND collected_at BETWEEN timestamp AND timestamp+? AND symbol IS NOT NULL ORDER BY timestamp LIMIT 1",
                (
                    row["ins_code"],
                    row["session_id"],
                    row["target_at"],
                    min(now, row["cutoff_at"]),
                    self.max_age_ms,
                ),
            )
            if found:
                s = found[0]
                # save() only accepts detector-approved observations in this pipeline.
                self._observe(
                    row,
                    QueueSnapshot(
                        s["ins_code"],
                        s["symbol"],
                        s["timestamp"],
                        s["queue_type"],
                        s["volume"],
                        s["price"],
                        s["order_count"],
                    ),
                )
            elif (
                now > row["cutoff_at"]
                or self.schedule.session_id(now) != row["session_id"]
            ):
                self.db.execute(
                    "UPDATE alert_outcomes SET status='expired' WHERE id=?",
                    (row["id"],),
                )
                self.logger.info(
                    "Outcome observation unavailable",
                    extra={"window_minutes": row["window_minutes"]},
                )

    def delete_expired(self, now: int) -> int:
        cutoff = max(0, now - self.retention_ms)
        removed = self.db.execute(
            "DELETE FROM alert_outcomes WHERE id IN (SELECT id FROM alert_outcomes WHERE target_at<? ORDER BY target_at LIMIT 1000)",
            (cutoff,),
        ).rowcount
        removed += self.db.execute(
            "DELETE FROM alert_suppressions WHERE (rule_id, day_at) IN (SELECT rule_id, day_at FROM alert_suppressions WHERE day_at<? ORDER BY day_at LIMIT ?)",
            (cutoff, 1000 - removed),
        ).rowcount
        return removed
