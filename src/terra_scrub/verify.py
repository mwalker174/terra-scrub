"""verify -- prove what a delete wrapper actually did.

READ-ONLY: GETs only (one object GET per planned row, per keeper and per Terra
reference, one soft-deleted-object GET per planned row, plus one full re-listing
of the bucket done in-process by `terra_scrub.snapshot`). Contains no delete verb
and no subprocess.

Run it right after a `*.plan.sh` wrapper returns. It answers the only questions
that matter afterwards, and the third is the load-bearing one
(docs/SAFETY.md §8):

  1. is every planned object gone?  "Planned" is the plan's URI list (what the
     wrapper fed to the deleter) when plan.json records one, else the manifest rows.
  2. does every keeper still exist, with the md5 the manifest recorded for the
     row it was keeping?  ("a copy survives" is only true if it still matches)
  3. did the bucket lose EXACTLY the planned set and nothing else?  This needs a
     full before/after re-listing -- a spot check cannot see collateral loss.
  4. is every Terra-referenced object that was live BEFORE still live?  A reference
     that was already dangling before the delete is documented in a sidecar
     (<manifest>.dangling_refs.txt), not failed, and never mutated.
  5. is the deletion still reversible -- i.e. does each deleted object have a
     soft-deleted copy whose md5 matches, and until when?

Usage:
    terra-scrub verify --plan <...>.cleanup.tsv.plan.json
        [--before-snapshot <...>.jsonl]   # default: guessed from the run layout
        [--terra <...>.terra.json]        # default: from plan.json's contexts
        [--after-snapshot <path>]         # default: <before minus ext>.after.jsonl
                                          # (refused if it would be the before file)

Exit status is 1 if any check fails, so it can gate a follow-up step.
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import json
import os
import sys
import urllib.parse

from terra_scrub import http as _http
from terra_scrub import snapshot as _snapshot

FAILS = []


def _authed_session():
    """Indirection so tests can patch `verify._authed_session`. Not called on import."""
    return _http._authed_session()


def check(cond, msg):
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")
    if not cond:
        FAILS.append(msg)


def tsv_rows(path):
    """The manifest, as dicts keyed by its own '# name<TAB>...' header."""
    hdr, out = None, []
    with open(path) as fh:
        for line in fh:
            if line.startswith("#"):
                if hdr is None and "\t" in line:
                    hdr = line.lstrip("#").strip("\n").strip().split("\t")
                continue
            cells = line.rstrip("\n").split("\t")
            if hdr and len(cells) == len(hdr):
                out.append(dict(zip(hdr, cells)))
    if hdr is None:
        sys.exit(f"{path}: no '# name<TAB>...' header line")
    return out


def guess_before(plan):
    """<run>/cleanup/<key>/<bucket>.cleanup.tsv -> <run>/inv/<key>.jsonl"""
    cdir = os.path.dirname(plan["manifest"])
    key = os.path.basename(cdir)
    cand = os.path.join(os.path.dirname(os.path.dirname(cdir)), "inv", f"{key}.jsonl")
    return cand if os.path.exists(cand) else None


def default_after(before):
    """<before minus its extension>.after<ext or .jsonl>. Derived with splitext so a
    before path with no `.jsonl` suffix still gets a DIFFERENT after path (a plain
    str.replace would be a no-op there and re-list over the before snapshot)."""
    root, ext = os.path.splitext(before)
    return f"{root}.after{ext or '.jsonl'}"


def refuse_same_snapshot(before, after):
    """The after-listing must never overwrite the before-listing: check 3 would then
    compare the bucket with itself and pass, and the only record of what was there
    before the delete would be gone."""
    same = os.path.realpath(after) == os.path.realpath(before)
    if not same and os.path.exists(after):
        try:
            same = os.path.samefile(after, before)
        except OSError:
            same = False
    if same:
        sys.exit(f"REFUSING: the after-snapshot {after!r} is the before-snapshot "
                 f"{before!r} -- re-listing there would overwrite the before-listing. "
                 f"Pass a different --after-snapshot.")


def planned_names(plan, rows):
    """(names, source). The URI list is what the wrapper actually handed the deleter,
    so it is the planned set whenever plan.json has one; manifest rows are only the
    fallback (a manifest row the plan SKIPPED was never deleted, and counting it as
    planned would fail check 1 on an object that was rightly kept)."""
    uris = plan.get("uris_out")
    if uris:
        if not os.path.exists(uris):
            check(False, f"plan.json names a URI list that is missing: {uris} -- falling "
                         f"back to the manifest rows")
        else:
            pre = f"gs://{plan['bucket']}/"
            with open(uris) as fh:
                lines = [ln.rstrip("\n") for ln in fh if ln.strip()]
            bad = [u for u in lines if not u.startswith(pre)]
            check(not bad, f"every URI list line is in gs://{plan['bucket']}/ "
                           f"({len(bad)} foreign{': ' + bad[0][:80] if bad else ''})")
            return {u[len(pre):] for u in lines if u.startswith(pre)}, "URI list"
    return {r["name"] for r in rows}, "manifest rows"


def _relist(bucket, after, session):
    """Re-list the bucket in-process (GET only). Returns (rc, message)."""
    try:
        _snapshot.snapshot_bucket(bucket, after, session=session)
    except SystemExit as e:
        return 1, str(e.code)
    except Exception as e:                       # the check reports it; do not crash
        return 1, f"{type(e).__name__}: {e}"
    return 0, ""


def add_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--plan", required=True)
    ap.add_argument("--workers", type=int, default=24,
                    help="parallel GETs (default 24). A serial verifier takes hours on a "
                         "large list, which is how a verification step turns into a step "
                         "nobody runs")
    ap.add_argument("--before-snapshot")
    ap.add_argument("--after-snapshot")
    ap.add_argument("--terra", action="append", default=[])
    ap.add_argument("--skip-relist", action="store_true",
                    help="skip check 3 (the before/after re-listing). Only for a "
                         "bucket too large to re-list right now -- it is the only "
                         "check that can see UNPLANNED loss, so say so if you skip it")


def run(args: argparse.Namespace) -> int:
    FAILS.clear()
    with open(args.plan) as fh:
        plan = json.load(fh)
    bucket = plan["bucket"]
    rows = tsv_rows(plan["manifest"])
    # manifest cells may be bucket-qualified (a hand-merged list); key on object names
    pre = f"gs://{bucket}/"
    for r in rows:
        r["name"] = r["name"].removeprefix(pre)
    planned, planned_src = planned_names(plan, rows)
    md5_of = {r["name"]: r.get("md5", "") for r in rows}
    keep_of = {r["name"]: r.get("kept_copy", "") for r in rows}
    before = None
    if not args.skip_relist:
        before = args.before_snapshot or guess_before(plan)
        if before and os.path.exists(before):
            after = args.after_snapshot or default_after(before)
            refuse_same_snapshot(before, after)       # before any GCS call
    session = _authed_session()

    def stat(name, soft=False):
        base = f"{_http.GCS_API}/b/{bucket}/o"
        if soft:
            q = (f"?softDeleted=true&prefix={urllib.parse.quote(name, safe='')}"
                 "&fields=items(name,size,md5Hash,generation,softDeleteTime,hardDeleteTime)")
            r = session.get(base + q, timeout=60)
            if r.status_code != 200:
                return r.status_code, None
            return 200, [i for i in r.json().get("items", []) if i["name"] == name]
        q = f"/{urllib.parse.quote(name, safe='')}?fields=name,size,md5Hash"
        r = session.get(base + q, timeout=60)
        return r.status_code, (r.json() if r.status_code == 200 else r.text[:120])

    print(f"== post-delete verification: gs://{bucket} (plan {plan['plan_id']}) ==")
    print(f"   manifest {os.path.basename(plan['manifest'])} | "
          f"{plan['plan_objects']:,} planned / {plan['plan_bytes']:,} B")
    check(len(planned) == plan["plan_objects"],
          f"{planned_src} ({len(planned):,}) == plan_objects ({plan['plan_objects']:,})")

    # 1. planned rows gone
    with cf.ThreadPoolExecutor(args.workers) as ex:
        codes = list(ex.map(lambda n: (n, stat(n)[0]), planned))
    still = [n for n, c in codes if c == 200]
    check(not still, f"all {len(planned):,} planned objects are absent "
                     f"({len(still)} still live{': ' + still[0][-60:] if still else ''})")

    # 2. keepers survive with the right content
    keepers = collections.defaultdict(list)
    for n in planned:
        if keep_of.get(n) and keep_of[n] != "-":
            keepers[keep_of[n]].append(n)
    promoted = {r["name"] for r in rows
                if r.get("reasons", "").startswith("ABORTED_LAST_COPY")}

    def check_keeper(item):
        k, kept_for = item
        # A keeper may legitimately be GONE when it was itself promoted by the
        # aborted-last-copy policy: the whole md5 group was under aborted runs and
        # died together, which the generator's G2b already asserted. Absent AND not
        # on this plan is the real failure.
        if k in promoted:
            return None
        code, md = stat(k)
        if code != 200:
            return (k, f"HTTP {code}" + (" -- on this plan but NOT promoted"
                                         if k in planned else " -- not on this plan"))
        if md.get("md5Hash") != md5_of.get(kept_for[0]):
            return (k, f"md5 {md.get('md5Hash')} != manifest {md5_of[kept_for[0]]}")
        if k in planned:
            return (k, "keeper is itself on the delete list")
        return None
    with cf.ThreadPoolExecutor(args.workers) as ex:
        bad = [r for r in ex.map(check_keeper, keepers.items()) if r]
    n_prom_keep = sum(1 for k in keepers if k in promoted)
    check(not bad, f"all {len(keepers) - n_prom_keep:,} surviving keepers live with matching "
                   f"md5; {n_prom_keep} keepers were themselves promoted and deleted with "
                   f"their all-aborted group ({bad[:2]})")

    # 3. exactly the planned set, nothing else
    before_names = None          # set from the before-listing when check 3 runs (used by check 4)
    if args.skip_relist:
        print("  [SKIP] before/after re-listing skipped by flag -- UNPLANNED loss "
              "would not be detected")
    else:
        if not before or not os.path.exists(before):
            check(False, f"before-snapshot not found (pass --before-snapshot): {before}")
        else:
            rc, why = _relist(bucket, after, session)
            check(rc == 0, f"re-listed the bucket (rc={rc}){' ' + why if why else ''}")
            if rc == 0:
                b = {o["name"]: o for o in _snapshot.read_snapshot(before)[1]}
                a = {o["name"]: o for o in _snapshot.read_snapshot(after)[1]}
                gone, new = set(b) - set(a), set(a) - set(b)
                before_names = set(b)
                check(gone == planned,
                      f"exactly the planned set disappeared (gone={len(gone):,}, "
                      f"planned={len(planned):,}, UNPLANNED={len(gone - planned)}, "
                      f"planned-but-present={len(planned - gone)})")
                lost = sum(b[n]["size"] for n in gone)
                check(lost == plan["plan_bytes"],
                      f"bytes lost = {lost:,} (expected {plan['plan_bytes']:,})")
                print(f"     objects {len(b):,} -> {len(a):,} | new since capture: {len(new)}")

    # 4. nothing Terra points at moved
    #
    # A reference that is missing NOW is only this delete's fault if the object was
    # there BEFORE. Terra tables carry stale pointers that predate any cleanup;
    # judged against the after-listing alone they fail clean verifications and read
    # as data loss. So: missing AND present in the before-listing = LOST (fail);
    # missing AND absent before = PRE-EXISTING dangling pointer -- documented, written
    # to a sidecar next to the plan, never mutated (the table is Terra's; this tool is
    # GET-only). Without a before-listing (--skip-relist, or none found) every miss
    # still fails, because the distinction cannot be made and a silent pass would be
    # the wrong default (docs/SAFETY.md §8).
    ctxs = args.terra or [c["path"] for c in plan.get("contexts", [])]
    refs = set()
    for c in ctxs:
        if os.path.exists(c):
            with open(c) as fh:
                refs |= {u for u in json.load(fh).get("referenced_gs_uris", [])
                         if u.startswith(f"gs://{bucket}/")}
    if not refs:
        check(False, f"no Terra context readable ({ctxs}) -- reference check not performed")
    else:
        with cf.ThreadPoolExecutor(args.workers) as ex:
            missing = sorted(u for u, c in ex.map(lambda u: (u, stat(u.split("/", 3)[3])[0]), refs)
                             if c != 200)
        if before_names is None:
            check(not missing, f"all {len(refs):,} Terra-referenced objects still live "
                               f"({len(missing)} missing; no before-listing, so none can be "
                               f"classed pre-existing)")
        else:
            lost = [u for u in missing if u.split("/", 3)[3] in before_names]
            pre = [u for u in missing if u.split("/", 3)[3] not in before_names]
            check(not lost, f"all {len(refs) - len(pre):,} Terra-referenced objects that were "
                            f"present before are still live ({len(lost)} LOST by this delete"
                            f"{': ' + lost[0][-70:] if lost else ''})")
            if pre:
                side = plan["manifest"] + ".dangling_refs.txt"
                with open(side, "w") as fh:
                    fh.write(f"# {len(pre)} Terra-referenced URIs in gs://{bucket} absent both "
                             f"before and after plan {plan['plan_id']} -- stale pointers that "
                             f"predate this delete. Documented only; nothing here was mutated.\n")
                    fh.write("\n".join(pre) + "\n")
                print(f"     pre-existing dangling references: {len(pre):,} (absent in the "
                      f"before-listing too; NOT this delete's doing) -> {os.path.basename(side)}")

    # 5. reversibility (a GET of the soft-deleted listing; nothing is restored here)
    def check_soft(n):
        code, items = stat(n, soft=True)
        if code != 200 or not items:
            return (n, f"no soft-deleted copy (HTTP {code})", None)
        it = items[0]
        if md5_of.get(n) and it.get("md5Hash") != md5_of[n]:
            return (n, "soft-deleted md5 != manifest", None)
        return None, None, str(it.get("hardDeleteTime"))[:16]
    soft_ok, soft_bad, windows = 0, [], set()
    with cf.ThreadPoolExecutor(args.workers) as ex:
        for name, why, win in ex.map(check_soft, planned):
            if why:
                soft_bad.append((name, why))
            else:
                soft_ok += 1
                windows.add(win)
    check(not soft_bad, f"soft-deleted copies exist with matching md5 for "
                        f"{soft_ok}/{len(planned)} ({soft_bad[:2]})")
    if windows:
        print(f"     reversible until: {sorted(windows)}  "
              f"(restore '<uri>#<generation>' with the Cloud Storage CLI's objects "
              f"restore; restores land in STANDARD with a fresh generation and a reset "
              f"creation time -- see docs/SAFETY.md §8)")

    print("\nRESULT: " + ("ALL CHECKS PASS" if not FAILS else f"{len(FAILS)} FAILURE(S)"))
    for f in FAILS:
        print("   FAIL:", f)
    return 1 if FAILS else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="terra-scrub verify",
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(ap)
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
