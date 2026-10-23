"""Port of src/alerts/evaluators.ts; no I/O or persisted state.

Snapshots must come from the future verified detector/repository pipeline.
Structural validation alone does not establish freshness or market verification.
Session selection, history tolerance, cooldown and priming belong to the engine.
"""

from dataclasses import dataclass
from math import isfinite
from typing import Any, Literal

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
    window_minutes: Literal[1, 5, 15]
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
        if type(self.window_minutes) is not int or self.window_minutes not in (
            1,
            5,
            15,
        ):
            raise ValueError("Window must be one, five or fifteen minutes")
        _quantity(self.minimum_baseline_volume)


@dataclass(frozen=True, slots=True)
class DisappearedConfig:
    minimum_previous_volume: int

    def __post_init__(self) -> None:
        _quantity(self.minimum_previous_volume)


@dataclass(frozen=True, slots=True)
class ThresholdConfig:
    threshold_volume: int
    hysteresis_percent: float = 5

    def __post_init__(self) -> None:
        _quantity(self.threshold_volume)
        if self.threshold_volume == 0:
            raise ValueError("Threshold must be positive")
        _hysteresis(self.hysteresis_percent)


def _hysteresis(value: float) -> None:
    if type(value) not in (int, float) or not isfinite(value) or not 0 <= value <= 100:
        raise ValueError("Hysteresis must be between zero and 100")
    if round(value, 6) != value:
        raise ValueError("At most six decimals")


@dataclass(frozen=True, slots=True)
class PeakConfig:
    threshold_percent: float
    minimum_peak_volume: int = 0
    hysteresis_percent: float = 5

    def __post_init__(self) -> None:
        DropConfig(self.threshold_percent, 1, self.minimum_peak_volume)
        _hysteresis(self.hysteresis_percent)


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    status: Literal["INSUFFICIENT_DATA", "CLEAR", "TRIGGER"]
    previous_volume: int | None = None
    current_volume: int | None = None
    actual_percent: float | None = None
    window_minutes: int | None = None
    details: dict[str, Any] | None = None


def queue_threshold(
    side: Side,
    config: ThresholdConfig,
    below: bool,
    current: QueueSnapshot,
    previous: QueueSnapshot | None,
) -> EvaluationResult:
    if current.queue_type not in (side, "none"):
        return EvaluationResult("INSUFFICIENT_DATA")
    hit = (
        current.volume <= config.threshold_volume
        if below
        else current.volume >= config.threshold_volume
    )
    if not hit:
        return EvaluationResult("CLEAR")
    if (
        previous is None
        or previous.queue_type not in (side, "none")
        or previous.timestamp >= current.timestamp
        or previous.ins_code != current.ins_code
    ):
        return EvaluationResult("INSUFFICIENT_DATA")
    return EvaluationResult("TRIGGER", previous.volume, current.volume, 0)


def threshold_recovered(
    side: Side, config: ThresholdConfig, below: bool, current: QueueSnapshot
) -> bool:
    if current.queue_type not in (side, "none"):
        return False
    scale = 100_000_000
    margin = round(config.hysteresis_percent * 1_000_000)
    return (
        current.volume * scale > config.threshold_volume * (scale + margin)
        if below
        else current.volume * scale < config.threshold_volume * (scale - margin)
    )


@dataclass(frozen=True, slots=True)
class ObservedPeak:
    volume: int
    timestamp: int


def queue_peak_drop(
    side: Side, config: PeakConfig, current: QueueSnapshot, peak: ObservedPeak | None
) -> EvaluationResult:
    if (
        peak is None
        or peak.volume <= 0
        or peak.volume < config.minimum_peak_volume
        or current.queue_type not in (side, "none")
        or peak.timestamp > current.timestamp
    ):
        return EvaluationResult("INSUFFICIENT_DATA")
    lost = peak.volume - current.volume
    if lost <= 0 or lost * 100_000_000 < peak.volume * round(
        config.threshold_percent * 1_000_000
    ):
        return EvaluationResult("CLEAR")
    return EvaluationResult(
        "TRIGGER", peak.volume, current.volume, lost / peak.volume * 100
    )


def peak_recovered(
    side: Side, config: PeakConfig, current: QueueSnapshot, baseline: int | None
) -> bool:
    return (
        current.queue_type == side
        and baseline is not None
        and baseline > 0
        and current.volume * 100_000_000
        >= baseline
        * round(
            (100 - max(0, config.threshold_percent - config.hysteresis_percent))
            * 1_000_000
        )
    )


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
