"""Pure rule-tree evaluation; history must already be session/tolerance scoped."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from .definitions import (
    Condition,
    Group,
    RuleTree,
    VolumeConfig,
    definition_json,
    parse_tree,
)
from .evaluators import (
    DisappearedConfig,
    DropConfig,
    EvaluationResult,
    ObservedPeak,
    PeakConfig,
    QueueSnapshot,
    Side,
    ThresholdConfig,
    disappeared_recovered,
    drop_recovered,
    peak_recovered,
    queue_disappeared,
    queue_drop,
    queue_peak_drop,
    queue_threshold,
    threshold_recovered,
)
from .lifecycle import (
    LifecycleConfig,
    LifecycleContext,
    evaluate_lifecycle,
    event_kind,
    validate_event,
)
from .validation import integer, loads, number, obj, text

type Truth = Literal["TRUE", "FALSE", "UNKNOWN"]
type History = Callable[[int], QueueSnapshot | None]


@dataclass(frozen=True, slots=True)
class Strategy:
    evaluate: Callable[
        [QueueSnapshot, QueueSnapshot | None, QueueSnapshot | None], EvaluationResult
    ]
    recovered: Callable[[QueueSnapshot, int | None], bool]
    window_ms: int | None


def _drop(side: Side, condition: Condition) -> Strategy:
    config = condition.config
    if not isinstance(config, DropConfig):
        raise ValueError("Invalid drop configuration")
    return Strategy(
        lambda current, baseline, previous: queue_drop(side, config, current, baseline),
        lambda current, baseline: drop_recovered(side, config, current, baseline),
        config.window_minutes * 60_000,
    )


def _disappeared(side: Side, condition: Condition) -> Strategy:
    config = condition.config
    if not isinstance(config, DisappearedConfig):
        raise ValueError("Invalid disappearance configuration")
    return Strategy(
        lambda current, baseline, previous: queue_disappeared(
            side, config, current, previous
        ),
        lambda current, baseline: disappeared_recovered(side, config, current),
        None,
    )


def _peak(side: Side, condition: Condition) -> Strategy:
    config = condition.config
    if not isinstance(config, PeakConfig):
        raise ValueError("Invalid peak configuration")
    return Strategy(
        lambda current, baseline, previous: queue_peak_drop(
            side,
            config,
            current,
            ObservedPeak(baseline.volume, baseline.timestamp) if baseline else None,
        ),
        lambda current, baseline: peak_recovered(side, config, current, baseline),
        None,
    )


def _threshold(side: Side, condition: Condition) -> Strategy:
    config = condition.config
    if not isinstance(config, ThresholdConfig):
        raise ValueError("Invalid threshold configuration")
    below = condition.type.endswith("BELOW")
    return Strategy(
        lambda current, baseline, previous: queue_threshold(
            side, config, below, current, previous
        ),
        lambda current, baseline: threshold_recovered(side, config, below, current),
        None,
    )


REGISTRY: dict[str, Callable[[Condition], Strategy]] = {
    "BUY_QUEUE_DROP_PERCENT": lambda c: _drop("buy", c),
    "SELL_QUEUE_DROP_PERCENT": lambda c: _drop("sell", c),
    "BUY_QUEUE_DISAPPEARED": lambda c: _disappeared("buy", c),
    "SELL_QUEUE_DISAPPEARED": lambda c: _disappeared("sell", c),
    "BUY_QUEUE_PEAK_DROP_PERCENT": lambda c: _peak("buy", c),
    "SELL_QUEUE_PEAK_DROP_PERCENT": lambda c: _peak("sell", c),
    "BUY_QUEUE_VOLUME_BELOW": lambda c: _threshold("buy", c),
    "SELL_QUEUE_VOLUME_BELOW": lambda c: _threshold("sell", c),
    "BUY_QUEUE_VOLUME_ABOVE": lambda c: _threshold("buy", c),
    "SELL_QUEUE_VOLUME_ABOVE": lambda c: _threshold("sell", c),
}


def strategy_for(condition: Condition) -> Strategy:
    return REGISTRY[condition.type](condition)


def ready_thresholds(
    tree: RuleTree, current: QueueSnapshot, ready: dict[str, bool]
) -> None:
    """A threshold must observe its recovery side after creation/edit before firing."""

    def visit(node: RuleTree, path: str) -> None:
        if isinstance(node, Group):
            for index, child in enumerate(node.conditions):
                visit(child, f"{path}.{index}")
        elif isinstance(node.config, ThresholdConfig):
            ready[path] = ready.get(path, False) or strategy_for(node).recovered(
                current, None
            )

    visit(tree, "0")


def combine(operator: Literal["AND", "OR"], values: Sequence[Truth]) -> Truth:
    if operator == "AND":
        return (
            "FALSE"
            if "FALSE" in values
            else "UNKNOWN"
            if "UNKNOWN" in values
            else "TRUE"
        )
    if operator == "OR":
        return (
            "TRUE"
            if "TRUE" in values
            else "UNKNOWN"
            if "UNKNOWN" in values
            else "FALSE"
        )
    raise ValueError("Invalid operator")


def leaf(
    condition: Condition,
    current: QueueSnapshot,
    history: History,
    peaks: dict[str, ObservedPeak] | None = None,
    lifecycle: LifecycleContext | None = None,
) -> EvaluationResult:
    if isinstance(condition.config, LifecycleConfig):
        return evaluate_lifecycle(condition.type, condition.config, current, lifecycle)
    if isinstance(condition.config, PeakConfig):
        side: Side = "buy" if condition.type.startswith("BUY_") else "sell"
        return queue_peak_drop(side, condition.config, current, (peaks or {}).get(side))
    if isinstance(condition.config, VolumeConfig):
        side = "buy" if condition.type == "BUY_QUEUE_VOLUME_ABOVE" else "sell"
        if current.queue_type not in (side, "none"):
            return EvaluationResult("INSUFFICIENT_DATA")
        if current.volume >= condition.config.minimum_volume:
            return EvaluationResult("TRIGGER", current.volume, current.volume, 0, None)
        return EvaluationResult("CLEAR")
    strategy = strategy_for(condition)
    baseline = (
        None
        if strategy.window_ms is None
        else history(current.timestamp - strategy.window_ms)
    )
    return strategy.evaluate(current, baseline, history(current.timestamp - 1))


def recovered(condition: Condition, current: QueueSnapshot, baseline: int) -> Truth:
    side = "buy" if condition.type.startswith("BUY_") else "sell"
    if isinstance(condition.config, VolumeConfig):
        if current.queue_type not in (side, "none"):
            return "UNKNOWN"
        return (
            "FALSE"
            if current.volume * 100 < condition.config.minimum_volume * 95
            else "TRUE"
        )
    if strategy_for(condition).recovered(current, baseline):
        return "FALSE"
    return "TRUE" if current.queue_type in (side, "none") else "UNKNOWN"


@dataclass(frozen=True, slots=True)
class Evidence:
    path: str
    condition: Condition
    previous_volume: int
    current_volume: int
    actual_percent: float
    window_minutes: float | None
    details: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        value = dict(
            status="TRIGGER",
            path=self.path,
            condition=definition_json(self.condition),
            previousVolume=self.previous_volume,
            currentVolume=self.current_volume,
            actualPercent=self.actual_percent,
            windowMinutes=self.window_minutes,
        )
        if self.details is not None:
            value["lifecycle"] = self.details
        return value


def parse_evidence(value: str | None) -> tuple[Evidence, ...]:
    values = loads(value)
    if not isinstance(values, list) or len(values) > 8:
        raise ValueError("Invalid evidence list")
    result = []
    for value in values:
        v = obj(value)
        condition = parse_tree(v["condition"])
        if not isinstance(condition, Condition):
            raise ValueError("Expected a leaf condition")
        details = (
            validate_event(v["lifecycle"])
            if isinstance(condition.config, LifecycleConfig)
            else None
        )
        if details is not None and (
            details["event_type"] != event_kind(condition.type)
            or details["side"]
            != ("buy" if condition.type.startswith("BUY_") else "sell")
        ):
            raise ValueError("Mismatched lifecycle evidence")
        result.append(
            Evidence(
                text(v["path"], 2**53 - 1, trim=False, minimum=0),
                condition,
                integer(v["previousVolume"]),
                integer(v["currentVolume"]),
                number(v["actualPercent"], 0, 100),
                None
                if v["windowMinutes"] is None
                else number(v["windowMinutes"], -float("inf")),
                details,
            )
        )
    return tuple(result)


@dataclass(frozen=True, slots=True)
class TreeResult:
    truth: Truth
    matches: tuple[Evidence, ...]


def evaluate_tree(
    tree: RuleTree,
    current: QueueSnapshot,
    history: History,
    latched: Sequence[Evidence] = (),
    *,
    frozen_baselines: bool = False,
    peaks: dict[str, ObservedPeak] | None = None,
    threshold_ready: dict[str, bool] | None = None,
    lifecycle: LifecycleContext | None = None,
) -> TreeResult:
    matches = []

    def visit(node: RuleTree, path: str) -> Truth:
        if isinstance(node, Group):
            return combine(
                node.operator,
                [
                    visit(child, f"{path}.{index}")
                    for index, child in enumerate(node.conditions)
                ],
            )
        saved = next((item for item in latched if item.path == path), None)
        if isinstance(node.config, LifecycleConfig) and saved is not None:
            side: Side = "buy" if node.type.startswith("BUY_") else "sell"
            if lifecycle is None or side not in lifecycle.valid_sides:
                return "UNKNOWN"
            if node.type.endswith("WEAKENED"):
                if lifecycle.weak_occurrences.get(side) != (saved.details or {}).get(
                    "occurrence_at"
                ):
                    return "FALSE"
                if current.queue_type == "none":
                    return "UNKNOWN"
                if not frozen_baselines:
                    return (
                        "FALSE"
                        if peak_recovered(
                            side,
                            PeakConfig(node.config.threshold_percent),
                            current,
                            saved.previous_volume,
                        )
                        else "TRUE"
                    )
                if (
                    current.queue_type not in (side, "none")
                    or saved.previous_volume <= 0
                ):
                    return "UNKNOWN"
                percent = (
                    (saved.previous_volume - current.volume)
                    / saved.previous_volume
                    * 100
                )
                if percent < node.config.threshold_percent:
                    return "FALSE"
                matches.append(
                    Evidence(
                        path,
                        node,
                        saved.previous_volume,
                        current.volume,
                        percent,
                        None,
                        saved.details,
                    )
                )
                return "TRUE"
            if not frozen_baselines:
                # A later transition is a new pulse, even when its kind matches.
                return (
                    "TRUE"
                    if any(
                        e["id"] == (saved.details or {}).get("id")
                        and e["timestamp"] == current.timestamp
                        for e in lifecycle.events
                    )
                    else "FALSE"
                )
            # Earlier pulses cannot satisfy a composite escalation on a later update.
            if (saved.details or {}).get("timestamp") != current.timestamp:
                return "FALSE"
        if saved is not None and not frozen_baselines:
            return recovered(node, current, saved.previous_volume)
        if saved is not None and isinstance(node.config, (DropConfig, PeakConfig)):
            side = "buy" if node.type.startswith("BUY_") else "sell"
            if current.queue_type not in (side, "none") or saved.previous_volume <= 0:
                return "UNKNOWN"
            percent = (
                (saved.previous_volume - current.volume) / saved.previous_volume * 100
            )
            result = (
                EvaluationResult(
                    "TRIGGER",
                    saved.previous_volume,
                    current.volume,
                    percent,
                    node.config.window_minutes
                    if isinstance(node.config, DropConfig)
                    else None,
                )
                if percent >= node.config.threshold_percent
                else EvaluationResult("CLEAR")
            )
        else:
            result = leaf(node, current, history, peaks, lifecycle)
        if (
            isinstance(node.config, ThresholdConfig)
            and threshold_ready is not None
            and not threshold_ready.get(path, False)
        ):
            return "UNKNOWN"
        if result.status == "TRIGGER":
            assert result.previous_volume is not None
            assert result.current_volume is not None
            assert result.actual_percent is not None
            matches.append(
                Evidence(
                    path,
                    node,
                    result.previous_volume,
                    result.current_volume,
                    result.actual_percent,
                    result.window_minutes,
                    result.details,
                )
            )
            return "TRUE"
        return "FALSE" if result.status == "CLEAR" else "UNKNOWN"

    truth = visit(tree, "0")
    return TreeResult(truth, tuple(matches))


def result_json(result: EvaluationResult) -> dict[str, Any]:
    if result.status != "TRIGGER":
        return dict(status=result.status)
    return dict(
        status=result.status,
        previousVolume=result.previous_volume,
        currentVolume=result.current_volume,
        actualPercent=result.actual_percent,
        windowMinutes=result.window_minutes,
    )


def evaluation_detail(
    tree: RuleTree,
    current: QueueSnapshot,
    history: History,
    peaks: dict[str, ObservedPeak],
    ready: dict[str, bool],
    lifecycle: LifecycleContext,
) -> dict[str, Any]:
    """Explain the accepted update using the same pure evaluators and cached history."""

    def visit(node: RuleTree, path: str) -> dict[str, Any]:
        if isinstance(node, Group):
            children = [visit(c, f"{path}.{i}") for i, c in enumerate(node.conditions)]
            return dict(
                truth=combine(node.operator, [c["truth"] for c in children]),
                operator=node.operator,
                children=children,
            )
        result = leaf(node, current, history, peaks, lifecycle)
        reason: str = result.status
        side = "buy" if node.type.startswith("BUY_") else "sell"
        if current.queue_type not in (side, "none"):
            reason = "OPPOSITE_SIDE"
        elif isinstance(node.config, ThresholdConfig) and not ready.get(path, False):
            reason = "CROSSING_NOT_READY"
        elif (
            isinstance(node.config, LifecycleConfig)
            and result.status == "INSUFFICIENT_DATA"
        ):
            reason = "CONFIRMATION_REQUIRED"
        elif result.status == "INSUFFICIENT_DATA":
            reason = "BASELINE_REQUIRED"
        return dict(
            type=node.type,
            truth="TRUE"
            if reason == "TRIGGER"
            else "FALSE"
            if reason == "CLEAR"
            else "UNKNOWN",
            reason=reason,
        )

    return visit(tree, "0")


def evaluate_definition(
    tree: RuleTree,
    current: QueueSnapshot,
    history: History,
    peaks: dict[str, ObservedPeak] | None = None,
    threshold_ready: dict[str, bool] | None = None,
    lifecycle: LifecycleContext | None = None,
) -> dict[str, Any]:
    if isinstance(tree, Condition) and isinstance(tree.config, ThresholdConfig):
        if threshold_ready is not None and not threshold_ready.get("0", False):
            return dict(status="INSUFFICIENT_DATA")
        return result_json(leaf(tree, current, history, peaks))
    if isinstance(tree, Condition) and not isinstance(
        tree.config, (VolumeConfig, LifecycleConfig)
    ):
        return result_json(leaf(tree, current, history, peaks))
    result = evaluate_tree(
        tree,
        current,
        history,
        peaks=peaks,
        threshold_ready=threshold_ready,
        lifecycle=lifecycle,
    )
    if result.truth != "TRUE":
        return dict(status="CLEAR" if result.truth == "FALSE" else "INSUFFICIENT_DATA")
    if not result.matches:
        return dict(status="INSUFFICIENT_DATA")
    first = result.matches[0]
    return dict(
        status="TRIGGER",
        previousVolume=first.previous_volume,
        currentVolume=first.current_volume,
        actualPercent=first.actual_percent,
        windowMinutes=first.window_minutes,
        matches=[e.to_json() for e in result.matches],
    )


def definition_recovered(
    tree: RuleTree,
    current: QueueSnapshot,
    history: History,
    baseline: int | None,
    data: str | None,
    peaks: dict[str, ObservedPeak] | None = None,
    lifecycle: LifecycleContext | None = None,
) -> bool:
    if isinstance(tree, Condition) and not isinstance(
        tree.config, (VolumeConfig, LifecycleConfig)
    ):
        return strategy_for(tree).recovered(current, baseline)
    try:
        matches = parse_evidence(data)
        return (
            bool(matches)
            and evaluate_tree(
                tree, current, history, matches, peaks=peaks, lifecycle=lifecycle
            ).truth
            == "FALSE"
        )
    except ValueError, TypeError, KeyError, RecursionError, OverflowError:
        return False
