"""plan: the delete-PLAN gate, exercised with its ONE GCS call (`live_stat`) stubbed.

The real `run()` is exercised with `live_stat` replaced by a dict lookup, so every
verdict branch is hit with NO NETWORK (see conftest.run_plan_with_stub)."""
import copy
import json
import os
import shutil
import stat
import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from conftest import (
    BUCKET,
    EXPECTED_DELETE,
    EXPECTED_DELETE_BYTES,
    EXPECTED_PROTECTED,
    NAMES,
    SUBS,
    S,
    append_rows,
    make_plan_inputs,
    restamp_context,
    set_generated,
    tsv_table,
)

from terra_scrub import plan


def _load(p):
    with open(p) as f:
        return json.load(f)


def _lines(p):
    with open(p) as f:
        return [l for l in f.read().split("\n") if l]


def test_apply_clean_plan(tmp_path, run_plan_with_stub):
    tsv, fresh = make_plan_inputs(tmp_path / "plan-clean")
    r = run_plan_with_stub("--manifest", tsv, "--terra", fresh, "--workers", "4")
    assert r.returncode == 0, f"exit 0 on a live-validated plan (got {r.returncode}: {r.stderr[-300:]})"
    p = _load(tsv + ".plan.json")
    assert p["objects_by_status"].get("PLAN_DELETE") == len(EXPECTED_DELETE) \
        and p["plan_bytes"] == EXPECTED_DELETE_BYTES, \
        f"all candidates / 970 B planned, nothing blocked (got {p['objects_by_status']})"
    assert p["pointer_check"] == "on" and p["executable"] is True, \
        "plan is marked executable only because a fresh context certified the pointers"
    uris = _lines(tsv + ".plan.uris.txt")
    assert len(uris) == len(EXPECTED_DELETE) and all(
        u.startswith(f"gs://{BUCKET}/") and u.count("gs://") == 1 for u in uris), \
        "URI list is one canonical gs://<bucket>/<name> per row"
    assert {u[len(f"gs://{BUCKET}/"):] for u in uris} == {NAMES[k] for k in EXPECTED_DELETE}, \
        "URI list names exactly the candidate objects"
    with open(tsv + ".plan.sh") as f:
        sh = f.read()
    assert 'CONFIRM=""' in sh and p["plan_id"] in sh and "gcloud storage rm -I" in sh, \
        "wrapper ships with an EMPTY token and names the plan id it wants"
    i_gate = sh.find('CONFIRM=""')
    i_rm = sh.find("gcloud storage rm -I")
    assert -1 < i_gate < i_rm and "exit 1" in sh[i_gate:i_rm], \
        "in the emitted wrapper the gcloud storage rm sits strictly BEHIND the CONFIRM gate + exit 1"
    assert f"from gs://{BUCKET}" in sh, "wrapper's refusal names the real bucket"
    assert "docs/SAFETY.md" in sh, "wrapper comment cites docs/SAFETY.md"
    assert f"arm with `terra-scrub approve {p['plan_id']}`" in r.stdout, \
        "console names the approve command"
    mode = os.stat(tsv + ".plan.sh").st_mode
    assert not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH), \
        f"wrapper is not executable (mode {oct(mode & 0o777)})"
    # Run the wrapper as-generated: it MUST refuse. PATH is emptied (and bash is
    # given by absolute path) so that even a broken gate cannot reach a real gcloud.
    bash = "/bin/bash" if os.path.exists("/bin/bash") else "/usr/bin/bash"
    rr = subprocess.run([bash, tsv + ".plan.sh"], capture_output=True, text=True,
                        env={"PATH": "/nonexistent-bin"}, timeout=60)
    assert rr.returncode != 0 and "refusing" in (rr.stdout + rr.stderr), \
        f"the shipped wrapper refuses to delete when run (rc={rr.returncode})"


