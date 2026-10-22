"""Offline regressions against captured outputs of the former TypeScript app."""

import json
import random
import unittest
from copy import deepcopy
from pathlib import Path

from reference import reference

from trade_alert.composite import (
    combine,
    definition_recovered,
    evaluate_definition,
    evaluate_tree,
    parse_evidence,
)
from trade_alert.definitions import definition_json, parse_definition, parse_tree
from trade_alert.evaluators import (
    MAX_SAFE_INTEGER,
    DisappearedConfig,
    DropConfig,
    QueueSnapshot,
    disappeared_recovered,
    drop_recovered,
    queue_disappeared,
    queue_drop,
)
from trade_alert.market import QueueDetector, tehran_date

ROOT = Path(__file__).resolve().parents[1]


def oracle(cases):
    return reference("reference", cases)


def snapshot(volume=1000, side="buy", timestamp=1000):
    return dict(
        insCode="123",
        symbol="TEST",
        timestamp=timestamp,
        queueType=side,
        volume=volume,
        price=None if side == "none" else 100,
        orderCount=0 if side == "none" else 1,
    )


def model(value):
    if value is None:
        return None
    return QueueSnapshot(
        value["insCode"],
        value["symbol"],
        value["timestamp"],
        value["queueType"],
        value["volume"],
        value["price"],
        value["orderCount"],
    )


def wire(result):
    if result.status != "TRIGGER":
        return {"status": result.status}
    return dict(
        status=result.status,
        previousVolume=result.previous_volume,
        currentVolume=result.current_volume,
        actualPercent=result.actual_percent,
        windowMinutes=result.window_minutes,
    )


class EvaluatorParityTests(unittest.TestCase):
    def test_unknown_reference_input_fails_instead_of_creating_expectations(self):
        with self.assertRaisesRegex(AssertionError, "No captured TypeScript scenario"):
            oracle([dict(operation="not-a-captured-scenario")])

    def test_shared_evaluator_inputs(self):
        cases = []
        rng = random.Random(42)
        for side in ("buy", "sell"):
            for threshold in (0.000001, 1, 5, 5.000001, 25, 33.333333, 99.999999, 100):
                for base in (1, 1000, 100_000_000, MAX_SAFE_INTEGER):
                    boundary = base * (1 - threshold / 100)
                    for volume in {
                        1,
                        base,
                        max(1, int(boundary)),
                        max(1, int(boundary) + 1),
                        rng.randint(1, base),
                    }:
                        for kind in ("DROP_PERCENT", "DISAPPEARED"):
                            config = (
                                dict(
                                    thresholdPercent=threshold,
                                    windowMinutes=1,
                                    minimumBaselineVolume=0,
                                )
                                if kind == "DROP_PERCENT"
                                else dict(minimumPreviousVolume=0)
                            )
                            cases.append(
                                dict(
                                    definition=dict(
                                        type=f"{side.upper()}_QUEUE_{kind}",
                                        config=config,
                                    ),
                                    context=dict(
                                        current=snapshot(volume, side, 61_000),
                                        baseline=snapshot(base, side),
                                        previous=snapshot(base, side),
                                    ),
                                    recoveryBaseline=base,
                                )
                            )
            for previous in (
                None,
                snapshot(0, "none"),
                snapshot(1, side),
                snapshot(timestamp=61_000),
                snapshot(timestamp=62_000),
                {**snapshot(), "insCode": "456"},
            ):
                for current in (
                    snapshot(0, "none", 61_000),
                    snapshot(700, "buy", 61_000),
                    snapshot(700, "sell", 61_000),
                ):
                    for kind, config in (
                        (
                            "DROP_PERCENT",
                            dict(
                                thresholdPercent=25,
                                windowMinutes=5,
                                minimumBaselineVolume=100,
                            ),
                        ),
                        ("DISAPPEARED", dict(minimumPreviousVolume=100)),
                    ):
                        cases.append(
                            dict(
                                definition=dict(
                                    type=f"{side.upper()}_QUEUE_{kind}", config=config
                                ),
                                context=dict(
                                    current=current,
                                    baseline=previous,
                                    previous=previous,
                                ),
                                recoveryBaseline=None,
                            )
                        )
        expected = oracle(cases)
        for index, (case, target) in enumerate(zip(cases, expected, strict=True)):
            with self.subTest(index=index):
                definition = case["definition"]
                side = "buy" if definition["type"].startswith("BUY") else "sell"
                context = case["context"]
                current = model(context["current"])
                if definition["type"].endswith("DROP_PERCENT"):
                    c = definition["config"]
                    config = DropConfig(
                        c["thresholdPercent"],
                        c["windowMinutes"],
                        c["minimumBaselineVolume"],
                    )
                    result = queue_drop(
                        side, config, current, model(context["baseline"])
                    )
                    recovered = drop_recovered(
                        side, config, current, case["recoveryBaseline"]
                    )
                    window = config.window_minutes * 60_000
                else:
                    config = DisappearedConfig(
                        definition["config"]["minimumPreviousVolume"]
                    )
                    result = queue_disappeared(
                        side, config, current, model(context["previous"])
                    )
                    recovered = disappeared_recovered(side, config, current)
                    window = None
                self.assertEqual(
                    dict(result=wire(result), recovered=recovered, windowMs=window),
                    target,
                )
        print(f"Evaluator parity: {len(cases)} shared cases")


