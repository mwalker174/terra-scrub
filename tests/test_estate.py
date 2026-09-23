"""estate: the ordering invariant is enforced by the driver, not documented.

`subprocess.run` (the only way estate reaches anything) is replaced by a recorder that
fakes each terra-scrub sub-command's on-disk effect. NO NETWORK, no child processes.
"""
import json
import os
import threading
import types

import pytest

from terra_scrub import estate

NS = "ns1"


class Recorder:
    """Stand-in for subprocess.run. Records (subcommand, argv) and writes what the
    real command would. `fail` maps (subcommand, bucket-ish token) -> rc."""

    def __init__(self, workspaces=()):
        self.calls = []
        self.workspaces = list(workspaces)
        self.fail = {}
        self.lock = threading.Lock()
        self.seen_ctx = {}           # plan: --terra path -> existed-at-call-time

    @staticmethod
    def _opt(argv, flag):
        return argv[argv.index(flag) + 1]

    def __call__(self, cmd, **kw):
        assert cmd[:3] == estate.TS, f"estate only ever runs terra-scrub itself: {cmd}"
        sub, argv = cmd[3], cmd[4:]
        with self.lock:
            self.calls.append((sub, argv))
        rc = next((v for (s, tok), v in self.fail.items()
                   if s == sub and any(tok in a for a in argv)), 0)
        if rc == 0:
            if sub == "workspaces":
                with open(self._opt(argv, "--out"), "w") as f:
                    json.dump({"workspaces": self.workspaces}, f)
            elif sub == "snapshot":
                with open(self._opt(argv, "--out"), "w") as f:
                    f.write('{"__bucket__": {}}\n{"__done__": true, "n_objects": 0, '
                            '"total_bytes": 0}\n')
            elif sub == "context":
                with open(self._opt(argv, "--out"), "w") as f:
                    json.dump({"bucket": "?", "captured_utc": "now"}, f)
            elif sub == "candidates":
                snap = self._opt(argv, "--snapshot")
                bucket = next(w["bucketName"] for w in self.workspaces
                              if estate.key_of(w["bucketName"]) in snap)
                od = self._opt(argv, "--out-dir")
                os.makedirs(od, exist_ok=True)
                with open(os.path.join(od, f"{bucket}.cleanup.tsv"), "w") as f:
                    f.write("# header\n")
            elif sub == "plan":
                ctx = self._opt(argv, "--terra")
                self.seen_ctx[ctx] = os.path.exists(ctx)
                tsv = self._opt(argv, "--manifest")
                with open(tsv + ".plan.json", "w") as f:
                    json.dump({"executable": True, "objects_by_status": {"PLAN_DELETE": 1},
                               "plan_bytes": 10, "plan_objects": 1, "plan_id": "abcdef012345"}, f)
        return types.SimpleNamespace(returncode=rc, stdout="", stderr="")

    def for_bucket(self, bucket):
        key = estate.key_of(bucket)
        return [(s, a) for s, a in self.calls
                if s != "workspaces" and any(key in x or bucket in x for x in a)]


def _ws(name, bucket):
    return {"namespace": NS, "name": name, "bucketName": bucket}


@pytest.fixture
def rec(monkeypatch):
    r = Recorder()
    monkeypatch.setattr(estate.subprocess, "run", r)
    return r


def _scan(root):
    return estate.main(["scan", "--root", str(root), "--namespace", NS,
                        "--no-reference-lists", "--workers", "1"])


def test_scan_order_per_bucket(tmp_path, rec):
    rec.workspaces = [_ws("w1", "fc-aaaa")]
    assert _scan(tmp_path / "run") == 0
    assert rec.calls[0][0] == "workspaces", "the estate is re-queried live first"
    seq = rec.for_bucket("fc-aaaa")
    assert [s for s, _ in seq] == ["snapshot", "context", "candidates"], \
        f"snapshot -> context -> candidates, strictly ({[s for s, _ in seq]})"
    cand = seq[2][1]
    i = cand.index("--max-snapshot-age")
    assert cand[i + 1] == "1", "candidates is gated on a snapshot at most 1 day old"
    snap_out = seq[0][1][seq[0][1].index("--out") + 1]
    ctx_out = seq[1][1][seq[1][1].index("--out") + 1]
    assert cand[cand.index("--snapshot") + 1] == snap_out \
        and cand[cand.index("--terra") + 1] == ctx_out, \
        "candidates reads exactly the snapshot and context this bucket just captured"


