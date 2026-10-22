"""File-based health, independent collector progress and an actual SQLite write."""

import json
import os
import sqlite3
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .users import now_ms
from .validation import integer, loads, obj

HEALTH_FILE = Path(tempfile.gettempdir()) / "tse-stock-alert-python-health.json"


def process_alive(pid: int) -> bool:
    if os.name == "nt":
        # os.kill(pid, 0) is not a portable Windows liveness probe.
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(
                kernel.GetExitCodeProcess(handle, ctypes.byref(code))
                and code.value == 259
            )
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def runtime_health(
    status: dict[str, Any],
    initialized: bool,
    database_ok: bool,
    last_success: int | None,
    telegram_errors: int,
    request_timeout_ms: int,
    now: int,
) -> dict[str, Any]:
    failures = [s for s in status["symbols"] if s["last_result"] == "transport-error"]
    market = (
        "MARKET_CLOSED"
        if not status["is_market_open"]
        else "NO_MONITORED_SYMBOLS"
        if not status["monitored_symbols"]
        else "TSETMC_PERSISTENT_FAILURE"
        if any(s["consecutive_errors"] >= 5 for s in failures)
        else "TSETMC_TEMPORARY_FAILURE"
        if failures
        else "FRESH_DATA"
        if status["fresh_symbols"]
        else "DATA_UNVERIFIED_OR_STALE"
    )
    telegram = (
        "STARTING"
        if not initialized
        else "CONNECTION_FAILURE"
        if last_success is None or not 0 <= now - last_success <= 120_000
        else "TEMPORARY_FAILURE"
        if telegram_errors
        else "CONNECTED"
    )
    return dict(
        pid=os.getpid(),
        timestamp=now,
        initialized=initialized,
        database_ok=database_ok,
        monitor_running=status["is_running"],
        monitor_progress_at=status["last_progress_at"],
        monitor_max_silence_ms=status["poll_interval_ms"] + request_timeout_ms + 30_000,
        last_telegram_success=last_success,
        market=market,
        telegram=telegram,
    )


def database_probe(db: sqlite3.Connection, now: int) -> bool:
    try:
        db.execute(
            "INSERT INTO application_state (key, value, updated_at) VALUES ('database_health', 'ok', ?) ON CONFLICT(key) DO UPDATE SET value='ok', updated_at=excluded.updated_at",
            (now,),
        )
        return True
    except sqlite3.Error:
        return False


def write_heartbeat(value: dict[str, Any], path: Path = HEALTH_FILE) -> None:
    # Same-directory replacement: readers see a complete old or new heartbeat.
    path = Path(path)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=path.name + ".",
        delete=False,
    ) as output:
        temporary = Path(output.name)
        try:
            json.dump(value, output)
        except BaseException:
            output.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def health_report(
    path: Path = HEALTH_FILE,
    now: int | None = None,
    alive: Callable[[int], bool] = process_alive,
) -> dict[str, Any]:
    try:
        value = obj(loads(Path(path).read_text(encoding="utf-8")))
        pid = integer(value["pid"], 1)
        timestamp = integer(value["timestamp"])
        silence = integer(value["monitor_max_silence_ms"], 1)
        for key in ("initialized", "database_ok", "monitor_running"):
            if type(value[key]) is not bool:
                raise ValueError("Invalid health flag")
        progress, poll = value["monitor_progress_at"], value["last_telegram_success"]
        if progress is not None:
            integer(progress)
        if poll is not None:
            integer(poll)
        now = now_ms() if now is None else now
        reasons = []
        if not alive(pid):
            reasons.append("PROCESS_NOT_RUNNING")
        if not 0 <= now - timestamp <= 90_000:
            reasons.append("STALE_HEARTBEAT")
        if not value["initialized"]:
            reasons.append("STARTING")
        if not value["database_ok"]:
            reasons.append("DATABASE_FAILURE")
        if (
            not value["monitor_running"]
            or progress is None
            or not 0 <= now - progress <= silence
        ):
            reasons.append("MONITOR_STALLED")
        if poll is None or not 0 <= now - poll <= 120_000:
            reasons.append("TELEGRAM_CONNECTION_FAILURE")
        return dict(
            healthy=not reasons,
            reasons=reasons,
            market=value["market"],
            telegram=value["telegram"],
            database="OK" if value["database_ok"] else "FAILURE",
        )
    except OSError, ValueError, TypeError, KeyError, OverflowError:
        return dict(
            healthy=False,
            reasons=["HEALTH_UNAVAILABLE"],
            market="UNKNOWN",
            telegram="UNKNOWN",
            database="UNKNOWN",
        )


if __name__ == "__main__":
    result = health_report()
    print(json.dumps(result))
    raise SystemExit(0 if result["healthy"] else 1)