def test_apply_verdicts(tmp_path, run_plan_with_stub, live):
    # A pointer created AFTER the capture is the bug class a stale list cannot see.
    subs2 = copy.deepcopy(SUBS)
    for s in subs2:
        if s["submissionId"] == "bbbb2222":
            s["status"] = "Submitted"          # d12b's submission re-queued
    # --include-zero-byte: need a row whose LIVE md5 is empty (the 0-byte d9b).
    tsv, fresh = make_plan_inputs(tmp_path / "plan-verdicts", subs=subs2,
                                  extra_refs=[f"gs://{BUCKET}/{NAMES['d1a']}"],
                                  gen_args=("--include-zero-byte",))
    live.update({
        NAMES["d1c"]: ("ok", {"size": str(S["d1c"][1] + 1), "md5Hash": "md5A"}),   # grew
        NAMES["d2b"]: ("absent", None),                                            # gone
        NAMES["d7b"]: ("ok", {"size": str(S["d7b"][1]), "md5Hash": "md5ZZZ"}),     # other bytes
        NAMES["d9b"]: ("ok", {"size": "0", "md5Hash": ""}),                         # no live md5
    })
    r = run_plan_with_stub("--manifest", tsv, "--terra", fresh, "--workers", "4")
    p = _load(tsv + ".plan.json")
    by = p["objects_by_status"]
    assert r.returncode == 0 and p["plan_objects"] == 0, \
        f"every row is blocked by something -> plan deletes nothing (by {by}) {r.stderr[-300:]}"
    for want in ("REFUSE_REFERENCED_NOW", "REFUSE_SUBMISSION_NOT_TERMINAL", "SKIP_SIZE_CHANGED",
                 "SKIP_ALREADY_ABSENT", "SKIP_MD5_CHANGED", "SKIP_MD5_UNVERIFIABLE"):
        assert by.get(want) == 1, f"{want} fires exactly once (got {by.get(want)})"
    assert not _lines(tsv + ".plan.uris.txt"), "a fully-blocked plan writes an EMPTY uri list"
    assert p["executable"] is True, "executable reflects gate state, not plan size"
    assert "REFUSE" in r.stdout and "SKIP_MD5_CHANGED" in r.stdout, \
        "the refusal classes are printed, not buried"

    # The '-' sub_id cell must not defeat the in-flight guard via name fallback.
    live.clear()
    tsv2, fresh2 = make_plan_inputs(
        tmp_path / "plan-dash-subid",
        subs=[dict(x, status="Submitted") if x["submissionId"] == "cccc3333" else x for x in SUBS],
        gen_args=("--include-zero-byte",))
    hdr, rows = tsv_table(tsv2)
    i = hdr.index("sub_id")
    with open(tsv2) as f:
        meta_line = f.readline().rstrip("\n")
    with open(tsv2, "w") as f:
        f.write(meta_line + "\n")
        f.write("# " + "\t".join(hdr) + "\n")
        for c in rows:
            if c[0] == NAMES["d1c"]:
                c[i] = "-"
            f.write("\t".join(c) + "\n")
    restamp_context(fresh2)
    r = run_plan_with_stub("--manifest", tsv2, "--terra", fresh2, "--workers", "4")
    p2 = _load(tsv2 + ".plan.json")
    by2 = p2["objects_by_status"]
    refused = {e["name"] for e in p2["examples"] if e["status"] == "REFUSE_SUBMISSION_NOT_TERMINAL"}
    assert by2.get("REFUSE_SUBMISSION_NOT_TERMINAL") == 2 and by2.get("PLAN_DELETE") == 4 \
        and NAMES["d1c"] in refused, \
        f"sub_id='-' still resolves cccc3333 from the name and refuses d1c (by {by2})"


