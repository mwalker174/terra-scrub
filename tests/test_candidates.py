"""candidates: the offline delete-list / protected-list generator (guards G1-G10)."""
import json
import os

from conftest import (
    BUCKET,
    EXPECTED_DELETE,
    EXPECTED_DELETE_BYTES,
    EXPECTED_LOGS_DEAD,
    EXPECTED_LOGS_DEAD_BYTES,
    EXPECTED_LOGS_DONE,
    EXPECTED_LOGS_DONE_BYTES,
    EXPECTED_NEITHER,
    EXPECTED_PROTECTED,
    EXPECTED_PROTECTED_BYTES,
    EXPECTED_PROVENANCE,
    EXPECTED_ZERO_BYTE,
    NAMES,
    NEVER_CANDIDATES,
    PROVENANCE_REVIVE,
    REFERENCED,
    SUBS,
    ZERO_BYTE_REVIVE,
    S,
    first_line,
    jsonl_names,
    jsonl_rows,
    parse_tsv_names,
    run_cmd,
    tsv_table,
    write_fixtures,
    write_snapshot,
)

from terra_scrub import candidates


def cand(*args):
    return run_cmd(candidates, list(args))


def both_lists(outdir):
    return (jsonl_names(os.path.join(outdir, f"{BUCKET}.cleanup.jsonl"))
            | jsonl_names(os.path.join(outdir, f"{BUCKET}.cleanup.protected.jsonl")))


