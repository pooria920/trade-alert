"""Explicit Tehran sessions; an empty schedule is closed."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone

from .market import TEHRAN
from .validation import integer, obj, text


@dataclass(frozen=True, slots=True)
class Session:
    market: str
    days: tuple[int, ...]
    start: str
    end: str


def calendar_date(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value
    ):
        raise ValueError("Invalid calendar date")
    date.fromisoformat(value)
    return value


def parse_sessions(value: object) -> tuple[Session, ...]:
    if not isinstance(value, list):
        raise ValueError("Expected sessions array")
    result = []
    for raw in value:
        v = obj(raw)
        market = text(v.get("market", "equities"), 50)
        if not isinstance(v["days"], list) or not v["days"]:
            raise ValueError("Expected weekdays")
        days = tuple(integer(day, 1, 7) for day in v["days"])
        start, end = v["start"], v["end"]
        for clock in (start, end):
            if not isinstance(clock, str) or not re.fullmatch(
                r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", clock
            ):
                raise ValueError("Invalid session time")
        if start >= end:
            raise ValueError("Use separate sessions for overnight hours")
        result.append(Session(market, days, start, end))
    return tuple(result)


class MarketSchedule:
    def __init__(
        self,
        sessions: Sequence[Session],
        closed_dates: Sequence[str] = (),
        force_active: bool = False,
        market: str = "equities",
    ) -> None:
        self.sessions = tuple(sessions)
        self.closed_dates = frozenset(calendar_date(d) for d in closed_dates)
        self.force_active = force_active
        self.market = market

    @property
    def configured(self) -> bool:
        return any(s.market == self.market for s in self.sessions)

    def session_id(self, timestamp: int) -> str | None:
        local = datetime.fromtimestamp(timestamp / 1000, timezone.utc).astimezone(
            TEHRAN
        )
        day = local.date().isoformat()
        if self.force_active:
            return f"{self.market}:{day}:forced"
        if day in self.closed_dates:
            return None
        clock = local.strftime("%H:%M")
        for s in self.sessions:
            if (
                s.market == self.market
                and local.isoweekday() in s.days
                and s.start <= clock < s.end
            ):
                return f"{self.market}:{day}:{s.start}:{s.end}"
        return None

    def is_open(self, timestamp: int) -> bool:
        return self.session_id(timestamp) is not None
