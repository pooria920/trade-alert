"""Environment validation; receives a mapping, never reads or logs os.environ."""

import base64
import binascii
import re
from collections.abc import Mapping
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .evaluators import MAX_SAFE_INTEGER
from .schedule import calendar_date, parse_sessions
from .validation import choice, integer, loads, text

# Same names/defaults as the reference, allowing an explicit later cutover.
NUMBERS = {
    "TSETMC_REQUEST_TIMEOUT_MS": (5000, 1, 120_000),
    "TSETMC_MAX_RETRIES": (2, 0, 3),
    "MARKET_DATA_MAX_AGE_MS": (30_000, 1, 300_000),
    "MARKET_POLL_INTERVAL_MS": (10_000, 1000, MAX_SAFE_INTEGER),
    "MARKET_MAX_CONCURRENT_REQUESTS": (3, 1, 10),
    "MARKET_REQUEST_TIMEOUT_MS": (5000, 1, 120_000),
    "SNAPSHOT_HISTORY_TOLERANCE_SECONDS": (20, 1, 300),
    "SNAPSHOT_CLEANUP_INTERVAL_MS": (900_000, 60_000, 86_400_000),
    "SNAPSHOT_RETENTION_HOURS": (24, 1, 8760),
    "ALERT_DEFAULT_COOLDOWN_SECONDS": (300, 0, 86_400),
    "ALERT_HISTORY_LIMIT": (10, 1, 10),
    "NOTIFICATION_MAX_RETRIES": (5, 0, 10),
    "NOTIFICATION_RETRY_BASE_MS": (2000, 1, 300_000),
    "NOTIFICATION_MAX_AGE_MINUTES": (15, 1, 1440),
}


def mtproxy(value: str) -> tuple[str, int, str] | None:
    """Parse a Telegram MTProxy link locally; never resolve or connect here."""
    if not value:
        return None
    try:
        if len(value) > 2048 or re.search(r"\s", value):
            raise ValueError
        link = urlsplit(value)
        if link.fragment or not (
            (link.scheme == "tg" and link.netloc == "proxy" and not link.path)
            or (
                link.scheme == "https"
                and link.netloc == "t.me"
                and link.path == "/proxy"
            )
        ):
            raise ValueError
        query = parse_qs(
            link.query, keep_blank_values=True, strict_parsing=True, max_num_fields=3
        )
        if set(query) != {"server", "port", "secret"} or any(
            len(v) != 1 for v in query.values()
        ):
            raise ValueError
        host, port, secret = (query[k][0] for k in ("server", "port", "secret"))
        try:
            ip_address(host)
        except ValueError:
            host = host.encode("idna").decode("ascii")
            if len(host) > 253 or not all(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in host.removesuffix(".").split(".")
            ):
                raise ValueError
        if not re.fullmatch(r"[1-9][0-9]{0,4}", port):
            raise ValueError
        if not re.fullmatch(
            r"(?:[dD]{2})?[a-fA-F0-9]{32}|[eE]{2}[a-fA-F0-9]{34,538}", secret
        ):
            if not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", secret):
                raise ValueError
            decoded = base64.b64decode(
                secret + "=" * (-len(secret) % 4), altchars=b"-_", validate=True
            )
            if base64.urlsafe_b64encode(decoded).decode().rstrip("=") != secret.rstrip(
                "="
            ):
                raise ValueError
            secret = decoded.hex()
        if re.fullmatch(r"[eE]{2}[a-fA-F0-9]{34,538}", secret):
            decoded = bytes.fromhex(secret)
            domain = decoded[17:].decode("ascii")
            # Fixed 517-byte browser ClientHello leaves 220 bytes for SNI.
            if len(domain) > 220 or not all(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in domain.split(".")
            ):
                raise ValueError
        elif not re.fullmatch(r"(?:[dD]{2})?[a-fA-F0-9]{32}", secret):
            raise ValueError
        return host, integer(int(port), 1, 65535), secret.lower()
    except ValueError, TypeError, UnicodeError, binascii.Error:
        raise ValueError("Invalid or unsupported TELEGRAM_MTPROXY_URL") from None


def channel_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"-[1-9][0-9]{0,15}", value)
        or int(value) < -(2**52 - 1)
        or int(value) == -(10**12)
    ):
        raise ValueError("Invalid channel ID")
    return value


def user_ids(value: str, *, optional: bool = False) -> list[str]:
    if optional and not value.strip():
        return []
    ids = [item.strip() for item in value.split(",")]
    if any(
        not re.fullmatch(r"[1-9][0-9]{0,15}", item) or int(item) > 2**52 - 1
        for item in ids
    ):
        raise ValueError("Invalid user IDs")
    return list(dict.fromkeys(ids))


def check_container_path(path: str | Path, container: bool) -> None:
    if container:
        root, resolved = Path("/app/data").resolve(), Path(path).resolve()
        if resolved == root or not resolved.is_relative_to(root):
            raise ValueError(
                "Container DATABASE_URL must remain below /app/data for persistent storage"
            )


