"""candidates — build a reviewable list of GCS objects to delete.

READ-ONLY: this command only reads local snapshot/context files and writes local
report files. It issues no GCS or Terra requests at all and contains no
delete/move code path. Deletion (if ever approved) happens elsewhere, by a
human, against the produced list (via `terra-scrub plan` / `approve`).

Inputs (produced by `terra-scrub snapshot` and `terra-scrub context`):
    --snapshot <uuid>.jsonl   one-pass object metadata snapshot of the bucket
                              (must carry its __done__ integrity marker — a
                              truncated/partial snapshot is REFUSED by
                              read_snapshot, not silently analyzed)
    --terra    <uuid>.terra.json  workspace attrs + referenced URIs +
                              submissions. REPEATABLE: pass one context per
                              workspace that can see this bucket; referenced
                              URIs and submission statuses are unioned, and
                              every context MUST carry a 'bucket' key matching
                              the snapshot's bucket (missing/null/mismatched
                              -> hard stop).

Rules (a file may match both; reasons are unioned):
  ABORTED_SUBMISSION
      Every object under submissions/<id>/ whose submission status is
      Aborted or Failed. Marked `superseded=True/False` when its md5 does /
      does not match an object outside that submission (False = only copy of
      that content anywhere in the bucket: regenerable in most pipeline
      cases, but flagged for human review).
  EXACT_DUPLICATE
      Every object (under the candidate prefix) whose md5 also occurs
      elsewhere, EXCEPT one canonical "kept" copy per md5 group. Keep
      selection (rank; lower wins, ties -> newest updated, then name):
        0  outside the candidate prefix (deliverables)
        1  under a Done submission, non-cacheCopy
        2  under a Done submission, cacheCopy
        3  under an in-flight/unknown submission
        4  under a dead (Aborted/Failed) submission, non-cacheCopy
        5  under a dead submission, cacheCopy

Safety guards (asserted before any output is written; the run aborts if any
fails). Rationale for each lives in docs/SAFETY.md § G<n>:
  G1  candidates only under --prefix (default submissions/; normalized to a
      trailing slash so 'submissions/cccc3333' cannot sweep in sibling
      'submissions/cccc3333X/'), and only under submissions with terminal
      status (Done for the duplicate rule, Aborted/Failed for the aborted
      rule) — never under in-flight ones
  G2  every md5 group keeps >= 1 copy in the bucket. Enforced by removing
      the canonical kept copy from the delete list when the list would
      otherwise delete ALL copies of an md5 (incl. unique-md5 objects under
      aborted subs); those protected objects go to the .protected.tsv review
      list instead and are the ONLY part a human may override. Objects with
      NO md5Hash under a dead sub are routed to the same review list
      (reason NO_MD5_PROTECTED): their uniqueness is unverifiable.
  G3  no candidate is referenced by any Terra workspace/entity attribute
      (union of all --terra contexts)
  G4  no object without an md5 is ever listed as EXACT_DUPLICATE
  G5  Cromwell's own execution records are kept off BOTH lists: `stdout`,
      `stderr`, `output`, `rc`, `memory_retry_rc`, `exec.sh`, `script`,
      `gcs_{localization,delocalization,transfer}.sh`, anything ending `.log`
      or `-rc.txt`. They are a negligible fraction of a bucket's bytes and
      the only surviving record of what each shard actually ran -- the
      per-call return codes are how a failed pipeline is diagnosed weeks
      later. Mirrors `fissfc mop`'s `can_delete()` (firecloud/fiss.py), which
      has always kept these. Opt out with --include-provenance.
      (docs/SAFETY.md § G5)

  G6  Zero-byte objects are kept off BOTH lists. They free no storage, so there
      is no upside, and they all share the empty-file md5 -- which makes the
      EXACT_DUPLICATE rule degenerate: `x.gc_bias.pdf` would be deleted because
      `x.bamout.bam` is also empty, which is not deduplication. Worse, an empty
      named output is evidence that a task RAN AND PRODUCED NOTHING; delete it and
      that becomes indistinguishable from a task that never ran.
      Opt out with --include-zero-byte. (docs/SAFETY.md § G6)

  G7  A sidecar (.bai/.crai/.csi/.tbi/.idx/.md5/.sbi/.fai) is never deleted while
      its data file survives in the bucket, and therefore a kept copy never loses
      its own index or checksum. Without this, EXACT_DUPLICATE splits companion
      pairs: two attempts of the same shard have byte-identical `.md5` sidecars, so
      one is deleted -- leaving the surviving attempt's BAM with no checksum.
      Opt out with --allow-index-split. (docs/SAFETY.md § G7)

  G8  An object whose URI appears inside a file passed with --reference-list is
      never a candidate on either list. G3 only sees Terra workspace/entity
      ATTRIBUTES, so it is blind to a file whose contents point at objects -- a
      delivered callset's input map can name outputs inside an ABORTED
      submission, and without G8 those survive only if G2 happens to keep them
      as md5-group keepers, not because anything understood the map.
      (docs/SAFETY.md § G8)
  G9  A deliverable-grade product (.g.vcf.gz/.cram/.vcf.gz + indexes, NOT .bam) is
      never promoted off the review list by --aborted-last-copy-deletable, whether or
      not its sample is named in a reference list. Name matching alone is not
      enough, because a callset name is not a sample name: last-copy shards of a
      delivered callset match no reference list. (docs/SAFETY.md § G9)

Other operator protections:
  - refuses to overwrite existing output files unless --force is given
  - --max-snapshot-age N: refuses to run on a snapshot older than N days
  - an empty candidate+protected set is flagged on the PASS line
    ([EMPTY: ...]) so a typo'd prefix cannot masquerade as a healthy run
  - every run prints snapshot/terra capture times and the snapshot age

Outputs (in --out-dir):
    <bucket>.cleanup.tsv            one row per candidate, name first (pipe-friendly)
    <bucket>.cleanup.jsonl          same, full evidence per object
    <bucket>.cleanup.protected.tsv  LAST-COPY objects held back from the delete
                                    list (human review required)
    summary printed to stdout

General usage (any workspace with a captured snapshot + terra context):
    terra-scrub candidates \\
        --snapshot runs/inv/<uuid>.jsonl \\
        --terra runs/inv/<uuid>.terra.json \\
        --out-dir runs/cleanup/<uuid>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime

from terra_scrub.snapshot import read_snapshot
from terra_scrub.util import human, parse_ts, tsv_escape

DONE = {"Done"}
DEAD = {"Aborted", "Failed"}
# anything else (Submitted, Running, Pending, or an unknown id) is in-flight:
# its exec dir is protected from the candidate list entirely.

# G5 provenance keep-list: Cromwell's execution-record files, by basename.
# Same vocabulary as `fissfc mop`'s can_delete(); suffix rules are handled in
# is_provenance(). Kept out of BOTH lists unless --include-provenance.
PROVENANCE_NAMES = {"rc", "memory_retry_rc", "exec.sh", "script", "output",
                    "stdout", "stderr",
                    "gcs_localization.sh", "gcs_delocalization.sh",
                    "gcs_transfer.sh"}


def is_provenance(name):
    """True for Cromwell's per-call execution records (logs, return codes,
    localization scripts). G5: these are kept off both output lists."""
    base = name.rsplit("/", 1)[-1]
    if base.endswith(".log") or base.endswith("-rc.txt"):
        return True
    return base in PROVENANCE_NAMES


# G7: companion files. A sidecar is useless without its data and, more to the point,
# data whose sidecar is deleted loses its index / checksum -- "a byte-identical copy
# survives elsewhere" is a statement about bytes, not about a file a caller can seek
# into or verify. Suffixes are matched on the full name, so foo.bam.bai pairs with
# foo.bam and foo.cram.crai with foo.cram.
SIDECAR_SUFFIXES = (".bai", ".crai", ".csi", ".tbi", ".idx", ".md5", ".sbi", ".fai")


def sidecar_of(name):
    """The data file this name is a companion to, or None."""
    for suf in SIDECAR_SUFFIXES:
        if name.endswith(suf) and len(name) > len(suf):
            return name[: -len(suf)]
    return None


# G9 protects DELIVERABLE-GRADE products only: what a callset consumes (gVCF), what
# the archive holds (CRAM), and per-sample VCFs -- plus their indexes. Deliberately
# NOT .bam: an aligned/unmapped BAM under an aborted run is an intermediate, and
# keeping it merely because its sample appears in a map buys no deliverable
# benefit while holding back the bulk of the reclaimable bytes (docs/SAFETY.md § G9).
FINAL_SUFFIXES = (".g.vcf.gz", ".g.vcf.gz.tbi", ".cram", ".cram.crai",
                   ".vcf.gz", ".vcf.gz.tbi")


def load_reference_lists(paths):
    """Contents of files that NAME objects or samples -- joint-call maps, gVCF
    lists, sample tables, id maps, PEDs.

    G3 only sees Terra workspace/entity ATTRIBUTES, so it is blind to a file whose
    *contents* point at objects. That blindness is not theoretical: a delivered
    callset's input map can name outputs inside an ABORTED submission, and those
    inputs would survive only by the accident of G2 keeping them as md5-group
    keepers (docs/SAFETY.md § G8).

    Which cells count as a sample name depends on the file's shape, because the key
    column is not always the first:

      * WIDE table (>6 columns, e.g. a per-sample metrics table) -> column 1.
        Every other column is a measurement.
      * NARROW file (<=6 columns: sample_name_map, id maps, PEDs, plain lists) ->
        every non-numeric cell. An id map may key objects by its SECOND column
        (e.g. a gatk-sv sample id map), and a PED keys the sample in column 2
        while column 1 is the family -- column-1-only would miss both, and G9
        would then fail to protect exactly the SV and trio material it exists for.

    Numeric cells, `-`, `.`, `NA`/`NaN` and 1-character cells are dropped, so a PED's
    sex/phenotype columns and a ploidy table's counts do not become "sample names".

    Returns (uris, sample_names).
    """
    uris, names = set(), set()
    DROP = {"-", ".", "na", "nan", "null", "none"}   # never a sample name

    def numeric(cell):
        try:
            float(cell)
            return True
        except ValueError:
            return False

    for path in paths:
        with open(path, errors="replace") as f:
            lines = [ln.rstrip("\n") for ln in f]
        wide = max((ln.count("\t") for ln in lines[:200]), default=0) + 1 > 6
        for line in lines:
            if not line.strip():
                continue
            cells = line.replace("\t", " ").split()
            for cell in cells:
                if cell.startswith("gs://"):
                    uris.add(cell.rstrip('",\';'))
            if line.lstrip().startswith("#"):
                continue
            fields = line.split("\t") if "\t" in line else line.split()
            keep = fields[:1] if wide else fields
            for i, cell in enumerate(keep):
                cell = cell.strip()
                if not cell or cell.startswith("gs://") or len(cell) < 2:
                    continue
                if cell.lower() in DROP:
                    continue
                # Column 1 is a sample name even when it looks like a number: some
                # cohorts literally name samples `007`, `014`. The numeric filter
                # applies only to the LATER columns of a narrow file, which is
                # where a PED's sex/phenotype and a ploidy table's counts live.
                if i > 0 and numeric(cell):
                    continue
                names.add(cell)
    return uris, names


def sub_status_of(subs, sid):
    if sid is None:
        return "UNKNOWN"
    s = subs.get(sid)
    return s.get("status", "UNKNOWN") if s else "UNKNOWN"


def keep_rank(name, prefix, subs):
    parts = name.split("/")
    in_prefix = name.startswith(prefix) if isinstance(prefix, str) \
        else any(name.startswith(p) for p in prefix)
    if not in_prefix:
        return 0
    is_cc = "cacheCopy" in parts
    if parts[0] == "submissions" and len(parts) > 1:
        st = sub_status_of(subs, parts[1])
        if st in DONE:
            return 1 if not is_cc else 2
        if st in DEAD:
            return 4 if not is_cc else 5
    return 3  # in-flight / unknown


def _upd_ts(o):
    """Defensive timestamp parse for keep-selection; bad/missing -> 0 (oldest)."""
    u = o.get("updated")
    if not u:
        return 0
    try:
        return int(datetime.fromisoformat(u.replace("Z", "+00:00")).timestamp())
    except ValueError:
        print(f"WARNING: unparseable updated={u!r} on {o['name'][:80]!r}; "
              f"treating as oldest", file=sys.stderr)
        return 0


def add_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--terra", action="append", required=True,
                    help="terra-context JSON (repeatable: union references + "
                         "submissions across every workspace that can see this "
                         "bucket; all must name the same bucket)")
    ap.add_argument("--prefix", action="append", default=None,
                    help="candidates restricted to objects under this prefix "
                         "(repeatable; default: submissions/; normalized to a "
                         "trailing slash so a bare 'submissions/cccc3333' cannot "
                         "silently include sibling 'submissions/cccc3333X/')")
    ap.add_argument("--out-dir", default=None, help="default: next to the snapshot")
    ap.add_argument("--top", type=int, default=25, help="top candidates to print")
    ap.add_argument("--max-snapshot-age", type=int, default=0,
                    help="refuse to run if the snapshot is older than N days "
                         "(0 = no check; a point-in-time list against a "
                         "non-versioned bucket should be re-verified before rm -- "
                         "the gate that enforces this lives in `terra-scrub plan`)")
    ap.add_argument("--max-context-skew", type=float, default=0.0,
                    help="how many hours a terra context may PREDATE the object "
                         "snapshot before it is refused (default 0 = any inversion). "
                         "See the capture-ordering guard in the code")
    ap.add_argument("--allow-stale-context", action="store_true",
                    help="downgrade the capture-ordering guard to a warning (for "
                         "reproducing an older run on purpose -- NOT for a list that "
                         "will be executed)")
    ap.add_argument("--include-provenance", action="store_true",
                    help="list Cromwell logs/rc/stdout/stderr as candidates too "
                         "(default: keep them off both lists -- G5, mirrors "
                         "`fissfc mop`, a negligible share of bytes)")
    ap.add_argument("--include-zero-byte", action="store_true",
                    help="list zero-byte objects as candidates too (default: keep "
                         "them off both lists -- G6, they free no bytes and share "
                         "the empty-file md5, so the duplicate rule is meaningless "
                         "for them)")
    ap.add_argument("--allow-index-split", action="store_true",
                    help="permit deleting a sidecar whose data file survives, and "
                         "deleting a kept copy's own index/checksum (default: G7 "
                         "keeps companion pairs together)")
    ap.add_argument("--reference-list", action="append", default=[], metavar="FILE",
                    help="a file whose CONTENTS name objects or samples (joint-call "
                         "sample_name_map, gVCF list, sample table). "
                         "Repeatable. Every gs:// URI in it is protected (G8) and "
                         "every column-1 sample name protects that sample's final "
                         "products from last-copy promotion (G9)")
    ap.add_argument("--aborted-last-copy-deletable", action="store_true",
                    help="OWNER POLICY: treat a LAST_COPY row under an "
                         "Aborted/Failed submission as deletable, moving it to the "
                         "delete list with reason ABORTED_LAST_COPY. Rows with no "
                         "md5 are never promoted, and --reference-list carve-outs "
                         "(G8/G9) still apply. Without this flag the review list is "
                         "unchanged")
    ap.add_argument("--force", action="store_true",
                    help="overwrite existing output files in --out-dir")


def run(args: argparse.Namespace) -> int:
    # prefix normalization: trailing slash so 'submissions/cccc3333' cannot
    # match sibling 'submissions/cccc3333X/'; '' keeps match-everything but is
    # called out loudly below
    prefixes = []
    for p in (args.prefix or ["submissions/"]):
        p = p.rstrip("\n")
        if p and not p.endswith("/"):
            p += "/"
        prefixes.append(p)
    if "" in prefixes:
        print("WARNING: --prefix '' matches EVERY object in the bucket; "
              "candidates will still be gated by terminal submission status",
              file=sys.stderr)

    meta, objs = read_snapshot(args.snapshot)
    bucket = meta["__bucket__"]["name"]
    total = sum(o["size"] for o in objs)

    # ---- terra contexts (repeatable, same-bucket enforced) ------------------
    subs = {}
    referenced = set()
    tc_captured = []
    tc_parsed = []
    for tpath in args.terra:
        with open(tpath) as fh:
            tc = json.load(fh)
        raw_cap = tc.get("captured_utc")
        cap = parse_ts(raw_cap) if raw_cap else None
        if cap is None:
            sys.exit(f"{tpath}: terra context has no parseable 'captured_utc' "
                     f"(got {raw_cap!r}) -- the capture-ordering guard cannot check it, "
                     f"so the context is untrustable; re-capture it")
        tc_captured.append(raw_cap)
        tc_parsed.append((tpath, cap))
        tc_bucket = tc.get("bucket")
        if not tc_bucket:
            sys.exit(f"{tpath}: terra context has no usable 'bucket' key (missing or null) — "
                     f"cannot verify it matches snapshot bucket '{bucket}'. Re-capture the "
                     f"context and retry; refusing to trust an unattributable context")
        if tc_bucket != bucket:
            sys.exit(f"{tpath} is a terra context for bucket '{tc_bucket}' but the "
                     f"snapshot is for '{bucket}' — refusing to mix contexts from "
                     f"different buckets")
        for s in tc.get("submissions", []):
            sid = s.get("submissionId")
            if sid in subs and subs[sid].get("status") != s.get("status"):
                print(f"WARNING: submission {sid} has conflicting statuses across "
                      f"contexts ({subs[sid].get('status')} vs {s.get('status')}); "
                      f"first context wins — re-capture the context and retry",
                      file=sys.stderr)
                continue
            subs[sid] = s
        referenced.update(tc.get("referenced_gs_uris", []))

    # ---- snapshot freshness ---------------------------------------------------
    snap_utc = parse_ts(meta.get("snapshot_utc"))
    if snap_utc is None:
        print("WARNING: snapshot header carries no snapshot_utc -- neither the age check "
              "nor the capture-ordering guard can run; re-capture with "
              "`terra-scrub snapshot` before this list is used for anything",
              file=sys.stderr)
    now_utc = datetime.now(UTC)
    if args.max_snapshot_age and snap_utc:
        age_days = (now_utc - snap_utc).total_seconds() / 86400
        if age_days > args.max_snapshot_age:
            sys.exit(f"snapshot is {age_days:.1f} days old "
                     f"(>{args.max_snapshot_age}d) — re-capture it before generating "
                     f"a delete list (non-versioned bucket: contents may have changed)")

    # ---- capture-ordering guard -------------------------------------------
    # The object listing (snapshot_utc) and the terra contexts (captured_utc) are
    # two separate reads, and their ORDER is load-bearing. A context read BEFORE
    # the listing cannot see a workspace/entity attribute written in between, so a
    # pointer that exists right now can look unreferenced -- and an unreferenced
    # object in a terminal submission is exactly what becomes a delete candidate.
    # Correct order: list the bucket, then read terra. Inversions of a few minutes
    # happen in practice when captures are run by hand (docs/SAFETY.md §2
    # (the ordering invariant)).
    stale = [(p, c) for p, c in tc_parsed
             if snap_utc is not None and c < snap_utc]
    if stale:
        worst_h = max((snap_utc - c).total_seconds() / 3600.0 for _, c in stale)
        violating = [(p, c) for p, c in stale
                     if (snap_utc - c).total_seconds() / 3600.0 > args.max_context_skew]
        if violating and not args.allow_stale_context:
            sys.exit(
                f"{len(violating)} terra context(s) predate the object snapshot by up to "
                f"{worst_h:.2f}h -- refusing to generate a list from them. An attribute "
                f"written in that window would be invisible here and its bytes would look "
                f"unreferenced. Offending: "
                + ", ".join(f"{p} ({(snap_utc - c).total_seconds():.0f}s before the listing)"
                            for p, c in violating[:5])
                + " -- re-capture the context AFTER the listing, or pass "
                  "--allow-stale-context to reproduce an older run knowingly")
        if violating:
            print(f"WARNING: {len(violating)} terra context(s) predate the object snapshot "
                  f"by up to {worst_h:.2f}h (--allow-stale-context): pointers created in "
                  f"that window are NOT protected here, so re-capture and re-verify before "
                  f"any rm", file=sys.stderr)
        else:
            print(f"NOTE: {len(stale)} context(s) predate the object listing by up to "
                  f"{worst_h:.2f}h -- within --max-context-skew {args.max_context_skew:g}h",
                  file=sys.stderr)

    def eligible(o):
        return any(o["name"].startswith(p) for p in prefixes)

    def sub_id(o):
        parts = o["name"].split("/")
        return parts[1] if parts[0] == "submissions" and len(parts) > 1 else None

    # ---- group by md5 (rule 2) --------------------------------------------
    by_md5 = defaultdict(list)
    no_md5 = 0
    for o in objs:
        if o.get("md5Hash"):
            by_md5[o["md5Hash"]].append(o)
        else:
            no_md5 += 1

    dup_of = {}          # name -> kept name (for EXACT_DUPLICATE candidates)
    n_copies = {}        # name -> group size
    keep_by_md5 = {}     # md5 -> name of the canonical kept copy

    def keyfn(o):
        # newest first within rank: sort by rank asc, updated desc, name asc
        return (keep_rank(o["name"], prefixes, subs), -_upd_ts(o), o["name"])

    for md5, group in by_md5.items():
        if len(group) < 2:
            continue
        keep = sorted(group, key=keyfn)[0]
        keep_by_md5[md5] = keep["name"]
        for o in group:
            n_copies[o["name"]] = len(group)
        for o in group:
            if o is not keep:
                dup_of.setdefault(o["name"], keep["name"])  # all but the kept copy

    # ---- build candidate set ----------------------------------------------
    cand = {}
    n_skipped_referenced = 0
    n_inflight_protected = 0
    n_provenance_kept = 0
    n_zero_byte_kept = 0
    n_sidecar_kept = 0
    n_reflist_kept = 0
    ref_uris, ref_names = load_reference_lists(args.reference_list)
    if args.reference_list:
        print(f"   reference lists: {len(args.reference_list)} file(s) -> "
              f"{len(ref_uris):,} gs:// URIs (G8), {len(ref_names):,} sample names (G9)")
    # G7 needs to know what exists and what is already doomed. cand_names is the
    # delete set this loop is building, so a sidecar is only kept when its data
    # file is NOT itself a candidate -- a pair that dies together is fine.
    by_name = {o["name"] for o in objs}
    cand_names = {o["name"] for o in objs
                  if (args.include_provenance or not is_provenance(o["name"]))
                  and (args.include_zero_byte or o["size"] != 0)
                  and eligible(o)
                  and (sub_status_of(subs, sub_id(o)) in DEAD
                       or (sub_status_of(subs, sub_id(o)) in DONE
                           and o["name"] in dup_of))}
    prov_bytes = 0
    for o in objs:
        if not args.include_provenance and is_provenance(o["name"]):
            n_provenance_kept += 1
            prov_bytes += o["size"]
            continue    # G5: execution provenance, not a candidate on either list
        if not args.include_zero_byte and o["size"] == 0:
            n_zero_byte_kept += 1
            continue    # G6
        if f"gs://{bucket}/{o['name']}" in ref_uris:
            n_reflist_kept += 1
            continue    # G8: named inside a map/list file
        if not args.allow_index_split:
            data = sidecar_of(o["name"])
            if data is not None and data in by_name and data not in cand_names:
                n_sidecar_kept += 1
                continue    # G7: its data file is not being deleted
        sid = sub_id(o)
        st = sub_status_of(subs, sid)
        reasons = []
        detail = {}
        if eligible(o):
            if st in DEAD:
                reasons.append("ABORTED_SUBMISSION")
            elif st not in DONE:
                if sid is not None:
                    n_inflight_protected += 1
                continue  # in-flight / unknown: never a candidate
            if o["name"] in dup_of:
                reasons.append("EXACT_DUPLICATE")
        if not reasons:
            continue
        uri = f"gs://{bucket}/{o['name']}"
        if uri in referenced:
            n_skipped_referenced += 1
            continue
        s = subs.get(sid, {}) if sid else {}
        detail["aborted"] = ({"submissionId": sid, "status": st,
                              "submitted": str(s.get("submissionDate", ""))[:10],
                              "config": s.get("config", "?")}
                             if st in DEAD else None)
        # ABORTED only: does an identical copy exist OUTSIDE this submission?
        # unique md5 (or none) -> False = only copy in the bucket, needs review.
        superseded = None
        if st in DEAD:
            g = by_md5.get(o.get("md5Hash") or "", [])
            superseded = any(x["name"] != o["name"] and
                             not x["name"].startswith(f"submissions/{sid}/")
                             for x in g)
        detail["duplicate"] = ({"kept": dup_of[o["name"]],
                                "n_copies": n_copies.get(o["name"])}
                               if o["name"] in dup_of else None)
        detail["superseded"] = superseded
        cand[o["name"]] = {
            "name": o["name"], "size": o["size"], "md5": o.get("md5Hash"),
            "updated": o.get("updated"), "submission_id": sid,
            "submission_status": st, "reasons": reasons, **detail,
        }

    # ---- last-copy protection (G2 by construction) --------------------------
    # If a candidate set would delete EVERY copy of some md5 (or a unique-md5
    # object), the canonical kept copy is pulled off the delete list and put
    # on a separate review list: it is the last copy of that content in the
    # bucket, and only a human may judge that acceptable.
    protected = {}
    for md5, group in by_md5.items():
        deleting = [o for o in group if o["name"] in cand]
        if not deleting or len(deleting) < len(group):
            continue
        if len(group) == 1:
            keep_name = group[0]["name"]
        else:
            keep_name = keep_by_md5.get(md5)
        if keep_name not in cand:
            continue  # the survivor wasn't being deleted anyway
        rec = cand.pop(keep_name)
        rec["reasons"] = ["LAST_COPY_PROTECTED"] + [r for r in rec["reasons"]
                                                    if r != "EXACT_DUPLICATE"]
        rec["note"] = ("only copy of this md5 in the bucket (n_copies=1)"
                       if len(group) == 1 else
                       f"all {len(group)} copies would be deleted; kept as last "
                       "survivor")
        protected[keep_name] = rec

    # ---- no-md5 protection ---------------------------------------------------
    # Objects without an md5Hash can never be proven a duplicate. For dead-sub
    # candidates the "superseded" question is unanswerable, so they go to the
    # review list (same conservatism as last-copy protection). NOTE: this does
    # NOT re-enlarge the delete list — G1/G3 already excluded in-flight and
    # referenced objects before we got here.
    for name, rec in list(cand.items()):
        if rec["submission_status"] in DEAD and not rec.get("md5"):
            rec = cand.pop(name)
            rec["reasons"] = ["NO_MD5_PROTECTED"] + rec["reasons"]
            rec["note"] = ("no md5Hash on this object — cannot verify whether a "
                           "duplicate exists elsewhere; verify before deletion")
            protected[name] = rec
    # ---- owner policy: aborted last copies (docs/SAFETY.md § G2) ------------
    n_promoted = 0
    n_deliverable_kept = 0
    if args.aborted_last_copy_deletable:
        for name, rec in list(protected.items()):
            if rec["submission_status"] not in DEAD:
                continue            # only aborted/failed runs are covered by the ruling
            if not rec.get("md5"):
                continue            # no digest, no plan -- `plan` could not certify it
            # G9 is FORM-based, not name-based. A name-based rule misses last-copy
            # shards of a delivered callset (`<callset>.<shard>.vcf.gz`) -- a
            # callset name is not a sample name, so no reference list could match
            # it. A gVCF/CRAM/VCF is the form a deliverable takes; a last copy of
            # one gets human review, full stop (docs/SAFETY.md § G9).
            if name.endswith(FINAL_SUFFIXES):
                samp = name.rsplit("/", 1)[-1].split(".")[0]
                why = ("sample is named in a reference list" if samp in ref_names
                       else "deliverable-grade form (gVCF/CRAM/VCF)")
                rec["note"] = rec.get("note", "") + f" | KEPT (G9): {why}"
                n_deliverable_kept += 1
                continue
            rec = protected.pop(name)
            rec["reasons"] = ["ABORTED_LAST_COPY"] + [r for r in rec["reasons"]
                                                      if r != "LAST_COPY_PROTECTED"]
            cand[name] = rec
            n_promoted += 1

    # ---- G7, re-applied AFTER every move (docs/SAFETY.md § G7) --------------
    # G7 runs while the candidate set is being built, but the last-copy logic, the
    # no-md5 rule and the aborted-last-copy policy all MOVE rows afterwards. A
    # sidecar cleared earlier because its data was dying too becomes an orphan the
    # moment G9 pulls that data back to the review list (e.g. a .cram.crai whose
    # .cram G9 has just protected). Ordering, not logic: the rule has to be the
    # last word, not an early one.
    if not args.allow_index_split:
        for name in list(cand):
            data = sidecar_of(name)
            if data is not None and data in by_name and data not in cand:
                rec = cand.pop(name)
                rec["reasons"] = ["SIDECAR_OF_KEPT_DATA"] + rec["reasons"]
                rec["note"] = ("its data file is NOT being deleted -- G7 re-applied "
                               "after the last-copy/policy moves")
                protected[name] = rec
                n_sidecar_kept += 1

    n_nomd5_protected = sum(1 for r in protected.values()
                            if r["reasons"][0] == "NO_MD5_PROTECTED")

    # ---- safety guards ------------------------------------------------------
    checks = []
    g1 = all(
        any(r["name"].startswith(p) for p in prefixes) and
        ((r["submission_status"] in DEAD) if "ABORTED_SUBMISSION" in r["reasons"]
         else r["submission_status"] in DONE)
        for r in cand.values())
    checks.append(("G1 prefixes + terminal status only", g1))

    # G2 normally requires a surviving copy of every md5. The owner policy
    # (--aborted-last-copy-deletable) deliberately breaks that for ONE case: a
    # group whose every copy lives under an aborted/failed submission. The
    # exception is scoped to exactly that -- a group with any Done or in-flight
    # copy must still keep one, so the guard keeps its teeth and a policy run
    # cannot quietly wipe live content (docs/SAFETY.md § G2).
    def g2_ok(g):
        if sum(1 for o in g if o["name"] not in cand) >= 1:
            return True
        if not args.aborted_last_copy_deletable:
            return False
        return all(sub_status_of(subs, sub_id(o)) in DEAD for o in g)
    g2 = all(g2_ok(g) for md5, g in by_md5.items() if len(g) >= 2)
    checks.append(("G2 every md5 group keeps >=1 copy"
                   + (" (except all-aborted groups, per policy)"
                      if args.aborted_last_copy_deletable else ""), g2))
    g2b = all(all(sub_status_of(subs, sub_id(o)) in DEAD for o in g)
              for md5, g in by_md5.items() if len(g) >= 2
              and sum(1 for o in g if o["name"] not in cand) == 0)
    checks.append(("G2b a fully-deleted md5 group is all-aborted", g2b))
    g7 = args.allow_index_split or not [
        n for n in cand
        if (sidecar_of(n) or "") in by_name and sidecar_of(n) not in cand]
    checks.append(("G7 no sidecar deleted while its data survives", g7))
    g3 = all(f"gs://{bucket}/{r['name']}" not in referenced for r in cand.values())
    checks.append(("G3 no Terra-referenced object listed", g3))
    g4 = all(r["md5"] for r in cand.values()
             if "EXACT_DUPLICATE" in r["reasons"])
    checks.append(("G4 duplicate rule only uses md5", g4))
    bad = [n for n, ok in checks if not ok]
    empty_note = (f" [EMPTY: {len(cand) + len(protected)} candidates — check --prefix "
                  f"and inputs before trusting this output]"
                  if not cand and not protected else "")
    print(f"safety checks: {'PASS' if not bad else 'FAIL ' + str(bad)} "
          f"({sum(1 for _, ok in checks if ok)}/{len(checks)}){empty_note}")
    if bad:
        sys.exit(1)

    # ---- write outputs ------------------------------------------------------
    out_dir = args.out_dir or os.path.dirname(os.path.abspath(args.snapshot))
    os.makedirs(out_dir, exist_ok=True)
    tsv = os.path.join(out_dir, f"{bucket}.cleanup.tsv")
    js = os.path.join(out_dir, f"{bucket}.cleanup.jsonl")
    ptsv = os.path.join(out_dir, f"{bucket}.cleanup.protected.tsv")
    pjs = os.path.join(out_dir, f"{bucket}.cleanup.protected.jsonl")
    existing = [p for p in (tsv, js, ptsv, pjs) if os.path.exists(p)]
    if existing and not args.force:
        sys.exit("refusing to overwrite existing outputs (use --force to allow):\n  "
                 + "\n  ".join(existing))
    gen_by_name = {o["name"]: o.get("generation", 0) for o in objs}
    prows = sorted(protected.values(), key=lambda r: -r["size"])
    with open(ptsv, "w") as f:
        f.write("# LAST-COPY PROTECTED: human review required before any deletion. "
                f"bucket={bucket} generated={datetime.now(UTC).isoformat()}\n")
        f.write("# name\tsize_bytes\tsub_id\tsub_status\tsub_date\tconfig\tnote\tmd5\n")
        for r in prows:
            a = r.get("aborted") or {}
            f.write("{name}\t{size}\t{sid}\t{st}\t{date}\t{cfg}\t{note}\t{md5}\n".format(
                name=tsv_escape(r["name"]), size=r["size"], sid=r["submission_id"] or "-",
                st=r["submission_status"], date=a.get("submitted", "-"),
                cfg=tsv_escape(a.get("config", "-")), note=tsv_escape(r["note"]),
                md5=r.get("md5") or "-"))
    with open(pjs, "w") as f:
        for r in prows:
            rec = dict(r)
            rec["bucket"] = bucket
            rec["generation"] = gen_by_name.get(r["name"], 0)
            f.write(json.dumps(rec, sort_keys=True) + "\n")
    rows = sorted(cand.values(), key=lambda r: -r["size"])
    with open(tsv, "w") as f:
        # The header tag is kept byte-identical to the original generator so that
        # manifests produced by either tool are interchangeable for `plan`.
        f.write("# gcs_cleanup_candidates: REVIEW before any deletion. "
                f"bucket={bucket} snapshot={meta.get('snapshot_utc')} "
                f"provenance_keep={'off' if args.include_provenance else 'on'} "
                f"aborted_last_copy={'deletable' if args.aborted_last_copy_deletable else 'protected'} "
                f"generated={datetime.now(UTC).isoformat()}\n")
        f.write("# name\tsize_bytes\treasons\tsub_id\tsub_status\tsub_date\tconfig"
                "\tkept_copy\tn_copies\tsuperseded_outside_sub\tmd5\n")
        for r in rows:
            a = r.get("aborted") or {}
            d = r.get("duplicate") or {}
            f.write("{name}\t{size}\t{reasons}\t{sid}\t{st}\t{date}\t{cfg}\t"
                    "{kept}\t{n}\t{sup}\t{md5}\n".format(
                        name=tsv_escape(r["name"]), size=r["size"],
                        reasons="|".join(r["reasons"]),
                        sid=r["submission_id"] or "-", st=r["submission_status"],
                        date=a.get("submitted", "-"), cfg=tsv_escape(a.get("config", "-")),
                        kept=tsv_escape(d.get("kept", "-")), n=d.get("n_copies", "-"),
                        sup=r.get("superseded") if r.get("superseded") is not None else "-",
                        md5=r.get("md5") or "-"))
    with open(js, "w") as f:
        for r in rows:
            rec = dict(r)
            rec["bucket"] = bucket
            rec["generation"] = gen_by_name.get(r["name"], 0)
            f.write(json.dumps(rec, sort_keys=True) + "\n")

    # ---- summary -----------------------------------------------------------
    tot_c = sum(r["size"] for r in rows)
    by_reason = defaultdict(lambda: [0, 0])
    for r in rows:
        for reason in r["reasons"]:
            by_reason[reason][0] += 1
            by_reason[reason][1] += r["size"]
    union_aborted = sum(r["size"] for r in rows
                        if "ABORTED_SUBMISSION" in r["reasons"])
    dup_only = sum(r["size"] for r in rows
                   if r["reasons"] == ["EXACT_DUPLICATE"])
    print(f"\n== cleanup candidates: gs://{bucket} ==")
    print(f"   snapshot: {meta.get('snapshot_utc')} ({len(objs):,} objects in file); "
          f"terra context(s) captured: {', '.join(tc_captured)}")
    snap_age = ((now_utc - snap_utc).total_seconds() / 86400) if snap_utc else None
    if snap_age is not None:
        print(f"   snapshot age at run time: {snap_age:.1f} days — this list is "
              f"point-in-time; re-capture and re-verify before any rm "
              f"(non-versioned bucket)")
    print(f"   effective --prefix: {prefixes}")
    st_seen = Counter(s.get("status", "UNKNOWN") for s in subs.values())
    print(f"   submission statuses seen in terra context: {dict(st_seen)} "
          f"(terminal set: {', '.join(sorted(DONE | DEAD))}; any other status is "
          f"treated as in-flight and protected)")
    print(f"   bucket total: {human(total)} ({len(objs):,} objects)")
    print(f"   candidates:   {len(rows):,} objects, {human(tot_c)} "
          f"({100.0 * tot_c / (total or 1):.1f}% of bucket)")
    for reason, (n, s) in sorted(by_reason.items(), key=lambda x: -x[1][1]):
        print(f"     {reason:<20} {n:>8,} objs  {human(s):>12}  (members, "
              f"overlap allowed)")
    print(f"     union of both rules: {human(tot_c)}; "
          f"aborted rows (ABORTED_SUBMISSION members, overlap allowed): "
          f"{human(union_aborted)}; dup-only (Done subs): {human(dup_only)}")
    only_copy = [r for r in rows if r.get("superseded") is False]
    if only_copy:
        print(f"   ABORTED entries that are the ONLY md5-copy in the bucket "
              f"(review!): {len(only_copy):,} objs, "
              f"{human(sum(r['size'] for r in only_copy))}")
    prot_bytes = sum(r["size"] for r in prows)
    if args.aborted_last_copy_deletable:
        print(f"   OWNER POLICY aborted_last_copy=deletable: {n_promoted:,} last-copy "
              f"rows under aborted submissions PROMOTED to the delete list "
              f"(reason ABORTED_LAST_COPY); {n_deliverable_kept:,} held back by G9 "
              f"(sample named in a reference list)")
    print(f"   PROTECTED review list (NOT deletable by default): "
          f"{len(prows):,} objs, {human(prot_bytes)}"
          + (f"  (incl. {n_nomd5_protected} with no md5 — uniqueness unverifiable)"
             if n_nomd5_protected else ""))
    pb = defaultdict(lambda: [0, 0])
    for r in prows:
        pb[r["submission_id"] or "(outside submissions)"][0] += 1
        pb[r["submission_id"] or "(outside submissions)"][1] += r["size"]
    for k, (n, sz) in sorted(pb.items(), key=lambda x: -x[1][1])[:8]:
        cfg = next((r.get("aborted") or {}).get("config", "?")
                   for r in prows if (r["submission_id"] or "(outside submissions)") == k)
        print(f"     {human(sz):>12}  {n:>5,}  {k}  [{cfg}]")
    print(f"   guarded: {n_inflight_protected:,} objects under in-flight/unknown "
          f"subs never listed; {n_skipped_referenced} referenced objects skipped; "
          f"{no_md5} objects without md5 (never EXACT_DUPLICATE); "
          f"{n_zero_byte_kept:,} zero-byte objects kept off both lists (G6); "
          f"{n_sidecar_kept:,} sidecars kept with surviving data (G7); "
          f"{n_reflist_kept:,} objects named in a reference list kept (G8); "
          f"{n_provenance_kept:,} provenance objects kept off both lists "
          f"({human(prov_bytes)}"
          f"{'' if not args.include_provenance else ' --include-provenance: LISTED'})")
    top_n = max(0, args.top)
    print(f"\n-- top {min(top_n, len(rows))} candidates --")
    for r in rows[:top_n]:
        print(f"   {human(r['size']):>12}  {'|'.join(r['reasons']):<24} "
              f"[{r['submission_status']}] {r['name'][:100]}")
    print(f"\noutputs: {tsv}\n          {js}\n          {ptsv} "
          f"(+ {pjs})")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="terra-scrub candidates",
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(ap)
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
