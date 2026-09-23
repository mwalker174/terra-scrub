"""verify: the post-delete proof, with GCS replaced by an in-memory bucket.

`verify._authed_session` returns a fake session whose `.get(url, timeout=...)` answers
object stats and soft-deleted listings from dicts; `snapshot.snapshot_bucket` (the
in-process after-listing) writes a fixture after-snapshot in `read_snapshot` format.
NO NETWORK. The plan under verification is built by the REAL `plan` command.

Hand-computed from the conftest fixture: the delete list is {d1a, d1c, d2b, d7b, d12b}
(970 B); their keepers are d1b (for d1a, d1c), d2a (d2b), d7a (d7b), d12a (d12b); the
context references d6 (live) and cram_crai/ghost.cram (absent before AND after, so a
pre-existing dangling pointer, not a failure).
"""
import json
import os
import shutil
import urllib.parse

import pytest
from conftest import BUCKET, EXPECTED_DELETE, NAMES, S, make_plan_inputs, run_cmd

from terra_scrub import http as _http
from terra_scrub import snapshot as _snapshot
from terra_scrub import verify

BASE = f"{_http.GCS_API}/b/{BUCKET}/o"


class _Resp:
    def __init__(self, code, body=None):
        self.status_code = code
        self._body = body
        self.text = json.dumps(body) if body is not None else "not found"

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeGCS:
    """live: name -> (size, md5); soft: name -> md5 of its soft-deleted copy."""

    def __init__(self, live, soft):
        self.live, self.soft, self.calls = dict(live), dict(soft), []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, timeout))
        if url.startswith(BASE + "?"):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            assert q.get("softDeleted") == ["true"]
            pre = q["prefix"][0]
            return _Resp(200, {"items": [
                {"name": n, "md5Hash": m, "generation": "1",
                 "hardDeleteTime": "2026-09-30T00:00:00Z"}
                for n, m in self.soft.items() if n.startswith(pre)]})
        assert url.startswith(BASE + "/"), url
        name = urllib.parse.unquote(url[len(BASE) + 1:].split("?", 1)[0])
        if name in self.live:
            size, md5 = self.live[name]
            return _Resp(200, {"name": name, "size": str(size), "md5Hash": md5})
        return _Resp(404)


def _objs(keys):
    return [S[k] for k in sorted(keys)]


def _write_snapshot(path, objs):
    """An after-snapshot exactly as read_snapshot expects it (see conftest.write_snapshot)."""
    with open(path, "w") as f:
        f.write(json.dumps({"__bucket__": {"id": BUCKET, "name": BUCKET},
                            "snapshot_utc": "2026-09-23T00:00:00+00:00",
                            "source": "fixture"}, sort_keys=True) + "\n")
        for name, size, md5, upd in objs:
            rec = {"name": name, "size": size, "updated": upd}
            if md5:
                rec["md5Hash"] = md5
            f.write(json.dumps(rec, sort_keys=True) + "\n")
        f.write(json.dumps({"__done__": True, "n_objects": len(objs),
                            "total_bytes": sum(o[1] for o in objs)}, sort_keys=True) + "\n")


