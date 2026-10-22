import unittest
from datetime import datetime

from test_parity import oracle

from trade_alert.config import NUMBERS, parse_env
from trade_alert.schedule import MarketSchedule, parse_sessions

REQUIRED = dict(
    TELEGRAM_BOT_TOKEN="123456:" + "x" * 35,
    TELEGRAM_ALLOWED_USER_IDS="1,2,3,1",
    TELEGRAM_API_ID="12345",
    TELEGRAM_API_HASH="a" * 32,
)


class ConfigTests(unittest.TestCase):
    def test_shared_destination_accepts_canonical_basic_and_supergroup_ids(self):
        for destination in ("-123", "-1001234567890"):
            self.assertEqual(
                parse_env(
                    {
                        **REQUIRED,
                        "TELEGRAM_ADMIN_USER_IDS": "1",
                        "TELEGRAM_CHANNEL_ID": destination,
                    }
                )["TELEGRAM_CHANNEL_ID"],
                destination,
            )
        for destination in (
            "123",
            "-0",
            "-0123",
            "-1000000000000",
            "-4503599627370496",
            "@group",
        ):
            with self.assertRaisesRegex(ValueError, "TELEGRAM_CHANNEL_ID"):
                parse_env(
                    {
                        **REQUIRED,
                        "TELEGRAM_ADMIN_USER_IDS": "1",
                        "TELEGRAM_CHANNEL_ID": destination,
                    }
                )

    def test_force_active_is_test_only(self):
        # Intentional tightening to the project requirement; TS also allows development.
        for environment in ("development", "production"):
            with self.assertRaises(ValueError):
                parse_env(
                    {
                        **REQUIRED,
                        "NODE_ENV": environment,
                        "MARKET_MONITOR_FORCE_ACTIVE": "true",
                    }
                )
        self.assertTrue(
            parse_env(
                {**REQUIRED, "NODE_ENV": "test", "MARKET_MONITOR_FORCE_ACTIVE": "true"}
            )["MARKET_MONITOR_FORCE_ACTIVE"]
        )

    def test_shared_configuration_inputs(self):
        overrides = [
            {},
            {"TELEGRAM_ADMIN_USER_IDS": "1", "TELEGRAM_CHANNEL_ID": "-100123"},
        ]
        for field, values in {
            "TELEGRAM_BOT_TOKEN": ["", "secret", "123:" + "x" * 30],
            "TELEGRAM_ALLOWED_USER_IDS": [
                "",
                "1.5",
                "123,",
                "0",
                "01",
                "-1",
                "4503599627370496",
            ],
            "TELEGRAM_ALLOWED_USER_ID": ["1"],
            "TELEGRAM_ADMIN_USER_IDS": ["1", "999"],
            "TELEGRAM_CHANNEL_ID": ["-100123", "123", "@channel"],
            "TSETMC_BASE_URL": [
                "http://example.com",
                "invalid",
                "https://example.com:bad",
            ],
            "DATABASE_URL": ["", "https://host/db", "foo\0bar", ":memory:"],
            "MARKET_SESSIONS_JSON": [
                "invalid",
                "{}",
                '[{"days":[6],"start":"09:00","end":"12:00"}]',
                '[{"days":[0],"start":"09:00","end":"12:00"}]',
                '[{"days":[6],"start":"12:00","end":"09:00"}]',
            ],
            "MARKET_CLOSED_DATES": ["2026-02-30", "2026-10-22", "2026-10-22,"],
            "LOG_LEVEL": ["everything"],
            "MARKET_MONITOR_FORCE_ACTIVE": ["yes", "false"],
        }.items():
            overrides.extend({field: value} for value in values)
        for field, (_, minimum, maximum) in NUMBERS.items():
            overrides.extend(
                {field: str(value)}
                for value in (minimum - 1, minimum, maximum, maximum + 1)
            )
            overrides.extend(
                {field: value} for value in ("NaN", "1_000", "1e3", "0x10")
            )
        overrides.extend(
            [
                dict(NODE_ENV="production", MARKET_MONITOR_FORCE_ACTIVE="true"),
                dict(NODE_ENV="production", DATABASE_URL=":memory:"),
                dict(NODE_ENV="production", DEBUG="*"),
                dict(NODE_ENV="production", DEBUG=" "),
                dict(TELEGRAM_ADMIN_USER_IDS="4", TELEGRAM_CHANNEL_ID="-100123"),
                dict(STOCK_ALERT_CONTAINER="true", DATABASE_URL="/app/data/bot.db"),
                dict(STOCK_ALERT_CONTAINER="true", DATABASE_URL="/app/data"),
                dict(
                    STOCK_ALERT_CONTAINER="true", DATABASE_URL="/app/data/../outside.db"
                ),
            ]
        )
        cases = [
            dict(operation="config", input={**REQUIRED, **value}) for value in overrides
        ]
        for case, expected in zip(cases, oracle(cases), strict=True):
            with self.subTest(input=case["input"]):
                try:
                    actual = dict(valid=True, values=parse_env(case["input"]))
                    # Python-only MTProto credentials have no TypeScript counterpart.
                    actual["values"].pop("TELEGRAM_API_ID")
                    actual["values"].pop("TELEGRAM_API_HASH")
                except ValueError:
                    actual = dict(valid=False)
                self.assertEqual(actual, expected)
        print(f"Configuration parity: {len(cases)} shared cases")

    def test_errors_do_not_echo_secret_values(self):
        secret = "do-not-print-this-secret"
        with self.assertRaises(ValueError) as failure:
            parse_env({**REQUIRED, "TELEGRAM_BOT_TOKEN": secret})
        self.assertNotIn(secret, str(failure.exception))
        self.assertIn("TELEGRAM_BOT_TOKEN", str(failure.exception))

    def test_schedule_parity(self):
        sessions = [
            dict(days=[6, 7, 1, 2, 3], start="09:00", end="12:30"),
            dict(market="other", days=[4], start="10:00", end="11:00"),
        ]
        cases = []
        for raw in ([], sessions):
            for market in ("equities", "other"):
                for closed in ([], ["2026-10-21"]):
                    for forced in (False, True):
                        for value in (
                            "2026-10-21T08:59:59+03:30",
                            "2026-10-21T09:00:00+03:30",
                            "2026-10-21T12:29:59+03:30",
                            "2026-10-21T12:30:00+03:30",
                            "2026-10-22T10:30:00+03:30",
                            "2026-10-23T10:30:00+03:30",
                        ):
                            now = int(datetime.fromisoformat(value).timestamp() * 1000)
                            cases.append(
                                dict(
                                    operation="schedule",
                                    sessions=raw,
                                    market=market,
                                    closedDates=closed,
                                    forceActive=forced,
                                    now=now,
                                )
                            )
        for case, expected in zip(cases, oracle(cases), strict=True):
            schedule = MarketSchedule(
                parse_sessions(case["sessions"]),
                case["closedDates"],
                case["forceActive"],
                case["market"],
            )
            self.assertEqual(
                dict(
                    configured=schedule.configured,
                    sessionId=schedule.session_id(case["now"]),
                ),
                expected,
            )
        print(f"Schedule parity: {len(cases)} shared cases")
