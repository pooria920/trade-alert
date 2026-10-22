"""Online backups and maintenance only touch disposable SQLite paths."""

import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trade_alert.backup import (
    GENERATED,
    backup_database,
    backup_prefix,
    prune_backups,
    restore_database,
)
from trade_alert.database import instance_lock, open_database
from trade_alert.maintenance import main

NOW = 1_800_000_000_000


class BackupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = self.directory / "source.db"
        self.backups = self.directory / "backups"
        self.backups.mkdir()

    def test_online_backup_contains_committed_wal_and_is_portable(self):
        with open_database(self.path) as db:
            db.execute(
                "INSERT INTO users (telegram_user_id, private_chat_id, created_at, updated_at) VALUES ('1', '1', ?, ?)",
                (NOW, NOW),
            )
            self.assertTrue(Path(str(self.path) + "-wal").exists())
            target = backup_database(self.path, self.backups, NOW)
            second = backup_database(self.path, self.backups, NOW)
            self.assertNotEqual(target, second)
            self.assertTrue(GENERATED.fullmatch(target.name))
            self.assertTrue(target.name.startswith(backup_prefix(self.path)))
            self.assertFalse(Path(str(target) + "-wal").exists())
            with contextlib.closing(
                sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)
            ) as copy:
                self.assertEqual(
                    copy.execute("PRAGMA journal_mode").fetchone()[0], "delete"
                )
                self.assertEqual(
                    copy.execute("SELECT private_chat_id FROM users").fetchall(),
                    [("1",)],
                )
                self.assertEqual(
                    copy.execute("PRAGMA integrity_check").fetchall(), [("ok",)]
                )
                self.assertEqual(
                    copy.execute("PRAGMA foreign_key_check").fetchall(), []
                )
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_restore_new_path_and_refuse_existing_sidecars_or_active_owner(self):
        with open_database(self.path):
            backup = backup_database(self.path, self.backups, NOW)
        target = self.directory / "restored.db"
        with instance_lock(target):
            with self.assertRaises(RuntimeError):
                restore_database(backup, target)
        for suffix in ("", "-wal", "-shm"):
            marker = Path(str(target) + suffix)
            marker.write_bytes(b"KEEP")
            with self.assertRaises(ValueError):
                restore_database(backup, target)
            self.assertEqual(marker.read_bytes(), b"KEEP")
            marker.unlink()
        self.assertEqual(restore_database(backup, target), target)
        with open_database(target) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_backup_refuses_corruption_and_foreign_key_damage_and_removes_partial_file(
        self,
    ):
        self.path.write_bytes(b"not a SQLite file")
        with self.assertRaises(sqlite3.DatabaseError):
            backup_database(self.path, self.backups)
        self.assertEqual(list(self.backups.iterdir()), [])
        self.path.unlink()
        with open_database(self.path) as db:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute(
                "INSERT INTO watchlists (user_id, ins_code, created_at) VALUES ('missing', '123', 0)"
            )
            with self.assertRaisesRegex(ValueError, "integrity"):
                backup_database(self.path, self.backups)
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_no_overwrite_on_generated_name_collision(self):
        with open_database(self.path):
            with patch(
                "trade_alert.backup.uuid4",
                return_value="11111111-1111-4111-8111-111111111111",
            ):
                target = backup_database(self.path, self.backups, NOW)
                original = target.read_bytes()
                with self.assertRaises(FileExistsError):
                    backup_database(self.path, self.backups, NOW)
                self.assertEqual(target.read_bytes(), original)

    def test_prune_requires_confirmation_and_only_removes_old_matching_regular_files(
        self,
    ):
        with open_database(self.path):
            old = backup_database(self.path, self.backups, NOW)
            recent = backup_database(self.path, self.backups, NOW)
        os.utime(old, (0, 0))
        os.utime(recent, (NOW / 1000, NOW / 1000))
        foreign = self.backups / old.name.replace(
            backup_prefix(self.path), "stock-alert-000000000000-"
        )
        foreign.write_bytes(b"KEEP")
        unrelated = self.backups / "notes.db"
        unrelated.write_bytes(b"KEEP")
        # Use another generated UUID for a same-pattern directory.
        folder = self.backups / old.name.replace(
            old.name[-39:-3], "22222222-2222-4222-8222-222222222222"
        )
        folder.mkdir()
        for value in (foreign, unrelated, folder):
            os.utime(value, (0, 0))
        with self.assertRaises(ValueError):
            prune_backups(self.path, self.backups, 14, now=NOW)
        self.assertEqual(
            prune_backups(self.path, self.backups, 14, confirm_prune=True, now=NOW), 1
        )
        for kept in (recent, foreign, unrelated, folder):
            self.assertTrue(kept.exists())

    def test_prune_preserves_links_and_restore_refuses_dangling_link(self):
        with open_database(self.path):
            backup = backup_database(self.path, self.backups, NOW)
        os.utime(backup, (0, 0))
        linked = self.backups / backup.name.replace(
            backup.name[-39:-3], "33333333-3333-4333-8333-333333333333"
        )
        os.link(backup, linked)
        self.assertEqual(
            prune_backups(self.path, self.backups, 1, confirm_prune=True, now=NOW), 0
        )
        dangling = self.directory / "dangling.db"
        try:
            dangling.symlink_to(self.directory / "missing.db")
        except OSError:
            return  # Windows without symlink privileges still checks hard links above.
        with self.assertRaises(ValueError):
            restore_database(backup, dangling)
        linked.unlink()
        linked.symlink_to(backup)
        self.assertEqual(
            prune_backups(self.path, self.backups, 1, confirm_prune=True, now=NOW), 1
        )
        self.assertTrue(linked.is_symlink())

    def test_migration_cli_takes_same_lock_and_needs_no_bot_credentials(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            main(["migrate", str(self.path)])
            with open_database(self.path):
                with self.assertRaises(RuntimeError):
                    main(["migrate", str(self.path)])
            main(["migrate", str(self.path)])

    def test_cli_requires_confirmations_and_container_paths(self):
        with contextlib.redirect_stderr(io.StringIO()):
            for args in (
                ["restore", str(self.path), str(self.directory / "new.db")],
                ["backup", str(self.path), str(self.backups), "--days", "14"],
                ["backup", str(self.path), str(self.backups), "--confirm-prune"],
            ):
                with self.assertRaises(SystemExit):
                    main(args)
        with patch.dict(os.environ, {"STOCK_ALERT_CONTAINER": "true"}):
            with self.assertRaisesRegex(ValueError, "/app/data"):
                main(["migrate", str(self.path)])
        self.assertFalse(self.path.exists())
