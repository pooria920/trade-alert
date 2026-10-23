"""Synchronous persistent evaluation. No Telegram I/O or delivery destinations."""

import json
import logging
import sqlite3
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from .channels import ChannelService
from .composite import (
    definition_recovered,
    evaluate_definition,
    evaluate_tree,
    evaluation_detail,
    parse_evidence,
    ready_thresholds,
)
from .database import rows, transaction
from .definitions import Group, RuleTree, conditions, definition_json, parse_definition
from .evaluators import ObservedPeak, PeakConfig, QueueSnapshot
from .lifecycle import LifecycleConfig, LifecycleContext, LifecycleTracker, weakening
from .outcomes import AlertOutcomeTracker
from .quality import QualityPolicy, classify, stored_policy
from .snapshots import SnapshotRepository
from .users import UserService, now_ms
from .validation import integer, text


class AlertEngine:
    def __init__(
        self,
        db: sqlite3.Connection,
        snapshots: SnapshotRepository,
        users: UserService,
        max_age_ms: int = 30_000,
        now: Callable[[], int] = now_ms,
        channels: ChannelService | None = None,
        logger: logging.Logger | None = None,
        outcomes: AlertOutcomeTracker | None = None,
        lifecycle: LifecycleTracker | None = None,
    ) -> None:
        self.db, self.snapshots, self.users = db, snapshots, users
        self.max_age_ms = integer(max_age_ms)
        self.now, self.channels = now, channels
        self.logger = logger or logging.getLogger(__name__)
        self.started_at = integer(now())
        self.outcomes = outcomes
        self.lifecycle = lifecycle or LifecycleTracker(db)

    def on_snapshot(self, value: QueueSnapshot, session_id: str) -> None:
        """Called after a new detector-accepted insert; persisted state blocks replays.

        An explicit retry after a rolled-back evaluation is safe. Reopening the
        engine never replays snapshots older than this process's startup.
        """
        if not isinstance(value, QueueSnapshot):
            return
        text(session_id, 2**53 - 1, trim=False)
        now = integer(self.now())
        if (
            value.timestamp < self.started_at
            or value.timestamp > now
            or now - value.timestamp > self.max_age_ms
            or not self.snapshots.has_alert_snapshot(value, session_id)
        ):
            return
        history: dict[int, QueueSnapshot | None] = {}
        latest = self.snapshots.get_latest_snapshot(value.ins_code)
        if latest is not None and latest.timestamp > value.timestamp:
            return
        peaks = self.snapshots.observe_peaks(value, session_id)
        if self.outcomes is not None:
            try:
                self.outcomes.on_snapshot(value, session_id)
            except Exception:
                self.logger.error(
                    "Outcome observation failed; alert evaluation continues"
                )

        def at(timestamp: int) -> QueueSnapshot | None:
            if timestamp not in history:
                history[timestamp] = self.snapshots.alert_baseline(
                    value.ins_code, timestamp, session_id
                )
            return history[timestamp]

        eligible: list[tuple[dict[str, Any], RuleTree]] = []
        for rule in rows(
            self.db,
            "SELECT * FROM alert_rules WHERE ins_code=? AND enabled=1 AND owner_id IS NOT NULL",
            (value.ins_code,),
        ):
            try:
                private = rule["channel_id"] is None
                definition = parse_definition(rule["definition"], channel=not private)
                if (
                    definition is None
                    or value.timestamp < rule["created_at"]
                    or (
                        private
                        and (
                            rule["owner_id"] not in self.users.allowed
                            or not self.users.find_symbol(
                                rule["owner_id"], value.ins_code
                            )
                        )
                    )
                    or (
                        not private
                        and (self.channels is None or not self.channels.eligible(rule))
                    )
                ):
                    continue
                eligible.append((rule, definition))
            except Exception:
                # Never include definitions, raw errors, credentials or market payloads.
                self.logger.error(
                    "Rule evaluation failed", extra={"rule_id": rule["id"]}
                )
        levels: dict[str, list[float]] = {"buy": [], "sell": []}
        for _, tree in eligible:
            for leaf in conditions(tree):
                if isinstance(leaf.config, LifecycleConfig) and leaf.type.endswith(
                    "WEAKENED"
                ):
                    levels["buy" if leaf.type.startswith("BUY_") else "sell"].append(
                        leaf.config.threshold_percent
                    )
        context = self.lifecycle.observe(value, session_id, peaks, levels)
        for rule, definition in eligible:
            try:
                with transaction(self.db):
                    self._evaluate(
                        rule, definition, value, session_id, now, at, peaks, context
                    )
            except Exception:
                self.logger.error(
                    "Rule evaluation failed", extra={"rule_id": rule["id"]}
                )

    def _evaluate(
        self,
        rule: dict[str, Any],
        definition: RuleTree,
        value: QueueSnapshot,
        session_id: str,
        now: int,
        at: Callable[[int], QueueSnapshot | None],
        peaks: dict[str, ObservedPeak],
        lifecycle: LifecycleContext,
    ) -> None:
        previous = rows(
            self.db, "SELECT * FROM alert_states WHERE rule_id=?", (rule["id"],)
        )
        state = previous[0] if previous else None
        result: dict[str, Any]
        policy = stored_policy(rule["quality_config"])
        if state and value.timestamp <= state["last_evaluated_at"]:
            return
        if (
            state is None
            or state["revision"] != rule["revision"]
            or state["session_id"] != session_id
        ):
            ready: dict[str, bool] = {}
            ready_thresholds(definition, value, ready)
            self.db.execute(
                "INSERT INTO alert_states (rule_id, revision, session_id, status, last_evaluated_at, last_triggered_at, strategy_data) VALUES (?, ?, ?, 'ARMED', ?, ?, ?) ON CONFLICT(rule_id) DO UPDATE SET revision=excluded.revision, session_id=excluded.session_id, status='ARMED', baseline_volume=NULL, recovery_data=NULL, quality_data=NULL, strategy_data=excluded.strategy_data, last_evaluated_at=excluded.last_evaluated_at",
                (
                    rule["id"],
                    rule["revision"],
                    session_id,
                    value.timestamp,
                    state["last_triggered_at"] if state else None,
                    json.dumps(ready),
                ),
            )
            initial = evaluate_tree(
                definition, value, at, peaks=peaks, threshold_ready=ready
            )
            if initial.truth == "TRUE" and any(
                isinstance(e.condition.config, PeakConfig) for e in initial.matches
            ):
                self.db.execute(
                    "UPDATE alert_states SET status='WAITING_FOR_RECOVERY', baseline_volume=?, recovery_data=? WHERE rule_id=?",
                    (
                        initial.matches[0].previous_volume,
                        json.dumps([e.to_json() for e in initial.matches]),
                        rule["id"],
                    ),
                )
            self.db.execute(
                "UPDATE alert_states SET evaluation_detail=? WHERE rule_id=?",
                (json.dumps(dict(phase="PRIMING")), rule["id"]),
            )
            return
        ready = json.loads(state["strategy_data"] or "{}")
        ready_thresholds(definition, value, ready)
        was_armed = state["status"] == "ARMED"
        cooled = (
            state["last_triggered_at"] is None
            or now - state["last_triggered_at"] >= rule["cooldown_seconds"] * 1000
        )
        recovery_met = False
        if state["status"] != "ARMED":
            state["status"] = "WAITING_FOR_RECOVERY"
            recovery_met = definition_recovered(
                definition,
                value,
                at,
                state["baseline_volume"],
                state["recovery_data"],
                peaks,
                lifecycle,
            )
            if recovery_met and cooled:
                state.update(
                    status="ARMED",
                    baseline_volume=None,
                    recovery_data=None,
                    quality_data=None,
                )
            elif state["quality_data"]:
                quality = json.loads(state["quality_data"])
                fixed = evaluate_tree(
                    definition,
                    value,
                    at,
                    parse_evidence(json.dumps(quality["matches"])),
                    frozen_baselines=True,
                    peaks=peaks,
                    lifecycle=lifecycle,
                )
                if fixed.truth == "TRUE":
                    drops = {
                        e.path: e.actual_percent
                        for e in fixed.matches
                        if weakening(e.condition.type) and e.path in quality["notified"]
                    }
                    escalation = (
                        policy.escalation_enabled
                        and cooled
                        and now - quality["last_notification_at"]
                        >= policy.minimum_interval_seconds * 1000
                        and any(
                            path in quality["notified"]
                            and percent - quality["notified"][path]
                            >= policy.additional_drop_percent
                            for path, percent in drops.items()
                        )
                    )
                    if escalation:
                        first = fixed.matches[0]
                        result = dict(
                            previousVolume=first.previous_volume,
                            currentVolume=first.current_volume,
                            actualPercent=first.actual_percent,
                            windowMinutes=first.window_minutes,
                            initialTimestamp=quality["trigger_timestamp"],
                        )
                        if isinstance(definition, Group) or any(
                            isinstance(c.config, LifecycleConfig)
                            for c in conditions(definition)
                        ):
                            result["matches"] = [e.to_json() for e in fixed.matches]
                        quality["sequence"] += 1
                        self._emit(
                            rule,
                            definition,
                            value,
                            session_id,
                            now,
                            result,
                            policy,
                            f"{quality['occurrence']}:escalation:{quality['sequence']}",
                            True,
                        )
                        for path, percent in drops.items():
                            quality["notified"][path] = max(
                                quality["notified"][path], percent
                            )
                        quality["last_notification_at"] = now
                        state["last_triggered_at"] = now
                        state["quality_data"] = json.dumps(quality)
                        self.logger.info(
                            "Alert escalation recorded", extra={"rule_id": rule["id"]}
                        )
                    else:
                        day = now // 86_400_000 * 86_400_000
                        self.db.execute(
                            "INSERT INTO alert_suppressions(rule_id, day_at, count) VALUES (?, ?, 1) ON CONFLICT(rule_id, day_at) DO UPDATE SET count=count+1",
                            (rule["id"], day),
                        )
                        count = self.db.execute(
                            "SELECT count FROM alert_suppressions WHERE rule_id=? AND day_at=?",
                            (rule["id"], day),
                        ).fetchone()[0]
                        if count & (count - 1) == 0:
                            self.logger.info(
                                "Continued alert suppressed",
                                extra={"rule_id": rule["id"], "count": count},
                            )
        if state["status"] == "ARMED" and (
            was_armed
            or any(
                isinstance(c.config, LifecycleConfig) for c in conditions(definition)
            )
        ):
            result = evaluate_definition(definition, value, at, peaks, ready, lifecycle)
            if result["status"] == "TRIGGER" and cooled:
                matches = result.get("matches")
                state.update(
                    status="TRIGGERED",
                    last_triggered_at=now,
                    baseline_volume=result["previousVolume"],
                    recovery_data=json.dumps(matches) if matches else None,
                )
                occurrence = (
                    f"{rule['id']}:{rule['revision']}:{session_id}:{value.timestamp}"
                )
                evidence = evaluate_tree(
                    definition,
                    value,
                    at,
                    peaks=peaks,
                    threshold_ready=ready,
                    lifecycle=lifecycle,
                ).matches
                state["quality_data"] = json.dumps(
                    dict(
                        occurrence=occurrence,
                        trigger_timestamp=value.timestamp,
                        sequence=0,
                        last_notification_at=now,
                        matches=[e.to_json() for e in evidence],
                        notified={
                            e.path: e.actual_percent
                            for e in evidence
                            if weakening(e.condition.type)
                        },
                    )
                )
                self._emit(
                    rule, definition, value, session_id, now, result, policy, occurrence
                )
        detail = evaluation_detail(definition, value, at, peaks, ready, lifecycle)
        detail["phase"] = (
            "TRIGGERED"
            if state["status"] == "TRIGGERED"
            else "COOLDOWN"
            if recovery_met and not cooled
            else "RECOVERY"
            if state["status"] != "ARMED"
            else "COOLDOWN"
            if not cooled
            else "REARMED"
            if not was_armed
            else "EVALUATED"
        )
        self.db.execute(
            "UPDATE alert_states SET status=?, last_evaluated_at=?, last_triggered_at=?, baseline_volume=?, recovery_data=?, quality_data=?, strategy_data=?, evaluation_detail=? WHERE rule_id=?",
            (
                state["status"],
                value.timestamp,
                state["last_triggered_at"],
                state["baseline_volume"],
                state["recovery_data"],
                state["quality_data"],
                json.dumps(ready),
                json.dumps(detail),
                rule["id"],
            ),
        )

    def _emit(
        self,
        rule: dict[str, Any],
        definition: RuleTree,
        value: QueueSnapshot,
        session_id: str,
        now: int,
        result: dict[str, Any],
        policy: QualityPolicy,
        occurrence: str,
        escalation: bool = False,
    ) -> None:
        matches = result.get("matches")
        payload = dict(
            symbol=value.symbol,
            type="COMPOSITE"
            if matches or isinstance(definition, Group)
            else definition.type,
            operator=definition.operator if isinstance(definition, Group) else None,
            timestamp=value.timestamp,
            previousVolume=result["previousVolume"],
            currentVolume=result["currentVolume"],
            actualPercent=result["actualPercent"],
            windowMinutes=result["windowMinutes"],
        )
        payload["definition"] = definition_json(definition)
        if matches:
            payload["matches"] = matches
        if escalation:
            payload["escalation"] = True
            payload["initialTimestamp"] = result["initialTimestamp"]
        metrics = (
            [
                e.actual_percent
                for e in parse_evidence(json.dumps(matches))
                if weakening(e.condition.type)
                or e.condition.type.endswith("DISAPPEARED")
            ]
            if matches
            else (
                [result["actualPercent"]] if not isinstance(definition, Group) else []
            )
        )
        severity = classify(policy, max(metrics) if metrics else None)
        event_id = str(uuid4())
        inserted = self.db.execute(
            "INSERT INTO alert_events (id, rule_id, occurrence_key, rule_revision, payload, message, created_at, severity) VALUES (?, ?, ?, ?, ?, '', ?, ?) ON CONFLICT DO NOTHING",
            (
                event_id,
                rule["id"],
                occurrence,
                rule["revision"],
                json.dumps(payload),
                now,
                severity,
            ),
        ).rowcount
        if inserted:
            self.db.execute(
                "INSERT INTO notification_outbox (event_id, next_attempt_at) VALUES (?, ?)",
                (event_id, now),
            )
            if self.outcomes is not None and policy.outcome_tracking:
                self.outcomes.schedule_event(event_id, rule, value, session_id)
            if severity != "normal":
                self.logger.info(
                    "Alert severity classified",
                    extra={"severity": severity, "rule_id": rule["id"]},
                )