def test_cleanup(tmp_path):
    snap, terra = write_fixtures(tmp_path / "clean")
    outdir = str(tmp_path / "clean-out")
    r = cand("--snapshot", snap, "--terra", terra, "--out-dir", outdir)
    assert r.returncode == 0, f"exit 0 (got {r.returncode}) {r.stdout[-400:]} {r.stderr[-400:]}"
    # 7 checks: G2b and G7 re-asserted after the last-copy/policy moves, plus G10.
    # The count stays pinned -- a drift here is deleted-assertion detection.
    assert "safety checks: PASS (7/7)" in r.stdout, "all 7 safety checks pass"
    base = f"{BUCKET}.cleanup"
    tsv = os.path.join(outdir, base + ".tsv")
    pjson = os.path.join(outdir, base + ".jsonl")
    pjson_prot = os.path.join(outdir, base + ".protected.jsonl")

    with open(pjson) as f:
        rows = [json.loads(l) for l in f]
    got = {r0["name"]: r0 for r0 in rows}
    want = {NAMES[k]: k for k in EXPECTED_DELETE}
    key = {v: k for k, v in NAMES.items()}
    got_keys = sorted(key.get(n, n) for n in got)
    assert set(got) == set(want), \
        f"delete list membership == {sorted(want.values())} (got {got_keys})"
    assert sum(r0["size"] for r0 in rows) == EXPECTED_DELETE_BYTES, "delete bytes == 970"

    # reasons
    assert got[NAMES["d1a"]]["reasons"] == ["EXACT_DUPLICATE"], "d1a EXACT_DUPLICATE only (Done)"
    assert set(got[NAMES["d1c"]]["reasons"]) == {"ABORTED_SUBMISSION", "EXACT_DUPLICATE"}, \
        "d1c aborted+dup"
    assert got[NAMES["d1c"]]["superseded"] is True, "d1c superseded (copy exists outside its sub)"
    assert got[NAMES["d2b"]]["superseded"] is False, "d2b not superseded (sibling in same sub)"
    assert got[NAMES["d7b"]]["reasons"] == ["EXACT_DUPLICATE"] \
        and got[NAMES["d7b"]]["duplicate"]["kept"] == NAMES["d7a"], \
        "d7b keep = root-level deliverable (rank 0)"
    assert NAMES["d9b"] not in got and NAMES["d9a"] not in got, \
        "G6: the 0-byte tie pair is not a candidate at all"
    assert got[NAMES["d1c"]]["aborted"]["config"] == "cram-fastq", "aborted detail carries config"

    assert all(r0.get("bucket") == BUCKET for r0 in rows), \
        "every delete JSONL record carries the bucket field"

    for k in ("d1b", "d2a", "d4a", "d7a", "d9a", "d12a"):
        assert NAMES[k] not in got, f"keeper {k} not in delete list"

    # protected
    with open(pjson_prot) as f:
        prot = [json.loads(l) for l in f]
    gotp = {r0["name"] for r0 in prot}
    assert gotp == {NAMES[k] for k in EXPECTED_PROTECTED}, \
        f"protected == {sorted(EXPECTED_PROTECTED)} (got {len(gotp)})"
    assert sum(r0["size"] for r0 in prot) == EXPECTED_PROTECTED_BYTES, "protected bytes == 550"
    assert any(r0["note"] and "n_copies=1" in r0["note"]
               for r0 in prot if r0["name"] == NAMES["d3"]), "d3 note says unique-md5 last copy"
    assert any(NAMES["d2a"] == r0["name"] and "last survivor" in r0["note"] for r0 in prot), \
        "d2a note says last survivor of all-dead group"
    assert any(r0["name"] == NAMES["d13"] and "NO_MD5_PROTECTED" in r0["reasons"] for r0 in prot), \
        "d13 (no md5, aborted sub, not a provenance name) -> NO_MD5_PROTECTED"
    assert NAMES["d8"] not in (set(got) | gotp), "d8 (.log) is excluded by G5 even with no md5"

    # G5: execution provenance on neither list by default
    prov = {NAMES[k] for k in EXPECTED_PROVENANCE}
    assert not (prov & (set(got) | gotp)), \
        f"no provenance object in either list (hit {sorted(prov & (set(got) | gotp))[:3]})"
    assert "provenance objects kept off both lists" in r.stdout, \
        "summary reports the G5 provenance count"
    assert "provenance_keep=on" in first_line(tsv), "delete TSV header records provenance_keep=on"

    # G3 / in-flight / root / Done-unique all absent from BOTH lists
    neither = {NAMES[k] for k in EXPECTED_NEITHER} | {NAMES[k] for k in NEVER_CANDIDATES} | prov
    assert not (set(got) | gotp) & neither, "no G3/in-flight/root/Done-unique object in any list"

    # TSV integrity: header(2) + one line per candidate (newline-in-name escaped)
    with open(tsv) as f:
        nlines = sum(1 for _ in f)
    assert nlines == 2 + len(rows), \
        f"TSV line count == 2 + {len(rows)} (got {nlines}; control chars must be escaped)"
    tnames = parse_tsv_names(tsv)
    assert len(tnames) == len(rows), "TSV name column has one entry per candidate"

    hdr, cells = tsv_table(tsv)
    assert hdr is not None and hdr[0] == "name", "delete TSV header declares 'name' first"
    assert "md5" in hdr, f"delete TSV carries an md5 column (header ends {hdr[-2:]})"
    assert all(len(c) == len(hdr) for c in cells), \
        f"delete TSV rows all match the {len(hdr)}-column header"
    got_md5 = {c[0]: c[hdr.index("md5")] for c in cells}
    want_md5 = {NAMES[k]: S[k][2] for k in EXPECTED_DELETE}
    assert got_md5 == want_md5, "delete TSV md5 == snapshot md5 for every row"

    phdr, pcells = tsv_table(os.path.join(outdir, base + ".protected.tsv"))
    assert phdr is not None and "md5" in phdr and all(len(c) == len(phdr) for c in pcells), \
        "protected TSV carries the same md5 column and row width"
    pmd5 = {c[0]: c[phdr.index("md5")] for c in pcells}
    assert pmd5.get(NAMES["d3"]) == "md5C" and pmd5.get(NAMES["d13"]) == "-", \
        "protected md5: real value for d3, literal '-' for no-md5 d13 (never blank)"

    # independent re-derivation of G1/G2/G3 from the outputs (not from the code)
    subs_status = {s["submissionId"]: s["status"] for s in SUBS}

    def sub_of(name):
        p = name.split("/")
        return p[1] if p[0] == "submissions" and len(p) > 1 else None
    assert all(n.startswith("submissions/")
               and subs_status.get(sub_of(n)) in ("Done", "Aborted", "Failed") for n in got), \
        "G1 (independent): every candidate under terminal-status sub under submissions/"
    by_md5 = {}
    for name, size, md5, upd in S.values():
        if md5:
            by_md5.setdefault(md5, []).append(name)
    assert all(sum(1 for n in g if n not in got) >= 1 for g in by_md5.values() if len(g) > 1), \
        "G2 (independent): every md5 group retains >=1 copy"
    assert all(f"gs://{BUCKET}/{n}" not in REFERENCED for n in got), \
        "G3 (independent): no referenced URI in delete list"

    # summary numbers in stdout
    assert f"{EXPECTED_DELETE_BYTES} bytes" in r.stdout.replace(",", ""), \
        "summary prints exact candidate byte total (970 bytes)"
    assert "PROTECTED review list" in r.stdout, "summary prints protected section"
    assert "ONLY md5-copy" in r.stdout and "2 objs" in r.stdout, \
        "only-md5-copy line counts exactly d3+d10"
    assert "effective --prefix: ['submissions/']" in r.stdout, "summary echoes effective prefixes"
    assert "'Aborted': 1" in r.stdout and "'Done': 3" in r.stdout, \
        "summary prints submission status distribution for auditability"


def test_provenance(tmp_path):
    """G5: Cromwell execution records stay off both lists, and the switch is real."""
    d = tmp_path / "prov"
    snap, terra = write_fixtures(d)

    out1 = str(d / "keep")
    r1 = cand("--snapshot", snap, "--terra", terra, "--out-dir", out1)
    assert r1.returncode == 0, f"default run exits 0 (rc={r1.returncode}) {r1.stderr[-160:]}"
    both1 = both_lists(out1)
    prov = {NAMES[k] for k in EXPECTED_PROVENANCE}
    assert not (prov & both1), f"default: no provenance listed (hit {sorted(prov & both1)[:3]})"
    assert NAMES["d3"] in both1 and NAMES["d13"] in both1, \
        "default: real last-copy objects are still protected (switch did not blind G2)"
    assert "provenance_keep=on" in first_line(os.path.join(out1, f"{BUCKET}.cleanup.tsv")), \
        "delete TSV header records provenance_keep=on"

    out2 = str(d / "list")
    r2 = cand("--snapshot", snap, "--terra", terra, "--out-dir", out2, "--include-provenance")
    assert r2.returncode == 0, f"--include-provenance exits 0 (rc={r2.returncode})"
    both2 = both_lists(out2)
    revive = {NAMES[k] for k in PROVENANCE_REVIVE}
    assert revive <= both2, \
        f"--include-provenance brings dead-sub execution records back (missing {sorted(revive - both2)[:3]})"
    assert NAMES["p9"] not in both2, "p9 (.log under a Done sub) is not a candidate in either mode"
    assert "provenance_keep=off" in first_line(os.path.join(out2, f"{BUCKET}.cleanup.tsv")), \
        "header records provenance_keep=off when the flag is passed"
    assert both1 <= both2 and (both2 - both1) <= revive, \
        "the flag only ADDS provenance rows; it never drops or rewrites a decision"
    assert NAMES["d6"] not in both1 and NAMES["d6"] not in both2, "G3 holds in both modes"
    assert NAMES["d4b"] not in both1 and NAMES["d4b"] not in both2, "in-flight holds in both modes"