def parse_env(source: Mapping[str, str]) -> dict[str, Any]:
    """Return validated settings. Errors contain field names only, never values."""
    if "TELEGRAM_ALLOWED_USER_ID" in source:
        raise ValueError(
            "Replace TELEGRAM_ALLOWED_USER_ID with TELEGRAM_ALLOWED_USER_IDS"
        )
    values: dict[str, Any] = {}
    field = "NODE_ENV"
    try:
        values[field] = choice(
            source.get(field, "development"), ("development", "test", "production")
        )
        field = "TELEGRAM_BOT_TOKEN"
        token = source[field].strip()
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{30,}", token):
            raise ValueError("Invalid token")
        values[field] = token
        field = "TELEGRAM_API_ID"
        raw_id = source[field].strip()
        if not re.fullmatch(r"[1-9][0-9]{0,9}", raw_id):
            raise ValueError("Invalid Telegram API ID")
        values[field] = integer(int(raw_id), 1, 2**31 - 1)
        field = "TELEGRAM_API_HASH"
        api_hash = source[field].strip()
        if not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash):
            raise ValueError("Invalid Telegram API hash")
        values[field] = api_hash
        field = "TELEGRAM_MTPROXY_URL"
        values[field] = source.get(field, "").strip()
        mtproxy(values[field])
        field = "TELEGRAM_PROXY_AUTO"
        values[field] = choice(source.get(field, "false"), ("true", "false")) == "true"
        field = "TELEGRAM_ALLOWED_USER_IDS"
        values[field] = user_ids(source[field])
        field = "TELEGRAM_ADMIN_USER_IDS"
        values[field] = user_ids(source.get(field, ""), optional=True)
        field = "TELEGRAM_CHANNEL_ID"
        channel = source.get(field, "")
        if channel:
            channel_id(channel)
        values[field] = channel
        if bool(channel) != bool(values["TELEGRAM_ADMIN_USER_IDS"]):
            raise ValueError("Configure channel and admins together")
        field = "TSETMC_BASE_URL"
        url = source.get(field, "https://cdn.tsetmc.com/api")
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or any(c.isspace() for c in parts.netloc)
        ):
            raise ValueError("Invalid HTTPS URL")
        _ = parts.port
        values[field] = url
        for field, (default, minimum, maximum) in NUMBERS.items():
            raw = source.get(field)
            # Match JS Number for ordinary decimal, exponent and unsigned radix syntax.
            if raw is None:
                n: int | float = default
            elif not raw.strip():
                n = 0
            elif re.fullmatch(r"0[xX][0-9a-fA-F]+|0[bB][01]+|0[oO][0-7]+", raw.strip()):
                n = int(raw.strip(), 0)
            elif re.fullmatch(
                r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?",
                raw.strip(),
            ):
                n = float(raw)
            else:
                raise ValueError("Invalid numeric setting")
            values[field] = integer(n, minimum, maximum)
        field = "MARKET_MONITOR_FORCE_ACTIVE"
        values[field] = choice(source.get(field, "false"), ("true", "false")) == "true"
        field = "MARKET_SESSIONS_JSON"
        sessions = parse_sessions(loads(source.get(field, "[]")))
        values[field] = [
            dict(market=s.market, days=list(s.days), start=s.start, end=s.end)
            for s in sessions
        ]
        field = "MARKET_CLOSED_DATES"
        raw = source.get(field, "")
        values[field] = (
            [calendar_date(d.strip()) for d in raw.split(",")] if raw.strip() else []
        )
        field = "MARKET_SESSION_MARKET"
        values[field] = text(source.get(field, "equities"), 50)
        field = "DATABASE_URL"
        path = source.get(field, "./data/stock-alert.db").strip()
        if not path or "\0" in path or "://" in path:
            raise ValueError("Invalid local database path")
        values[field] = path
        field = "LOG_LEVEL"
        values[field] = choice(
            source.get(field, "info"),
            ("fatal", "error", "warn", "info", "debug", "trace", "silent"),
        )
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        OverflowError,
        RecursionError,
    ):
        raise ValueError(f"Invalid environment variables: {field}") from None
    if not set(values["TELEGRAM_ADMIN_USER_IDS"]).issubset(
        values["TELEGRAM_ALLOWED_USER_IDS"]
    ):
        raise ValueError(
            "TELEGRAM_ADMIN_USER_IDS must be authorized in TELEGRAM_ALLOWED_USER_IDS"
        )
    if values["NODE_ENV"] == "production" and (
        values["MARKET_MONITOR_FORCE_ACTIVE"]
        or values["DATABASE_URL"] == ":memory:"
        or source.get("DEBUG", "").strip()
    ):
        raise ValueError(
            "Production forbids schedule bypass, in-memory databases and DEBUG request logging"
        )
    if values["MARKET_MONITOR_FORCE_ACTIVE"] and values["NODE_ENV"] != "test":
        raise ValueError("MARKET_MONITOR_FORCE_ACTIVE is test-only")
    check_container_path(
        values["DATABASE_URL"], source.get("STOCK_ALERT_CONTAINER") == "true"
    )
    return values
