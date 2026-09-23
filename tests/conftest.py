"""Shared offline fixture for the terra-scrub test suite.

NO NETWORK. NO GCS. NO TERRA. NO gcloud. Every command exercised by the suite is
either purely offline (report / stale / dupes / candidates), a local file
round-trip, or an in-process call to ``plan.main()`` with its ONE GCS call
(``live_stat``) replaced by a fixture lookup -- see ``run_plan``.

The fixture below encodes hand-computed expectations -- the tests do NOT
re-derive expectations with the code under test (no tautology):

submissions:  aaaa1111 Done  bbbb2222 Done  cccc3333 Aborted
              dddd4444 Submitted (in-flight)  eeee5555 Done

objects (size in bytes):
  d1a 100 md5A  aaaa1111 WGS/wf1 .../cacheCopy/S1.dup.bam   (Done, cacheCopy)
  d1b 100 md5A  bbbb2222 WGS/wf2 .../S1.dup.bam            (Done, non-cc)  <- KEEP
  d1c 100 md5A  cccc3333 WGS/wf3 .../S1.dup.bam            (Aborted)
  d2a 200 md5B  cccc3333 WGS/wf3 only1.bam                (Aborted)        <- KEEP (tie: newer)
  d2b 200 md5B  cccc3333 WGS/wf3 only2.bam                (Aborted)
  d3  300 md5C  cccc3333 WGS/wf3 unique.bam               (Aborted, unique md5)
  d4a 400 md5D  eeee5555 WGS/wf5 final.bam                (Done)           <- KEEP
  d4b 400 md5D  dddd4444 WGS/wf6 cacheCopy/final.bam      (in-flight)  NEVER candidate
  d5   40 md5J  aaaa1111 WGS/wf1 keepme.bam               (Done, unique md5)
  d6   10 md5F  cccc3333 WGS/wf3 referenced.fastq         (Aborted, Terra-REFERENCED)
  d7a 500 md5E  deliverables/S1.cram                      (root level)      <- KEEP (rank 0)
  d7b 500 md5E  aaaa1111 WGS/wf1 cacheCopy/S1.cram        (Done, cc)
  d8   20 (no md5) cccc3333 WGS/wf3 nomd5.log             (Aborted, PROVENANCE: .log)
  d9a  0 md5H  bbbb2222 empty.txt                         (Done)            <- KEEP (tie: newer)
  d9b  0 md5H  aaaa1111 empty.txt                         (Done)
  d10  30 md5I  cccc3333 WGS/weird\\nname.bin             (Aborted, name has newline)
  d11  60 md5K  rootfile.txt                              (bucket root)
  d12a 70 md5L  aaaa1111 WGS/wf1 GEN99-1-1-D1...unsorted.bam (Done)
  d12b 70 md5L  bbbb2222 WGS/wf2 GEN99-1-1-D1...unsorted.bam (Done)
  d13  20 (no md5) cccc3333 WGS/wf3 nomd5.bin             (Aborted, no md5, NOT provenance)
  p1..p9  Cromwell execution records under an Aborted sub: stdout, stderr, rc,
          exec.sh, memory_retry_rc, attempt-2/output, gcs_delocalization.sh,
          shard-1/run-rc.txt, cromwell.cacheCopy.log (p9 under a Done sub)

Hand-computed expectations:
  delete list   = {d1a, d1c, d2b, d7b, d12b}                 970 bytes
      (d9b only under --include-zero-byte, G6)
  protected     = {d2a, d3, d10, d13}                        550 bytes
  G5 provenance kept (NEITHER list, by default) = {d8, p1..p9}
  G10 --include-logs: LOG_FILE rows = {d8, p1, p2}             40 bytes
      (the logs under Aborted cccc3333; p3..p8 are rc/scripts and stay under G5)
      + --include-done-logs: also p9 (Done aaaa1111)           +8 bytes
      --logs-older-than 120 (snapshot 2026-09-11): d8/p1/p2 are 133 days old,
      p9 is 102 days old -> p9 drops out even with --include-done-logs
  G3-referenced (neither)   = {d6}
  in-flight never listed    = {d4b}
  dupes redundant-bytes (md5, (n-1)*size) = 2*100 + 200 + 400 + 500 + 0 + 70 = 1370
"""
from __future__ import annotations

