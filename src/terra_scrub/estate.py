"""estate -- drive `scan` and `plan` across every bucket in a set of Terra namespaces.

Both drivers are READ-ONLY against the cloud. They never call gsutil, gcloud or any
deleting thing: they shell out to terra-scrub itself
(``[sys.executable, "-m", "terra_scrub", <cmd>, ...]``) so every bucket gets process
isolation and its own log file. The commands they invoke are GET-only by
construction (see docs/SAFETY.md).

``estate scan`` -- capture + generate candidate lists
    Re-queries the workspace list LIVE every run (a pinned list goes stale: the
    set of workspaces moves day to day). Then, per bucket, strictly in this order,
    because the order is load-bearing and enforced downstream:

      1. snapshot     (object listing)                <- MUST be first
      2. context      (attrs + references + submissions; sees later pointers)
      3. candidates --max-snapshot-age 1              -> delete / review lists

    The listing precedes the Terra read so a pointer written in between cannot look
    unreferenced (see docs/SAFETY.md §2 (the ordering invariant)). Each bucket generates its
    lists immediately after its own context, so the capture-ordering guard in
    `candidates` is satisfied instead of argued around.

    Resumable: a bucket with a completed snapshot (``__done__`` marker), a context and
    a candidate list is skipped. Re-run after an interruption; a finished bucket is
    never overwritten. Give each capture its own dated ``--root`` so a re-scan never
    overwrites the capture an earlier number was computed from.

``estate plan`` -- per-bucket plan-time context + plan, for a whole run root
    Every plan needs its own Terra context captured AFTER its manifest's
    ``generated=`` stamp, or `plan` rightly refuses (a context older than the list
    would certify rather than check). Getting that ordering wrong by hand across many
    buckets is how a gate gets worked around, so this driver does it for you.
    Buckets with an empty delete list are skipped; a refusal is REPORTED, not hidden.

Run layout (``<root>``)::

    scope.json                              live workspace list for this run
    inv/<key>.jsonl                         snapshot
    inv/<key>.terra.json                    scan-time context
    inv/<key>.plan-time.terra.json          plan-time context (estate plan)
    cleanup/<key>/<bucket>.cleanup.tsv      delete list (+ .jsonl, .protected.tsv)
    cleanup/<key>/<bucket>.cleanup.tsv.plan.json / .plan.uris.txt / .plan.sh
    logs/scan-<key>.log, logs/plan-<key>.log
    scan-status.json
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import glob
import json
import os
import subprocess
import sys
import time

TS = [sys.executable, "-m", "terra_scrub"]


def _env():
    env = dict(os.environ)
    env.setdefault("PYTHONWARNINGS", "ignore")
    return env


def _ts(*args):
    return [*TS, *[str(a) for a in args]]


# ----------------------------------------------------------------- shared helpers

def has_done(path):
    """True iff the snapshot file ends with its ``__done__`` integrity marker."""
    if not os.path.exists(path):
        return False
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 400))
        tail = f.read().decode("utf-8", "replace")
    return '"__done__": true' in tail or '"__done__":true' in tail


def key_of(bucket):
    return bucket.replace("fc-secure-", "fc-")[:22]


def manifest_rows(path):
    n = 0
    with open(path) as f:
        for line in f:
            if not line.startswith("#"):
                n += 1
    return n


# ----------------------------------------------------------------- scan

def reference_flags(args):
    """--reference-list flags for the candidate runs, from a file of paths.

    Refuses to proceed if the list file or any path in it is missing: a reference
    list that quietly resolves to nothing turns G8/G9 off for the whole estate,
    which is the failure this function exists to prevent (docs/SAFETY.md § G8, G9).
    Relative paths inside the file resolve against the file's own directory.
    """
    if args.no_reference_lists:
        print("WARNING: --no-reference-lists: G8/G9 are OFF for this run", flush=True)
        return []
    if not args.reference_lists:
        sys.exit("REFUSING: no reference lists given.\n"
                 "pass --reference-lists <file>, or --no-reference-lists to run "
                 "without G8/G9 (and say so in the write-up)")
    path = os.path.abspath(args.reference_lists)
    if not os.path.exists(path):
        sys.exit(f"REFUSING: reference list file not found: {path}\n"
                 f"pass --reference-lists <file>, or --no-reference-lists to run "
                 f"without G8/G9 (and say so in the write-up)")
    base = os.path.dirname(path)
    with open(path) as f:
        paths = [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    resolved = [q if os.path.isabs(q) else os.path.join(base, q) for q in paths]
    missing = [q for q in resolved if not os.path.exists(q)]
    if missing:
        sys.exit(f"REFUSING: {len(missing)} reference list(s) missing, refusing to run "
                 f"with partial G8/G9 coverage: {missing[:5]}")
    if not resolved:
        sys.exit(f"REFUSING: {path} names no reference lists -- that is G8/G9 OFF; "
                 f"say so explicitly with --no-reference-lists")
    flags = []
    for q in resolved:
        flags += ["--reference-list", q]
    print(f"reference lists: {len(resolved)} file(s) -> G8/G9 active", flush=True)
    return flags


def policy_flags(args):
    """The owner's aborted-last-copy policy is OPT-IN per run."""
    if args.aborted_last_copy_deletable:
        print("OWNER POLICY: aborted last copies are deletable this run", flush=True)
        return ["--aborted-last-copy-deletable"]
    return []


