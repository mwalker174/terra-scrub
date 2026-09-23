"""analyze: report / stale / dupes (offline), plus the terra-context bucket guards
that both `stale` and `candidates` enforce."""
import json
import re

from conftest import BUCKET, SUBS, TOTAL_BYTES, S, run_cmd, write_fixtures, write_snapshot

from terra_scrub import analyze, candidates
from terra_scrub.util import human


def test_report(tmp_path):
    snap, _ = write_fixtures(tmp_path / "rep")
    r = run_cmd(analyze, ["report", snap, "--depth", "2", "--top", "5"])
    assert r.returncode == 0, f"exit 0 (got {r.returncode}) {r.stderr}"
    out = r.stdout
    assert f"objects: {len(S)}" in out, "object count correct"
    assert f"total: {human(TOTAL_BYTES)}" in out, f"total bytes == {TOTAL_BYTES}"
    sub_bytes = sum(v[1] for v in S.values() if v[0].startswith("submissions/"))
    n_sub = len([v for v in S.values() if v[0].startswith("submissions/")])
    assert re.search(rf"^\s+{re.escape(human(sub_bytes))}\s+{n_sub}\s+submissions$", out, re.MULTILINE), \
        f"depth-1 submissions/ row == {human(sub_bytes)}, {n_sub} objs"
    assert "rootfile.txt" in out or "(root)" in out, "root object handled at depth 1"
    top = [l for l in out.splitlines()
           if l.strip().endswith((".bam", ".cram", ".fastq", ".log", ".txt"))]
    assert len(top) >= 5, "top-N objects printed"


def test_stale(tmp_path):
    snap, terra = write_fixtures(tmp_path / "stale")
    r = run_cmd(analyze, ["stale", snap, "--terra", terra, "--days", "30", "--depth2",
                          "--top", "5"])
    assert r.returncode == 0, f"exit 0 (got {r.returncode}) {r.stderr}"
    out = r.stdout
    # per-submission footprint for cccc3333 (Aborted, submitted 2026-05-01) is
    # derived from the fixture, so growing S cannot silently stale this row
    ab = [v for v in S.values() if v[0].startswith("submissions/cccc3333/")]
    ab_bytes, ab_files = sum(v[1] for v in ab), len(ab)
    assert re.search(rf"{ab_bytes} bytes\s+{ab_files}\s+2026-05-01\s+Aborted\s+cram-fastq", out), \
        f"aborted sub footprint row: {ab_bytes} bytes / {ab_files} files / 2026-05-01 / " \
        f"Aborted / cram-fastq"
    assert "not referenced by any Terra" in out, "unreferenced section present"
    assert "per-submission footprint" in out, "per-submission section present"


def test_dupes(tmp_path):
    snap, _ = write_fixtures(tmp_path / "dup")
    r = run_cmd(analyze, ["dupes", snap, "--sample"])
    assert r.returncode == 0, f"exit 0 (got {r.returncode}) {r.stderr}"
    out = r.stdout
    # (n-1)*size per group: md5A 2*100 + md5B 200 + md5D 400 + md5E 500 + md5H 0 + md5L 70 = 1370
    assert f"{human(1370)} redundant" in out, f"md5 redundant bytes == 1370 ({human(1370)})"
    assert "7 redundant objects" in out.replace(",", ""), "redundant object count == 7"
    assert "GEN99-1-1-D1" in out and "2 subs" in out, "per-sample: GEN99-1-1-D1 in 2 submissions"
    # sample id must be EXACTLY the GEN token -- the pipeline suffix is NOT part of it
    exact = [l for l in out.splitlines() if re.search(r"\bGEN99-1-1-D1$", l.rstrip())]
    assert len(exact) == 1, "per-sample id is exactly 'GEN99-1-1-D1' (no dot-suffix)"


def test_terra_bucket_mismatch(tmp_path):
    d = tmp_path / "bm"
    snap, terra = write_snapshot(d, list(S.values()), SUBS, terra_bucket="other-bucket")
    r = run_cmd(candidates, ["--snapshot", snap, "--terra", terra, "--out-dir", d / "out"])
    assert r.returncode != 0, "candidates refuses foreign-bucket terra context"
    assert "other-bucket" in r.stderr and BUCKET in r.stderr, "message names both buckets"
    r2 = run_cmd(analyze, ["stale", snap, "--terra", terra, "--days", "30"])
    assert r2.returncode != 0, "stale refuses foreign-bucket terra context"


