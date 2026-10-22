"""Small JSON-boundary validators; never coerce strings or booleans to numbers."""

import json
import re
from math import isfinite
from typing import Any

MAX_SAFE_INTEGER = 2**53 - 1


def obj(value: object, keys: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or (keys is not None and set(value) != keys):
        raise ValueError("Invalid object fields")
    return value


def number(value: object, minimum: float = 0, maximum: float = float("inf")) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("Expected a finite number")
    try:
        valid = isfinite(value) and minimum <= value <= maximum
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("Number outside permitted range")
    return value


def integer(value: object, minimum: int = 0, maximum: int = MAX_SAFE_INTEGER) -> int:
    n = number(value, minimum, maximum)
    if int(n) != n:
        raise ValueError("Expected an integer")
    return int(n)


def text(value: object, maximum: int, *, trim: bool = True, minimum: int = 1) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected text")
    if trim:
        # ECMAScript trim set; Python strip also removes some control characters.
        value = value.strip(
            "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
        )
    # The installed Zod version counts Unicode code points.
    if not minimum <= len(value) <= maximum:
        raise ValueError("Invalid text length")
    return value


def code(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", value):
        raise ValueError("Invalid instrument code")
    return value


def choice(value: object, choices: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ValueError("Unsupported value")
    return value


def loads(value: str | None) -> Any:
    def reject(value: str) -> None:
        raise ValueError("Non-JSON numeric constant")

    return json.loads(value if value is not None else "null", parse_constant=reject)
