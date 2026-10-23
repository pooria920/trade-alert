"""Explanations use synthetic accepted evidence and disposable databases only."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import test_persistence as persistence
from test_services import DROP, ITEM
from test_strategies import rolling, threshold

from trade_alert.bot_text import history_text, rule_status_text
from trade_alert.composite import evaluate_tree, evaluation_detail
from trade_alert.database import MIGRATIONS, open_database, rows
from trade_alert.definitions import parse_tree
from trade_alert.delivery import parse_payload
from trade_alert.lifecycle import LifecycleContext, LifecycleOptions, LifecycleTracker


class ExplanationTests(unittest.IsolatedAsyncioTestCase):
    setUp = persistence.PersistenceTests.setUp
    restart = persistence.PersistenceTests.restart
    accept = persistence.PersistenceTests.accept
    events = persistence.PersistenceTests.events
    states = persistence.PersistenceTests.states

    def detail(self):
        return json.loads(self.states()[0]["evaluation_detail"])

    def rendered(self, rule, **kwargs):
        return rule_status_text(
            self.alerts.get("1", rule),
            self.clock,
            kwargs.get("open", True),
            kwargs.get("session", "A"),
            30_000,
        )

    async def test_priming_missing_history_clear_trigger_recovery_cooldown_and_replay(
        self,
    ):
        rule = self.alerts.create("1", "123", rolling(minutes=1), 120)
        self.assertIn("هنوز ارزیابی ثبت نشده", self.rendered(rule))
        self.accept(0)
        self.assertEqual(self.detail()["phase"], "PRIMING")
        self.accept(10)
        self.assertEqual(self.detail()["reason"], "BASELINE_REQUIRED")
        self.assertIn("دادهٔ مبنای", self.rendered(rule))
        self.accept(60, 900)
        self.assertEqual(self.detail()["reason"], "CLEAR")
        self.accept(70, 600)
        self.assertEqual(self.detail()["phase"], "TRIGGERED")
        payload = parse_payload(self.events()[0]["payload"])
        self.assertEqual(payload["definition"], rolling(minutes=1))
        rendered = history_text(self.events()[0])
        self.assertIn("علت هشدار:", rendered)
        self.assertIn("۳۰", rendered)
        self.assertIn("۶۰۰", rendered)
        mismatched = {
            **payload,
            "definition": {**rolling(minutes=1), "type": "SELL_QUEUE_DROP_PERCENT"},
        }
        with self.assertRaisesRegex(ValueError, "Mismatched"):
            parse_payload(json.dumps(mismatched))
        self.assertIn("تحویل پیام نیست", self.rendered(rule))
        before = self.states()
        self.accept(70, 600)
        self.assertEqual(self.states(), before)
        self.accept(80, 600)
        self.assertEqual(self.detail()["phase"], "RECOVERY")
        self.accept(90, 1000)
        self.assertEqual(self.detail()["phase"], "COOLDOWN")
        self.assertIn("باقی‌مانده", self.rendered(rule))
        self.accept(200, 1000)
        self.assertEqual(self.detail()["phase"], "REARMED")
        self.assertEqual(len(self.events()), 1)
        self.restart()
        self.assertEqual(self.detail()["phase"], "REARMED")

    async def test_legacy_missing_diagnostics_and_invalid_policy_are_explicit(self):
        rule = self.alerts.create("1", "123", DROP)
        self.accept(0)
        self.db.execute("UPDATE alert_states SET evaluation_detail=NULL")
        self.assertIn("توضیح آخرین ارزیابی موجود نیست", self.rendered(rule))
        self.db.execute("UPDATE alert_rules SET quality_config='[]', enabled=0")
        rendered = self.rendered(rule)
        self.assertIn("قانون خاموش است", rendered)
        self.assertIn("تنظیمات پیشرفته نامعتبر", rendered)

    async def test_threshold_readiness_opposite_side_stale_and_session_reset(self):
        rule = self.alerts.create("1", "123", threshold(), 0)
        self.accept(0, 100)
        self.accept(10, 100)
        self.assertEqual(self.detail()["reason"], "CROSSING_NOT_READY")
        self.assertIn("عبور از حد", self.rendered(rule))
        self.accept(20, 1000, "sell")
        self.assertEqual(self.detail()["reason"], "OPPOSITE_SIDE")
        self.assertEqual(self.detail()["truth"], "UNKNOWN")
        self.assertIn("صف مخالف", self.rendered(rule))
        self.clock += 30_001
        self.assertIn("وضعیت فعلی صف را نشان نمی‌دهد", self.rendered(rule))
        self.assertIn("همین جلسه", self.rendered(rule, session="B"))
        self.assertIn("خارج از جلسه", self.rendered(rule, open=False))
        self.accept(60, session="B")
        self.assertEqual(self.detail()["phase"], "PRIMING")
        self.assertFalse(self.events())

    async def test_composite_diagnostics_match_kleene_evaluation_and_explain_matches(
        self,
    ):
        for operator in ("AND", "OR"):
            tree = parse_tree(
                dict(
                    type="GROUP",
                    operator=operator,
                    conditions=[
                        rolling(minutes=1),
                        dict(
                            type="BUY_QUEUE_VOLUME_ABOVE",
                            config=dict(minimumVolume=500),
                        ),
                    ],
                )
            )
            current = persistence.snapshot(10, 1000)
            context = LifecycleContext(frozenset())
            result = evaluate_tree(tree, current, lambda _: None)
            detail = evaluation_detail(tree, current, lambda _: None, {}, {}, context)
            self.assertEqual(detail["truth"], result.truth)
            self.assertEqual(detail["children"][0]["reason"], "BASELINE_REQUIRED")
        rule = await self.channels.create(
            "1",
            ITEM,
            dict(
                type="GROUP",
                operator="OR",
                conditions=[
                    rolling(minutes=1),
                    dict(type="BUY_QUEUE_VOLUME_ABOVE", config=dict(minimumVolume=500)),
                ],
            ),
        )
        self.accept(0)
        self.accept(10)
        rendered = history_text(self.events()[0])
        self.assertIn("علت هشدار: حداقل یکی", rendered)
        self.assertIn("حداقل حجم صف خرید", rendered)
        self.assertNotIn("کاهش صف خرید", rendered)
        self.users.set_notifications("1", False)
        shared = self.channels.get("1", rule)
        self.assertNotIn(
            "اعلان‌های خصوصی", rule_status_text(shared, self.clock, True, "A", 30_000)
        )

    async def test_lifecycle_requires_confirmation_and_does_not_invent_transition(self):
        self.engine.lifecycle = LifecycleTracker(
            self.db, LifecycleOptions(confirmation_snapshots=3)
        )
        rule = self.alerts.create(
            "1",
            "123",
            dict(
                type="BUY_QUEUE_FORMED",
                config=dict(minimumVolume=1),
            ),
        )
        self.accept(0)
        self.accept(1)
        self.assertEqual(self.detail()["reason"], "CONFIRMATION_REQUIRED")
        self.assertIn("هنوز تأیید نشده", self.rendered(rule))
        self.assertFalse(self.events())

    async def test_diagnostics_remain_owner_and_channel_scoped_and_edits_clear_them(
        self,
    ):
        private = self.alerts.create("1", "123", DROP)
        shared = await self.channels.create("1", ITEM, DROP)
        self.accept(0)
        self.accept(10)
        self.assertIsNone(self.alerts.get("2", private))
        self.assertIsNone(self.alerts.get("1", shared))
        self.assertIsNone(self.channels.get("1", private))
        self.users.set_notifications("1", False)
        self.assertIn("اعلان‌های خصوصی شما خاموش", self.rendered(private))
        self.alerts.update("1", private, DROP, revision=1)
        self.assertIsNone(self.alerts.get("1", private)["evaluation"])
        self.admins.remove("1")
        self.assertFalse(self.channels.get("2", shared)["eligible"])

    async def test_migration_preserves_old_state_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            old = folder / "old"
            shutil.copytree(MIGRATIONS, old)
            journal_path = old / "meta/_journal.json"
            journal = json.loads(journal_path.read_text())
            journal["entries"] = journal["entries"][:9]
            journal_path.write_text(json.dumps(journal))
            path = folder / "upgrade.db"
            with open_database(path, migrations=old) as db:
                db.execute("INSERT INTO users VALUES ('1', '1', 1, 1, 1)")
                db.execute("INSERT INTO symbols VALUES ('123', 'TEST', 1)")
                db.execute(
                    "INSERT INTO alert_rules (id, owner_id, ins_code, type, created_at) VALUES ('r', '1', '123', 'BUY_QUEUE_DISAPPEARED', 1)"
                )
                db.execute(
                    "INSERT INTO alert_states (rule_id, revision, session_id, status, last_evaluated_at) VALUES ('r', 1, 'A', 'ARMED', 1)"
                )
                state = rows(db, "SELECT * FROM alert_states")[0]
            for _ in range(2):
                with open_database(path) as db:
                    upgraded = rows(db, "SELECT * FROM alert_states")[0]
                    self.assertIsNone(upgraded.pop("evaluation_detail"))
                    self.assertEqual(upgraded, state)
                    self.assertEqual(
                        db.execute("PRAGMA foreign_key_check").fetchall(), []
                    )


if __name__ == "__main__":
    unittest.main()
