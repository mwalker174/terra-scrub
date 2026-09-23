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
