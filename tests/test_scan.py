"""scan: one command from ns/ws to a reviewed plan. Offline: every network call faked.

The fakes write the shared conftest fixture (so the hand-computed expectations
apply: delete list 5 objects / 970 bytes, review list 4 rows / 550 bytes) and
record the order in which scan calls each step.
"""
import json
import os
import shutil
from datetime import UTC, datetime

import pytest
from conftest import (
    BUCKET,
    EXPECTED_DELETE,
    EXPECTED_DELETE_BYTES,
    EXPECTED_PROTECTED,
    EXPECTED_PROTECTED_BYTES,
    REFERENCED,
    SUBS,
    TOTAL_BYTES,
    S,
    fake_live_stat_factory,
    run_cmd,
    write_snapshot,
)

from terra_scrub import candidates, http, plan, runs, scan, snapshot, terra


class _Resp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def install_fakes(monkeypatch, tmp_path, calls=None, *, bucket_name=BUCKET, ctx_bucket=None):
    """Replace every network touchpoint scan has; returns the call-order list.

    Snapshot and contexts are stamped NOW at call time, so the real ordering guards
    in candidates/plan see exactly the order scan produced."""
    calls = [] if calls is None else calls
    home = tmp_path / "home"
    monkeypatch.setenv("TERRA_SCRUB_HOME", str(home))
    monkeypatch.setattr(http, "_authed_session", lambda: None)
    monkeypatch.setattr(plan, "_authed_session", lambda: None)
    monkeypatch.setattr(plan, "live_stat", fake_live_stat_factory({}))

    def fake_api_get(url, params=None, session=None, *, timeout=120):
        calls.append("resolve")
        assert "/api/workspaces/" in url and params == {"fields": "workspace.bucketName"}
        return _Resp({"workspace": {"bucketName": bucket_name} if bucket_name else {}})

    def fake_snapshot_bucket(bucket, out_path, page_size=1000, session=None):
        calls.append("snapshot")
        assert bucket == BUCKET
        src = tmp_path / "fixture-snap"
        snap, _ = write_snapshot(src, list(S.values()), SUBS,
                                 snapshot_utc=datetime.now(UTC).isoformat())
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        shutil.copyfile(snap, out_path)
        return len(S), TOTAL_BYTES

    def fake_capture_context(ns, ws, out, session=None):
        calls.append("plan-context" if out.endswith(".plan-time.terra.json") else "context")
        ctx = {"namespace": ns, "workspace": ws, "bucket": ctx_bucket or BUCKET,
               "isLocked": False, "captured_utc": datetime.now(UTC).isoformat(),
               "workspace_attributes": {}, "entity_counts": {},
               "referenced_gs_uris": list(REFERENCED), "n_referenced": len(REFERENCED),
               "submissions": SUBS}
        with open(out, "w") as f:
            json.dump(ctx, f)
        return ctx

    real_cand, real_plan = candidates.main, plan.main

    def rec_cand(argv=None):
        calls.append("candidates")
        return real_cand(argv)

    def rec_plan(argv=None):
        calls.append("plan")
        return real_plan(argv)

    monkeypatch.setattr(http, "api_get", fake_api_get)
    monkeypatch.setattr(snapshot, "snapshot_bucket", fake_snapshot_bucket)
    monkeypatch.setattr(terra, "capture_context", fake_capture_context)
    monkeypatch.setattr(candidates, "main", rec_cand)
    monkeypatch.setattr(plan, "main", rec_plan)
    return calls


def scripted_scan(monkeypatch, tmp_path, *extra, target="ns/ws", **kw):
    """Install the fakes and run `scan target *extra`. Returns (result, calls, run)."""
    calls = install_fakes(monkeypatch, tmp_path, **kw)
    res = run_cmd(scan, [target, *extra])
    ns, ws = runs.parse_target(target)
    return res, calls, runs.latest_run(ns, ws)


