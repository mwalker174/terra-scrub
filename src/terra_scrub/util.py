"""Small pure helpers shared across terra-scrub modules (no I/O, no network)."""
from __future__ import annotations

from datetime import datetime


def human(n):
    """Bytes -> '1.23 TiB' (FiSS-style units)."""
    units = ["bytes", "KiB", "MiB", "GiB", "TiB", "PiB"]
    n = float(n)
    i = 0
    while n >= 1024.0 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    return f"{n:.2f} {units[i]}" if i else f"{int(n)} bytes"


def duration(sec):
    """Seconds -> '0.4s' / '5m12s' / '2h03m'."""
    sec = float(sec)
    if sec < 60:
        return f"{sec:.1f}s"
    m, s = divmod(round(sec), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def step_times(secs, order=()):
    """run.json step_seconds -> 'snapshot 5m12s, context 41.0s, ... (total 6m03s)',
    steps in ``order`` first, anything else after them."""
    if not secs:
        return ""
    rank = {k: i for i, k in enumerate(order)}
    keys = sorted(secs, key=lambda k: rank.get(k, len(rank)))
    parts = ", ".join(f"{k} {duration(secs[k])}" for k in keys)
    return f"{parts} (total {duration(sum(secs.values()))})"


def parse_ts(s):
    """ISO-8601 (incl. trailing 'Z') -> aware datetime; None if empty/unparseable."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def tsv_escape(s):
    """Escape control chars so a name-first TSV stays one-object-per-line
    (GCS object names may legally contain newlines/tabs)."""
    return (str(s).replace("\\", "\\\\").replace("\t", "\\t")
            .replace("\n", "\\n").replace("\r", "\\r"))
