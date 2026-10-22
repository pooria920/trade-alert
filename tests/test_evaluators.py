"""Synthetic domain cases only; no network or database access."""

import unittest
from dataclasses import replace

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


def snapshot(volume=1000, side="buy", timestamp=1000):
    return QueueSnapshot(
        "123",
        "TEST",
        timestamp,
        side,
        volume,
        None if side == "none" else 100,
        0 if side == "none" else 1,
    )


class EvaluatorTests(unittest.TestCase):
    def test_drop_boundaries_and_confirmed_none(self):
        for side in ("buy", "sell"):
            config = DropConfig(25, 1, 100)
            baseline = snapshot(side=side)
            for volume, expected in ((751, "CLEAR"), (750, "TRIGGER"), (1100, "CLEAR")):
                with self.subTest(side=side, volume=volume):
                    result = queue_drop(
                        side, config, snapshot(volume, side, 61_000), baseline
                    )
                    self.assertEqual(result.status, expected)
            result = queue_drop(side, config, snapshot(0, "none", 61_000), baseline)
            self.assertEqual(result.actual_percent, 100)
            self.assertEqual(result.window_minutes, 1)

    def test_exact_decimal_and_safe_integer_boundary(self):
        config = DropConfig(33.333333, 5, 0)
        baseline = snapshot(100_000_000)
        self.assertEqual(
            queue_drop(
                "buy", config, snapshot(66_666_667, timestamp=2000), baseline
            ).status,
            "TRIGGER",
        )
        self.assertEqual(
            queue_drop(
                "buy", config, snapshot(66_666_668, timestamp=2000), baseline
            ).status,
            "CLEAR",
        )
        baseline = snapshot(MAX_SAFE_INTEGER)
        result = queue_drop(
            "buy", DropConfig(100, 1, 0), snapshot(0, "none", 2000), baseline
        )
        self.assertEqual(result.previous_volume, MAX_SAFE_INTEGER)

    def test_missing_opposite_old_and_other_instrument_are_insufficient(self):
        current = snapshot(0, "none", 2000)
        for previous in (
            None,
            snapshot(side="sell"),
            snapshot(timestamp=2000),
            snapshot(timestamp=3000),
            replace(snapshot(), ins_code="456"),
            snapshot(0, "none"),
            snapshot(99),
        ):
            with self.subTest(previous=previous):
                self.assertEqual(
                    queue_drop("buy", DropConfig(25, 1, 100), current, previous).status,
                    "INSUFFICIENT_DATA",
                )
                self.assertEqual(
                    queue_disappeared(
                        "buy", DisappearedConfig(100), current, previous
                    ).status,
                    "INSUFFICIENT_DATA",
                )
        opposite = snapshot(100, "sell", 2000)
        self.assertEqual(
            queue_drop("buy", DropConfig(25, 1, 0), opposite, snapshot()).status,
            "INSUFFICIENT_DATA",
        )
        self.assertEqual(
            queue_disappeared("buy", DisappearedConfig(0), opposite, snapshot()).status,
            "INSUFFICIENT_DATA",
        )

    def test_disappearance_and_recovery(self):
        for side in ("buy", "sell"):
            config = DisappearedConfig(100)
            self.assertEqual(
                queue_disappeared(
                    side, config, snapshot(0, "none", 2000), snapshot(side=side)
                ).status,
                "TRIGGER",
            )
            self.assertEqual(
                queue_disappeared(
                    side, config, snapshot(1, side, 2000), snapshot(side=side)
                ).status,
                "CLEAR",
            )
            self.assertFalse(disappeared_recovered(side, config, snapshot(99, side)))
            self.assertTrue(disappeared_recovered(side, config, snapshot(100, side)))
            self.assertFalse(
                disappeared_recovered(side, DisappearedConfig(0), snapshot(0, "none"))
            )

    def test_hysteresis(self):
        for side in ("buy", "sell"):
            config = DropConfig(25, 1, 0)
            self.assertFalse(drop_recovered(side, config, snapshot(799, side), 1000))
            self.assertTrue(drop_recovered(side, config, snapshot(800, side), 1000))
            self.assertFalse(drop_recovered(side, config, snapshot(1000, side), None))
            self.assertFalse(drop_recovered(side, config, snapshot(0, "none"), 1000))
            for threshold in (1, 5):
                config = DropConfig(threshold, 1, 0)
                self.assertFalse(
                    drop_recovered(side, config, snapshot(999, side), 1000)
                )
                self.assertTrue(
                    drop_recovered(side, config, snapshot(1000, side), 1000)
                )

    def test_validation(self):
        for value in (True, -1, MAX_SAFE_INTEGER + 1, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                DisappearedConfig(value)
        for value in (True, 0, -1, 101, float("nan"), float("inf"), 0.1234567):
            with self.subTest(value=value), self.assertRaises(ValueError):
                DropConfig(value, 1, 0)
        for window in (True, 2, 1.0):
            with self.assertRaises(ValueError):
                DropConfig(25, window, 0)
        for changes in (
            {"ins_code": 123},
            {"ins_code": "01"},
            {"volume": True},
            {"queue_type": "none"},
            {"price": float("nan")},
            {"order_count": 1001},
            {"timestamp": -1},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(snapshot(), **changes)


if __name__ == "__main__":
    unittest.main()