def _lists(outdir):
    return (jsonl_rows(os.path.join(outdir, f"{BUCKET}.cleanup.jsonl")),
            jsonl_rows(os.path.join(outdir, f"{BUCKET}.cleanup.protected.jsonl")))


def test_logs(tmp_path):
    """G10: --include-logs lists dead-sub logs as LOG_FILE; Done logs only on request."""
    d = tmp_path / "logs"
    snap, terra = write_fixtures(d)
    dead = {NAMES[k] for k in EXPECTED_LOGS_DEAD}
    done = {NAMES[k] for k in EXPECTED_LOGS_DONE}
    base_del = {NAMES[k] for k in EXPECTED_DELETE}
    base_prot = {NAMES[k] for k in EXPECTED_PROTECTED}

    out0 = str(d / "default")
    r0 = cand("--snapshot", snap, "--terra", terra, "--out-dir", out0)
    assert r0.returncode == 0
    assert " logs=kept " in first_line(os.path.join(out0, f"{BUCKET}.cleanup.tsv")), \
        "default header records logs=kept"

    out1 = str(d / "dead")
    r1 = cand("--snapshot", snap, "--terra", terra, "--out-dir", out1, "--include-logs")
    assert r1.returncode == 0, r1.stderr[-300:]
    assert "safety checks: PASS (7/7)" in r1.stdout
    dl, pl = _lists(out1)
    assert set(dl) == base_del | dead, f"delete list = base + dead-sub logs (got {sorted(set(dl) ^ (base_del | dead))})"
    assert set(pl) == base_prot, "review list unchanged: logs never land on it"
    assert all(dl[n]["reasons"] == ["LOG_FILE"] for n in dead), "log rows carry LOG_FILE only"
    assert sum(dl[n]["size"] for n in dead) == EXPECTED_LOGS_DEAD_BYTES
    assert NAMES["d8"] in dl, "a log with no md5 is still listed (plan skips it as SKIP_NO_DIGEST)"
    for k in ("p3", "p4", "p5", "p6", "p7", "p8"):
        assert NAMES[k] not in dl and NAMES[k] not in pl, f"{k}: rc/scripts stay under G5"
    assert not (done & (set(dl) | set(pl))), "Done-sub logs need --include-done-logs"
    assert " logs=dead " in first_line(os.path.join(out1, f"{BUCKET}.cleanup.tsv"))
    assert "LOG CLEANUP logs=dead: 3 log files" in r1.stdout

    out2 = str(d / "done")
    r2 = cand("--snapshot", snap, "--terra", terra, "--out-dir", out2,
              "--include-logs", "--include-done-logs")
    assert r2.returncode == 0, r2.stderr[-300:]
    dl2, pl2 = _lists(out2)
    assert set(dl2) == base_del | dead | done
    assert set(pl2) == base_prot
    assert dl2[NAMES["p9"]]["reasons"] == ["LOG_FILE"] \
        and dl2[NAMES["p9"]]["submission_status"] == "Done"
    assert sum(dl2[n]["size"] for n in done) == EXPECTED_LOGS_DONE_BYTES
    assert " logs=dead+done " in first_line(os.path.join(out2, f"{BUCKET}.cleanup.tsv"))

    out3 = str(d / "aged")
    r3 = cand("--snapshot", snap, "--terra", terra, "--out-dir", out3,
              "--include-logs", "--include-done-logs", "--logs-older-than", "120")
    assert r3.returncode == 0, r3.stderr[-300:]
    dl3, pl3 = _lists(out3)
    assert set(dl3) == base_del | dead, "p9 (102 days) is too young; dead logs (133 days) are not"
    assert NAMES["p9"] not in pl3
    assert "logs=dead+done,older_than=120d " in first_line(os.path.join(out3, f"{BUCKET}.cleanup.tsv"))

    out4 = str(d / "none-old-enough")
    r4 = cand("--snapshot", snap, "--terra", terra, "--out-dir", out4,
              "--include-logs", "--logs-older-than", "140")
    assert r4.returncode == 0
    assert set(_lists(out4)[0]) == base_del, "no log is 140 days old"

    for bad in (["--include-done-logs"], ["--logs-older-than", "30"],
                ["--include-logs", "--logs-older-than", "-1"]):
        rb = cand("--snapshot", snap, "--terra", terra, "--out-dir", str(d / "bad"), *bad)
        assert rb.returncode != 0 and "REFUSING" in rb.stderr, f"{bad} is refused"
        assert not os.path.exists(str(d / "bad")), "a refusal writes nothing"