def test_terra_missing_bucket(tmp_path):
    # a context with NO "bucket" key must be refused (the mismatch guard used to
    # silently skip unattributable contexts and trust their statuses wholesale)
    d = tmp_path / "mb"
    snap, terra = write_snapshot(d, list(S.values()), SUBS, terra_no_bucket=True)
    r = run_cmd(candidates, ["--snapshot", snap, "--terra", terra, "--out-dir", d / "out"])
    assert r.returncode != 0, f"candidates refuses context missing the bucket key (rc={r.returncode})"
    assert "bucket" in r.stderr and "re-capture" in r.stderr.lower(), \
        "message names the missing bucket key and the remedy"
    r2 = run_cmd(analyze, ["stale", snap, "--terra", terra, "--days", "30"])
    assert r2.returncode != 0, "stale refuses context missing the bucket key"
    # and a null bucket key is refused the same way
    with open(terra) as f:
        ctx = json.load(f)
    ctx["bucket"] = None
    with open(terra, "w") as f:
        json.dump(ctx, f)
    r3 = run_cmd(candidates, ["--snapshot", snap, "--terra", terra, "--out-dir", d / "out2"])
    assert r3.returncode != 0, "candidates refuses context with null bucket key"


def test_stale_depth2_largest(tmp_path):
    # four prefixes each >5%: name order a<b<c<zzz would let the old code
    # detail a,b,c and skip the LARGEST (zzz, 40% of bytes)
    objs = [("a/x.bin", 100, None, "2026-09-01T00:00:00Z"),
            ("b/y.bin", 100, None, "2026-09-01T00:00:00Z"),
            ("c/z.bin", 100, None, "2026-09-01T00:00:00Z"),
            ("zzz/big.bin", 400, None, "2026-09-01T00:00:00Z")]
    snap, terra = write_snapshot(tmp_path / "d2", objs, [],
                                 referenced=[f"gs://{BUCKET}/cram_crai/ghost.cram"])
    r = run_cmd(analyze, ["stale", snap, "--terra", terra, "--days", "30", "--depth2",
                          "--top", "5"])
    assert r.returncode == 0, f"exit 0 (got {r.returncode}) {r.stderr}"
    assert "second level under zzz/" in r.stdout, \
        "depth2 details the LARGEST prefix (zzz), not just the first three by name"


def test_dupes_workflow_and_wallclock(tmp_path):
    objs = [("submissions/aaaa1111/workflow.logs", 10, None, "2026-09-01T00:00:00Z"),
            ("submissions/aaaa1111/WGS/wf1/final.bam", 10, None, "2026-09-01T00:00:00Z")]
    snap, _ = write_snapshot(tmp_path / "wf", objs, SUBS[:1],
                             referenced=[f"gs://{BUCKET}/cram_crai/ghost.cram"])
    r = run_cmd(analyze, ["dupes", snap])
    assert r.returncode == 0, f"exit 0 (got {r.returncode}) {r.stderr}"
    assert "workflow.logs" not in r.stdout, "direct-under-sub file is not counted as a workflow type"
    assert "(direct under sub dir)" in r.stdout, "direct-under-sub files labeled as such"
    assert "WGS" in r.stdout, "real workflow type (WGS) still counted"
    # empty bucket: zero-division guard
    snap2, _ = write_snapshot(tmp_path / "wfempty", [], SUBS[:1],
                              referenced=[f"gs://{BUCKET}/cram_crai/ghost.cram"])
    r2 = run_cmd(analyze, ["dupes", snap2])
    assert r2.returncode == 0, f"empty bucket does not crash dupes (rc={r2.returncode}) {r2.stderr}"
    # missing snapshot_utc: stale warns on stderr and still runs
    snap3, terra3 = write_snapshot(tmp_path / "wlc", list(S.values()), SUBS, snapshot_utc=None)
    r3 = run_cmd(analyze, ["stale", snap3, "--terra", terra3, "--days", "30"])
    assert r3.returncode == 0, f"stale still runs without snapshot_utc (rc={r3.returncode})"
    assert "WARNING" in r3.stderr and "wall clock" in r3.stderr, \
        "stale warns when it must fall back to wall-clock ages"
