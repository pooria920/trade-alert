"""Normalized-book validation and detection. No transport or implicit verification."""

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from math import inf
from typing import Literal
from zoneinfo import ZoneInfo

from .evaluators import MAX_SAFE_INTEGER, QueueSnapshot
from .validation import choice, code, integer, number, obj, text

TEHRAN = ZoneInfo("Asia/Tehran")


def tehran_date(timestamp: int) -> str:
    return (
        datetime.fromtimestamp(timestamp / 1000, timezone.utc)
        .astimezone(TEHRAN)
        .strftime("%Y%m%d")
    )


@dataclass(frozen=True, slots=True)
class Instrument:
    ins_code: str
    symbol: str
    name: str


@dataclass(frozen=True, slots=True)
class Order:
    price: float
    volume: int
    order_count: int


@dataclass(frozen=True, slots=True)
class Level:
    rank: int
    bid: Order
    ask: Order


@dataclass(frozen=True, slots=True)
class Limits:
    ins_code: str
    date: str
    upper: float
    lower: float


@dataclass(frozen=True, slots=True)
class Book:
    instrument: Instrument
    timestamp: int | None
    received_at: int
    trading_status: str
    completeness: str
    verified: bool
    levels: tuple[Level, ...]
    limits: Limits | None


def parse_instrument(value: object) -> Instrument:
    v = obj(value)
    return Instrument(code(v["insCode"]), text(v["symbol"], 80), text(v["name"], 200))


def parse_book(value: object) -> Book:
    v = obj(value)
    instrument = parse_instrument(v["instrument"])
    timestamp = None if v["timestamp"] is None else integer(v["timestamp"])
    received_at = integer(v["receivedAt"])
    trading_status = choice(v["tradingStatus"], ("open", "closed", "unknown"))
    completeness = choice(v["completeness"], ("complete", "incremental", "unknown"))
    if type(v["verified"]) is not bool:
        raise ValueError("Expected verified boolean")
    if not isinstance(v["levels"], list) or len(v["levels"]) > 100:
        raise ValueError("Invalid levels")

    def order(value: object) -> Order:
        side = obj(value)
        return Order(
            number(side["price"]), integer(side["volume"]), integer(side["orderCount"])
        )

    levels = []
    for value in v["levels"]:
        level = obj(value)
        levels.append(
            Level(
                integer(level["rank"], 1, 100), order(level["bid"]), order(level["ask"])
            )
        )
    limits = None
    if v["limits"] is not None:
        limit = obj(v["limits"])
        date = limit["date"]
        if not isinstance(date, str) or not re.fullmatch(r"[0-9]{8}", date):
            raise ValueError("Invalid limits date")
        upper, lower = number(limit["upper"]), number(limit["lower"])
        if not upper > lower > 0:
            raise ValueError("Invalid price boundaries")
        limits = Limits(code(limit["insCode"]), date, upper, lower)
    return Book(
        instrument,
        timestamp,
        received_at,
        trading_status,
        completeness,
        v["verified"],
        tuple(levels),
        limits,
    )


@dataclass(frozen=True, slots=True)
class Detection:
    valid: bool
    snapshot: QueueSnapshot | None = None
    reason: str | None = None


class QueueDetector:
    def __init__(self, max_age_ms: int = 30_000) -> None:
        self.max_age_ms = integer(max_age_ms)

    def detect(self, value: object, now: int | None = None) -> Detection:
        now = time.time_ns() // 1_000_000 if now is None else integer(now)
        try:
            book = parse_book(value)
        except ValueError, TypeError, KeyError:
            return Detection(False, reason="invalid-data")
        if not book.verified or book.completeness != "complete":
            return Detection(False, reason="unverified-book")
        if (
            book.timestamp is None
            or book.timestamp > now
            or now - book.timestamp > self.max_age_ms
            or book.received_at > now
            or book.received_at < book.timestamp
            or now - book.received_at > self.max_age_ms
        ):
            return Detection(False, reason="stale-data")
        if book.trading_status != "open":
            return Detection(False, reason="trading-state")
        limits = book.limits
        try:
            valid_limits = (
                limits is not None
                and limits.ins_code == book.instrument.ins_code
                and limits.date == tehran_date(book.timestamp)
                and limits.date == tehran_date(now)
            )
        except ValueError, OverflowError, OSError:
            valid_limits = False
        if not valid_limits or limits is None:
            return Detection(False, reason="invalid-limits")
        ranks: set[int] = set()
        best_bid, best_ask, previous_bid, previous_ask = 0.0, inf, inf, 0.0
        for level in sorted(book.levels, key=lambda level: level.rank):
            if level.rank in ranks:
                return Detection(False, reason="duplicate-level")
            ranks.add(level.rank)
            for side in (level.bid, level.ask):
                empty = side.volume == 0 and side.order_count == 0
                if not empty and (
                    side.volume <= 0
                    or side.order_count <= 0
                    or side.order_count > side.volume
                    or side.price < limits.lower
                    or side.price > limits.upper
                ):
                    return Detection(False, reason="inconsistent-order")
            if (level.bid.volume and level.bid.price >= previous_bid) or (
                level.ask.volume and level.ask.price <= previous_ask
            ):
                return Detection(False, reason="inconsistent-level-order")
            previous_bid = level.bid.price if level.bid.volume else 0
            previous_ask = level.ask.price if level.ask.volume else inf
            if level.bid.volume:
                best_bid = max(best_bid, level.bid.price)
            if level.ask.volume:
                best_ask = min(best_ask, level.ask.price)
        if ranks and max(ranks) != len(ranks):
            return Detection(False, reason="missing-level")
        if best_bid >= best_ask:
            return Detection(False, reason="crossed-book")
        queue_type: Literal["buy", "sell", "none"] = (
            "buy"
            if best_bid == limits.upper
            else "sell"
            if best_ask == limits.lower
            else "none"
        )
        price = (
            limits.upper
            if queue_type == "buy"
            else limits.lower
            if queue_type == "sell"
            else None
        )
        volume = order_count = 0
        if queue_type != "none":
            for level in book.levels:
                side = level.bid if queue_type == "buy" else level.ask
                if side.price == price and side.volume > 0:
                    volume += side.volume
                    order_count += side.order_count
        if volume > MAX_SAFE_INTEGER or order_count > MAX_SAFE_INTEGER:
            return Detection(False, reason="unsafe-quantity")
        return Detection(
            True,
            QueueSnapshot(
                book.instrument.ins_code,
                book.instrument.symbol,
                book.timestamp,
                queue_type,
                volume,
                price,
                order_count,
            ),
        )