def test_apply_gates(tmp_path, run_plan_with_stub):
    d = tmp_path / "plan-gates"
    tsv, fresh = make_plan_inputs(d)

    def outs(stem):
        return ["--out", str(d / f"{stem}.json"), "--uris-out", str(d / f"{stem}.uris"),
                "--commands-out", str(d / f"{stem}.sh")]

    # no --terra -> pointer check OFF -> must not be marked executable
    r = run_plan_with_stub("--manifest", tsv, *outs("np"))
    assert r.returncode == 0 and "pointer check: OFF" in r.stdout, \
        f"without a context it says the pointer check is OFF {r.stderr[-300:]}"
    assert _load(d / "np.json")["executable"] is False, \
        "a plan with no pointer re-check is NOT marked executable"

    # --limit -> partial plan
    r = run_plan_with_stub("--manifest", tsv, "--terra", fresh, "--limit", "2", *outs("lim"))
    p = _load(d / "lim.json")
    assert r.returncode == 0 and p["plan_objects"] == 2 and p["executable"] is False, \
        f"--limit gives a PARTIAL plan that is not executable (got {p['plan_objects']}/{p['executable']})"

    # stale manifest -> refuse, then warn with the opt-out
    stale = str(d / "stale.tsv")
    shutil.copyfile(tsv, stale)
    set_generated(stale, "2026-01-01T00:00:00+00:00")
    r = run_plan_with_stub("--manifest", stale, "--terra", fresh)
    assert r.returncode != 0 and "h old" in r.stderr and "--allow-stale-manifest" in r.stderr, \
        f"a manifest older than --max-manifest-age-hours is refused ({r.stderr[-160:]})"
    r2 = run_plan_with_stub("--manifest", stale, "--terra", fresh, "--allow-stale-manifest",
                            *outs("st"))
    assert r2.returncode == 0 and "NOT EXECUTABLE" in r2.stderr, \
        "--allow-stale-manifest runs but labels the plan NOT EXECUTABLE on stderr"
    assert _load(d / "st.json")["executable"] is False, "...and records executable=false"

    # naive generated= (older generator) -> conservative UTC assumption, loudly
    naive = str(d / "naive.tsv")
    shutil.copyfile(tsv, naive)
    set_generated(naive, (datetime.now(UTC) - timedelta(minutes=5))
                  .replace(tzinfo=None).isoformat())
    restamp_context(fresh)
    r3 = run_plan_with_stub("--manifest", naive, "--terra", fresh, *outs("nv"))
    assert r3.returncode == 0 and "carries no timezone" in r3.stderr, \
        f"a naive generated= warns instead of flattering the list ({r3.stderr[-160:]})"

    # context captured BEFORE the manifest certifies instead of checking -> refuse
    old_ctx = _load(fresh)
    old_ctx["captured_utc"] = "2020-01-01T00:00:00+00:00"
    oldp = str(d / "terra-old.json")
    with open(oldp, "w") as f:
        json.dump(old_ctx, f)
    r4 = run_plan_with_stub("--manifest", tsv, "--terra", oldp)
    assert r4.returncode != 0 and "certify rather than check" in r4.stderr, \
        f"a context older than the manifest is refused ({r4.stderr[-160:]})"

    # a context for another bucket cannot certify this one
    mix = _load(fresh)
    mix["bucket"] = "some-other-bucket"
    mixp = str(d / "terra-mixbucket.json")
    with open(mixp, "w") as f:
        json.dump(mix, f)
    r5 = run_plan_with_stub("--manifest", tsv, "--terra", mixp)
    assert r5.returncode != 0 and "refusing to mix buckets" in r5.stderr, \
        f"cross-bucket context mixups are refused at the gate ({r5.stderr[-160:]})"