@pytest.fixture
def world(tmp_path, run_plan_with_stub, monkeypatch, live):
    """Build a real plan, then fake the bucket AFTER its wrapper ran.

    `world.set(deleted=..., after=...)` describes the post-delete bucket: `deleted`
    are gone from live GCS and have a soft-deleted copy; `after` (default: the same)
    is what the after-listing returns."""
    class W:
        pass
    w = W()
    w.tmp = tmp_path
    w.relist_calls = []
    w.relist_error = None
    w.session_made = []

    def build(plan_live=None):
        if plan_live:
            live.update(plan_live)
        tsv, fresh = make_plan_inputs(tmp_path / "v")
        r = run_plan_with_stub("--manifest", tsv, "--terra", fresh, "--workers", "4")
        assert r.returncode == 0, r.stderr
        w.tsv, w.fresh, w.plan_json = tsv, fresh, tsv + ".plan.json"
        w.before = str(tmp_path / "v" / "gen" / "snap.jsonl")
        return w

    def set_(deleted, after_gone=None, still_live=()):
        after_gone = set(deleted if after_gone is None else after_gone)
        w.gcs = FakeGCS(
            live={S[k][0]: (S[k][1], S[k][2] or "")
                  for k in S if k not in deleted or k in still_live},
            soft={S[k][0]: S[k][2] for k in deleted if k not in still_live})
        w.after_objs = _objs(set(S) - after_gone)
        return w

    def fake_snapshot_bucket(bucket, out_path, page_size=1000, session=None):
        w.relist_calls.append(out_path)
        if w.relist_error:
            raise w.relist_error
        _write_snapshot(out_path, w.after_objs)
        return len(w.after_objs), sum(o[1] for o in w.after_objs)

    def fake_session():
        w.session_made.append(True)
        return w.gcs

    w.build, w.set = build, set_
    monkeypatch.setattr(verify, "_authed_session", fake_session)
    monkeypatch.setattr(_snapshot, "snapshot_bucket", fake_snapshot_bucket)
    return w


def _verify(w, *extra):
    return run_cmd(verify, ["--plan", w.plan_json, "--before-snapshot", w.before,
                            "--workers", "4", *extra])


def test_happy_path(world):
    w = world.build().set(deleted=EXPECTED_DELETE)
    r = _verify(w)
    assert r.returncode == 0, r.stdout[-1500:]
    assert "ALL CHECKS PASS" in r.stdout
    assert "URI list (5) == plan_objects (5)" in r.stdout, "planned set comes from the URI list"
    assert "all 5 planned objects are absent" in r.stdout
    assert "all 4 surviving keepers live" in r.stdout
    assert "gone=5" in r.stdout and "UNPLANNED=0" in r.stdout
    assert "bytes lost = 970" in r.stdout
    assert "soft-deleted copies exist with matching md5 for 5/5" in r.stdout
    side = w.tsv + ".dangling_refs.txt"
    with open(side) as f:
        assert f"gs://{BUCKET}/cram_crai/ghost.cram" in f.read(), \
            "the pre-existing dangling pointer is documented, not failed"
    assert w.relist_calls == [str(w.tmp / "v" / "gen" / "snap.after.jsonl")], \
        "default after-path for <x>.jsonl is <x>.after.jsonl"
    assert w.gcs.calls and all(t for _, t in w.gcs.calls), "every stat GET carries a timeout"


def test_planned_object_still_present(world):
    w = world.build().set(deleted=EXPECTED_DELETE, after_gone=EXPECTED_DELETE - {"d1a"},
                          still_live={"d1a"})
    r = _verify(w)
    assert r.returncode == 1
    assert "[FAIL] all 5 planned objects are absent (1 still live" in r.stdout, r.stdout[-1500:]
    assert "planned-but-present=1" in r.stdout


def test_collateral_loss(world):
    # d5 (40 B, never a candidate) vanished too
    w = world.build().set(deleted=EXPECTED_DELETE | {"d5"})
    r = _verify(w)
    assert r.returncode == 1
    assert "[PASS] all 5 planned objects are absent" in r.stdout
    assert "[FAIL] exactly the planned set disappeared (gone=6, planned=5, UNPLANNED=1" \
        in r.stdout, r.stdout[-1500:]
    assert "[FAIL] bytes lost = 1,010 (expected 970)" in r.stdout


