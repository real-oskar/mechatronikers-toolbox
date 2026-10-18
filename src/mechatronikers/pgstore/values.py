"""Values on their way into Postgres: ids, values Postgres cannot hold, JSONB and timestamps.

Postgres stores no NUL character: `\\u0000` fails in JSONB and a NUL byte fails in a text column.
Lone UTF-16 surrogates (`"\\ud800"` in a JSON line) cannot be encoded as UTF-8 at all, and JSONB
has no NaN or Infinity, which `json.loads` and pydantic accept. All of them come from untrusted
lines (container output, reviewer text), so `clean` makes them storable: text gets U+FFFD, the
replacement character, which marks the spot; a non-finite number becomes null, as mode 1 stores it
(pydantic writes JSON null). The caller logs what was replaced, so one bad value never fails a
write.
"""

import hashlib
import math
import re
from datetime import UTC, datetime
from typing import Any

REPLACEMENT = "\ufffd"
_UNSTORABLE = re.compile("[\x00\ud800-\udfff]")
ID = re.compile(r"[A-Za-z0-9._-]{1,100}")  # the `mt_id` domain of 0001_initial.sql
BIGINT = (-(2**63), 2**63 - 1)


def clean_text(value: str) -> str:
    """`value` with NUL and lone surrogates replaced by U+FFFD."""
    return _UNSTORABLE.sub(REPLACEMENT, value)


def clean(value: Any, replaced: list[str], path: str = "") -> Any:
    """A JSON-compatible value that Postgres can store: every string (keys too) without NUL or
    lone surrogates, every non-finite number null. Appends what was replaced, by path, to
    `replaced`. Keys that become equal get a `#2`, `#3`, ... suffix, so no value is dropped."""
    if isinstance(value, str):
        if _UNSTORABLE.search(value) is None:
            return value
        replaced.append(path or "/")
        return clean_text(value)
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        replaced.append(f"{path or '/'} ({value!r} -> null)")
        return None
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = clean(key, replaced, f"{path}/{key!r}") if isinstance(key, str) else key
            if name in out:
                n = 2
                while f"{name}#{n}" in out:
                    n += 1
                replaced.append(f"{path}/{key!r} (collides; kept as {name}#{n})")
                name = f"{name}#{n}"
            out[name] = clean(item, replaced, f"{path}/{name}")
        return out
    if isinstance(value, list):
        return [clean(item, replaced, f"{path}/{i}") for i, item in enumerate(value)]
    return value


def check_id(value: str, what: str = "id") -> str:
    """`value` if it fits the store's id charset, else ValueError (never a database error)."""
    if not isinstance(value, str) or ID.fullmatch(value) is None:
        raise ValueError(f"invalid {what} {str(value)[:120]!r}")
    return value


def is_id(value: str) -> bool:
    return isinstance(value, str) and ID.fullmatch(value) is not None


def lock_key(*parts: str) -> int:
    """A stable signed 32-bit key for `pg_advisory_xact_lock(namespace, key)`."""
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8", "replace")).digest()
    return int.from_bytes(digest[:4], "big", signed=True)


def parse_ts(value: str | None) -> datetime | None:
    """An RFC 3339 timestamp as sent, or None if it does not parse or has no time zone."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def format_ts(value: datetime) -> str:
    """RFC 3339 UTC with microseconds, as everywhere else in the system."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