def test_logs_md5_groups(tmp_path):
    """G10 vs G2: a log never keeps an md5 group while another copy exists, and a
    group made only of logs may die whole."""
    A, C = "submissions/aaaa1111/WGS/wf1", "submissions/cccc3333/WGS/wf3"
    objs = [
        # all-log group under the Aborted sub: both go
        (f"{C}/call-A/stdout", 9, "md5X", "2026-05-01T00:00:00Z"),
        (f"{C}/call-B/stdout", 9, "md5X", "2026-05-02T00:00:00Z"),
        # Done: log newer than its twin, so it would be the keeper without G10's
        # preference -- and k.txt would then be an EXACT_DUPLICATE of a doomed file
        (f"{A}/call-C/stdout", 7, "md5Y", "2026-06-02T00:00:00Z"),
        (f"{A}/k.txt", 7, "md5Y", "2026-06-01T00:00:00Z"),
        # Aborted: log + its only non-log twin; the twin goes to the review list
        (f"{C}/call-D/stderr", 5, "md5Z", "2026-05-02T00:00:00Z"),
        (f"{C}/y.bin", 5, "md5Z", "2026-05-01T00:00:00Z"),
    ]
    snap, terra = write_snapshot(tmp_path / "g", objs, SUBS, referenced=[])
    out = str(tmp_path / "g-out")
    r = cand("--snapshot", snap, "--terra", terra, "--out-dir", out,
             "--include-logs", "--include-done-logs")
    assert r.returncode == 0, r.stdout[-400:] + r.stderr[-400:]
    assert "safety checks: PASS (7/7)" in r.stdout
    dl, pl = _lists(out)
    assert set(dl) == {f"{C}/call-A/stdout", f"{C}/call-B/stdout",
                       f"{A}/call-C/stdout", f"{C}/call-D/stderr"}
    assert all(v["reasons"] == ["LOG_FILE"] for v in dl.values())
    assert f"{A}/k.txt" not in dl and f"{A}/k.txt" not in pl, "k.txt is the keeper, not a duplicate"
    assert set(pl) == {f"{C}/y.bin"} and pl[f"{C}/y.bin"]["reasons"][0] == "LAST_COPY_PROTECTED"


def test_zero_byte(tmp_path):
    """G6: zero-byte objects stay off both lists, and the switch is real."""
    d = tmp_path / "zerobyte"
    snap, terra = write_fixtures(d)

    out1 = str(d / "keep")
    r1 = cand("--snapshot", snap, "--terra", terra, "--out-dir", out1)
    assert r1.returncode == 0, f"default run exits 0 (rc={r1.returncode}) {r1.stderr[-160:]}"
    both1 = both_lists(out1)
    zero = {NAMES[k] for k in EXPECTED_ZERO_BYTE}
    assert not (zero & both1), f"default: no zero-byte object on either list (hit {sorted(zero & both1)[:3]})"
    assert "zero-byte objects kept off both lists" in r1.stdout, \
        "the run says how many zero-byte objects it held back"

    out2 = str(d / "list")
    r2 = cand("--snapshot", snap, "--terra", terra, "--out-dir", out2, "--include-zero-byte")
    assert r2.returncode == 0, f"--include-zero-byte exits 0 (rc={r2.returncode})"
    both2 = both_lists(out2)
    revive = {NAMES[k] for k in ZERO_BYTE_REVIVE}
    assert revive <= both2, f"--include-zero-byte brings the dup 0-byte row back (missing {sorted(revive - both2)[:3]})"
    assert both1 <= both2 and (both2 - both1) <= zero, \
        "the flag only ADDS zero-byte rows; it never drops or rewrites a decision"

    rows = jsonl_rows(os.path.join(out2, f"{BUCKET}.cleanup.jsonl"))
    assert rows[NAMES["d9b"]]["duplicate"]["kept"] == NAMES["d9a"], \
        "under the flag, keep of the 0-byte tie is the newer copy (d9a) -- unchanged rule"
    assert NAMES["d9a"] not in rows, "and the kept copy of the pair is never itself a delete row"


