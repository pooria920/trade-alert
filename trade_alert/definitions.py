"""Strict, bounded parsing of authoritative rule-definition JSON."""

from dataclasses import dataclass
from typing import Any, Literal, cast

from .evaluators import DisappearedConfig, DropConfig
from .validation import choice, integer, loads, number, obj


@dataclass(frozen=True, slots=True)
class VolumeConfig:
    minimum_volume: int

    def __post_init__(self) -> None:
        integer(self.minimum_volume, 1)


@dataclass(frozen=True, slots=True)
class Condition:
    type: str
    config: DropConfig | DisappearedConfig | VolumeConfig


@dataclass(frozen=True, slots=True)
class Group:
    operator: Literal["AND", "OR"]
    conditions: tuple["RuleTree", ...]


type RuleTree = Condition | Group


def parse_tree(value: object, *, channel: bool = True) -> RuleTree:
    leaves = 0

    def visit(value: object, depth: int) -> RuleTree:
        nonlocal leaves
        v = obj(value)
        if v.get("type") == "GROUP":
            obj(v, {"type", "operator", "conditions"})
            if not channel or depth == 0:
                raise ValueError("Groups not allowed here")
            operator = cast(Literal["AND", "OR"], choice(v["operator"], ("AND", "OR")))
            children = v["conditions"]
            if not isinstance(children, list) or not 1 <= len(children) <= 8:
                raise ValueError("Expected one to eight children")
            return Group(operator, tuple(visit(child, depth - 1) for child in children))
        obj(v, {"type", "config"})
        kind = v["type"]
        allowed = tuple(
            f"{side}_QUEUE_{suffix}"
            for side in ("BUY", "SELL")
            for suffix in (
                ("DROP_PERCENT", "DISAPPEARED", "VOLUME_ABOVE")
                if channel
                else ("DROP_PERCENT", "DISAPPEARED")
            )
        )
        choice(kind, allowed)
        c = obj(v["config"])
        config: DropConfig | DisappearedConfig | VolumeConfig
        if kind.endswith("DROP_PERCENT"):
            obj(c, {"thresholdPercent", "windowMinutes", "minimumBaselineVolume"})
            window = integer(c["windowMinutes"])
            if window not in (1, 5):
                raise ValueError("Invalid window")
            config = DropConfig(
                number(c["thresholdPercent"]),
                cast(Literal[1, 5], window),
                integer(c["minimumBaselineVolume"]),
            )
        elif kind.endswith("DISAPPEARED"):
            obj(c, {"minimumPreviousVolume"})
            config = DisappearedConfig(integer(c["minimumPreviousVolume"]))
        else:
            obj(c, {"minimumVolume"})
            config = VolumeConfig(integer(c["minimumVolume"], 1))
        leaves += 1
        if leaves > 8:
            raise ValueError("At most eight leaves")
        return Condition(kind, config)

    return visit(value, 4)


def parse_definition(value: str | None, *, channel: bool = False) -> RuleTree | None:
    try:
        return parse_tree(loads(value), channel=channel)
    except ValueError, TypeError, KeyError, RecursionError, OverflowError:
        return None


def definition_json(tree: RuleTree) -> dict[str, Any]:
    if isinstance(tree, Group):
        return dict(
            type="GROUP",
            operator=tree.operator,
            conditions=[definition_json(child) for child in tree.conditions],
        )
    c = tree.config
    if isinstance(c, DropConfig):
        config = dict(
            thresholdPercent=c.threshold_percent,
            windowMinutes=c.window_minutes,
            minimumBaselineVolume=c.minimum_baseline_volume,
        )
    elif isinstance(c, DisappearedConfig):
        config = dict(minimumPreviousVolume=c.minimum_previous_volume)
    else:
        config = dict(minimumVolume=c.minimum_volume)
    return dict(type=tree.type, config=config)