def scope(root, namespaces, log_dir):
    """Re-query the estate live. Never reuse a pinned workspace list."""
    out = os.path.join(root, "scope.json")
    logp = os.path.join(log_dir, "scope.log")
    cmd = _ts("workspaces", *[x for ns in namespaces for x in ("--namespace", ns)],
              "--out", out)
    with open(logp, "w") as log:
        r = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, env=_env(), check=False)
    if r.returncode != 0 or not os.path.exists(out):
        sys.exit(f"REFUSING: workspace query failed (rc={r.returncode}) -- see {logp}")
    with open(out) as f:
        data = json.load(f)
    wanted = set(namespaces)
    return [w for w in data.get("workspaces", []) if w.get("namespace") in wanted]


class _Scan:
    def __init__(self, root, guard_flags):
        self.root = root
        self.inv = os.path.join(root, "inv")
        self.clean = os.path.join(root, "cleanup")
        self.logs = os.path.join(root, "logs")
        self.guard_flags = guard_flags
        self.t0 = time.time()

    def one(self, w):
        ns, name, bucket = w["namespace"], w["name"], w.get("bucketName")
        if not bucket:
            return {"workspace": name, "namespace": ns, "bucket": None,
                    "snapshot": "no-bucket"}
        key = key_of(bucket)
        snap = os.path.join(self.inv, f"{key}.jsonl")
        tc = os.path.join(self.inv, f"{key}.terra.json")
        cand_dir = os.path.join(self.clean, key)
        cand = os.path.join(cand_dir, f"{bucket}.cleanup.tsv")
        logp = os.path.join(self.logs, f"scan-{key}.log")
        out = {"workspace": name, "namespace": ns, "bucket": bucket, "key": key}
        if bucket.startswith("fc-secure-"):
            out.update(snapshot="skipped-secure", terra_context="skipped-secure",
                       candidates="skipped-secure")
            return out
        done_before = has_done(snap) and os.path.exists(tc) and os.path.exists(cand)
        if done_before:
            out.update(snapshot="skip", terra_context="skip", candidates="skip")
            return out
        tb = time.time()
        with open(logp, "w") as log:
            # 1. listing FIRST
            if has_done(snap):
                out["snapshot"] = "skip-existing"
            else:
                r = subprocess.run(_ts("snapshot", bucket, "--out", snap),
                                   stdout=log, stderr=subprocess.STDOUT, env=_env(), check=False)
                out["snapshot"] = "ok" if r.returncode == 0 else f"rc={r.returncode}"
            # 2. then the Terra context. A context left over from an interrupted run
            # may predate this listing; the capture-ordering guard would refuse it, so
            # it is re-captured AFTER the listing rather than reused.
            if out["snapshot"].startswith(("ok", "skip")):
                if os.path.exists(tc):
                    os.remove(tc)
                log.flush()
                r = subprocess.run(_ts("context", ns, name, "--out", tc),
                                   stdout=log, stderr=subprocess.STDOUT, env=_env(), check=False)
                out["terra_context"] = "ok" if r.returncode == 0 else f"rc={r.returncode}"
            else:
                out["terra_context"] = "not-run"
            # 3. then this bucket's own lists, against a snapshot that is minutes old
            if out["terra_context"] == "ok":
                log.flush()
                cr = subprocess.run(_ts("candidates", "--snapshot", snap, "--terra", tc,
                                        "--out-dir", cand_dir, "--max-snapshot-age", "1",
                                        *self.guard_flags),
                                    capture_output=True, text=True, env=_env(), check=False)
                log.write("\n# ---- candidates stdout\n" + (cr.stdout or ""))
                log.write("\n# ---- candidates stderr\n" + (cr.stderr or ""))
                out["candidates"] = "ok" if cr.returncode == 0 else f"rc={cr.returncode}"
                out["candidates_note"] = ((cr.stderr or "").strip()[-300:]
                                          if cr.returncode else "")
                for line in (cr.stdout or "").splitlines():
                    if "candidates:" in line or "PROTECTED review list" in line:
                        out.setdefault("summary", []).append(line.strip())
        out["secs"] = round(time.time() - tb, 1)
        out["elapsed_min"] = round((time.time() - self.t0) / 60.0, 1)
        print(f"[{out.get('snapshot', '?'):>13}/{out.get('terra_context', '?'):>3}/"
              f"{out.get('candidates', '--'):>3}] {out['secs']:>7.1f}s "
              f"(+{out['elapsed_min']:>5.1f}m) {name}", flush=True)
        return out


