"""SQLite startup using the original SQL and Drizzle journal, under its OS lock.

This is a library, not a migration CLI. Callers must finish all workers before
leaving open_database. The original TypeScript migrations remain authoritative.
"""

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """Short synchronous writes; never hold this transaction across an await."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def rows(
    connection: sqlite3.Connection, sql: str, parameters: tuple[object, ...] = ()
) -> list[dict[str, Any]]:
    cursor = connection.execute(sql, parameters)
    cursor.row_factory = sqlite3.Row
    return [dict(row) for row in cursor.fetchall()]


@contextmanager
def instance_lock(database_path: str | Path) -> Iterator[Path]:
    if str(database_path) == ":memory:":
        raise ValueError("An on-disk database is required for the instance lock")
    path = Path(database_path).absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_nlink > 1:
        raise ValueError("Hard-linked database paths are not supported")
    canonical = path.resolve()
    lock = sqlite3.connect(
        str(canonical) + ".instance-lock.sqlite", timeout=0, isolation_level=None
    )
    try:
        try:
            lock.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError:
            raise RuntimeError(
                "Database is already owned by an application or maintenance process"
            ) from None
        yield canonical
    finally:
        lock.close()


def _migrate(connection: sqlite3.Connection, folder: Path) -> None:
    journal = json.loads((folder / "meta/_journal.json").read_text(encoding="utf-8"))
    migrations = []
    for entry in journal["entries"]:
        # Preserve bytes: universal-newline translation would change Drizzle hashes.
        raw = (folder / f"{entry['tag']}.sql").read_bytes()
        migrations.append(
            (entry["when"], hashlib.sha256(raw).hexdigest(), raw.decode("utf-8"))
        )
    connection.execute(
        'CREATE TABLE IF NOT EXISTS "__drizzle_migrations" ( id SERIAL PRIMARY KEY, hash text NOT NULL, created_at numeric )'
    )
    last = connection.execute(
        "SELECT created_at FROM __drizzle_migrations ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    connection.execute("BEGIN IMMEDIATE")
    try:
        for timestamp, digest, sql in migrations:
            if last is not None and last[0] >= timestamp:
                continue
            # executescript implicitly commits; execute each generated breakpoint instead.
            for statement in sql.split("--> statement-breakpoint"):
                if statement.strip():
                    connection.execute(statement)
            connection.execute(
                "INSERT INTO __drizzle_migrations (hash, created_at) VALUES (?, ?)",
                (digest, timestamp),
            )
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


@contextmanager
def open_database(
    path: str | Path, *, migrations: Path = MIGRATIONS
) -> Iterator[sqlite3.Connection]:
    """Lock before migrations, yield the connection, then close before unlocking."""
    if not str(path).strip() or "\0" in str(path) or "://" in str(path):
        raise ValueError("Expected a local database path")
    with instance_lock(path) as canonical:
        connection = sqlite3.connect(canonical, timeout=5, isolation_level=None)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            _migrate(connection, migrations)
            yield connection
        finally:
            connection.close()