import io
import json
import os
import re
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

BUCKET = "test-bucket"

S = dict(
    d1a=("submissions/aaaa1111/WGS/wf1/call-X/call-MarkDuplicates/cacheCopy/S1.dup.bam", 100, "md5A", "2026-06-01T00:00:00Z"),
    d1b=("submissions/bbbb2222/WGS/wf2/call-X/call-MarkDuplicates/S1.dup.bam", 100, "md5A", "2026-06-15T00:00:00Z"),
    d1c=("submissions/cccc3333/WGS/wf3/call-X/call-MarkDuplicates/S1.dup.bam", 100, "md5A", "2026-05-01T00:00:00Z"),
    d2a=("submissions/cccc3333/WGS/wf3/only1.bam", 200, "md5B", "2026-05-02T00:00:00Z"),
    d2b=("submissions/cccc3333/WGS/wf3/only2.bam", 200, "md5B", "2026-05-01T00:00:00Z"),
    d3=("submissions/cccc3333/WGS/wf3/unique.bam", 300, "md5C", "2026-05-01T00:00:00Z"),
    d4a=("submissions/eeee5555/WGS/wf5/final.bam", 400, "md5D", "2026-07-01T00:00:00Z"),
    d4b=("submissions/dddd4444/WGS/wf6/cacheCopy/final.bam", 400, "md5D", "2026-09-01T00:00:00Z"),
    d5=("submissions/aaaa1111/WGS/wf1/keepme.bam", 40, "md5J", "2026-06-01T00:00:00Z"),
    d6=("submissions/cccc3333/WGS/wf3/referenced.fastq", 10, "md5F", "2026-05-01T00:00:00Z"),
    d7a=("deliverables/S1.cram", 500, "md5E", "2026-07-01T00:00:00Z"),
    d7b=("submissions/aaaa1111/WGS/wf1/cacheCopy/S1.cram", 500, "md5E", "2026-06-01T00:00:00Z"),
    d8=("submissions/cccc3333/WGS/wf3/nomd5.log", 20, None, "2026-05-01T00:00:00Z"),
    d9a=("submissions/bbbb2222/empty.txt", 0, "md5H", "2026-06-02T00:00:00Z"),
    d9b=("submissions/aaaa1111/empty.txt", 0, "md5H", "2026-06-01T00:00:00Z"),
    d10=("submissions/cccc3333/WGS/weird\nname.bin", 30, "md5I", "2026-05-01T00:00:00Z"),
    d11=("rootfile.txt", 60, "md5K", "2026-06-01T00:00:00Z"),
    d12a=("submissions/aaaa1111/WGS/wf1/GEN99-1-1-D1.query_sorted.unmapped.aligned.unsorted.bam", 70, "md5L", "2026-06-02T00:00:00Z"),
    d12b=("submissions/bbbb2222/WGS/wf2/GEN99-1-1-D1.query_sorted.unmapped.aligned.unsorted.bam", 70, "md5L", "2026-06-01T00:00:00Z"),
    d13=("submissions/cccc3333/WGS/wf3/nomd5.bin", 20, None, "2026-05-01T00:00:00Z"),
    # G5: Cromwell's own execution records (all under an Aborted sub, so without
    # the keep-list every one of them WOULD be an ABORTED_SUBMISSION candidate)
    p1=("submissions/cccc3333/WGS/wf3/call-X/stdout", 15, "md5P1", "2026-05-01T00:00:00Z"),
    p2=("submissions/cccc3333/WGS/wf3/call-X/stderr", 5, "md5P2", "2026-05-01T00:00:00Z"),
    p3=("submissions/cccc3333/WGS/wf3/call-X/rc", 2, "md5P3", "2026-05-01T00:00:00Z"),
    p4=("submissions/cccc3333/WGS/wf3/call-X/exec.sh", 7, "md5P4", "2026-05-01T00:00:00Z"),
    p5=("submissions/cccc3333/WGS/wf3/call-X/memory_retry_rc", 3, "md5P5", "2026-05-01T00:00:00Z"),
    p6=("submissions/cccc3333/WGS/wf3/call-X/attempt-2/output", 11, "md5P6", "2026-05-01T00:00:00Z"),
    p7=("submissions/cccc3333/WGS/wf3/call-X/gcs_delocalization.sh", 4, "md5P7", "2026-05-01T00:00:00Z"),
    p8=("submissions/cccc3333/WGS/wf3/call-X/shard-1/run-rc.txt", 6, "md5P8", "2026-05-01T00:00:00Z"),
    p9=("submissions/aaaa1111/WGS/wf1/call-X/cromwell.cacheCopy.log", 8, "md5P9", "2026-06-01T00:00:00Z"),
)
NAMES = {k: v[0] for k, v in S.items()}
TOTAL_BYTES = sum(v[1] for v in S.values())