def test_apply_manifest_contract(tmp_path, run_plan_with_stub):
    d = tmp_path / "plan-contract"
    tsv, fresh = make_plan_inputs(d)

    # hand-merged manifest: foreign bucket, object outside the audited prefix, junk size
    hm = str(d / "handmerged.tsv")
    shutil.copyfile(tsv, hm)
    append_rows(hm, [
        f"gs://someone-elses-bucket/deep/x.bam\t100\tEXACT_DUPLICATE\t{'x'*8}\tDone\t-\t-\t-\t1\t-\tmd5Q",
        "deliverables/SECRET.cram\t500\tEXACT_DUPLICATE\tffff5555\tDone\t-\t-\t-\t1\t-\tmd5R",
        "submissions/aaaa1111/WGS/wf1/junk.bam\tnotanumber\tEXACT_DUPLICATE\taaaa1111\tDone\t-\t-\t-\t1\t-\tmd5S",
    ])
    r = run_plan_with_stub("--manifest", hm, "--terra", fresh, "--workers", "4",
                           "--out", str(d / "hm.json"), "--uris-out", str(d / "hm.uris"),
                           "--commands-out", str(d / "hm.sh"))
    p = _load(d / "hm.json")
    by = p["objects_by_status"]
    assert r.returncode == 0 and by.get("SKIP_FOREIGN_BUCKET") == 1 \
        and by.get("SKIP_OUTSIDE_PREFIX") == 1 and by.get("SKIP_BAD_SIZE_FIELD") == 1, \
        f"a hand-edited row cannot smuggle: foreign bucket / outside prefix / junk size (by {by})"
    uris = _lines(d / "hm.uris")
    assert len(uris) == len(EXPECTED_DELETE) and all(BUCKET in u for u in uris), \
        "the 3 suspicious rows never reach the URI list"

    # a name written as a full gs://<our bucket>/... URI must still yield ONE canonical URI
    d2 = tmp_path / "plan-qualified"
    tsv2, fresh2 = make_plan_inputs(d2)
    q = str(d2 / "qualified.tsv")
    with open(tsv2) as f:
        lines = f.read().split("\n")
    lines = [l if not l or l.startswith("#") else "gs://" + BUCKET + "/" + l for l in lines]
    with open(q, "w") as f:
        f.write("\n".join(lines))
    r2 = run_plan_with_stub("--manifest", q, "--terra", fresh2, "--workers", "4",
                            "--out", str(d2 / "q.json"), "--uris-out", str(d2 / "q.uris"),
                            "--commands-out", str(d2 / "q.sh"))
    uris2 = _lines(d2 / "q.uris")
    assert r2.returncode == 0 and len(uris2) == len(EXPECTED_DELETE) and all(
        u.startswith(f"gs://{BUCKET}/submissions/") and u.count("gs://") == 1 for u in uris2), \
        f"bucket-qualified cells do NOT produce gs://<bucket>/gs://<bucket>/... ({uris2[:1]})"

    # malformed manifests are refused, not guessed
    nohdr = d / "nohdr.tsv"
    nohdr.write_text("submissions/aaaa1111/x.bam\t10\n")
    r1 = run_plan_with_stub("--manifest", nohdr)
    assert r1.returncode != 0 and "no column header line" in r1.stderr, \
        "TSV with no '# name' header is refused"

    short = str(d / "short.tsv")
    shutil.copyfile(tsv, short)
    append_rows(short, "submissions/aaaa1111/WGS/wf1/short.bam\t10\tONLYTWO")
    r2 = run_plan_with_stub("--manifest", short)
    assert r2.returncode != 0 and "refusing" in r2.stderr, \
        "row width != header width is refused (name could hide a tab)"

    nobucket = str(d / "nobucket.tsv")
    shutil.copyfile(tsv, nobucket)
    with open(nobucket) as f:
        lines = f.read().split("\n")
    lines[0] = lines[0].replace("bucket=" + BUCKET, "zzz=" + BUCKET)
    with open(nobucket, "w") as f:
        f.write("\n".join(lines))
    r3 = run_plan_with_stub("--manifest", nobucket)
    assert r3.returncode != 0 and "bucket" in r3.stderr, \
        "a manifest that cannot be attributed to a bucket is refused"

    # the protected list parses too, and is labelled as what it is
    prot = os.path.join(os.path.dirname(tsv), f"{BUCKET}.cleanup.protected.tsv")
    meta, prow = plan.read_manifest(prot)
    assert meta["kind"] == "protected" and meta["bucket"] == BUCKET, \
        f"protected TSV is recognised as a review list, not a delete list ({meta['kind']})"
    assert len(prow) == len(EXPECTED_PROTECTED), f"protected TSV rows == {len(EXPECTED_PROTECTED)}"


