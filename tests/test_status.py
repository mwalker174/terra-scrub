"""status: offline read of the latest run for a workspace. Writes nothing."""
import json
import os
from datetime import UTC, datetime, timedelta

from conftest import run_cmd
from test_scan import install_fakes

from terra_scrub import runs, scan, status


def do_scan(monkeypatch, tmp_path, *extra, target="ns/ws", fakes=True):
    if fakes:
        install_fakes(monkeypatch, tmp_path)
    res = run_cmd(scan, [target, "--quiet", *extra])
    assert res.returncode == 0, res.stderr
    ns, ws = runs.parse_target(target)
    return runs.latest_run(ns, ws)


def _snapshot_tree(root):
    out = {}
    for dp, _dn, fn in os.walk(root):
        for n in fn:
            p = os.path.join(dp, n)
            with open(p, "rb") as f:
                out[p] = (os.path.getmtime(p), f.read())
    return out


def test_status_after_scan_says_clean_next(monkeypatch, tmp_path):
    r = do_scan(monkeypatch, tmp_path)
    plan_id = r.read_meta()["plan_id"]
    before = _snapshot_tree(tmp_path / "home")
    res = run_cmd(status, ["ns/ws"])
    assert res.returncode == 0, res.stderr
    assert plan_id in res.stdout
    assert "not armed" in res.stdout
    assert "fresh" in res.stdout and "STALE" not in res.stdout
    assert "next:" in res.stdout and "terra-scrub clean ns/ws" in res.stdout
    assert f"gs://{r.bucket}" in res.stdout
    assert _snapshot_tree(tmp_path / "home") == before, "status must write nothing"


def _age(r, hours):
    old = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    r.write_meta(started_utc=old, finished_utc=old)
    with open(r.plan_json) as f:
        p = json.load(f)
    p["generated_utc"] = old
    p["manifest_generated"] = old
    with open(r.plan_json, "w") as f:
        json.dump(p, f)


def test_status_stale_plan_says_rescan(monkeypatch, tmp_path):
    r = do_scan(monkeypatch, tmp_path)
    _age(r, 30)
    res = run_cmd(status, ["ns/ws"])
    assert res.returncode == 0
    assert "STALE" in res.stdout
    nxt = next(l for l in res.stdout.splitlines() if "next:" in l)
    assert "terra-scrub scan ns/ws" in nxt and "clean" not in nxt


def test_status_armed_wrapper(monkeypatch, tmp_path):
    r = do_scan(monkeypatch, tmp_path)
    pid = r.read_meta()["plan_id"]
    with open(r.wrapper) as f:
        txt = f.read()
    with open(r.wrapper, "w") as f:
        f.write(txt.replace('CONFIRM=""', f"CONFIRM={pid}", 1))
    res = run_cmd(status, ["ns/ws"])
    assert "ARMED" in res.stdout


def test_status_after_clean_shows_verify(monkeypatch, tmp_path):
    r = do_scan(monkeypatch, tmp_path)
    r.write_meta(cleaned_utc="2026-09-23T12:00:00+00:00", verify_rc=0, deleted_objects=5)
    res = run_cmd(status, ["ns/ws"])
    assert "5 objects deleted" in res.stdout
    assert "next:" in res.stdout and "verified OK" in res.stdout
    r.write_meta(verify_rc=3)
    res = run_cmd(status, ["ns/ws"])
    assert "verified FAILED" in res.stdout


def test_status_no_runs(monkeypatch, tmp_path):
    monkeypatch.setenv("TERRA_SCRUB_HOME", str(tmp_path / "empty"))
    res = run_cmd(status, ["ns/ws"])
    assert res.returncode == 0
    assert "no runs" in res.stdout and "terra-scrub scan ns/ws" in res.stdout
    res = run_cmd(status, [])
    assert res.returncode == 0 and "no runs" in res.stdout


def test_status_table_lists_workspaces(monkeypatch, tmp_path):
    r = do_scan(monkeypatch, tmp_path)
    do_scan(monkeypatch, tmp_path, "--prefix", "nomatch/", target="ns2/other", fakes=False)
    pid = r.read_meta()["plan_id"]
    for argv in (["--all"], []):
        res = run_cmd(status, argv)
        assert res.returncode == 0, res.stderr
        lines = res.stdout.splitlines()
        ws_line = next(l for l in lines if " ns/ws " in l)
        assert pid in ws_line and "planned" in ws_line and "terra-scrub clean ns/ws" in ws_line
        other = next(l for l in lines if "ns2/other" in l)
        assert "nothing_to_delete" in other and "nothing to delete" in other


def test_status_refused_run(monkeypatch, tmp_path):
    install_fakes(monkeypatch, tmp_path, ctx_bucket="some-other-bucket")
    assert run_cmd(scan, ["ns/ws", "--quiet"]).returncode == 1
    res = run_cmd(status, ["ns/ws"])
    assert res.returncode == 0
    assert "refused" in res.stdout and "refusing to mix" in res.stdout
    assert "terra-scrub scan ns/ws" in res.stdout


def test_status_unreadable_run_json(monkeypatch, tmp_path):
    r = do_scan(monkeypatch, tmp_path)
    with open(r.run_json, "w") as f:
        f.write("{not json")
    res = run_cmd(status, ["ns/ws"])
    assert res.returncode == 1 and "UNREADABLE" in res.stderr
