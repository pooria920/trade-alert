"""Explicit offline database CLI; migrations take the application's instance lock."""

import argparse
import os
import sqlite3
from pathlib import Path

from .backup import backup_database, prune_backups, restore_database
from .config import check_container_path
from .database import open_database


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    migrate = commands.add_parser("migrate")
    migrate.add_argument("database", type=Path)
    backup = commands.add_parser("backup")
    backup.add_argument("source", type=Path)
    backup.add_argument("directory", type=Path)
    backup.add_argument("--days", type=int)
    backup.add_argument("--confirm-prune", action="store_true")
    restore = commands.add_parser("restore")
    restore.add_argument("source", type=Path)
    restore.add_argument("target", type=Path)
    restore.add_argument("--confirm-restore", action="store_true", required=True)
    args = parser.parse_args(argv)
    container = os.environ.get("STOCK_ALERT_CONTAINER") == "true"
    if args.operation == "migrate":
        check_container_path(args.database, container)
        with open_database(args.database):
            pass
        print("Migrations applied; application remains stopped")
    elif args.operation == "backup":
        if (args.days is not None) != args.confirm_prune or (
            args.days is not None and not 1 <= args.days <= 3650
        ):
            parser.error("Retention requires --days 1..3650 --confirm-prune")
        check_container_path(args.source, container)
        print(backup_database(args.source, args.directory))
        if args.confirm_prune:
            print(
                f"Expired backups removed: {prune_backups(args.source, args.directory, args.days, confirm_prune=True)}"
            )
    else:
        check_container_path(args.target, container)
        print(restore_database(args.source, args.target))


if __name__ == "__main__":
    try:
        main()
    except OSError, ValueError, RuntimeError, sqlite3.Error:
        raise SystemExit(
            "Database maintenance failed; no application was started"
        ) from None