def book():
    # Synthetic evidence, deliberately separate from captured API fixtures.
    now = 1_792_655_400_000
    return dict(
        instrument=dict(insCode="123", symbol=" TEST ", name="Test"),
        timestamp=now,
        receivedAt=now,
        tradingStatus="open",
        completeness="complete",
        verified=True,
        limits=dict(insCode="123", date=tehran_date(now), lower=90, upper=110),
        levels=[
            dict(
                rank=1,
                bid=dict(price=110, volume=100, orderCount=2),
                ask=dict(price=0, volume=0, orderCount=0),
            )
        ],
    )


class DetectorParityTests(unittest.TestCase):
    def test_captured_best_limits_remains_unverified(self):
        capture = json.loads(
            (ROOT / "tests/fixtures/tsetmc/best-1.json").read_text(encoding="utf-8")
        )
        value = book()
        value.update(timestamp=None, completeness="unknown", verified=False)
        value["levels"] = [
            dict(
                rank=row["number"],
                bid=dict(
                    price=row["pMeDem"],
                    volume=row["qTitMeDem"],
                    orderCount=row["zOrdMeDem"],
                ),
                ask=dict(
                    price=row["pMeOf"],
                    volume=row["qTitMeOf"],
                    orderCount=row["zOrdMeOf"],
                ),
            )
            for row in capture["bestLimits"]
        ]
        actual = QueueDetector().detect(value, value["receivedAt"])
        self.assertFalse(actual.valid)
        self.assertEqual(actual.reason, "unverified-book")
        self.assertIsNone(actual.snapshot)

    def test_detector_shared_inputs(self):
        base = book()
        now = base["timestamp"]
        books = [base]
        for symbol in ("\ufeffTEST\ufeff", "\x1cTEST\x1c", "😀" * 40, "😀" * 41):
            b = deepcopy(base)
            b["instrument"]["symbol"] = symbol
            books.append(b)
        for field, values in {
            "timestamp": [None, -1, now + 1, now - 30_000, now - 30_001, True],
            "receivedAt": [None, now + 1, now - 1, -1, True],
            "verified": [False, None, 1],
            "completeness": ["unknown", "incremental", "bad"],
            "tradingStatus": ["unknown", "closed", "bad"],
            "levels": [None, [], [{}]],
            "limits": [None, {}],
            "instrument": [None, {}],
        }.items():
            for value in values:
                books.append({**deepcopy(base), field: value})
        for field, values in {
            "insCode": [123, "01", "456"],
            "date": ["20200101", "bad"],
            "lower": [0, 110, 111],
            "upper": [0, 89, 90],
        }.items():
            for value in values:
                b = deepcopy(base)
                b["limits"][field] = value
                books.append(b)
        for field, values in {
            "price": [-1, 0, 90, 100, 109, 111, True],
            "volume": [-1, 0, 1, MAX_SAFE_INTEGER, MAX_SAFE_INTEGER + 1, True],
            "orderCount": [-1, 0, 101, 0.5, True],
        }.items():
            for value in values:
                b = deepcopy(base)
                b["levels"][0]["bid"][field] = value
                books.append(b)
        for rank in (0, 2, 101, True):
            b = deepcopy(base)
            b["levels"][0]["rank"] = rank
            books.append(b)
        for rank, price in ((1, 109), (2, 109), (2, 110), (2, 111), (3, 109)):
            b = deepcopy(base)
            b["levels"].append(
                dict(
                    rank=rank,
                    bid=dict(price=price, volume=50, orderCount=1),
                    ask=dict(price=0, volume=0, orderCount=0),
                )
            )
            books.append(b)
            books.append({**b, "levels": list(reversed(b["levels"]))})
        for bid, ask in ((0, 0), (0, 90), (100, 100), (100, 99), (100, 101)):
            b = deepcopy(base)
            b["levels"] = [
                dict(
                    rank=1,
                    bid=dict(
                        price=bid, volume=100 if bid else 0, orderCount=1 if bid else 0
                    ),
                    ask=dict(
                        price=ask, volume=100 if ask else 0, orderCount=1 if ask else 0
                    ),
                )
            ]
            books.append(b)
        # Tehran date boundary; receipt must not confer same-day provenance.
        midnight = 1_792_612_200_000
        b = deepcopy(base)
        b.update(timestamp=midnight - 1, receivedAt=midnight)
        b["limits"]["date"] = tehran_date(midnight - 1)
        cases = [dict(operation="detect", book=b, now=now) for b in books]
        cases.append(dict(operation="detect", book=b, now=midnight))
        expected = oracle(cases)
        for index, (case, target) in enumerate(zip(cases, expected, strict=True)):
            with self.subTest(index=index):
                result = QueueDetector().detect(case["book"], case["now"])
                if result.valid:
                    s = result.snapshot
                    actual = dict(
                        valid=True,
                        snapshot=dict(
                            insCode=s.ins_code,
                            symbol=s.symbol,
                            timestamp=s.timestamp,
                            queueType=s.queue_type,
                            volume=s.volume,
                            price=s.price,
                            orderCount=s.order_count,
                        ),
                    )
                else:
                    actual = dict(valid=False, reason=result.reason)
                self.assertEqual(actual, target)
        print(f"Detector parity: {len(cases)} shared cases")

    def test_missing_verification_never_becomes_none(self):
        b = book()
        b.update(timestamp=None, verified=False, completeness="unknown", levels=[])
        self.assertEqual(
            QueueDetector().detect(b, b["receivedAt"]).reason, "unverified-book"
        )

    def test_tehran_timezone_includes_historical_dst(self):
        # 2021-07-01 20:00 UTC is already the next day in Tehran (DST).
        self.assertEqual(tehran_date(1_625_169_600_000), "20210702")


