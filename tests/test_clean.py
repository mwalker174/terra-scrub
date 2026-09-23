"""clean: the one command that runs a delete, exercised fully offline.

The run directory is built with the real `candidates` + `plan` (stubbed live_stat)
at the paths terra_scrub.runs derives. `clean.subprocess.run` is replaced by a
recorder, so NO wrapper ever executes, and `verify.main` is replaced by a stub."""
import ast
import json
import os
import shutil
import sys
from datetime import UTC, datetime, timedelta

import pytest
from conftest import BUCKET, make_plan_inputs, run_cmd

from terra_scrub import clean, runs, verify

NS, WS = "ns", "ws"
TARGET = f"{NS}/{WS}"


class _Proc:
    def __init__(self, rc, out):
        self.returncode, self.stdout = rc, out


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = str(tmp_path / "home")
    monkeypatch.setenv("TERRA_SCRUB_HOME", h)
    return h


@pytest.fixture
def calls(monkeypatch):
    """Recorder for subprocess.run and verify.main; configurable return codes."""
    rec = {"run": [], "verify": [], "run_rc": 0, "run_out": "deleted\n", "verify_rc": 0}

    def fake_run(argv, **kw):
        rec["run"].append(list(argv))
        return _Proc(rec["run_rc"], rec["run_out"])

    def fake_verify(argv):
        rec["verify"].append(list(argv))
        print("     reversible until: ['2026-09-30T00:00']  (restore ...)")
        print("RESULT: ALL CHECKS PASS" if rec["verify_rc"] == 0 else "RESULT: 1 FAILURE(S)")
        return rec["verify_rc"]

    monkeypatch.setattr(clean.subprocess, "run", fake_run)
    monkeypatch.setattr(verify, "main", fake_verify)
    return rec


@pytest.fixture
def planned(tmp_path, home, run_plan_with_stub):
    """A `planned` run for ns/ws laid out exactly as runs.Run says."""
    r = runs.new_run(NS, WS, home).with_bucket(BUCKET)
    tsv, fresh = make_plan_inputs(tmp_path / "work")
    os.makedirs(r.cleanup_dir)
    shutil.copy(tsv, r.manifest)
    res = run_plan_with_stub("--manifest", r.manifest, "--terra", fresh, "--workers", "4")
    assert res.returncode == 0, res.stderr
    with open(r.plan_json) as f:
        p = json.load(f)
    assert p["executable"] and os.path.realpath(p["commands_out"]) == os.path.realpath(r.wrapper)
    r.write_meta(outcome="planned", plan_id=p["plan_id"], plan_objects=p["plan_objects"],
                 plan_bytes=p["plan_bytes"])
    return r, p


def _confirm_line(sh):
    with open(sh) as f:
        return [l.rstrip("\n") for l in f if l.startswith("CONFIRM=")]


def _meta(r):
    with open(r.run_json) as f:
        return json.load(f)


def test_no_scan_refuses(home, calls):
    res = run_cmd(clean, [TARGET, "--confirm", "x"])
    assert res.returncode == 1
    assert "REFUSING: no scan for ns/ws" in res.stderr and "terra-scrub scan ns/ws" in res.stderr
    assert not calls["run"]


def test_not_interactive_without_confirm_refuses(planned, calls, monkeypatch):
    r, _p = planned
    monkeypatch.setattr(clean, "_interactive", lambda: False)
    res = run_cmd(clean, [TARGET])
    assert res.returncode == 1
    assert "REFUSING: not interactive; pass --confirm <plan_id>" in res.stderr
    assert _confirm_line(r.wrapper) == ['CONFIRM=""'] and not calls["run"]


def test_wrong_confirm_aborts(planned, calls):
    r, _p = planned
    res = run_cmd(clean, [TARGET, "--confirm", "000000000000"])
    assert res.returncode == 1 and "does not match" in res.stderr
    assert _confirm_line(r.wrapper) == ['CONFIRM=""'] and not calls["run"] and not calls["verify"]


def test_dry_run_writes_nothing(planned, calls, monkeypatch):
    r, _p = planned
    monkeypatch.setattr(clean, "_interactive", lambda: False)
    before = _meta(r)
    res = run_cmd(clean, [TARGET, "--dry-run"])
    assert res.returncode == 0, res.stderr
    assert f"DRY RUN: would arm {r.wrapper} and run it" in res.stdout
    assert _confirm_line(r.wrapper) == ['CONFIRM=""']
    assert not calls["run"] and not calls["verify"]
    assert _meta(r) == before and not os.path.exists(r.logs_dir)


def test_happy_path(planned, calls):
    r, p = planned
    res = run_cmd(clean, [TARGET, "--confirm", p["plan_id"], "--workers", "3"])
    assert res.returncode == 0, res.stderr
    assert _confirm_line(r.wrapper) == [f"CONFIRM={p['plan_id']}"]
    assert calls["run"] == [["bash", r.wrapper]]
    assert calls["verify"] == [["--plan", r.plan_json, "--workers", "3"]]
    m = _meta(r)
    assert m["outcome"] == "cleaned" and m["clean_rc"] == 0 and m["verify_rc"] == 0
    assert m["deleted_objects"] == p["plan_objects"]
    assert all(m.get(k) for k in ("armed_utc", "cleaned_utc", "verified_utc"))
    # summary + final line
    assert f"gs://{BUCKET}" in res.stdout and r.manifest in res.stdout
    assert "soft-delete" in res.stdout
    assert f"CLEANED ns/ws: {p['plan_objects']:,} objects" in res.stdout
    assert "verify OK" in res.stdout and "2026-09-30T00:00" in res.stdout
    # tee'd logs
    with open(r.log("clean")) as f:
        assert f.read() == "deleted\n"
    with open(r.log("verify")) as f:
        assert "ALL CHECKS PASS" in f.read()