EXPECTED_DELETE = {"d1a", "d1c", "d2b", "d7b", "d12b"}
# G6: zero-byte objects are off BOTH lists by default. d9a/d9b are the 0-byte
# tie-break pair, so they are only candidates under --include-zero-byte.
EXPECTED_ZERO_BYTE = {"d9a", "d9b"}
ZERO_BYTE_REVIVE = {"d9b"}          # d9a is the kept copy of the pair
EXPECTED_DELETE_BYTES = 970
EXPECTED_PROTECTED = {"d2a", "d3", "d10", "d13"}
EXPECTED_PROTECTED_BYTES = 550
# G5: kept off BOTH lists by default; p9 is under a Done sub so it is not a
# candidate either way -- it is here to prove .log exclusion is unconditional.
EXPECTED_PROVENANCE = {"d8", "p1", "p2", "p3", "p4", "p5", "p6", "p7", "p8", "p9"}
# with --include-provenance, the ones under a DEAD sub come back as last copies
PROVENANCE_REVIVE = {"d8", "p1", "p2", "p3", "p4", "p5", "p6", "p7", "p8"}
# G10: --include-logs lists the logs under the Aborted sub (d8 .log, p1 stdout,
# p2 stderr); --include-done-logs adds p9 (.log under Done aaaa1111).
EXPECTED_LOGS_DEAD = {"d8", "p1", "p2"}
EXPECTED_LOGS_DEAD_BYTES = 40
EXPECTED_LOGS_DONE = {"p9"}
EXPECTED_LOGS_DONE_BYTES = 8
EXPECTED_NEITHER = {"d6"}          # G3-referenced
NEVER_CANDIDATES = {"d4b", "d5", "d11", "d1b", "d4a", "d7a", "d9a", "d12a"}

SUBS = [
    {"submissionId": "aaaa1111", "submissionDate": "2026-06-01T00:00:00Z", "status": "Done",
     "submitter": "t", "config": "wggs-1", "entity": "e", "n_workflows": 1, "workflow_status_counts": {}},
    {"submissionId": "bbbb2222", "submissionDate": "2026-06-15T00:00:00Z", "status": "Done",
     "submitter": "t", "config": "wggs-1", "entity": "e", "n_workflows": 1, "workflow_status_counts": {}},
    {"submissionId": "cccc3333", "submissionDate": "2026-05-01T00:00:00Z", "status": "Aborted",
     "submitter": "t", "config": "cram-fastq", "entity": "e", "n_workflows": 1, "workflow_status_counts": {}},
    {"submissionId": "dddd4444", "submissionDate": "2026-09-01T00:00:00Z", "status": "Submitted",
     "submitter": "t", "config": "wggs-2", "entity": "e", "n_workflows": 1, "workflow_status_counts": {}},
    {"submissionId": "eeee5555", "submissionDate": "2026-07-01T00:00:00Z", "status": "Done",
     "submitter": "t", "config": "wggs-2", "entity": "e", "n_workflows": 1, "workflow_status_counts": {}},
]
REFERENCED = [f"gs://{BUCKET}/{NAMES['d6']}", f"gs://{BUCKET}/cram_crai/ghost.cram"]


