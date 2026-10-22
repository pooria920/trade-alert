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
    QueueSnapshot,
    Side,
    disappeared_recovered,
    drop_recovered,
    queue_disappeared,
    queue_drop,
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


REGISTRY: dict[str, Callable[[Condition], Strategy]] = {
    "BUY_QUEUE_DROP_PERCENT": lambda c: _drop("buy", c),
    "SELL_QUEUE_DROP_PERCENT": lambda c: _drop("sell", c),
    "BUY_QUEUE_DISAPPEARED": lambda c: _disappeared("buy", c),
    "SELL_QUEUE_DISAPPEARED": lambda c: _disappeared("sell", c),
}


def strategy_for(condition: Condition) -> Strategy:
    return REGISTRY[condition.type](condition)


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
    condition: Condition, current: QueueSnapshot, history: History
) -> EvaluationResult:
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

    def to_json(self) -> dict[str, Any]:
        return dict(
            status="TRIGGER",
            path=self.path,
            condition=definition_json(self.condition),
            previousVolume=self.previous_volume,
            currentVolume=self.current_volume,
            actualPercent=self.actual_percent,
            windowMinutes=self.window_minutes,
        )


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
        if saved is not None:
            return recovered(node, current, saved.previous_volume)
        result = leaf(node, current, history)
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


def evaluate_definition(
    tree: RuleTree, current: QueueSnapshot, history: History
) -> dict[str, Any]:
    if isinstance(tree, Condition) and not isinstance(tree.config, VolumeConfig):
        return result_json(leaf(tree, current, history))
    result = evaluate_tree(tree, current, history)
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
) -> bool:
    if isinstance(tree, Condition) and not isinstance(tree.config, VolumeConfig):
        return strategy_for(tree).recovered(current, baseline)
    try:
        matches = parse_evidence(data)
        return (
            bool(matches)
            and evaluate_tree(tree, current, history, matches).truth == "FALSE"
        )
    except ValueError, TypeError, KeyError, RecursionError, OverflowError:
        return False