def test_apply_no_digest_and_review(tmp_path, run_plan_with_stub):
    """No digest, no plan; and a LAST-COPY PROTECTED list never gets a wrapper."""
    d = tmp_path / "plan-nodigest"
    tsv, fresh = make_plan_inputs(d)

    # (a) a manifest from before the md5 column: refuse, with no opt-out
    old = str(d / "pre-md5-column.tsv")
    hdr, rows = tsv_table(tsv)
    i = hdr.index("md5")
    with open(tsv) as f:
        meta_line = f.readline().rstrip("\n")
    with open(old, "w") as f:
        f.write(meta_line + "\n")
        f.write("# " + "\t".join(c for j, c in enumerate(hdr) if j != i) + "\n")
        f.writelines("\t".join(c[:i]) + "\n" for c in rows)
    r = run_plan_with_stub("--manifest", old, "--terra", restamp_context(fresh))
    assert r.returncode != 0 and "SIZE ALONE" in r.stderr and "Re-capture and re-generate" in r.stderr, \
        f"a manifest with no md5 column is refused, not size-validated ({r.stderr[-150:]})"
    assert "terra-scrub candidates" in r.stderr, "the refusal names the generator to re-run"
    assert not os.path.exists(old + ".plan.json"), "the refusal writes no plan"
    assert "--allow-md5-less" not in r.stderr, "and there is no opt-out to discover"

    # (b) per-row: a delete row with no digest is never planned
    d3 = tmp_path / "plan-row-nodigest"
    tsv3, fresh3 = make_plan_inputs(d3)
    hdr3, rows3 = tsv_table(tsv3)
    j = hdr3.index("md5")
    with open(tsv3) as f:
        meta3 = f.readline().rstrip("\n")
    with open(tsv3, "w") as f:
        f.write(meta3 + "\n")
        f.write("# " + "\t".join(hdr3) + "\n")
        for c in rows3:
            if c[0] == NAMES["d1a"]:
                c[j] = "-"
            f.write("\t".join(c) + "\n")
    run_plan_with_stub("--manifest", tsv3, "--terra", restamp_context(fresh3), "--workers", "4")
    p3 = _load(tsv3 + ".plan.json")
    assert p3["objects_by_status"].get("SKIP_NO_DIGEST") == 1 \
        and p3["plan_objects"] == len(EXPECTED_DELETE) - 1, \
        f"no digest, no plan: the '-' row is skipped ({p3['objects_by_status']})"

    # (c) a LAST-COPY PROTECTED list never gets a delete wrapper
    prot = os.path.join(os.path.dirname(tsv), f"{BUCKET}.cleanup.protected.tsv")
    r4 = run_plan_with_stub("--manifest", prot, "--terra", fresh, "--workers", "4",
                            "--out", str(d / "prot.json"), "--uris-out", str(d / "prot.uris"),
                            "--commands-out", str(d / "prot.sh"))
    p4 = _load(d / "prot.json")
    assert r4.returncode == 0 and p4["manifest_kind"] == "protected", f"protected list plans (rc={r4.returncode})"
    assert not os.path.exists(d / "prot.sh") and p4["commands_out"] is None, \
        "NO rm wrapper is written for a last-copy review list"
    assert p4["executable"] is False and any("review list" in x for x in p4["not_executable_reason"]), \
        f"and the plan says why ({p4['not_executable_reason']})"
    # (d) ...nor the `rm -I` stdin
    assert not os.path.exists(d / "prot.uris") and p4["uris_out"] is None, \
        "NO URI list is written for a review queue (the rm -I stdin is the hazard)"
    assert len(p4["uri_list_sha256"]) == 64, "the withheld blob is still fingerprinted"

    # (e) a plan that plan.json calls NOT executable must not ship a runnable wrapper
    d2 = tmp_path / "plan-pointer-off"
    tsv2, _ = make_plan_inputs(d2)
    r5 = run_plan_with_stub("--manifest", tsv2, "--workers", "4",
                            "--out", str(d2 / "off.json"), "--uris-out", str(d2 / "off.uris"),
                            "--commands-out", str(d2 / "off.sh"))
    p5 = _load(d2 / "off.json")
    assert p5["pointer_check"] == "off" and p5["executable"] is False, \
        f"no --terra context: pointer check off, plan not executable (rc={r5.returncode})"
    assert not os.path.exists(d2 / "off.sh") and p5["commands_out"] is None, \
        "NO rm wrapper is written for a plan with the pointer check off"
    assert not os.path.exists(d2 / "off.uris") and p5["uris_out"] is None \
        and p5["uris_written"] is False, \
        "a NOT-executable delete plan writes no URI list either (the rm -I stdin is the hazard)"
    assert len(p5["uri_list_sha256"]) == 64, "the withheld blob is still fingerprinted"

    # (f) a submission id no fresh context lists is NOT terminal
    d4 = tmp_path / "plan-unknown-sub"
    tsv4, fresh4 = make_plan_inputs(d4, subs=[])
    run_plan_with_stub("--manifest", tsv4, "--terra", fresh4, "--workers", "4")
    p6 = _load(tsv4 + ".plan.json")
    assert p6["objects_by_status"].get("REFUSE_SUBMISSION_UNKNOWN") == len(EXPECTED_DELETE) \
        and not p6["objects_by_status"].get("PLAN_DELETE"), \
        f"an unknown submission id refuses, never plans ({p6['objects_by_status']})"