# ---------------------------------------------------------------------------
# in-process command runner (replaces the subprocess `run_cli(script, ...)`)
# ---------------------------------------------------------------------------

@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str


def run_cmd(module, argv):
    """Run ``module.main(argv)`` in-process; capture rc/stdout/stderr.

    A string SystemExit code IS the error message (the ``sys.exit(msg)`` idiom the
    commands use); the interpreter would print it outside the redirect, so it is
    recorded into the captured stderr here -- otherwise every refusal would assert
    against an empty string and pass for the wrong reason."""
    out, err = io.StringIO(), io.StringIO()
    rc = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            ret = module.main([str(a) for a in argv])
        rc = int(ret or 0)
    except SystemExit as e:
        if e.code in (None, 0):
            rc = 0
        elif isinstance(e.code, int):
            rc = e.code
        else:
            rc = 1
            err.write(str(e.code) + "\n")
    return Result(rc, out.getvalue(), err.getvalue())


# ---------------------------------------------------------------------------
# fixture writers
# ---------------------------------------------------------------------------

def write_snapshot(d, objs, subs, bucket=BUCKET, terra_bucket=None,
                   snapshot_utc="2026-09-11T00:00:00+00:00",
                   captured_utc="2026-09-11T00:00:00+00:00",
                   referenced=None, done_count=None, drop_done=False,
                   bad_line_after=None, terra_no_bucket=False):
    """Generic fixture writer; write_fixtures() is the base-case instance."""
    d = str(d)
    os.makedirs(d, exist_ok=True)
    snap = os.path.join(d, "snap.jsonl")
    with open(snap, "w") as f:
        hdr = {"__bucket__": {"id": bucket, "name": bucket, "location": "US",
                              "versioning": False, "storageClass": "STANDARD"},
               "source": "fixture"}
        if snapshot_utc is not None:
            hdr["snapshot_utc"] = snapshot_utc
        f.write(json.dumps(hdr, sort_keys=True) + "\n")
        for i, (name, size, md5, upd) in enumerate(objs):
            rec = {"name": name, "size": size, "updated": upd}
            if md5:
                rec["md5Hash"] = md5
            f.write(json.dumps(rec, sort_keys=True) + "\n")
            if bad_line_after == i:
                f.write('this is not json {broken\n')
        n = len(objs) if done_count is None else done_count
        tb = sum(o[1] for o in objs)
        if not drop_done:
            f.write(json.dumps({"__done__": True, "n_objects": n,
                                "total_bytes": tb}, sort_keys=True) + "\n")
    terra = os.path.join(d, "terra.json")
    ref = list(referenced if referenced is not None else REFERENCED)
    ctx = {"namespace": "ns", "workspace": "ws",
           "isLocked": False,
           "workspace_attributes": {}, "entity_counts": {},
           "referenced_gs_uris": ref, "n_referenced": len(ref),
           "submissions": subs}
    if captured_utc is not None:
        ctx["captured_utc"] = captured_utc
    if not terra_no_bucket:
        ctx["bucket"] = terra_bucket or bucket
    with open(terra, "w") as f:
        json.dump(ctx, f)
    return snap, terra


def write_fixtures(d):
    return write_snapshot(d, list(S.values()), SUBS)


def tsv_table(path):
    """(header_columns, rows) from a candidates TSV whose header is a '# name<TAB>'
    comment line. Column order is READ, never assumed."""
    hdr = None
    rows = []
    with open(path) as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("#"):
                if line.lstrip("# ").startswith("name\t") and hdr is None:
                    hdr = line.lstrip("# ").split("\t")
                continue
            if line:
                rows.append(line.split("\t"))
    return hdr, rows


