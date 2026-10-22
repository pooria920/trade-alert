"""Disposable databases; verify original Drizzle schema, hashes and locking."""

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from trade_alert.database import MIGRATIONS, instance_lock, open_database


class DatabaseTests(unittest.TestCase):
    def test_note_migration_upgrades_existing_database_without_changing_watchlists(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            migration_folder = folder / "old-migrations"
            shutil.copytree(MIGRATIONS, migration_folder)
            journal_path = migration_folder / "meta/_journal.json"
            journal = json.loads(journal_path.read_text())
            journal["entries"] = journal["entries"][:5]
            journal_path.write_text(json.dumps(journal))
            database_path = folder / "upgrade.db"
            with open_database(database_path, migrations=migration_folder) as db:
                db.execute("INSERT INTO users VALUES ('1', '1', 1, 1, 1)")
                db.execute("INSERT INTO symbols VALUES ('123', 'TEST', 1)")
                db.execute("INSERT INTO watchlists VALUES ('1', '123', 1)")
                db.execute("INSERT INTO telegram_channels (channel_id) VALUES ('-123')")
            with open_database(database_path) as db:
                self.assertEqual(
                    db.execute("SELECT * FROM watchlists").fetchall(), [("1", "123", 1)]
                )
                db.execute(
                    "INSERT INTO channel_symbol_notes VALUES ('-123', '123', 'persisted note', '1', 2)"
                )
            with open_database(database_path) as db:
                self.assertEqual(
                    db.execute("SELECT note FROM channel_symbol_notes").fetchone()[0],
                    "persisted note",
                )

    def test_deleting_rule_cascades_only_its_dependents(self):
        with tempfile.TemporaryDirectory() as directory:
            with open_database(Path(directory) / "cascade.db") as db:
                db.execute("INSERT INTO symbols VALUES ('123', 'TEST', 1)")
                db.execute("INSERT INTO users VALUES ('1', '1', 1, 1, 1)")
                db.execute(
                    "INSERT INTO telegram_channels (channel_id) VALUES ('-100123')"
                )
                for rule, channel in (("private", None), ("channel", "-100123")):
                    db.execute(
                        "INSERT INTO alert_rules (id, ins_code, type, created_at, owner_id, channel_id) VALUES (?, '123', 'BUY_QUEUE_DISAPPEARED', 1, '1', ?)",
                        (rule, channel),
                    )
                    db.execute(
                        "INSERT INTO alert_states (rule_id, revision, session_id, status, last_evaluated_at) VALUES (?, 1, 'session', 'ARMED', 1)",
                        (rule,),
                    )
                    db.execute(
                        "INSERT INTO alert_events (id, rule_id, message, created_at) VALUES (?, ?, 'test', 1)",
                        (rule, rule),
                    )
                    db.execute(
                        "INSERT INTO notification_outbox (event_id, next_attempt_at) VALUES (?, 1)",
                        (rule,),
                    )
                db.execute(
                    "INSERT INTO queue_snapshots (ins_code, timestamp, queue_type, volume, order_count) VALUES ('123', 1, 'none', 0, 0)"
                )
                db.execute("DELETE FROM alert_rules WHERE id = 'private'")
                self.assertEqual(
                    db.execute("SELECT id FROM alert_rules").fetchall(), [("channel",)]
                )
                self.assertEqual(
                    db.execute("SELECT rule_id FROM alert_events").fetchall(),
                    [("channel",)],
                )
                self.assertEqual(
                    db.execute("SELECT rule_id FROM alert_states").fetchall(),
                    [("channel",)],
                )
                self.assertEqual(
                    db.execute("SELECT event_id FROM notification_outbox").fetchall(),
                    [("channel",)],
                )
                self.assertEqual(
                    db.execute("SELECT count(*) FROM queue_snapshots").fetchone()[0], 1
                )
                self.assertEqual(
                    db.execute("SELECT count(*) FROM symbols").fetchone()[0], 1
                )

    def test_original_schema_migrations_idempotence_and_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            py_path = folder / "python.db"
            ts = json.loads(
                (Path(__file__).parent / "fixtures/reference/database.json").read_text(
                    encoding="utf-8"
                )
            )
            with open_database(py_path) as db:
                db.row_factory = sqlite3.Row
                migrations = [
                    dict(r)
                    for r in db.execute(
                        "SELECT hash, created_at FROM __drizzle_migrations ORDER BY created_at"
                    )
                ]
                schema = [
                    dict(r)
                    for r in db.execute(
                        "SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
                    )
                ]
                self.assertEqual(migrations[:5], ts["migrations"])
                self.assertEqual(len(migrations), 6)

                # SQLite preserves insignificant formatting in CREATE statements.
                def normalized(rows):
                    return [
                        (r["name"], " ".join(r["sql"].split()) if r["sql"] else None)
                        for r in rows
                    ]

                self.assertEqual(
                    normalized(
                        [r for r in schema if r["name"] != "channel_symbol_notes"]
                    ),
                    normalized(ts["schema"]),
                )
                with self.assertRaises(RuntimeError), instance_lock(py_path):
                    pass
                with self.assertRaises(RuntimeError), open_database(py_path):
                    pass
                self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            with open_database(py_path) as db:
                self.assertEqual(
                    db.execute(
                        "SELECT hash, created_at FROM __drizzle_migrations ORDER BY created_at"
                    ).fetchall(),
                    [(r["hash"], r["created_at"]) for r in migrations],
                )
                self.assertEqual(
                    db.execute("SELECT count(*) FROM __drizzle_migrations").fetchone()[
                        0
                    ],
                    6,
                )
                db.execute("INSERT INTO symbols VALUES ('123', 'TEST', 1)")
            with open_database(py_path) as db:
                self.assertEqual(
                    db.execute("SELECT symbol FROM symbols").fetchone()[0], "TEST"
                )

    def test_legacy_upgrade_preserves_unknown_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            migrations = folder / "migrations"
            shutil.copytree(MIGRATIONS, migrations)
            journal_path = migrations / "meta/_journal.json"
            journal = json.loads(journal_path.read_text())
            journal["entries"] = journal["entries"][:1]
            journal_path.write_text(json.dumps(journal))
            path = folder / "legacy.db"
            with open_database(path, migrations=migrations) as db:
                db.execute("INSERT INTO symbols VALUES ('123', 'TEST', 1)")
                db.execute(
                    "INSERT INTO alert_rules (id, ins_code, type, created_at) VALUES ('legacy', '123', 'BUY_QUEUE_DISAPPEARED', 1)"
                )
                db.execute(
                    "INSERT INTO queue_snapshots (ins_code, timestamp, queue_type, volume, order_count) VALUES ('123', 1, 'none', 0, 0)"
                )
            with open_database(path) as db:
                self.assertEqual(
                    db.execute(
                        "SELECT owner_id, definition FROM alert_rules"
                    ).fetchone(),
                    (None, None),
                )
                self.assertEqual(
                    db.execute(
                        "SELECT collected_at, symbol, session_id FROM queue_snapshots"
                    ).fetchone(),
                    (None, None, None),
                )
                self.assertEqual(
                    db.execute("PRAGMA integrity_check").fetchone()[0], "ok"
                )
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_failed_migration_rolls_back_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            migrations = folder / "migrations"
            shutil.copytree(MIGRATIONS, migrations)
            last = migrations / "0004_channel_alerts.sql"
            last.write_bytes(
                last.read_bytes() + b"\n--> statement-breakpoint\nINVALID SQL;"
            )
            path = folder / "failure.db"
            with (
                self.assertRaises(sqlite3.OperationalError),
                open_database(path, migrations=migrations),
            ):
                pass
            with open_database(path) as db:
                self.assertEqual(
                    db.execute("SELECT count(*) FROM __drizzle_migrations").fetchone()[
                        0
                    ],
                    6,
                )

    def test_process_death_releases_lock_without_deleting_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "crash.db"
            code = "from trade_alert.database import instance_lock; import sys; c=instance_lock(sys.argv[1]); c.__enter__(); print('locked', flush=True); sys.stdin.read()"
            child = subprocess.Popen(
                [sys.executable, "-c", code, str(path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(child.stdout.readline().strip(), "locked")
                with self.assertRaises(RuntimeError), open_database(path):
                    pass
            finally:
                child.kill()
                child.communicate(timeout=10)
            self.assertTrue(Path(str(path) + ".instance-lock.sqlite").exists())
            with open_database(path) as db:
                self.assertEqual(
                    db.execute("PRAGMA integrity_check").fetchone()[0], "ok"
                )
