"""approve: list pending plans, and arm one by writing CONFIRM=<plan_id> exactly once.

Plans are built by running the REAL `plan` command with the stubbed live_stat, so the
plan.json / URI list / wrapper under test are exactly what plan writes. The armed
wrapper is never executed."""
import json
import os
from datetime import UTC, datetime, timedelta

import pytest
from conftest import BUCKET, make_plan_inputs, run_cmd

from terra_scrub import approve


@pytest.fixture
def plans(tmp_path, run_plan_with_stub):
    """A root holding one executable delete plan and one review (protected) plan."""
    root = tmp_path / "runs"
    tsv, fresh = make_plan_inputs(root / "b1")
    r = run_plan_with_stub("--manifest", tsv, "--terra", fresh, "--workers", "4")
    assert r.returncode == 0, r.stderr
    prot = os.path.join(os.path.dirname(tsv), f"{BUCKET}.cleanup.protected.tsv")
    r2 = run_plan_with_stub("--manifest", prot, "--terra", fresh, "--workers", "4")
    assert r2.returncode == 0, r2.stderr
    with open(tsv + ".plan.json") as f:
        dplan = json.load(f)
    with open(prot + ".plan.json") as f:
        rplan = json.load(f)
    assert dplan["executable"] is True and dplan["commands_out"]
    assert rplan["manifest_kind"] == "protected" and rplan["commands_out"] is None
    return {"root": str(root), "delete": dplan, "delete_json": tsv + ".plan.json",
            "review": rplan}


def _confirm_lines(sh):
    with open(sh) as f:
        return [l.rstrip("\n") for l in f if l.startswith("CONFIRM=")]


def test_list_mode(plans):
    r = run_cmd(approve, ["--root", plans["root"]])
    assert r.returncode == 0, r.stderr
    assert plans["delete"]["plan_id"] in r.stdout and plans["review"]["plan_id"] in r.stdout, \
        "both plans are listed"
    assert "not armed" in r.stdout, "the delete plan shows as not armed"
    assert "no wrapper (review list)" in r.stdout, "the review plan shows it has no wrapper"
    assert "fresh" in r.stdout, "a just-generated manifest reads as fresh"
    assert f"bucket {BUCKET}" in r.stdout
    assert "terra-scrub approve <plan_id>" in r.stdout
    # listing writes nothing
    assert _confirm_lines(plans["delete"]["commands_out"]) == ['CONFIRM=""']


def test_arm_writes_token_once(plans):
    pid = plans["delete"]["plan_id"]
    sh = plans["delete"]["commands_out"]
    with open(sh) as f:
        before = f.read()
    r = run_cmd(approve, [pid, "--root", plans["root"]])
    assert r.returncode == 0, r.stderr
    assert f"ARMED: CONFIRM={pid}" in r.stdout
    assert _confirm_lines(sh) == [f"CONFIRM={pid}"], "exactly one CONFIRM line, carrying the id"
    with open(sh) as f:
        after = f.read()
    assert after.count(f"CONFIRM={pid}") == 2, \
        "the token line plus the refusal message's pre-existing mention"
    assert after == before.replace('CONFIRM=""', f"CONFIRM={pid}", 1), \
        "nothing but the one CONFIRM line changed"

    # already armed -> refuse, file untouched
    r2 = run_cmd(approve, [pid, "--root", plans["root"]])
    assert r2.returncode != 0 and "REFUSING" in r2.stderr and "already armed" in r2.stderr
    with open(sh) as f:
        assert f.read() == after


def test_refuses_sha256_mismatch(plans):
    uris = plans["delete"]["uris_out"]
    with open(uris, "a") as f:
        f.write(f"gs://{BUCKET}/submissions/aaaa1111/smuggled.bam\n")
    r = run_cmd(approve, [plans["delete"]["plan_id"], "--root", plans["root"]])
    assert r.returncode != 0 and "REFUSING" in r.stderr and "sha256" in r.stderr
    assert _confirm_lines(plans["delete"]["commands_out"]) == ['CONFIRM=""']


def test_refuses_review_plan(plans):
    r = run_cmd(approve, [plans["review"]["plan_id"], "--root", plans["root"]])
    assert r.returncode != 0 and "REFUSING" in r.stderr
    assert "manifest_kind='protected'" in r.stderr and "only a delete manifest" in r.stderr


def test_refuses_stale_manifest(plans):
    p = dict(plans["delete"])
    p["manifest_generated"] = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    with open(plans["delete_json"], "w") as f:
        json.dump(p, f)
    r = run_cmd(approve, [p["plan_id"], "--root", plans["root"]])
    assert r.returncode != 0 and "REFUSING" in r.stderr and "manifest is 25.0 h old" in r.stderr
    assert _confirm_lines(p["commands_out"]) == ['CONFIRM=""']


def test_refuses_missing_wrapper(plans):
    os.unlink(plans["delete"]["commands_out"])
    r = run_cmd(approve, [plans["delete"]["plan_id"], "--root", plans["root"]])
    assert r.returncode != 0 and "REFUSING" in r.stderr and "wrapper missing" in r.stderr


def test_refuses_unknown_plan_id(plans):
    r = run_cmd(approve, ["000000000000", "--root", plans["root"]])
    assert r.returncode != 0 and "found 0" in r.stderr
