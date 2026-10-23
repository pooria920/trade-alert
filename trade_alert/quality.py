"""Strict optional notification policy, independent of market evaluation/transport."""

from dataclasses import asdict, dataclass
from typing import Literal, cast

from .validation import choice, integer, loads, number, obj

type Severity = Literal["normal", "important", "critical"]
SEVERITIES = ("normal", "important", "critical")
SEVERITY_LABELS = dict(normal="عادی", important="مهم", critical="بحرانی")


@dataclass(frozen=True, slots=True)
class SeverityThreshold:
    severity: Severity
    minimum_drop_percent: float


@dataclass(frozen=True, slots=True)
class QualityPolicy:
    severity: Severity = "normal"
    thresholds: tuple[SeverityThreshold, ...] = ()
    escalation_enabled: bool = False
    additional_drop_percent: float = 25
    minimum_interval_seconds: int = 300
    outcome_tracking: bool = False

    def to_json(self) -> dict[str, object]:
        return asdict(self)


def parse_policy(value: object) -> QualityPolicy:
    v = obj(value)
    if set(v) - set(QualityPolicy().to_json()):
        raise ValueError("Unknown quality setting")
    severity = cast(Severity, choice(v.get("severity", "normal"), SEVERITIES))
    thresholds = v.get("thresholds", [])
    if not isinstance(thresholds, (list, tuple)) or len(thresholds) > 3:
        raise ValueError("At most three severity thresholds")
    parsed = []
    for threshold in thresholds:
        t = obj(threshold, {"severity", "minimum_drop_percent"})
        parsed.append(
            SeverityThreshold(
                cast(Severity, choice(t["severity"], SEVERITIES)),
                number(t["minimum_drop_percent"], 0, 100),
            )
        )
    if any(
        a.minimum_drop_percent >= b.minimum_drop_percent
        or SEVERITIES.index(a.severity) >= SEVERITIES.index(b.severity)
        for a, b in zip(parsed, parsed[1:])
    ):
        raise ValueError("Thresholds and severity must strictly increase")
    enabled, outcomes = (
        v.get("escalation_enabled", False),
        v.get("outcome_tracking", False),
    )
    if type(enabled) is not bool or type(outcomes) is not bool:
        raise ValueError("Expected boolean quality settings")
    additional = number(v.get("additional_drop_percent", 25), 0.000001, 100)
    return QualityPolicy(
        severity,
        tuple(parsed),
        enabled,
        additional,
        integer(v.get("minimum_interval_seconds", 300), 1, 86_400),
        outcomes,
    )


def stored_policy(raw: str | None) -> QualityPolicy:
    # Malformed non-NULL settings fail closed at callers, never enable defaults.
    return QualityPolicy() if raw is None else parse_policy(loads(raw))


def classify(policy: QualityPolicy, drop_percent: float | None) -> Severity:
    severity = policy.severity
    if drop_percent is not None:
        number(drop_percent, 0, 100)
        for threshold in policy.thresholds:
            if drop_percent >= threshold.minimum_drop_percent and SEVERITIES.index(
                threshold.severity
            ) > SEVERITIES.index(severity):
                severity = threshold.severity
    return severity