# ---------------------------------------------------------------------------
# P2-1: an armed wrapper must only ever run the delete set that was live-checked
# ---------------------------------------------------------------------------

def _bash():
    return "/bin/bash" if os.path.exists("/bin/bash") else "/usr/bin/bash"


def _sha_only_bin(tmp_path):
    """A PATH holding ONLY a sha256 tool: the wrapper can verify, but no gcloud
    (nor anything else) is reachable, so even a broken gate cannot delete."""
    tool = shutil.which("sha256sum") or shutil.which("shasum")
    if not tool:
        pytest.skip("neither sha256sum nor shasum on this machine")
    b = tmp_path / "shabin"
    b.mkdir()
    os.symlink(tool, b / os.path.basename(tool))
    return str(b)


def _arm_copy(sh_path, plan_id):
    """Arm the wrapper the way a human would (one token); the test, not approve."""
    with open(sh_path) as f:
        text = f.read()
    assert text.count('CONFIRM=""') == 1
    with open(sh_path, "w") as f:
        f.write(text.replace('CONFIRM=""', f"CONFIRM={plan_id}", 1))


def _run_wrapper(sh_path, path):
    return subprocess.run([_bash(), sh_path], capture_output=True, text=True,
                          env={"PATH": path}, timeout=60, check=False)


def test_armed_wrapper_refuses_modified_uri_list(tmp_path, run_plan_with_stub):
    tsv, fresh = make_plan_inputs(tmp_path / "plan-armed")
    r = run_plan_with_stub("--manifest", tsv, "--terra", fresh, "--workers", "4")
    assert r.returncode == 0, r.stderr
    p = _load(tsv + ".plan.json")
    sh, uris = p["commands_out"], p["uris_out"]
    with open(sh) as f:
        text = f.read()
    assert f'WANT_SHA256="{p["uri_list_sha256"]}"' in text \
        and f"WANT_LINES={len(EXPECTED_DELETE)}" in text, \
        "the wrapper carries the plan-time sha256 and line count"
    i_gate, i_sha, i_rm = (text.find('CONFIRM=""'), text.find('"$WANT_SHA256"'),
                           text.find("gcloud storage rm -I"))
    assert -1 < i_gate < i_sha < i_rm, "self-check sits behind the CONFIRM gate, before rm"
    _arm_copy(sh, p["plan_id"])
    shabin = _sha_only_bin(tmp_path)

    # positive control: unmodified list passes the self-check and reaches exec, which
    # cannot find gcloud (PATH has only the sha tool) -> 127, not the refusal path
    ok = _run_wrapper(sh, shabin)
    assert ok.returncode == 127 and "refusing" not in ok.stdout and "gcloud" in ok.stderr, \
        f"an intact list passes the check (rc={ok.returncode}: {ok.stdout}{ok.stderr})"

    # an appended URI: sha256 no longer matches -> refused before exec
    with open(uris, "a") as f:
        f.write(f"gs://{BUCKET}/submissions/aaaa1111/smuggled.bam\n")
    bad = _run_wrapper(sh, shabin)
    assert bad.returncode == 1 and "refusing" in bad.stdout and "sha256" in bad.stdout, \
        f"an armed wrapper refuses a modified URI list (rc={bad.returncode}: {bad.stdout})"

    # no sha tool at all -> refuse, never skip the check
    none = _run_wrapper(sh, "/nonexistent-bin")
    assert none.returncode == 1 and "neither sha256sum nor shasum" in none.stdout, \
        f"no sha tool -> refusal (rc={none.returncode}: {none.stdout})"