def test_scan_skips_finished_bucket(tmp_path, rec):
    root = tmp_path / "run"
    rec.workspaces = [_ws("w1", "fc-done"), _ws("w2", "fc-todo")]
    key = estate.key_of("fc-done")
    inv, cdir = root / "inv", root / "cleanup" / key
    inv.mkdir(parents=True)
    cdir.mkdir(parents=True)
    (inv / f"{key}.jsonl").write_text('{"__bucket__": {}}\n{"__done__": true}\n')
    (inv / f"{key}.terra.json").write_text("{}")
    (cdir / "fc-done.cleanup.tsv").write_text("# header\n")
    assert _scan(root) == 0
    assert rec.for_bucket("fc-done") == [], "a finished bucket runs nothing"
    assert [s for s, _ in rec.for_bucket("fc-todo")] == ["snapshot", "context", "candidates"]
    with open(root / "scan-status.json") as f:
        rows = {r["bucket"]: r for r in json.load(f)["rows"]}
    assert rows["fc-done"]["snapshot"] == "skip" and rows["fc-done"]["candidates"] == "skip"


def test_scan_failed_snapshot_skips_context(tmp_path, rec):
    rec.workspaces = [_ws("w1", "fc-bad")]
    rec.fail[("snapshot", "fc-bad")] = 2
    assert _scan(tmp_path / "run") == 1, "a failed bucket makes the scan not-clean"
    assert [s for s, _ in rec.for_bucket("fc-bad")] == ["snapshot"], \
        "no context (and no candidates) after a failed listing"
    with open(tmp_path / "run" / "scan-status.json") as f:
        row = json.load(f)["rows"][0]
    assert row["snapshot"] == "rc=2" and row["terra_context"] == "not-run" \
        and "candidates" not in row


def test_estate_plan_captures_context_then_plans(tmp_path, rec):
    root = tmp_path / "run"
    bucket = "fc-plan"
    key = estate.key_of(bucket)
    cdir = root / "cleanup" / key
    cdir.mkdir(parents=True)
    tsv = cdir / f"{bucket}.cleanup.tsv"
    tsv.write_text("# header\nsubmissions/x/y.bam\t10\n")
    (root / "inv").mkdir()
    with open(root / "scope.json", "w") as f:
        json.dump({"workspaces": [_ws("wp", bucket)]}, f)
    assert estate.main(["plan", "--root", str(root)]) == 0
    seq = rec.for_bucket(bucket)
    assert [s for s, _ in seq] == ["context", "plan"], f"context BEFORE plan ({seq})"
    ctx_out = seq[0][1][seq[0][1].index("--out") + 1]
    assert ctx_out == str(root / "inv" / f"{key}.plan-time.terra.json"), \
        "a plan-time context, distinct from the scan-time one"
    assert seq[0][1][:2] == [NS, "wp"], "captured for this bucket's own workspace"
    plan_argv = seq[1][1]
    assert plan_argv[plan_argv.index("--manifest") + 1] == str(tsv)
    assert plan_argv[plan_argv.index("--terra") + 1] == ctx_out, \
        "plan is given exactly the context just captured"
    assert rec.seen_ctx == {ctx_out: True}, "and it existed when plan ran"


def test_estate_plan_context_failure_skips_plan(tmp_path, rec):
    root = tmp_path / "run"
    bucket = "fc-ctxfail"
    cdir = root / "cleanup" / estate.key_of(bucket)
    cdir.mkdir(parents=True)
    (cdir / f"{bucket}.cleanup.tsv").write_text("# header\nsubmissions/x/y.bam\t10\n")
    with open(root / "scope.json", "w") as f:
        json.dump({"workspaces": [_ws("wc", bucket)]}, f)
    rec.fail[("context", "wc")] = 1
    assert estate.main(["plan", "--root", str(root)]) == 1
    assert [s for s, _ in rec.for_bucket(bucket)] == ["context"], \
        "no plan without a fresh context"
