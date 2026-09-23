"""Offline analysis of a snapshot (and, for ``stale``, a terra context).

Commands (all offline; they read local files only):

    report   space analysis: prefix tree (du -d N), top-N largest objects,
             extension breakdown, date range.
    stale    staleness: object age vs snapshot time, per-prefix last-write
             time, objects not referenced by any Terra attribute, and
             per-submission size + date + status. REFUSES a terra context whose
             bucket does not match the snapshot's, or that has no usable bucket
             key (missing/null) — see docs/SAFETY.md.
    dupes    duplicates: exact-md5 groups, cacheCopy totals, per-workflow
             totals, optional per-sample BAM counts.

Every command goes through :func:`terra_scrub.snapshot.read_snapshot`, which
refuses a snapshot without a consistent ``__done__`` marker. ``stale`` prints a
WARNING to stderr if the snapshot has no usable snapshot_utc and it falls back
to the wall clock.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from terra_scrub.snapshot import read_snapshot
from terra_scrub.util import human, parse_ts

# default --sample-pattern: the sample id is the .bam basename up to its FIRST
# dot, so pipeline stage suffixes (e.g. .aligned.unsorted.duplicates_marked) are
# NOT part of it. Override with --sample-pattern when sample ids carry dots or
# sit inside a longer token (group 1 = sample id).
DEFAULT_SAMPLE_PATTERN = r"^([^.]+)"


# ---------------------------------------------------------------------------
# command: dupes
# ---------------------------------------------------------------------------

def run_dupes(args: argparse.Namespace) -> int:
    meta, objs = read_snapshot(args.snapshot)
    total = sum(o["size"] for o in objs)

    def seg(o, segs):
        return any(seg in o["name"].split("/") for seg in segs)

    # 1) exact-content duplicates by md5 (md5Hash is metadata, free)
    by_md5 = defaultdict(list)
    for o in objs:
        if o.get("md5Hash"):
            by_md5[o["md5Hash"]].append(o)
    md5_waste = 0
    md5_groups = []
    for md5, group in by_md5.items():
        if len(group) > 1:
            s = group[0]["size"]
            md5_waste += s * (len(group) - 1)
            md5_groups.append((s * (len(group) - 1), s, len(group), group))
    print(f"== duplicate analysis: gs://{meta['__bucket__']['name']} "
          f"({len(objs):,} objects, {human(total)}) ==")
    print("\n-- exact duplicates by md5 --")
    md5_n = sum(g[2] - 1 for g in md5_groups)
    print(f"   {len(md5_groups)} dup groups, {md5_n:,} redundant objects, {human(md5_waste)} redundant")
    for waste, s, n, group in sorted(md5_groups, key=lambda g: g[0], reverse=True)[:15]:
        print(f"   {human(s * (n - 1)):>12} redundant  {n}x {human(s)}  {group[0]['name']}")
        for g in group[1:][:5]:
            print(f"                       {g['name']}")

    # 2) same basename + size across different trees (near-dups incl. no-md5)
    by_bs = defaultdict(list)
    for o in objs:
        by_bs[(os.path.basename(o["name"]), o["size"])].append(o)
    bs_waste = 0
    bs_groups = []
    for (base, s), group in by_bs.items():
        if len(group) > 1 and s >= args.minbytes:
            bs_waste += s * (len(group) - 1)
            bs_groups.append((s * (len(group) - 1), base, s, len(group), group))
    print(f"\n-- same basename+size, >= {human(args.minbytes)} "
          f"(overlaps with md5 group above) --")
    bs_n = sum(g[3] - 1 for g in bs_groups)
    print(f"   {len(bs_groups)} groups, {bs_n:,} extra objects, {human(bs_waste)} extra bytes")
    for waste, base, s, n, group in sorted(bs_groups, key=lambda g: g[0], reverse=True)[:20]:
        subs = defaultdict(int)
        for g in group:
            parts = g["name"].split("/")
            subs[parts[1] if parts[0] == "submissions" and len(parts) > 1 else parts[0]] += 1
        top_sub = sorted(subs.items(), key=lambda x: -x[1])[:4]
        print(f"   {human(s * (n - 1)):>12} extra  {n}x {human(s)}  {base}  "
              f"subs: {top_sub}")

    # 3) cacheCopy segment
    cc = [o for o in objs if seg(o, ("cacheCopy",))]
    cc_bytes = sum(o["size"] for o in cc)
    print("\n-- objects under a cacheCopy/ segment --")
    print(f"   {len(cc):,} objects, {human(cc_bytes)} ({100.0 * cc_bytes / (total or 1):.1f}% of bucket)")
    print("   (caveat: a regular FILE named 'cacheCopy' would also match — a"
          " metadata-only listing cannot tell files from dirs)")
    ccw = defaultdict(int)
    for o in cc:
        parts = o["name"].split("/")
        ccw[parts[2] if len(parts) > 2 else "(?)"] += o["size"]
    for k, s in sorted(ccw.items(), key=lambda x: -x[1])[:10]:
        print(f"   {human(s):>12}  {k}")

    # 4) per workflow type (submissions/<id>/<Workflow>/...); require a real
    #    workflow segment so sibling files like <id>/workflow.logs are not
    #    counted as workflow "types"
    print("\n-- by workflow type --")
    wf = defaultdict(lambda: [0, 0])
    for o in objs:
        parts = o["name"].split("/")
        if parts[0] == "submissions" and len(parts) > 3:
            wf[parts[2]][0] += o["size"]
            wf[parts[2]][1] += 1
        elif parts[0] == "submissions" and len(parts) == 3:
            direct = "(direct under sub dir)"
            wf[direct][0] += o["size"]
            wf[direct][1] += 1
    for k, (s, c) in sorted(wf.items(), key=lambda x: -x[1][0]):
        print(f"   {human(s):>12}  {c:>8,}  {k}")
    other = sum(o["size"] for o in objs if o["name"].split("/")[0] != "submissions")
    print(f"   {human(other):>12}  (non-submissions: cram_crai/QC/notebooks/data_table)")

    # 5) per-sample: how many times was each sample's final BAM written?
    if args.sample:
        sam = defaultdict(lambda: [0, 0, set()])
        # see DEFAULT_SAMPLE_PATTERN: the token stops at the first dot/slash
        pat = re.compile(getattr(args, "sample_pattern", None) or DEFAULT_SAMPLE_PATTERN)
        for o in objs:
            if not o["name"].endswith(".bam"):
                continue
            m = pat.search(os.path.basename(o["name"]))
            if m:
                sid = m.group(1)
                sam[sid][0] += o["size"]
                sam[sid][1] += 1
                parts = o["name"].split("/")
                if parts[0] == "submissions" and len(parts) > 1:
                    sam[sid][2].add(parts[1])
        multi = [(sid, v) for sid, v in sam.items() if len(v[2]) > 1]
        print(f"\n-- samples with BAMs in >1 submission ({len(multi)} of {len(sam)}) --")
        print(f"   total across all: {human(sum(v[0] for v in sam.values()))}, "
              f"{sum(v[1] for v in sam.values()):,} BAMs, {len(sam)} samples")
        for sid, (s, c, subs) in sorted(multi, key=lambda x: -x[1][0])[:20]:
            print(f"   {human(s):>12}  {c:>3} BAMs  {len(subs)} subs  {sid}")
    return 0


def _count(seq, key):
    d = defaultdict(int)
    for x in seq:
        k = x.get(key, "?") if isinstance(x, dict) else str(x)
        d[k] += 1
    return d.items()


# ---------------------------------------------------------------------------
# command: report
# ---------------------------------------------------------------------------

def run_report(args: argparse.Namespace) -> int:
    meta, objs = read_snapshot(args.snapshot)
    total = sum(o["size"] for o in objs)
    print(f"== snapshot of gs://{meta['__bucket__']['name']} ==")
    print(f"   objects: {len(objs):,}   total: {human(total)}   "
          f"snapshot taken: {meta.get('snapshot_utc')}")
    ups = [parse_ts(o.get("updated")) for o in objs]
    ups = [u for u in ups if u]
    if ups:
        print(f"   last-modified range: {min(ups):%Y-%m-%d} .. {max(ups):%Y-%m-%d}")

    for depth in range(1, args.depth + 1):
        agg = defaultdict(lambda: [0, 0])
        for o in objs:
            parts = o["name"].split("/")
            key = "/".join(parts[:depth]) if len(parts) > depth else "(root)"
            agg[key][0] += o["size"]
            agg[key][1] += 1
        print(f"\n-- prefix sizes at depth {depth} --")
        print(f"{'size':>12}  {'objects':>8}  prefix")
        for k, (s, c) in sorted(agg.items(), key=lambda x: -x[1][0])[:40]:
            print(f"{human(s):>12}  {c:>8,}  {k}")

    top = sorted(objs, key=lambda o: -o["size"])[:args.top]
    print(f"\n-- top {len(top)} largest objects --")
    print(f"{'size':>12}  last-modified  object")
    for o in top:
        print(f"{human(o['size']):>12}  {str(o.get('updated'))[:10]}  {o['name']}")

    ext = defaultdict(lambda: [0, 0])
    for o in objs:
        base = o["name"].rsplit("/", 1)[-1]
        e = os.path.splitext(base)[1].lower() or "(none)"
        ext[e][0] += o["size"]
        ext[e][1] += 1
    print("\n-- by extension --")
    for e, (s, c) in sorted(ext.items(), key=lambda x: -x[1][0])[:20]:
        print(f"{human(s):>12}  {c:>8,}  {e}")
    return 0


# ---------------------------------------------------------------------------
# command: stale
# ---------------------------------------------------------------------------

def run_stale(args: argparse.Namespace) -> int:
    meta, objs = read_snapshot(args.snapshot)
    with open(args.terra) as f:
        tc = json.load(f)
    bucket = meta["__bucket__"]["name"]
    tc_bucket = tc.get("bucket")
    if not tc_bucket:
        raise SystemExit(f"terra context {args.terra} has no usable 'bucket' key "
                         f"(missing or null) — cannot verify it matches snapshot "
                         f"bucket '{bucket}'; refusing to mix unattributable context")
    if tc_bucket != bucket:
        raise SystemExit(f"terra context is for bucket '{tc_bucket}' but the snapshot is "
                         f"for '{bucket}' — refusing to mix contexts from different buckets")
    now = parse_ts(meta.get("snapshot_utc"))
    if now is None:
        now = datetime.now(UTC)
        print(f"WARNING: snapshot has no usable snapshot_utc; using wall clock "
              f"({now:%Y-%m-%d %H:%M}Z) as the age reference — ages below are approximate",
              file=sys.stderr)
    total = sum(o["size"] for o in objs)

    print(f"== staleness: gs://{bucket} (snapshot {meta.get('snapshot_utc')}, "
          f"terra context captured {tc.get('captured_utc')}) ==")
    print(f"   objects: {len(objs):,}   total: {human(total)}")

    # --- per top-level prefix: age profile ---------------------------------
    prof = defaultdict(lambda: {"n": 0, "bytes": 0, "oldest": None, "newest": None,
                                "old_n": 0, "old_bytes": 0})
    cutoff = None
    if args.days is not None:
        cutoff = now - timedelta(days=args.days)
    for o in objs:
        u = parse_ts(o.get("updated"))
        top1 = o["name"].split("/")[0] if "/" in o["name"] else "(root)"
        p = prof[top1]
        p["n"] += 1
        p["bytes"] += o["size"]
        if u:
            p["oldest"] = u if p["oldest"] is None or u < p["oldest"] else p["oldest"]
            p["newest"] = u if p["newest"] is None or u > p["newest"] else p["newest"]
            if cutoff and u < cutoff:
                p["old_n"] += 1
                p["old_bytes"] += o["size"]
    print(f"\n-- per top-level prefix (age vs {args.days}d) --")
    print(f"{'total':>12}  {'> N d old':>12}  {'last write':>10}  {'oldest':>10}  objs  prefix")
    for k, p in sorted(prof.items(), key=lambda x: -x[1]["bytes"]):
        old = f"{human(p['old_bytes'])} ({p['old_n']:,})" if args.days is not None else "-"
        print(f"{human(p['bytes']):>12}  {old:>12}  "
              f"{str(p['newest'])[:10] if p['newest'] else '-':>10}  "
              f"{str(p['oldest'])[:10] if p['oldest'] else '-':>10}  "
              f"{p['n']:>6,}  {k}")

    # --- second-level detail for big prefixes -------------------------------
    if args.depth2:
        # the three LARGEST prefixes by bytes (name order would silently skip
        # the largest once >=4 top-level prefixes clear the threshold)
        for big in sorted(prof, key=lambda k: -prof[k]["bytes"])[:3]:
            if prof[big]["bytes"] < total / 20:
                continue
            print(f"\n-- second level under {big}/ (largest first) --")
            agg = defaultdict(lambda: [0, 0, None])
            for o in objs:
                if not o["name"].startswith(big + "/"):
                    continue
                parts = o["name"].split("/")
                key = "/".join(parts[:2])
                a = agg[key]
                a[0] += o["size"]
                a[1] += 1
                u = parse_ts(o.get("updated"))
                a[2] = u if (a[2] is None or u > a[2]) else a[2]
            print(f"{'total':>12}  {'last write':>10}  objs  prefix")
            for k, (s, c, newest) in sorted(agg.items(), key=lambda x: -x[1][0])[:25]:
                print(f"{human(s):>12}  {str(newest)[:10] if newest else '-':>10}  {c:>6,}  {k}")

    # --- objects not referenced by any Terra attribute ----------------------
    ref = set(tc.get("referenced_gs_uris", []))
    unref = [o for o in objs if f"gs://{bucket}/{o['name']}" not in ref]
    unref_bytes = sum(o["size"] for o in unref)
    print("\n-- not referenced by any Terra workspace/entity attribute --")
    print(f"   {len(unref):,} objects, {human(unref_bytes)} "
          f"({100.0 * unref_bytes / (total or 1):.1f}% of bucket)")
    print("   (caveat: Cromwell exec-dir outputs are never in attributes, so this is")
    print("    a strong signal for top-level deliverables, not for submissions/ internals)")
    by_prefix = defaultdict(int)
    for o in unref:
        by_prefix[o["name"].split("/")[0] if "/" in o["name"] else "(root)"] += o["size"]
    for k, s in sorted(by_prefix.items(), key=lambda x: -x[1])[:10]:
        print(f"   {human(s):>12}  {k}/")

    # --- submission attribution --------------------------------------------
    subs = {s["submissionId"]: s for s in tc.get("submissions", [])}
    sub_bytes = defaultdict(int)
    sub_files = defaultdict(int)
    for o in objs:
        parts = o["name"].split("/")
        for idx in (1, 2):  # <sub_id>/... or submissions/<sub_id>/...
            if len(parts) > idx and parts[idx] in subs:
                sub_bytes[parts[idx]] += o["size"]
                sub_files[parts[idx]] += 1
                break
    print(f"\n-- per-submission footprint ({len(sub_bytes)} of {len(subs)} submissions have files) --")
    print(f"{'size':>12}  {'files':>6}  {'submitted':>10}  {'status':<12} {'config'}")
    for sid, s in sorted(sub_bytes.items(), key=lambda x: -x[1]):
        meta_s = subs.get(sid, {})
        print(f"{human(s):>12}  {sub_files[sid]:>6,}  "
              f"{str(meta_s.get('submissionDate', ''))[:10]:>10}  "
              f"{meta_s.get('status', '?')!s:<12} {meta_s.get('config', '?')}")
    orphan = [o for o in objs
              if not any(p in subs for p in o["name"].split("/")[:2])]
    orphan_bytes = sum(o["size"] for o in orphan)
    if orphan:
        op = defaultdict(int)
        for o in orphan:
            op[o["name"].split("/")[0] if "/" in o["name"] else "(root)"] += o["size"]
        print(f"\n   NOT under any KNOWN submission dir (unknown ids included): "
              f"{len(orphan):,} objs, {human(orphan_bytes)}")
        for k, s in sorted(op.items(), key=lambda x: -x[1])[:10]:
            print(f"   {human(s):>12}  {k}/")

    # --- non-submission objects, any depth (strong stale candidates) -------
    print(f"\n-- non-submission objects (any depth), newest-first, {args.top} shown --")
    tops = [o for o in objs
            # len==1 clause: a root object literally named "submissions" is NOT a
            # submission dir; without it that file would be excluded here.
            if o["name"].split("/")[0] != "submissions" or len(o["name"].split("/")) == 1]
    tops.sort(key=lambda o: o["updated"] or "", reverse=True)
    print(f"{'size':>12}  {'last-modified':>10}  referenced?  object")
    for o in tops[:args.top]:
        marker = "REF" if f"gs://{bucket}/{o['name']}" in ref else "   "
        print(f"{human(o['size']):>12}  {str(o.get('updated'))[:10]:>10}  {marker:<11}  {o['name']}")
    return 0


# ---------------------------------------------------------------------------
# argument wiring
# ---------------------------------------------------------------------------

def add_arguments_report(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("snapshot")
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--top", type=int, default=25)


def add_arguments_dupes(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("snapshot")
    parser.add_argument("--minbytes", type=int, default=10 * 2**30,
                        help="minimum object size for basename+size grouping (default 10 GiB)")
    parser.add_argument("--sample", action="store_true",
                        help="per-sample BAM analysis (sample tokens in .bam basenames)")
    parser.add_argument("--sample-pattern", default=DEFAULT_SAMPLE_PATTERN,
                        help="regex whose group 1 is the sample id in a .bam basename "
                             "(default %(default)s)")


def add_arguments_stale(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("snapshot")
    parser.add_argument("--terra", required=True)
    parser.add_argument("--days", type=int, default=180, help="age threshold (default 180)")
    parser.add_argument("--depth2", action="store_true",
                        help="show second level of biggest prefixes")
    parser.add_argument("--top", type=int, default=30)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="terra-scrub (analyze)",
                                 description="offline snapshot analysis")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, adder, runner, help_ in (
            ("report", add_arguments_report, run_report,
             "offline space report from a snapshot"),
            ("stale", add_arguments_stale, run_stale,
             "staleness report from snapshot + context"),
            ("dupes", add_arguments_dupes, run_dupes,
             "duplicate analysis from a snapshot")):
        p = sub.add_parser(name, help=help_)
        adder(p)
        p.set_defaults(_run=runner)
    args = ap.parse_args(argv)
    return int(args._run(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