def test_sidecar_pairs(tmp_path):
    """G7: a sidecar is not deleted while its data file survives."""
    d = tmp_path / "sidecar"
    # DATA keeper and SIDECAR keeper land in different submissions: both groups
    # rank 1 (Done, non-cacheCopy) so the tie-break is newest updated: aaaa1111
    # wins the BAM, bbbb2222 wins the .md5.
    objs = [
        ("submissions/aaaa1111/WGS/x.bam", 100, "mA", "2026-06-20T00:00:00Z"),   # keeper
        ("submissions/aaaa1111/WGS/x.bam.md5", 10, "mB", "2026-06-01T00:00:00Z"),  # would split
        ("submissions/bbbb2222/WGS/x.bam", 100, "mA", "2026-06-01T00:00:00Z"),   # candidate
        ("submissions/bbbb2222/WGS/x.bam.md5", 10, "mB", "2026-06-20T00:00:00Z"),  # keeper
    ]
    snap, terra = write_snapshot(d, objs, SUBS)

    def listed(outdir):
        return jsonl_names(os.path.join(outdir, f"{BUCKET}.cleanup.jsonl"))

    out1 = str(d / "keep")
    r1 = cand("--snapshot", snap, "--terra", terra, "--out-dir", out1)
    assert r1.returncode == 0, f"default run exits 0 (rc={r1.returncode}) {r1.stderr[-200:]}"
    l1 = listed(out1)
    assert "submissions/bbbb2222/WGS/x.bam" in l1, f"the dup BAM is still a candidate (got {sorted(l1)})"
    assert "submissions/aaaa1111/WGS/x.bam.md5" not in l1, \
        "G7: the SURVIVING BAM's checksum is held back instead of deleted"
    assert l1 == {"submissions/bbbb2222/WGS/x.bam"}, f"and nothing else is listed (got {sorted(l1)})"
    assert "sidecars kept with surviving data" in r1.stdout, "run reports how many sidecars G7 held"

    out2 = str(d / "split")
    r2 = cand("--snapshot", snap, "--terra", terra, "--out-dir", out2, "--allow-index-split")
    assert r2.returncode == 0, f"--allow-index-split exits 0 (rc={r2.returncode})"
    l2 = listed(out2)
    assert "submissions/aaaa1111/WGS/x.bam.md5" in l2, \
        "--allow-index-split is real: the surviving BAM's checksum becomes deletable again"
    assert l1 <= l2 and (l2 - l1) == {"submissions/aaaa1111/WGS/x.bam.md5"}, \
        f"the flag only ADDS the split row (diff {sorted(l2 - l1)})"

    # G7 must survive the policy moves: a sidecar cleared while its data was dying
    # becomes an orphan the moment G9/last-copy pulls that data back.
    d2 = tmp_path / "sidecar-postmove"
    objs2 = [
        ("submissions/cccc3333/WGS/SAMP9.cram", 4000, "mC", "2026-05-01T00:00:00Z"),
        ("submissions/cccc3333/WGS/SAMP9.cram.crai", 40, "mI", "2026-05-01T00:00:00Z"),
    ]
    snap2, terra2 = write_snapshot(d2, objs2, SUBS)
    ref2 = d2 / "map.tsv"
    ref2.write_text("SAMP9\tgs://elsewhere/SAMP9.rb.g.vcf.gz\n")
    out4 = str(d2 / "policy")
    r4 = cand("--snapshot", snap2, "--terra", terra2, "--out-dir", out4,
              "--aborted-last-copy-deletable", "--reference-list", ref2)
    assert r4.returncode == 0, f"post-move run exits 0 (rc={r4.returncode}) {r4.stderr[-200:]}"
    l4 = listed(out4)
    assert "submissions/cccc3333/WGS/SAMP9.cram" not in l4, "G9 keeps the mapped CRAM off"
    assert "submissions/cccc3333/WGS/SAMP9.cram.crai" not in l4, \
        "G7 re-applied: its index is not left behind on the delete list"

    # G9 is form-based: a deliverable-grade last copy is held even when NOTHING in
    # the reference lists names it.
    d3 = tmp_path / "sidecar-formbased"
    objs3 = [("submissions/cccc3333/JG/call-GenotypeGVCFs/shard-19/CALLSET_2026.19.vcf.gz",
              5000, "mV", "2026-05-01T00:00:00Z"),
             ("submissions/cccc3333/WGS/plain.bam", 5000, "mP", "2026-05-01T00:00:00Z")]
    snap3, terra3 = write_snapshot(d3, objs3, SUBS)
    out5 = str(d3 / "policy")
    r5 = cand("--snapshot", snap3, "--terra", terra3, "--out-dir", out5,
              "--aborted-last-copy-deletable")          # deliberately NO reference list
    assert r5.returncode == 0, f"form-based run exits 0 (rc={r5.returncode}) {r5.stderr[-200:]}"
    l5 = listed(out5)
    assert "submissions/cccc3333/JG/call-GenotypeGVCFs/shard-19/CALLSET_2026.19.vcf.gz" not in l5, \
        "G9: a .vcf.gz last copy is NOT promoted even with no reference list at all"
    assert "submissions/cccc3333/WGS/plain.bam" in l5, \
        "while a .bam last copy still is -- the rule is the form, not the words"