def run_scan(args):
    root = os.path.abspath(args.root or os.path.join(
        "runs", "run-" + time.strftime("%Y-%m-%d", time.gmtime())))
    namespaces = list(dict.fromkeys(args.namespace))
    print(f"run root: {root}", flush=True)
    guard_flags = reference_flags(args) + policy_flags(args)
    s = _Scan(root, guard_flags)
    for d in (s.inv, s.clean, s.logs):
        os.makedirs(d, exist_ok=True)
    ws = scope(root, namespaces, s.logs)
    print(f"scope: {len(ws)} workspace(s) in {len(namespaces)} namespace(s) "
          f"(re-queried live) | workers={args.workers} | out={s.inv}", flush=True)
    with cf.ThreadPoolExecutor(args.workers) as ex:
        rows = list(ex.map(s.one, ws))
    status = {"finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "total_minutes": round((time.time() - s.t0) / 60.0, 1),
              "namespaces": namespaces, "guard_flags": guard_flags, "rows": rows}
    with open(os.path.join(root, "scan-status.json"), "w") as f:
        json.dump(status, f, indent=1)
    bad = [r for r in rows
           if not (str(r.get("snapshot", "")).startswith(("ok", "skip"))
                   and str(r.get("terra_context", "")).startswith(("ok", "skip"))
                   and str(r.get("candidates", "")).startswith(("ok", "skip")))]
    print(f"\nDONE {len(rows)} workspaces in {status['total_minutes']} min | "
          f"not-clean: {len(bad)}")
    for r in bad:
        print("   ", {k: r.get(k) for k in ("workspace", "snapshot", "terra_context",
                                            "candidates", "candidates_note")})
    print(f"\nnext: review {os.path.join(root, 'cleanup')}, then "
          f"terra-scrub estate plan --root {root}")
    return 0 if not bad else 1


# ----------------------------------------------------------------- plan