def test_relist_failure_fails_check3_and_skips_after(world, monkeypatch):
    w = world.build().set(deleted=EXPECTED_DELETE)
    w.relist_error = RuntimeError("boom")
    after = str(w.tmp / "v" / "gen" / "snap.after.jsonl")
    _write_snapshot(after, w.after_objs)       # a plausible stale file sits at the after path
    read = []
    real_read = _snapshot.read_snapshot

    def rec_read(path, *a, **k):
        read.append(os.path.abspath(path))
        return real_read(path, *a, **k)
    monkeypatch.setattr(_snapshot, "read_snapshot", rec_read)
    r = _verify(w)
    assert r.returncode == 1
    assert "[FAIL] re-listed the bucket (rc=1) RuntimeError: boom" in r.stdout, r.stdout[-1500:]
    assert os.path.abspath(after) not in read, "a failed re-list never consults the after file"
    assert "exactly the planned set disappeared" not in r.stdout
    assert "no before-listing, so none can be classed pre-existing" in r.stdout, \
        "without a before-listing every missing reference fails"


def test_after_equals_before_is_refused(world, tmp_path):
    w = world.build().set(deleted=EXPECTED_DELETE)
    with open(w.before, "rb") as f:
        orig = f.read()
    r = _verify(w, "--after-snapshot", w.before)
    assert r.returncode != 0 and "REFUSING" in r.stderr and "before-snapshot" in r.stderr, \
        r.stderr[-400:]
    # the same file under another spelling / via a symlink is the same file
    link = str(tmp_path / "alias.jsonl")
    os.symlink(w.before, link)
    r2 = _verify(w, "--after-snapshot", link)
    assert r2.returncode != 0 and "REFUSING" in r2.stderr
    r3 = _verify(w, "--after-snapshot",
                 os.path.join(os.path.dirname(w.before), ".", os.path.basename(w.before)))
    assert r3.returncode != 0 and "REFUSING" in r3.stderr
    assert w.relist_calls == [] and w.session_made == [], "refused before any GCS call"
    with open(w.before, "rb") as f:
        assert f.read() == orig, "the before-snapshot is untouched"


def test_default_after_path_derivation(world, tmp_path):
    assert verify.default_after("/r/inv/k.jsonl") == "/r/inv/k.after.jsonl"
    assert verify.default_after("/r/inv/k") == "/r/inv/k.after.jsonl"
    assert verify.default_after("/r/inv/k.json") == "/r/inv/k.after.json"
    w = world.build().set(deleted=EXPECTED_DELETE)
    noext = str(tmp_path / "before-snap")
    shutil.copyfile(w.before, noext)
    with open(noext, "rb") as f:
        orig = f.read()
    r = run_cmd(verify, ["--plan", w.plan_json, "--before-snapshot", noext, "--workers", "4"])
    assert r.returncode == 0, r.stdout[-1500:]
    assert w.relist_calls == [noext + ".after.jsonl"], \
        f"a before path with no .jsonl gets a distinct after path ({w.relist_calls})"
    with open(noext, "rb") as f:
        assert f.read() == orig


def test_planned_set_comes_from_uri_list(world):
    # at plan time d7b's bytes changed -> SKIP_MD5_CHANGED: it stays on the MANIFEST
    # but not on the URI list, and the wrapper never deleted it
    w = world.build(plan_live={NAMES["d7b"]: ("ok", {"size": str(S["d7b"][1]),
                                                       "md5Hash": "md5ZZZ"})})
    with open(w.plan_json) as f:
        p = json.load(f)
    assert p["plan_objects"] == 4 and p["plan_bytes"] == 470
    deleted = EXPECTED_DELETE - {"d7b"}
    w.set(deleted=deleted)
    r = _verify(w)
    assert r.returncode == 0, r.stdout[-1500:]
    assert "URI list (4) == plan_objects (4)" in r.stdout
    assert "all 4 planned objects are absent" in r.stdout
    assert "gone=4, planned=4, UNPLANNED=0" in r.stdout and "bytes lost = 470" in r.stdout

    # plan.json without a URI list -> fall back to manifest rows (5), which now
    # (rightly) disagrees with what was planned
    p["uris_out"] = None
    with open(w.plan_json, "w") as f:
        json.dump(p, f)
    r2 = _verify(w)
    assert r2.returncode == 1
    assert "[FAIL] manifest rows (5) == plan_objects (4)" in r2.stdout, r2.stdout[-1500:]
    assert "keepers" in r2.stdout, "keepers still come from the manifest"