def test_aborted_last_copy_policy(tmp_path):
    """The owner ruling (aborted last copies are disposable), plus the G8/G9 carve-outs."""
    d = tmp_path / "abortedpolicy"
    objs = [
        ("submissions/cccc3333/WGS/junk.bam", 500, "mJ", "2026-05-01T00:00:00Z"),
        ("submissions/cccc3333/WGS/MAPPED.g.vcf.gz", 600, "mM", "2026-05-01T00:00:00Z"),
        ("submissions/cccc3333/WGS/SAMP1.g.vcf.gz", 700, "mS", "2026-05-01T00:00:00Z"),
        ("submissions/cccc3333/WGS/nomd5.cram", 800, None, "2026-05-01T00:00:00Z"),
        # G9 is deliberately NOT extended to .bam
        ("submissions/cccc3333/WGS/SAMP1.aligned.bam", 900, "mB1", "2026-05-01T00:00:00Z"),
    ]
    snap, terra = write_snapshot(d, objs, SUBS)
    ref = d / "sample_name_map.tsv"
    ref.write_text(f"SAMP1\tgs://{BUCKET}/somewhere/else/SAMP1.rb.g.vcf.gz\n"
                   f"MAPPED\tgs://{BUCKET}/submissions/cccc3333/WGS/MAPPED.g.vcf.gz\n")

    def lists(outdir):
        return (jsonl_rows(os.path.join(outdir, f"{BUCKET}.cleanup.jsonl")),
                jsonl_rows(os.path.join(outdir, f"{BUCKET}.cleanup.protected.jsonl")))

    # (a) default: the ruling is NOT applied, everything stays a review row
    out1 = str(d / "default")
    r1 = cand("--snapshot", snap, "--terra", terra, "--out-dir", out1, "--reference-list", ref)
    assert r1.returncode == 0, f"default run exits 0 (rc={r1.returncode}) {r1.stderr[-200:]}"
    dele1, prot1 = lists(out1)
    assert not dele1, f"default: nothing on the delete list (got {sorted(dele1)})"
    assert "submissions/cccc3333/WGS/junk.bam" in prot1, \
        "default: the aborted intermediate is a review row, not deletable"
    assert "submissions/cccc3333/WGS/MAPPED.g.vcf.gz" not in prot1 \
        and "submissions/cccc3333/WGS/MAPPED.g.vcf.gz" not in dele1, \
        "G8: an object named by URI in a reference list is on NEITHER list"
    assert "aborted_last_copy=protected" in first_line(os.path.join(out1, f"{BUCKET}.cleanup.tsv")), \
        "the header records the default policy"

    # (b) policy on: promote, but honour G9 and the no-md5 rule
    out2 = str(d / "policy")
    r2 = cand("--snapshot", snap, "--terra", terra, "--out-dir", out2,
              "--reference-list", ref, "--aborted-last-copy-deletable")
    assert r2.returncode == 0, f"policy run exits 0 (rc={r2.returncode}) {r2.stderr[-200:]}"
    dele2, prot2 = lists(out2)
    assert "submissions/cccc3333/WGS/junk.bam" in dele2 \
        and dele2["submissions/cccc3333/WGS/junk.bam"]["reasons"][0] == "ABORTED_LAST_COPY", \
        "the aborted intermediate is promoted with its own reason"
    assert "submissions/cccc3333/WGS/SAMP1.g.vcf.gz" in prot2 \
        and "submissions/cccc3333/WGS/SAMP1.g.vcf.gz" not in dele2, \
        "G9: a final product whose SAMPLE is named in a reference list stays protected"
    assert "G9" in prot2["submissions/cccc3333/WGS/SAMP1.g.vcf.gz"].get("note", ""), \
        "and the review row says why it was held back"
    assert "submissions/cccc3333/WGS/nomd5.cram" in prot2 \
        and "submissions/cccc3333/WGS/nomd5.cram" not in dele2, \
        "no digest, no promotion: an md5-less row is never moved to the delete list"
    assert "aborted_last_copy=deletable" in first_line(os.path.join(out2, f"{BUCKET}.cleanup.tsv")), \
        "the header records the policy this manifest was built under"
    assert "OWNER POLICY" in r2.stdout and "PROMOTED" in r2.stdout, \
        "the run states how many rows the policy moved"
    assert "submissions/cccc3333/WGS/SAMP1.aligned.bam" in dele2, \
        "G9 does NOT protect a .bam even when its sample is named in a reference list"

    # (c) without the list: G9 still covers deliverable forms; G8 is what the list buys
    out3 = str(d / "noref")
    r3 = cand("--snapshot", snap, "--terra", terra, "--out-dir", out3,
              "--aborted-last-copy-deletable")
    assert r3.returncode == 0, f"noref run exits 0 (rc={r3.returncode}) {r3.stderr[-200:]}"
    dele3, prot3 = lists(out3)
    assert "submissions/cccc3333/WGS/SAMP1.aligned.bam" in dele3, \
        "without --reference-list, a mapped sample's BAM is promoted"
    assert "submissions/cccc3333/WGS/SAMP1.g.vcf.gz" not in dele3 \
        and "submissions/cccc3333/WGS/MAPPED.g.vcf.gz" not in dele3, \
        "but both gVCFs stay held with NO list at all -- G9 is the form, not the words"
    assert "submissions/cccc3333/WGS/MAPPED.g.vcf.gz" not in dele2, \
        "and with the list, the URI-named object is off both lists (G8)"


def test_reference_list_shapes(tmp_path):
    """The key column is not always column 1 (id maps, PEDs, wide metrics tables)."""
    d = tmp_path / "reflists"
    d.mkdir()
    idmap = d / "gatksv_sample_id_map.tsv"
    idmap.write_text("entity:sample_id\tgatk_sv_id\n10175_F\t__10175_f__e95ade\n")
    ped = d / "cohort.ped"
    ped.write_text("FAM1\tSAMP_KID\tSAMP_DAD\tSAMP_MUM\t1\t2\n")
    wide = d / "callset_per_sample_metrics.tsv"
    wide.write_text("SAMPLE_ALIAS\t" + "\t".join(f"M{i}" for i in range(9)) + "\n"
                    "PSQ_UNC_30239_C\t" + "\t".join("2.4707" for _ in range(9)) + "\n")
    mapf = d / "sample_name_map.tsv"
    mapf.write_text("SAMP_X\tgs://b/x.g.vcf.gz\n")

    uris, names = candidates.load_reference_lists([str(idmap), str(ped), str(wide), str(mapf)])
    assert "__10175_f__e95ade" in names, "id map column 2 is a sample name"
    assert "10175_F" in names, "and column 1 still is too"
    assert {"SAMP_KID", "SAMP_DAD", "SAMP_MUM"} <= names, "PED columns 2-4 are sample names"
    assert "FAM1" in names, "the family id is kept too"
    assert "1" not in names and "2" not in names, "PED sex/phenotype columns are not sample names"
    numsamp = d / "mosaic_sample_map.tsv"
    numsamp.write_text("sample\tcohort\tmosaic_set\n007\tBCH\t\n014\tBCH\t\n")
    u2, n2 = candidates.load_reference_lists([str(numsamp), str(ped)])
    assert {"007", "014"} <= n2, \
        f"a numeric column-1 sample id is kept (got {sorted(x for x in n2 if x.isdigit())})"
    assert "1" not in n2 and "2" not in n2, "while numeric LATER columns are still dropped"
    assert "PSQ_UNC_30239_C" in names, "a WIDE metrics table contributes its column 1"
    assert "2.4707" not in names, "and NOT its measurements"
    assert not any(n.startswith("M") and len(n) == 2 for n in names), \
        "nor a wide table's other column headers"
    assert uris == {"gs://b/x.g.vcf.gz"}, f"every gs:// URI is collected for G8 (got {uris})"