def condition(kind="BUY_QUEUE_DROP_PERCENT", **config):
    return dict(
        type=kind,
        config=config
        or dict(thresholdPercent=25, windowMinutes=1, minimumBaselineVolume=0),
    )


def group(operator, *children):
    return dict(type="GROUP", operator=operator, conditions=list(children))


class CompositeParityTests(unittest.TestCase):
    def test_definition_parsing(self):
        drop = condition()
        volume = condition("BUY_QUEUE_VOLUME_ABOVE", minimumVolume=100)
        values = [
            None,
            1,
            [],
            {},
            drop,
            volume,
            group("AND", drop),
            group("OR", drop, volume),
            group("AND"),
            group("NOT", drop),
            group("OR", *([drop] * 9)),
            group("AND", group("OR", *([drop] * 5)), group("AND", *([drop] * 4))),
            {**drop, "extra": True},
            condition("PRICE_CHANGE", value=1),
        ]
        deep = drop
        for _ in range(5):
            deep = group("AND", deep)
            values.append(deep)
        for threshold in (
            True,
            "25",
            0,
            -1,
            100.000001,
            0.1234567,
            0.000001,
            99.999999,
        ):
            values.append(
                condition(
                    thresholdPercent=threshold, windowMinutes=1, minimumBaselineVolume=0
                )
            )
        for value in (0, True, "10", -1, MAX_SAFE_INTEGER, MAX_SAFE_INTEGER + 1, 1.5):
            values.append(condition("SELL_QUEUE_VOLUME_ABOVE", minimumVolume=value))
            values.append(
                condition("SELL_QUEUE_DISAPPEARED", minimumPreviousVolume=value)
            )
        for config in ({}, {"minimumPreviousVolume": 0, "extra": 1}):
            values.append(dict(type="BUY_QUEUE_DISAPPEARED", config=config))
        strings = [json.dumps(v) for v in values] + [
            None,
            "{",
            "NaN",
            '{"type":"BUY_QUEUE_DISAPPEARED","config":{"minimumPreviousVolume":Infinity}}',
        ]
        cases = [
            dict(operation="parse", value=value, channel=channel)
            for channel in (False, True)
            for value in strings
        ]
        for case, target in zip(cases, oracle(cases), strict=True):
            with self.subTest(case=case):
                parsed = parse_definition(case["value"], channel=case["channel"])
                self.assertEqual(
                    None if parsed is None else definition_json(parsed), target
                )
        print(f"Definition parity: {len(cases)} shared cases")

    def test_tree_evaluation_and_frozen_recovery(self):
        drop = condition()
        sell = condition("SELL_QUEUE_DISAPPEARED", minimumPreviousVolume=10)
        volume = condition("BUY_QUEUE_VOLUME_ABOVE", minimumVolume=500)
        trees = [
            drop,
            sell,
            volume,
            group("AND", drop, volume),
            group("OR", drop, volume),
            group("AND", drop, sell),
            group("OR", drop, sell),
            group("AND", volume, group("OR", sell, drop)),
        ]
        cases = []
        for tree in trees:
            for current in [snapshot(0, "none", 61_000)] + [
                snapshot(n, side, 61_000)
                for side in ("buy", "sell")
                for n in (474, 475, 499, 500, 700, 750, 799, 800, 1000)
            ]:
                for history in ([], [snapshot()], [snapshot(side="sell")]):
                    cases.append(
                        dict(
                            operation="tree",
                            definition=tree,
                            current=current,
                            history=history,
                            latched=[],
                            baseline=1000,
                            data=None,
                        )
                    )
        # Capture evidence once, then retain it through all recovery samples.
        triggering = dict(
            operation="tree",
            definition=group("AND", drop, volume),
            current=snapshot(700, timestamp=61_000),
            history=[snapshot()],
            latched=[],
            baseline=1000,
            data=None,
        )
        latched = oracle([triggering])[0]["tree"]["matches"]
        for current in [
            snapshot(n, side, 121_000)
            for side in ("buy", "sell")
            for n in (474, 475, 700, 799, 800, 1000)
        ] + [snapshot(0, "none", 121_000)]:
            for operator in ("AND", "OR"):
                cases.append(
                    dict(
                        operation="tree",
                        definition=group(operator, drop, volume),
                        current=current,
                        history=[snapshot(10, timestamp=61_000)],
                        latched=latched,
                        baseline=1000,
                        data=json.dumps(latched),
                    )
                )
        for data in (
            None,
            "[]",
            "null",
            "{}",
            "[{}]",
            "invalid",
            json.dumps(latched * 5),
        ):
            cases.append({**triggering, "data": data})
        for index, (case, target) in enumerate(zip(cases, oracle(cases), strict=True)):
            with self.subTest(index=index):
                tree = parse_tree(case["definition"])
                current = model(case["current"])

                def history(at):
                    return next(
                        (
                            model(s)
                            for s in reversed(case["history"])
                            if s["timestamp"] <= at
                        ),
                        None,
                    )

                evidence = parse_evidence(json.dumps(case["latched"]))
                result = evaluate_tree(tree, current, history, evidence)
                actual = dict(
                    tree=dict(
                        truth=result.truth,
                        matches=[e.to_json() for e in result.matches],
                    ),
                    result=evaluate_definition(tree, current, history),
                    recovered=definition_recovered(
                        tree, current, history, case["baseline"], case["data"]
                    ),
                )
                self.assertEqual(actual, target)
        print(f"Composite parity: {len(cases)} shared cases")

    def test_kleene_truth_table(self):
        for left in ("TRUE", "FALSE", "UNKNOWN"):
            for right in ("TRUE", "FALSE", "UNKNOWN"):
                expected_and = (
                    "FALSE"
                    if "FALSE" in (left, right)
                    else "TRUE"
                    if left == right == "TRUE"
                    else "UNKNOWN"
                )
                expected_or = (
                    "TRUE"
                    if "TRUE" in (left, right)
                    else "FALSE"
                    if left == right == "FALSE"
                    else "UNKNOWN"
                )
                self.assertEqual(combine("AND", [left, right]), expected_and)
                self.assertEqual(combine("OR", [left, right]), expected_or)
