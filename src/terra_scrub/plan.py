"""plan -- turn a candidate TSV into an auditable delete PLAN.

This is the gate that must stand between a candidate list and any `rm`: the
manifest is a point-in-time claim about a non-versioned bucket, so nothing on it
may be trusted at delete time. This command re-checks every line against LIVE GCS
(and, if given a fresh Terra context, against LIVE pointers) and emits a plan a
human can read -- it never deletes. See docs/SAFETY.md §6 (plan gates and verdicts).

IT HAS NO DELETE VERB. That is not a mode, it is the shape of the file: the only
outbound calls are `session.get(...)`, there is no subprocess call, and there is
no `--execute` flag to discover. What it writes is (a) a JSON summary, (b) a
`rm -I`-compatible list of URIs -- the exact stdin format the wrapper's deleter
eats, one canonical `gs://<bucket>/<name>` per line -- and (c) a wrapper script
that refuses to run until a human writes the CONFIRM token matching the plan id
(`terra-scrub approve <plan_id>`), and that re-checks the URI list's sha256 and line
count against the plan before deleting. (b) and (c) are written only for an
executable plan. Deletion itself stays a deliberate,
owner-approved act.

Two things it will NOT do (docs/SAFETY.md §6 (plan gates and verdicts)):
  * certify a row it cannot fingerprint. `no digest, no plan`: a row whose md5 cell
    is '-' is skipped, never planned, and a manifest with no md5 column at all (an
    older generator) is refused outright with no opt-out -- because a LENGTH match is
    not evidence that the bytes are the bytes.
  * hand a review queue a way to delete. A `LAST-COPY PROTECTED` manifest produces a
    plan for reading, and NO wrapper script and NO URI list, ever. The same holds for
    ANY plan that is not executable (pointer check off, --limit, a flag-accepted stale
    manifest): it writes neither artifact, and it never overwrites a URI list or
    wrapper an earlier executable plan of the same manifest left on disk.

Why each check exists:
  * live 404            -> the object is already gone; deleting it again is a no-op
  * live size differs   -> the bytes behind that NAME are not the bytes that were
                           audited; name-keyed manifests are right (generation must
                           never be the delete key) but they still need a content
                           tie-break
  * live md5 differs    -> same, caught by content rather than by length
  * referenced now      -> a pointer created AFTER the capture is the one bug class
                           that a stale list cannot see; only a fresh context fixes it
  * submission in flight-> the same argument for status: resubmission/retry happens
  * submission unknown  -> an id no fresh context lists is absence of evidence,
                           never a pass
  * foreign bucket      -> a hand-merged or sed-mangled TSV must not be executable
  * outside --prefix    -> the audited scope was `submissions/`; anything else in a
                           hand-edited file is a red flag, not a target
  * stale manifest      -> older than --max-manifest-age-hours: refuse outright
  * no md5 column       -> size-only re-validation: refuse outright (see above)
  * review list         -> protected/last-copy list: plan only, no wrapper, no URIs
  * stale context       -> a context older than the manifest cannot see pointers
                           written in between, so it would certify instead of check

Read-only by construction: `tests/test_readonly_lint.py` parses this file's AST and
fails if a mutating verb, a `subprocess`/`shutil` import or an `--execute`-style
flag ever appears. `live_stat` is the ONE GCS call; the behaviour tests replace it
with a fixture lookup.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import sys
import urllib.parse
from datetime import UTC, datetime

from terra_scrub import http as _http
from terra_scrub.util import human, parse_ts

DONE = {"Done", "DONE", "done"}
DEAD = {"Aborted", "ABORTED", "aborted", "Failed", "FAILED", "failed"}
TERMINAL = DONE | DEAD


def _authed_session():
    """Indirection so tests can patch either `plan._authed_session` or
    `terra_scrub.http._authed_session`. Never called at import time."""
    return _http._authed_session()


def read_manifest(path):
    """Parse a `terra-scrub candidates` TSV (delete list or protected list).

    Returns (meta, rows). meta carries bucket/snapshot/generated/kind. Column
    order is taken from the '# name<TAB>...' header line, never assumed.
    """
    meta = {"path": os.path.abspath(path), "kind": "delete"}
    header = None
    rows = []
    with open(path) as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("#"):
                if line.lstrip("# ").startswith("name\t"):
                    header = line.lstrip("# ").split("\t")
                if "LAST-COPY PROTECTED" in line:
                    meta["kind"] = "protected"
                for key in ("bucket", "snapshot", "generated"):
                    tag = f"{key}="
                    i = line.find(tag)
                    if i >= 0 and key not in meta:
                        meta[key] = line[i + len(tag):].split()[0]
                continue
            if not line:
                continue
            parts = line.split("\t")
            if header is None:
                sys.exit(f"{path}: no column header line (starting with '# name') -- "
                         f"refusing to guess which column is the object name")
            if len(parts) != len(header):
                sys.exit(f"{path}: row has {len(parts)} fields but the header declares "
                         f"{len(header)} -- a name containing a newline/tab? refusing")
            rows.append(dict(zip(header, parts)))
    if header is None or header[0] != "name":
        sys.exit(f"{path}: header must start with 'name' (got {header!r})")
    missing = [k for k in ("bucket", "generated") if not meta.get(k)]
    if missing:
        sys.exit(f"{path}: header lacks {missing}, so the plan cannot be aged or "
                 f"attributed to a bucket -- refusing")
    meta["has_md5"] = "md5" in header
    return meta, rows


def obj_name(bucket, raw):
    """Object name (never bucket-qualified) from a manifest cell.

    The generator writes bucket-relative names, but a hand-merged or hand-edited
    manifest may carry full `gs://<bucket>/…` URIs. Returning the stripped name is
    what lets the plan emit ONE canonical URI per object -- building the URI from the
    raw cell would print `gs://<bucket>/gs://<bucket>/…`, i.e. a malformed line on
    the list that `rm -I` eats. Whatever deletes must key on this file, so the file
    has to be unambiguous."""
    if raw.startswith("gs://"):
        head = raw[len("gs://"):].split("/", 1)
        if head[0] == bucket:
            return head[1] if len(head) > 1 else ""
    return raw


def manifest_md5(row):
    """The manifest's md5, or '' when it carries none.

    The generator writes '-' for a missing md5 (and the protected list is full of
    them: NO_MD5_PROTECTED rows). '-' must read as "no md5 on file", never as a
    digest -- otherwise every such row is reported as SKIP_MD5_CHANGED, which
    accuses the bucket of mutating when the manifest simply had nothing to say.
    """
    v = (row.get("md5") or row.get("md5Hash") or "").strip()
    return "" if v in ("", "-", "None", "null") else v


def manifest_sub_id(row, name):
    """Submission id for a row: the column, else the name's second path element.

    '-' is the generator's empty marker and is *truthy in Python*, so `row['sub_id']
    or <name>` never falls through -- the in-flight guard would silently no-op on any
    row that lost its column. Treat '-' (and friends) as empty."""
    v = (row.get("sub_id") or "").strip()
    if v not in ("", "-", "None", "null"):
        return v
    if name.startswith("submissions/") and name.count("/") > 1:
        return name.split("/")[1]
    return None


def live_stat(session, bucket, name):
    """GET one object's metadata. Returns ('ok', meta) | ('absent', None) |
    ('error', message). GET only -- there is no verb here that can change GCS."""
    url = f"{_http.GCS_API}/b/{bucket}/o/{urllib.parse.quote(name, safe='')}"
    r = session.get(url, params={"projection": "noAcl",
                                 "fields": "name,size,md5Hash,updated,generation"},
                    timeout=60)
    if r.status_code == 404:
        return "absent", None
    if r.status_code != 200:
        return "error", f"HTTP {r.status_code}"
    return "ok", r.json()


def add_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--manifest", required=True,
                    help="a *.cleanup.tsv or *.cleanup.protected.tsv from "
                         "`terra-scrub candidates`")
    ap.add_argument("--terra", action="append", default=[],
                    help="a FRESH terra context (same bucket) to re-check live "
                         "pointers and submission status; repeatable. Without it the "
                         "plan validates bytes only and says so loudly.")
    ap.add_argument("--prefix", default="submissions/",
                    help="objects outside this prefix are refused as suspicious "
                         "(default 'submissions/'; pass '' to allow any name)")
    ap.add_argument("--max-manifest-age-hours", type=float, default=24.0,
                    help="refuse a manifest generated more than N hours ago "
                         "(default 24; 0 = no check)")
    ap.add_argument("--allow-stale-manifest", action="store_true",
                    help="downgrade the manifest-age check to a warning (review only "
                         "-- a plan you intend to execute must be regenerated)")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--limit", type=int, default=0,
                    help="stat only the first N lines (smoke test; the plan is then "
                         "explicitly partial)")
    ap.add_argument("--sample", type=int, default=5, help="example lines to print")
    ap.add_argument("--out", default=None, help="plan JSON (default: <manifest>.plan.json)")
    ap.add_argument("--uris-out", default=None,
                    help="newline-delimited URIs, `rm -I` compatible "
                         "(default: <manifest>.plan.uris.txt)")
    ap.add_argument("--commands-out", default=None,
                    help="wrapper script with a CONFIRM gate (default: <manifest>.plan.sh)")


def run(args: argparse.Namespace) -> int:
    meta, rows = read_manifest(args.manifest)
    bucket = meta["bucket"]
    with open(args.manifest, "rb") as fh:
        plan_id = hashlib.sha256(fh.read()).hexdigest()[:12]
    out_json = args.out or (args.manifest + ".plan.json")
    out_uris = args.uris_out or (args.manifest + ".plan.uris.txt")
    out_sh = args.commands_out or (args.manifest + ".plan.sh")

    print(f"manifest: {args.manifest}")
    print(f"   kind={meta['kind']} bucket={bucket} snapshot={meta.get('snapshot', '?')} "
          f"generated={meta['generated']} rows={len(rows):,} plan_id={plan_id}")

    # ---- gate 1: is the manifest still current enough to reason about? -----
    gen = parse_ts(meta["generated"])
    now = datetime.now(UTC)
    if gen is None:
        sys.exit(f"cannot parse generated={meta['generated']!r} from the manifest header")
    if gen.tzinfo is None:
        # Manifests written by an older generator carry naive local timestamps.
        # Assuming UTC is the conservative reading: it ages the list rather than
        # flattering it.
        print("WARNING: manifest generated= carries no timezone (an older generator); "
              "assuming UTC, which ages this list rather than flattering it -- regenerate "
              "for an exact age", file=sys.stderr)
        gen = gen.replace(tzinfo=UTC)
    age_h = (now - gen).total_seconds() / 3600.0
    if age_h > args.max_manifest_age_hours:
        msg = (f"manifest is {age_h:.1f} h old (> {args.max_manifest_age_hours:g} h). The "
               f"bucket is non-versioned: re-capture and regenerate, do not plan from this.")
        if not args.allow_stale_manifest:
            sys.exit(msg + " (--allow-stale-manifest for a review-only pass)")
        print(f"WARNING: {msg} -- THIS PLAN IS NOT EXECUTABLE", file=sys.stderr)
    else:
        print(f"   manifest age at run time: {age_h:.1f} h "
              f"(limit {args.max_manifest_age_hours:g} h)")

    # ---- gate 1b: can rows be tied back to CONTENT at all? ----------------
    # A list written before the generator emitted an md5 column can only be
    # re-validated on length, and same-length-different-bytes is not a hypothetical
    # -- it is why the md5 column exists. There is deliberately no opt-out: the row
    # rule below refuses to plan any row without a digest, so an md5-less manifest
    # could only ever produce an empty plan. Say that up front
    # (docs/SAFETY.md §6 (plan gates and verdicts)).
    if not meta["has_md5"]:
        sys.exit(f"{args.manifest}: header has no md5 column (an older generator). "
                 f"Rows could only be re-validated on SIZE ALONE, which cannot tell "
                 f"'the audited bytes' from 'bytes of the same length' -- and 'no digest, "
                 f"no plan' would then skip every row. Re-capture and re-generate with the "
                 f"current `terra-scrub candidates`.")

    # ---- gate 2: fresh contexts, same bucket, newer than the manifest ------
    referenced = set()
    subs = {}
    ctx_ages = []
    for tpath in args.terra:
        with open(tpath) as fh:
            tc = json.load(fh)
        if tc.get("bucket") != bucket:
            sys.exit(f"{tpath} is a context for '{tc.get('bucket')}' but the manifest is "
                     f"for '{bucket}' -- refusing to mix buckets")
        cap = parse_ts(tc.get("captured_utc"))
        if cap is None:
            sys.exit(f"{tpath}: no parseable captured_utc -- cannot certify freshness")
        if cap.tzinfo is None:
            cap = cap.replace(tzinfo=UTC)
        if cap < gen:
            sys.exit(f"{tpath} was captured {(gen - cap).total_seconds():.0f}s BEFORE the "
                     f"manifest, so it cannot see pointers created since -- it would "
                     f"certify rather than check. Re-capture the context NOW (after the "
                     f"manifest's generated= stamp, i.e. at planning time) and re-run.")
        ctx_ages.append((tpath, cap))
        referenced.update(tc.get("referenced_gs_uris", []))
        for s in tc.get("submissions", []):
            if s.get("submissionId"):
                subs.setdefault(s["submissionId"], s)
    prefix = args.prefix
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    # The pointer check is ON when a fresh context was GIVEN -- even one with zero
    # referenced URIs, which is a legitimate answer ("nothing points at anything") for
    # a workspace whose tables carry no gs:// attributes. It is OFF only when no
    # context was passed at all. (Testing `referenced` here conflated the two.)
    pointer_check = bool(ctx_ages)
    if pointer_check:
        print(f"   live pointer check: ON ({len(referenced):,} referenced URIs from "
              f"{len(ctx_ages)} context(s), newest "
              f"{max(c for _, c in ctx_ages).isoformat()})")
    else:
        print("   live pointer check: OFF -- no --terra context given, so this plan knows "
              "the bytes are unchanged but NOT that nothing points at them. Review only.")

    if args.limit:
        rows = rows[:args.limit]
        print(f"   NOTE: --limit {args.limit} -> plan is PARTIAL, {len(rows):,} rows checked")

    # ---- the live pass -----------------------------------------------------
    session = _authed_session()
    tally = {}
    bytes_by = {}
    uri_list = []
    examples = []
    errors = []

    def verdict(row):
        """One row -> (status, size_bytes, note, uri). GET only; uri is set for
        PLAN_DELETE and empty for every other outcome."""
        name = obj_name(bucket, row["name"])
        try:
            size = int(row["size_bytes"])
        except ValueError:
            return "SKIP_BAD_SIZE_FIELD", 0, f"size_bytes={row['size_bytes']!r}", ""
        if name.startswith("gs://"):
            # gs://other-bucket/... was caught above by the head test; a bare
            # 'gs://' or a foreign URI that survived that is not ours to delete.
            return "SKIP_FOREIGN_BUCKET", size, "unparseable gs:// cell", ""
        if not name:
            return "SKIP_BAD_NAME", size, "empty object name (after gs://<bucket>/)", ""
        if prefix and not name.startswith(prefix):
            return "SKIP_OUTSIDE_PREFIX", size, f"outside --prefix {prefix!r}", ""
        uri = f"gs://{bucket}/{name}"
        if uri in referenced:
            return "REFUSE_REFERENCED_NOW", size, "a live attribute points at it", ""
        sid = manifest_sub_id(row, name)
        st = (subs.get(sid) or {}).get("status") if sid else None
        if sid and not st:
            # An id the fresh contexts do not list is NOT a terminal submission: it
            # may be newer than every context, or live in a workspace we did not
            # pass. Absence of a status is absence of evidence, never a pass.
            return "REFUSE_SUBMISSION_UNKNOWN", size, f"{sid} not in any --terra context", ""
        if st and st not in TERMINAL:
            return "REFUSE_SUBMISSION_NOT_TERMINAL", size, f"{sid} now {st}", ""
        state, md = live_stat(session, bucket, name)
        if state == "absent":
            return "SKIP_ALREADY_ABSENT", size, "404 at delete time", ""
        if state == "error":
            errors.append((name, md))
            return "ERROR_STAT", size, md, ""
        live_size = int(md.get("size", -1))
        if live_size != size:
            return "SKIP_SIZE_CHANGED", size, f"manifest {size:,} B, live {live_size:,} B", ""
        m_md5 = manifest_md5(row)
        l_md5 = md.get("md5Hash") or ""
        if not m_md5:
            # 'no digest, no plan': a row the manifest could not fingerprint is not
            # certifiable by content, whatever its length says.
            return ("SKIP_NO_DIGEST", size,
                    "manifest row carries no md5 ('-'): size alone is not certification", "")
        if m_md5 and not l_md5:
            return ("SKIP_MD5_UNVERIFIABLE", size,
                    "live object carries no md5 (composite/KMS?)", "")
        if m_md5 and l_md5 and m_md5 != l_md5:
            return ("SKIP_MD5_CHANGED", size,
                    "bytes behind this name are not what was audited", "")
        return "PLAN_DELETE", size, "", uri

    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(verdict, r): r for r in rows}
        done_n = 0
        for fu in cf.as_completed(futs):
            row = futs[fu]
            try:
                status, size, note, uri = fu.result()
            except Exception as e:                     # one bad row must not lose the run
                status, size, note, uri = "ERROR_ROW", int(row.get("size_bytes", 0) or 0), \
                    f"{type(e).__name__}: {e}", ""
            tally[status] = tally.get(status, 0) + 1
            bytes_by[status] = bytes_by.get(status, 0) + size
            if status == "PLAN_DELETE":
                uri_list.append(uri)
            if status.startswith(("ERROR", "REFUSE", "SKIP_SIZE", "SKIP_MD5_CHANGED",
                                  "SKIP_NO_DIGEST")) \
                    and len(examples) < 200:
                examples.append({"name": row["name"], "status": status, "note": note})
            done_n += 1
            if done_n % 2000 == 0:
                print(f"   ...{done_n:,}/{len(rows):,} rows live-checked", flush=True)

    if errors:
        print(f"NOTE: {len(errors)} transport errors recorded -- the plan is not "
              f"complete for those rows; see ERROR_STAT in {out_json}", file=sys.stderr)

    n_del = tally.get("PLAN_DELETE", 0)
    b_del = bytes_by.get("PLAN_DELETE", 0)
    blocked = {k: v for k, v in tally.items() if k != "PLAN_DELETE"}

    print(f"\n== live-validated plan: gs://{bucket} ==")
    print(f"   rows in manifest      : {len(rows):,}")
    print(f"   planned deletions     : {n_del:,} objects, {human(b_del)}")
    for k in sorted(blocked, key=lambda x: -bytes_by[x]):
        print(f"   {k:32s}: {blocked[k]:8,} objects, {human(bytes_by[k]):>10}")
    if examples:
        print("   first blocked/changed rows (max 200 recorded, showing "
              f"{min(len(examples), args.sample)}):")
        for e in examples[:args.sample]:
            print(f"      {e['status']}: {e['name'][:110]}  {e['note']}")

    # ---- artifacts ---------------------------------------------------------
    # A protected/review list is a HUMAN QUEUE, not a delete set: it is exactly the
    # last copy of its content. Handing such a list a CONFIRM-gated rm wrapper would
    # be the one-click path to the thing this whole tool exists to prevent, so the
    # wrapper is simply not written for it (docs/SAFETY.md §6 (plan gates and verdicts)).
    review_only = meta["kind"] != "delete"
    if review_only:
        print(f"\n   REVIEW LIST (kind={meta['kind']}): no delete wrapper and no URI list are "
              f"written for a last-copy review list, by design.", file=sys.stderr)
    uri_blob = "\n".join(uri_list) + ("\n" if uri_list else "")
    uri_sha = hashlib.sha256(uri_blob.encode()).hexdigest()
    reasons = []
    if not pointer_check:
        reasons.append("pointer check OFF: no --terra context, so nothing re-checked for new references")
    if review_only:
        reasons.append(f"review list (kind={meta['kind']}): never deletable by this tool")
    if args.allow_stale_manifest:
        reasons.append("manifest older than --max-manifest-age-hours (accepted by flag)")
    if args.limit:
        reasons.append(f"--limit {args.limit}: PARTIAL plan")
    executable = not reasons
    sh = f"""#!/bin/bash
# Delete plan for gs://{bucket} -- generated {now.isoformat()}
# from {os.path.basename(args.manifest)} (plan_id {plan_id}, sha256 of the URI list
# {uri_sha}, {n_del:,} objects / {b_del:,} bytes).
#
# Reviewed pointers re-checked live at plan time: {'ON' if pointer_check else 'OFF -- DO NOT RUN THIS'}
# Recovery after this runs is a soft-delete restore, and only inside the bucket's
# soft-delete window. That window is set by the bucket's softDeletePolicy (the GCS
# default is 7 days; it can be 0, i.e. no recovery at all). Check the policy on
# gs://{bucket} before running this (see docs/SAFETY.md §8).
#
# This script deliberately refuses to run until a human edits CONFIRM. Writing the
# token is the approval; do not script it.
#
# Before deleting it also re-checks that the URI list is byte-for-byte the list this
# plan live-validated (sha256 and line count recorded below), so a list rewritten or
# edited after planning is refused even when CONFIRM is set (docs/SAFETY.md §7).
set -euo pipefail
CONFIRM=""
if [ "$CONFIRM" != "{plan_id}" ]; then
    echo "refusing: edit this file and set CONFIRM={plan_id} to approve deleting"
    echo "{n_del} objects / {b_del:,} bytes from gs://{bucket}"
    exit 1
fi
test -f "{os.path.abspath(out_uris)}" || {{ echo "missing {os.path.abspath(out_uris)}"; exit 1; }}
URIS="{os.path.abspath(out_uris)}"
WANT_SHA256="{uri_sha}"
WANT_LINES={n_del}
if command -v sha256sum >/dev/null 2>&1; then
    GOT_SHA256="$(sha256sum < "$URIS")"
elif command -v shasum >/dev/null 2>&1; then
    GOT_SHA256="$(shasum -a 256 < "$URIS")"
else
    echo "refusing: neither sha256sum nor shasum is on PATH -- cannot verify the URI list"
    exit 1
fi
GOT_SHA256="${{GOT_SHA256%% *}}"
if [ "$GOT_SHA256" != "$WANT_SHA256" ]; then
    echo "refusing: URI list sha256 changed since planning (plan_id {plan_id})"
    echo "  planned $WANT_SHA256"
    echo "  on disk $GOT_SHA256"
    echo "re-plan; never hand-edit the URI list"
    exit 1
fi
GOT_LINES=0
while IFS= read -r line || [ -n "$line" ]; do
    if [ -n "$line" ]; then GOT_LINES=$((GOT_LINES + 1)); fi
done < "$URIS"
if [ "$GOT_LINES" -ne "$WANT_LINES" ]; then
    echo "refusing: URI list has $GOT_LINES lines but the plan has $WANT_LINES objects"
    exit 1
fi
# Stops at the first failed object (no --continue-on-error): verify diagnoses a partial run.
exec gcloud storage rm -I < "{os.path.abspath(out_uris)}"
"""

    # The URI list and the wrapper are written ONLY for a plan with no disqualifying
    # reason. Writing the wrapper first and computing the reasons afterwards would give
    # a plan with the pointer check off, a flag-accepted stale manifest, or a --limit
    # PARTIAL row set a runnable script whose only warning was a comment inside it --
    # and `executable: false` in plan.json would be merely advisory.
    #
    # The URI list is withheld on the same condition, and an existing one is never
    # overwritten by a non-executable re-plan: plan_id is the manifest's hash, so a
    # re-plan of the same manifest (say with --limit, or without --terra) would
    # otherwise swap an unchecked/partial list in under a wrapper armed earlier. A
    # review list must not leave an `rm -I`-ready file on disk either: a
    # one-line-per-URI blob IS the delete command's stdin. The sha256 is computed
    # either way (it is the plan's fingerprint).
    left_untouched = []
    if executable:
        with open(out_uris, "w") as f:
            f.write(uri_blob)
        with open(out_sh, "w") as f:
            f.write(sh)
        os.chmod(out_sh, 0o644)  # readable, deliberately not executable-by-default
    else:
        left_untouched = [os.path.abspath(x) for x in (out_uris, out_sh) if os.path.exists(x)]
        for x in left_untouched:
            print(f"   NOTE: {x} exists from an earlier plan and was LEFT UNTOUCHED -- it "
                  f"does not belong to this (non-executable) plan", file=sys.stderr)

    with open(out_json, "w") as fh:
        json.dump({
            "generated_utc": now.isoformat(), "plan_id": plan_id, "bucket": bucket,
            "manifest": os.path.abspath(args.manifest), "manifest_kind": meta["kind"],
            "manifest_generated": meta["generated"], "manifest_age_hours": round(age_h, 2),
            "manifest_sha256": f"{plan_id}...", "snapshot_utc": meta.get("snapshot"),
            "prefix": prefix, "limit": args.limit, "rows": len(rows),
            "pointer_check": "on" if pointer_check else "off",
            "contexts": [{"path": p, "captured_utc": c.isoformat()} for p, c in ctx_ages],
            "plan_objects": n_del, "plan_bytes": b_del,
            "objects_by_status": tally, "bytes_by_status": bytes_by,
            "uri_list_sha256": uri_sha,
            "uris_out": (os.path.abspath(out_uris) if executable else None),
            "commands_out": (os.path.abspath(out_sh) if executable else None),
            "uris_written": executable, "commands_written": executable,
            "withheld_reason": (None if executable else
                                "plan is not executable: no URI list or wrapper written"),
            "left_untouched": left_untouched,
            "examples": examples[:20],
            "executable": executable, "not_executable_reason": reasons,
        }, fh, indent=1)
    print(f"\n   plan JSON     : {out_json}")
    if review_only:
        print(f"   URI list      : NOT WRITTEN -- {meta['kind']} list is a review queue "
              f"(sha256 of the withheld blob {uri_sha[:16]}…)")
    elif not executable:
        print(f"   URI list      : NOT WRITTEN -- plan is not executable "
              f"(sha256 of the withheld blob {uri_sha[:16]}…)")
    else:
        print(f"   URI list      : {out_uris}   (sha256 {uri_sha[:16]}…)")
    if reasons:
        print("   wrapper script: NOT WRITTEN -- plan is not executable")
    else:
        print(f"   wrapper script: {out_sh}   (refuses until CONFIRM={plan_id}; "
              f"arm with `terra-scrub approve {plan_id}`)")
    if reasons:
        print("   NOT EXECUTABLE: " + "; ".join(reasons), file=sys.stderr)
    print("   NOTE: this tool has no delete verb; the wrapper is what a human runs.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="terra-scrub plan",
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(ap)
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
