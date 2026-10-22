"""Online SQLite backup and explicit restore; never migrates or resumes the source."""

import hashlib
import os
import re
import sqlite3
import stat
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .database import instance_lock
from .users import now_ms
from .validation import integer

GENERATED = re.compile(
    r"stock-alert-[a-f0-9]{12}-\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d-\d{3}Z-[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\.db"
)


def source_path(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute() or not path.is_file():
        raise ValueError("Source must be an existing absolute file path")
    return path.resolve(strict=True)


def backup_prefix(source: str | Path) -> str:
    digest = hashlib.sha256(str(source_path(source)).encode()).hexdigest()[:12]
    return f"stock-alert-{digest}-"


def _copy(source: Path, target: Path) -> None:
    if not target.is_absolute() or not target.parent.is_dir():
        raise ValueError("Target requires an absolute path and existing directory")
    # Exclusive creation refuses existing files and dangling links, including races.
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    try:
        with closing(
            sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5)
        ) as origin:
            if origin.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("Source integrity check failed")
            with closing(sqlite3.connect(target, timeout=5)) as destination:
                origin.backup(destination)
                if (
                    destination.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
                    != "delete"
                ):
                    raise ValueError("Portable backup journal mode failed")
                if (
                    destination.execute("PRAGMA integrity_check").fetchall()
                    != [("ok",)]
                    or destination.execute("PRAGMA foreign_key_check").fetchall()
                ):
                    raise ValueError("Backup integrity check failed")
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def backup_database(
    source: str | Path, directory: str | Path, now: int | None = None
) -> Path:
    source = source_path(source)
    directory = Path(directory)
    if not directory.is_absolute() or not directory.is_dir():
        raise ValueError("Backup directory must already exist and be absolute")
    stamp = (
        datetime.fromtimestamp(
            integer(now_ms() if now is None else now) / 1000, timezone.utc
        )
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
        .replace(":", "-")
        .replace(".", "-")
    )
    target = directory.resolve() / f"{backup_prefix(source)}{stamp}-{uuid4()}.db"
    _copy(source, target)
    return target


def restore_database(source: str | Path, target: str | Path) -> Path:
    source, target = source_path(source), Path(target)
    if not target.is_absolute() or any(
        os.path.lexists(str(target) + suffix) for suffix in ("", "-wal", "-shm")
    ):
        raise ValueError(
            "Restore requires a new absolute path without DB/WAL/SHM files"
        )
    with instance_lock(target) as canonical:
        # Recheck under the same lock used by startup and manual migrations.
        if any(
            os.path.lexists(str(canonical) + suffix) for suffix in ("", "-wal", "-shm")
        ):
            raise ValueError("Restore target already exists")
        _copy(source, canonical)
    return target


def prune_backups(
    source: str | Path,
    directory: str | Path,
    days: int,
    *,
    confirm_prune: bool = False,
    now: int | None = None,
) -> int:
    if confirm_prune is not True:
        raise ValueError("Pruning requires explicit --confirm-prune")
    days = integer(days, 1, 3650)
    directory = Path(directory)
    if not directory.is_absolute() or not directory.is_dir():
        raise ValueError("Backup directory must already exist and be absolute")
    prefix, now = backup_prefix(source), now_ms() if now is None else integer(now)
    removed = 0
    for path in directory.iterdir():
        if not path.name.startswith(prefix) or not GENERATED.fullmatch(path.name):
            continue
        info = path.lstat()
        if (
            stat.S_ISREG(info.st_mode)
            and info.st_nlink == 1
            and now - info.st_mtime_ns // 1_000_000 > days * 86_400_000
        ):
            path.unlink()
            removed += 1
    return removed
