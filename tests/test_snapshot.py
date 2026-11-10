"""snapshot.read_snapshot: round-trip, the __done__ integrity marker, compact
records and the memory preflight."""
import os

import pytest
from conftest import BUCKET, NAMES, SUBS, S, run_cmd, write_fixtures, write_snapshot

from terra_scrub import analyze
from terra_scrub import snapshot as _snapshot
from terra_scrub.snapshot import read_snapshot


def test_read_snapshot_roundtrip(tmp_path):
    snap, _ = write_fixtures(tmp_path / "rt")
    meta, objs = read_snapshot(snap)
    assert meta["__bucket__"]["name"] == BUCKET, "bucket header parsed"
    assert len(objs) == len(S), f"object count {len(objs)} == {len(S)}"
    assert all(isinstance(o["size"], int) for o in objs), "sizes coerced to int"
    by_name = {o["name"]: o for o in objs}
    assert by_name[NAMES["d10"]]["size"] == 30, "newline name survives round-trip intact"
    assert NAMES["d8"] in by_name and "md5Hash" not in by_name[NAMES["d8"]], \
        "object without md5 preserved without md5 key"


def test_snapshot_integrity(tmp_path):
    # 1) truncated: __done__ marker dropped -> refuse, with a clear message
    snap, _ = write_snapshot(tmp_path / "si1", list(S.values()), SUBS, drop_done=True)
    r = run_cmd(analyze, ["report", snap])
    assert r.returncode != 0, f"truncated (no __done__) refused (rc={r.returncode})"
    assert "__done__" in r.stderr and "truncated" in r.stderr, \
        "refusal message names the missing __done__ marker"
    # 2) __done__ present but count mismatched -> refuse
    snap, _ = write_snapshot(tmp_path / "si2", list(S.values()), SUBS, done_count=len(S) - 3)
    r = run_cmd(analyze, ["report", snap])
    assert r.returncode != 0, "__done__ count mismatch refused"
    assert "n_objects" in r.stderr, "mismatch message shows the __done__ count"
    # 3) malformed JSON line -> refuse with a line number, not a traceback
    snap, _ = write_snapshot(tmp_path / "si3", list(S.values()), SUBS, bad_line_after=2)
    r = run_cmd(analyze, ["report", snap])
    assert r.returncode != 0, "malformed line refused"
    assert "invalid JSON" in r.stderr and ":5:" in r.stderr, \
        "malformed-line error carries the file line number (line 5 = 1 header + 3 objs)"
    # 4) read_snapshot library call on a good file returns everything
    snap, _ = write_fixtures(tmp_path / "si4")
    meta, objs = read_snapshot(snap)
    assert len(objs) == len(S), "valid snapshot with __done__ still parses fully"


def test_snapshot_records_read_like_the_old_dicts(tmp_path):
    """Records are compact SnapObj slots, but keep the dict reads callers use."""
    snap, _ = write_fixtures(tmp_path / "rec")
    _, objs = read_snapshot(snap)
    o = {x["name"]: x for x in objs}[NAMES["d8"]]
    assert o.get("md5Hash") is None and o.get("md5Hash", "dflt") == "dflt", \
        "absent md5 reads as absent through get()"
    with pytest.raises(KeyError):
        o["md5Hash"]
    with pytest.raises(KeyError):
        o["storageClass"]       # kept in the file, not held in memory
    assert o.get("storageClass") is None and "storageClass" not in o
    assert "name" in o and "size" in o


def test_snapshot_too_big_for_ram_is_refused(tmp_path, monkeypatch):
    """Preflight: a snapshot whose estimated footprint exceeds MEM_FRACTION of RAM
    is refused before parsing, unless TERRA_SCRUB_ALLOW_LARGE_SNAPSHOT=1."""
    snap, _ = write_fixtures(tmp_path / "big")
    size = os.path.getsize(snap)
    monkeypatch.delenv(_snapshot.ALLOW_LARGE_ENV, raising=False)
    # RAM such that the file needs just over the 60% limit: 2.6*size > 0.6*ram
    monkeypatch.setattr(_snapshot, "_physical_memory", lambda: int(size * 4))
    with pytest.raises(SystemExit) as e:
        read_snapshot(snap)
    assert "REFUSING" in str(e.value) and _snapshot.ALLOW_LARGE_ENV in str(e.value)
    # just under the limit: 2.6*size < 0.6*(5*size)=3.0*size -> loads
    monkeypatch.setattr(_snapshot, "_physical_memory", lambda: int(size * 5))
    assert len(read_snapshot(snap)[1]) == len(S)
    # override
    monkeypatch.setattr(_snapshot, "_physical_memory", lambda: 1)
    monkeypatch.setenv(_snapshot.ALLOW_LARGE_ENV, "1")
    assert len(read_snapshot(snap)[1]) == len(S)
    # platform will not say -> no refusal
    monkeypatch.delenv(_snapshot.ALLOW_LARGE_ENV)
    monkeypatch.setattr(_snapshot, "_physical_memory", lambda: None)
    assert len(read_snapshot(snap)[1]) == len(S)
