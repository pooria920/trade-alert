"""Immutable offline outputs captured from the former TypeScript implementation.

Inputs must match a captured scenario exactly. Never regenerate expectations from
the Python implementation under test. See fixtures/reference/README.md.
"""

import json
from pathlib import Path


def reference(name, value):
    records = json.loads(
        (Path(__file__).parent / f"fixtures/reference/{name}.json").read_text(
            encoding="utf-8"
        )
    )
    for record in records:
        if record["input"] == value:
            return record["output"]
    raise AssertionError(f"No captured TypeScript scenario matches {name}")
