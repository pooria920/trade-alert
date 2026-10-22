"""Small structured logger; only operational fields enter output, never raw errors."""

import base64
import json
import logging
import re
import sys
from typing import TextIO

from .users import now_ms


def create_logger(
    level: str = "info",
    token: str = "",
    stream: TextIO | None = None,
    *,
    api_hash: str = "",
    proxy_secret: str = "",
) -> logging.Logger:
    class SafeJSON(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            message = record.getMessage()
            if token:
                message = message.replace(token, "[REDACTED]")
            if api_hash:
                message = message.replace(api_hash, "[REDACTED]")
            if proxy_secret:
                variants = [
                    proxy_secret,
                    proxy_secret[2:34] if len(proxy_secret) > 32 else proxy_secret,
                ]
                for secret in variants:
                    message = re.sub(
                        re.escape(secret), "[REDACTED]", message, flags=re.I
                    )
                    try:
                        decoded = bytes.fromhex(secret)
                    except ValueError:
                        continue
                    encoded = base64.urlsafe_b64encode(decoded).decode().rstrip("=")
                    message = message.replace(encoded, "[REDACTED]")
            message = re.sub(r"\d+:[A-Za-z0-9_-]{30,}", "[REDACTED]", message)
            value = dict(time=now_ms(), level=record.levelname.lower(), message=message)
            for key in (
                "rule_id",
                "code",
                "attempts",
                "removed",
                "is_market_open",
                "signal",
            ):
                item = getattr(record, key, None)
                if type(item) in (int, bool) or item is None:
                    value[key] = item
                elif (
                    key == "rule_id"
                    and isinstance(item, str)
                    and re.fullmatch(r"[a-fA-F0-9-]{36}", item)
                ):
                    value[key] = item
                elif key == "signal" and item in ("SIGINT", "SIGTERM"):
                    value[key] = item
            return json.dumps(value, ensure_ascii=False)

    logger = logging.Logger(
        "trade_alert",
        dict(trace=10, debug=10, info=20, warn=30, error=40, fatal=50, silent=100)[
            level
        ],
    )
    logger.propagate = False
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(SafeJSON())
    logger.addHandler(handler)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).disabled = True
    return logger
