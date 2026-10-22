"""Port of src/alerts/evaluators.ts; no I/O or persisted state.

Snapshots must come from the future verified detector/repository pipeline.
Structural validation alone does not establish freshness or market verification.
Session selection, history tolerance, cooldown and priming belong to the engine.
"""

from dataclasses import dataclass
from math import isfinite
from typing import Literal

from .validation import MAX_SAFE_INTEGER as MAX_SAFE_INTEGER
from .validation import code, text

Side = Literal["buy", "sell"]


def _quantity(value: int) -> None:
    if type(value) is not int or not 0 <= value <= MAX_SAFE_INTEGER:
        raise ValueError("Expected a nonnegative safe integer")


def _side(value: Side) -> None:
    if value not in ("buy", "sell"):
        raise ValueError("Expected buy or sell")


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    ins_code: str
    symbol: str
    timestamp: int
    queue_type: Literal["buy", "sell", "none"]
    volume: int
    price: float | None
    order_count: int

    def __post_init__(self) -> None:
        code(self.ins_code)
        object.__setattr__(self, "symbol", text(self.symbol, 80))
        for value in (self.timestamp, self.volume, self.order_count):
            _quantity(value)
        if self.queue_type == "none":
            if self.volume != 0 or self.order_count != 0 or self.price is not None:
                raise ValueError("A confirmed none snapshot must have zero quantities")
        else:
            _side(self.queue_type)
            if (
                not 0 < self.order_count <= self.volume
                or not isinstance(self.price, (int, float))
                or isinstance(self.price, bool)
                or not isfinite(self.price)
                or self.price <= 0
            ):
                raise ValueError("Invalid queue quantities or price")


@dataclass(frozen=True, slots=True)
class DropConfig:
    threshold_percent: float
    window_minutes: Literal[1, 5]
    minimum_baseline_volume: int

    def __post_init__(self) -> None:
        value = self.threshold_percent
        if (
            type(value) not in (int, float)
            or not isfinite(value)
            or not 0 < value <= 100
            or round(value, 6) != value
        ):
            raise ValueError("Percentage must be in (0, 100] with at most six decimals")
        if type(self.window_minutes) is not int or self.window_minutes not in (1, 5):
            raise ValueError("Window must be one or five minutes")
        _quantity(self.minimum_baseline_volume)


@dataclass(frozen=True, slots=True)
class DisappearedConfig:
    minimum_previous_volume: int

    def __post_init__(self) -> None:
        _quantity(self.minimum_previous_volume)


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    status: Literal["INSUFFICIENT_DATA", "CLEAR", "TRIGGER"]
    previous_volume: int | None = None
    current_volume: int | None = None
    actual_percent: float | None = None
    window_minutes: int | None = None


def queue_drop(
    side: Side,
    config: DropConfig,
    current: QueueSnapshot,
    baseline: QueueSnapshot | None,
) -> EvaluationResult:
    _side(side)
    if (
        baseline is None
        or baseline.ins_code != current.ins_code
        or baseline.timestamp >= current.timestamp
        or baseline.queue_type != side
        or current.queue_type not in (side, "none")
        or baseline.volume <= 0
        or baseline.volume < config.minimum_baseline_volume
    ):
        return EvaluationResult("INSUFFICIENT_DATA")
    lost = baseline.volume - current.volume
    if lost <= 0 or lost * 100_000_000 < baseline.volume * round(
        config.threshold_percent * 1_000_000
    ):
        return EvaluationResult("CLEAR")
    return EvaluationResult(
        "TRIGGER",
        baseline.volume,
        current.volume,
        lost / baseline.volume * 100,
        config.window_minutes,
    )


def queue_disappeared(
    side: Side,
    config: DisappearedConfig,
    current: QueueSnapshot,
    previous: QueueSnapshot | None,
) -> EvaluationResult:
    _side(side)
    if (
        previous is None
        or previous.ins_code != current.ins_code
        or previous.timestamp >= current.timestamp
        or previous.queue_type != side
        or previous.volume <= 0
        or previous.volume < config.minimum_previous_volume
    ):
        return EvaluationResult("INSUFFICIENT_DATA")
    if current.queue_type == side:
        return EvaluationResult("CLEAR")
    if current.queue_type != "none":
        return EvaluationResult("INSUFFICIENT_DATA")
    return EvaluationResult("TRIGGER", previous.volume, 0, 100, None)


def drop_recovered(
    side: Side, config: DropConfig, current: QueueSnapshot, baseline: int | None
) -> bool:
    _side(side)
    if baseline is not None:
        _quantity(baseline)
    return (
        current.queue_type == side
        and baseline is not None
        and current.volume * 100_000_000
        >= baseline * round((100 - max(0, config.threshold_percent - 5)) * 1_000_000)
    )


def disappeared_recovered(
    side: Side, config: DisappearedConfig, current: QueueSnapshot
) -> bool:
    _side(side)
    return current.queue_type == side and current.volume >= max(
        1, config.minimum_previous_volume
    )