def test_prefix_slash(tmp_path):
    sib_sub = [{"submissionId": "cccc3333X", "submissionDate": "2026-05-01T00:00:00Z",
                "status": "Aborted", "submitter": "t", "config": "x",
                "entity": "e", "n_workflows": 1, "workflow_status_counts": {}}]
    sib_objs = [("submissions/cccc3333X/WGS/wf3x/sib1.bam", 10, "sibmd5a", "2026-05-01T00:00:00Z"),
                ("submissions/cccc3333X/WGS/wf3x/sib2.bam", 10, "sibmd5b", "2026-05-01T00:00:00Z")]
    d = tmp_path / "ps"
    snap, terra = write_snapshot(d, list(S.values()) + sib_objs, SUBS + sib_sub,
                                 referenced=[f"gs://{BUCKET}/{NAMES['d6']}"])
    outdir = str(d / "out")
    r = cand("--snapshot", snap, "--terra", terra, "--out-dir", outdir,
             "--prefix", "submissions/cccc3333")  # NO trailing slash on purpose
    assert r.returncode == 0, f"exit 0 (got {r.returncode}) {r.stderr[-300:]}"
    assert "effective --prefix: ['submissions/cccc3333/']" in r.stdout, \
        "prefix normalized to trailing slash and echoed"
    names = jsonl_names(os.path.join(outdir, f"{BUCKET}.cleanup.jsonl"))
    assert not any(n.startswith("submissions/cccc3333X/") for n in names), \
        "sibling submission cccc3333X NOT swept in by slash-less prefix"
    assert any(n.startswith("submissions/cccc3333/") for n in names), \
        "intended submission cccc3333 IS still covered"


def _ctx(path, ref, workspace="ws"):
    with open(path, "w") as f:
        json.dump({"namespace": "ns", "workspace": workspace, "bucket": BUCKET,
                   "isLocked": False, "captured_utc": "2026-09-11T01:00:00+00:00",
                   "workspace_attributes": {}, "entity_counts": {},
                   "referenced_gs_uris": ref, "n_referenced": len(ref),
                   "submissions": SUBS}, f)
    return str(path)


def test_repeatable_terra(tmp_path):
    d = tmp_path / "rt2"
    snap, t1 = write_snapshot(d, list(S.values()), SUBS,
                              referenced=[f"gs://{BUCKET}/{NAMES['d6']}"])
    # second context: SAME bucket, references d1c (currently on the delete list)
    t2 = _ctx(d / "terra2.json", [f"gs://{BUCKET}/{NAMES['d1c']}",
                                  f"gs://{BUCKET}/{NAMES['d6']}"], workspace="ws2")
    outdir = str(d / "out")
    r = cand("--snapshot", snap, "--terra", t1, "--terra", t2, "--out-dir", outdir)
    assert r.returncode == 0, f"exit 0 (got {r.returncode}) {r.stderr[-300:]}"
    names = jsonl_names(os.path.join(outdir, f"{BUCKET}.cleanup.jsonl"))
    assert NAMES["d1c"] not in names, "G3 now protects d1c via the union of both contexts"
    assert "referenced objects skipped" in r.stdout, "summary still reports referenced skips"


def test_repeatable_terra_union(tmp_path):
    # pins UNION semantics: a first-wins or last-wins single-file implementation
    # passes at most one of the two orderings below.
    d = tmp_path / "ru"
    objs = list(S.values())
    ref_a1c = [f"gs://{BUCKET}/{NAMES['d1c']}"]
    ref_d6 = [f"gs://{BUCKET}/{NAMES['d6']}"]
    snap, t1 = write_snapshot(d, objs, SUBS, referenced=ref_d6)
    t2 = _ctx(d / "terra2.json", ref_a1c)
    out1, out2 = str(d / "o1"), str(d / "o2")
    rA = cand("--snapshot", snap, "--terra", t1, "--terra", t2, "--out-dir", out1)
    assert rA.returncode == 0, f"run A exit 0 (got {rA.returncode}) {rA.stderr[-300:]}"
    assert NAMES["d1c"] not in jsonl_names(os.path.join(out1, f"{BUCKET}.cleanup.jsonl")), \
        "union protects d1c via SECOND context"
    snap2, t1b = write_snapshot(d, objs, SUBS, referenced=ref_a1c)
    t2b = _ctx(d / "terra2b.json", ref_d6)
    rB = cand("--snapshot", snap2, "--terra", t1b, "--terra", t2b, "--out-dir", out2)
    assert rB.returncode == 0, f"run B exit 0 (got {rB.returncode}) {rB.stderr[-300:]}"
    assert NAMES["d1c"] not in jsonl_names(os.path.join(out2, f"{BUCKET}.cleanup.jsonl")), \
        "union protects d1c via FIRST context"