def test_scan_end_to_end_order_files_and_summary(monkeypatch, tmp_path):
    res, calls, r = scripted_scan(monkeypatch, tmp_path)
    assert res.returncode == 0, res.stderr + res.stdout
    # the ordering invariant (docs/SAFETY.md §2), by construction
    assert calls == ["resolve", "snapshot", "context", "candidates", "plan-context", "plan"]
    assert r.root.startswith(str(tmp_path / "home" / "runs" / "ns" / "ws"))
    for p in (r.snapshot, r.context, r.plan_context, r.manifest, r.protected,
              r.plan_json, r.plan_uris, r.wrapper, r.log("candidates"), r.log("plan")):
        assert os.path.exists(p), p
    meta = r.read_meta()
    with open(r.plan_json) as f:
        p = json.load(f)
    assert meta["outcome"] == "planned"
    assert meta["plan_id"] == p["plan_id"] and p["executable"] is True
    assert meta["bucket"] == BUCKET
    assert (meta["plan_objects"], meta["plan_bytes"]) == (len(EXPECTED_DELETE),
                                                          EXPECTED_DELETE_BYTES)
    assert (meta["candidate_objects"], meta["candidate_bytes"]) == (len(EXPECTED_DELETE),
                                                                    EXPECTED_DELETE_BYTES)
    assert (meta["protected_rows"], meta["protected_bytes"]) == (len(EXPECTED_PROTECTED),
                                                                 EXPECTED_PROTECTED_BYTES)
    assert (meta["snapshot_objects"], meta["snapshot_bytes"]) == (len(S), TOTAL_BYTES)
    assert meta["started_utc"] and meta["finished_utc"]
    # the low-level chatter went to the logs, not the terminal
    with open(r.log("candidates")) as f:
        assert "safety checks: PASS" in f.read()
    assert "safety checks" not in res.stdout
    # the human summary
    assert p["plan_id"] in res.stdout
    assert "executable: yes" in res.stdout
    assert "next:" in res.stdout and "terra-scrub clean ns/ws" in res.stdout
    assert "970 bytes" in res.stdout and r.protected in res.stdout
    # nothing is armed
    with open(r.wrapper) as f:
        assert 'CONFIRM=""' in f.read()


def test_scan_no_plan_stops_after_candidates(monkeypatch, tmp_path):
    res, calls, r = scripted_scan(monkeypatch, tmp_path, "--no-plan")
    assert res.returncode == 0, res.stderr
    assert calls == ["resolve", "snapshot", "context", "candidates"]
    assert os.path.exists(r.manifest)
    assert not os.path.exists(r.plan_context) and not os.path.exists(r.plan_json)
    assert r.read_meta()["outcome"] == "candidates_only"
    assert "terra-scrub clean" not in res.stdout


def test_scan_candidates_refusal_is_recorded(monkeypatch, tmp_path):
    res, calls, r = scripted_scan(monkeypatch, tmp_path, ctx_bucket="some-other-bucket")
    assert res.returncode == 1
    assert calls == ["resolve", "snapshot", "context", "candidates"]
    meta = r.read_meta()
    assert meta["outcome"] == "refused" and meta["step"] == "candidates"
    assert "refusing to mix" in meta["refusal"]
    assert "REFUSED" in res.stdout and "refusing to mix" in res.stdout
    assert not os.path.exists(r.plan_json)
    with open(r.log("candidates")) as f:
        assert "refusing to mix" in f.read()


def test_scan_empty_delete_list_is_nothing_to_delete(monkeypatch, tmp_path):
    res, calls, r = scripted_scan(monkeypatch, tmp_path, "--prefix", "nomatch/")
    assert res.returncode == 0, res.stderr
    assert calls == ["resolve", "snapshot", "context", "candidates"]
    assert r.read_meta()["outcome"] == "nothing_to_delete"
    assert "nothing to delete" in res.stdout
    for p in (r.plan_context, r.plan_json, r.plan_uris, r.wrapper):
        assert not os.path.exists(p), p