def run_plan(args):
    root = os.path.abspath(args.root)
    only = [x for x in args.only.split(",") if x]
    excl = [x for x in args.exclude.split(",") if x]
    scope_path = os.path.join(root, "scope.json")
    if not os.path.exists(scope_path):
        sys.exit(f"REFUSING: {scope_path} not found -- is {root} a run root from "
                 f"`terra-scrub estate scan`?")
    with open(scope_path) as f:
        sc = json.load(f)
    ws_by_bucket = {w["bucketName"]: w for w in sc["workspaces"] if w.get("bucketName")}
    logs = os.path.join(root, "logs")
    os.makedirs(logs, exist_ok=True)

    jobs = []
    for tsv in sorted(glob.glob(os.path.join(root, "cleanup", "*", "*.cleanup.tsv"))):
        bucket = os.path.basename(tsv).replace(".cleanup.tsv", "")
        key = os.path.basename(os.path.dirname(tsv))
        if only and not any(bucket.startswith(o) or key.startswith(o) for o in only):
            continue
        if excl and any(bucket.startswith(e) or key.startswith(e) for e in excl):
            continue
        n = manifest_rows(tsv)
        if n < args.min_rows:
            print(f"[skip   ] {bucket[:14]}  {n} rows", flush=True)
            continue
        jobs.append((bucket, key, tsv, n))

    jobs.sort(key=lambda j: j[3])   # cheapest first: fail fast; the long tail runs last
    print(f"planning {len(jobs)} bucket(s), {sum(j[3] for j in jobs):,} rows total, "
          f"--workers {args.workers}\n", flush=True)
    results = []
    t0 = time.time()
    for bucket, key, tsv, n in jobs:
        ws = ws_by_bucket.get(bucket)
        if not ws:
            print(f"[NO-WS  ] {bucket[:14]}  not in scope.json -- skipped", flush=True)
            results.append((bucket, n, None, "no workspace in scope.json"))
            continue
        log = os.path.join(logs, f"plan-{key}.log")
        tb = time.time()
        # 1. context captured NOW, i.e. after the manifest's generated= stamp
        ctx = os.path.join(root, "inv", f"{key}.plan-time.terra.json")
        with open(log, "w") as lf:
            r = subprocess.run(_ts("context", ws["namespace"], ws["name"], "--out", ctx),
                               stdout=lf, stderr=subprocess.STDOUT, env=_env(), check=False)
        if r.returncode != 0:
            print(f"[CTX-RC ] {bucket[:14]}  context rc={r.returncode} -- see {log}",
                  flush=True)
            results.append((bucket, n, None, f"context rc={r.returncode}"))
            continue
        # 2. the plan
        with open(log, "a") as lf:
            r = subprocess.run(_ts("plan", "--manifest", tsv, "--terra", ctx,
                                   "--workers", args.workers),
                               stdout=lf, stderr=subprocess.STDOUT, env=_env(), check=False)
        pj = tsv + ".plan.json"
        if r.returncode != 0 or not os.path.exists(pj):
            print(f"[REFUSED] {bucket[:14]}  plan rc={r.returncode} -- see {log}", flush=True)
            results.append((bucket, n, None, f"REFUSED plan rc={r.returncode}"))
            continue
        with open(pj) as f:
            p = json.load(f)
        ok = p["executable"] and p["objects_by_status"].get("PLAN_DELETE") == n
        print(f"[{'ok     ' if ok else 'CHECK  '}] {bucket[:14]}  {n:>7,} rows  "
              f"{p['plan_bytes'] / 2**40:8.4f} TiB  plan {p['plan_id']}  "
              f"{time.time() - tb:6.1f}s  {'' if ok else p['objects_by_status']}",
              flush=True)
        results.append((bucket, n, p, None))

    print(f"\n{'plan_id':>14}  {'rows':>8}  {'TiB':>9}  exec  bucket", flush=True)
    tot_b = tot_n = 0
    for bucket, n, p, why in results:
        if not p:
            print(f"{'--':>14}  {n:>8,}  {'--':>9}  {'--':>4}  {bucket[:14]}  {why}",
                  flush=True)
            continue
        tot_b += p["plan_bytes"]
        tot_n += p["plan_objects"]
        print(f"{p['plan_id']:>14}  {p['plan_objects']:>8,}  {p['plan_bytes'] / 2**40:9.4f}  "
              f"{p['executable']!s:>4}  {bucket[:14]}", flush=True)
    print(f"\n{len([r for r in results if r[2]])} plan(s), {tot_n:,} objects, "
          f"{tot_b / 2**40:.3f} TiB, in {(time.time() - t0) / 60:.1f} min", flush=True)
    print(f"Arm one at a time (a human step):  terra-scrub approve --root {root} <plan_id>",
          flush=True)
    return 0 if all(r[2] for r in results) else 1


# ----------------------------------------------------------------- CLI

def add_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="estate_cmd", required=True, metavar="{scan,plan}")

    s = sub.add_parser("scan", help="per bucket: snapshot -> context -> candidates "
                                    "(read-only, resumable)")
    s.add_argument("--root", default=None,
                   help="run root (default: runs/run-<UTC date>). Use a new root per "
                        "capture; a finished bucket is never overwritten")
    s.add_argument("--namespace", action="append", required=True, metavar="NS",
                   help="Terra billing namespace to scan (repeatable, required)")
    s.add_argument("--workers", type=int, default=4, help="buckets in parallel (default 4)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--reference-lists", metavar="FILE", default=None,
                   help="file of paths (one per line, '#' comments) passed to every "
                        "candidates run as --reference-list (G8/G9). Refuses if the file "
                        "or any listed path is missing")
    g.add_argument("--no-reference-lists", action="store_true",
                   help="run WITHOUT G8/G9 (prints a WARNING; say so in the write-up)")
    s.add_argument("--aborted-last-copy-deletable", action="store_true",
                   help="OWNER POLICY passthrough to candidates: LAST_COPY rows under "
                        "Aborted/Failed submissions go to the delete list (G8/G9 still apply)")
    s.set_defaults(_estate_run=run_scan)

    p = sub.add_parser("plan", help="per bucket: fresh plan-time context -> plan")
    p.add_argument("--root", required=True, help="a run root from `estate scan`")
    p.add_argument("--workers", type=int, default=32,
                   help="plan's live-validation workers (default 32). Planning is "
                        "per-object, so buckets with many rows cost more wall-clock "
                        "than buckets with many bytes")
    p.add_argument("--only", default="", help="comma-separated bucket/key prefixes")
    p.add_argument("--exclude", default="", help="comma-separated bucket/key prefixes")
    p.add_argument("--min-rows", type=int, default=1,
                   help="skip delete lists with fewer rows (default 1)")
    p.set_defaults(_estate_run=run_plan)


def run(args: argparse.Namespace) -> int:
    return int(args._estate_run(args) or 0)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="terra-scrub estate", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(ap)
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