def test_empty_set(tmp_path):
    d = tmp_path / "es"
    snap, terra = write_fixtures(d)
    # a prefix with no objects at all
    r = cand("--snapshot", snap, "--terra", terra, "--out-dir", d / "out", "--prefix", "cram_crai/")
    assert r.returncode == 0, f"exit 0 (got {r.returncode}) {r.stderr[-300:]}"
    assert "[EMPTY" in r.stdout and "check --prefix" in r.stdout, \
        "empty candidate+protected set is flagged on the PASS line"


def test_overwrite_protection(tmp_path):
    d = tmp_path / "ow"
    snap, terra = write_fixtures(d)
    outdir = str(d / "out")
    r1 = cand("--snapshot", snap, "--terra", terra, "--out-dir", outdir)
    assert r1.returncode == 0, "first run exit 0"
    r2 = cand("--snapshot", snap, "--terra", terra, "--out-dir", outdir)
    assert r2.returncode != 0, "second run refuses to overwrite existing outputs"
    assert "refusing to overwrite" in r2.stderr, "refusal message names the problem"
    r3 = cand("--snapshot", snap, "--terra", terra, "--out-dir", outdir, "--force")
    assert r3.returncode == 0, "--force allows the overwrite"


def test_max_snapshot_age(tmp_path):
    d = tmp_path / "age"
    snap, terra = write_snapshot(d, list(S.values()), SUBS,
                                 snapshot_utc="2020-01-01T00:00:00+00:00")
    r = cand("--snapshot", snap, "--terra", terra, "--out-dir", d / "out",
             "--max-snapshot-age", "30")
    assert r.returncode != 0, "ancient snapshot refused under --max-snapshot-age 30"
    assert "re-capture" in r.stderr, "message tells the operator to re-capture"


def test_missing_snapshot_utc_warning(tmp_path):
    """Renamed-wording check: the WARNING points at `terra-scrub snapshot`."""
    d = tmp_path / "nosnaputc"
    snap, terra = write_snapshot(d, list(S.values()), SUBS, snapshot_utc=None)
    r = cand("--snapshot", snap, "--terra", terra, "--out-dir", d / "out")
    assert r.returncode == 0, f"runs without snapshot_utc (rc={r.returncode}) {r.stderr[-300:]}"
    assert "WARNING" in r.stderr and "re-capture with `terra-scrub snapshot`" in r.stderr
    assert "# gcs_cleanup_candidates:" in first_line(os.path.join(d / "out", f"{BUCKET}.cleanup.tsv"))


def test_capture_ordering(tmp_path):
    """The listing and the terra read must happen in that order."""
    d = tmp_path / "ordering"
    # context read 1 h BEFORE the object listing
    snap, terra = write_snapshot(d, list(S.values()), SUBS,
                                 captured_utc="2026-09-10T23:00:00+00:00")
    r = cand("--snapshot", snap, "--terra", terra, "--out-dir", d / "out1")
    assert r.returncode != 0, "a context older than the listing is refused"
    assert "predate the object snapshot" in r.stderr, "message names the capture-ordering problem"
    assert "re-capture the context AFTER the listing" in r.stderr, "message gives the fix"
    out1 = d / "out1"
    assert not out1.exists() or not [f for f in os.listdir(out1) if f.endswith(".cleanup.tsv")], \
        "no delete list is written on the refusal path"
    r2 = cand("--snapshot", snap, "--terra", terra, "--out-dir", d / "out2", "--allow-stale-context")
    assert r2.returncode == 0, f"--allow-stale-context still runs (rc={r2.returncode})"
    assert "NOT protected" in r2.stderr, "--allow-stale-context warns loudly on stderr"
    r3 = cand("--snapshot", snap, "--terra", terra, "--out-dir", d / "out3",
              "--max-context-skew", "2")
    assert r3.returncode == 0, f"--max-context-skew 2 tolerates a 1h skew (rc={r3.returncode})"
    assert "within --max-context-skew" in r3.stderr, "a tolerated skew is a NOTE, not a stop"
    assert "WARNING" not in r3.stderr, "a tolerated skew is not dressed up as a warning"


def test_context_undated(tmp_path):
    d = tmp_path / "undated"
    snap, terra = write_snapshot(d, list(S.values()), SUBS, captured_utc=None)
    r = cand("--snapshot", snap, "--terra", terra, "--out-dir", d / "out")
    assert r.returncode != 0, "a context with no captured_utc is refused"
    assert "captured_utc" in r.stderr, "message says which key is unparseable/missing"


def test_top_negative(tmp_path):
    d = tmp_path / "top"
    snap, terra = write_fixtures(d)
    r = cand("--snapshot", snap, "--terra", terra, "--out-dir", d / "out", "--top", "-1")
    assert r.returncode == 0, f"--top -1 does not crash (rc={r.returncode}) {r.stderr[-300:]}"
    assert "top 0 candidates" in r.stdout, "--top -1 clamped to 0 rows"