def parse_tsv_names(path):
    """names from a name-first TSV whose first lines start with '#'."""
    names = []
    with open(path) as f:
        for line in f:
            if line.startswith("#"):
                continue
            names.append(line.rstrip("\n").split("\t")[0])
    return names


def jsonl_names(path):
    with open(path) as f:
        return {json.loads(l)["name"] for l in f if l.strip()}


def jsonl_rows(path):
    with open(path) as f:
        return {json.loads(l)["name"]: json.loads(l) for l in f if l.strip()}


def first_line(path):
    with open(path) as f:
        return f.readline()


# ---------------------------------------------------------------------------
# plan helpers
# ---------------------------------------------------------------------------

def restamp_context(path):
    """Re-capture a context 'now' (the planning-time context MUST postdate the
    manifest's generated= stamp)."""
    with open(path) as f:
        ctx = json.load(f)
    ctx["captured_utc"] = datetime.now(UTC).isoformat()
    with open(path, "w") as f:
        json.dump(ctx, f)
    return path


def make_plan_inputs(d, subs=None, extra_refs=(), captured=None, gen_args=()):
    """Run the REAL generator to write a candidate TSV, then write a terra context
    NEWER than it -- the order the capture-ordering guard demands."""
    from terra_scrub import candidates

    d = str(d)
    gen = os.path.join(d, "gen")
    snap, terra = write_snapshot(gen, list(S.values()), SUBS)
    cand = os.path.join(d, "cand")
    r = run_cmd(candidates, ["--snapshot", snap, "--terra", terra, "--out-dir", cand, *gen_args])
    if r.returncode != 0:
        raise RuntimeError(f"generator failed: {r.stderr[-400:]}")
    tsv = os.path.join(cand, f"{BUCKET}.cleanup.tsv")
    with open(terra) as f:
        ctx = json.load(f)
    ctx["captured_utc"] = captured or datetime.now(UTC).isoformat()
    if subs is not None:
        ctx["submissions"] = subs
    ctx["referenced_gs_uris"] = list(ctx["referenced_gs_uris"]) + list(extra_refs)
    ctx["n_referenced"] = len(ctx["referenced_gs_uris"])
    fresh = os.path.join(d, "terra-fresh.json")
    with open(fresh, "w") as f:
        json.dump(ctx, f)
    return tsv, fresh


def set_generated(tsv, value):
    """Rewrite the header's generated= stamp. Returns the old value."""
    with open(tsv) as f:
        text = f.read()
    first, rest = text.split("\n", 1)
    old = re.search(r"generated=(\S+)", first).group(1)
    with open(tsv, "w") as f:
        f.write(re.sub(r"generated=\S+", "generated=" + value, first) + "\n" + rest)
    return old


def append_rows(tsv, rows):
    """Simulate a hand-merged manifest."""
    if isinstance(rows, str):
        rows = [rows]
    with open(tsv, "a") as f:
        f.writelines(r + "\n" for r in rows)


def fake_live_stat_factory(live):
    def fake_live_stat(session, bucket, name):
        """Stand-in for the one GCS call in plan: ('ok'|'absent'|'error', payload).
        Defaults to the fixture's own size/md5, i.e. 'the bytes did not move'."""
        if name in live:
            return live[name]
        for _k, (n, size, md5, _u) in S.items():
            if n == name:
                return "ok", {"name": name, "size": str(size), "md5Hash": md5 or ""}
        return "absent", None
    return fake_live_stat


@pytest.fixture
def live():
    """Per-test override table for the stubbed live_stat (name -> (state, meta))."""
    return {}


@pytest.fixture
def run_plan_with_stub(monkeypatch, live):
    """``plan.main`` in-process with the network primitive replaced.

    ``live_stat`` becomes a fixture lookup (overridable via the ``live`` dict) and
    ``_authed_session`` returns None -- it must never be reached by a stubbed call."""
    from terra_scrub import plan

    monkeypatch.setattr(plan, "live_stat", fake_live_stat_factory(live))
    monkeypatch.setattr(plan, "_authed_session", lambda: None)

    def _run(*args):
        return run_cmd(plan, list(args))
    return _run