def test_armed_wrapper_refuses_line_count_mismatch(tmp_path, run_plan_with_stub):
    """sha256 matching but the count not (a hand-edited WANT_LINES) is still refused."""
    tsv, fresh = make_plan_inputs(tmp_path / "plan-armed-count")
    run_plan_with_stub("--manifest", tsv, "--terra", fresh, "--workers", "4")
    p = _load(tsv + ".plan.json")
    sh = p["commands_out"]
    _arm_copy(sh, p["plan_id"])
    with open(sh) as f:
        text = f.read()
    with open(sh, "w") as f:
        f.write(text.replace(f"WANT_LINES={len(EXPECTED_DELETE)}",
                             f"WANT_LINES={len(EXPECTED_DELETE) + 1}"))
    rr = _run_wrapper(sh, _sha_only_bin(tmp_path))
    assert rr.returncode == 1 and "lines but the plan has" in rr.stdout, rr.stdout


def test_non_executable_replan_leaves_uri_list(tmp_path, run_plan_with_stub, live):
    tsv, fresh = make_plan_inputs(tmp_path / "plan-replan")
    r = run_plan_with_stub("--manifest", tsv, "--terra", fresh, "--workers", "4")
    assert r.returncode == 0
    p1 = _load(tsv + ".plan.json")
    with open(tsv + ".plan.uris.txt", "rb") as f:
        uris_before = f.read()
    with open(tsv + ".plan.sh", "rb") as f:
        sh_before = f.read()
    assert len(_lines(tsv + ".plan.uris.txt")) == len(EXPECTED_DELETE)

    # same manifest (-> same plan_id), a different live state, and NOT executable:
    # --limit 2 would otherwise write a 2-line partial list under the armed name
    live[NAMES["d2b"]] = ("absent", None)
    r2 = run_plan_with_stub("--manifest", tsv, "--terra", fresh, "--limit", "2")
    assert r2.returncode == 0, r2.stderr
    p2 = _load(tsv + ".plan.json")
    assert p2["plan_id"] == p1["plan_id"] and p2["executable"] is False
    with open(tsv + ".plan.uris.txt", "rb") as f:
        assert f.read() == uris_before, "the earlier, live-checked URI list is untouched"
    with open(tsv + ".plan.sh", "rb") as f:
        assert f.read() == sh_before, "and so is the earlier wrapper"
    assert p2["uris_out"] is None and p2["commands_out"] is None \
        and p2["uris_written"] is False and p2["commands_written"] is False, \
        "plan.json records that this plan wrote neither artifact"
    assert set(p2["left_untouched"]) == {os.path.abspath(tsv + ".plan.uris.txt"),
                                         os.path.abspath(tsv + ".plan.sh")}, \
        f"...and names the earlier files it left alone ({p2['left_untouched']})"
    assert "LEFT UNTOUCHED" in r2.stderr

    # the pointer-check-off re-plan is not executable either
    r3 = run_plan_with_stub("--manifest", tsv)
    assert r3.returncode == 0 and _load(tsv + ".plan.json")["executable"] is False
    with open(tsv + ".plan.uris.txt", "rb") as f:
        assert f.read() == uris_before
