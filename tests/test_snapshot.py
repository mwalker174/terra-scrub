"""snapshot.read_snapshot: round-trip and the __done__ integrity marker."""
from conftest import BUCKET, NAMES, SUBS, S, run_cmd, write_fixtures, write_snapshot

from terra_scrub import analyze
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
