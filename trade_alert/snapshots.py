"""Accepted snapshots only; structural validity does not verify market evidence.

The collector must call save only after detector acceptance, and evaluate only
when save returns True. Source time, receipt time and session stay independent.
"""

import sqlite3
from dataclasses import dataclass
from typing import Any

from .database import rows, transaction
from .evaluators import ObservedPeak, QueueSnapshot
from .validation import code, integer, text


@dataclass(frozen=True, slots=True)
class StoredSnapshot(QueueSnapshot):
    collected_at: int


def _snapshot(row: dict[str, Any]) -> StoredSnapshot:
    return StoredSnapshot(
        row["ins_code"],
        row["symbol"],
        row["timestamp"],
        row["queue_type"],
        row["volume"],
        row["price"],
        row["order_count"],
        integer(row["collected_at"]),
    )


class SnapshotRepository:
    def __init__(self, db: sqlite3.Connection, tolerance_ms: int = 20_000) -> None:
        self.db = db
        self.tolerance_ms = integer(tolerance_ms)

    def observe_peaks(
        self, value: QueueSnapshot, session_id: str
    ) -> dict[str, ObservedPeak]:
        """Called by the fresh-evidence engine only; no claim of full-session coverage.

        One indexed row per instrument/session survives history retention and restart.
        Opposite-side observations never erase either side's highest observed volume.
        """
        buy = value.volume if value.queue_type == "buy" else 0
        sell = value.volume if value.queue_type == "sell" else 0
        self.db.execute(
            "INSERT INTO queue_session_peaks(ins_code, session_id, first_observed_at, last_observed_at, buy_volume, buy_timestamp, sell_volume, sell_timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(ins_code, session_id) DO UPDATE SET last_observed_at=excluded.last_observed_at, buy_volume=max(buy_volume, excluded.buy_volume), buy_timestamp=CASE WHEN excluded.buy_volume>buy_volume THEN excluded.buy_timestamp ELSE buy_timestamp END, sell_volume=max(sell_volume, excluded.sell_volume), sell_timestamp=CASE WHEN excluded.sell_volume>sell_volume THEN excluded.sell_timestamp ELSE sell_timestamp END WHERE excluded.last_observed_at>last_observed_at",
            (
                value.ins_code,
                session_id,
                value.timestamp,
                value.timestamp,
                buy,
                value.timestamp if buy else None,
                sell,
                value.timestamp if sell else None,
            ),
        )
        row = rows(
            self.db,
            "SELECT * FROM queue_session_peaks WHERE ins_code=? AND session_id=?",
            (value.ins_code, session_id),
        )[0]
        result: dict[str, ObservedPeak] = {}
        for side in ("buy", "sell"):
            if row[f"{side}_timestamp"] is not None:
                result[side] = ObservedPeak(
                    row[f"{side}_volume"], row[f"{side}_timestamp"]
                )
        return result

    def save(
        self, value: QueueSnapshot, collected_at: int, session_id: str | None = None
    ) -> bool:
        if not isinstance(value, QueueSnapshot):
            raise ValueError("Expected a validated snapshot")
        collected_at = integer(collected_at)
        if collected_at < value.timestamp:
            raise ValueError("Collection precedes source timestamp")
        if session_id is not None:
            text(session_id, 2**53 - 1, trim=False)
        with transaction(self.db):
            existing = rows(
                self.db,
                "SELECT * FROM queue_snapshots WHERE ins_code=? AND timestamp=?",
                (value.ins_code, value.timestamp),
            )
            if existing:
                row = existing[0]
                if row["collected_at"] is None:
                    raise ValueError("Source timestamp collides with legacy history")
                if any(
                    row[key] != getattr(value, key)
                    for key in ("queue_type", "volume", "price", "order_count")
                ):
                    raise ValueError("Conflicting update at the same source timestamp")
                return False
            self.db.execute(
                "INSERT INTO queue_snapshots (ins_code, symbol, timestamp, queue_type, volume, price, order_count, collected_at, session_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    value.ins_code,
                    value.symbol,
                    value.timestamp,
                    value.queue_type,
                    value.volume,
                    value.price,
                    value.order_count,
                    collected_at,
                    session_id,
                ),
            )
        return True

    def alert_baseline(
        self, ins_code: str, at: int, session_id: str
    ) -> QueueSnapshot | None:
        code(ins_code)
        # A window can precede the Unix epoch; no nonnegative source can match.
        if at < 0:
            return None
        integer(at)
        text(session_id, 2**53 - 1, trim=False)
        found = rows(
            self.db,
            "SELECT * FROM queue_snapshots WHERE ins_code=? AND session_id=? AND timestamp BETWEEN ? AND ? AND collected_at IS NOT NULL AND symbol IS NOT NULL ORDER BY timestamp DESC LIMIT 1",
            (ins_code, session_id, max(0, at - self.tolerance_ms), at),
        )
        try:
            return _snapshot(found[0]) if found else None
        except ValueError, TypeError:
            return None

    def has_alert_snapshot(self, value: QueueSnapshot, session_id: str) -> bool:
        found = rows(
            self.db,
            "SELECT * FROM queue_snapshots WHERE ins_code=? AND timestamp=? AND session_id=? AND collected_at IS NOT NULL",
            (value.ins_code, value.timestamp, session_id),
        )
        return bool(found) and all(
            found[0][key] == getattr(value, key)
            for key in ("symbol", "queue_type", "volume", "price", "order_count")
        )

    def get_latest_snapshot(self, ins_code: str) -> StoredSnapshot | None:
        code(ins_code)
        found = rows(
            self.db,
            "SELECT * FROM queue_snapshots WHERE ins_code=? AND collected_at IS NOT NULL AND symbol IS NOT NULL ORDER BY timestamp DESC LIMIT 1",
            (ins_code,),
        )
        return _snapshot(found[0]) if found else None

    def get_snapshot_at_or_before(
        self, ins_code: str, at: int
    ) -> StoredSnapshot | None:
        code(ins_code)
        at = integer(at)
        found = rows(
            self.db,
            "SELECT * FROM queue_snapshots WHERE ins_code=? AND timestamp BETWEEN ? AND ? AND collected_at IS NOT NULL AND symbol IS NOT NULL ORDER BY timestamp DESC LIMIT 1",
            (ins_code, max(0, at - self.tolerance_ms), at),
        )
        return _snapshot(found[0]) if found else None

    def get_snapshots_between(
        self, ins_code: str, start: int, end: int
    ) -> list[StoredSnapshot]:
        code(ins_code)
        start, end = integer(start), integer(end)
        if start > end:
            raise ValueError("Invalid history range")
        return [
            _snapshot(row)
            for row in rows(
                self.db,
                "SELECT * FROM queue_snapshots WHERE ins_code=? AND timestamp BETWEEN ? AND ? AND collected_at IS NOT NULL AND symbol IS NOT NULL ORDER BY timestamp",
                (ins_code, start, end),
            )
        ]

    def delete_expired(self, cutoff: int, batch_size: int = 1000) -> int:
        cutoff, batch_size = integer(cutoff), integer(batch_size, 1, 1000)
        return self.db.execute(
            "DELETE FROM queue_snapshots WHERE id IN (SELECT id FROM queue_snapshots WHERE timestamp < ? ORDER BY timestamp LIMIT ?)",
            (cutoff, batch_size),
        ).rowcount

    def delete_expired_peaks(self, cutoff: int) -> int:
        return self.db.execute(
            "DELETE FROM queue_session_peaks WHERE rowid IN (SELECT rowid FROM queue_session_peaks WHERE last_observed_at<? ORDER BY last_observed_at LIMIT 1000)",
            (integer(cutoff),),
        ).rowcount