def test_verify_failure(planned, calls):
    r, p = planned
    calls["verify_rc"] = 1
    res = run_cmd(clean, [TARGET, "--confirm", p["plan_id"]])
    assert res.returncode == 1
    assert _meta(r)["outcome"] == "cleaned-verify-failed"
    assert "FAILED ns/ws" in res.stderr and r.log("verify") in res.stderr


def test_wrapper_self_refusal_exits_without_verify(planned, calls):
    r, p = planned
    calls["run_rc"], calls["run_out"] = 1, "refusing: URI list sha256 changed since planning\n"
    res = run_cmd(clean, [TARGET, "--confirm", p["plan_id"]])
    assert res.returncode == 1 and "REFUSED by the wrapper" in res.stderr
    m = _meta(r)
    assert not calls["verify"] and m["clean_rc"] == 1
    assert m["outcome"] == "planned" and "cleaned_utc" not in m, "a refusal is not a clean"


def test_wrapper_failure_still_verifies(planned, calls):
    _r, p = planned
    calls["run_rc"], calls["run_out"] = 1, "ERROR: 403 on object 17\n"
    res = run_cmd(clean, [TARGET, "--confirm", p["plan_id"]])
    assert calls["verify"], "a partial run is diagnosed by verify"
    assert res.returncode == 0 and "wrapper exited rc=1" in res.stderr


def test_stale_manifest_refuses(planned, calls):
    r, p = planned
    p = dict(p, manifest_generated=(datetime.now(UTC) - timedelta(hours=25)).isoformat())
    with open(r.plan_json, "w") as f:
        json.dump(p, f)
    res = run_cmd(clean, [TARGET, "--confirm", p["plan_id"]])
    assert res.returncode == 1 and "manifest is 25.0 h old" in res.stderr
    assert "re-run `terra-scrub scan ns/ws`" in res.stderr
    assert _confirm_line(r.wrapper) == ['CONFIRM=""'] and not calls["run"]


def test_not_planned_outcome_refuses(planned, calls):
    r, p = planned
    r.write_meta(outcome="cleaned")
    res = run_cmd(clean, [TARGET, "--confirm", p["plan_id"]])
    assert res.returncode == 1 and "outcome='cleaned'" in res.stderr and not calls["run"]


def test_already_armed_is_not_rearmed(planned, calls):
    r, p = planned
    from terra_scrub import approve
    approve.arm(r.wrapper, p["plan_id"])
    with open(r.wrapper) as f:
        armed_text = f.read()
    # still needs the confirmation
    res0 = run_cmd(clean, [TARGET, "--confirm", "nope"])
    assert res0.returncode == 1 and not calls["run"]
    res = run_cmd(clean, [TARGET, "--confirm", p["plan_id"]])
    assert res.returncode == 0, res.stderr
    assert "not re-arming" in res.stdout
    with open(r.wrapper) as f:
        assert f.read() == armed_text
    assert calls["run"] == [["bash", r.wrapper]]


def test_armed_with_other_token_refuses(planned, calls):
    r, _p = planned
    from terra_scrub import approve
    approve.arm(r.wrapper, "ffffffffffff")
    res = run_cmd(clean, [TARGET, "--confirm", _p["plan_id"]])
    assert res.returncode == 1 and "not this plan's id" in res.stderr and not calls["run"]


class _TTY:
    def isatty(self):
        return True


def test_tty_prompt(planned, calls, monkeypatch):
    r, p = planned
    prompts = []
    monkeypatch.setattr(sys, "stdin", _TTY())
    monkeypatch.setattr("builtins.input", lambda msg: prompts.append(msg) or p["plan_id"])
    res = run_cmd(clean, [TARGET])
    assert res.returncode == 0, res.stderr
    assert prompts and prompts[0].startswith(
        f"Type the plan id to delete {p['plan_objects']:,} objects (")
    assert prompts[0].endswith(f"from gs://{BUCKET}, or anything else to abort: ")
    assert _meta(r)["outcome"] == "cleaned"


def test_tty_prompt_mismatch(planned, calls, monkeypatch):
    r, _p = planned
    monkeypatch.setattr(sys, "stdin", _TTY())
    monkeypatch.setattr("builtins.input", lambda msg: "yes")
    res = run_cmd(clean, [TARGET])
    assert res.returncode == 1 and "ABORTED" in res.stderr
    assert _confirm_line(r.wrapper) == ['CONFIRM=""'] and not calls["run"]


def test_clean_names_no_cloud_cli():
    """clean runs the wrapper; it must never name the deleter itself."""
    path = os.path.join(os.path.dirname(clean.__file__), "clean.py")
    with open(path) as f:
        src = f.read()
    assert "gsutil" not in src and "gcloud" not in src
    tree = ast.parse(src)
    runs_ = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr in {"run", "Popen", "call"}
             and isinstance(n.func.value, ast.Name) and n.func.value.id == "subprocess"]
    assert len(runs_) == 1, "exactly one subprocess call: bash <wrapper>"
    first = runs_[0].args[0]
    assert isinstance(first, ast.List) and first.elts[0].value == "bash"
