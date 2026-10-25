from __future__ import annotations

import hashlib
import json
import statistics
import time
from datetime import datetime, timezone


def canonical(value) -> str:
    """Matches canonical() in web/app.js: sorted keys, compact separators."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(value) -> str:
    text = value if isinstance(value, str) else canonical(value)
    return hashlib.sha256(text.encode()).hexdigest()


def iso(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def percentile(values, p: float):
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def median(values):
    return statistics.median(values) if values else None


def threshold(text, default: float) -> float:
    """'>= 35' -> 35.0"""
    try:
        return float("".join(ch for ch in str(text) if ch.isdigit() or ch == "."))
    except ValueError:
        return default