def test_scan_prefix_passthrough_reaches_candidates_and_plan(monkeypatch, tmp_path):
    seen = {}
    install_fakes(monkeypatch, tmp_path)
    rec_cand, rec_plan = candidates.main, plan.main
    monkeypatch.setattr(candidates, "main",
                        lambda argv=None: (seen.setdefault("cand", argv), rec_cand(argv))[1])
    monkeypatch.setattr(plan, "main",
                        lambda argv=None: (seen.setdefault("plan", argv), rec_plan(argv))[1])
    res = run_cmd(scan, ["ns/ws", "--prefix", "submissions/cccc3333", "--include-zero-byte",
                         "--aborted-last-copy-deletable", "--workers", "3"])
    assert res.returncode == 0, res.stderr + res.stdout
    c = seen["cand"]
    assert c[c.index("--max-snapshot-age") + 1] == "1"
    assert c[c.index("--prefix") + 1] == "submissions/cccc3333"
    assert "--include-zero-byte" in c and "--aborted-last-copy-deletable" in c
    p = seen["plan"]
    assert p[p.index("--prefix") + 1] == "submissions/cccc3333/"
    assert p[p.index("--workers") + 1] == "3"


def test_plan_prefix():
    assert scan.plan_prefix(None) is None
    assert scan.plan_prefix(["submissions/a"]) == "submissions/a/"
    assert scan.plan_prefix(["submissions/aa/", "submissions/ab/"]) == "submissions/"
    assert scan.plan_prefix(["a/", "b/"]) == ""


def test_scan_refuses_disjoint_prefixes_before_any_call(monkeypatch, tmp_path):
    calls = install_fakes(monkeypatch, tmp_path)
    res = run_cmd(scan, ["ns/ws", "--prefix", "a/", "--prefix", "b/"])
    assert res.returncode == 1 and "REFUSING" in res.stderr
    assert calls == []


def test_scan_workspace_without_bucket_refuses(monkeypatch, tmp_path):
    res, calls, r = scripted_scan(monkeypatch, tmp_path, bucket_name=None)
    assert res.returncode == 1
    assert calls == ["resolve"]
    assert "no bucketName" in res.stdout
    assert r is None     # nothing written for a workspace that never resolved


def test_scan_bad_target_refuses(monkeypatch, tmp_path):
    install_fakes(monkeypatch, tmp_path)
    res = run_cmd(scan, ["just-a-name"])
    assert res.returncode == 1 and "namespace>/<workspace" in res.stderr


def test_scan_multiple_targets_table(monkeypatch, tmp_path):
    calls = install_fakes(monkeypatch, tmp_path)
    res = run_cmd(scan, ["ns/ws", "ns/ws2", "--quiet"])
    assert res.returncode == 0, res.stderr
    assert res.stdout == ""
    assert calls.count("plan") == 2
    calls.clear()
    # re-run without --quiet (fresh home: run stamps have 1 s resolution)
    monkeypatch.setenv("TERRA_SCRUB_HOME", str(tmp_path / "home2"))
    res = run_cmd(scan, ["ns/ws", "ns/ws2"])
    assert res.returncode == 0, res.stderr
    assert "== scan ns/ws ==" in res.stdout and "== scan ns/ws2 ==" in res.stdout
    table = res.stdout.split("== summary ==", 1)[1]
    assert "ns/ws " in table and "ns/ws2" in table and table.count("planned") == 2


def test_scan_home_flag(monkeypatch, tmp_path):
    install_fakes(monkeypatch, tmp_path)
    other = tmp_path / "elsewhere"
    res = run_cmd(scan, ["ns/ws", "--home", str(other)])
    assert res.returncode == 0, res.stderr
    r = runs.latest_run("ns", "ws", str(other))
    assert r is not None and r.read_meta()["outcome"] == "planned"
    assert f"terra-scrub clean ns/ws --home {other}" in res.stdout


@pytest.mark.parametrize("argv", [["--help"]])
def test_scan_help(argv):
    res = run_cmd(scan, argv)
    assert res.returncode == 0 and "ordering invariant" in res.stdout
